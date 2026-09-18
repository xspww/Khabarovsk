from __future__ import annotations

"""Notify-only app update check (opencode-style).

The old self-updater (download + exe swap via a visible batch script) is
gone on purpose: spawning console/curl windows looked like malware.
This module only *checks* GitHub Releases and reports. Failures are
silent, exactly like opencode's upgrade(): never annoy the user.
"""

import json
import os
import shutil
import urllib.request
from typing import Any, Dict

from version import (
    app_display_version,
    is_newer_version,
    release_tag_url,
    releases_api_latest,
    strip_tag_prefix,
)

HTTP_TIMEOUT_SECONDS = 20.0
UPDATE_USER_AGENT = f"CronusLauncher-Update/{app_display_version()}"
LEGACY_STAGE_DIRNAME = "update_stage"


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


def check_app_update() -> Dict[str, Any]:
    """Return {current_version, latest_version, latest_url, update_available}.

    Never raises and never downloads anything. Any failure (offline,
    rate-limited, unexpected payload) just reports no update available.
    """
    current = app_display_version()
    base: Dict[str, Any] = {
        "ok": True,
        "current_version": current,
        "latest_version": "",
        "latest_url": "",
        "update_available": False,
        "assets": [],
    }
    try:
        payload = _api_get_json(releases_api_latest())
        if not isinstance(payload, dict) or payload.get("draft"):
            return base
        tag = str(payload.get("tag_name") or "").strip()
        raw_assets = payload.get("assets")
        if isinstance(raw_assets, list):
            base["assets"] = [
                {"name": str(item.get("name") or ""),
                 "browser_download_url": str(item.get("browser_download_url") or "")}
                for item in raw_assets
                if isinstance(item, dict)
            ]
        if not tag or not is_newer_version(tag, current):
            return base
        latest = strip_tag_prefix(tag)
        base.update(
            latest_version=latest,
            latest_url=str(payload.get("html_url") or release_tag_url(tag)),
            update_available=True,
        )
        return base
    except Exception:
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
