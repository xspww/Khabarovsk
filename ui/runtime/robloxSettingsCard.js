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

  function toast(message) {
    const el = $("toast");
    if (el) {
      el.textContent = String(message || "");
      el.classList.add("show");
      clearTimeout(toast._t);
      toast._t = setTimeout(() => el.classList.remove("show"), 3200);
    }
  }

  function setNotice(message, isError) {
    const el = $("roblox-settings-notice");
    if (!el) return;
    el.classList.remove("notice-error", "notice-success", "notice-info", "notice-warning");
    if (!message || message === "ok") {
      el.textContent = "";
      el.classList.remove("show");
      return;
    }
    el.textContent = String(message);
    el.classList.add("show", isError ? "notice-error" : "notice-warning");
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
      if (!data.exists) setNotice("File not found — Roblox will recreate it on next launch", false);
      else setNotice("", false);
    } catch (err) {
      if (stateEl) stateEl.textContent = "-";
      setNotice(err?.message || "Status check failed", true);
    }
  }

  async function onDelete() {
    const btn = $("roblox-settings-delete");
    if (!btn || btn.disabled) return;
    if (!confirm("Delete GlobalBasicSettings_13.xml?\nRoblox will recreate default settings on next launch.")) return;
    btn.disabled = true;
    setNotice("Deleting...", false);
    try {
      const res = await fetch("/api/performance/roblox-settings/delete", {
        method: "POST",
        headers: apiHeaders(true),
        body: JSON.stringify({ confirm: true }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.msg || res.statusText);
      toast(data.msg || "Deleted");
      setNotice(data.msg || "Deleted", false);
    } catch (err) {
      setNotice(err?.message || "Delete failed", true);
      toast(err?.message || "Delete failed");
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
