from __future__ import annotations

import json
from typing import Any

LUA_EVENT_PAYLOAD_MAX_BYTES = 32 * 1024
LUA_EVENT_STRING_MAX_CHARS = 512
LUA_EVENT_MAX_DEPTH = 6
LUA_EVENT_MAX_LIST_LENGTH = 100


def _encode_payload(payload: Any) -> bytes | None:
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    except Exception:
        return None


def _check_dict(value: dict[Any, Any], depth: int, path: str) -> tuple[bool, str]:
    for key, item in value.items():
        key_text = str(key)
        if len(key_text) > LUA_EVENT_STRING_MAX_CHARS:
            return False, f"Lua event field name '{path}' exceeds {LUA_EVENT_STRING_MAX_CHARS} chars"
        ok, msg = _check_value(item, depth + 1, key_text)
        if not ok:
            return ok, msg
    return True, ""


def _check_list(value: list[Any], depth: int, path: str) -> tuple[bool, str]:
    if len(value) > LUA_EVENT_MAX_LIST_LENGTH:
        return False, f"Lua event list '{path}' exceeds {LUA_EVENT_MAX_LIST_LENGTH} items"
    for item in value:
        ok, msg = _check_value(item, depth + 1, path)
        if not ok:
            return ok, msg
    return True, ""


def _check_value(value: Any, depth: int, path: str) -> tuple[bool, str]:
    if depth > LUA_EVENT_MAX_DEPTH:
        return False, "Lua event payload exceeds nesting depth limit"
    if isinstance(value, str):
        if len(value) > LUA_EVENT_STRING_MAX_CHARS:
            return False, f"Lua event field '{path}' exceeds {LUA_EVENT_STRING_MAX_CHARS} chars"
        return True, ""
    if isinstance(value, dict):
        return _check_dict(value, depth, path)
    if isinstance(value, list):
        return _check_list(value, depth, path)
    return True, ""


def validate_lua_event_payload(payload: Any) -> tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "Expected Lua event object"
    encoded = _encode_payload(payload)
    if encoded is None:
        return False, "Lua event payload is not JSON serializable"
    if len(encoded) > LUA_EVENT_PAYLOAD_MAX_BYTES:
        return False, f"Lua event payload exceeds {LUA_EVENT_PAYLOAD_MAX_BYTES} bytes"
    return _check_value(payload, 0, "payload")


def lua_event_handler_error_response(acc: Any, event_name: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status_code": 500,
        "accepted": False,
        "event": event_name,
        "account": getattr(acc, "_config_username", ""),
        "msg": "Event processing failed, retry later",
    }
