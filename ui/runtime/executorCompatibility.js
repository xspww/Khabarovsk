"use strict";
{
  const $ = (id) => document.getElementById(id);
  const headers = (json) => {
    const h = {};
    const token =
      document.querySelector('meta[name="cronus-api-token"]')?.content || "";
    if (token) h["X-Cronus-Token"] = token;
    if (json) h["Content-Type"] = "application/json";
    return h;
  };
  const esc = (value) => {
    const n = document.createElement("span");
    n.textContent = String(value || "");
    return n.innerHTML;
  };
  const NOTICE_ICONS = {
    warning:
      '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#f59e0b"/><path d="M12 7.5v5.2" stroke="white" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="16.2" r="1.2" fill="white"/></svg>',
    error:
      '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#ef4444"/><path d="M15 9l-6 6M9 9l6 6" stroke="white" stroke-width="2" stroke-linecap="round"/></svg>',
    success:
      '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#22c55e"/><path d="M8.2 12.2l2.6 2.6 4.5-5" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };

  // ── Notice banner ─────────────────────────────────────────────────
  const mentionsAny = (text, needles) =>
    needles.some((needle) => text.includes(needle));

  const noticeKind = (message, error) => {
    if (error) return "error";
    const lower = String(message || "").toLowerCase();
    if (lower.includes("saved") || lower.includes("complete")) return "success";
    return "warning";
  };

  const noticeTitle = (kind, lower) => {
    let title =
      kind === "error"
        ? "Action needed"
        : kind === "success"
          ? "Saved"
          : "Heads up";
    if (mentionsAny(lower, ["close roblox", "stop auto rejoin", "stop guard"]))
      title = "Roblox is running";
    if (kind === "error" && mentionsAny(lower, ["browse", "relaunch"]))
      title = "Executor setup needed";
    return title;
  };

  const noticeClassName = (kind) => {
    if (kind === "error") return "notice show notice-error";
    if (kind === "success") return "notice show notice-success";
    return "notice show";
  };

  const noticeHtml = (kind, title, message) => {
    const icon = NOTICE_ICONS[kind] || NOTICE_ICONS.warning;
    return `<span class="notice-icon">${icon}</span><span class="notice-copy"><span class="notice-title">${esc(title)}</span><span class="notice-desc">${esc(message)}</span></span>`;
  };

  const clearNotice = (node) => {
    if (node.dataset.showKey === "clear") return;
    node.innerHTML = "";
    node.className = "notice";
    node.dataset.showKey = "clear";
  };

  const show = (message, error = false) => {
    const node = $("executor-monitor-notice");
    if (!node) return;
    if (!message) {
      clearNotice(node);
      return;
    }
    const kind = noticeKind(message, error);
    const title = noticeTitle(kind, String(message || "").toLowerCase());
    const key = kind + "|" + title + "|" + String(message || "");
    if (node.dataset.showKey === key) return;
    node.dataset.showKey = key;
    node.innerHTML = noticeHtml(kind, title, message);
    node.className = noticeClassName(kind);
  };

  // ── API ───────────────────────────────────────────────────────────
  async function api(path, method = "GET", body) {
    const response = await fetch(`/api${path}`, {
      method,
      headers: headers(body !== undefined),
      cache: "no-store",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok)
      throw new Error(payload.detail || payload.msg || response.statusText);
    return payload;
  }

  // ── Lua gate ──────────────────────────────────────────────────────
  const LUA_DISABLED_MSG = "Enable Use Lua to use Roblox Executor";
  const DEFAULT_EXECUTORS = ["Volt", "Potassium", "Real", "Madium"];
  let luaEnabledCache = true;
  let lastGateState = null;

  function isLuaEnabled(config) {
    if (config && typeof config === "object" && "lua_enabled" in config)
      return !!config.lua_enabled;
    if (typeof config === "boolean") return config;
    return true;
  }

  const setGateControls = (card, disabled) => {
    card.querySelectorAll("input,select,textarea,button").forEach((el) => {
      if (el.classList && el.classList.contains("queue-collapse-btn")) return;
      el.disabled = disabled;
    });
  };

  const disableToggleLabels = () => {
    try {
      document
        .querySelectorAll("#executor-compatibility-card .toggle-row span")
        .forEach((label) => {
          label.textContent = "Disabled";
          label.classList.toggle("enabled", false);
          label.classList.toggle("disabled", true);
        });
    } catch (_) {}
  };

  const resetGatedFields = () => {
    const state = $("executor-compatibility-state");
    if (state) state.textContent = "Disabled";
    const latest = $("executor-latest-version");
    if (latest) latest.textContent = "-";
    const relaunchState = $("executor-relaunch-state");
    if (relaunchState) relaunchState.textContent = "idle";
    const retry = $("executor-retry");
    if (retry) retry.hidden = true;
    show(LUA_DISABLED_MSG, true);
    disableToggleLabels();
  };

  function applyLuaGate(config) {
    luaEnabledCache = isLuaEnabled(config);
    const card = $("executor-compatibility-card");
    if (!card) return luaEnabledCache;
    const enabled = luaEnabledCache;
    // Guard: gating writes to the DOM (disabled flags, labels, notice),
    // which retriggers the MutationObserver below. Without this check the
    // observer -> gate -> DOM write -> observer chain never settles and
    // starves the main thread (total UI freeze). Only touch DOM on change.
    if (lastGateState === enabled && card.dataset.luaGate === String(enabled))
      return luaEnabledCache;
    lastGateState = enabled;
    card.dataset.luaGate = String(enabled);
    card.classList.toggle("gated-off", !enabled);
    setGateControls(card, !enabled);
    if (!enabled) resetGatedFields();
    return luaEnabledCache;
  }

  // ── Card scaffolding ──────────────────────────────────────────────
  function buildCard() {
    const view = $("view-troubleshoot");
    if (!view || $("executor-compatibility-card")) return;
    const card = document.createElement("section");
    card.className = "panel panel-body-only executor-compatibility-panel";
    card.innerHTML = `<div class="settings-card cards-mode"><section class="queue-card" id="executor-compatibility-card"><div class="queue-card-head"><div class="queue-card-title">Roblox Executor<div class="hint">Check Official Roblox compatibility before rejoin</div></div><button type="button" class="queue-collapse-btn" aria-expanded="true" title="Collapse" aria-label="Collapse Roblox Executor"><span class="caret" aria-hidden="true"></span></button></div><div class="queue-card-body"><div class="queue-row"><div class="queue-row-copy"><strong>Executor compatibility monitor</strong><div class="hint">Uses Official Roblox version and executor status</div></div><div class="toggle-row"><input id="executor-monitor-enabled" type="checkbox"><span></span></div></div><div class="queue-row queue-row-stack" id="executor-selector-row"><div class="queue-row-copy"><strong>Roblox Executor</strong><div class="hint">Select one Windows executor</div></div><div class="queue-row-input"><select id="executor-selected" class="input"><option value="">Select Executor</option></select></div></div><div class="queue-row queue-row-stack"><div class="queue-row-copy"><strong>Executor path</strong><div class="hint">Browse path for the selected Executor; Stop Auto Rejoin first</div></div><div class="queue-row-input executor-path-controls"><input id="executor-path" class="input" type="text" readonly placeholder="Select Executor first"><button class="btn ghost" id="executor-browse" type="button">Browse</button></div></div><div class="queue-row"><div class="queue-row-copy"><strong>Auto Relaunch Executor</strong><div class="hint">Close Roblox and Executor, reopen, verify Lua loader, then rejoin</div></div><div class="toggle-row"><input id="executor-relaunch-enabled" type="checkbox"><span></span></div></div><div class="queue-row"><div class="queue-row-copy"><strong>Auto update Roblox version</strong><div class="hint">Updates official Roblox client version from Official Roblox CDN</div></div><div class="toggle-row"><input id="roblox-auto-update-enabled" type="checkbox"><span></span></div></div><div class="meta-row"><div class="meta-box"><span>Roblox latest</span><strong id="executor-latest-version">-</strong></div><div class="meta-box"><span>Compatibility</span><strong id="executor-compatibility-state">Disabled</strong></div><div class="meta-box"><span>Relaunch</span><strong id="executor-relaunch-state">Idle</strong></div><div class="meta-box"><span>Local Executor</span><strong id="executor-local-version">-</strong></div></div><div class="executor-actions"><button class="btn ghost" id="executor-check-now" type="button">Check now</button><button class="btn ghost" id="executor-retry" type="button" hidden>Retry</button></div><div id="executor-monitor-notice" class="notice"></div></div></section></div>`;
    const install = view
      .querySelector("#troubleshoot-install-card")
      ?.closest(".panel");
    if (install) install.insertAdjacentElement("afterend", card);
    else view.appendChild(card);
    bindCard();
  }

  // ── Status rendering ──────────────────────────────────────────────
  const statusRows = (status) => {
    if (Array.isArray(status?.executors) && status.executors.length)
      return status.executors;
    if (status?.state === "disabled")
      return DEFAULT_EXECUTORS.map((name) => ({ name, version: "" }));
    return [];
  };

  const executorOptionsHtml = (rows) =>
    rows
      .map(
        (row) =>
          `<option value="${esc(row.name)}">${esc(row.name)}${row.version ? ` · ${esc(row.version)}` : ""}</option>`,
      )
      .join("");

  const renderExecutorOptions = (status) => {
    const select = $("executor-selected");
    if (!select) return;
    const current = select.value;
    select.innerHTML =
      '<option value="">Select Executor</option>' +
      executorOptionsHtml(statusRows(status));
    select.value = status.selected || current || "";
  };

  const STATE_LABELS = {
    compatible: "Compatible",
    incompatible: "Not compatible",
    api_error: "Version check unavailable",
    unknown: "Unknown",
  };

  function renderStatus(status) {
    renderExecutorOptions(status);
    const latest = $("executor-latest-version");
    const state = $("executor-compatibility-state");
    if (latest) latest.textContent = status.latest_version || "-";
    if (state) state.textContent = STATE_LABELS[status.state] || "Disabled";
    if (status.error) show(`${status.error}`, true);
  }

  function selectedExecutorPath(status, config) {
    const selected = String(config.executor_selected || "")
      .trim()
      .toLowerCase();
    const paths = status?.paths || {};
    return (
      paths[selected[0]?.toUpperCase() + selected.slice(1)] ||
      paths[selected] ||
      ""
    );
  }

  const relaunchTitle = (status, luaEnabled, supported) => {
    if (!luaEnabled) return LUA_DISABLED_MSG;
    if (supported) return "Auto Relaunch is available";
    return (
      status?.relaunch_support_reason || "Selected Executor is not supported"
    );
  };

  function renderRelaunchToggle(status, config, luaEnabled) {
    const relaunch = $("executor-relaunch-enabled");
    const supported = luaEnabled && status?.relaunch_supported === true;
    if (!relaunch) return supported;
    relaunch.disabled = !supported || !luaEnabled;
    if (!supported) relaunch.checked = false;
    relaunch.title = relaunchTitle(status, luaEnabled, supported);
    const label = relaunch.parentElement?.querySelector("span");
    if (label) {
      label.textContent = relaunch.checked ? "Enabled" : "Disabled";
      label.classList.toggle("enabled", relaunch.checked);
      label.classList.toggle("disabled", !relaunch.checked);
    }
    if (!supported && config.executor_relaunch_enabled)
      show(
        status?.relaunch_support_reason || "Auto Relaunch is unavailable",
        true,
      );
    return supported;
  }

  const localVersionText = (local, version, running) => {
    const name = String(local.name || "").trim();
    if (!version && !running) {
      if (name) return "Not running";
      return "-";
    }
    return (
      (running ? "" : "Stopped ") +
        [name, version ? "v" + version : ""].filter(Boolean).join(" ").trim() ||
      "-"
    );
  };

  function renderLocalExecutor(status) {
    const local = status?.local || {};
    const node = $("executor-local-version");
    if (!node) return;
    const version = String(status?.local_version || local.version || "").trim();
    const running = Boolean(status?.local_running ?? local.running);
    node.textContent = localVersionText(local, version, running);
    node.title = String(status?.local_exe || local.exe || "");
  }

  function renderRelaunch(status, config) {
    const luaEnabled = isLuaEnabled(config) && luaEnabledCache !== false;
    const path = selectedExecutorPath(status, config);
    const input = $("executor-path");
    if (input) input.value = path;
    renderRelaunchToggle(status, config, luaEnabled);
    const state = $("executor-relaunch-state");
    if (state)
      state.textContent = String(status?.state || "idle").replaceAll("_", " ");
    const retry = $("executor-retry");
    if (retry) retry.hidden = status?.state !== "failed";
    renderLocalExecutor(status);
  }

  // ── Load / save ───────────────────────────────────────────────────
  const applyConfigToControls = (config, luaEnabled) => {
    const enabled = $("executor-monitor-enabled");
    const update = $("roblox-auto-update-enabled");
    const relaunch = $("executor-relaunch-enabled");
    if (enabled) {
      enabled.checked = !!config.executor_monitor_enabled;
      enabled.disabled = !luaEnabled;
    }
    if (update) {
      update.checked = !!config.roblox_auto_update_enabled;
      update.disabled = !luaEnabled;
    }
    if (relaunch) relaunch.checked = !!config.executor_relaunch_enabled;
  };

  const refreshExecutorStatus = async (config) => {
    const status = await api("/troubleshoot/executor");
    renderStatus(status);
    renderRelaunch(await api("/troubleshoot/executor/relaunch"), config);
    return status;
  };

  const executorAvailable = (status, selected) => {
    const rows =
      status.state === "disabled"
        ? DEFAULT_EXECUTORS.map((name) => ({ name }))
        : status.executors || [];
    return rows.some(
      (row) => String(row.name || "").toLowerCase() === selected.toLowerCase(),
    );
  };

  const clearUnavailableExecutor = async (status, config) => {
    const selected = String(config.executor_selected || "").trim();
    if (!selected || executorAvailable(status, selected)) return;
    await api("/config", "POST", { executor_selected: "" });
    if ($("executor-selected")) $("executor-selected").value = "";
    show(
      `Executor ${selected} was not found in supported list and has been cleared`,
      true,
    );
  };

  async function load() {
    try {
      const config = await api("/config");
      const luaEnabled = applyLuaGate(config);
      applyConfigToControls(config, luaEnabled);
      const status = await refreshExecutorStatus(config);
      if (!luaEnabled) return;
      await clearUnavailableExecutor(status, config);
    } catch (error) {
      show(error.message, true);
    }
  }

  async function save(patch) {
    if (!luaEnabledCache) {
      show(LUA_DISABLED_MSG, true);
      return;
    }
    try {
      await api("/config", "POST", patch);
      const status = await api("/troubleshoot/executor/check", "POST", {});
      renderStatus(status);
      renderRelaunch(
        await api("/troubleshoot/executor/relaunch"),
        await api("/config"),
      );
      show("Executor settings saved");
    } catch (error) {
      show(error.message, true);
    }
  }

  // ── Card event wiring ─────────────────────────────────────────────
  const pickExecutorPath = async () => {
    try {
      if (!luaEnabledCache) {
        show(LUA_DISABLED_MSG, true);
        return;
      }
      const picked = await api("/troubleshoot/executor/browse", "POST", {});
      if (picked.path) {
        const name = $("executor-selected")?.value || "";
        const input = $("executor-path");
        if (input) input.value = picked.path;
        await save({ [`executor_path_${name.toLowerCase()}`]: picked.path });
      }
    } catch (error) {
      show(error.message, true);
    }
  };

  const retryRelaunch = async () => {
    try {
      if (!luaEnabledCache) {
        show(LUA_DISABLED_MSG, true);
        return;
      }
      renderRelaunch(
        await api("/troubleshoot/executor/relaunch", "POST", {}),
        await api("/config"),
      );
    } catch (error) {
      show(error.message, true);
    }
  };

  const checkExecutorNow = async () => {
    try {
      if (!luaEnabledCache) {
        show(LUA_DISABLED_MSG, true);
        return;
      }
      renderStatus(await api("/troubleshoot/executor/check", "POST", {}));
      show("Compatibility check complete");
    } catch (error) {
      show(error.message, true);
    }
  };

  function bindCard() {
    $("executor-monitor-enabled")?.addEventListener("change", (event) =>
      save({ executor_monitor_enabled: !!event.target.checked }),
    );
    $("roblox-auto-update-enabled")?.addEventListener("change", (event) =>
      save({ roblox_auto_update_enabled: !!event.target.checked }),
    );
    $("executor-relaunch-enabled")?.addEventListener("change", (event) =>
      save({ executor_relaunch_enabled: !!event.target.checked }),
    );
    $("executor-selected")?.addEventListener("change", (event) =>
      save({ executor_selected: event.target.value }),
    );
    $("executor-browse")?.addEventListener("click", pickExecutorPath);
    $("executor-retry")?.addEventListener("click", retryRelaunch);
    $("executor-check-now")?.addEventListener("click", checkExecutorNow);
  }

  // ── Global wiring + bootstrap ─────────────────────────────────────
  document.addEventListener("change", (event) => {
    try {
      if (event.target && event.target.id === "dev-lua-enabled") {
        applyLuaGate(!!event.target.checked);
        if (!event.target.checked) show(LUA_DISABLED_MSG, true);
      }
    } catch (_) {}
  });
  document.addEventListener("cronus:lua-enabled", (event) => {
    try {
      applyLuaGate(event?.detail?.enabled);
    } catch (_) {}
  });
  document.addEventListener("click", (event) => {
    try {
      const nav = event.target?.closest?.(
        '#nav button[data-view="troubleshoot"]',
      );
      if (nav)
        setTimeout(() => {
          if (!document.hidden) load();
        }, 300);
    } catch (_) {}
  });
  const observer = new MutationObserver(() => {
    if (!document.hidden) {
      const had = !!$("executor-compatibility-card");
      buildCard();
      if (!had && $("executor-compatibility-card")) load();
    }
  });
  observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
  });
  buildCard();
  setTimeout(load, 500);
  setInterval(() => {
    if (
      !document.hidden &&
      $("view-troubleshoot")?.classList.contains("active")
    )
      load();
  }, 30000);
}
