from __future__ import annotations

import os
import random
import re
import subprocess
import time
import urllib.parse
from typing import Any, Dict, Optional, Tuple

from core import Account, ServerType, account_launch_block_reason, flog, flog_kv
from services.browser_tracker import tracker_label

def parse_vip_link(vip_url: str) -> Tuple[str, str]:
    if not vip_url:
        return "", ""
    try:
        parsed = urllib.parse.urlparse(vip_url.strip())
        qs     = urllib.parse.parse_qs(parsed.query)
        m = re.search(r"/games/(\d+)", parsed.path)
        place_id = m.group(1) if m else qs.get("placeId", [""])[0]
        link_code = (
            qs.get("privateServerLinkCode", [""])[0] or
            qs.get("linkCode",              [""])[0] or
            qs.get("code",                  [""])[0]
        )
        if not place_id:
            flog("[VIP] Could not parse place_id from configured VIP link", "warning")
        if not link_code:
            flog("[VIP] No linkCode in configured VIP link", "warning")
        return place_id, link_code
    except Exception as e:
        flog(f"[VIP] parse error: {e}", "warning")
        return "", ""


class AccountLaunchService:
    """Boundary for manual account launch endpoints.

    Queue launch still belongs to LaunchController. This service keeps API
    routes from calling HybridLauncher or process window mutation directly.
    """

    @staticmethod
    def build_target(body: Dict[str, Any], record: Dict[str, Any], cfg_mgr: Any) -> Dict[str, Any]:
        target = dict(body or {})
        place_id = str(target.get("place_id") or cfg_mgr.get("game_place_id", "") or record.get("place_id") or "").strip()
        vip_links = list(record.get("vip_links") or [])
        if place_id:
            vip_links = [
                link for link in vip_links
                if not parse_vip_link(str(link or "").strip())[0]
                or parse_vip_link(str(link or "").strip())[0] == place_id
            ]
        target.setdefault("vip_links", vip_links)
        target["place_id"] = place_id
        global_vip = str(cfg_mgr.get("game_private_server_url", "") or "").strip()
        global_place = parse_vip_link(global_vip)[0] if global_vip else ""
        target.setdefault("global_vip_link", global_vip if (not place_id or not global_place or global_place == place_id) else "")
        target.setdefault("auto_create_private_server_enabled", cfg_mgr.get("auto_create_private_server_enabled", False))
        target.setdefault("auto_create_private_server_free_only", cfg_mgr.get("auto_create_private_server_free_only", True))
        return target

    @staticmethod
    def launch_record(
        record: Dict[str, Any],
        body: Dict[str, Any],
        cfg_mgr: Any,
        window_settings: Optional[Dict[str, Any]] = None,
        idempotency_key: str = "",
        body_hash: str = "",
    ) -> Dict[str, Any]:
        from roblox_hybrid import HybridLauncher

        username = str(record.get("username") or "")
        multi_roblox = bool(body.get("multi_roblox", cfg_mgr.get("multi_roblox_enabled", True)))
        result = HybridLauncher.launch_record(
            record,
            target=AccountLaunchService.build_target(body, record, cfg_mgr),
            multi_roblox=multi_roblox,
        )
        flog_kv(
            "LAUNCH",
            "manual_account_launch",
            account=username,
            ok=bool(result.get("ok")),
            mode=result.get("mode", ""),
            detail=result.get("msg", ""),
            launch_action="manual_account_launch",
            idempotency_key=idempotency_key,
            idempotency_body_hash=body_hash,
        )
        if result.get("ok") and window_settings and (window_settings.get("enabled") or window_settings.get("arrange_enabled")):
            try:
                from services.process_service import ProcessService

                if window_settings.get("arrange_enabled"):
                    resize_result = ProcessService.arrange_roblox_windows(
                        int(window_settings["width"]),
                        int(window_settings["height"]),
                        int(window_settings.get("arrange_columns") or 6),
                        int(window_settings.get("arrange_gap") or 0),
                        int(window_settings.get("arrange_margin") or 0),
                        unlock_size=bool(window_settings.get("unlock_size_enabled", True)),
                        resize=bool(window_settings.get("enabled")),
                        rows=int(window_settings.get("arrange_rows") or 4),
                        reason="manual_account_launch",
                        idempotency_key=idempotency_key,
                    )
                else:
                    resize_result = ProcessService.resize_roblox_windows(
                        int(window_settings["width"]),
                        int(window_settings["height"]),
                        unlock_size=bool(window_settings.get("unlock_size_enabled", True)),
                        reason="manual_account_launch",
                        idempotency_key=idempotency_key,
                    )
                result["window_resize"] = {
                    "ok": bool(resize_result.get("ok", True)),
                    "resized": int(resize_result.get("resized") or 0),
                    "arranged": int(resize_result.get("arranged") or 0),
                    "count": int(resize_result.get("count") or 0),
                    "width": window_settings["width"],
                    "height": window_settings["height"],
                }
            except Exception as exc:
                flog_kv("WINDOW", "manual_launch_resize_failed", "warning", account=username, error=str(exc))
        return result

    @staticmethod
    def kill_duplicate_instances(
        record: Dict[str, Any],
        idempotency_key: str = "",
        body_hash: str = "",
    ) -> Dict[str, Any]:
        from roblox_hybrid import HybridLauncher

        username = str(record.get("username") or "")
        tracker = str(record.get("browser_tracker_id") or "")
        result = HybridLauncher.kill_duplicate_instances(tracker)
        flog_kv(
            "LAUNCH",
            "manual_kill_duplicate",
            account=username,
            ok=bool(result.get("ok")),
            killed=result.get("killed", []),
            launch_action="kill_duplicate_instances",
            idempotency_key=idempotency_key,
            idempotency_body_hash=body_hash,
        )
        return result

