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
  const esc = (value) => { const n = document.createElement("span"); n.textContent = String(value || ""); return n.innerHTML; };
  const show = (message, error = false) => {
    const node = $("executor-monitor-notice");
    if (node) { node.textContent = message || ""; node.classList.toggle("show", !!message); node.classList.toggle("notice-error", !!error); }
  };
  async function api(path, method = "GET", body) {
    const response = await fetch(`/api${path}`, { method, headers: headers(body !== undefined), cache: "no-store", body: body === undefined ? undefined : JSON.stringify(body) });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || payload.msg || response.statusText);
    return payload;
  }
  function buildCard() {
    const view = $("view-troubleshoot");
    if (!view || $("executor-compatibility-card")) return;
    const card = document.createElement("section");
    card.className = "panel panel-body-only executor-compatibility-panel";
    card.innerHTML = `<div class="settings-card cards-mode"><section class="queue-card" id="executor-compatibility-card"><div class="queue-card-head"><div class="queue-card-title">Roblox Executor<div class="hint">Check Official Roblox compatibility before rejoin</div></div><button type="button" class="queue-collapse-btn" aria-expanded="true" title="Collapse" aria-label="Collapse Roblox Executor"><span class="caret" aria-hidden="true"></span></button></div><div class="queue-card-body"><div class="queue-row"><div class="queue-row-copy"><strong>Executor compatibility monitor</strong><div class="hint">Uses Official Roblox version and executor status</div></div><div class="toggle-row"><input id="executor-monitor-enabled" type="checkbox"><span></span></div></div><div class="queue-row queue-row-stack" id="executor-selector-row"><div class="queue-row-copy"><strong>Roblox Executor</strong><div class="hint">Select one Windows executor</div></div><div class="queue-row-input"><select id="executor-selected" class="input"><option value="">Select Executor</option></select></div></div><div class="queue-row queue-row-stack"><div class="queue-row-copy"><strong>Executor path</strong><div class="hint">Browse path for the selected Executor; Stop Auto Rejoin first</div></div><div class="queue-row-input executor-path-controls"><input id="executor-path" class="input" type="text" readonly placeholder="Select Executor first"><button class="btn ghost" id="executor-browse" type="button">Browse</button></div></div><div class="queue-row"><div class="queue-row-copy"><strong>Auto Relaunch Executor</strong><div class="hint">Close Roblox and Executor, reopen, verify Lua loader, then rejoin</div></div><div class="toggle-row"><input id="executor-relaunch-enabled" type="checkbox"><span></span></div></div><div class="queue-row"><div class="queue-row-copy"><strong>Auto update Roblox version</strong><div class="hint">Updates Roblox and ExploitStrap client versions from Official Roblox CDN</div></div><div class="toggle-row"><input id="roblox-auto-update-enabled" type="checkbox"><span></span></div></div><div class="meta-row"><div class="meta-box"><span>Roblox latest</span><strong id="executor-latest-version">-</strong></div><div class="meta-box"><span>Compatibility</span><strong id="executor-compatibility-state">Disabled</strong></div><div class="meta-box"><span>Relaunch</span><strong id="executor-relaunch-state">Idle</strong></div></div><div class="executor-actions"><button class="btn ghost" id="executor-check-now" type="button">Check now</button><button class="btn ghost" id="executor-retry" type="button" hidden>Retry</button></div><div id="executor-monitor-notice" class="notice"></div></div></section></div>`;
    const install = view.querySelector("#troubleshoot-install-card")?.closest(".panel");
    if (install) install.insertAdjacentElement("afterend", card); else view.appendChild(card);
    bindCard();
  }
  function renderStatus(status) {
    const select = $("executor-selected");
    const rows = Array.isArray(status?.executors) && status.executors.length
      ? status.executors
      : status?.state === "disabled"
        ? ["Volt", "Potassium", "Real", "Madium"].map((name) => ({ name, version: "" }))
        : [];
    if (select) {
      const current = select.value;
      select.innerHTML = '<option value="">Select Executor</option>' + rows.map((row) => `<option value="${esc(row.name)}">${esc(row.name)}${row.version ? ` · ${esc(row.version)}` : ""}</option>`).join("");
      select.value = status.selected || current || "";
    }
    const latest = $("executor-latest-version");
    const state = $("executor-compatibility-state");
    if (latest) latest.textContent = status.latest_version || "-";
    if (state) state.textContent = status.state === "compatible" ? "Compatible" : status.state === "incompatible" ? "Not compatible" : status.state === "api_error" ? "Version check unavailable" : status.state === "unknown" ? "Unknown" : "Disabled";
    if (status.error) show(`${status.error}`, true);
  }
  function renderRelaunch(status, config) {
    const selected = String(config.executor_selected || "").trim().toLowerCase();
    const paths = status?.paths || {};
    const path = paths[selected[0]?.toUpperCase() + selected.slice(1)] || paths[selected] || "";
    const input = $("executor-path"); if (input) input.value = path;
    const relaunch = $("executor-relaunch-enabled");
    const supported = status?.relaunch_supported === true;
    if (relaunch) {
      relaunch.disabled = !supported;
      if (!supported) relaunch.checked = false;
      relaunch.title = supported ? "Auto Relaunch is available" : (status?.relaunch_support_reason || "Selected Executor is not supported");
      const label = relaunch.parentElement?.querySelector("span");
      if (label) {
        label.textContent = relaunch.checked ? "Enabled" : "Disabled";
        label.classList.toggle("enabled", relaunch.checked);
        label.classList.toggle("disabled", !relaunch.checked);
      }
    }
    if (!supported && config.executor_relaunch_enabled) show(status?.relaunch_support_reason || "Auto Relaunch is unavailable", true);
    const state = $("executor-relaunch-state"); if (state) state.textContent = String(status?.state || "idle").replaceAll("_", " ");
    const retry = $("executor-retry"); if (retry) retry.hidden = status?.state !== "failed";
  }
  async function load() {
    try {
      const config = await api("/config");
      const enabled = $("executor-monitor-enabled");
      const update = $("roblox-auto-update-enabled");
      if (enabled) enabled.checked = !!config.executor_monitor_enabled;
      if (update) update.checked = !!config.roblox_auto_update_enabled;
      const relaunch = $("executor-relaunch-enabled");
      if (relaunch) relaunch.checked = !!config.executor_relaunch_enabled;
      const status = await api("/troubleshoot/executor");
      renderStatus(status);
      renderRelaunch(await api("/troubleshoot/executor/relaunch"), config);
      const selected = String(config.executor_selected || "").trim();
      const availableRows = status.state === "disabled" ? ["Volt", "Potassium", "Real", "Madium"].map((name) => ({ name })) : (status.executors || []);
      const available = availableRows.some((row) => String(row.name || "").toLowerCase() === selected.toLowerCase());
      if (selected && !available) {
        await api("/config", "POST", { executor_selected: "" });
        if ($("executor-selected")) $("executor-selected").value = "";
        show(`Executor ${selected} was not found in supported list and has been cleared`, true);
      }
    } catch (error) { show(error.message, true); }
  }
  async function save(patch) {
    try {
      await api("/config", "POST", patch);
      const status = await api("/troubleshoot/executor/check", "POST", {});
      renderStatus(status);
      renderRelaunch(await api("/troubleshoot/executor/relaunch"), await api("/config"));
      show("Executor settings saved");
    } catch (error) { show(error.message, true); }
  }
  function bindCard() {
    $("executor-monitor-enabled")?.addEventListener("change", (event) => save({ executor_monitor_enabled: !!event.target.checked }));
    $("roblox-auto-update-enabled")?.addEventListener("change", (event) => save({ roblox_auto_update_enabled: !!event.target.checked }));
    $("executor-relaunch-enabled")?.addEventListener("change", (event) => save({ executor_relaunch_enabled: !!event.target.checked }));
    $("executor-selected")?.addEventListener("change", (event) => save({ executor_selected: event.target.value }));
    $("executor-browse")?.addEventListener("click", async () => { try { const picked = await api("/troubleshoot/executor/browse", "POST", {}); if (picked.path) { const name = $("executor-selected")?.value || ""; const input = $("executor-path"); if (input) input.value = picked.path; await save({ [`executor_path_${name.toLowerCase()}`]: picked.path }); } } catch (error) { show(error.message, true); } });
    $("executor-retry")?.addEventListener("click", async () => { try { renderRelaunch(await api("/troubleshoot/executor/relaunch", "POST", {}), await api("/config")); } catch (error) { show(error.message, true); } });
    $("executor-check-now")?.addEventListener("click", async () => { try { renderStatus(await api("/troubleshoot/executor/check", "POST", {})); show("Compatibility check complete"); } catch (error) { show(error.message, true); } });
  }
  const observer = new MutationObserver(buildCard);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  buildCard();
  setTimeout(load, 500);
  setInterval(() => { if ($("view-troubleshoot")?.classList.contains("active")) load(); }, 30000);
})();
