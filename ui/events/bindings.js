const DESC_TOOL_TAGS = {
  bold: ["<b>", "</b>", "bold"],
  italic: ["<i>", "</i>", "italic"],
  underline: ["<u>", "</u>", "underline"],
  strike: ["<s>", "</s>", "strike"],
  big: ["<big>", "</big>", "large"],
  star: ["<star>", "</star>", "highlight"],
  block: ["<block>", "</block>", "badge"],
  code: ["<code>", "</code>", "code"],
  badge: ["<badge>", "</badge>", "badge"],
};
const ROW_ACTIONS = {
  delete: "deleteAccount",
  launch: "launchAccount",
  dedupe: "dedupeAccount",
  "focus-captcha": "focusCaptcha",
  "open-captcha-login": "openCaptchaLogin",
  "resume-captcha": "resumeCaptcha",
};

function bindClick(api, id, handler) {
  const node = api.$(id);
  if (node) node.onclick = handler;
}

function bindChange(api, id, handler) {
  const node = api.$(id);
  if (node) node.onchange = handler;
}

function bindInput(api, id, handler) {
  const node = api.$(id);
  if (node) node.oninput = handler;
}

function markSection(api, section) {
  return () => api.markDirty(section);
}

function markAndRender(api, section, renderName) {
  return () => {
    api.markDirty(section);
    api[renderName]();
  };
}

function markAndMaybeRender(api, section, renderName) {
  return () => {
    api.markDirty(section);
    if (api[renderName]) api[renderName]();
  };
}

function wireNavigation(api, doc) {
  doc.querySelectorAll("#nav button[data-view]").forEach((button) => {
    button.onclick = () => api.setView(button.dataset.view);
  });
  doc.querySelectorAll("[data-nav-toggle]").forEach((button) => {
    const group = button.closest(".nav-group");
    if (!group) return;
    button.setAttribute(
      "aria-expanded",
      String(!group.classList.contains("collapsed")),
    );
    button.onclick = () => {
      const collapsed = group.classList.contains("collapsed");
      group.classList.toggle("collapsed", !collapsed);
      group.classList.toggle("expanded", collapsed);
      button.setAttribute("aria-expanded", String(collapsed));
    };
  });
}

function wireGlobalChange(api, doc) {
  doc.addEventListener("change", (event) => {
    if (event.target?.matches?.('.toggle-row input[type="checkbox"]'))
      api.syncToggleLabels();
  });
}

function wireHeaderButtons(api) {
  bindClick(api, "guard-btn", api.onGuardPress || api.toggleGuard);
  bindClick(api, "add-btn", api.openAdd);
  bindClick(api, "reload-cookies-btn", api.reloadCookies);
  const deleteSelected = api.$("delete-selected-btn");
  if (deleteSelected) {
    deleteSelected.onclick = api.deleteSelectedAccounts;
    const finishToggle = api.$("finish-toggle-btn");
    if (finishToggle) finishToggle.onclick = api.toggleFinishedAccounts;
  }
  bindClick(api, "unban-toggle-btn", api.unbanMarkSelected);
  bindClick(api, "resume-selected-btn", api.resumeSelected);
  bindClick(api, "clear-selection-btn", api.clearSelection);
  bindClick(api, "close-all-roblox-btn", api.closeAllRoblox);
  bindClick(api, "modal-close", api.closeModal);
  const search = api.$("account-search");
  if (search) {
    search.oninput = window.__cronusDebounce
      ? window.__cronusDebounce(api.renderAccountsTable, 150)
      : api.renderAccountsTable;
  }
}

