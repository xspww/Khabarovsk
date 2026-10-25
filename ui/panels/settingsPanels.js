const RAM_LIMIT_PRESETS = [4096, 6144, 8192, 12288, 16384, 24576, 32768, 65536];
const NOTICE_WARN_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>';
function setPanelNotice(el, msg, title) {
  if (!el) return;
  el.classList.remove(
    "notice-error",
    "notice-success",
    "notice-info",
    "notice-warning",
  );
  if (!msg || msg === "ok") {
    el.textContent = "";
    el.classList.remove("show");
    return;
  }
  el.innerHTML = "";
  let _ic = document.createElement("span");
  _ic.className = "notice-icon";
  _ic.innerHTML = NOTICE_WARN_SVG;
  el.prepend(_ic);
  let _cp = document.createElement("span");
  _cp.className = "notice-copy";
  if (title) {
    let _t = document.createElement("span");
    _t.className = "notice-title";
    _t.textContent = title;
    _cp.append(_t);
  }
  let _d = document.createElement("span");
  _d.className = "notice-desc";
  _d.textContent = msg;
  _cp.append(_d);
  el.append(_cp);
  el.classList.add("show", "notice-warning");
}
function setGateControls(e, a) {
  try {
    e.querySelectorAll("input,select,textarea,button").forEach(function (n) {
      n.disabled = !a;
    });
    e.querySelectorAll(".custom-select").forEach(function (r) {
      r.classList.toggle("is-disabled", !a);
    });
  } catch (_) {}
}
function setHidden(e, a) {
  e && ((e.hidden = !a), e.setAttribute("aria-hidden", String(!a)));
}
export function setReveal(e, a) {
  e &&
    (e.classList.add("reveal-panel"),
    (e.dataset.revealState = a ? "open" : "closed"),
    e.setAttribute("aria-hidden", "false"),
    (e.hidden = false),
    e.classList.toggle("is-open", a),
    e.classList.toggle("gated-off", !a),
    e.style.removeProperty("--reveal-height"),
    setGateControls(e, a));
}
export function setDim(e, a) {
  e && e.classList.toggle("gated-off", !a);
}
function setChecked(node, value) {
  if (node) node.checked = value;
}
function setValue(node, value) {
  if (node) node.value = value;
}
function normalizeRamLimit(e) {
  const a = Number(e);
  return Number.isFinite(a)
    ? Math.max(512, Math.min(Math.round(a), 65536))
    : 8192;
}
function readRamLimit(e) {
  const a = e("ram-limit-preset")?.value || "8192";
  return normalizeRamLimit(
    "custom" === a ? e("ram-limit-custom")?.value || 8192 : a,
  );
}
const GRID_PICKER_MAX = 12;
function numberOr(value, fallback) {
  return Number(value) || fallback;
}
function screenSize(axis, fallback) {
  return (window.screen && screen[axis]) || fallback;
}
function gridMetrics(e) {
  const cols = Math.max(1, numberOr(e("window-grid-cols")?.value, 6)),
    rows = Math.max(1, numberOr(e("window-grid-rows")?.value, 4)),
    width = numberOr(e("window-size-width")?.value, 200),
    height = numberOr(e("window-size-height")?.value, 150),
    fitCols = Math.floor(screenSize("availWidth", 1920) / width),
    fitRows = Math.floor(screenSize("availHeight", 1080) / height);
  return {
    cols,
    rows,
    maxCols: Math.max(1, Math.min(GRID_PICKER_MAX, fitCols)),
    maxRows: Math.max(1, Math.min(GRID_PICKER_MAX, fitRows)),
  };
}
function gridCellHtml(col, row, selected, fits, fitCols, fitRows) {
  const selectedClass = selected ? " is-sel" : "",
    offClass = fits ? "" : " is-off",
    disabled = fits ? "" : " disabled",
    title = fits
      ? `${col} × ${row}`
      : `${col} × ${row} — screen fits ${fitCols} × ${fitRows}`;
  return `<button type="button" class="wgrid-cell${selectedClass}${offClass}" data-col="${col}" data-row="${row}"${disabled} title="${title}"></button>`;
}
function gridRowsHtml(metrics) {
  let html = "";
  for (let row = 1; row <= GRID_PICKER_MAX; row++) {
    html += '<div class="wgrid-row">';
    for (let col = 1; col <= GRID_PICKER_MAX; col++) {
      const selected = col <= metrics.cols && row <= metrics.rows,
        fits = col <= metrics.maxCols && row <= metrics.maxRows;
      html += gridCellHtml(
        col,
        row,
        selected,
        fits,
        metrics.maxCols,
        metrics.maxRows,
      );
    }
    html += "</div>";
  }
  return html;
}
function renderGridPicker(e) {
  const picker = e("window-grid-picker");
  if (!picker) return;
  const metrics = gridMetrics(e);
  picker.innerHTML = `<div class="wgrid">${gridRowsHtml(metrics)}</div>`;
  const label = e("window-grid-label"),
    count = e("window-grid-count");
  if (label) label.textContent = `${metrics.cols} × ${metrics.rows}`;
  if (count) count.textContent = `${metrics.cols * metrics.rows} windows`;
}
const GAME_FIELDS = [
  "game-private",
  "game-place",
  "game-auto-private-enabled",
  "game-multi-roblox",
  "game-block-same-server",
];
const QUEUE_FIELDS = [
  "queue-max",
  "queue-delay",
  "queue-autoclose-enabled",
  "queue-autoclose-minutes",
  "popup-disconnected-enabled",
  "popup-scan-interval",
  "popup-scan-max-parallel",
];
function fillGameFields(e, config) {
  e("game-private").value = config.game_private_server_url || "";
  e("game-place").value = config.game_place_id || "";
  e("game-auto-private-enabled").checked =
    !!config.auto_create_private_server_enabled;
  e("game-multi-roblox").checked = !1 !== config.multi_roblox_enabled;
  setChecked(e("game-block-same-server"), !!config.block_same_server_enabled);
}
function syncGameModeToggle(toggle, config, dirty) {
  if (!toggle || dirty("game") || toggle.contains(document.activeElement)) return;
  const mode =
    "per_account" === String(config.game_mode || "").trim()
      ? "per_account"
      : "shared";
  toggle.dataset.savedValue = mode;
  toggle.dataset.value = mode;
  toggle
    .querySelectorAll("button")
    .forEach((button) => button.classList.toggle("active", button.dataset.mode === mode));
}
function renderGameSection(e, config, dirty) {
  const active = document.activeElement?.id || "";
  if (!dirty("game") && !GAME_FIELDS.includes(active)) fillGameFields(e, config);
  syncGameModeToggle(e("game-mode-toggle"), config, dirty);
}
function fillQueueFields(e, config) {
  e("queue-max").value = config.max_concurrent_accounts ?? 40;
  e("queue-delay").value =
    config.queue_delay_seconds ?? config.launch_rate_interval ?? 15;
  e("queue-autoclose-enabled").checked = !!config.auto_close_enabled;
  e("queue-autoclose-minutes").value = config.auto_close_minutes ?? 0;
}
function fillPopupFields(e, config) {
  e("popup-disconnected-enabled").checked =
    !1 !== config.popup_disconnected_enabled;
  e("popup-scan-interval").value = config.popup_scan_interval_seconds ?? 30;
  e("popup-scan-max-parallel").value = config.popup_scan_max_parallel ?? 2;
}
function renderQueueSection(e, config, dirty) {
  const active = document.activeElement?.id || "";
  if (dirty("queue") || QUEUE_FIELDS.includes(active)) return;
  fillQueueFields(e, config);
  fillPopupFields(e, config);
}
function syncQueueCardCollapse() {
  document.querySelectorAll(".queue-card").forEach((card) => {
    const key = "rg_collapsed_" + (card.id || "card");
    const collapsed = "1" === localStorage.getItem(key);
    card.classList.toggle("collapsed", collapsed);
    const body = card.querySelector(".queue-rows,.queue-card-body");
    if (body) body.hidden = collapsed;
    const button = card.querySelector(".queue-collapse-btn");
    if (button) button.setAttribute("aria-expanded", String(!collapsed));
  });
}
export function renderSettingsPanel(e) {
  const { $, CONFIG, dirty, updateSaveState } = e;
  if (!CONFIG) return;
  renderGameSection($, CONFIG, dirty);
  renderQueueSection($, CONFIG, dirty);
  setReveal($("autoclose-controls"), !!$("queue-autoclose-enabled").checked);
  setReveal(
    $("popup-disconnected-controls"),
    !!$("popup-disconnected-enabled").checked,
  );
  syncQueueCardCollapse();
  updateSaveState("game");
  updateSaveState("queue");
}
const PERF_FIELDS = ["fps-enabled", "fps-limit"];
function fillFpsFields(e, perf, dirty) {
  const active = document.activeElement?.id || "";
  if (dirty("performance") || PERF_FIELDS.includes(active)) return;
  e("fps-enabled").checked = !!perf.enabled;
  e("fps-limit").value = perf.fps_limit ?? perf.framerate_cap ?? 240;
}
function perfNoticeMessage(perf) {
  const message = perf.warning || perf.msg || "";
  if (!message || "ok" === message) return "";
  return perf.required_action || message;
}
export function renderPerformancePanel(e) {
  const { $, PERF, dirty, updateSaveState } = e;
  const fpsEnabled = $("fps-enabled");
  if (!fpsEnabled) return;
  fillFpsFields($, PERF, dirty);
  setReveal($("fps-limit-field"), !!fpsEnabled.checked);
  setPanelNotice(
    $("fps-notice"),
    perfNoticeMessage(PERF),
    PERF.warning_title || "",
  );
  updateSaveState("performance");
}
const RAM_FIELDS = ["ram-enabled", "ram-limit-preset", "ram-limit-custom"];
function fillRamFields(e, config, dirty, preset, custom) {
  const active = document.activeElement?.id || "";
  if (dirty("ram") || RAM_FIELDS.includes(active)) return;
  e("ram-enabled").checked = !1 !== config.roblox_memory_guard_enabled;
  const limit = normalizeRamLimit(config.roblox_memory_guard_mb ?? 8192);
  if (RAM_LIMIT_PRESETS.includes(limit)) {
    preset.value = String(limit);
    custom.value = String(limit);
    return;
  }
  preset.value = "custom";
  custom.value = String(limit);
}
export function renderRamPanel(e) {
  const { $, CONFIG, dirty, updateSaveState } = e;
  if (!$("ram-enabled")) return;
  const preset = $("ram-limit-preset"),
    custom = $("ram-limit-custom"),
    currentField = $("ram-current-field"),
    customField = $("ram-limit-custom-field");
  if (!(preset && custom && currentField && customField))
    return updateSaveState("ram");
  fillRamFields($, CONFIG, dirty, preset, custom);
  const enabled = !!$("ram-enabled").checked;
  setReveal(currentField, enabled);
  setHidden(customField, enabled && "custom" === preset.value);
  setPanelNotice($("ram-notice"), "");
  updateSaveState("ram");
}
const GRAPHICS_FIELDS = [
  "graphics-auto-enabled",
  "graphics-quality",
  "graphics-quality-range",
  "priority-enabled",
  "process-priority",
  "volume-muted",
  "volume-level",
  "volume-level-range",
];
function syncQualityRange(e) {
  const range = e("graphics-quality-range");
  if (!range) return;
  range.value = e("graphics-quality").value;
  if (window.__paintRange) window.__paintRange(range);
}
function fillGraphicsFields(e, graphics, dirty) {
  const active = document.activeElement?.id || "";
  if (dirty("graphics") || GRAPHICS_FIELDS.includes(active)) return;
  e("graphics-auto-enabled").checked =
    !!(graphics.graphics_low_enabled ?? graphics.graphics_auto_enabled);
  e("graphics-quality").value =
    graphics.graphics_quality_level ??
    graphics.graphics_quality_level_current ??
    1;
  syncQualityRange(e);
  e("priority-enabled").checked = !!graphics.auto_process_priority_enabled;
  e("process-priority").value = graphics.process_priority || "low";
  renderVolumeFields(e, graphics);
}
export function renderGraphicsPanel(e) {
  const { $, GRAPHICS, dirty, updateSaveState } = e;
  if (!$("graphics-auto-enabled")) return;
  fillGraphicsFields($, GRAPHICS, dirty);
  setReveal(
    $("graphics-quality-controls"),
    !!$("graphics-auto-enabled").checked,
  );
  setReveal($("priority-controls"), !!$("priority-enabled").checked);
  syncVolumeDisabled($);
  setPanelNotice(
    $("graphics-notice"),
    GRAPHICS.required_action || GRAPHICS.warning || GRAPHICS.msg || "",
    GRAPHICS.warning_title || "",
  );
  setPanelNotice($("volume-notice"), "");
  updateSaveState("graphics");
}
function clampVolumeLevel(e) {
  const a = Number(e);
  return Number.isFinite(a) ? Math.max(0, Math.min(10, Math.round(a))) : 10;
}
function volumeLevelOf(config) {
  return clampVolumeLevel(
    config.roblox_volume_level ?? config.volume_level_current ?? 10,
  );
}
function setVolumeInput(input, value) {
  if (!input || document.activeElement === input) return;
  input.value = value;
}
function renderVolumeFields(e, config) {
  try {
    const muted = e("volume-muted");
    if (muted) muted.checked = !!config.roblox_volume_muted;
    const level = volumeLevelOf(config),
      range = e("volume-level-range");
    setVolumeInput(e("volume-level"), level);
    setVolumeInput(range, String(level));
    if (range && window.__paintRange) window.__paintRange(range);
  } catch (_) {}
}
function syncVolumeDisabled(e) {
  try {
    const muted = e("volume-muted"),
      level = e("volume-level"),
      range = e("volume-level-range");
    const disabled = !!muted?.checked;
    if (level) level.disabled = disabled;
    if (range) range.disabled = disabled;
  } catch (_) {}
}
const WINDOW_SIZE_FIELDS = [
  "window-unlock-enabled",
  "window-arrange-enabled",
  "window-size-enabled",
  "window-grid-cols",
  "window-grid-rows",
  "window-size-preset",
  "window-size-width",
  "window-size-height",
  "window-auto-minimize-enabled",
  "window-auto-minimize-seconds",
  "window-hide-enabled",
];
function fillWindowSizeLayout(e, size) {
  setChecked(e("window-unlock-enabled"), !1 !== size.unlock_size_enabled);
  setChecked(e("window-arrange-enabled"), !!size.arrange_enabled);
  setChecked(e("window-size-enabled"), !!size.enabled);
  setValue(e("window-grid-cols"), size.arrange_columns ?? 6);
  setValue(e("window-grid-rows"), size.arrange_rows ?? 4);
  setValue(e("window-size-preset"), size.preset || "200x150");
  setValue(e("window-size-width"), size.width ?? 200);
  setValue(e("window-size-height"), size.height ?? 150);
}
function fillWindowSizeExtras(e, size) {
  setChecked(e("window-auto-minimize-enabled"), !!size.auto_minimize_enabled);
  setValue(e("window-auto-minimize-seconds"), size.auto_minimize_seconds ?? 10);
  setChecked(e("window-hide-enabled"), !!size.hide_enabled);
}
function fillWindowSizeFields(e, size, dirty, active) {
  if (dirty("window-size") || WINDOW_SIZE_FIELDS.includes(active)) return;
  fillWindowSizeLayout(e, size);
  fillWindowSizeExtras(e, size);
}
function applyWindowPreset(e, presets, active) {
  const preset = e("window-size-preset").value;
  setHidden(e("window-size-custom"), "custom" === preset);
  if ("custom" === preset || !presets[preset]) return;
  if (["window-size-width", "window-size-height"].includes(active)) return;
  const [width, height] = presets[preset];
  e("window-size-width").value = width;
  e("window-size-height").value = height;
}
export function renderWindowSizePanel(e) {
  const { $, WINDOW_SIZE, WINDOW_SIZE_PRESETS, dirty, updateSaveState } = e;
  if (!$("window-size-enabled")) return;
  const active = document.activeElement?.id || "";
  fillWindowSizeFields($, WINDOW_SIZE, dirty, active);
  setReveal(
    $("window-auto-minimize-seconds-field"),
    !!$("window-auto-minimize-enabled")?.checked,
  );
  applyWindowPreset($, WINDOW_SIZE_PRESETS, active);
  renderGridPicker($);
  setPanelNotice($("window-size-notice"), WINDOW_SIZE.msg || "");
  updateSaveState("window-size");
}
const CPU_FIELDS = [
  "cpu-enabled",
  "cpu-mode",
  "cpu-default-limit",
  "cpu-apply-all",
];
const CPU_EMPTY_ROW =
  '<tr><td colspan="5" style="height:90px;text-align:center;color:var(--muted)">No accounts</td></tr>';
