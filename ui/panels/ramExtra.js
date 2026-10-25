/* RAM Cleanup + Virtual Memory panels (MemReduct port, GPLv3 — see THIRD_PARTY_NOTICES).
 * Follows settingsPanels.js conventions: render/save/reset.
 */
function num(v, fb) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fb;
}
function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v));
}

const NOTICE_SVG = {
  warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
  error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M15 9l-6 6"/><path d="M9 9l6 6"/></svg>',
  ok: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.1V12a10 10 0 1 1-5.9-9.1"/><path d="M22 4 12 14l-3-3"/></svg>',
  info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>',
};
const NOTICE_TONE_CLASS = { error: "notice-error", ok: "notice-success", info: "notice-info", warn: "notice-warning" };

function paintNotice(el, tone) {
  const cls = NOTICE_TONE_CLASS[tone] || "";
  el.classList.remove("notice-error", "notice-success", "notice-info", "notice-warning");
  if (cls) el.classList.add(cls);
  let icon = el.querySelector(":scope > .notice-icon");
  if (!icon) {
    icon = document.createElement("span");
    icon.className = "notice-icon";
    el.prepend(icon);
  }
  icon.innerHTML = NOTICE_SVG[tone] || NOTICE_SVG.warn;
  let copy = el.querySelector(":scope > .notice-copy");
  if (!copy) {
    copy = document.createElement("span");
    copy.className = "notice-copy";
    const desc = document.createElement("span");
    desc.className = "notice-desc";
    copy.append(desc);
    el.append(copy);
  }
  return copy;
}

/** Structured alert: icon + optional title + message + tone color. tone: error|warn|ok|info */
export function setNotice(el, msg, tone, title) {
  if (!el) return;
  if (!msg) {
    el.textContent = "";
    el.classList.remove("show", "notice-error", "notice-success", "notice-info", "notice-warning");
    return;
  }
  const copy = paintNotice(el, tone || "warn");
  copy.innerHTML = "";
  if (title) {
    const t = document.createElement("span");
    t.className = "notice-title";
    t.textContent = title;
    copy.append(t);
  }
  const d = document.createElement("span");
  d.className = "notice-desc";
  d.textContent = msg;
  copy.append(d);
  el.classList.add("show");
}

/** Global upgrade: any plain-text .notice.show becomes a real alert with icon.
 * Runs on every render so old panels get it too — no per-panel edits needed. */
export function decorateNotices() {
  document.querySelectorAll(".notice.show").forEach((n) => {
    if (n.querySelector(":scope > .notice-icon")) return;
    const text = n.textContent.trim();
    if (!text) return;
    let tone = "warn";
    if (n.classList.contains("notice-error")) tone = "error";
    else if (n.classList.contains("notice-success")) tone = "ok";
    else if (n.classList.contains("notice-info")) tone = "info";
    n.textContent = "";
    const copy = paintNotice(n, tone);
    const d = document.createElement("span");
    d.className = "notice-desc";
    d.textContent = text;
    copy.append(d);
    n.classList.add("show");
  });
}

// ── RAM cleanup ─────────────────────────────────────────────────────

const RAM_FIELDS = [
  "ram-cleanup-enabled",
  "ram-cleanup-threshold",
  "ram-cleanup-interval",
];

function ramCleanupSrc(cleanup) {
  return cleanup && typeof cleanup === "object" && "enabled" in cleanup
    ? cleanup
    : null;
}

function ramCleanupInputs(src, config) {
  return {
    enabled: src ? !!src.enabled : !!config.ram_cleanup_enabled,
    threshold:
      src && src.threshold_pct != null
        ? src.threshold_pct
        : (config.ram_cleanup_threshold_pct ?? 85),
    interval:
      src && src.interval_min != null
        ? src.interval_min
        : (config.ram_cleanup_interval_min ?? 15),
  };
}

function syncRamCleanupInputs($, inputs, active, isDirty) {
  if (isDirty || RAM_FIELDS.includes(active)) return;
  $("ram-cleanup-enabled").checked = inputs.enabled;
  $("ram-cleanup-threshold").value = inputs.threshold;
  $("ram-cleanup-interval").value = inputs.interval;
}