function wireSettingsButtons(api) {
  bindClick(api, "game-reset", api.resetGameSettings);
  bindClick(api, "game-save", api.saveGame);
  bindClick(api, "limiter-reset", api.resetLimiterSettings);
  bindClick(api, "limiter-save", api.saveLimiter);
  bindClick(api, "ram-reset", api.resetRamViewSettings);
  bindClick(api, "ram-save", api.saveRamView);
  bindClick(api, "graphics-reset", api.resetGraphicsSettings);
  bindClick(api, "graphics-save", api.saveGraphics);
  bindClick(api, "window-size-reset", api.resetWindowSizeSettings);
  bindClick(api, "window-size-save", api.saveWindowSize);
  bindClick(api, "cronus-save", api.saveCronusSettings);
  bindClick(api, "cronus-reset", api.resetCronusSettings);
  bindClick(api, "developer-save", api.saveDeveloperSettings);
  bindClick(api, "developer-reset", api.resetDeveloperSettings);
  bindClick(api, "window-rearrange-now", api.rearrangeWindowSize);
  bindClick(api, "queue-reset", api.resetQueueSettings);
  bindClick(api, "queue-save", api.saveQueue);
  bindClick(api, "roblox-uninstall", () => api.robloxInstallConfirm("uninstall"));
  bindClick(api, "roblox-latest", () => api.robloxInstallConfirm("latest"));
  bindClick(api, "developer-copy-lua", api.copyDeveloperLua);
  bindChange(api, "dev-lua-enabled", () => {
    api.markDirty("developer");
    api.renderDeveloper();
  });
  bindInput(api, "dev-lua-timeout", () => {
    api.markDirty("developer");
  });
}

function wireGameSettings(api) {
  const filters = api.$("account-filters");
  if (filters) {
    filters.addEventListener("click", (event) => {
      const target = event.target.closest("[data-account-filter]");
      if (target) api.setAccountFilter(target.dataset.accountFilter);
    });
  }
  bindChange(
    api,
    "game-auto-private-enabled",
    markAndRender(api, "game", "renderSettings"),
  );
  bindChange(
    api,
    "game-multi-roblox",
    markAndRender(api, "game", "renderSettings"),
  );
  bindChange(
    api,
    "game-block-same-server",
    markAndRender(api, "game", "renderSettings"),
  );
}

function wireCronusToggles(api) {
  bindChange(api, "cronus-start-on-boot", markSection(api, "cronus"));
  bindChange(api, "cronus-start-farming-on-boot", markSection(api, "cronus"));
  bindChange(api, "cronus-auto-update-on-boot", markSection(api, "cronus"));
  bindChange(
    api,
    "cronus-privacy-blur",
    markAndMaybeRender(api, "cronus", "renderAccountsTable"),
  );
  bindChange(
    api,
    "cronus-privacy-random",
    markAndMaybeRender(api, "cronus", "renderAccountsTable"),
  );
  bindChange(api, "cronus-discord-enabled", markSection(api, "cronus"));
  bindChange(api, "cronus-discord-show-accounts", markSection(api, "cronus"));
  bindChange(api, "cronus-discord-show-game", markSection(api, "cronus"));
}

function setGameMode(api, doc, button) {
  const toggle = api.$("game-mode-toggle");
  if (!toggle) return;
  toggle.dataset.value = button.dataset.mode;
  toggle.querySelectorAll("button").forEach((node) => {
    node.classList.toggle("active", node === button);
  });
  doc.dispatchEvent(
    new CustomEvent("cronus:game-mode", {
      detail: {
        mode: button.dataset.mode,
        preview:
          button.dataset.mode !== (toggle.dataset.savedValue || "shared"),
      },
    }),
  );
  api.markDirty("game");
}

function wireGameMode(api, doc) {
  const toggle = api.$("game-mode-toggle");
  if (!toggle) return;
  toggle.querySelectorAll("button").forEach((button) => {
    button.onclick = () => setGameMode(api, doc, button);
  });
}

function wirePerformanceSettings(api) {
  bindChange(
    api,
    "fps-enabled",
    markAndRender(api, "performance", "renderPerformance"),
  );
  bindInput(api, "fps-limit", markSection(api, "performance"));
}

function wireRamSettings(api) {
  bindChange(api, "ram-enabled", markAndRender(api, "ram", "renderRam"));
  bindChange(
    api,
    "ram-limit-preset",
    markAndRender(api, "ram", "renderRam"),
  );
  bindInput(
    api,
    "ram-limit-custom",
    markAndRender(api, "ram", "renderRam"),
  );
  bindChange(
    api,
    "ram-cleanup-enabled",
    markAndMaybeRender(api, "ram-cleanup", "renderRamCleanup"),
  );
  bindInput(api, "ram-cleanup-threshold", markSection(api, "ram-cleanup"));
  bindInput(api, "ram-cleanup-interval", markSection(api, "ram-cleanup"));
  bindClick(api, "ram-cleanup-now", () => {
    if (api.cleanNowRam) api.cleanNowRam();
  });
}

