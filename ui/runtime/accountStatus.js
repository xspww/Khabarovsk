const lower = e => String(e || "").toLowerCase(), num = e => Number(e || 0) || 0;
const matchesAny = (text, needles) => needles.some(needle => text.includes(needle));
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
  return n.includes("identity_locked") || matchesAny(n, LOCKED_KEYWORDS);
}
export function isAccountRecovery(e) {
  if (!!e?.recovery_required || !!e?.account_recovery) return true;
  const b = lower(e?.import_status);
  if ("account_recovery" === b) return true;
  const n = statusText(e);
  return n.includes("account_recovery") || matchesAny(n, RECOVERY_KEYWORDS);
}
export function isRecovery(e) { return isAccountRecovery(e) }

export function isCaptcha(e) {
  if (isLocked(e) || isAccountRecovery(e)) return false;
  const n = statusText(e);
  return !!e?.captcha_required || n.includes("captcha") || n.includes("arkose") || n.includes("funcaptcha") || n.includes("challenge required") || n.includes("security challenge");
}
const COOKIE_INVALID_MARKERS = ["invalid", "expired", "authenticated", "validation failed", "missing"];
function cookieText(e) {
  return lower([e?.blocked_reason, e?.manual_status, e?.import_status, e?.last_recovery_reason, e?.last_state_reason].filter(Boolean).join(" "));
}
function isInvalid(e) {
  if (isSuspended(e) || isBanned(e) || isLocked(e) || isAccountRecovery(e)) return false;
  const n = cookieText(e);
  return !isCaptcha(e) && (n.includes("cookie_invalid") || n.includes("cookie") && matchesAny(n, COOKIE_INVALID_MARKERS));
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
const BLOCKED_LABEL_TESTS = [
  [isSuspended, "Suspended"],
  [isBanned, "Banned"],
  [isLocked, "Locked"],
  [isAccountRecovery, "Recovery"],
  [isCaptcha, "Captcha"],
  [isInvalid, "Invalid"]
];
function blockedStatusLabel(e) {
  const matched = BLOCKED_LABEL_TESTS.find(([test]) => test(e));
  if (matched) return matched[1];
  const n = blockedStatusText(e);
  if (n.includes("multi_roblox_guard_failed") || n.includes("multi roblox guard")) return "Guard Failed";
  if (n.includes("cookie_mismatch") || n.includes("cookie mismatch") || n.includes("cookie belongs")) return "Cookie Mismatch";
  return "Blocked";
}
function blockedStatusText(e) {
  return lower(e?.blocked_reason || e?.manual_status || e?.import_status || e?.last_recovery_reason || e?.last_state_reason || "");
}
const NETWORK_LOST_STATES = ["NETWORK_LOST", "COOLDOWN", "RECOVERING"];
const RECOVERING_RUNTIME_STATES = ["RECOVERING", "BACKOFF"];
const DISCONNECT_TEXT_MARKERS = ["disconnect_dialog", "visual_disconnect", "session_conflict", "error 273", "code 273"];
const NETWORK_TEXT_MARKERS = ["network_lost", "network lost", "network_drop", "network drop", "connection_error", "connection error", "disconnected", "disconnect", "error 277", "code 277"];
function isNetworkIssue(e) {
  const n = String(e?.public_state || e?.state || "").toUpperCase(), t = String(e?.runtime_state || "").toUpperCase(), s = statusText(e);
  const disconnected = matchesAny(s, DISCONNECT_TEXT_MARKERS);
  const recovering = NETWORK_LOST_STATES.includes(n) || RECOVERING_RUNTIME_STATES.includes(t);
  return "NETWORK_LOST" === n || "reconnecting" === lower(e?.liveness_state) || disconnected || recovering && matchesAny(s, NETWORK_TEXT_MARKERS);
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
  const s = stateOf(e);
  if (luaWaiting(e) || !e?.pid || !1 === e?.process_alive || isRuntimeSuspect(e)) return false;
  if ("IN_GAME" !== s.publicState && "RUNNING" !== s.runtimeState) return false;
  if (s.inflight || ["NETWORK_LOST", "COOLDOWN", "FAILED", "CRASH"].includes(s.publicState)) return false;
  return true;
}
function stateOf(e) {
  return {
    publicState: String(e?.public_state || e?.state || "").toUpperCase(),
    runtimeState: String(e?.runtime_state || "").toUpperCase(),
    inflight: lower(e?.command_inflight?.action || e?.command_inflight || "")
  };
}
const BUCKET_TESTS = [
  [isFinished, "finished"],
  [isSuspended, "suspended"],
  [isBanned, "banned"],
  [isLocked, "locked"],
  [isAccountRecovery, "recovery"],
  [isCaptcha, "captcha"],
  [isInvalid, "invalid"],
  [isBlocked, "blocked"],
  [isGameClosed, "blocked"],
  [isWaitExploitPatch, "cooldown"]
];
export function bucket(e) {
  const matched = BUCKET_TESTS.find(([test]) => test(e));
  if (matched) return matched[1];
  const n = String(e.public_state || e.state || "").toUpperCase(), t = String(e.runtime_state || "").toUpperCase();
  return runningBucket(e, n, t);
}
function runningBucket(e, n, t) {
  if ("QUEUED" === n) return "queued";
  if (waitingForLuaInGame(e, n, t)) return "recovery";
  if (isStableInGame(e)) return "online";
  if (isRuntimeSuspect(e)) return "recovery";
  if (isNetworkIssue(e)) return "network";
  if (runningInGame(e, n, t)) return "online";
  return idleBucket(e, n, t);
}
function waitingForLuaInGame(e, n, t) {
  return luaWaiting(e) && (e.pid || "IN_GAME" === n || "RUNNING" === t);
}
function runningInGame(e, n, t) {
  return !luaWaiting(e) && ("IN_GAME" === n || "RUNNING" === t && e.pid);
}
function idleBucket(e, n, t) {
  if ("IDLE" === n || "READY" === n || "STOPPED" === t) return "idled";
  if ("COOLDOWN" === n) return "cooldown";
  if ("FAILED" === n || "CRASH" === n) return "recovery";
  if (["LAUNCHING", "VERIFY", "NETWORK_LOST"].includes(n) || lower(e.recovery_status).includes("recover")) return "recovery";
  return "queued";
}
export function label(e) { return { all: "All", online: "Online", queued: "Queued", invalid: "Invalid", captcha: "Captcha", finished: "Finished", idled: "Idled", blocked: "Blocked", banned: "Banned", suspended: "Suspended", locked: "Locked", recovery: "Recovery", cooldown: "Cooldown", network: "Disconnected" }[e] || "Queued" }
export function counts(e) { const n = { all: e.length, online: 0, queued: 0, invalid: 0, captcha: 0, finished: 0, idled: 0, blocked: 0, banned: 0, suspended: 0, locked: 0, recovery: 0, cooldown: 0, network: 0 }; return e.forEach(e => n[bucket(e)]++), n }
export function filterAccounts(e, n, t) { const i = lower(n || ""); return e.filter(e => { if (!i) return !0; const n = t(e.username); return lower([e.username, e.display, n.alias, e.description, n.description, e.state, e.pid].join(" ")).includes(i) }) }
const ROW_LABEL_TESTS = [
  [isFinished, "Finished"],
  [isSuspended, "Suspended"],
  [isBanned, "Banned"],
  [isLocked, "Locked"],
  [isAccountRecovery, "Recovery"],
  [isCaptcha, "Captcha"],
  [isInvalid, "Invalid"]
];
const STATE_LABEL_ALIASES = { "In Game": "Online", Idle: "Idled", Launching: "Launching", Disconnected: "Disconnected", Rejoining: "Rejoining", Cooldown: "Cooldown", Failed: "Rejoining" };
const REJOIN_TEXT_MARKERS = ["rejoin", "recover", "session_conflict", "error 273", "code 273", "security_kick", "unexpected_client_behavior"];
const DISCONNECTED_TEXT_MARKERS = ["connection_error", "visual_disconnect", "disconnect_dialog", "disconnected", "disconnect", "error 277", "code 277"];
export function rowStatusLabel(e, n) {
  const matched = ROW_LABEL_TESTS.find(([test]) => test(e));
  if (matched) return matched[1];
  if (isBlocked(e)) return blockedStatusLabel(e);
  if (isNetworkIssue(e) || isNetworkRecovery(e)) return "Disconnected";
  if (isGameClosed(e)) return "Game Close";
  if (isWaitExploitPatch(e)) return "Wait for exploit patch";
  return stateLabel(e, rowState(e), n);
}
function rowState(e) {
  return { ...stateOf(e), text: statusText(e), label: String(e.state_label || "").trim() };
}
function stateLabel(e, s, n) {
  if ("Locked" === s.label || "Recovery" === s.label || "Captcha" === s.label) return s.label;
  if ("Queued" === s.label || "QUEUED" === s.publicState) return queuedLabel(e);
  if (luaWaitingForLabel(e, s)) return "Waiting For Lua";
  if (isStableInGame(e)) return "Online";
  const alias = STATE_LABEL_ALIASES[s.label];
  if (alias) return alias;
  return derivedStateLabel(e, s, n);
}
function luaWaitingForLabel(e, s) {
  return luaWaiting(e) && ("Waiting For Lua" === s.label || e.pid || "IN_GAME" === s.publicState || "RUNNING" === s.runtimeState);
}
function derivedStateLabel(e, s, n) {
  if (isRuntimeSuspect(e)) return "Checking";
  return signalLabel(e, s) || runtimeStateLabel(e, s) || label(n);
}
function signalLabel(e, s) {
  if (s.inflight.includes("launch")) return "Launching";
  if (isRejoinSignal(s)) return "Rejoining";
  if (isDisconnectSignal(e, s)) return "Disconnected";
  return "";
}
function isRejoinSignal(s) {
  return s.inflight.includes("rejoin") || matchesAny(s.text, REJOIN_TEXT_MARKERS);
}
function isDisconnectSignal(e, s) {
  return matchesAny(s.text, DISCONNECTED_TEXT_MARKERS) || isNetworkIssue(e);
}
function runtimeStateLabel(e, s) {
  if ("LAUNCHING" === s.publicState || "STARTING" === s.runtimeState) return "Launching";
  if ("VERIFY" === s.publicState || "JOINING" === s.runtimeState) return "Rejoining";
  if (runningInGame(e, s.publicState, s.runtimeState)) return "Online";
  return terminalStateLabel(s);
}
function terminalStateLabel(s) {
  if ("COOLDOWN" === s.publicState || "BACKOFF" === s.runtimeState) return "Cooldown";
  if ("FAILED" === s.publicState || "CRASH" === s.publicState) return "Rejoining";
  if (["IDLE", "READY"].includes(s.publicState) || "STOPPED" === s.runtimeState) return "Idled";
  if ("RECOVERING" === s.publicState || "RECOVERING" === s.runtimeState) return "Rejoining";
  return "";
}
