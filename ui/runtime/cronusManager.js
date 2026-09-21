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
  const show = (message, error = false) => {
    const node = $("cronus-manager-notice");
    if (node) { node.textContent = message || ""; node.classList.toggle("show", !!message); node.classList.toggle("notice-error", !!error); }
  };
  async function api(path, method = "GET", body) {
    const response = await fetch(path, { method, headers: headers(body !== undefined), cache: "no-store", body: body === undefined ? undefined : JSON.stringify(body) });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || payload.msg || response.statusText);
    return payload;
  }
  async function load() {
    try {
      const config = await api("/api/config");
      if ($("cronus-start-on-boot")) $("cronus-start-on-boot").checked = !!config.start_on_boot;
      if ($("cronus-start-farming-on-boot")) $("cronus-start-farming-on-boot").checked = !!config.start_farming_on_boot;
      if ($("cronus-auto-update-on-boot")) $("cronus-auto-update-on-boot").checked = !!config.auto_update_on_boot;
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
    $("cronus-start-on-boot")?.addEventListener("change", (event) => save({ start_on_boot: !!event.target.checked }));
    $("cronus-start-farming-on-boot")?.addEventListener("change", (event) => save({ start_farming_on_boot: !!event.target.checked }));
    $("cronus-auto-update-on-boot")?.addEventListener("change", (event) => save({ auto_update_on_boot: !!event.target.checked }));
  }
  bind();
  new MutationObserver(bind).observe(document.documentElement, { childList: true, subtree: true });
  setTimeout(load, 500);
  setInterval(() => { if ($("view-cronus")?.classList.contains("active")) load(); }, 30000);
})();