function applyVmPreset(api, button) {
  try {
    const mult = Number(button.dataset.vmMult) || 1;
    const total = (api.getTotalRamGb && api.getTotalRamGb()) || 0;
    const sizeInput = api.$("vm-size");
    if (total && sizeInput) {
      sizeInput.value = String(
        Math.max(1, Math.min(64, Math.round(total * mult))),
      );
      api.markDirty("virtual-memory");
      if (api.renderVirtualMemory) api.renderVirtualMemory();
    }
  } catch (_) {
    /* intentional: preset is best effort */
  }
}

function wireVirtualMemory(api, doc) {
  bindInput(
    api,
    "vm-size",
    markAndMaybeRender(api, "virtual-memory", "renderVirtualMemory"),
  );
  bindClick(api, "vm-apply", () => api.applyVirtualMemory && api.applyVirtualMemory());
  bindClick(api, "vm-revert", () => api.revertVirtualMemory && api.revertVirtualMemory());
  doc.querySelectorAll("#vm-presets button").forEach((button) => {
    button.onclick = () => applyVmPreset(api, button);
  });
}

function syncLinkedRange(api, sourceId, targetId, section) {
  const target = api.$(targetId);
  if (target && document.activeElement !== target)
    target.value = String(api.$(sourceId).value);
  if (window.__paintRange) window.__paintRange(target || api.$(targetId));
  api.markDirty(section);
}

function syncRangeToInput(api, rangeId, inputId) {
  const input = api.$(inputId);
  if (input) input.value = String(api.$(rangeId).value);
  if (window.__paintRange) window.__paintRange(api.$(rangeId));
  api.markDirty("graphics");
}

function wireGraphicsSettings(api) {
  bindChange(
    api,
    "priority-enabled",
    markAndRender(api, "graphics", "renderGraphics"),
  );
  bindChange(
    api,
    "graphics-auto-enabled",
    markAndRender(api, "graphics", "renderGraphics"),
  );
  const quality = api.$("graphics-quality");
  if (quality) {
    quality.oninput = () =>
      syncLinkedRange(
        api,
        "graphics-quality",
        "graphics-quality-range",
        "graphics",
      );
  }
  const qualityRange = api.$("graphics-quality-range");
  if (qualityRange) {
    qualityRange.oninput = () =>
      syncRangeToInput(api, "graphics-quality-range", "graphics-quality");
  }
  bindChange(api, "process-priority", markSection(api, "graphics"));
  bindChange(
    api,
    "volume-muted",
    markAndRender(api, "graphics", "renderGraphics"),
  );
  const volume = api.$("volume-level");
  if (volume) {
    volume.oninput = () =>
      syncLinkedRange(api, "volume-level", "volume-level-range", "graphics");
  }
  const volumeRange = api.$("volume-level-range");
  if (volumeRange) {
    volumeRange.oninput = () =>
      syncRangeToInput(api, "volume-level-range", "volume-level");
  }
}

function wireWindowSizeSettings(api) {
  bindChange(
    api,
    "window-unlock-enabled",
    markAndRender(api, "window-size", "renderWindowSize"),
  );
  bindChange(
    api,
    "window-arrange-enabled",
    markAndRender(api, "window-size", "renderWindowSize"),
  );
  bindChange(
    api,
    "window-size-enabled",
    markAndRender(api, "window-size", "renderWindowSize"),
  );
  bindChange(
    api,
    "window-auto-minimize-enabled",
    markAndRender(api, "window-size", "renderWindowSize"),
  );
  bindInput(api, "window-auto-minimize-seconds", markSection(api, "window-size"));
  bindChange(api, "window-hide-enabled", markSection(api, "window-size"));
  const picker = api.$("window-grid-picker");
  if (picker) {
    picker.addEventListener("click", (event) => {
      const cell = event.target.closest("[data-col]");
      if (cell) {
        api.pickWindowSizeGrid(
          Number(cell.dataset.col),
          Number(cell.dataset.row),
        );
      }
    });
  }
  bindChange(
    api,
    "window-size-preset",
    markAndRender(api, "window-size", "renderWindowSize"),
  );
  bindInput(api, "window-size-width", markSection(api, "window-size"));
  bindInput(api, "window-size-height", markSection(api, "window-size"));
}

