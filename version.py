from __future__ import annotations

import re
from typing import Tuple

# Single source of truth for the launcher version.
# Release tags must match: tag "v1.0.0" <-> APP_VERSION "1.0.0".
# The release workflow fails the build when they differ.
APP_VERSION = "1.0.0"
TAG_PREFIX = "v"

# Build tag baked in at release time (e.g. "v1.0.0-beta.3").
# The release workflow generates build_info.py before building the exe.
# Source runs have no such file and fall back to APP_VERSION.
try:
    from build_info import BUILD_TAG as _BUILD_TAG
except Exception:
    _BUILD_TAG = ""
BUILD_TAG = str(_BUILD_TAG or "").strip()


def app_display_version() -> str:
    """Version shown in the title bar and API. Prefers the baked build tag."""
    if BUILD_TAG:
        return strip_tag_prefix(BUILD_TAG)
    return APP_VERSION

# GitHub Releases location used by the in-app updater.
GITHUB_OWNER = "q0HtHHftAS"
GITHUB_REPO = "Roblox-Auto-Rejoin-windown"

# Asset names published by .github/workflows/release.yml. Keep in sync.
CHECKSUMS_ASSET = "checksums.txt"


def exe_asset_name(version: str = APP_VERSION) -> str:
    return f"CronusLauncher-{version}.exe"


def portable_asset_name(version: str = APP_VERSION) -> str:
    return f"CronusLauncher-{version}-portable.zip"


def tag_for_version(version: str = APP_VERSION) -> str:
    text = str(version or "").strip()
    if text.startswith(TAG_PREFIX):
        return text
    return f"{TAG_PREFIX}{text}"


def strip_tag_prefix(value: str) -> str:
    text = str(value or "").strip()
    if text.lower().startswith(TAG_PREFIX.lower()) and len(text) > 1:
        return text[1:].strip()
    return text


def parse_version_tuple(value: str) -> Tuple[int, ...]:
    parts = [int(item) for item in re.findall(r"\d+", str(value or ""))[:4]]
    return tuple(parts or [0])


def _split_prerelease(value: str) -> Tuple[str, str]:
    text = strip_tag_prefix(value)
    head, sep, tail = text.partition("-")
    if sep and tail.strip():
        return head.strip() or text, tail.strip()
    return text, ""


def compare_versions(left: str, right: str) -> int:
    """Return -1/0/1 when left is older/same/newer than right.

    A prerelease suffix (e.g. 1.0.0-beta.1) is older than the plain
    release with the same numbers, and two prereleases compare by
    their numeric parts first, then by suffix text.
    """
    left_head, left_pre = _split_prerelease(left)
    right_head, right_pre = _split_prerelease(right)
    left_tuple = parse_version_tuple(left_head)
    right_tuple = parse_version_tuple(right_head)
    width = max(len(left_tuple), len(right_tuple))
    left_padded = tuple(list(left_tuple) + [0] * (width - len(left_tuple)))
    right_padded = tuple(list(right_tuple) + [0] * (width - len(right_tuple)))
    if left_padded < right_padded:
        return -1
    if left_padded > right_padded:
        return 1
    if left_pre == right_pre:
        return 0
    if not left_pre:
        return 1
    if not right_pre:
        return -1
    left_pre_tuple = parse_version_tuple(left_pre)
    right_pre_tuple = parse_version_tuple(right_pre)
    pre_width = max(len(left_pre_tuple), len(right_pre_tuple))
    left_pre_padded = tuple(list(left_pre_tuple) + [0] * (pre_width - len(left_pre_tuple)))
    right_pre_padded = tuple(list(right_pre_tuple) + [0] * (pre_width - len(right_pre_tuple)))
    if left_pre_padded != right_pre_padded:
        return 1 if left_pre_padded > right_pre_padded else -1
    if left_pre < right_pre:
        return -1
    if left_pre > right_pre:
        return 1
    return 0


def is_newer_version(candidate: str, current: str = APP_VERSION) -> bool:
    return compare_versions(candidate, current) > 0


def releases_api_latest() -> str:
    return f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"


def releases_api_list(per_page: int = 20) -> str:
    count = max(1, min(int(per_page or 20), 100))
    return f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases?per_page={count}"


def releases_page_url() -> str:
    return f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases"


def release_tag_url(tag: str) -> str:
    return f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/tag/{tag}"
