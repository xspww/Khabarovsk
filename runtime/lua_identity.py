from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower_set(values: Iterable[Any]) -> set[str]:
    return {str(value).strip().lower() for value in values if str(value or "").strip()}


def _pid(value: Any) -> Optional[int]:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


@dataclass(frozen=True)
class LuaIdentity:
    account: str
    username: str
    configured_account: str
    user_id: str
    pid: Optional[int]
    place_id: str
    job_id: str


@dataclass(frozen=True)
class LuaAccountResolution:
    identity: LuaIdentity
    account: Any = None
    matched: bool = False
    ambiguous: bool = False
    match_reason: str = ""
    score: int = 0
    candidates: Tuple[str, ...] = ()
    bound_pid: Optional[int] = None
    pid_match: bool = True

    @property
    def account_key(self) -> str:
        if not self.account:
            return ""
        return str(getattr(self.account, "_config_username", "") or getattr(self.account, "username", "") or "")

    @property
    def display_name(self) -> str:
        if not self.account:
            return ""
        return str(getattr(self.account, "display_name", "") or self.account_key)


def normalize_lua_identity(payload: Dict[str, Any]) -> LuaIdentity:
    return LuaIdentity(
        account=_text(payload.get("account")),
        username=_text(payload.get("username") or payload.get("player_name") or payload.get("name")),
        configured_account=_text(payload.get("configured_account") or payload.get("account_hint")),
        user_id=_text(payload.get("user_id") or payload.get("userid") or payload.get("player_user_id")),
        pid=_pid(payload.get("pid") or payload.get("process_id") or payload.get("roblox_pid")),
        place_id=_text(payload.get("place_id")),
        job_id=_text(payload.get("job_id")),
    )


def _account_identity(acc: Any) -> Tuple[set[str], set[str]]:
    names = _lower_set(
        [
            getattr(acc, "_config_username", ""),
            getattr(acc, "username", ""),
            getattr(acc, "cookie_username", ""),
            getattr(acc, "alias", ""),
            getattr(acc, "display_name", ""),
        ]
    )
    user_ids = _lower_set(
        [
            getattr(acc, "user_id", ""),
            getattr(acc, "cookie_user_id", ""),
        ]
    )
    return names, user_ids


def _bound_pid(acc: Any) -> Optional[int]:
    lock = getattr(acc, "_lock", None)
    if lock:
        with lock:
            return _pid(getattr(acc, "pid", None))
    return _pid(getattr(acc, "pid", None))


def resolve_lua_account(accounts: Iterable[Any], payload: Dict[str, Any]) -> LuaAccountResolution:
    identity = normalize_lua_identity(payload)
    username = (identity.username or identity.account).lower()
    account_name = identity.account.lower()
    configured = identity.configured_account.lower()
    user_id = identity.user_id.lower()
    allow_configured_hint = not username and not account_name and not user_id
    matches: List[Tuple[int, str, Any]] = []

    for acc in accounts:
        names, user_ids = _account_identity(acc)
        score = 0
        reasons: List[str] = []
        if user_id and user_id in user_ids:
            score += 100
            reasons.append("user_id")
        if username and username in names:
            score += 80
            reasons.append("username")
        if account_name and account_name in names:
            score += 60
            reasons.append("account")
        if allow_configured_hint and configured and configured in names:
            score += 10
            reasons.append("configured_account")
        if score:
            matches.append((score, "+".join(reasons), acc))

    if not matches:
        return LuaAccountResolution(identity=identity, matched=False, match_reason="not_found")

    matches.sort(key=lambda item: item[0], reverse=True)
    top_score = matches[0][0]
    top = [item for item in matches if item[0] == top_score]
    if len(top) > 1:
        return LuaAccountResolution(
            identity=identity,
            matched=False,
            ambiguous=True,
            match_reason="ambiguous_identity",
            score=top_score,
            candidates=tuple(str(getattr(item[2], "_config_username", "") or getattr(item[2], "username", "")) for item in top),
        )

    _score, reason, acc = matches[0]
    bound = _bound_pid(acc)
    pid_match = True
    if identity.pid is not None:
        pid_match = bool(bound and bound == identity.pid)
    return LuaAccountResolution(
        identity=identity,
        account=acc,
        matched=True,
        match_reason=reason,
        score=_score,
        candidates=tuple(str(getattr(item[2], "_config_username", "") or getattr(item[2], "username", "")) for item in matches[:5]),
        bound_pid=bound,
        pid_match=pid_match,
    )


def lua_event_requires_pid_guard(event_name: str) -> bool:
    return str(event_name or "").strip().lower() in {
        "disconnect",
        "error_code",
        "teleport_error",
        "rejoin_requested",
    }