function wireCpuLimiterSettings(api) {
  bindChange(
    api,
    "cpu-enabled",
    markAndRender(api, "cpu-limiter", "renderCpuLimiter"),
  );
  bindChange(api, "cpu-mode", markSection(api, "cpu-limiter"));
  bindInput(
    api,
    "cpu-default-limit",
    markAndRender(api, "cpu-limiter", "renderCpuLimiter"),
  );
  bindChange(
    api,
    "cpu-apply-all",
    markAndRender(api, "cpu-limiter", "renderCpuLimiter"),
  );
  const rows = api.$("cpu-account-rows");
  if (rows) {
    rows.addEventListener("input", markSection(api, "cpu-limiter"));
    rows.addEventListener("change", markSection(api, "cpu-limiter"));
  }
}

function toggleQueueCard(button) {
  const card = button.closest(".queue-card");
  if (!card) return;
  const collapsed = !card.classList.contains("collapsed");
  card.classList.toggle("collapsed", collapsed);
  const body = card.querySelector(".queue-rows,.queue-card-body");
  if (body) body.hidden = collapsed;
  button.setAttribute("aria-expanded", String(!collapsed));
  try {
    const key = "rg_collapsed_" + (card.id || "card");
    localStorage.setItem(key, collapsed ? "1" : "0");
  } catch (_) {
    /* intentional: storage may be unavailable */
  }
}

function wireQueueSettings(api, doc) {
  bindChange(
    api,
    "queue-autoclose-enabled",
    markAndRender(api, "queue", "renderSettings"),
  );
  bindInput(api, "queue-autoclose-minutes", markSection(api, "queue"));
  bindChange(
    api,
    "popup-disconnected-enabled",
    markAndRender(api, "queue", "renderSettings"),
  );
  bindInput(api, "popup-scan-interval", markSection(api, "queue"));
  bindInput(api, "popup-scan-max-parallel", markSection(api, "queue"));
  bindInput(api, "queue-max", markSection(api, "queue"));
  bindInput(api, "queue-delay", markSection(api, "queue"));
  doc.querySelectorAll(".queue-collapse-btn").forEach((button) => {
    button.onclick = () => toggleQueueCard(button);
  });
}

function handleGamePlaceInput(api) {
  api.markDirty("game");
  clearTimeout(api.gameLookupTimer());
  const place = api.$("game-place").value.trim();
  api.renderGameMap(place);
  if (/^\d{3,}$/.test(place)) {
    api.setGameLookupTimer(
      setTimeout(() => {
        api
          .loadGamePlace(place, !0)
          .catch((err) => api.renderGameMap(place, null, err.message));
      }, 650),
    );
  }
}

function handleGamePrivateBlur(api) {
  if (api.$("game-place").value.trim()) return;
  const place = api.placeFromPrivate(api.$("game-private").value.trim());
  if (!place) return;
  api.$("game-place").value = place;
  api.markDirty("game");
  api
    .loadGamePlace(place, !0)
    .catch((err) => api.renderGameMap(place, null, err.message));
}

function wireGamePlace(api) {
  const place = api.$("game-place");
  if (place) place.addEventListener("input", () => handleGamePlaceInput(api));
  const privateServer = api.$("game-private");
  if (privateServer) {
    privateServer.addEventListener("input", () => api.markDirty("game"));
    privateServer.addEventListener("blur", () => handleGamePrivateBlur(api));
  }
}

function applyTagToEditor(editor, openTag, closeTag, placeholder = "") {
  const textarea = editor?.querySelector(".desc-textarea");
  if (!textarea) return;
  const start = textarea.selectionStart ?? textarea.value.length,
    end = textarea.selectionEnd ?? textarea.value.length,
    selected = textarea.value.substring(start, end),
    content = selected || placeholder || "text",
    insertion = `${openTag}${content}${closeTag}`;
  textarea.focus();
  textarea.setRangeText(insertion, start, end, "select");
  if (!selected) {
    textarea.setSelectionRange(
      start + openTag.length,
      start + openTag.length + content.length,
    );
  }
  const count = editor.querySelector(".desc-char-count");
  if (count) count.textContent = String(textarea.value.length);
}

function saveDescriptionFromClick(api, event, button) {
  event.stopPropagation();
  const user = button.dataset.user;
  const editor = button.closest(".desc-editor");
  const textarea = editor?.querySelector(".desc-textarea");
  const value = textarea ? textarea.value : "";
  return api
    .saveDescription(user, value)
    .then(() => api.cancelEditDescription?.())
    .catch((err) => api.toast(err.message));
}

