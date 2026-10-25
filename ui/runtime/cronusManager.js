/* Cronus Manager: Windows startup + auto farming toggles.
 * Pattern follows executorCompatibility.js: static section in index.html,
 * config via /config, Startup shortcut via /api/startup/apply.
 */
"use strict";
const cronusManager = {};

cronusManager.NOTICE_ICONS = {
  warning:
    '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#f59e0b"/><path d="M12 7.5v5.2" stroke="white" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="16.2" r="1.2" fill="white"/></svg>',
  error:
    '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#ef4444"/><path d="M15 9l-6 6M9 9l6 6" stroke="white" stroke-width="2" stroke-linecap="round"/></svg>',
  success:
    '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#22c55e"/><path d="M8.2 12.2l2.6 2.6 4.5-5" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  info: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#3b82f6"/><path d="M12 11v5" stroke="white" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="8" r="1.2" fill="white"/></svg>',
};

// Element ids whose unsaved value must survive a config refresh.
cronusManager.EDITABLE_IDS = [
  "cronus-start-on-boot",
  "cronus-start-farming-on-boot",
  "cronus-auto-update-on-boot",
  "cronus-privacy-blur",
  "cronus-privacy-random",
  "cronus-discord-enabled",
  "cronus-discord-show-accounts",
  "cronus-discord-show-game",
];

// [element id, config key, default-when-missing]
cronusManager.CONFIG_TOGGLES = [
  ["cronus-start-on-boot", "start_on_boot", "false"],
  ["cronus-start-farming-on-boot", "start_farming_on_boot", "false"],
  ["cronus-auto-update-on-boot", "auto_update_on_boot", "false"],
  ["cronus-privacy-blur", "privacy_blur_usernames", "false"],
  ["cronus-privacy-random", "privacy_randomize_usernames", "false"],
  ["cronus-discord-enabled", "discord_presence_enabled", "false"],
  ["cronus-discord-show-accounts", "discord_show_accounts", "true"],
  ["cronus-discord-show-game", "discord_show_game", "true"],
];

cronusManager.$ = function (id) {
  return document.getElementById(id);
};

cronusManager.headers = function (json) {
  const h = {};
  const token =
    document.querySelector('meta[name="cronus-api-token"]')?.content || "";
  if (token) h["X-Cronus-Token"] = token;
  if (json) h["Content-Type"] = "application/json";
  return h;
};

cronusManager.escNotice = function (value) {
  const n = document.createElement("span");
  n.textContent = String(value || "");
  return n.innerHTML;
};

cronusManager.noticeKind = function (message, error) {
  if (error) return "error";
  const lower = String(message || "").toLowerCase();
  const saved =
    lower.includes("saved") ||
    lower.includes("complete") ||
    lower.includes("applied");
  return saved ? "success" : "warning";
};

cronusManager.noticeTitle = function (kind) {
  if (kind === "error") return "Action needed";
  return kind === "success" ? "Saved" : "Heads up";
};

cronusManager.noticeClass = function (kind) {
  if (kind === "error") return "notice show notice-error";
  return "notice show" + (kind === "success" ? " notice-success" : "");
};

cronusManager.show = function (message, error = false) {
  const node = cronusManager.$("cronus-manager-notice");
  if (!node) return;
  if (!message) {
    node.innerHTML = "";
    node.className = "notice";
    return;
  }
  const kind = cronusManager.noticeKind(message, error);
  const icon =
    cronusManager.NOTICE_ICONS[kind] || cronusManager.NOTICE_ICONS.warning;
  const title = cronusManager.noticeTitle(kind);
  node.innerHTML = `<span class="notice-icon">${icon}</span><span class="notice-copy"><span class="notice-title">${cronusManager.escNotice(title)}</span><span class="notice-desc">${cronusManager.escNotice(message)}</span></span>`;
  node.className = cronusManager.noticeClass(kind);
};