def build_launch_url(cls, acc: Account) -> Tuple[str, ServerType, str]:
    use_public_fallback = bool(
        acc.place_id and
        acc.vip_links and
        int(acc.launch_fail_count or 0) >= 2
    )
    if acc.vip_links and hasattr(acc, '_vip_tracker') and acc._vip_tracker:
        vip_url = acc._vip_tracker.pick()
    elif acc.vip_links:
        vip_url = acc.active_vip or random.choice(acc.vip_links)
    else:
        vip_url = ""

    if vip_url and not use_public_fallback:
        place_id, link_code = cls.parse_vip_link(vip_url)
        if not place_id and link_code and acc.place_id:
            place_id = acc.place_id
        if place_id and link_code:
            url = (
                f"roblox://experiences/start"
                f"?placeId={place_id}"
                f"&linkCode={link_code}"
                f"&launchData="
            )
            acc.active_vip = vip_url
            return url, ServerType.VIP, vip_url
        elif place_id and not link_code:
            acc.active_vip = ""
            url = f"roblox://experiences/start?placeId={place_id}"
            return url, ServerType.PUBLIC, ""

    if use_public_fallback:
        acc.launch_strategy = "public_fallback"
    elif vip_url:
        acc.launch_strategy = "vip_preferred"
    else:
        acc.launch_strategy = "public_only"

    if acc.place_id:
        url = f"roblox://experiences/start?placeId={acc.place_id}"
        return url, ServerType.PUBLIC, ""

    return "", ServerType.UNKNOWN, ""

