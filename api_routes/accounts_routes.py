from __future__ import annotations

import html as html_lib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request

from account_hybrid import ACCOUNT_STORE, audit_event
from core import Account, account_launch_block_reason, cookie_identity_block_reason, flog_kv
from roblox_hybrid import HybridLauncher, resolve_vip_access_code, validate_cookie as hybrid_validate_cookie, validate_cookie_details
from services.process_service import ProcessManager

from .context import ApiContext
from .settings_state import _apply_game_defaults, _normalize_window_size_settings

APP_USER_AGENT = "ArgusLauncher/RT"
_AVATAR_CACHE: Dict[str, Tuple[float, str]] = {}
_AVATAR_CACHE_TTL = 300.0


def register(app, ctx: ApiContext) -> None:
    cfg_mgr = ctx.cfg_mgr
    farm = ctx.farm
    def _account_data_records(include_cookies: bool = False) -> List[Dict[str, Any]]:
        try:
            return ACCOUNT_STORE.read_records(include_cookies=include_cookies)
        except Exception as e:
            flog_kv("ACCOUNT_DATA", "read_failed", "warning", error=e)
            return []


    def _account_data_api_records() -> List[Dict[str, Any]]:
        records = _account_data_records(include_cookies=False)
        runtime_by_user = {
            str(account.username or "").strip().lower(): account
            for account in farm._accounts
            if str(account.username or "").strip()
        }
        result: List[Dict[str, Any]] = []
        for record in records:
            item = ACCOUNT_STORE.to_api_record(record)
            blocked_reason = cookie_identity_block_reason(
                str(item.get("username") or ""),
                str(item.get("cookie_username") or ""),
                bool(item.get("cookie_mismatch", False)),
            )
            runtime = runtime_by_user.get(str(item.get("username") or "").strip().lower())
            if runtime:
                runtime_snapshot = runtime.runtime_snapshot()
                runtime_blocked = account_launch_block_reason(runtime)
                if runtime_blocked:
                    blocked_reason = runtime_blocked
                item["state"] = runtime.state.name
                item["pid"] = runtime.pid
                item["runtime_state"] = runtime_snapshot.get("runtime_state") or str(runtime.runtime.lifecycle_state)
                item["can_rejoin"] = bool(farm.running and runtime.state.name != "FAILED" and not blocked_reason)
                item["can_kill"] = bool(runtime.pid)
                item["cookie_username"] = runtime.cookie_username or item.get("cookie_username", "")
                item["cookie_user_id"] = runtime.cookie_user_id or item.get("cookie_user_id", "")
                item["cookie_mismatch"] = bool(runtime.cookie_mismatch or item.get("cookie_mismatch", False))
            item["blocked_reason"] = blocked_reason
            item["launchable"] = not bool(blocked_reason)
            result.append(item)
        return result


    def _load_accounts_from_account_data() -> List[Account]:
        return [Account.from_dict(item) for item in ACCOUNT_STORE.to_roboguard_accounts()]


    def _replace_farm_accounts_from_store() -> int:
        new_accounts = _load_accounts_from_account_data()
        _apply_game_defaults(ctx, new_accounts, persist=False)
        was_running = farm.running
        if was_running:
            farm.stop()
            time.sleep(0.5)
        farm.set_accounts(new_accounts)
        cfg_mgr.save_accounts(new_accounts)
        if was_running:
            farm.start()
        return len(new_accounts)


    def _validate_cookie_records_from_store() -> Dict[str, Any]:
        records = ACCOUNT_STORE.read_records(include_cookies=True)
        kept: List[Dict[str, Any]] = []
        removed: List[Dict[str, str]] = []

        for record in records:
            username = str(record.get("username") or "").strip()
            cookie = str(record.get("cookie") or "").strip()
            label = username or "Unknown"
            if not cookie:
                removed.append({"username": label, "reason": "missing cookie"})
                continue

            try:
                ok, cookie_username, detail, meta = validate_cookie_details(cookie)
            except Exception as exc:
                raise RuntimeError(f"Cookie validation unavailable for {label}: {exc}") from exc

            if not ok:
                removed.append({"username": label, "reason": detail or "invalid cookie"})
                continue

            normalized = ACCOUNT_STORE.normalize_record(record)
            validated_username = str(meta.get("username") or cookie_username or "").strip()
            if not username and validated_username:
                normalized["username"] = validated_username
                username = validated_username
            normalized["cookie_username"] = validated_username
            normalized["cookie_user_id"] = str(meta.get("user_id") or "")
            normalized["cookie_mismatch"] = bool(
                validated_username
                and username
                and username.lower() != validated_username.lower()
            )
            normalized["import_status"] = "cookie_mismatch" if normalized["cookie_mismatch"] else ""
            kept.append(normalized)

        ACCOUNT_STORE.write_records(kept)
        for item in removed:
            audit_event("reload_cookie_removed", item.get("username", ""), False, reason=item.get("reason", ""))
        flog_kv(
            "ACCOUNT_DATA",
            "reload_cookie_validation",
            kept=len(kept),
            removed=len(removed),
            total=len(records),
        )
        return {
            "total": len(records),
            "kept": len(kept),
            "removed": len(removed),
            "removed_accounts": removed,
        }


    def _find_account_record(username: str, include_cookie: bool = True) -> Optional[Dict[str, Any]]:
        wanted = str(username or "").strip().lower()
        for record in _account_data_records(include_cookies=include_cookie):
            if str(record.get("username") or "").strip().lower() == wanted:
                return record
        return None


    def _import_cookie_validator(cookie: str):
        ok, username, detail, meta = validate_cookie_details(cookie)
        return ok, username, detail, meta


    def _global_launch_target(body: Dict[str, Any], record: Dict[str, Any]) -> Dict[str, Any]:
        target = dict(body or {})
        place_id = str(target.get("place_id") or cfg_mgr.get("game_place_id", "") or record.get("place_id") or "").strip()
        vip_links = list(record.get("vip_links") or [])
        if place_id:
            vip_links = [
                link for link in vip_links
                if not ProcessManager.parse_vip_link(str(link or "").strip())[0]
                or ProcessManager.parse_vip_link(str(link or "").strip())[0] == place_id
            ]
        target.setdefault("vip_links", vip_links)
        target["place_id"] = place_id
        global_vip = str(cfg_mgr.get("game_private_server_url", "") or "").strip()
        global_place = ProcessManager.parse_vip_link(global_vip)[0] if global_vip else ""
        target.setdefault("global_vip_link", global_vip if (not place_id or not global_place or global_place == place_id) else "")
        target.setdefault("auto_create_private_server_enabled", cfg_mgr.get("auto_create_private_server_enabled", False))
        target.setdefault("auto_create_private_server_free_only", cfg_mgr.get("auto_create_private_server_free_only", True))
        return target


    def _lookup_roblox_place(place_id: str) -> Dict[str, Any]:
        place = str(place_id or "").strip()
        if not place.isdigit():
            raise HTTPException(400, "place_id must be numeric")

        details: Dict[str, Any] = {}
        universe_id = ""
        image_url = ""

        def fetch_json(url: str) -> Any:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": f"Mozilla/5.0 {APP_USER_AGENT}", "Accept": "application/json, text/plain, */*"},
            )
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))

        try:
            universe_payload = fetch_json(f"https://apis.roblox.com/universes/v1/places/{place}/universe")
            if isinstance(universe_payload, dict):
                universe_id = str(universe_payload.get("universeId") or "")
            if universe_id:
                games_payload = fetch_json(
                    "https://games.roblox.com/v1/games?" + urllib.parse.urlencode({"universeIds": universe_id})
                )
                data = games_payload.get("data") if isinstance(games_payload, dict) else []
                if isinstance(data, list) and data:
                    details = data[0] if isinstance(data[0], dict) else {}
        except Exception:
            details = {}

        try:
            thumb_target = universe_id or place
            thumb_path = "games/icons" if universe_id else "places/gameicons"
            thumb_key = "universeIds" if universe_id else "placeIds"
            thumb_url = "https://thumbnails.roblox.com/v1/" + thumb_path + "?" + urllib.parse.urlencode(
                {
                    thumb_key: thumb_target,
                    "size": "150x150",
                    "format": "Png",
                    "isCircular": "false",
                }
            )
            payload = fetch_json(thumb_url)
            items = payload.get("data") if isinstance(payload, dict) else []
            if isinstance(items, list) and items:
                image_url = str(items[0].get("imageUrl") or "")
        except Exception:
            image_url = ""

        name = str(details.get("name") or details.get("sourceName") or "").strip()
        creator = details.get("creator") if isinstance(details.get("creator"), dict) else {}
        builder = str(
            details.get("builder")
            or details.get("creatorName")
            or creator.get("name")
            or ""
        ).strip()

        if not name:
            try:
                req = urllib.request.Request(
                    f"https://www.roblox.com/games/{place}",
                    headers={"User-Agent": f"Mozilla/5.0 {APP_USER_AGENT}", "Accept": "text/html, */*"},
                )
                with urllib.request.urlopen(req, timeout=8.0) as resp:
                    page = resp.read().decode("utf-8", errors="replace")
                title_match = re.search(r"<title>(.*?)</title>", page, flags=re.I | re.S)
                if title_match:
                    title = html_lib.unescape(re.sub(r"\s+", " ", title_match.group(1))).strip()
                    name = re.sub(r"\s*\|\s*Roblox\s*$", "", title, flags=re.I).strip()
                image_match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', page, flags=re.I)
                if image_match and not image_url:
                    image_url = html_lib.unescape(image_match.group(1)).strip()
            except Exception as exc:
                raise HTTPException(502, f"Roblox place lookup failed: {exc}")

        return {
            "ok": True,
            "place_id": place,
            "name": name or f"Place {place}",
            "builder": builder,
            "universe_id": universe_id,
            "image_url": image_url,
            "url": f"https://www.roblox.com/games/{place}",
        }

    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    @app.post("/api/account/{username}/save-cookie")
    def api_save_cookie(username: str):
        acc = next((a for a in farm._accounts if a.username == username), None)
        if not acc:
            raise HTTPException(404, "Account not found")
        if not str(acc.cookie or "").strip():
            return {"ok": False, "msg": "No cookie loaded for this account"}
        ok, cookie_username, detail, meta = validate_cookie_details(acc.cookie)
        if not ok:
            return {"ok": False, "msg": detail}
        mismatch = bool(cookie_username and acc.username.lower() != cookie_username.lower())
        if mismatch:
            ACCOUNT_STORE.update_record(
                username,
                {
                    "cookie_username": cookie_username,
                    "cookie_user_id": str(meta.get("user_id") or ""),
                    "cookie_mismatch": True,
                    "import_status": "cookie_mismatch",
                },
            )
            return {"ok": False, "msg": f"Cookie belongs to {cookie_username}, not {username}. Reimport the correct cookie."}
        ACCOUNT_STORE.upsert_records([acc.to_dict()])
        cfg_mgr.save_accounts(farm._accounts)
        return {"ok": True, "msg": f"Saved encrypted cookie to AccountData.json for {username}"}

    @app.get("/api/accounts")
    def api_get_accounts():
        return _account_data_api_records()


    @app.post("/api/accounts/reload")
    def api_reload_accounts():
        try:
            validation = _validate_cookie_records_from_store()
            count = _replace_farm_accounts_from_store()
        except Exception as e:
            raise HTTPException(400, f"Reload failed: {e}")
        removed = int(validation.get("removed") or 0)
        kept = int(validation.get("kept") or count)
        msg = f"Checked cookies: {kept} valid"
        if removed:
            msg += f", removed {removed} invalid"
        return {"ok": True, "count": count, "msg": msg, **validation}


    @app.get("/api/accounts/avatars")
    def api_account_avatars(user_ids: str = ""):
        ids: List[str] = []
        seen = set()
        for raw in re.split(r"[,\s]+", str(user_ids or "")):
            uid = raw.strip()
            if not uid.isdigit() or uid in seen:
                continue
            seen.add(uid)
            ids.append(uid)
            if len(ids) >= 100:
                break

        now = time.time()
        avatars: Dict[str, str] = {}
        missing: List[str] = []
        to_fetch: List[str] = []
        for uid in ids:
            cached = _AVATAR_CACHE.get(uid)
            if cached and (now - cached[0]) < _AVATAR_CACHE_TTL:
                avatars[uid] = cached[1]
            else:
                to_fetch.append(uid)

        if to_fetch:
            try:
                url = "https://thumbnails.roblox.com/v1/users/avatar-headshot?" + urllib.parse.urlencode({
                    "userIds": ",".join(to_fetch),
                    "size": "48x48",
                    "format": "Png",
                    "isCircular": "false",
                })
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": APP_USER_AGENT, "Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=8.0) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                for item in payload.get("data", []) if isinstance(payload, dict) else []:
                    uid = str(item.get("targetId") or "")
                    image_url = str(item.get("imageUrl") or "")
                    if uid and image_url:
                        avatars[uid] = image_url
                        _AVATAR_CACHE[uid] = (now, image_url)
            except Exception as exc:
                return {"ok": False, "avatars": avatars, "missing": to_fetch, "msg": str(exc)}

        for uid in ids:
            if uid not in avatars:
                missing.append(uid)
        return {"ok": True, "avatars": avatars, "missing": missing}


    @app.post("/api/accounts")
    async def api_set_accounts(request: Request):
        body = await request.json()
        if not isinstance(body, list):
            raise HTTPException(400, "Expected array")
        try:
            ACCOUNT_STORE.replace_from_roboguard_payload([dict(item) for item in body])
            count = _replace_farm_accounts_from_store()
        except Exception as e:
            raise HTTPException(400, f"Bad account payload: {e}")
        return {"ok": True, "count": count, "store": "AccountData.json"}


    @app.get("/api/accounts/export")
    def api_export_accounts():
        return {"ok": True, "accounts": _account_data_api_records(), "path": ACCOUNT_STORE.path}


    @app.post("/api/accounts/import")
    async def api_import_accounts(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected object")
        kind = str(body.get("kind") or body.get("type") or "auto").strip().lower()
        lines = body.get("lines") or body.get("text") or ""
        if isinstance(lines, str):
            line_list = [line.strip() for line in lines.splitlines() if line.strip()]
        elif isinstance(lines, list):
            line_list = [str(line).strip() for line in lines if str(line).strip()]
        else:
            line_list = []
        try:
            if kind in {"cookie", "cookies", "roblosecurity"}:
                result = ACCOUNT_STORE.import_cookie_lines(line_list, validator=_import_cookie_validator)
            elif kind in {"accountdata", "ram", "file"}:
                path = str(body.get("path") or "").strip()
                if not path or not os.path.exists(path):
                    raise HTTPException(400, "path not found")
                with open(path, "rb") as f:
                    records = ACCOUNT_STORE.decode_account_file_bytes(f.read())
                imported, merged = ACCOUNT_STORE.upsert_records(records)
                result = {"ok": True, "imported": imported, "count": len(merged)}
            elif kind in {"json", "accounts"} and isinstance(body.get("accounts"), list):
                imported, merged = ACCOUNT_STORE.upsert_records(body.get("accounts") or [])
                result = {"ok": True, "imported": imported, "count": len(merged)}
            else:
                raise HTTPException(400, "Unsupported import kind")
            count = _replace_farm_accounts_from_store()
            result["count"] = count
            return result
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, str(e))


    @app.post("/api/account/{username}/launch")
    async def api_launch_account(username: str, request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
        record = _find_account_record(username, include_cookie=True)
        if not record:
            raise HTTPException(404, "Account not found")
        blocked_reason = cookie_identity_block_reason(
            str(record.get("username") or username),
            str(record.get("cookie_username") or ""),
            bool(record.get("cookie_mismatch", False)),
        )
        if blocked_reason:
            result = {"ok": False, "fatal": True, "msg": blocked_reason, "blocked_reason": blocked_reason, "cookie_mismatch": True}
            audit_event("launch", username=username, ok=False, detail=blocked_reason, mode="blocked")
            return result
        multi_roblox = bool(body.get("multi_roblox", cfg_mgr.get("multi_roblox_enabled", True)))
        result = HybridLauncher.launch_record(record, target=_global_launch_target(body, record), multi_roblox=multi_roblox)
        audit_event("launch", username=username, ok=bool(result.get("ok")), detail=result.get("msg", ""), mode=result.get("mode", ""))
        if result.get("ok"):
            try:
                window_settings = _normalize_window_size_settings(ctx, {})
                if window_settings["enabled"]:
                    resize_result = ProcessManager.resize_roblox_windows(window_settings["width"], window_settings["height"])
                    result["window_resize"] = {
                        "ok": bool(resize_result.get("ok", True)),
                        "resized": int(resize_result.get("resized") or 0),
                        "count": int(resize_result.get("count") or 0),
                        "width": window_settings["width"],
                        "height": window_settings["height"],
                    }
            except Exception as exc:
                flog_kv("WINDOW", "manual_launch_resize_failed", "warning", account=username, error=str(exc))
            _replace_farm_accounts_from_store()
        return result


    @app.post("/api/account/{username}/kill-duplicate")
    def api_kill_duplicate(username: str):
        record = _find_account_record(username, include_cookie=False)
        if not record:
            raise HTTPException(404, "Account not found")
        tracker = str(record.get("browser_tracker_id") or "")
        result = HybridLauncher.kill_duplicate_instances(tracker)
        audit_event("kill_duplicate", username=username, ok=bool(result.get("ok")), killed=result.get("killed", []))
        return result

    @app.post("/api/account/{username}/test-vip")
    async def api_test_vip(username: str, request: Request):
        body = await request.json()
        vip_url = body.get("vip_url", "")
        if not vip_url:
            raise HTTPException(400, "vip_url required")
        place_id, link_code = ProcessManager.parse_vip_link(vip_url)
        if not place_id:
            return {"ok": False, "msg": "Cannot parse place_id"}
        if not link_code:
            return {"ok": False,
                    "msg": "âš  No linkCode found â€” this link will join a PUBLIC server, not VIP!"}
        resolved = {}
        record = _find_account_record(username, include_cookie=True)
        if record and record.get("cookie"):
            resolved = resolve_vip_access_code(str(record.get("cookie") or ""), vip_url)
        return {
            "ok":        True,
            "place_id":  place_id,
            "link_code": f"{link_code[:6]}...{link_code[-4:]}",
            "vip_resolved": bool(resolved.get("ok")) if resolved else False,
            "access_code_present": bool(resolved.get("access_code")) if resolved else False,
            "url":       f"roblox://experiences/start?placeId={place_id}&linkCode=***",
            "msg":       "âœ… VIP link valid" + (" and accessCode resolved" if resolved.get("ok") else ""),
        }


    @app.get("/api/game/place/{place_id}")
    def api_game_place(place_id: str):
        return _lookup_roblox_place(place_id)

    @app.post("/api/test-cookie")
    async def api_test_cookie(request: Request):
        body = await request.json()
        cookie = body.get("cookie", "")
        if not cookie:
            raise HTTPException(400, "cookie required")
        ok, username, detail, meta = validate_cookie_details(cookie)
        return {"ok": ok, "username": username if ok else "", "user_id": meta.get("user_id", "") if ok else "", "msg": detail if not ok else ""}

    @app.get("/api/vip-tracker/{username}")
    def api_vip_tracker(username: str):
        acc = next((a for a in farm._accounts if a.username == username), None)
        if not acc:
            raise HTTPException(404, "Account not found")
        if not acc._vip_tracker:
            return {"ok": False, "msg": "No VipTracker (no VIP links)"}
        return {"ok": True, "links": acc._vip_tracker.status()}


    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    #  WEB UI â€” Argus Launcher
    # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
