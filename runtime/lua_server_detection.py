from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from domain.games import _text

_VIP_TYPES = {"VIP", "PRIVATE", "PRIVATE_SERVER"}
_PUBLIC_TYPES = {"PUBLIC", "STANDARD"}


@dataclass(frozen=True)
class LuaServerDetection:
    observed: bool = False
    server_type: str = ""
    is_vip: bool = False
    private_server_id: str = ""
    private_server_owner_id: str = ""
    place_id: str = ""
    job_id: str = ""
    universe_id: str = ""


def _boolish(value: Any) -> bool | None:
    text = _text(value).lower()
    if text in {"1", "true", "yes", "on", "vip", "private", "private_server"}:
        return True
    if text in {"0", "false", "no", "off", "public", "standard"}:
        return False
    return None


def _first_payload_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value:
            return _text(value)
    return ""


def _vip_flag(payload: dict[str, Any]) -> bool | None:
    value = payload.get("is_vip_server")
    if value in (None, ""):
        value = payload.get("is_private_server")
    return _boolish(value)


def _is_vip_server(
    private_server_id: str,
    owner_is_private: bool,
    explicit_vip: bool | None,
    server_type: str,
) -> bool:
    return bool(
        private_server_id
        or owner_is_private
        or explicit_vip is True
        or server_type in _VIP_TYPES
    )


def _has_public_signal(explicit_vip: bool | None, server_type: str) -> bool:
    return bool(explicit_vip is False or server_type in _PUBLIC_TYPES)


def detect_lua_server(payload: dict[str, Any]) -> LuaServerDetection:
    private_server_id = _first_payload_text(payload, "private_server_id", "privateServerId")
    private_server_owner_id = _first_payload_text(payload, "private_server_owner_id", "privateServerOwnerId")
    server_type = _first_payload_text(payload, "server_type", "observed_server_type").upper()
    explicit_vip = _vip_flag(payload)
    place_id = _text(payload.get("place_id"))
    job_id = _text(payload.get("job_id"))
    universe_id = _text(payload.get("universe_id"))

    owner_is_private = private_server_owner_id not in {"", "0"}
    if _is_vip_server(private_server_id, owner_is_private, explicit_vip, server_type):
        return LuaServerDetection(
            observed=True,
            server_type="VIP",
            is_vip=True,
            private_server_id=private_server_id,
            private_server_owner_id=private_server_owner_id,
            place_id=place_id,
            job_id=job_id,
            universe_id=universe_id,
        )

    if _has_public_signal(explicit_vip, server_type):
        return LuaServerDetection(
            observed=True,
            server_type="PUBLIC",
            is_vip=False,
            private_server_owner_id=private_server_owner_id,
            place_id=place_id,
            job_id=job_id,
            universe_id=universe_id,
        )

    return LuaServerDetection(place_id=place_id, job_id=job_id, universe_id=universe_id)