def launch(cls, acc: Account) -> Tuple[bool, str, str]:
    block_reason = account_launch_block_reason(acc)
    if block_reason:
        flog(f"[LAUNCH] Blocked for {acc.display_name}: {block_reason}", "warning")
        return False, block_reason, ""

    if str(getattr(acc, "cookie", "") or "").strip():
        try:
            from roblox_hybrid import HybridLauncher

            target_place = str(acc.place_id or "")
            auto_private_enabled = bool(cls.AUTO_CREATE_PRIVATE_SERVER_ENABLED)
            active_vip = str(acc.active_vip or "") if auto_private_enabled else ""
            if target_place and active_vip:
                active_place, _active_code = cls.parse_vip_link(active_vip)
                if active_place and active_place != target_place:
                    active_vip = ""
            vip_links = list(acc.vip_links or []) if auto_private_enabled else []
            if target_place:
                vip_links = [
                    link for link in vip_links
                    if not cls.parse_vip_link(str(link or "").strip())[0]
                    or cls.parse_vip_link(str(link or "").strip())[0] == target_place
                ]
            global_vip = cls.GLOBAL_VIP_LINK if auto_private_enabled else ""
            global_place = cls.parse_vip_link(global_vip)[0] if global_vip else ""
            if target_place and global_place and global_place != target_place:
                global_vip = ""
            target = {
                "place_id": target_place,
                "vip_links": vip_links,
                "vip_link": active_vip,
                "global_vip_link": global_vip,
                "browser_tracker_id": getattr(acc, "browser_tracker_id", ""),
                "auto_create_private_server_enabled": auto_private_enabled,
                "auto_create_private_server_free_only": bool(cls.AUTO_CREATE_PRIVATE_SERVER_FREE_ONLY),
            }
            record = {
                "username": acc.username,
                "alias": acc.alias,
                "cookie": acc.cookie,
                "place_id": target_place,
                "vip_links": vip_links,
                "global_vip_link": global_vip,
                "browser_tracker_id": getattr(acc, "browser_tracker_id", ""),
                "auto_create_private_server_enabled": auto_private_enabled,
                "auto_create_private_server_free_only": bool(cls.AUTO_CREATE_PRIVATE_SERVER_FREE_ONLY),
            }
            result = HybridLauncher.launch_record(record, target=target, multi_roblox=bool(cls.MULTI_ROBLOX_ENABLED))
            if result.get("ok"):
                acc.browser_tracker_id = str(result.get("browser_tracker_id") or getattr(acc, "browser_tracker_id", "") or "")
                flog_kv(
                    "LAUNCH",
                    "browser_tracker_assigned",
                    account=acc.display_name,
                    browser_tracker_id=tracker_label(acc.browser_tracker_id),
                    mode=str(result.get("mode") or ""),
                )
                mode = str(result.get("mode") or "")
                attempted_vip_hybrid = str(result.get("attempted_vip") or "")
                if mode == "vip":
                    attempted_vip_hybrid = attempted_vip_hybrid or str(acc.active_vip or "")
                    acc.server_type = ServerType.VIP
                    acc.active_vip = attempted_vip_hybrid
                elif mode in {"job", "public"}:
                    acc.server_type = ServerType.PUBLIC
                    acc.active_vip = ""
                acc.last_launch_at = time.time()
                return True, str(result.get("msg") or "Launched via auth ticket"), attempted_vip_hybrid
            flog(f"[LAUNCH] Auth-ticket launch failed for {acc.display_name}: {result.get('msg')}", "warning")
            if result.get("fatal") or bool(cls.MULTI_ROBLOX_ENABLED):
                return False, str(result.get("msg") or "Auth-ticket launch blocked"), ""
        except Exception as e:
            flog(f"[LAUNCH] Auth-ticket launch path errored for {acc.display_name}: {e}", "warning")
            if bool(cls.MULTI_ROBLOX_ENABLED):
                return False, str(e), ""

    url, server_type, attempted_vip = cls.build_launch_url(acc)
    if not url:
        return False, "ไม่มี place_id หรือ VIP link ที่ถูกต้อง", ""

    acc.server_type = server_type
    safe_url = re.sub(
        r"([?&](?:privateServerLinkCode|linkCode|code|accessCode|reservedServerAccessCode)=)[^&\s]+",
        r"\1<redacted>",
        url,
        flags=re.IGNORECASE,
    )
    flog(f"[LAUNCH] {acc.display_name} → {safe_url[:120]}")

    try:
        os.startfile(cls.LOGIN_WARMUP_URL)
        flog(f"[LAUNCH] Warmup home for {acc.display_name}")
        time.sleep(cls.LOGIN_WARMUP_DELAY)
    except Exception as e:
        flog(f"[LAUNCH] warmup startfile failed: {e}", "warning")

    try:
        os.startfile(url)
        acc.last_launch_at = time.time()
        return True, url, attempted_vip
    except Exception as e:
        flog(f"[LAUNCH] os.startfile failed: {e} — trying subprocess fallback", "warning")
        try:
            subprocess.Popen(
                f'start "" "{url}"',
                shell=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
            )
            acc.last_launch_at = time.time()
            return True, url, attempted_vip
        except Exception as e2:
            flog(f"[LAUNCH] all methods failed for {acc.display_name}: {e2}", "warning")
            return False, str(e2), attempted_vip