function ramCleanupCountdown(src, enabled, interval) {
  if (!enabled) return "";
  const nextIn =
    src && src.next_clean_in_seconds != null
      ? num(src.next_clean_in_seconds, 0)
      : num(src && src.next_check_in_seconds, 0);
  if (nextIn > 0) {
    const s = Math.ceil(nextIn);
    return s >= 60
      ? ` · Next clean in ${Math.ceil(s / 60)} min`
      : ` · Next clean in ${s}s`;
  }
  return ` · Cleans every ${interval} min`;
}

function ramCleanupStatusText(src, pct, freed, countdown) {
  if (src && src.last_run_at) {
    const when = new Date(src.last_run_at * 1000).toLocaleTimeString();
    return (
      `Last cleaned ${when} · Freed ${freed} MB` +
      (Number.isFinite(pct) ? ` · Now ${pct.toFixed(1)}%` : "") +
      countdown
    );
  }
  if (Number.isFinite(pct))
    return `Current ${pct.toFixed(1)}% — not cleaned yet` + countdown;
  return "Not cleaned yet" + countdown;
}

function renderRamCleanupStatus($, src, countdown) {
  const status = $("ram-cleanup-status");
  if (!status) return;
  const pct = src && src.current ? num(src.current.percent, NaN) : NaN;
  const freed = src ? num(src.last_freed_mb, 0) : 0;
  status.textContent = ramCleanupStatusText(src, pct, freed, countdown);
}

function renderRamCleanupNotice(notice, src, enabled) {
  if (!notice) return;
  if (src && !src.ok && src.msg)
    setNotice(notice, src.msg, "error", "RAM Cleanup unavailable");
  else if (src && src.is_admin === false && enabled)
    setNotice(
      notice,
      "Requires admin — relaunch as administrator, otherwise auto-clean stays skipped.",
      "warn",
      "Admin required",
    );
  else setNotice(notice, "", "warn");
}

export function renderRamCleanupPanel(e) {
  const { $, RAM_CLEANUP, CONFIG, dirty, updateSaveState } = e;
  if (!$("ram-cleanup-enabled")) return;
  const active = document.activeElement ? document.activeElement.id || "" : "";
  const src = ramCleanupSrc(RAM_CLEANUP);
  const inputs = ramCleanupInputs(src, CONFIG);
  syncRamCleanupInputs($, inputs, active, dirty("ram-cleanup"));
  const countdown = ramCleanupCountdown(src, inputs.enabled, inputs.interval);
  renderRamCleanupStatus($, src, countdown);
  renderRamCleanupNotice($("ram-cleanup-notice"), src, inputs.enabled);
  updateSaveState("ram-cleanup");
}

export async function saveRamCleanupPanel(e) {
  const { $, api, clearDirty, toast, loadConfig, manualSnapshot, renderRamCleanup } = e;
  const payload = {
    enabled: $("ram-cleanup-enabled").checked,
    threshold_pct: clamp(Math.round(num($("ram-cleanup-threshold").value, 85)), 50, 95),
    interval_min: Math.max(5, Math.round(num($("ram-cleanup-interval").value, 15))),
  };
  try {
    const res = await api("/performance/ram-cleanup", "POST", payload);
    clearDirty("ram-cleanup");
    renderRamCleanup(res);
    toast("RAM Cleanup saved");
    await loadConfig();
    await manualSnapshot();
    return res;
  } catch (err) {
    setNotice($("ram-cleanup-notice"), err.message, "error", "Save failed");
    toast(err.message, "error");
    return e.RAM_CLEANUP;
  }
}

export function resetRamCleanupPanel(e) {
  e.clearDirty("ram-cleanup");
  renderRamCleanupPanel(e);
}

function cleanupBlocked(notice, toast, res) {
  const msg = (res && res.msg) || "Cleanup blocked";
  setNotice(notice, msg, "error", "Cleanup blocked");
  toast(msg, "error");
  return res;
}

