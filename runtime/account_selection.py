from __future__ import annotations

from typing import Any, Iterable, List, Set


def _items(raw: Any) -> Iterable[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return raw.replace("\n", ",").replace(";", ",").split(",")
    if isinstance(raw, (list, tuple, set)):
        return [str(item or "") for item in raw]
    return [str(raw or "")]


def runtime_account_allowlist(cfg: dict | None) -> List[str]:
    seen: Set[str] = set()
    result: List[str] = []
    for item in _items((cfg or {}).get("runtime_account_allowlist")):
        text = str(item or "").strip()
        key = text.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def runtime_account_allowlist_keys(cfg: dict | None) -> Set[str]:
    return {item.lower() for item in runtime_account_allowlist(cfg)}


def account_identity_keys(account: Any) -> Set[str]:
    values = {
        getattr(account, "_config_username", ""),
        getattr(account, "username", ""),
        getattr(account, "alias", ""),
        getattr(account, "cookie_username", ""),
    }
    return {str(value or "").strip().lower() for value in values if str(value or "").strip()}


def is_runtime_account_selected(account: Any, cfg: dict | None) -> bool:
    allowed = runtime_account_allowlist_keys(cfg)
    return not allowed or bool(account_identity_keys(account) & allowed)


def runtime_account_filter_reason(account: Any, cfg: dict | None) -> str:
    if is_runtime_account_selected(account, cfg):
        return ""
    allowlist = ", ".join(runtime_account_allowlist(cfg))
    return f"Disabled by runtime account allowlist: {allowlist}"


def runtime_account_filter_blocked_keys(accounts: Iterable[Any], cfg: dict | None) -> Set[str]:
    return {
        str(getattr(account, "_config_username", getattr(account, "username", "")) or "")
        for account in accounts
        if not is_runtime_account_selected(account, cfg)
    }
