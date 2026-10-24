const lower = e => String(e || "").toLowerCase(), num = e => Number(e || 0) || 0;
function truthState(e) { return lower(e?.runtime_truth?.truth_state) }
function truthReasons(e) { return Array.isArray(e?.runtime_truth?.reasons) ? e.runtime_truth.reasons.join(" ") : "" }
function statusText(e) { return lower([e?.recovery_status, e?.last_recovery_reason, e?.last_state_reason, e?.last_crash_reason, e?.watchdog_classification, e?.health_flags, e?.recovery_step, e?.blocked_reason, e?.manual_status, e?.import_status, e?.last_error, truthState(e), truthReasons(e)].filter(Boolean).join(" ")) }
function isRuntimeSuspect(e) { return "suspect" === truthState(e) }

const LOCKED_KEYWORDS = [
  "verify your identity",
  "verify it's you",
  "verify its you",
  "verify it is you",
  "verify you are",
  "verify you're",
  "verify you\u2019re",
  "identity verification",
  "age verification",
  "id verification",
  "verify your age",
  "verify age",
  "face scan",
  "scan your face",
  "face verification",
  "selfie verification",
  "selfie check",
  "liveness verification",
  "liveness check",
  "government id",
  "government-issued",
  "upload id",
  "submit id",
  "id document",
  "provide id",
  "account locked",
  "account has been locked",
  "your account is locked",
  "temporarily locked",
  "locked for verification",
  "confirm your identity",
  "confirm identity",
  "proof of identity",
  "need to verify you",
  "complete verification",
  "\u0e22\u0e37\u0e19\u0e22\u0e31\u0e19\u0e15\u0e31\u0e27\u0e15\u0e19",
  "\u0e2a\u0e41\u0e01\u0e19\u0e2b\u0e19\u0e49\u0e32",
  "\u0e2a\u0e41\u0e01\u0e19\u0e43\u0e1a\u0e2b\u0e19\u0e49\u0e32",
  "\u0e1a\u0e31\u0e0d\u0e0a\u0e35\u0e16\u0e39\u0e01\u0e25\u0e47\u0e2d\u0e04",
  "\u0e1a\u0e31\u0e0d\u0e0a\u0e35\u0e15\u0e34\u0e14\u0e25\u0e47\u0e2d\u0e04",
  "\u0e22\u0e37\u0e19\u0e22\u0e31\u0e19\u0e2d\u0e32\u0e22\u0e38",
  "account is locked",
  "your account has been locked",
  "temporarily locked",
  "locked for verification",
  "suspicious activity",
  "unusual activity",
  "verify you are human",
  "verify you're human",
  "verify you're a human",
  "prove you're human",
  "prove you are human",
  "confirm you are human",
  "confirm you're human",
  "\u0e1a\u0e31\u0e0d\u0e0a\u0e35\u0e16\u0e39\u0e01\u0e25\u0e47\u0e2d\u0e01",
  "\u0e1a\u0e31\u0e0d\u0e0a\u0e35\u0e15\u0e34\u0e14\u0e25\u0e47\u0e2d\u0e01",
  "\u0e01\u0e34\u0e08\u0e01\u0e23\u0e23\u0e21\u0e17\u0e35\u0e48\u0e19\u0e48\u0e32\u0e2a\u0e07\u0e2a\u0e31\u0e22",
  "\u0e22\u0e37\u0e19\u0e22\u0e31\u0e19\u0e27\u0e48\u0e32\u0e04\u0e38\u0e13\u0e40\u0e1b\u0e47\u0e19\u0e21\u0e19\u0e38\u0e29\u0e22\u0e4c",
  "\u0e04\u0e38\u0e13\u0e40\u0e1b\u0e47\u0e19\u0e21\u0e19\u0e38\u0e29\u0e22\u0e4c",
  "\u0e1b\u0e25\u0e14\u0e25\u0e47\u0e2d\u0e04\u0e1a\u0e31\u0e0d\u0e0a\u0e35",
  "\u0e1b\u0e25\u0e14\u0e25\u0e47\u0e2d\u0e01\u0e1a\u0e31\u0e0d\u0e0a\u0e35"
];
const RECOVERY_KEYWORDS = [
  "passkey",
  "pass key",
  "webauthn",
  "security key",
  "recovery key",
  "recovery code",
  "recovery phrase",
  "backup code",
  "backup codes",
  "backup verification code",
  "account recovery",
  "recover your account",
  "recover account",
  "2-step verification",
  "2 step verification",
  "two-factor",
  "two factor",
  "2fa",
  "authenticator",
  "prove account ownership",
  "confirm account ownership",
  "verify account ownership",
  "add a passkey",
  "create a passkey",
  "use passkey",
  "sign in with passkey",
  "login with passkey",
  "recovery email",
  "recovery phone",
  "\u0e01\u0e39\u0e49\u0e01\u0e38\u0e0d\u0e41\u0e08",
  "\u0e01\u0e39\u0e49\u0e1a\u0e31\u0e0d\u0e0a\u0e35",
  "\u0e23\u0e2b\u0e31\u0e2a\u0e01\u0e39\u0e49",
  "\u0e04\u0e35\u0e22\u0e4c\u0e01\u0e39\u0e49",
  "\u0e22\u0e37\u0e19\u0e22\u0e31\u0e19 2 \u0e02\u0e31\u0e49\u0e19",
  "\u0e23\u0e2b\u0e31\u0e2a\u0e2a\u0e33\u0e23\u0e2d\u0e07"
];
export function isLocked(e) {
  if (!!e?.locked_required || !!e?.locked) return true;
  const b = lower(e?.import_status);
  if ("identity_locked" === b || "locked" === b) return true;
  const n = statusText(e);
  if (n.includes("identity_locked")) return true;
  return LOCKED_KEYWORDS.some(k => n.includes(k));
}
export function isAccountRecovery(e) {
  if (!!e?.recovery_required || !!e?.account_recovery) return true;
  const b = lower(e?.import_status);
  if ("account_recovery" === b) return true;
  const n = statusText(e);
  if (n.includes("account_recovery")) return true;
  return RECOVERY_KEYWORDS.some(k => n.includes(k));
}
export function isRecovery(e) { return isAccountRecovery(e) }

