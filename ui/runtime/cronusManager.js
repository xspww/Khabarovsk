/* Cronus Manager: Windows startup + auto farming toggles.
 * Pattern follows executorCompatibility.js: static section in index.html,
 * config via /config, Startup shortcut via /api/startup/apply.
 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const headers = (json) => {
    const h = {};
    const token = document.querySelector('meta[name="cronus-api-token"]')?.content || "";
    if (token) h["X-Cronus-Token"] = token;
    if (json) h["Content-Type"] = "application/json";
    return h;
  };
  const escNotice = (value) => { const n = document.createElement("span"); n.textContent = String(value || ""); return n.innerHTML; };
  const NOTICE_ICONS = {
    warning: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#f59e0b"/><path d="M12 7.5v5.2" stroke="white" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="16.2" r="1.2" fill="white"/></svg>',
    error: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#ef4444"/><path d="M15 9l-6 6M9 9l6 6" stroke="white" stroke-width="2" stroke-linecap="round"/></svg>',
    success: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#22c55e"/><path d="M8.2 12.2l2.6 2.6 4.5-5" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#3b82f6"/><path d="M12 11v5" stroke="white" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="8" r="1.2" fill="white"/></svg>',
  };
  const show = (message, error = false) => {
    const node = $("cronus-manager-notice");
    if (!node) return;
    if (!message) { node.innerHTML = ""; node.className = "notice"; return; }
    const lower = String(message || "").toLowerCase();
    let kind = error ? "error" : "warning";
    if (!error && (lower.includes("saved") || lower.includes("complete") || lower.includes("applied"))) kind = "success";
    const title = kind === "error" ? "Action needed" : kind === "success" ? "Saved" : "Heads up";
    const icon = NOTICE_ICONS[kind] || NOTICE_ICONS.warning;
    node.innerHTML = `<span class="notice-icon">${icon}</span><span class="notice-copy"><span class="notice-title">${escNotice(title)}</span><span class="notice-desc">${escNotice(message)}</span></span>`;
    node.className = "notice show" + (kind === "error" ? " notice-error" : kind === "success" ? " notice-success" : "");
  };
  async function api(path, method = "GET", body) {
    const response = await fetch(path, { method, headers: headers(body !== undefined), cache: "no-store", body: body === undefined ? undefined : JSON.stringify(body) });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || payload.msg || response.statusText);
    return payload;
  }
  async function load() {
    try {
      // Don't clobber unsaved edits — dashboard owns Save/Reset now.
      // If Save button is visible, user has dirty changes; skip refresh.
      try {
        if ($("cronus-save") && !$("cronus-save").hidden) return;
        const active = document.activeElement?.id || "";
        if (["cronus-start-on-boot", "cronus-start-farming-on-boot", "cronus-auto-update-on-boot", "cronus-privacy-blur", "cronus-privacy-random", "cronus-discord-enabled", "cronus-discord-show-accounts", "cronus-discord-show-game"].includes(active)) return;
      } catch (_) {}
      const config = await api("/api/config");
      if ($("cronus-start-on-boot")) $("cronus-start-on-boot").checked = !!config.start_on_boot;
      if ($("cronus-start-farming-on-boot")) $("cronus-start-farming-on-boot").checked = !!config.start_farming_on_boot;
      if ($("cronus-auto-update-on-boot")) $("cronus-auto-update-on-boot").checked = !!config.auto_update_on_boot;
      if ($("cronus-privacy-blur")) $("cronus-privacy-blur").checked = !!config.privacy_blur_usernames;
      if ($("cronus-privacy-random")) $("cronus-privacy-random").checked = !!config.privacy_randomize_usernames;
      if ($("cronus-discord-enabled")) $("cronus-discord-enabled").checked = !!config.discord_presence_enabled;
      if ($("cronus-discord-show-accounts")) $("cronus-discord-show-accounts").checked = config.discord_show_accounts !== false;
      if ($("cronus-discord-show-game")) $("cronus-discord-show-game").checked = config.discord_show_game !== false;
      refreshDiscordStatus();
    } catch (error) { show(error.message, true); }
  }
  async function save(patch) {
    try {
      await api("/api/config", "POST", patch);
      if (patch.start_on_boot !== undefined) {
        const res = await api("/api/startup/apply", "POST", { enabled: !!patch.start_on_boot });
        if (!res.ok) throw new Error(res.msg || "Startup shortcut failed");
      }
      show("Cronus Manager settings saved");
    } catch (error) { show(error.message, true); }
  }
  function bind() {
    if ($("cronus-manager-card")?.dataset.bound === "1") return;
    if ($("cronus-manager-card")) $("cronus-manager-card").dataset.bound = "1";
    // NOTE: explicit Save/Reset (dashboard.js owns dirty state) — no auto-save here.
    // Legacy auto-save removed so Start can warn about unsaved Cronus changes.
    if ($("cronus-discord-test") && !$("cronus-discord-test").dataset.bound) {
      $("cronus-discord-test").dataset.bound = "1";
      $("cronus-discord-test").addEventListener("click", testDiscord);
    }
  }
  async function refreshDiscordStatus() {
    const node = $("cronus-discord-status");
    if (!node) return;
    try {
      const status = await api("/api/discord/status");
      const enabled = !!(status.configured?.enabled ?? status.enabled);
      const connected = !!status.connected;
      const err = String(status.last_error || "");
      if (!enabled) {
        node.textContent = "Disabled — turn on Rich Presence and Save";
        node.className = "hint";
      } else if (connected) {
        node.textContent = "Connected — presence is live on Discord";
        node.className = "hint";
      } else if (err) {
        node.textContent = "Not connected: " + err + " (open Discord desktop app)";
        node.className = "hint";
      } else {
        node.textContent = "Connecting… (open Discord desktop app)";
        node.className = "hint";
      }
    } catch (_) {}
  }
  async function testDiscord() {
    const btn = $("cronus-discord-test");
    const notice = $("cronus-discord-notice");
    if (btn) btn.disabled = true;
    try {
      const res = await api("/api/discord/test", "POST", {});
      if (notice) {
        notice.textContent = String(res.msg || (res.ok ? "Test sent" : "Test failed"));
        notice.className = "notice show" + (res.ok ? " notice-success" : " notice-error");
        setTimeout(() => { if (notice) { notice.className = "notice"; } }, 6000);
      } else {
        show(String(res.msg || ""), !res.ok);
      }
      refreshDiscordStatus();
    } catch (error) {
      if (notice) {
        notice.textContent = String(error.message || "Test failed");
        notice.className = "notice show notice-error";
      } else {
        show(error.message, true);
      }
    } finally {
      if (btn) btn.disabled = false;
    }
  }
  bind();
  new MutationObserver(() => { if (!document.hidden) bind(); }).observe(document.documentElement, { childList: true, subtree: true });
  setTimeout(load, 500);
  setInterval(() => { if (!document.hidden && $("view-cronus")?.classList.contains("active")) load(); }, 30000);
})();
