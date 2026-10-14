"""Backward-compatible facade for the roblox_hybrid split.

The 1662-line module was divided by concern; new code should import from
the canonical modules directly:

- services.roblox_http (RobloxHTTP client, headers, launch URIs)
- services.roblox_moderation (live moderation / ban lookups)
- services.roblox_identity (offline cookie identity, dead-cookie resolve)
- services.roblox_validation (validate_cookie_details, record wrapper)
- services.roblox_multibox (multi-roblox guard subprocess)
- services.roblox_launcher (HybridLauncher orchestration)

Everything below is a re-export so existing `from services.roblox_hybrid
import ...` sites keep working unchanged.
"""

from __future__ import annotations

from domain.roblox_private_servers import (
    _safe_hash,
    build_owned_private_server_link,
    build_place_launcher_url,
    build_roblox_player_uri,
    fetch_private_server_metadata,
    game_name_for_universe,
    list_my_private_servers,
    list_private_servers_for_place,
    parse_vip_components,
    parse_vip_link,
    private_servers_enabled_for_universe,
    remembered_vip_unavailable,
    resolve_vip_access_code,
    universe_id_for_place,
    vip_unavailable_permanently,
)
from services.roblox_http import (
    AUTH_BASE,
    MODERATION_HEADERS,
    PUBLIC_HEADERS,
    ROBLOX_HOME,
    USER_AGENT,
    USERMODERATION_BASE,
    USERS_BASE,
    RobloxHTTP,
    _roblox_version_exe,
    open_roblox_uri,
    set_selected_roblox_version,
)
from services.roblox_identity import (
    cookie_embedded_identity,
    moderated_only_detail,
    reprobe_moderation_response,
    resolve_ban_for_dead_cookie,
    resolve_moderated_dead_cookie,
    username_for_cookie_in_store,
)
from services.roblox_launcher import (
    HybridLauncher,
    _ensure_owned_private_server,
    _merge_owned_private_server,
    ensure_owned_private_server,
)
from services.roblox_moderation import (
    _describe_moderation,
    fetch_ban_status_by_username,
    fetch_moderation_status,
    fetch_user_ban_status,
    fetch_user_id_by_username,
)
from services.roblox_multibox import (
    _multi_roblox_log,
    _parse_multi_roblox_ready_line,
    _read_guard_ready_line,
    _terminate_multi_roblox_helper_locked,
    ensure_multi_roblox_guard,
    multi_roblox_guard_status,
    record_multi_roblox_guard_failure,
    release_multi_roblox_guard,
)
from services.roblox_validation import (
    validate_cookie,
    validate_cookie_details,
    validate_record_cookie_identity,
)

__all__ = [
    "AUTH_BASE",
    "MODERATION_HEADERS",
    "PUBLIC_HEADERS",
    "ROBLOX_HOME",
    "USERMODERATION_BASE",
    "USERS_BASE",
    "USER_AGENT",
    "HybridLauncher",
    "RobloxHTTP",
    "_describe_moderation",
    "_ensure_owned_private_server",
    "_merge_owned_private_server",
    "_multi_roblox_log",
    "_parse_multi_roblox_ready_line",
    "_read_guard_ready_line",
    "_roblox_version_exe",
    "_safe_hash",
    "_terminate_multi_roblox_helper_locked",
    "build_owned_private_server_link",
    "build_place_launcher_url",
    "build_roblox_player_uri",
    "cookie_embedded_identity",
    "ensure_multi_roblox_guard",
    "ensure_owned_private_server",
    "fetch_ban_status_by_username",
    "fetch_moderation_status",
    "fetch_private_server_metadata",
    "fetch_user_ban_status",
    "fetch_user_id_by_username",
    "game_name_for_universe",
    "list_my_private_servers",
    "list_private_servers_for_place",
    "moderated_only_detail",
    "multi_roblox_guard_status",
    "open_roblox_uri",
    "parse_vip_components",
    "parse_vip_link",
    "private_servers_enabled_for_universe",
    "record_multi_roblox_guard_failure",
    "release_multi_roblox_guard",
    "remembered_vip_unavailable",
    "reprobe_moderation_response",
    "resolve_ban_for_dead_cookie",
    "resolve_moderated_dead_cookie",
    "resolve_vip_access_code",
    "set_selected_roblox_version",
    "universe_id_for_place",
    "username_for_cookie_in_store",
    "validate_cookie",
    "validate_cookie_details",
    "validate_record_cookie_identity",
    "vip_unavailable_permanently",
]