export async function cleanNowRamCleanup(e) {
  const { $, api, toast, renderRamCleanup } = e;
  const btn = $("ram-cleanup-now");
  const notice = $("ram-cleanup-notice");
  try {
    if (btn) btn.disabled = true;
    setNotice(notice, "Cleaning memory, please wait…", "info", "Working");
    const res = await api("/performance/ram-cleanup/clean-now", "POST", { source: "manual" });
    if (!res || res.ok === false) return cleanupBlocked(notice, toast, res);
    renderRamCleanup(res);
    setNotice(notice, res.msg || `Freed ${res.freed_mb ?? 0} MB`, "ok", "Memory cleaned");
    toast(res.msg || `Cleaned ${res.freed_mb ?? 0} MB`, "success");
    return res;
  } catch (err) {
    setNotice(notice, err.message, "error", "Cleanup failed");
    toast(err.message, "error");
    return null;
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ── Virtual memory ──────────────────────────────────────────────────

function vmCurrentText(vm, current) {
  if (current.mode === "custom" && current.size_gb != null)
    return `Custom ${current.size_gb} GB`;
  if (vm.managed_by_windows === false)
    return "Custom (Windows reports unmanaged)";
  return "Managed by Windows";
}

function vmMaxUsable(vm) {
  if (vm.max_custom_gb != null) return vm.max_custom_gb;
  if (vm.free_gb != null)
    return Math.max(1, Math.min(64, Math.floor(vm.free_gb - 5)));
  return 64;
}

function vmSizeHint(vm) {
  return vm.free_gb != null
    ? `Up to ${vmMaxUsable(vm)} GB usable (keeps 5 GB headroom)`
    : "Up to -- GB available on the system drive";
}

function renderVmHeader($, vm) {
  const current = vm.current || {};
  const curEl = $("vm-current");
  if (curEl) curEl.textContent = vmCurrentText(vm, current);
  const freeEl = $("vm-free");
  if (freeEl) {
    freeEl.textContent = vm.free_gb != null ? `${vm.free_gb} GB free on C:` : "";
  }
  const hint = $("vm-size-hint");
  if (hint) hint.textContent = vmSizeHint(vm);
}

function syncVmSize($, config, vm, isDirty, active) {
  if (isDirty || active === "vm-size") return;
  const staged = num(config.virtual_memory_size_gb ?? 16, 16);
  const shown = vm.configured_size_gb != null ? vm.configured_size_gb : staged;
  $("vm-size").value = clamp(Math.round(num(shown, 16)), 1, 64);
}

function syncVmPresets($, totalRam) {
  document.querySelectorAll("#vm-presets button").forEach((b) => {
    const mult = num(b.dataset.vmMult, 0);
    const expect = totalRam ? clamp(Math.round(totalRam * mult), 1, 64) : 0;
    const cur = num($("vm-size").value, 0);
    b.classList.toggle("active", !!expect && cur === expect);
  });
}

function renderVmNotice(notice, vm, isDirty) {
  if (!notice) return;
  if (vm && vm.ok === false && vm.msg)
    setNotice(notice, vm.msg, "error", "Virtual memory unavailable");
  else if (isDirty) setNotice(notice, "", "warn");
  else if (!notice.dataset.sticky) setNotice(notice, "", "warn");
}

export function renderVirtualMemoryPanel(e) {
  const { $, VIRTUAL_MEMORY, CONFIG, dirty, updateSaveState } = e;
  if (!$("vm-size")) return;
  const active = document.activeElement ? document.activeElement.id || "" : "";
  const vm = VIRTUAL_MEMORY || {};
  const isDirty = dirty("virtual-memory");
  renderVmHeader($, vm);
  syncVmSize($, CONFIG, vm, isDirty, active);
  syncVmPresets($, num(vm.total_ram_gb, 0));
  renderVmNotice($("vm-notice"), vm, isDirty);
  updateSaveState("virtual-memory");
}

export async function saveVirtualMemoryStage(e) {
  const { $, api, clearDirty, toast, loadConfig, renderVirtualMemory } = e;
  const size = clamp(Math.round(num($("vm-size").value, 16)), 1, 64);
  try {
    const res = await api("/performance/virtual-memory", "POST", { mode: "custom", size_gb: size });
    clearDirty("virtual-memory");
    renderVirtualMemory(res);
    toast("Virtual memory staged — press Apply");
    await loadConfig();
    return res;
  } catch (err) {
    setNotice($("vm-notice"), err.message, "error", "Save failed");
    toast(err.message, "error");
    return e.VIRTUAL_MEMORY;
  }
}

function vmButtons() {
  return {
    applyBtn: document.getElementById("vm-apply"),
    revertBtn: document.getElementById("vm-revert"),
  };
}

function setVmButtonsDisabled(buttons, disabled) {
  if (buttons.applyBtn) buttons.applyBtn.disabled = disabled;
  if (buttons.revertBtn) buttons.revertBtn.disabled = disabled;
}

function vmSpaceMessage(size, freeGb) {
  if (!Number.isFinite(freeGb) || freeGb <= 0 || freeGb >= size + 5) return "";
  return `Not enough free space on C: (${freeGb} GB free, need ~${size + 5} GB headroom)`;
}

function vmRebootModal(e) {
  try {
    if (e.modal)
      e.modal(
        "Reboot required",
        "Virtual memory was updated. Reboot Windows from the Start menu to apply it.",
        '<button class="btn ghost" id="vm-reboot-later">Later</button><button class="btn good" id="vm-reboot-ok">OK</button>',
      );
    const close = () => {
      try {
        e.closeModal && e.closeModal();
      } catch (_) {}
    };
    const later = document.getElementById("vm-reboot-later");
    const ok = document.getElementById("vm-reboot-ok");
    if (later) later.onclick = close;
    if (ok) ok.onclick = close;
  } catch (_) {}
}

function vmApplySuccess(e, toast, notice, size) {
  const msg = `Pagefile set to ${size} GB. Windows must reboot before it takes effect.`;
  setNotice(notice, msg, "warn", "Reboot required");
  if (notice) notice.dataset.sticky = "1";
  toast("Applied — reboot Windows to take effect", "warning");
  vmRebootModal(e);
}

function vmApplyFailure(notice, toast, res) {
  const msg = (res && res.msg) || "Apply failed";
  setNotice(notice, msg, "error", "Apply failed");
  toast(msg, "error");
}

export async function applyVirtualMemoryPanel(e) {
  const { $, api, toast, renderVirtualMemory } = e;
  const size = clamp(Math.round(num($("vm-size").value, 16)), 1, 64);
  const notice = $("vm-notice");
  const buttons = vmButtons();
  const blocked = vmSpaceMessage(
    size,
    num(e.VIRTUAL_MEMORY && e.VIRTUAL_MEMORY.free_gb, NaN),
  );
  if (blocked) {
    setNotice(notice, blocked, "error", "Apply blocked");
    toast(blocked, "error");
    return { ok: false, msg: blocked };
  }
  try {
    setVmButtonsDisabled(buttons, true);
    if (notice) delete notice.dataset.sticky;
    setNotice(notice, "Writing the new pagefile setting to Windows…", "info", "Applying");
    const res = await api("/performance/virtual-memory/apply", "POST", { mode: "custom", size_gb: size });
    renderVirtualMemory(res);
    if (res && res.ok) vmApplySuccess(e, toast, notice, size);
    else vmApplyFailure(notice, toast, res);
    return res;
  } catch (err) {
    setNotice(notice, err.message, "error", "Apply failed");
    toast(err.message, "error");
    return null;
  } finally {
    setVmButtonsDisabled(buttons, false);
  }
}

function revertSuccess(toast, notice) {
  setNotice(notice, "Back to System Managed. Reboot Windows to take effect.", "warn", "Reboot required");
  if (notice) notice.dataset.sticky = "1";
  toast("Reverted to System Managed — reboot to take effect", "warning");
}

function revertFailure(notice, toast, res) {
  const msg = (res && res.msg) || "Revert failed";
  setNotice(notice, msg, "error", "Revert failed");
  toast(msg, "error");
}

export async function revertVirtualMemoryPanel(e) {
  const { api, toast, renderVirtualMemory } = e;
  const notice = document.getElementById("vm-notice");
  const buttons = vmButtons();
  try {
    setVmButtonsDisabled(buttons, true);
    if (notice) delete notice.dataset.sticky;
    setNotice(notice, "Restoring Windows-managed pagefile…", "info", "Working");
    const res = await api("/performance/virtual-memory/apply", "POST", { mode: "system_managed", size_gb: 16 });
    renderVirtualMemory(res);
    if (res && res.ok) revertSuccess(toast, notice);
    else revertFailure(notice, toast, res);
    return res;
  } catch (err) {
    setNotice(notice, err.message, "error", "Revert failed");
    toast(err.message, "error");
    return null;
  } finally {
    setVmButtonsDisabled(buttons, false);
  }
}