export function isCaptcha(e) {
  if (isLocked(e) || isAccountRecovery(e)) return false;
  const n = statusText(e);
  return !!e?.captcha_required || n.includes("captcha") || n.includes("arkose") || n.includes("funcaptcha") || n.includes("challenge required") || n.includes("security challenge");
}
function isInvalid(e) {
  if (isSuspended(e) || isBanned(e) || isLocked(e) || isAccountRecovery(e)) return false;
  const n = lower([e?.blocked_reason, e?.manual_status, e?.import_status, e?.last_recovery_reason, e?.last_state_reason].filter(Boolean).join(" "));
  return !isCaptcha(e) && (n.includes("cookie_invalid") || n.includes("cookie") && (n.includes("invalid") || n.includes("expired") || n.includes("authenticated") || n.includes("validation failed") || n.includes("missing")));
}
export function isFinished(e) { return lower(e?.manual_status) === "finished" || num(e?.finished_at) > 0 }
export function isSuspended(e) {
  if (!!e?.suspended) return true;
  const b = lower(e?.import_status);
  if ("suspended" === b) return true;
  const n = lower([e?.blocked_reason, e?.manual_status, e?.import_status, e?.last_recovery_reason, e?.last_state_reason, e?.last_error].filter(Boolean).join(" "));
  return n.includes("suspended by roblox") || n.includes("account suspended");
}
export function isBanned(e) {
  if (isSuspended(e)) return false;
  if (!!e?.banned) return true;
  const b = lower(e?.import_status);
  if ("banned" === b) return true;
  const n = lower([e?.blocked_reason, e?.manual_status, e?.import_status, e?.last_recovery_reason, e?.last_state_reason, e?.last_error].filter(Boolean).join(" "));
  if (n.includes("suspended by roblox") || n.includes("account suspended")) return false;
  return n.includes("account banned") || n.includes("roblox ban");
}
export function isBlocked(e) { return !(isCaptcha(e) || isSuspended(e) || isBanned(e) || isLocked(e) || isAccountRecovery(e) || isInvalid(e) || !1 !== e.launchable && !e.blocked_reason && !e.cookie_mismatch) }
function blockedStatusLabel(e) {
  const n = lower(e?.blocked_reason || e?.manual_status || e?.import_status || e?.last_recovery_reason || e?.last_state_reason || "");
  if (isSuspended(e)) return "Suspended";
  if (isBanned(e)) return "Banned";
  if (isLocked(e)) return "Locked";
  if (isAccountRecovery(e)) return "Recovery";
  if (isCaptcha(e)) return "Captcha";
  if (isInvalid(e)) return "Invalid";
  if (n.includes("multi_roblox_guard_failed") || n.includes("multi roblox guard")) return "Guard Failed";
  if (n.includes("cookie_mismatch") || n.includes("cookie mismatch") || n.includes("cookie belongs")) return "Cookie Mismatch";
  return "Blocked";
}
function isNetworkIssue(e) {
  const n = String(e?.public_state || e?.state || "").toUpperCase(), t = String(e?.runtime_state || "").toUpperCase(), i = lower(e?.liveness_state), s = statusText(e), o = ["NETWORK_LOST", "COOLDOWN", "RECOVERING"].includes(n) || ["RECOVERING", "BACKOFF"].includes(t), r = s.includes("disconnect_dialog") || s.includes("visual_disconnect") || s.includes("session_conflict") || s.includes("error 273") || s.includes("code 273"), u = r || s.includes("network_lost") || s.includes("network lost") || s.includes("network_drop") || s.includes("network drop") || s.includes("connection_error") || s.includes("connection error") || s.includes("disconnected") || s.includes("disconnect") || s.includes("error 277") || s.includes("code 277");
  return "NETWORK_LOST" === n || "reconnecting" === i || r || o && u;
}
function isNetworkRecovery(e) {
  const n = lower(e?.recovery_status), t = lower(e?.last_recovery_reason);
  return "network_lost" === n || t.includes("network_drop") || t.includes("network_lost");
}
function luaWaiting(e) { return !!e?.lua_required && !e?.lua_online }
function isGameClosed(e) { return !(isNetworkIssue(e) || isNetworkRecovery(e)) && statusText(e).includes("game_unavailable") }
function isWaitExploitPatch(e) { const n = statusText(e); return n.includes("wait_exploit_patch") || n.includes("wait for exploit patch") || lower(e?.state_label).includes("wait for exploit"); }
function queuedLabel(e) { return "Queued" }
function isStableInGame(e) {
  const n = String(e?.public_state || e?.state || "").toUpperCase(), t = String(e?.runtime_state || "").toUpperCase(), i = lower(e?.command_inflight?.action || e?.command_inflight || "");
  return !(luaWaiting(e) || !e?.pid || !1 === e?.process_alive || isRuntimeSuspect(e) || "IN_GAME" !== n && "RUNNING" !== t || i || ["NETWORK_LOST", "COOLDOWN", "FAILED", "CRASH"].includes(n));
}
export function bucket(e) {
  if (isFinished(e)) return "finished";
  if (isSuspended(e)) return "suspended";
  if (isBanned(e)) return "banned";
  if (isLocked(e)) return "locked";
  if (isAccountRecovery(e)) return "recovery";
  if (isCaptcha(e)) return "captcha";
  if (isInvalid(e)) return "invalid";
  if (isBlocked(e)) return "blocked";
  if (isGameClosed(e)) return "blocked";
  if (isWaitExploitPatch(e)) return "cooldown";
  const n = String(e.public_state || e.state || "").toUpperCase(), t = String(e.runtime_state || "").toUpperCase();
  return "QUEUED" === n ? "queued" : luaWaiting(e) && (e.pid || "IN_GAME" === n || "RUNNING" === t) ? "recovery" : isStableInGame(e) ? "online" : isRuntimeSuspect(e) ? "recovery" : isNetworkIssue(e) ? "network" : !luaWaiting(e) && ("IN_GAME" === n || "RUNNING" === t && e.pid) ? "online" : "IDLE" === n || "READY" === n || "STOPPED" === t ? "idled" : "COOLDOWN" === n ? "cooldown" : "FAILED" === n || "CRASH" === n ? "recovery" : ["LAUNCHING", "VERIFY", "NETWORK_LOST"].includes(n) || lower(e.recovery_status).includes("recover") ? "recovery" : "queued";
}
export function label(e) { return { all: "All", online: "Online", queued: "Queued", invalid: "Invalid", captcha: "Captcha", finished: "Finished", idled: "Idled", blocked: "Blocked", banned: "Banned", suspended: "Suspended", locked: "Locked", recovery: "Recovery", cooldown: "Cooldown", network: "Disconnected" }[e] || "Queued" }
export function counts(e) { const n = { all: e.length, online: 0, queued: 0, invalid: 0, captcha: 0, finished: 0, idled: 0, blocked: 0, banned: 0, suspended: 0, locked: 0, recovery: 0, cooldown: 0, network: 0 }; return e.forEach(e => n[bucket(e)]++), n }
export function filterAccounts(e, n, t) { const i = lower(n || ""); return e.filter(e => { if (!i) return !0; const n = t(e.username); return lower([e.username, e.display, n.alias, e.description, n.description, e.state, e.pid].join(" ")).includes(i) }) }
export function rowStatusLabel(e, n) {
  if (isFinished(e)) return "Finished";
  if (isSuspended(e)) return "Suspended";
  if (isBanned(e)) return "Banned";
  if (isLocked(e)) return "Locked";
  if (isAccountRecovery(e)) return "Recovery";
  if (isCaptcha(e)) return "Captcha";
  if (isInvalid(e)) return "Invalid";
  if (isBlocked(e)) return blockedStatusLabel(e);
  if (isNetworkIssue(e) || isNetworkRecovery(e)) return "Disconnected";
  if (isGameClosed(e)) return "Game Close";
  if (isWaitExploitPatch(e)) return "Wait for exploit patch";
  const t = String(e.public_state || e.state || "").toUpperCase(), i = String(e.runtime_state || "").toUpperCase(), s = lower(e.command_inflight?.action || e.command_inflight || ""), o = statusText(e), r = String(e.state_label || "").trim();
  if ("Locked" === r) return "Locked";
  if ("Recovery" === r) return "Recovery";
  return "Captcha" === r ? "Captcha" : "Queued" === r || "QUEUED" === t ? queuedLabel(e) : luaWaiting(e) && ("Waiting For Lua" === r || e.pid || "IN_GAME" === t || "RUNNING" === i) ? "Waiting For Lua" : isStableInGame(e) ? "Online" : ["Idle", "Queued", "Launching", "In Game", "Disconnected", "Rejoining", "Cooldown", "Failed"].includes(r) ? "In Game" === r ? "Online" : "Idle" === r ? "Idled" : "Queued" === r ? queuedLabel(e) : "Failed" === r ? "Rejoining" : r : isRuntimeSuspect(e) ? "Checking" : s.includes("launch") ? "Launching" : s.includes("rejoin") || o.includes("rejoin") || o.includes("recover") || o.includes("session_conflict") || o.includes("error 273") || o.includes("code 273") || o.includes("security_kick") || o.includes("unexpected_client_behavior") ? "Rejoining" : o.includes("connection_error") || o.includes("visual_disconnect") || o.includes("disconnect_dialog") || o.includes("disconnected") || o.includes("disconnect") || o.includes("error 277") || o.includes("code 277") || isNetworkIssue(e) ? "Disconnected" : "LAUNCHING" === t || "STARTING" === i ? "Launching" : "VERIFY" === t || "JOINING" === i ? "Rejoining" : !luaWaiting(e) && ("IN_GAME" === t || "RUNNING" === i && e.pid) ? "Online" : "COOLDOWN" === t || "BACKOFF" === i ? "Cooldown" : "FAILED" === t || "CRASH" === t ? "Rejoining" : ["IDLE", "READY"].includes(t) || "STOPPED" === i ? "Idled" : "RECOVERING" === t || "RECOVERING" === i ? "Rejoining" : label(n);
}
