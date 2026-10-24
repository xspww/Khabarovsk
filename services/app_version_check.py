"""Notify-only app update check (opencode-style).

The old self-updater (download + exe swap via a visible batch script) is
gone on purpose: spawning console/curl windows looked like malware.
This module only *checks* GitHub Releases and reports. Failures are
silent, exactly like opencode's upgrade(): never annoy the user.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import urllib.request
from typing import Any

from infra import net_tls
from version import (
    app_display_version,
    compare_versions,
    is_newer_version,
    release_tag_url,
    releases_api_latest,
    releases_api_list,
    strip_tag_prefix,
)

HTTP_TIMEOUT_SECONDS = 20.0
UPDATE_USER_AGENT = f"CronusLauncher-Update/{app_display_version()}"
LEGACY_STAGE_DIRNAME = "update_stage"
_CHECK_CACHE_TTL_OK = 180.0
_CHECK_CACHE_TTL_FAIL = 30.0
_RELEASE_TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


class _UpdateCheckState:
    """Last error plus the short TTL snapshot cache, held off module globals."""

    def __init__(self) -> None:
        # Last check failure, kept in memory so the dashboard can show WHY the
        # update button is missing instead of failing silently for hours.
        self.last_error = ""
        self.last_error_at = 0.0
        # Short TTL cache: every dashboard open, focus and update-status poll
        # calls check_app_update(), and each uncached call is a live GitHub
        # round-trip. Serve the last snapshot instantly instead of stalling.
        self.cache_at = 0.0
        self.cache_ok = False
        self.cache_snapshot: dict[str, Any] = {}

    def cached_snapshot(self) -> dict[str, Any] | None:
        try:
            import time as _time

            age = float(_time.time()) - float(self.cache_at or 0.0)
        except Exception:
            return None
        ttl = _CHECK_CACHE_TTL_OK if self.cache_ok else _CHECK_CACHE_TTL_FAIL
        if age < 0 or age >= ttl or not isinstance(self.cache_snapshot, dict):
            return None
        try:
            snap = dict(self.cache_snapshot)
            assets = snap.get("assets")
            if isinstance(assets, list):
                snap["assets"] = [dict(item) for item in assets if isinstance(item, dict)]
            return snap
        except Exception:
            return None

    def store(self, snap: dict[str, Any], ok: bool) -> None:
        try:
            import time as _time

            self.cache_at = float(_time.time())
            self.cache_ok = bool(ok)
            stored = dict(snap)
            assets = stored.get("assets")
            if isinstance(assets, list):
                stored["assets"] = [dict(item) for item in assets if isinstance(item, dict)]
            self.cache_snapshot = stored
        except Exception:
            "intentional: ignore exception"

    def note_error(self, reason: str) -> None:
        try:
            import time as _time

            self.last_error = str(reason or "")[:300]
            self.last_error_at = float(_time.time())
        except Exception:
            "intentional: ignore exception"


_STATE = _UpdateCheckState()


def _cached_check_snapshot() -> dict[str, Any] | None:
    return _STATE.cached_snapshot()


def _store_check_snapshot(snap: dict[str, Any], ok: bool) -> None:
    _STATE.store(snap, ok)


def _note_check_error(reason: str) -> None:
    _STATE.note_error(reason)


def _api_get_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": UPDATE_USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    # Frozen Python builds may not have a usable machine-wide CA store on
    # clean Windows installations, so every HTTPS call in the app goes
    # through the one shared context in net_tls (certifi plus the Windows
    # root store). See that module for why the trust store cannot be left
    # to ssl.create_default_context().
    with urllib.request.urlopen(
        request, timeout=HTTP_TIMEOUT_SECONDS, context=net_tls.default_context()
    ) as response:
        body = response.read()
    return json.loads(body.decode("utf-8", errors="replace"))


def _assets_from_payload(payload: dict[str, Any]) -> list:
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        return []
    assets = []
    for item in raw_assets:
        if not isinstance(item, dict):
            continue
        try:
            size = max(0, int(item.get("size") or 0))
        except (TypeError, ValueError):
            size = 0
        assets.append({
            "name": str(item.get("name") or ""),
            "browser_download_url": str(item.get("browser_download_url") or ""),
            "size": size,
        })
    return assets


def _is_usable_release(payload: Any) -> bool:
    """Stable, published release only: skip drafts and prereleases."""
    if not isinstance(payload, dict):
        return False
    if payload.get("draft"):
        return False
    if payload.get("prerelease"):
        return False
    tag = str(payload.get("tag_name") or "").strip()
    return bool(tag and _RELEASE_TAG_RE.fullmatch(tag))


def _release_tag(payload: dict[str, Any]) -> str:
    return str(payload.get("tag_name") or "").strip()


def _release_url(payload: dict[str, Any], tag: str) -> str:
    return str(payload.get("html_url") or release_tag_url(tag))


def _apply_release(base: dict[str, Any], payload: dict[str, Any], tag: str) -> None:
    base["assets"] = _assets_from_payload(payload)
    base.update(
        latest_version=strip_tag_prefix(tag),
        latest_url=_release_url(payload, tag),
        update_available=True,
    )


def _release_candidate(item: Any, current: str) -> str:
    """Return the release tag when item is a usable newer stable release."""
    if not _is_usable_release(item):
        return ""
    tag = _release_tag(item)
    if not tag or not is_newer_version(tag, current):
        return ""
    return tag


def _newer_than(tag: str, best: dict[str, Any]) -> bool:
    try:
        return compare_versions(tag, str(best.get("tag_name") or "")) > 0
    except Exception:
        return False


def _pick_newest_stable(payloads: Any, current: str) -> dict[str, Any]:
    """Return the newest stable release newer than current, or {}."""
    if not isinstance(payloads, list):
        return {}
    best: dict[str, Any] = {}
    for item in payloads:
        tag = _release_candidate(item, current)
        if not tag:
            continue
        if not best:
            best = item
            continue
        if _newer_than(tag, best):
            best = item
    return best


def _apply_release_list(base: dict[str, Any], current: str) -> str:
    """Fill base from the releases list; return an error string on failure."""
    try:
        payloads = _api_get_json(releases_api_list(100))
        if not isinstance(payloads, list):
            raise RuntimeError("bad response from GitHub releases list")  # noqa: TRY004 - external service failure, not a type error
        best = _pick_newest_stable(payloads, current)
        if best:
            _apply_release(base, best, _release_tag(best))
        _STATE.last_error = ""
        base["check_error"] = ""
        _store_check_snapshot(base, True)
        return ""
    except Exception as exc:
        detail = str(exc) or "connection failed"
        if "403" in detail:
            detail += " (GitHub rate limit - retry in a few minutes)"
        return f"GitHub release list failed: {detail}"


def _apply_latest_release(base: dict[str, Any], current: str, list_error: str) -> dict[str, Any]:
    try:
        payload = _api_get_json(releases_api_latest())
        if _is_usable_release(payload) and is_newer_version(_release_tag(payload), current):
            _apply_release(base, payload, _release_tag(payload))
            _STATE.last_error = ""
            base["check_error"] = ""
            _store_check_snapshot(base, True)
            return base
        raise RuntimeError("latest release is not newer or is not a stable release")
    except Exception as exc:
        latest_error = str(exc) or "latest release lookup failed"
        _note_check_error(f"{list_error} | latest fallback failed: {latest_error}")
        base["check_error"] = _STATE.last_error
        _store_check_snapshot(base, False)
        return base


def check_app_update() -> dict[str, Any]:
    """Return {current_version, latest_version, latest_url, update_available}.

    Never raises and never downloads anything. Any failure (offline,
    rate-limited, unexpected payload) just reports no update available,
    but records the reason in check_error so the UI can show it.

    Select the highest semantic version among the newest 100 stable
    releases. Fall back to /releases/latest only if listing fails.
    Results are cached (success 180s, failure 30s) to avoid stalling
    dashboard opens with a live GitHub round-trip every time.
    """
    current = app_display_version()
    hit = _cached_check_snapshot()
    if hit is not None:
        # Refresh the version stamp: the binary never changes under us, but
        # a cheap copy keeps the cached path identical to a live one.
        hit["current_version"] = current
        # Keep check_error in sync with the latest known error so the UI
        # does not show a stale error after a successful cached hit.
        hit["check_error"] = _STATE.last_error if not hit.get("update_available") else ""
        return hit
    base: dict[str, Any] = {
        "ok": True,
        "current_version": current,
        "latest_version": "",
        "latest_url": "",
        "update_available": False,
        "assets": [],
        "check_error": _STATE.last_error,
    }
    # Select by semantic version from the release list, not GitHub's
    # manually assignable "latest" pointer or publication order.
    list_error = _apply_release_list(base, current)
    if not list_error:
        return base
    # If listing releases is unavailable, try GitHub's designated latest
    # stable release so an API outage does not hide every possible update.
    return _apply_latest_release(base, current, list_error)


def cleanup_legacy_update_stage() -> None:
    """One-way removal of leftovers from the deleted (<1.2.0) self-updater."""
    try:
        from infra.app_paths import APP_DATA_DIR

        stage = os.path.join(APP_DATA_DIR, LEGACY_STAGE_DIRNAME)
        if os.path.isdir(stage):
            shutil.rmtree(stage, ignore_errors=True)
    except Exception:
        "intentional: ignore exception"
