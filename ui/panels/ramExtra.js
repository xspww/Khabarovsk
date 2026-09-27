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
const NOTICE_TONE_CLASS = { error: "notice-error", ok: "notice-success", info: "notice-info", warn: "" };

function paintNotice(el, tone) {
  const cls = NOTICE_TONE_CLASS[tone] || "";
  el.classList.remove("notice-error", "notice-success", "notice-info");
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
    el.classList.remove("show", "notice-error", "notice-success", "notice-info");
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

export function renderRamCleanupPanel(e) {
  const { $, RAM_CLEANUP, CONFIG, dirty, updateSaveState } = e;
  if (!$("ram-cleanup-enabled")) return;
  const active = document.activeElement ? document.activeElement.id || "" : "";
  const src = (RAM_CLEANUP && typeof RAM_CLEANUP === "object" && "enabled" in RAM_CLEANUP) ? RAM_CLEANUP : null;
  const enabled = src ? !!src.enabled : !!CONFIG.ram_cleanup_enabled;
  const threshold = (src && src.threshold_pct != null) ? src.threshold_pct : (CONFIG.ram_cleanup_threshold_pct ?? 85);
  const interval = (src && src.interval_min != null) ? src.interval_min : (CONFIG.ram_cleanup_interval_min ?? 15);
  if (!dirty("ram-cleanup") && !["ram-cleanup-enabled", "ram-cleanup-threshold", "ram-cleanup-interval"].includes(active)) {
    $("ram-cleanup-enabled").checked = enabled;
    $("ram-cleanup-threshold").value = threshold;
    $("ram-cleanup-interval").value = interval;
  }
  const pct = src && src.current ? num(src.current.percent, NaN) : NaN;
  const freed = src ? num(src.last_freed_mb, 0) : 0;
  const status = $("ram-cleanup-status");
  if (status) {
    if (src && src.last_run_at) {
      const when = new Date(src.last_run_at * 1000).toLocaleTimeString();
      status.textContent = `Last cleaned ${when} · Freed ${freed} MB` +
        (Number.isFinite(pct) ? ` · Now ${pct.toFixed(1)}%` : "");
    } else if (Number.isFinite(pct)) {
      status.textContent = `Current ${pct.toFixed(1)}% — not cleaned yet`;
    } else {
      status.textContent = "Not cleaned yet";
    }
  }
  const notice = $("ram-cleanup-notice");
  if (notice) {
    if (src && !src.ok && src.msg) setNotice(notice, src.msg, "error", "RAM Cleanup unavailable");
    else setNotice(notice, "", "warn");
  }
  updateSaveState("ram-cleanup");
}

export async function saveRamCleanupPanel(e) {
  const { $, api, clearDirty, toast, loadConfig, manualSnapshot, renderRamCleanup } = e;
  const payload = {
    enabled: $("ram-cleanup-enabled").checked,
    threshold_pct: clamp(Math.round(num($("ram-cleanup-threshold").value, 85)), 50, 95),
    interval_min: clamp(Math.round(num($("ram-cleanup-interval").value, 15)), 5, 120),
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

export async function cleanNowRamCleanup(e) {
  const { $, api, toast, renderRamCleanup } = e;
  const btn = $("ram-cleanup-now");
  const notice = $("ram-cleanup-notice");
  try {
    if (btn) btn.disabled = true;
    setNotice(notice, "Cleaning memory, please wait…", "info", "Working");
    const res = await api("/performance/ram-cleanup/clean-now", "POST", { source: "manual" });
    if (!res || res.ok === false) {
      const msg = (res && res.msg) || "Cleanup blocked";
      setNotice(notice, msg, "error", "Cleanup blocked");
      toast(msg, "error");
      return res;
    }
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

export function renderVirtualMemoryPanel(e) {
  const { $, VIRTUAL_MEMORY, CONFIG, dirty, updateSaveState } = e;
  if (!$("vm-size")) return;
  const active = document.activeElement ? document.activeElement.id || "" : "";
  const vm = VIRTUAL_MEMORY || {};
  const totalRam = num(vm.total_ram_gb, 0);
  const current = vm.current || {};
  const isDirty = dirty("virtual-memory");
  const curEl = $("vm-current");
  if (curEl) {
    if (current.mode === "custom" && current.size_gb != null) {
      curEl.textContent = `Custom ${current.size_gb} GB`;
    } else if (vm.managed_by_windows === false) {
      curEl.textContent = "Custom (Windows reports unmanaged)";
    } else {
      curEl.textContent = "Managed by Windows";
    }
  }
  const freeEl = $("vm-free");
  if (freeEl) {
    freeEl.textContent = vm.free_gb != null ? `${vm.free_gb} GB free on C:` : "";
  }
  const hint = $("vm-size-hint");
  if (hint) {
    hint.textContent = vm.free_gb != null
      ? `Up to ${vm.free_gb} GB available on the system drive`
      : "Up to -- GB available on the system drive";
  }
  if (!isDirty && active !== "vm-size") {
    const staged = num(CONFIG.virtual_memory_size_gb ?? 16, 16);
    const shown = (vm.configured_size_gb != null) ? vm.configured_size_gb : staged;
    $("vm-size").value = clamp(Math.round(num(shown, 16)), 1, 64);
  }
  const presets = document.querySelectorAll("#vm-presets button");
  presets.forEach((b) => {
    const mult = num(b.dataset.vmMult, 0);
    const expect = totalRam ? clamp(Math.round(totalRam * mult), 1, 64) : 0;
    const cur = num($("vm-size").value, 0);
    b.classList.toggle("active", !!expect && cur === expect);
  });
  const notice = $("vm-notice");
  if (notice) {
    if (vm && vm.ok === false && vm.msg) setNotice(notice, vm.msg, "error", "Virtual memory unavailable");
    else if (isDirty) setNotice(notice, "", "warn");
    else if (!notice.dataset.sticky) setNotice(notice, "", "warn");
  }
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

export async function applyVirtualMemoryPanel(e) {
  const { $, api, toast, renderVirtualMemory, modal } = e;
  const size = clamp(Math.round(num($("vm-size").value, 16)), 1, 64);
  const notice = $("vm-notice");
  try {
    if (notice) delete notice.dataset.sticky;
    setNotice(notice, "Writing the new pagefile setting to Windows…", "info", "Applying");
    const res = await api("/performance/virtual-memory/apply", "POST", { mode: "custom", size_gb: size });
    renderVirtualMemory(res);
    if (res && res.ok) {
      const msg = `Pagefile set to ${size} GB. Windows must reboot before it takes effect.`;
      setNotice(notice, msg, "warn", "Reboot required");
      if (notice) notice.dataset.sticky = "1";
      toast("Applied — reboot Windows to take effect", "warning");
      try {
        if (modal) modal("Reboot required", "Virtual memory was updated. Reboot Windows from the Start menu to apply it.", '<button class="btn ghost" id="vm-reboot-later">Later</button><button class="btn good" id="vm-reboot-ok">OK</button>');
        const later = document.getElementById("vm-reboot-later");
        const ok = document.getElementById("vm-reboot-ok");
        if (later) later.onclick = () => { try { e.closeModal && e.closeModal(); } catch (_) {} };
        if (ok) ok.onclick = () => { try { e.closeModal && e.closeModal(); } catch (_) {} };
      } catch (_) {}
    } else {
      setNotice(notice, (res && res.msg) || "Apply failed", "error", "Apply failed");
      toast((res && res.msg) || "Apply failed", "error");
    }
    return res;
  } catch (err) {
    setNotice(notice, err.message, "error", "Apply failed");
    toast(err.message, "error");
    return null;
  }
}

export async function revertVirtualMemoryPanel(e) {
  const { api, toast, renderVirtualMemory } = e;
  const notice = document.getElementById("vm-notice");
  try {
    if (notice) delete notice.dataset.sticky;
    setNotice(notice, "Restoring Windows-managed pagefile…", "info", "Working");
    const res = await api("/performance/virtual-memory/apply", "POST", { mode: "system_managed", size_gb: 16 });
    renderVirtualMemory(res);
    setNotice(notice, "Back to System Managed. Reboot Windows to take effect.", "warn", "Reboot required");
    if (notice) notice.dataset.sticky = "1";
    toast("Reverted to System Managed — reboot to take effect", "warning");
    return res;
  } catch (err) {
    setNotice(notice, err.message, "error", "Revert failed");
    toast(err.message, "error");
    return null;
  }
}
