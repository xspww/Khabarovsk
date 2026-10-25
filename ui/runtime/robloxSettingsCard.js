/* Cronus Roblox settings card: detect/delete GlobalBasicSettings_13.xml.
 * Pattern follows executorCompatibility.js: static section in index.html,
 * wired lazily so dynamic DOM swaps re-bind.
 */
"use strict";
const robloxSettingsCard = {};

// Project-style notifications: same stacked .toast-item cards as
// components/feedback.js (success/warning/error/info + icons, max 4, 3.2s).
robloxSettingsCard.TOAST_ICONS = {
  success:
    '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#22c55e"/><path d="M8.2 12.2l2.6 2.6 4.5-5" stroke="white" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  warning:
    '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#f59e0b"/><path d="M12 7.5v5.2" stroke="white" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="16.2" r="1.2" fill="white"/></svg>',
  error:
    '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#ef4444"/><path d="M15 9l-6 6M9 9l6 6" stroke="white" stroke-width="2" stroke-linecap="round"/></svg>',
  info: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#3b82f6"/><path d="M12 11v5" stroke="white" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="8" r="1.2" fill="white"/></svg>',
};

robloxSettingsCard.apiHeaders = function (json) {
  const headers = {};
  const token =
    document.querySelector('meta[name="cronus-api-token"]')?.content || "";
  if (token) headers["X-Cronus-Token"] = token;
  if (json) headers["Content-Type"] = "application/json";
  return headers;
};

robloxSettingsCard.$ = function (id) {
  return document.getElementById(id);
};

robloxSettingsCard.escHtml = function (value) {
  const node = document.createElement("span");
  node.textContent = String(value || "");
  return node.innerHTML;
};

robloxSettingsCard.pickKind = function (message) {
  const s = String(message || "").toLowerCase();
  if (/error|failed|invalid|cannot|denied|missing|required/i.test(s))
    return "error";
  if (/not found|nothing to delete/i.test(s)) return "info";
  if (/unsaved|save changes/i.test(s)) return "warning";
  return "success";
};

robloxSettingsCard.notify = function (message, type) {
  const box = robloxSettingsCard.$("toast");
  const text = String(message || "").trim();
  if (!text || !box) return;
  const kind = type || robloxSettingsCard.pickKind(text);
  while (box.children.length >= 4) {
    if (box.firstElementChild) box.firstElementChild.remove();
    else break;
  }
  const node = document.createElement("div");
  node.className = "toast-item toast-" + kind;
  node.innerHTML = `<span class="toast-icon">${robloxSettingsCard.TOAST_ICONS[kind] || robloxSettingsCard.TOAST_ICONS.info}</span><span class="toast-text">${robloxSettingsCard.escHtml(text)}</span>`;
  box.appendChild(node);
  requestAnimationFrame(() => node.classList.add("show"));
  setTimeout(() => {
    node.classList.remove("show");
    node.classList.add("hide");
    setTimeout(() => node.remove(), 200);
  }, 3200);
};

robloxSettingsCard.clearNotice = function () {
  const el = robloxSettingsCard.$("roblox-settings-notice");
  if (!el) return;
  el.textContent = "";
  el.classList.remove(
    "show",
    "notice-error",
    "notice-success",
    "notice-info",
    "notice-warning",
  );
  el.hidden = true;
};

robloxSettingsCard.readJson = async function (res) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.msg || res.statusText);
  return data;
};

robloxSettingsCard.applySettingsData = function (stateEl, btn, data) {
  if (stateEl) stateEl.textContent = data.exists ? "Found" : "Not found";
  const pathEl = robloxSettingsCard.$("roblox-settings-path");
  if (pathEl && data.path) pathEl.textContent = String(data.path);
  if (btn) btn.disabled = false;
  robloxSettingsCard.clearNotice();
};

robloxSettingsCard.markSettingsError = function (stateEl, btn) {
  if (stateEl) stateEl.textContent = "-";
  if (btn) btn.disabled = false;
  robloxSettingsCard.clearNotice();
};

robloxSettingsCard.refresh = async function () {
  const stateEl = robloxSettingsCard.$("roblox-settings-state");
  const btn = robloxSettingsCard.$("roblox-settings-delete");
  if (!stateEl && !btn) return;
  try {
    const res = await fetch("/api/performance/roblox-settings", {
      headers: robloxSettingsCard.apiHeaders(false),
      cache: "no-store",
    });
    const data = await robloxSettingsCard.readJson(res);
    robloxSettingsCard.applySettingsData(stateEl, btn, data);
  } catch (err) {
    robloxSettingsCard.markSettingsError(stateEl, btn);
  }
};

robloxSettingsCard.onDelete = async function () {
  const btn = robloxSettingsCard.$("roblox-settings-delete");
  if (!btn || btn.disabled) return;
  if (
    !confirm(
      "Delete GlobalBasicSettings_13.xml?\nRoblox will recreate default settings on next launch.",
    )
  )
    return;
  btn.disabled = true;
  try {
    const res = await fetch("/api/performance/roblox-settings/delete", {
      method: "POST",
      headers: robloxSettingsCard.apiHeaders(true),
      body: JSON.stringify({ confirm: true }),
    });
    const data = await robloxSettingsCard.readJson(res);
    robloxSettingsCard.notify(data.msg || "Deleted");
  } catch (err) {
    robloxSettingsCard.notify(err?.message || "Delete failed", "error");
  } finally {
    await robloxSettingsCard.refresh();
  }
};

robloxSettingsCard.watchCard = function (card) {
  robloxSettingsCard.refresh();
  // Refresh when Graphic view becomes visible.
  new MutationObserver(() => {
    const view = robloxSettingsCard.$("view-limiter");
    if (view && view.classList.contains("active")) robloxSettingsCard.refresh();
  }).observe(card, { attributes: true, attributeFilter: ["class"] });
  setInterval(() => {
    const view = robloxSettingsCard.$("view-limiter");
    if (view && view.classList.contains("active") && !document.hidden)
      robloxSettingsCard.refresh();
  }, 30000);
};

robloxSettingsCard.wire = function () {
  const btn = robloxSettingsCard.$("roblox-settings-delete");
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = "1";
    btn.addEventListener("click", robloxSettingsCard.onDelete);
  }
  const card = robloxSettingsCard.$("roblox-settings-card");
  if (card && !card.dataset.wired) {
    card.dataset.wired = "1";
    robloxSettingsCard.watchCard(card);
  }
};

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => {
    robloxSettingsCard.wire();
    robloxSettingsCard.refresh();
  });
} else {
  robloxSettingsCard.wire();
  robloxSettingsCard.refresh();
}
// Card HTML is static in index.html; re-wire after dynamic DOM swaps.
new MutationObserver(() => robloxSettingsCard.wire()).observe(
  document.documentElement,
  { childList: true, subtree: true },
);
