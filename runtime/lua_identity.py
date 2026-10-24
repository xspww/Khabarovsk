from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, NamedTuple

from domain.games import _text


def _lower_set(values: Iterable[Any]) -> set[str]:
    return {str(value).strip().lower() for value in values if str(value or "").strip()}


def _pid(value: Any) -> int | None:
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
    pid: int | None
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
    candidates: tuple[str, ...] = ()
    bound_pid: int | None = None
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


def normalize_lua_identity(payload: dict[str, Any]) -> LuaIdentity:
    return LuaIdentity(
        account=_text(payload.get("account")),
        username=_text(payload.get("username") or payload.get("player_name") or payload.get("name")),
        configured_account=_text(payload.get("configured_account") or payload.get("account_hint")),
        user_id=_text(payload.get("user_id") or payload.get("userid") or payload.get("player_user_id")),
        pid=_pid(payload.get("pid") or payload.get("process_id") or payload.get("roblox_pid")),
        place_id=_text(payload.get("place_id")),
        job_id=_text(payload.get("job_id")),
    )


def _account_identity(acc: Any) -> tuple[set[str], set[str]]:
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


def _bound_pid(acc: Any) -> int | None:
    lock = getattr(acc, "_lock", None)
    if lock:
        with lock:
            return _pid(getattr(acc, "pid", None))
    return _pid(getattr(acc, "pid", None))


class _IdentityKeys(NamedTuple):
    username: str
    account_name: str
    configured: str
    user_id: str
    allow_configured_hint: bool


def _identity_keys(identity: LuaIdentity) -> _IdentityKeys:
    username = (identity.username or identity.account).lower()
    account_name = identity.account.lower()
    configured = identity.configured_account.lower()
    user_id = identity.user_id.lower()
    allow_configured_hint = not username and not account_name and not user_id
    return _IdentityKeys(
        username,
        account_name,
        configured,
        user_id,
        allow_configured_hint,
    )


def _identity_score(acc: Any, keys: _IdentityKeys) -> tuple[int, str]:
    names, user_ids = _account_identity(acc)
    score = 0
    reasons: list[str] = []
    if keys.user_id and keys.user_id in user_ids:
        score += 100
        reasons.append("user_id")
    if keys.username and keys.username in names:
        score += 80
        reasons.append("username")
    if keys.account_name and keys.account_name in names:
        score += 60
        reasons.append("account")
    if keys.allow_configured_hint and keys.configured and keys.configured in names:
        score += 10
        reasons.append("configured_account")
    return score, "+".join(reasons)


def _account_key(acc: Any) -> str:
    return str(
        getattr(acc, "_config_username", "") or getattr(acc, "username", "")
    )


def _ambiguous_resolution(
    identity: LuaIdentity,
    top_score: int,
    top: list[tuple[int, str, Any]],
) -> LuaAccountResolution:
    return LuaAccountResolution(
        identity=identity,
        matched=False,
        ambiguous=True,
        match_reason="ambiguous_identity",
        score=top_score,
        candidates=tuple(_account_key(item[2]) for item in top),
    )


def _matched_resolution(
    identity: LuaIdentity,
    match: tuple[int, str, Any],
    matches: list[tuple[int, str, Any]],
) -> LuaAccountResolution:
    score, reason, acc = match
    bound = _bound_pid(acc)
    pid_match = True
    if identity.pid is not None:
        pid_match = bool(bound and bound == identity.pid)
    return LuaAccountResolution(
        identity=identity,
        account=acc,
        matched=True,
        match_reason=reason,
        score=score,
        candidates=tuple(_account_key(item[2]) for item in matches[:5]),
        bound_pid=bound,
        pid_match=pid_match,
    )


def _pick_resolution(
    identity: LuaIdentity,
    matches: list[tuple[int, str, Any]],
) -> LuaAccountResolution:
    matches.sort(key=lambda item: item[0], reverse=True)
    top_score = matches[0][0]
    top = [item for item in matches if item[0] == top_score]
    if len(top) > 1:
        return _ambiguous_resolution(identity, top_score, top)
    return _matched_resolution(identity, matches[0], matches)


def resolve_lua_account(accounts: Iterable[Any], payload: dict[str, Any]) -> LuaAccountResolution:
    identity = normalize_lua_identity(payload)
    keys = _identity_keys(identity)
    matches: list[tuple[int, str, Any]] = []
    for acc in accounts:
        score, reason = _identity_score(acc, keys)
        if score:
            matches.append((score, reason, acc))
    if not matches:
        return LuaAccountResolution(
            identity=identity,
            matched=False,
            match_reason="not_found",
        )
    return _pick_resolution(identity, matches)


def lua_event_requires_pid_guard(event_name: str) -> bool:
    return str(event_name or "").strip().lower() in {
        "disconnect",
        "error_code",
        "loaded",
        "in_game",
        "teleport_error",
        "rejoin_requested",
        "finished",
        "mark_finished",
    }
