(function () {
  "use strict";

  function apiHeaders(json) {
    const headers = {};
    const token = document.querySelector('meta[name="cronus-api-token"]')?.content || "";
    if (token) headers["X-Cronus-Token"] = token;
    if (json) headers["Content-Type"] = "application/json";
    return headers;
  }

  function $(id) {
    return document.getElementById(id);
  }

  function escHtml(value) {
    const node = document.createElement("span");
    node.textContent = String(value || "");
    return node.innerHTML;
  }

  // Project-style notifications: same stacked .toast-item cards as
  // components/feedback.js (success/warning/error/info + icons, max 4, 3.2s).
  const TOAST_ICONS = {
    success: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#22c55e"/><path d="M8.2 12.2l2.6 2.6 4.5-5" stroke="white" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    warning: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#f59e0b"/><path d="M12 7.5v5.2" stroke="white" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="16.2" r="1.2" fill="white"/></svg>',
    error: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#ef4444"/><path d="M15 9l-6 6M9 9l6 6" stroke="white" stroke-width="2" stroke-linecap="round"/></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#3b82f6"/><path d="M12 11v5" stroke="white" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="8" r="1.2" fill="white"/></svg>',
  };

  function pickKind(message) {
    const s = String(message || "").toLowerCase();
    if (/error|failed|invalid|cannot|denied|missing|required/i.test(s)) return "error";
    if (/not found|nothing to delete/i.test(s)) return "info";
    if (/unsaved|save changes/i.test(s)) return "warning";
    return "success";
  }

  function notify(message, type) {
    const box = $("toast");
    const text = String(message || "").trim();
    if (!text || !box) return;
    const kind = type || pickKind(text);
    while (box.children.length >= 4) {
      if (box.firstElementChild) box.firstElementChild.remove();
      else break;
    }
    const node = document.createElement("div");
    node.className = "toast-item toast-" + kind;
    node.innerHTML = `<span class="toast-icon">${TOAST_ICONS[kind] || TOAST_ICONS.info}</span><span class="toast-text">${escHtml(text)}</span>`;
    box.appendChild(node);
    requestAnimationFrame(() => node.classList.add("show"));
    setTimeout(() => {
      node.classList.remove("show");
      node.classList.add("hide");
      setTimeout(() => node.remove(), 200);
    }, 3200);
  }

  function clearNotice() {
    const el = $("roblox-settings-notice");
    if (!el) return;
    el.textContent = "";
    el.classList.remove("show", "notice-error", "notice-success", "notice-info", "notice-warning");
    el.hidden = true;
  }

  async function refresh() {
    const stateEl = $("roblox-settings-state");
    const btn = $("roblox-settings-delete");
    if (!stateEl && !btn) return;
    try {
      const res = await fetch("/api/performance/roblox-settings", {
        headers: apiHeaders(false),
        cache: "no-store",
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.msg || res.statusText);
      if (stateEl) stateEl.textContent = data.exists ? "Found" : "Not found";
      const pathEl = $("roblox-settings-path");
      if (pathEl && data.path) pathEl.textContent = String(data.path);
      if (btn) btn.disabled = false;
      clearNotice();
    } catch (err) {
      if (stateEl) stateEl.textContent = "-";
      if (btn) btn.disabled = false;
      clearNotice();
    }
  }

  async function onDelete() {
    const btn = $("roblox-settings-delete");
    if (!btn || btn.disabled) return;
    if (!confirm("Delete GlobalBasicSettings_13.xml?\nRoblox will recreate default settings on next launch.")) return;
    btn.disabled = true;
    try {
      const res = await fetch("/api/performance/roblox-settings/delete", {
        method: "POST",
        headers: apiHeaders(true),
        body: JSON.stringify({ confirm: true }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.msg || res.statusText);
      notify(data.msg || "Deleted");
    } catch (err) {
      notify(err?.message || "Delete failed", "error");
    } finally {
      await refresh();
    }
  }

  function wire() {
    const btn = $("roblox-settings-delete");
    if (btn && !btn.dataset.wired) {
      btn.dataset.wired = "1";
      btn.addEventListener("click", onDelete);
    }
    const card = $("roblox-settings-card");
    if (card && !card.dataset.wired) {
      card.dataset.wired = "1";
      refresh();
      // Refresh when Graphic view becomes visible.
      new MutationObserver(() => {
        const view = $("view-limiter");
        if (view && view.classList.contains("active")) refresh();
      }).observe(card, { attributes: true, attributeFilter: ["class"] });
      setInterval(() => {
        const view = $("view-limiter");
        if (view && view.classList.contains("active") && !document.hidden) refresh();
      }, 30000);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => { wire(); refresh(); });
  } else {
    wire();
    refresh();
  }
  // Card HTML is static in index.html; re-wire after dynamic DOM swaps.
  new MutationObserver(() => wire()).observe(document.documentElement, { childList: true, subtree: true });
})();