function fillCpuFields(e, limiter, dirty) {
  const active = document.activeElement?.id || "";
  if (
    dirty("cpu-limiter") ||
    CPU_FIELDS.includes(active) ||
    active.startsWith("cpu-row-")
  )
    return;
  e("cpu-enabled").checked = !!limiter.enabled;
  e("cpu-mode").value = limiter.mode || "hard";
  e("cpu-default-limit").value = limiter.default_limit_percent ?? 20;
  e("cpu-apply-all").checked = !1 !== limiter.apply_all;
}
function cpuPanelState(e, limiter) {
  const enabled = !!e("cpu-enabled").checked,
    applyAll = !!e("cpu-apply-all").checked;
  const configured =
    Number(e("cpu-default-limit").value) ||
    Number(limiter.default_limit_percent);
  return {
    enabled,
    applyAll,
    defaultLimit: configured || 20,
    rows: Array.isArray(limiter.rows) ? limiter.rows : [],
  };
}
function cpuRowContext(row, ctx) {
  const username = String(row.username || "");
  const disabled = !ctx.enabled || ctx.applyAll;
  const limit = ctx.applyAll
    ? ctx.defaultLimit
    : (row.limit_percent ?? ctx.defaultLimit);
  return {
    username,
    disabled: disabled ? "disabled" : "",
    checked: ctx.applyAll || !!row.enabled ? "checked" : "",
    limit: Number(limit).toFixed(2),
  };
}
function cpuRowHtml(row, ctx) {
  const esc = ctx.esc,
    cells = cpuRowContext(row, ctx),
    user = esc(cells.username);
  return [
    `<tr><td><div class="name">${esc(row.display || cells.username)}</div><div class="handle">${user}</div></td>`,
    `<td>${esc(row.pid || "-")}</td>`,
    `<td><input id="cpu-row-enabled-${user}" class="cpu-row-enabled" data-user="${user}" type="checkbox" ${cells.checked} ${cells.disabled}></td>`,
    `<td><input id="cpu-row-limit-${user}" class="input cpu-row-limit" data-user="${user}" type="number" min="5" max="95" step="0.01" value="${esc(cells.limit)}" ${cells.disabled}></td>`,
    `<td><strong>${esc(row.status || "Pending")}</strong><div class="handle">${esc(row.message || "")}</div></td></tr>`,
  ].join("");
}
function cpuNoticeMessage(limiter, rows) {
  const failed = rows.filter((row) => "Failed" === row.status).length,
    fallback = rows.filter((row) => "Fallback" === row.status).length;
  return (
    limiter.msg || (failed ? `${failed} failed` : fallback ? `${fallback} fallback` : "")
  );
}
function renderCpuRowControls(e, enabled) {
  setHidden(e("cpu-controls"), enabled);
  setReveal(e("cpu-default-limit").closest(".queue-row"), enabled);
  setReveal(e("cpu-apply-all").closest(".queue-row"), enabled);
  setDim(e("cpu-account-rows").closest(".queue-row"), enabled);
}
export function renderCpuLimiterPanel(e) {
  const { $, CPU_LIMITER, dirty, updateSaveState, esc } = e;
  if (!$("cpu-enabled")) return;
  fillCpuFields($, CPU_LIMITER, dirty);
  const state = cpuPanelState($, CPU_LIMITER);
  const ctx = {
    enabled: state.enabled,
    applyAll: state.applyAll,
    defaultLimit: state.defaultLimit,
    esc,
  };
  const html = state.rows.map((row) => cpuRowHtml(row, ctx)).join("");
  $("cpu-account-rows").innerHTML = html || CPU_EMPTY_ROW;
  renderCpuRowControls($, state.enabled);
  setPanelNotice($("cpu-notice"), cpuNoticeMessage(CPU_LIMITER, state.rows));
  updateSaveState("cpu-limiter");
}
function robloxVersionPresentation(installed, latestVersion, isUpToDate) {
  if (!installed)
    return {
      className: "unknown",
      title: "Not installed",
      label: "Not installed",
    };
  if (!latestVersion)
    return {
      className: "unknown",
      title: "Version check unavailable",
      label: installed.version || "Installed",
    };
  return isUpToDate
    ? {
        className: "latest",
        title: "Latest version",
        label: installed.version || "Installed",
      }
    : {
        className: "outdated",
        title: "Update available",
        label: installed.version || "Installed",
      };
}
function blockerTitle(status, isRunning) {
  const blockers = status.blockers;
  if (!isRunning || !Array.isArray(blockers) || !blockers.length) return "";
  return blockers
    .map((blocker) => `${blocker.name || "Process"} (PID ${blocker.pid})`)
    .join(", ");
}
function renderTroubleshootNotice(element, status, job, isRunning) {
  const message = isRunning
    ? status.block_msg || "Stop Cronus and close Roblox first"
    : status.msg || job.error || job.msg || "";
  setPanelNotice(element, message, isRunning ? "Roblox is running" : "");
  if (element) element.title = blockerTitle(status, isRunning);
}
export function renderTroubleshootPanel(e) {
  const { $, TROUBLESHOOT } = e;
  const versionNode = $("roblox-installed-version");
  if (!versionNode) return;
  const installed = TROUBLESHOOT.installed || {};
  const job = TROUBLESHOOT.job || {};
  const isRunning = !!TROUBLESHOOT.running_blocked;
  const presentation = robloxVersionPresentation(
    installed,
    TROUBLESHOOT.latest_version,
    !!TROUBLESHOOT.installed_up_to_date,
  );
  versionNode.textContent = presentation.label;
  const dot = $("roblox-version-dot");
  if (dot) {
    dot.className = `install-version-dot ${presentation.className}`;
    dot.title = presentation.title;
  }
  $("roblox-install-state").textContent = job.state || "Ready";
  $("roblox-install-progress").textContent = job.progress || job.msg || "-";
  renderTroubleshootNotice($("roblox-install-notice"), TROUBLESHOOT, job, isRunning);
  ["roblox-uninstall", "roblox-latest"].forEach((id) => {
    const control = $(id);
    if (control) control.disabled = isRunning || !!job.active;
  });
}
function dispatchGameMode(mode, preview = false) {
  try {
    document.dispatchEvent(
      new CustomEvent("cronus:game-mode", { detail: { mode, preview } }),
    );
  } catch (_) {}
}
export function resetGamePanel(e) {
  (e.clearDirty("game"),
    renderSettingsPanel(e),
    dispatchGameMode(e.CONFIG?.game_mode || "shared"));
}
export function resetQueuePanel(e) {
  (e.clearDirty("queue"), renderSettingsPanel(e));
}
export function resetPerformancePanel(e) {
  (e.clearDirty("performance"), renderPerformancePanel(e));
}
export function resetRamPanel(e) {
  (e.clearDirty("ram"), renderRamPanel(e));
}
export function resetGraphicsPanel(e) {
  (e.clearDirty("graphics"), renderGraphicsPanel(e));
}
export function resetWindowSizePanel(e) {
  (e.clearDirty("window-size"), renderWindowSizePanel(e));
}
export function resetCpuLimiterPanel(e) {
  (e.clearDirty("cpu-limiter"), renderCpuLimiterPanel(e));
}
function gamePayload(e, config) {
  const toggle = e("game-mode-toggle"),
    blockSame = e("game-block-same-server");
  return {
    game_private_server_url: e("game-private").value.trim(),
    game_place_id: e("game-place").value.trim(),
    game_mode:
      toggle?.querySelector("button.active")?.dataset.mode ||
      toggle?.dataset.value ||
      "shared",
    auto_create_private_server_enabled: e("game-auto-private-enabled").checked,
    auto_create_private_server_free_only: !0,
    multi_roblox_enabled: e("game-multi-roblox").checked,
    block_same_server_enabled: blockSame
      ? !!blockSame.checked
      : !!config?.block_same_server_enabled,
  };
}
function pluralAccounts(count) {
  return `${count} account${count === 1 ? "" : "s"}`;
}
function gameSavedMessage(result) {
  const rejoined = Array.isArray(result.game_rejoined)
      ? result.game_rejoined.length
      : 0,
    stopped = Array.isArray(result.game_stopped)
      ? result.game_stopped.length
      : 0,
    defaulted = result.game_defaults_applied
      ? ` (${result.game_defaults_applied} defaulted)`
      : "",
    stoppedText = stopped ? ` - stopped ${pluralAccounts(stopped)}` : "",
    rejoinedText = rejoined ? ` - switching ${pluralAccounts(rejoined)}` : "";
  return `Game saved${defaulted}${stoppedText}${rejoinedText}`;
}
export async function saveGamePanel(e) {
  const { $, api, clearDirty, toast, loadConfig, manualSnapshot } = e;
  const payload = gamePayload($, e.CONFIG);
  const result = await api("/config", "POST", payload);
  clearDirty("game");
  toast(gameSavedMessage(result));
  await loadConfig();
  await manualSnapshot();
  dispatchGameMode(payload.game_mode);
}
export async function saveQueuePanel(e) {
  const {
      $: a,
      api: t,
      clearDirty: n,
      toast: r,
      loadConfig: i,
      manualSnapshot: o,
    } = e,
    l = {
      max_concurrent_accounts: Number(a("queue-max").value) || 1,
      queue_delay_seconds: Number(a("queue-delay").value) || 0,
      auto_close_enabled: a("queue-autoclose-enabled").checked,
      auto_close_minutes: Number(a("queue-autoclose-minutes").value) || 0,
      popup_disconnected_enabled: a("popup-disconnected-enabled").checked,
      popup_scan_interval_seconds: Number(a("popup-scan-interval").value) || 30,
      popup_scan_max_parallel: Number(a("popup-scan-max-parallel").value) || 2,
    };
  (await t("/config", "POST", l),
    n("queue"),
    r("Queue saved"),
    await i(),
    await o());
}
export async function savePerformancePanel(e) {
  const {
      $: a,
      api: t,
      clearDirty: n,
      toast: r,
      loadConfig: i,
      loadGraphics: o,
      renderPerformance: l,
    } = e,
    fpsRaw = String(a("fps-limit").value || "").trim(),
    fpsValue = Number(fpsRaw),
    s = {
      enabled: a("fps-enabled").checked,
      fps_limit: fpsRaw && Number.isFinite(fpsValue) ? fpsValue : 240,
    };
  try {
    const c = await t("/performance/fps-limiter", "POST", s);
    return (
      (e.PERF = c),
      n("performance"),
      l(c),
      r(c.warning || "FPS saved"),
      await i(),
      await o(),
      c
    );
  } catch (t) {
    return (setPanelNotice(a("fps-notice"), t.message), r(t.message), e.PERF);
  }
}
export async function saveRamPanel(e) {
  const {
      $: a,
      api: t,
      clearDirty: n,
      toast: r,
      loadConfig: i,
      manualSnapshot: o,
      renderRam: l,
    } = e,
    s = {
      roblox_memory_guard_enabled: a("ram-enabled").checked,
      roblox_memory_guard_mb: readRamLimit(a),
    };
  try {
    (await t("/config", "POST", s), n("ram"));
    const e = await i();
    return (l(), r("RAM Limiter saved"), await o(), e);
  } catch (t) {
    return (setPanelNotice(a("ram-notice"), t.message), r(t.message), e.CONFIG);
  }
}
function graphicsQualityLevel(e) {
  const quality = Number(e("graphics-quality")?.value);
  if (Number.isFinite(quality) && quality >= 1 && quality <= 10)
    return Math.round(quality);
  const range = Number(e("graphics-quality-range")?.value);
  return Number.isFinite(range)
    ? Math.max(1, Math.min(10, Math.round(range)))
    : 1;
}
function graphicsVolumeLevel(e) {
  const range = e("volume-level-range");
  let level = Number(e("volume-level")?.value);
  if (!Number.isFinite(level)) level = Number(range?.value);
  return clampVolumeLevel(level);
}
function graphicsPayload(e) {
  const lowEnabled = e("graphics-auto-enabled").checked;
  return {
    graphics_low_enabled: lowEnabled,
    graphics_auto_enabled: lowEnabled,
    graphics_quality_level: graphicsQualityLevel(e),
    auto_process_priority_enabled: e("priority-enabled").checked,
    process_priority: e("process-priority").value,
    roblox_volume_muted: !!e("volume-muted")?.checked,
    roblox_volume_level: graphicsVolumeLevel(e),
  };
}
export async function saveGraphicsPanel(e) {
  const { $, api, clearDirty, toast, loadConfig, renderGraphics } = e;
  try {
    const result = await api("/performance/graphics", "POST", graphicsPayload($));
    e.GRAPHICS = result;
    clearDirty("graphics");
    renderGraphics(result);
    toast(result.warning || "Graphics saved");
    await loadConfig();
    return result;
  } catch (error) {
    setPanelNotice($("graphics-notice"), error.message);
    setPanelNotice($("volume-notice"), error.message);
    toast(error.message);
    return e.GRAPHICS;
  }
}
function windowSizeExtras(e, state, includeHide) {
  const minimize = e("window-auto-minimize-enabled");
  const extras = {
    auto_minimize_enabled: minimize
      ? !!minimize.checked
      : !!state?.auto_minimize_enabled,
    auto_minimize_seconds:
      Number(e("window-auto-minimize-seconds")?.value) ||
      Number(state?.auto_minimize_seconds) ||
      10,
  };
  if (includeHide) {
    const hide = e("window-hide-enabled");
    extras.hide_enabled = hide ? !!hide.checked : !!state?.hide_enabled;
  }
  return extras;
}
function windowSizePayload(e, state, includeHide) {
  return {
    unlock_size_enabled: e("window-unlock-enabled").checked,
    enabled: e("window-size-enabled").checked,
    preset: e("window-size-preset").value,
    width: Number(e("window-size-width").value) || 200,
    height: Number(e("window-size-height").value) || 150,
    arrange_enabled: e("window-arrange-enabled").checked,
    arrange_columns: Number(e("window-grid-cols")?.value) || 6,
    arrange_rows: Number(e("window-grid-rows")?.value) || 4,
    arrange_gap: 0,
    ...windowSizeExtras(e, state, includeHide),
  };
}
export async function saveWindowSizePanel(e) {
  const { $, api, clearDirty, toast, loadConfig, renderWindowSize } = e;
  const payload = windowSizePayload($, e.WINDOW_SIZE, true);
  try {
    const result = await api("/performance/window-size", "POST", payload);
    e.WINDOW_SIZE = result;
    clearDirty("window-size");
    await loadConfig();
    renderWindowSize(result);
    toast(result.msg || "Windows saved");
    return result;
  } catch (error) {
    setPanelNotice($("window-size-notice"), error.message);
    toast(error.message);
    return e.WINDOW_SIZE;
  }
}
export async function rearrangeWindowSizePanel(e) {
  const { $, api, toast, renderWindowSize } = e;
  const payload = windowSizePayload($, e.WINDOW_SIZE, false);
  try {
    const result = await api("/performance/window-size/rearrange", "POST", payload);
    e.WINDOW_SIZE = result;
    renderWindowSize(result);
    toast(result.msg || "Windows re-arranged");
    return result;
  } catch (error) {
    setPanelNotice($("window-size-notice"), error.message);
    toast(error.message);
    return e.WINDOW_SIZE;
  }
}
function collectCpuRowsPanel(e) {
  const { $: a } = e;
  return a("cpu-apply-all").checked
    ? []
    : Array.from(document.querySelectorAll("#cpu-account-rows tr"))
        .map((row) => {
          const enabled = row.querySelector(".cpu-row-enabled"),
            limit = row.querySelector(".cpu-row-limit"),
            username = enabled?.dataset.user || limit?.dataset.user || "";
          return username
            ? {
                username,
                enabled: !!enabled?.checked,
                limit_percent: Number(limit?.value) || 20,
              }
            : null;
        })
        .filter(Boolean);
}
export async function saveCpuLimiterPanel(e) {
  const { $: a, api: t, clearDirty: n, toast: r, renderCpuLimiter: i } = e,
    o = {
      enabled: a("cpu-enabled").checked,
      mode: a("cpu-mode").value,
      default_limit_percent: Number(a("cpu-default-limit").value) || 20,
      apply_all: a("cpu-apply-all").checked,
      accounts: collectCpuRowsPanel(e),
    };
  try {
    const a = await t("/performance/cpu-limiter", "POST", o);
    return (
      (e.CPU_LIMITER = a),
      n("cpu-limiter"),
      i(a),
      r("CPU limiter saved"),
      a
    );
  } catch (t) {
    return (
      setPanelNotice(a("cpu-notice"), t.message),
      r(t.message),
      e.CPU_LIMITER
    );
  }
}