function applyToolFromClick(event, button) {
  event.stopPropagation();
  const editor = button.closest(".desc-editor");
  const tag = DESC_TOOL_TAGS[button.dataset.tool];
  if (!tag) return;
  applyTagToEditor(editor, tag[0], tag[1], tag[2]);
}

function applyColorFromClick(event, dot) {
  event.stopPropagation();
  const color = dot.dataset.color;
  applyTagToEditor(dot.closest(".desc-editor"), `<color:${color}>`, "</>", color);
}

function handleDescToolsClick(api, event) {
  const editButton = event.target.closest('[data-action="edit-desc"]');
  if (editButton) {
    event.stopPropagation();
    api.startEditDescription?.(editButton.dataset.user);
    return true;
  }
  const cancelButton = event.target.closest('[data-action="cancel-desc"]');
  if (cancelButton) {
    event.stopPropagation();
    api.cancelEditDescription?.();
    return true;
  }
  const saveButton = event.target.closest('[data-action="save-desc"]');
  if (saveButton) {
    saveDescriptionFromClick(api, event, saveButton);
    return true;
  }
  const toolButton = event.target.closest(".desc-tool-btn[data-tool]");
  if (toolButton) {
    applyToolFromClick(event, toolButton);
    return true;
  }
  const colorDot = event.target.closest(".desc-color-dot[data-color]");
  if (colorDot) {
    applyColorFromClick(event, colorDot);
    return true;
  }
  return false;
}

function tableTextSelectionActive(api, doc) {
  const selection = doc.getSelection?.();
  const table = api.$("accounts-table");
  return !!(
    selection &&
    table &&
    !selection.isCollapsed &&
    String(selection).trim() &&
    table.contains(selection.anchorNode)
  );
}

function handleAccountsTableClick(api, doc, event) {
  if (handleDescToolsClick(api, event)) return;
  const actionNode = event.target.closest("[data-action]");
  if (actionNode) {
    event.stopPropagation();
    const method = ROW_ACTIONS[actionNode.dataset.action];
    if (method) api[method](actionNode.dataset.user);
    return;
  }
  if (
    event.target.closest(".desc-editor") ||
    event.target.closest(".account-copy-text") ||
    tableTextSelectionActive(api, doc)
  )
    return;
  const row = event.target.closest("tr[data-user]");
  if (!row) return;
  api.selectUser(row.dataset.user);
  api.renderAccountsTable();
}

function handleAccountsTableInput(api, event) {
  if (!event.target?.classList.contains("desc-textarea")) return;
  const count = event.target
    .closest(".desc-editor")
    ?.querySelector(".desc-char-count");
  if (count) count.textContent = String(event.target.value.length);
}

function handleAccountsTableKeydown(api, event) {
  if (!event.target?.classList.contains("desc-textarea")) return;
  if (event.key === "Escape") {
    event.preventDefault();
    api.cancelEditDescription?.();
    return;
  }
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    api
      .saveDescription(event.target.dataset.user, event.target.value)
      .then(() => api.cancelEditDescription?.())
      .catch((err) => api.toast(err.message));
  }
}

function wireAccountsTable(api, doc) {
  const table = api.$("accounts-table");
  if (!table) return;
  table.addEventListener("click", (event) =>
    handleAccountsTableClick(api, doc, event),
  );
  table.addEventListener("input", (event) =>
    handleAccountsTableInput(api, event),
  );
  table.addEventListener("keydown", (event) =>
    handleAccountsTableKeydown(api, event),
  );
}

export function bindDashboardEvents(e) {
  try {
    window.__cronusMarkDirty = e.markDirty;
  } catch (_) {}
  const doc = e.document;
  wireNavigation(e, doc);
  wireGlobalChange(e, doc);
  wireHeaderButtons(e);
  wireSettingsButtons(e);
  wireGameSettings(e);
  wireCronusToggles(e);
  wireGameMode(e, doc);
  wirePerformanceSettings(e);
  wireRamSettings(e);
  wireVirtualMemory(e, doc);
  wireGraphicsSettings(e);
  wireWindowSizeSettings(e);
  wireCpuLimiterSettings(e);
  wireQueueSettings(e, doc);
  wireGamePlace(e);
  wireAccountsTable(e, doc);
}
