from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


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


def _text(value: Any) -> str:
    return str(value or "").strip()


def _boolish(value: Any) -> bool | None:
    text = _text(value).lower()
    if text in {"1", "true", "yes", "on", "vip", "private", "private_server"}:
        return True
    if text in {"0", "false", "no", "off", "public", "standard"}:
        return False
    return None


def detect_lua_server(payload: Dict[str, Any]) -> LuaServerDetection:
    private_server_id = _text(payload.get("private_server_id") or payload.get("privateServerId"))
    private_server_owner_id = _text(payload.get("private_server_owner_id") or payload.get("privateServerOwnerId"))
    server_type = _text(payload.get("server_type") or payload.get("observed_server_type")).upper()
    vip_value = payload.get("is_vip_server")
    if vip_value in (None, ""):
        vip_value = payload.get("is_private_server")
    explicit_vip = _boolish(vip_value)
    place_id = _text(payload.get("place_id"))
    job_id = _text(payload.get("job_id"))
    universe_id = _text(payload.get("universe_id"))

    owner_is_private = private_server_owner_id not in {"", "0"}
    if private_server_id or owner_is_private or explicit_vip is True or server_type in _VIP_TYPES:
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

    has_public_signal = explicit_vip is False or server_type in _PUBLIC_TYPES
    if has_public_signal:
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
