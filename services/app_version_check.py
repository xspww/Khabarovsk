from __future__ import annotations

"""Notify-only app update check (opencode-style).

The old self-updater (download + exe swap via a visible batch script) is
gone on purpose: spawning console/curl windows looked like malware.
This module only *checks* GitHub Releases and reports. Failures are
silent, exactly like opencode's upgrade(): never annoy the user.
"""

import json
import os
import re
import shutil
import urllib.request
from typing import Any, Dict

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

# Last check failure, kept in memory so the dashboard can show WHY the
# update button is missing instead of failing silently for hours.
_LAST_CHECK_ERROR = ""
_LAST_CHECK_AT = 0.0

# Short TTL cache: every dashboard open, focus and update-status poll calls
# check_app_update(), and each uncached call is a live GitHub round-trip.
# Serve the last snapshot instantly instead of stalling page opens.
_CHECK_CACHE_AT = 0.0
_CHECK_CACHE_OK = False
_CHECK_CACHE_SNAP: Dict[str, Any] = {}
_CHECK_CACHE_TTL_OK = 180.0
_CHECK_CACHE_TTL_FAIL = 30.0
_RELEASE_TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def _cached_check_snapshot() -> Dict[str, Any] | None:
    try:
        import time as _time

        age = float(_time.time()) - float(_CHECK_CACHE_AT or 0.0)
    except Exception:
        return None
    ttl = _CHECK_CACHE_TTL_OK if _CHECK_CACHE_OK else _CHECK_CACHE_TTL_FAIL
    if age < 0 or age >= ttl or not isinstance(_CHECK_CACHE_SNAP, dict):
        return None
    try:
        snap = dict(_CHECK_CACHE_SNAP)
        assets = snap.get("assets")
        if isinstance(assets, list):
            snap["assets"] = [dict(item) for item in assets if isinstance(item, dict)]
        return snap
    except Exception:
        return None


def _store_check_snapshot(snap: Dict[str, Any], ok: bool) -> None:
    global _CHECK_CACHE_AT, _CHECK_CACHE_OK, _CHECK_CACHE_SNAP
    try:
        import time as _time

        _CHECK_CACHE_AT = float(_time.time())
        _CHECK_CACHE_OK = bool(ok)
        stored = dict(snap)
        assets = stored.get("assets")
        if isinstance(assets, list):
            stored["assets"] = [dict(item) for item in assets if isinstance(item, dict)]
        _CHECK_CACHE_SNAP = stored
    except Exception:
        pass


def _api_get_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": UPDATE_USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        body = response.read()
    return json.loads(body.decode("utf-8", errors="replace"))


def _note_check_error(reason: str) -> None:
    global _LAST_CHECK_ERROR, _LAST_CHECK_AT
    try:
        import time as _time

        _LAST_CHECK_ERROR = str(reason or "")[:300]
        _LAST_CHECK_AT = float(_time.time())
    except Exception:
        pass


def _assets_from_payload(payload: Dict[str, Any]) -> list:
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


def _pick_newest_stable(payloads: Any, current: str) -> Dict[str, Any]:
    """Return the newest stable release newer than current, or {}."""
    best: Dict[str, Any] = {}
    if not isinstance(payloads, list):
        return best
    for item in payloads:
        if not _is_usable_release(item):
            continue
        tag = str(item.get("tag_name") or "").strip()
        if not tag or not is_newer_version(tag, current):
            continue
        if not best:
            best = item
            continue
        try:
            if compare_versions(tag, str(best.get("tag_name") or "")) > 0:
                best = item
        except Exception:
            continue
    return best if isinstance(best, dict) else {}


def check_app_update() -> Dict[str, Any]:
    """Return {current_version, latest_version, latest_url, update_available}.

    Never raises and never downloads anything. Any failure (offline,
    rate-limited, unexpected payload) just reports no update available,
    but records the reason in check_error so the UI can show it.

    Select the highest semantic version among the newest 100 stable
    releases. Fall back to /releases/latest only if listing fails.
    Results are cached (success 180s, failure 30s) to avoid stalling
    dashboard opens with a live GitHub round-trip every time.
    """
    global _LAST_CHECK_ERROR
    current = app_display_version()
    hit = _cached_check_snapshot()
    if hit is not None:
        # Refresh the version stamp: the binary never changes under us, but
        # a cheap copy keeps the cached path identical to a live one.
        hit["current_version"] = current
        # Keep check_error in sync with the latest known error so the UI
        # does not show a stale error after a successful cached hit.
        hit["check_error"] = _LAST_CHECK_ERROR if not hit.get("update_available") else ""
        return hit
    base: Dict[str, Any] = {
        "ok": True,
        "current_version": current,
        "latest_version": "",
        "latest_url": "",
        "update_available": False,
        "assets": [],
        "check_error": _LAST_CHECK_ERROR,
    }
    # Select by semantic version from the release list, not GitHub's
    # manually assignable "latest" pointer or publication order.
    try:
        payloads = _api_get_json(releases_api_list(100))
        if not isinstance(payloads, list):
            raise RuntimeError("bad response from GitHub releases list")
        best = _pick_newest_stable(payloads, current)
        if best:
            tag = str(best.get("tag_name") or "").strip()
            base["assets"] = _assets_from_payload(best)
            base.update(
                latest_version=strip_tag_prefix(tag),
                latest_url=str(best.get("html_url") or release_tag_url(tag)),
                update_available=True,
            )
        _LAST_CHECK_ERROR = ""
        base["check_error"] = ""
        _store_check_snapshot(base, True)
        return base
    except Exception as exc:
        detail = str(exc) or "connection failed"
        if "403" in detail:
            detail += " (GitHub rate limit - retry in a few minutes)"
        list_error = f"GitHub release list failed: {detail}"

    # If listing releases is unavailable, try GitHub's designated latest
    # stable release so an API outage does not hide every possible update.
    try:
        payload = _api_get_json(releases_api_latest())
        if _is_usable_release(payload):
            tag = str(payload.get("tag_name") or "").strip()
            if is_newer_version(tag, current):
                base["assets"] = _assets_from_payload(payload)
                base.update(
                    latest_version=strip_tag_prefix(tag),
                    latest_url=str(payload.get("html_url") or release_tag_url(tag)),
                    update_available=True,
                )
                _LAST_CHECK_ERROR = ""
                base["check_error"] = ""
                _store_check_snapshot(base, True)
                return base
        raise RuntimeError("latest release is not newer or is not a stable release")
    except Exception as exc:
        latest_error = str(exc) or "latest release lookup failed"
        _note_check_error(f"{list_error} | latest fallback failed: {latest_error}")
        base["check_error"] = _LAST_CHECK_ERROR
        _store_check_snapshot(base, False)
        return base


def cleanup_legacy_update_stage() -> None:
    """One-way removal of leftovers from the deleted (<1.2.0) self-updater."""
    try:
        from app_paths import APP_DATA_DIR

        stage = os.path.join(APP_DATA_DIR, LEGACY_STAGE_DIRNAME)
        if os.path.isdir(stage):
            shutil.rmtree(stage, ignore_errors=True)
    except Exception:
        pass