cronusManager.api = async function (path, method = "GET", body) {
  const response = await fetch(path, {
    method,
    headers: cronusManager.headers(body !== undefined),
    cache: "no-store",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new Error(payload.detail || payload.msg || response.statusText);
  return payload;
};

cronusManager.hasUnsavedEdits = function () {
  try {
    const save = cronusManager.$("cronus-save");
    if (save && !save.hidden) return true;
    const active = document.activeElement?.id || "";
    return cronusManager.EDITABLE_IDS.includes(active);
  } catch (_) {
    return false;
  }
};

cronusManager.applyConfigCheckboxes = function (config) {
  for (const [id, key, fallback] of cronusManager.CONFIG_TOGGLES) {
    const node = cronusManager.$(id);
    if (!node) continue;
    node.checked =
      fallback === "true" ? config[key] !== false : !!config[key];
  }
};

cronusManager.load = async function () {
  try {
    // Don't clobber unsaved edits — dashboard owns Save/Reset now.
    // If Save button is visible, user has dirty changes; skip refresh.
    if (cronusManager.hasUnsavedEdits()) return;
    const config = await cronusManager.api("/api/config");
    cronusManager.applyConfigCheckboxes(config);
    cronusManager.refreshDiscordStatus();
  } catch (error) {
    cronusManager.show(error.message, true);
  }
};

cronusManager.save = async function (patch) {
  try {
    await cronusManager.api("/api/config", "POST", patch);
    if (patch.start_on_boot !== undefined) {
      const res = await cronusManager.api("/api/startup/apply", "POST", {
        enabled: !!patch.start_on_boot,
      });
      if (!res.ok) throw new Error(res.msg || "Startup shortcut failed");
    }
    cronusManager.show("Cronus Manager settings saved");
  } catch (error) {
    cronusManager.show(error.message, true);
  }
};

cronusManager.bindDiscordTest = function () {
  const button = cronusManager.$("cronus-discord-test");
  if (!button || button.dataset.bound) return;
  button.dataset.bound = "1";
  button.addEventListener("click", cronusManager.testDiscord);
};

cronusManager.bind = function () {
  if (cronusManager.$("cronus-manager-card")?.dataset.bound === "1") return;
  if (cronusManager.$("cronus-manager-card"))
    cronusManager.$("cronus-manager-card").dataset.bound = "1";
  // NOTE: explicit Save/Reset (dashboard.js owns dirty state) — no auto-save here.
  // Legacy auto-save removed so Start can warn about unsaved Cronus changes.
  cronusManager.bindDiscordTest();
};

cronusManager.discordStatusText = function (enabled, connected, err) {
  if (!enabled) return "Disabled — turn on Rich Presence and Save";
  if (connected) return "Connected — presence is live on Discord";
  if (err) return "Not connected: " + err + " (open Discord desktop app)";
  return "Connecting… (open Discord desktop app)";
};

cronusManager.refreshDiscordStatus = async function () {
  const node = cronusManager.$("cronus-discord-status");
  if (!node) return;
  try {
    const status = await cronusManager.api("/api/discord/status");
    const enabled = !!(status.configured?.enabled ?? status.enabled);
    const connected = !!status.connected;
    const err = String(status.last_error || "");
    node.textContent = cronusManager.discordStatusText(enabled, connected, err);
    node.className = "hint";
  } catch (_) {
    /* intentional: status refresh is best effort */
  }
};

cronusManager.showTestNotice = function (notice, message, ok) {
  notice.textContent = String(message);
  notice.className =
    "notice show" + (ok ? " notice-success" : " notice-error");
  setTimeout(() => {
    if (notice) notice.className = "notice";
  }, 6000);
};

cronusManager.showTestError = function (notice, message) {
  notice.textContent = String(message);
  notice.className = "notice show notice-error";
};

cronusManager.testDiscord = async function () {
  const btn = cronusManager.$("cronus-discord-test");
  const notice = cronusManager.$("cronus-discord-notice");
  if (btn) btn.disabled = true;
  try {
    const res = await cronusManager.api("/api/discord/test", "POST", {});
    if (notice) {
      cronusManager.showTestNotice(
        notice,
        res.msg || (res.ok ? "Test sent" : "Test failed"),
        res.ok,
      );
    } else {
      cronusManager.show(String(res.msg || ""), !res.ok);
    }
    cronusManager.refreshDiscordStatus();
  } catch (error) {
    if (notice) {
      cronusManager.showTestError(notice, error.message || "Test failed");
    } else {
      cronusManager.show(error.message, true);
    }
  } finally {
    if (btn) btn.disabled = false;
  }
};

cronusManager.bind();
new MutationObserver(() => {
  if (!document.hidden) cronusManager.bind();
}).observe(document.documentElement, { childList: true, subtree: true });
setTimeout(cronusManager.load, 500);
setInterval(() => {
  if (
    !document.hidden &&
    cronusManager.$("view-cronus")?.classList.contains("active")
  )
    cronusManager.load();
}, 30000);
