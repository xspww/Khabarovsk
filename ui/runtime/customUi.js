/* Custom UI enhancements - extracted from ui_dashboard.py for cache/perf.
   Optimized: reduced polling intervals, use MutationObserver where possible */
"use strict";
{
  // NOTE: nav/guard icons are inline SVG (icons.js + currentColor) — no image files.

  // ── Window grid picker ────────────────────────────────────────────
  const applyGridSelection = (cells, columns, rows) => {
    cells.forEach((cell) => {
      const active =
        Number(cell.dataset.column) <= columns &&
        Number(cell.dataset.row) <= rows;
      cell.classList.toggle("is-selected", active);
    });
  };

  const readGridSize = (select) => {
    const [columns, rows] = String(select.value || "6x4")
      .split("x")
      .map(Number);
    return {
      columns: Math.max(1, Math.min(12, columns || 6)),
      rows: Math.max(1, Math.min(12, rows || 4)),
    };
  };

  const syncGridPicker = (select, cells, summary) => {
    const { columns, rows } = readGridSize(select);
    applyGridSelection(cells, columns, rows);
    summary.textContent = `${columns} x ${rows} (${columns * rows} windows)`;
  };

  const ensureGridOption = (select, value, label) => {
    const existing = Array.from(select.options).find(
      (item) => item.value === value,
    );
    if (existing) return;
    select.add(new Option(label, value));
  };

  const createGridCell = (select, cells, summary, column, row) => {
    const cell = document.createElement("button");
    cell.type = "button";
    cell.className = "window-grid-cell";
    cell.dataset.column = String(column);
    cell.dataset.row = String(row);
    cell.setAttribute("role", "gridcell");
    cell.setAttribute("aria-label", `${column} columns by ${row} rows`);
    cell.addEventListener("click", () => {
      const value = `${column}x${row}`;
      ensureGridOption(select, value, `${column} x ${row} (${column * row})`);
      select.value = value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
      syncGridPicker(select, cells, summary);
    });
    return cell;
  };

  const initGridPicker = () => {
    const select = document.getElementById("window-grid-preset");
    if (!select) return;
    const host = select.parentElement;
    if (!host) return;
    host
      .querySelector(".custom-select")
      ?.classList.add("grid-picker-native-hidden");
    if (select.dataset.gridPickerReady === "1") return;
    select.dataset.gridPickerReady = "1";
    const picker = document.createElement("div");
    picker.className = "window-grid-picker";
    picker.setAttribute("role", "grid");
    const summary = document.createElement("div");
    summary.className = "window-grid-summary";
    const cells = [];
    for (let row = 1; row <= 12; row += 1) {
      for (let column = 1; column <= 12; column += 1) {
        const cell = createGridCell(select, cells, summary, column, row);
        picker.appendChild(cell);
        cells.push(cell);
      }
    }
    host.append(picker, summary);
    select.addEventListener("change", () =>
      syncGridPicker(select, cells, summary),
    );
    syncGridPicker(select, cells, summary);
  };

  if (!document.getElementById("window-grid-picker-style")) {
    const style = document.createElement("style");
    style.id = "window-grid-picker-style";
    style.textContent =
      ".grid-picker-native-hidden{display:none!important}.window-grid-picker{display:grid;grid-template-columns:repeat(12,minmax(10px,1fr));gap:2px;width:198px;padding:7px;background:#0d1019;border:1px solid #282a31;border-radius:5px}.window-grid-cell{width:100%;aspect-ratio:1;border:1px solid #282a31;border-radius:2px;background:#131419;padding:0;cursor:pointer}.window-grid-cell:hover,.window-grid-cell.is-selected{background:#5962a9;border-color:#8290ef}.window-grid-cell:hover{box-shadow:0 0 0 1px #a5b4fc inset}.window-grid-summary{margin-top:5px;color:#cdced4;font:700 11px var(--mono)}";
    document.head.appendChild(style);
  }
  initGridPicker();
  // Replaced 500ms polling with observer + lazy retry (was heavy layout thrash)
  let _gridPickerObserved = false;
  const _ensureGridPicker = () => {
    if (
      document.getElementById("window-grid-preset")?.dataset.gridPickerReady ===
      "1"
    )
      return;
    initGridPicker();
  };
  if (!_gridPickerObserved) {
    _gridPickerObserved = true;
    const _gridObserver = new MutationObserver(() => {
      if (document.hidden) return;
      _ensureGridPicker();
      if (
        document.getElementById("window-grid-preset")?.dataset
          .gridPickerReady === "1"
      ) {
        try {
          _gridObserver.disconnect();
        } catch (_) {}
      }
    });
    _gridObserver.observe(document.body, { childList: true, subtree: true });
    // fallback single retry after dashboard render
    setTimeout(_ensureGridPicker, 1500);
  }

  // ── Range fill ────────────────────────────────────────────────────
  const rangeFillPercent = (el) => {
    const min = Number(el.min),
      max = Number(el.max),
      value = Number(el.value);
    if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min)
      return null;
    return Math.max(
      0,
      Math.min(
        100,
        (((Number.isFinite(value) ? value : min) - min) / (max - min)) * 100,
      ),
    );
  };

  window.__paintRange = (el) => {
    try {
      if (!el || el.type !== "range") return;
      const percent = rangeFillPercent(el);
      if (percent === null) return;
      el.style.setProperty("--fill", percent.toFixed(1) + "%");
    } catch (_) {}
  };
  document.addEventListener(
    "input",
    (e) => {
      const r =
        e.target instanceof Element
          ? e.target.closest(".volume-level-row input[type=range]")
          : null;
      if (r) window.__paintRange(r);
    },
    true,
  );

  // ── Toasts ────────────────────────────────────────────────────────
  const apiHeaders = () => {
    const token =
      document.querySelector('meta[name="cronus-api-token"]')?.content || "";
    return token ? { "X-Cronus-Token": token } : {};
  };

  // Same look as the feedback toast (icon + auto type), without importing it.
  const TOAST_ICONS_SHARED = {
    success:
      '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#5e8bff"/><path d="M8.2 12.2l2.6 2.6 4.5-5" stroke="white" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    warning:
      '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#f59e0b"/><path d="M12 7.5v5.2" stroke="white" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="16.2" r="1.2" fill="white"/></svg>',
    error:
      '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#ef4444"/><path d="M15 9l-6 6M9 9l6 6" stroke="white" stroke-width="2" stroke-linecap="round"/></svg>',
  };
  const RESULT_WARNING_PHRASES = ["unsaved", "save changes"];
  const RESULT_ERROR_PHRASES = [
    "error",
    "failed",
    "invalid",
    "cannot",
    "denied",
    "missing",
    "not found",
    "required",
  ];
  const FARM_START_WARNINGS = ["blocked", "skipped", "unavailable"];

  const includesAny = (text, needles) =>
    needles.some((needle) => text.includes(needle));

  const isBlockedLaunchable = (text) =>
    text.includes("blocked") && text.includes("launchable");

  const farmStartedType = (text) =>
    includesAny(text, FARM_START_WARNINGS) ? "warning" : "success";

  const pickResultType = (message) => {
    const text = String(message || "").toLowerCase();
    if (text.includes("farm started")) return farmStartedType(text);
    if (includesAny(text, RESULT_WARNING_PHRASES)) return "warning";
    if (includesAny(text, RESULT_ERROR_PHRASES))
      return isBlockedLaunchable(text) ? "warning" : "error";
    return isBlockedLaunchable(text) ? "warning" : "success";
  };

  const flipToastShift = (h, s) => {
    try {
      const kids = [...h.children].filter((n) => n !== s);
      const first = new Map(
        kids.map((n) => [n, n.getBoundingClientRect().top]),
      );
      return () => {
        kids.forEach((n) => {
          if (!n.isConnected) return;
          const a = first.get(n),
            b = n.getBoundingClientRect().top,
            d = a - b;
          if (a === undefined || !d) return;
          n.style.transition = "none";
          n.style.transform = `translateY(${d}px)`;
          void n.offsetHeight;
          n.style.transition = "";
          n.style.transform = "";
        });
      };
    } catch (_) {
      return () => {
        /* intentional: no-op */
      };
    }
  };

  const trimToastStack = (host) => {
    while (host.children.length >= 4) {
      const old = host.firstElementChild;
      try {
        old && clearTimeout(old._timer);
      } catch (_) {}
      if (!old) return;
      const play = flipToastShift(host, old);
      old.remove();
      play();
    }
  };

  const buildToastItem = (text, type) => {
    const item = document.createElement("div");
    item.className = `toast-item toast-${type}`;
    const icon = document.createElement("span");
    icon.className = "toast-icon";
    icon.innerHTML = TOAST_ICONS_SHARED[type] || TOAST_ICONS_SHARED.success;
    const label = document.createElement("span");
    label.className = "toast-text";
    label.textContent = text;
    item.append(icon, label);
    return item;
  };

  const scheduleToastDismiss = (host, item) => {
    item._timer = setTimeout(() => {
      item.classList.remove("show");
      item.classList.add("hide");
      setTimeout(() => {
        const drop = flipToastShift(host, item);
        item.remove();
        drop();
      }, 200);
    }, 3200);
  };

  const showResultToast = (message) => {
    const host = document.getElementById("toast");
    if (!host) return;
    const text = String(message || "");
    const type = pickResultType(text);
    trimToastStack(host);
    const item = buildToastItem(text, type);
    const play = flipToastShift(host);
    host.appendChild(item);
    play();
    requestAnimationFrame(() => item.classList.add("show"));
    scheduleToastDismiss(host, item);
  };

  // ── Window controls + visibility card ─────────────────────────────
  const moveWindowControlsTop = () => {
    const rows = document.querySelector("#window-settings-card .queue-rows");
    if (!rows) return;
    const ids = [
      "window-grid-preset",
      "window-size-preset",
      "window-rearrange-now",
    ];
    const controls = ids
      .map((id) => document.getElementById(id)?.closest(".queue-row"))
      .filter(Boolean);
    controls.reverse().forEach((row) => rows.prepend(row));
  };
  moveWindowControlsTop();

  const markWindowSizeDirty = () => {
    try {
      if (window.__cronusMarkDirty) window.__cronusMarkDirty("window-size");
    } catch (_) {}
  };

  const hideEnabledFrom = (data) => {
    if (typeof data.hide_enabled === "boolean") return !!data.hide_enabled;
    const hidden = Number(data.hidden_windows || 0);
    const visible = Number(data.visible_windows || 0);
    return hidden > 0 && visible === 0;
  };

  const syncVisibilityToggle = async (toggle) => {
    if (!toggle || toggle.disabled) return;
    try {
      const res = await fetch("/api/performance/window-size", {
        headers: { ...apiHeaders() },
      });
      const data = await res.json().catch(() => ({}));
      toggle.checked = hideEnabledFrom(data);
    } catch (_) {}
  };

  const wireVisibilityToggle = (card) => {
    const toggle = card.querySelector("#window-hide-enabled");
    toggle?.addEventListener("change", () => {
      // Staged only: the shared Windows savebar (Save/Reset) applies it.
      markWindowSizeDirty();
    });
    syncVisibilityToggle(toggle);
  };

  const VISIBILITY_CARD_HTML =
    '<div class="queue-card-head"><div class="queue-card-title">Visibility<div class="hint">Show or hide game windows</div></div></div><div class="queue-rows"><div class="queue-row"><div class="queue-row-copy"><strong>Hide Roblox windows</strong><div class="hint">Hidden from screen and taskbar, still listed as Roblox in Task Manager.</div></div><div class="queue-row-input"><div class="toggle-row"><input id="window-hide-enabled" type="checkbox"><span hidden></span></div></div></div></div>';

  const ensureVisibilityCard = () => {
    try {
      document.getElementById("window-hide-row")?.remove();
    } catch (_) {}
    if (document.getElementById("window-visibility-card")) return;
    const anchor = document.getElementById("window-settings-card");
    if (!anchor || !anchor.parentElement) return;
    const card = document.createElement("section");
    card.className = "queue-card";
    card.id = "window-visibility-card";
    card.innerHTML = VISIBILITY_CARD_HTML;
    anchor.after(card);
    wireVisibilityToggle(card);
  };
  ensureVisibilityCard();
  new MutationObserver(() => {
    if (!document.hidden) ensureVisibilityCard();
  }).observe(document.body, { childList: true, subtree: true });

  // ── Troubleshoot labels ───────────────────────────────────────────
  document
    .querySelector('#nav button[data-view="troubleshoot"] .nav-text')
    ?.replaceChildren("Exploits Manager");
  document
    .querySelector("#view-troubleshoot .page-head .title")
    ?.replaceChildren("Exploits Manager");
  const currentVersionLabel = document.querySelector(
    "#view-troubleshoot .install-status",
  );
  if (currentVersionLabel)
    currentVersionLabel.childNodes.forEach((node) => {
      if (
        node.nodeType === Node.TEXT_NODE &&
        node.textContent.includes("Currently installed:")
      )
        node.textContent = "Current Version: ";
    });

  // Search + running indicators are inline SVG (magnifer icon, status clock) — no image files.
  const favicon = document.querySelector('link[rel="icon"]');
  if (favicon) favicon.href = "/assets/cronus_icon.png";

  // ── Close-selected modal ──────────────────────────────────────────
  const escPick = (value) => {
    const node = document.createElement("span");
    node.textContent = String(value || "");
    return node.innerHTML;
  };

  const CLOSE_STATE_KEYS = {
    IN_GAME: "online",
    QUEUED: "queued",
    LAUNCHING: "launching",
    COOLDOWN: "cooldown",
  };

  const stateKeyOf = (state) =>
    CLOSE_STATE_KEYS[String(state || "").toUpperCase()] || "unknown";

  const stateLabelOf = (state) => {
    const s = String(state || "").trim();
    if (!s) return "";
    return s.charAt(0).toUpperCase() + s.slice(1).toLowerCase().replace(/_/g, " ");
  };

  const closeClientName = (client, pid) =>
    String(client.display || client.account || "").trim() || `PID ${pid}`;

  const closeClientState = (client) => {
    if (!client.account) return { key: "unknown", label: "Unmanaged" };
    return {
      key: stateKeyOf(client.state),
      label: stateLabelOf(client.state) || "Running",
    };
  };

  const closeClientRow = (client) => {
    const pid = Number(client.pid) || 0;
    const name = closeClientName(client, pid);
    const bound = Boolean(client.account);
    const state = closeClientState(client);
    const sub =
      bound || !client.rss_mb ? "" : ` · ${Math.round(client.rss_mb)} MB`;
    return `<label class="close-pick is-checked"><input type="checkbox" data-close-pid="${pid}" checked><span class="close-check" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="M5 12.5l4.5 4.5L19 7.5" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg></span><span class="close-name">${escPick(name)}${bound ? "" : ` <span class="hint">PID ${pid}</span>`}</span><span class="close-st st-${state.key}">${escPick(state.label)}${escPick(sub)}</span></label>`;
  };

  const renderCloseList = (clients) => {
    if (!clients.length)
      return '<div class="v-empty">No running Roblox clients.</div>';
    return clients.map(closeClientRow).join("");
  };

  const setClosePicks = (body, checked) => {
    body.querySelectorAll("[data-close-pid]").forEach((input) => {
      input.checked = checked;
    });
  };

  const syncClosePicks = (body, confirmBtn) => {
    const boxes = Array.from(body.querySelectorAll("[data-close-pid]"));
    const n = boxes.filter((input) => input.checked).length;
    boxes.forEach((input) =>
      input
        .closest(".close-pick")
        ?.classList.toggle("is-checked", input.checked),
    );
    if (!confirmBtn) return;
    confirmBtn.textContent = n ? `Close Selected (${n})` : "Close Selected";
    confirmBtn.disabled = !n;
  };

  const wireCloseList = (body, confirmBtn) => {
    body
      .querySelector(".selected-close-list")
      ?.addEventListener("change", () => syncClosePicks(body, confirmBtn));
    document
      .getElementById("close-select-all")
      ?.addEventListener("click", () => {
        setClosePicks(body, true);
        syncClosePicks(body, confirmBtn);
      });
    document
      .getElementById("close-clear")
      ?.addEventListener("click", () => {
        setClosePicks(body, false);
        syncClosePicks(body, confirmBtn);
      });
  };

  const renderCloseClients = (body, confirmBtn, clients) => {
    const list = body.querySelector(".selected-close-list");
    if (list) list.innerHTML = renderCloseList(clients);
    syncClosePicks(body, confirmBtn);
  };

  const loadCloseClients = (body, confirmBtn) => {
    fetch("/api/roblox/running-clients", {
      cache: "no-store",
      headers: { ...apiHeaders() },
    })
      .then((response) => response.json())
      .then((result) => {
        const clients = Array.isArray(result?.clients)
          ? result.clients
          : [];
        renderCloseClients(body, confirmBtn, clients);
      })
      .catch(() => {
        const list = body.querySelector(".selected-close-list");
        if (list)
          list.innerHTML =
            '<div class="v-empty">Could not load running clients.</div>';
        syncClosePicks(body, confirmBtn);
      });
  };

  const selectedClosePids = (body) =>
    Array.from(body.querySelectorAll("[data-close-pid]:checked"))
      .map((input) => Number(input.dataset.closePid) || 0)
      .filter(Boolean);

  const confirmCloseSelected = async (body, backdrop) => {
    const pids = selectedClosePids(body);
    if (!pids.length) return;
    const response = await fetch("/api/roblox/close-pids", {
      method: "POST",
      headers: { ...apiHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ pids }),
    });
    const result = await response.json();
    backdrop.hidden = true;
    showResultToast(result.msg || "Roblox closed");
  };

  const openCloseSelectedModal = () => {
    const backdrop = document.getElementById("modal-backdrop");
    const title = document.getElementById("modal-title");
    const body = document.getElementById("modal-body");
    const foot = document.getElementById("modal-foot");
    if (!backdrop || !title || !body || !foot) return;
    // List only Roblox clients actually running on this machine
    // (verified live PIDs). Show the bound account name when known.
    title.textContent = "Close Roblox";
    body.innerHTML =
      '<div class="v-sub">Select the running Roblox clients to close.</div><div class="selected-close-list"><div class="v-empty">Loading running clients…</div></div>';
    foot.innerHTML =
      '<div class="close-pick-foot"><div class="close-pick-tools"><button class="btn ghost" id="close-select-all">Select All</button><button class="btn ghost" id="close-clear">Clear</button></div><button class="btn danger" id="close-selected-confirm">Close Selected</button></div>';
    backdrop.hidden = false;
    const confirmBtn = document.getElementById("close-selected-confirm");
    wireCloseList(body, confirmBtn);
    syncClosePicks(body, confirmBtn);
    loadCloseClients(body, confirmBtn);
    document
      .getElementById("close-selected-confirm")
      ?.addEventListener("click", () => confirmCloseSelected(body, backdrop));
  };

  const closeButton = document.getElementById("close-all-roblox-btn");
  if (closeButton && closeButton.dataset.selectedCloseReady !== "1") {
    closeButton.dataset.selectedCloseReady = "1";
    closeButton.addEventListener(
      "click",
      (event) => {
        event.preventDefault();
        event.stopImmediatePropagation();
        openCloseSelectedModal();
      },
      true,
    );
  }

  // NOTE: the installed-version text and dot are owned solely by
  // renderTroubleshootPanel (single writer). An earlier revision overwrote
  // the text with latest_version here every 30s, which fought the panel
  // and could show a version that is not actually installed.
  document
    .querySelector("#roblox-latest span")
    ?.replaceChildren("Update to Latest");
  // NOTE: do NOT post-filter `.vchoice` rows here. An earlier revision removed
  // `.vbadge.old` rows via MutationObserver after paint, which made the Join
  // game picker flash "2 Roblox rows then 1 disappears". The version list is
  // already deduplicated by the backend and rendered once by dashboard.js.

  // ── Unified limiter savebar ───────────────────────────────────────
  const syncLimiterActions = () => {
    const limiterSave = document.getElementById("limiter-save");
    const limiterReset = document.getElementById("limiter-reset");
    const graphicsSave = document.getElementById("graphics-save");
    const graphicsReset = document.getElementById("graphics-reset");
    if (!limiterSave || !limiterReset || !graphicsSave || !graphicsReset)
      return;
    const graphicsDirty = graphicsSave.classList.contains("settings-dirty");
    const limiterDirty = limiterSave.classList.contains("settings-dirty");
    const anyDirty = graphicsDirty || limiterDirty;
    limiterSave.hidden = !anyDirty;
    limiterReset.hidden = !anyDirty;
    graphicsSave.hidden = true;
    graphicsReset.hidden = true;
    if (limiterSave.dataset.unifiedActions === "1") return;
    limiterSave.dataset.unifiedActions = "1";
    limiterSave.addEventListener(
      "click",
      () => {
        if (graphicsSave.classList.contains("settings-dirty")) {
          setTimeout(() => graphicsSave.click(), 0);
        }
      },
      true,
    );
    limiterReset.addEventListener(
      "click",
      () => {
        if (graphicsSave.classList.contains("settings-dirty")) {
          setTimeout(() => graphicsReset.click(), 0);
        }
      },
      true,
    );
  };
  syncLimiterActions();
  // Safety net: the dashboard owns savebar visibility (single unified
  // limiter pair; the graphics pair stays hidden as click-targets). Re-sync
  // after dashboard updates — it toggles `hidden`, not just classes, so
  // observe both and run deferred to win ordering races.
  const _limiterRoot =
    document.getElementById("view-limiter") ||
    document.getElementById("limiter-save")?.closest(".page-head") ||
    document.body;
  let _limiterSyncQueued = false;
  const _queueLimiterSync = () => {
    if (_limiterSyncQueued || document.hidden) return;
    _limiterSyncQueued = true;
    requestAnimationFrame(() => {
      _limiterSyncQueued = false;
      try {
        syncLimiterActions();
      } catch (_) {}
    });
  };
  new MutationObserver(_queueLimiterSync).observe(_limiterRoot, {
    attributes: true,
    subtree: true,
    attributeFilter: ["class", "hidden"],
  });

  // ── Floating description editor ───────────────────────────────────
  // The dashboard renders the editor inline in the row and handles
  // save/cancel/toolbar itself. We only lift the editor node visually
  // (position:fixed) so it looks like a popup menu.
  // Dashboard handlers stopPropagation on the table, so listen in capture.
  const FLOAT_W = 360;
  const FLOAT_H = 320;
  const descDrafts = {};
  let floatingDescUser = "";
  let floatingDescPos = { x: 0, y: 0 };
  let restoringDesc = false;
  const descKey = (user) => String(user || "").toLowerCase();

  const placeDescEditor = (editor, x, y) => {
    const left = Math.max(
      8,
      Math.min(Math.round(x) + 8, window.innerWidth - FLOAT_W - 8),
    );
    let top = Math.round(y) + 8;
    if (top + FLOAT_H > window.innerHeight - 8)
      top = Math.max(8, Math.round(y) - FLOAT_H - 8);
    const wantLeft = `${left}px`,
      wantTop = `${top}px`;
    if (editor.style.left !== wantLeft) editor.style.left = wantLeft;
    if (editor.style.top !== wantTop) editor.style.top = wantTop;
  };

  const floatDescEditor = (x, y) => {
    const editor = document.querySelector("#accounts-table .desc-editor");
    if (!editor) return;
    const ta = editor.querySelector(".desc-textarea");
    const user = editor.dataset?.user || ta?.dataset.user || "";
    floatingDescUser = user;
    floatingDescPos = { x: x || 0, y: y || 0 };
    if (user && ta) descDrafts[descKey(user)] = ta.value;
    editor.classList.add("desc-floating");
    placeDescEditor(editor, floatingDescPos.x, floatingDescPos.y);
  };

  const restoreDescDraft = (editor, ta, user) => {
    const stash = descDrafts[descKey(user)];
    if (!ta || stash === undefined || ta.value === stash) return;
    ta.value = stash;
    const count = editor.querySelector(".desc-char-count");
    if (count) count.textContent = String(stash.length);
  };

  // A table re-render (row select, filter, live refresh while unfocused)
  // recreates the editor inline. Float it again and restore typed text.
  const refloatDescEditor = () => {
    if (restoringDesc || !floatingDescUser) return;
    if (document.querySelector("#accounts-table .desc-editor.desc-floating"))
      return;
    const editor = document.querySelector("#accounts-table .desc-editor");
    if (!editor) {
      floatingDescUser = "";
      return;
    }
    const ta = editor.querySelector(".desc-textarea");
    const user = editor.dataset?.user || ta?.dataset.user || floatingDescUser;
    floatingDescUser = user;
    restoringDesc = true;
    try {
      editor.classList.add("desc-floating");
      placeDescEditor(editor, floatingDescPos.x, floatingDescPos.y);
      restoreDescDraft(editor, ta, user);
    } finally {
      restoringDesc = false;
    }
  };

  document.addEventListener(
    "input",
    (event) => {
      const ta =
        event.target instanceof Element
          ? event.target.closest(".desc-textarea")
          : null;
      if (ta?.dataset.user) {
        descDrafts[descKey(ta.dataset.user)] = ta.value;
        // Cap: one draft per account, drop oldest beyond 50.
        const keys = Object.keys(descDrafts);
        if (keys.length > 50) delete descDrafts[keys[0]];
      }
    },
    true,
  );

  let _refloatQueued = false;
  new MutationObserver(() => {
    if (_refloatQueued || document.hidden) return;
    _refloatQueued = true;
    requestAnimationFrame(() => {
      _refloatQueued = false;
      try {
        refloatDescEditor();
      } catch (_) {}
    });
  }).observe(document.body, { childList: true, subtree: true });

  const refocusDescTextarea = () => {
    const ta = document.querySelector(
      "#accounts-table .desc-editor.desc-floating .desc-textarea",
    );
    if (ta && document.activeElement !== ta) {
      try {
        ta.focus({ preventScroll: true });
      } catch (_) {
        ta.focus();
      }
    }
  };

  document.addEventListener(
    "click",
    (event) => {
      if (!(event.target instanceof Element)) return;
      if (event.target.closest('[data-action="edit-desc"]')) {
        const x = event.clientX || 0,
          y = event.clientY || 0;
        setTimeout(() => floatDescEditor(x, y), 0);
        return;
      }
      if (
        event.target.closest(".desc-editor.desc-floating .desc-tool-btn") ||
        event.target.closest(".desc-editor.desc-floating .desc-color-dot")
      ) {
        // Keep focus in the textarea so live re-renders skip the editor.
        try {
          refocusDescTextarea();
        } catch (_) {}
      }
    },
    true,
  );

  document.addEventListener("click", (event) => {
    // Neutral clicks outside the floating editor cancel it via its own button.
    if (!(event.target instanceof Element)) return;
    const floating = document.querySelector(
      "#accounts-table .desc-editor.desc-floating",
    );
    if (!floating) return;
    if (event.target.closest(".desc-editor")) return;
    const cancel = floating.querySelector('[data-action="cancel-desc"]');
    cancel?.click();
  });
}
