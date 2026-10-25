import { api } from "../runtime/api.js?v=grid-5";
import { createStatusRuntime } from "../runtime/status.js?v=grid-6";
import {
  bucket,
  isAccountRecovery,
  isBanned,
  isBlocked,
  isCaptcha,
  isLocked,
  isSuspended,
  counts as countAccounts,
  filterAccounts,
  label as bucketLabel,
  rowStatusLabel,
} from "../runtime/accountStatus.js?v=js-verify2";
import { renderAccountRows } from "../components/accountsTable.js?v=js-verify3";
import { createFeedback } from "../components/feedback.js?v=grid-11";
import {
  applySolarStaticIcons,
  solarIcon,
} from "../components/icons.js?v=grid-6";
import { bindDashboardEvents } from "../events/bindings.js?v=grid-17";
import {
  renderSettingsPanel,
  renderPerformancePanel,
  renderRamPanel,
  renderGraphicsPanel,
  renderWindowSizePanel,
  renderCpuLimiterPanel,
  renderTroubleshootPanel,
  resetGamePanel,
  resetQueuePanel,
  resetPerformancePanel,
  resetRamPanel,
  resetGraphicsPanel,
  resetWindowSizePanel,
  resetCpuLimiterPanel,
  saveGamePanel,
  saveQueuePanel,
  savePerformancePanel,
  saveRamPanel,
  saveGraphicsPanel,
  saveWindowSizePanel,
  rearrangeWindowSizePanel,
  saveCpuLimiterPanel,
  setReveal,
} from "../panels/settingsPanels.js?v=grid-23";
import {
  renderRamCleanupPanel,
  saveRamCleanupPanel,
  resetRamCleanupPanel,
  renderVirtualMemoryPanel,
  saveVirtualMemoryStage,
  applyVirtualMemoryPanel,
  revertVirtualMemoryPanel,
  cleanNowRamCleanup,
  decorateNotices,
} from "../panels/ramExtra.js?v=ram-5";
const $ = (e) => document.getElementById(e);
let STATUS = {},
  ACCOUNTS = [],
  ACCOUNT_CONFIG = [],
  CONFIG = {},
  PERF = {},
  GRAPHICS = {},
  WINDOW_SIZE = {},
  CPU_LIMITER = {},
  RAM_CLEANUP = {},
  VIRTUAL_MEMORY = {},
  TROUBLESHOOT = {},
  DEVELOPER_LUA_SOURCE = "",
  DEVELOPER_LUA_ERROR = "",
  DEVELOPER_LUA_LOADING = !1,
  DEVELOPER_LUA_PROMISE = null,
  GAME_PLACE_CACHE = {},
  AVATARS = {},
  avatarLoadedKey = "",
  VIEW = (() => {
    try {
      const _v = localStorage.getItem("rg_view");
      return [
        "accounts",
        "game",
        "queue",
        "limiter",
        "graphics",
        "window-size",
        "troubleshoot",
        "developer",
        "cronus",
        "performance",
        "ram",
        "cpu-limiter",
      ].includes(_v)
        ? _v
        : "accounts";
    } catch (_e) {
      return "accounts";
    }
  })(),
  selectedUsers = new Set(
    (() => {
      try {
        const _s = JSON.parse(
          localStorage.getItem("rg_selected_multi") || "[]",
        );
        return Array.isArray(_s)
          ? _s.filter((_x) => typeof _x === "string")
          : [];
      } catch (_e) {
        return [];
      }
    })(),
  ),
  selectedUser = "",
  ACCOUNT_FILTER = localStorage.getItem("rg_account_filter") || "all",
  EDITING_DESC_USER = "",
  commandBusy = !1,
  configLoadedAt = 0,
  gameLookupTimer = 0,
  LAST_STATUS_REVISION = -1,
  LAST_STATUS_UPDATED_AT = 0,
  STATUS_REQUEST_SEQ = 0,
  STATUS_APPLIED_SEQ = 0,
  SETTINGS_DIRTY = {},
  RUNNING_CLOCK_ACTIVE = !1,
  RUNNING_CLOCK_BASE_SECONDS = 0,
  RUNNING_CLOCK_BASE_MS = Date.now();
const WINDOW_SIZE_PRESETS = {
    "200x150": [200, 150],
    "240x180": [240, 180],
    "320x240": [320, 240],
    "480x360": [480, 360],
    "640x480": [640, 480],
    "800x600": [800, 600],
    "1024x768": [1024, 768],
    "1280x720": [1280, 720],
    "1600x900": [1600, 900],
    "1920x1080": [1920, 1080],
  },
  ACCOUNT_FILTERS = ["all", "online", "queued"],
  CONDITIONAL_ACCOUNT_FILTERS = [
    "banned",
    "suspended",
    "invalid",
    "captcha",
    "locked",
    "recovery",
  ],
  LIMITER_VIEWS = ["performance", "cpu-limiter", "graphics"],
  RAM_VIEW_VIEWS = ["ram", "ram-cleanup", "virtual-memory"],
  esc = (e) =>
    String(e ?? "").replace(
      /[&<>"']/g,
      (e) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[e],
    ),
  {
    toast: toast,
    modal: modal,
    closeModal: closeModal,
    cardIcon: cardIcon,
  } = createFeedback({ $: $, esc: esc }),
  lower = (e) => String(e || "").toLowerCase();
function uptimeSeconds(e) {
  if ("number" === typeof e) return Math.max(0, Math.floor(e));
  const t = String(e || "").trim();
  if (!t) return 0;
  const a = t.split(":").map((e) => Number(e) || 0);
  return 3 === a.length
    ? 3600 * a[0] + 60 * a[1] + a[2]
    : 2 === a.length
      ? 60 * a[0] + a[1]
      : Number(t) || 0;
}
function shortDuration(e) {
  let t = Math.max(0, Math.floor(e || 0)),
    a = Math.floor(t / 3600);
  t %= 3600;
  const n = Math.floor(t / 60),
    r = t % 60;
  return a ? `${a}h ${n}m` : n ? `${n}m ${r}s` : `${r}s`;
}
function syncRunningClock() {
  ((RUNNING_CLOCK_ACTIVE = !!STATUS.running),
    (RUNNING_CLOCK_BASE_SECONDS = RUNNING_CLOCK_ACTIVE
      ? uptimeSeconds(STATUS.uptime)
      : 0),
    (RUNNING_CLOCK_BASE_MS = Date.now()),
    renderRunningClock());
}
function renderRunningClock() {
  const e = $("h-running-time");
  if (!e) return;
  const t = RUNNING_CLOCK_ACTIVE
    ? RUNNING_CLOCK_BASE_SECONDS +
      Math.floor((Date.now() - RUNNING_CLOCK_BASE_MS) / 1e3)
    : 0;
  ((e.textContent = `Running: ${shortDuration(t)}`), (e.title = e.textContent));
}
function statusRevisionRegressed(revision) {
  return revision >= 0 && LAST_STATUS_REVISION > revision;
}
function statusTimestampNewer(updatedAt) {
  return !!updatedAt && updatedAt > LAST_STATUS_UPDATED_AT;
}
function statusSequenceBehind(seq) {
  return !!seq && seq < STATUS_APPLIED_SEQ;
}
function statusMetaSuperseded(revision, updatedAt) {
  return (
    statusRevisionRegressed(revision) && !statusTimestampNewer(updatedAt)
  );
}
function statusMeta(e) {
  return {
    revision: Number(e?.status_revision ?? -1),
    updatedAt: Number(e?.status_updated_at ?? 0),
  };
}
function rememberStatusMeta(revision, updatedAt, seq) {
  seq && (STATUS_APPLIED_SEQ = seq);
  revision >= 0 && (LAST_STATUS_REVISION = revision);
  updatedAt && (LAST_STATUS_UPDATED_AT = updatedAt);
}
function acceptStatus(e, t = 0) {
  const { revision: a, updatedAt: n } = statusMeta(e);
  if (statusSequenceBehind(t)) return !1;
  if (statusRevisionRegressed(a) && statusTimestampNewer(n)) {
    LAST_STATUS_REVISION = -1;
  } else if (statusMetaSuperseded(a, n)) {
    return !1;
  }
  rememberStatusMeta(a, n, t);
  STATUS = e || {};
  return !0;
}
function dirty(e) {
  return !!SETTINGS_DIRTY[e];
}
function valueOf(e) {
  return String($(e)?.value ?? "").trim();
}
function checked(e) {
  return !!$(e)?.checked;
}
function gameModeValue() {
  const t = $("game-mode-toggle");
  return (
    t?.querySelector("button.active")?.dataset.mode ||
    t?.dataset.value ||
    "shared"
  );
}
function savedNum(e, t = 0) {
  const a = Number(e);
  return Number.isFinite(a) ? a : t;
}
function saveNumValue(e, t = 0) {
  const a = Number($(e)?.value);
  return Number.isFinite(a) ? a : t;
}
function sameNum(e, t) {
  return Number(e) === Number(t);
}
function normalizeRamLimitValue(e) {
  const t = Number(e);
  return Number.isFinite(t)
    ? Math.max(512, Math.min(Math.round(t), 65536))
    : 8192;
}
function currentRamLimit() {
  const e = valueOf("ram-limit-preset") || "8192";
  return normalizeRamLimitValue(
    "custom" === e ? valueOf("ram-limit-custom") || 8192 : e,
  );
}
function cpuRowsFromDom() {
  return Array.from(document.querySelectorAll("#cpu-account-rows tr"))
    .map((e) => {
      const t = e.querySelector(".cpu-row-enabled"),
        a = e.querySelector(".cpu-row-limit"),
        n = t?.dataset.user || a?.dataset.user || "",
        r = Number(a?.value);
      return n
        ? {
            username: n.toLowerCase(),
            enabled: !!t?.checked,
            limit_percent: r || 20,
          }
        : null;
    })
    .filter(Boolean)
    .sort((e, t) => e.username.localeCompare(t.username));
}
function cpuRowsFromState() {
  return (Array.isArray(CPU_LIMITER.rows) ? CPU_LIMITER.rows : [])
    .map((e) => ({
      username: String(e.username || "").toLowerCase(),
      enabled: !!e.enabled,
      limit_percent: savedNum(
        e.limit_percent ?? CPU_LIMITER.default_limit_percent,
        20,
      ),
    }))
    .filter((e) => e.username)
    .sort((e, t) => e.username.localeCompare(t.username));
}
function sameCpuRows(e, t) {
  return (
    e.length === t.length &&
    e.every(
      (e, a) =>
        e.username === t[a].username &&
        e.enabled === t[a].enabled &&
        sameNum(e.limit_percent, t[a].limit_percent),
    )
  );
}
function gameSettingsChanged() {
  return (
    valueOf("game-private") !== String(CONFIG.game_private_server_url || "") ||
    valueOf("game-place") !== String(CONFIG.game_place_id || "") ||
    gameModeValue() !== savedGameMode() ||
    gameFlagSettingsChanged()
  );
}
function savedGameMode() {
  return "per_account" === String(CONFIG.game_mode || "")
    ? "per_account"
    : "shared";
}
function gameFlagSettingsChanged() {
  return (
    checked("game-auto-private-enabled") !==
      !!CONFIG.auto_create_private_server_enabled ||
    checked("game-multi-roblox") !== (!1 !== CONFIG.multi_roblox_enabled) ||
    (!!$("game-block-same-server") &&
      checked("game-block-same-server") !==
        !!CONFIG.block_same_server_enabled)
  );
}
function queueLimitsChanged() {
  return (
    !sameNum(
      saveNumValue("queue-max", 1),
      savedNum(CONFIG.max_concurrent_accounts ?? 40, 40),
    ) ||
    !sameNum(
      saveNumValue("queue-delay", 0),
      savedNum(
        CONFIG.queue_delay_seconds ?? CONFIG.launch_rate_interval ?? 15,
        15,
      ),
    ) ||
    checked("queue-autoclose-enabled") !== !!CONFIG.auto_close_enabled ||
    !sameNum(
      saveNumValue("queue-autoclose-minutes", 0),
      savedNum(CONFIG.auto_close_minutes ?? 0, 0),
    )
  );
}
function queuePopupChanged() {
  return (
    checked("popup-disconnected-enabled") !==
      (!1 !== CONFIG.popup_disconnected_enabled) ||
    !sameNum(
      saveNumValue("popup-scan-interval", 30),
      savedNum(CONFIG.popup_scan_interval_seconds ?? 30, 30),
    ) ||
    !sameNum(
      saveNumValue("popup-scan-max-parallel", 2),
      savedNum(CONFIG.popup_scan_max_parallel ?? 2, 2),
    )
  );
}
function queueSettingsChanged() {
  return queueLimitsChanged() || queuePopupChanged();
}
function graphicsToggleChanged() {
  const enabled = !!(
    GRAPHICS.graphics_low_enabled ?? GRAPHICS.graphics_auto_enabled
  );
  return (
    checked("graphics-auto-enabled") !== enabled ||
    checked("priority-enabled") !== !!GRAPHICS.auto_process_priority_enabled ||
    checked("volume-muted") !== !!GRAPHICS.roblox_volume_muted
  );
}
function graphicsValueChanged() {
  return (
    !sameNum(
      saveNumValue("graphics-quality", 1),
      savedNum(
        GRAPHICS.graphics_quality_level ??
          GRAPHICS.graphics_quality_level_current ??
          1,
        1,
      ),
    ) ||
    valueOf("process-priority") !==
      String(GRAPHICS.process_priority || "low") ||
    !sameNum(
      saveNumValue("volume-level", 10),
      savedNum(
        GRAPHICS.roblox_volume_level ?? GRAPHICS.volume_level_current ?? 10,
        10,
      ),
    )
  );
}
function graphicsSettingsChanged() {
  return graphicsToggleChanged() || graphicsValueChanged();
}
function windowToggleSettingsChanged() {
  return (
    checked("window-unlock-enabled") !==
      (!1 !== WINDOW_SIZE.unlock_size_enabled) ||
    checked("window-arrange-enabled") !== !!WINDOW_SIZE.arrange_enabled ||
    checked("window-size-enabled") !== !!WINDOW_SIZE.enabled ||
    checked("window-auto-minimize-enabled") !==
      !!WINDOW_SIZE.auto_minimize_enabled ||
    checked("window-hide-enabled") !== !!WINDOW_SIZE.hide_enabled
  );
}
function windowSizeValueSettingsChanged() {
  return (
    valueOf("window-size-preset") !==
      String(WINDOW_SIZE.preset || "200x150") ||
    !sameNum(
      saveNumValue("window-size-width", 200),
      savedNum(WINDOW_SIZE.width ?? 200, 200),
    ) ||
    !sameNum(
      saveNumValue("window-size-height", 150),
      savedNum(WINDOW_SIZE.height ?? 150, 150),
    )
  );
}
function windowGridSettingsChanged() {
  return (
    !sameNum(
      Number(valueOf("window-grid-cols")) || 6,
      savedNum(WINDOW_SIZE.arrange_columns ?? 6, 6),
    ) ||
    !sameNum(
      Number(valueOf("window-grid-rows")) || 4,
      savedNum(WINDOW_SIZE.arrange_rows ?? 4, 4),
    ) ||
    !sameNum(
      Number(valueOf("window-auto-minimize-seconds")) || 10,
      savedNum(WINDOW_SIZE.auto_minimize_seconds ?? 10, 10),
    )
  );
}
function windowSizeSettingsChanged() {
  return (
    windowToggleSettingsChanged() ||
    windowSizeValueSettingsChanged() ||
    windowGridSettingsChanged()
  );
}
function cronusFieldsAvailable() {
  return !!(
    $("cronus-start-on-boot") &&
    $("cronus-start-farming-on-boot") &&
    $("cronus-auto-update-on-boot")
  );
}
function cronusSettingsChanged() {
  if (!cronusFieldsAvailable()) return dirty("cronus");
  return (
    checked("cronus-start-on-boot") !== !!CONFIG.start_on_boot ||
    checked("cronus-start-farming-on-boot") !==
      !!CONFIG.start_farming_on_boot ||
    checked("cronus-auto-update-on-boot") !== !!CONFIG.auto_update_on_boot ||
    checked("cronus-privacy-blur") !== !!CONFIG.privacy_blur_usernames ||
    checked("cronus-privacy-random") !== !!CONFIG.privacy_randomize_usernames ||
    checked("cronus-discord-enabled") !== !!CONFIG.discord_presence_enabled ||
    checked("cronus-discord-show-accounts") !==
      (CONFIG.discord_show_accounts !== !1) ||
    checked("cronus-discord-show-game") !== (CONFIG.discord_show_game !== !1)
  );
}
function performanceSettingsChanged() {
  return (
    checked("fps-enabled") !== !!PERF.enabled ||
    !sameNum(
      valueOf("fps-limit") === "" ? 240 : Number(valueOf("fps-limit")),
      savedNum(PERF.fps_limit ?? PERF.framerate_cap ?? 240, 240),
    )
  );
}
function ramSettingsChanged() {
  return (
    checked("ram-enabled") !== (!1 !== CONFIG.roblox_memory_guard_enabled) ||
    !sameNum(
      currentRamLimit(),
      normalizeRamLimitValue(CONFIG.roblox_memory_guard_mb ?? 8192),
    )
  );
}
function ramCleanupSettingsChanged() {
  return (
    checked("ram-cleanup-enabled") !==
      ("enabled" in RAM_CLEANUP
        ? !!RAM_CLEANUP.enabled
        : !!CONFIG.ram_cleanup_enabled) ||
    !sameNum(
      saveNumValue("ram-cleanup-threshold", 85),
      savedNum(
        RAM_CLEANUP.threshold_pct ?? CONFIG.ram_cleanup_threshold_pct ?? 85,
        85,
      ),
    ) ||
    !sameNum(
      saveNumValue("ram-cleanup-interval", 15),
      savedNum(
        RAM_CLEANUP.interval_min ?? CONFIG.ram_cleanup_interval_min ?? 15,
        15,
      ),
    )
  );
}
function virtualMemorySettingsChanged() {
  return !sameNum(
    saveNumValue("vm-size", 16),
    savedNum(
      VIRTUAL_MEMORY.configured_size_gb ?? CONFIG.virtual_memory_size_gb ?? 16,
      16,
    ),
  );
}
function cpuLimiterSettingsChanged() {
  return (
    checked("cpu-enabled") !== !!CPU_LIMITER.enabled ||
    valueOf("cpu-mode") !== String(CPU_LIMITER.mode || "hard") ||
    !sameNum(
      saveNumValue("cpu-default-limit", 20),
      savedNum(CPU_LIMITER.default_limit_percent ?? 20, 20),
    ) ||
    checked("cpu-apply-all") !== (!1 !== CPU_LIMITER.apply_all) ||
    (!checked("cpu-apply-all") &&
      !sameCpuRows(cpuRowsFromDom(), cpuRowsFromState()))
  );
}
function developerSettingsChanged() {
  return !$("dev-lua-enabled") || !$("dev-lua-timeout")
    ? dirty("developer")
    : checked("dev-lua-enabled") !== (!1 !== CONFIG.lua_enabled) ||
        !sameNum(
          Number(valueOf("dev-lua-timeout")) || 60,
          savedNum(CONFIG.lua_timeout_seconds ?? 60, 60),
        );
}
const SETTINGS_CHECKS = {
  game: gameSettingsChanged,
  queue: queueSettingsChanged,
  performance: performanceSettingsChanged,
  ram: ramSettingsChanged,
  "ram-cleanup": ramCleanupSettingsChanged,
  "virtual-memory": virtualMemorySettingsChanged,
  graphics: graphicsSettingsChanged,
  "window-size": windowSizeSettingsChanged,
  "cpu-limiter": cpuLimiterSettingsChanged,
  cronus: cronusSettingsChanged,
  developer: developerSettingsChanged,
};
function settingsChanged(section) {
  try {
    const check = SETTINGS_CHECKS[section];
    return check ? check() : dirty(section);
  } catch (_) {
    return dirty(section);
  }
}
function syncToggleLabels() {
  document
    .querySelectorAll('.toggle-row input[type="checkbox"]')
    .forEach((e) => {
      const t = e.parentElement?.querySelector("span");
      t &&
        ((t.textContent = e.checked ? "Enabled" : "Disabled"),
        (t.hidden = !1),
        t.classList.toggle("enabled", e.checked),
        t.classList.toggle("disabled", !e.checked));
    });
}
function updateLimiterSaveState() {
  const e = LIMITER_VIEWS.some(dirty),
    t = $("limiter-save"),
    a = $("limiter-reset");
  (t && (t.classList.toggle("settings-dirty", e), (t.hidden = !e)),
    a && (a.hidden = !e));
}
function updateRamViewSaveState() {
  const e = RAM_VIEW_VIEWS.some(dirty),
    t = $("ram-save"),
    a = $("ram-reset");
  (t && (t.classList.toggle("settings-dirty", e), (t.hidden = !e)),
    a && (a.hidden = !e));
}
function updateSaveState(e) {
  const t = dirty(e),
    a = $(
      {
        game: "game-save",
        queue: "queue-save",
        graphics: "graphics-save",
        "window-size": "window-size-save",
        cronus: "cronus-save",
        developer: "developer-save",
      }[e],
    ),
    n = $(
      {
        game: "game-reset",
        queue: "queue-reset",
        graphics: "graphics-reset",
        "window-size": "window-size-reset",
        cronus: "cronus-reset",
        developer: "developer-reset",
      }[e],
    );
  if ("graphics" === e) {
    (a && a.classList.toggle("settings-dirty", t),
      a && (a.hidden = !0),
      n && (n.hidden = !0));
  } else {
    (a && (a.classList.toggle("settings-dirty", t), (a.hidden = !t)),
      n && (n.hidden = !t));
  }
  (LIMITER_VIEWS.includes(e) && updateLimiterSaveState(),
    RAM_VIEW_VIEWS.includes(e) && updateRamViewSaveState(),
    syncToggleLabels());
}
function markDirty(e) {
  (settingsChanged(e) ? (SETTINGS_DIRTY[e] = !0) : delete SETTINGS_DIRTY[e],
    updateSaveState(e));
}
function clearDirty(e) {
  (delete SETTINGS_DIRTY[e], updateSaveState(e));
}
function unsavedSettings() {
  return Object.keys(SETTINGS_DIRTY);
}
function unsavedLabel(k) {
  return (
    {
      cronus: "Cronus Manager",
      developer: "Lua Manager",
      game: "Game",
      queue: "Queue",
      "window-size": "Windows",
      performance: "Limiter",
      ram: "RAM",
      "ram-cleanup": "RAM",
      "virtual-memory": "RAM",
      graphics: "Limiter",
      "cpu-limiter": "Limiter",
    }[k] || k
  );
}
function unsavedView(k) {
  return (
    {
      cronus: "cronus",
      developer: "developer",
      game: "game",
      queue: "queue",
      "window-size": "window-size",
      performance: "limiter",
      ram: "ram",
      "ram-cleanup": "ram",
      "virtual-memory": "ram",
      graphics: "limiter",
      "cpu-limiter": "limiter",
    }[k] || "accounts"
  );
}
function requireSavedSettings() {
  const u = unsavedSettings();
  if (!u.length) return !0;
  const names = u.map(unsavedLabel);
  const first = unsavedView(u[0]);
  const msg = `Unsaved changes in ${names.join(", ")} Ã¢â‚¬â€ click to review`;
  try {
    toast(msg, "warning", {
      onClick: () => {
        try {
          setView(first);
        } catch (_) {}
      },
      title: "Click to open " + names[0],
      duration: 6500,
    });
  } catch (_) {
    toast(msg);
  }
  return !1;
}
function initials(e) {
  return (
    String(e || "?")
      .split(/[^a-zA-Z0-9]+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((e) => e[0])
      .join("")
      .toUpperCase() || "?"
  );
}
function cfgFor(e) {
  const t = lower(e);
  return ACCOUNT_CONFIG.find((e) => lower(e.username) === t) || {};
}
function counts() {
  return countAccounts(ACCOUNTS);
}
function filtered() {
  const e = filterAccounts(
      ACCOUNTS,
      $("account-search")?.value || "",
      cfgFor,
    ).map((e, t) => ({ a: e, i: t })),
    t =
      "all" === ACCOUNT_FILTER
        ? e
        : e.filter((e) => bucket(e.a) === ACCOUNT_FILTER);
  return (
    "queued" === ACCOUNT_FILTER &&
      t.sort((e, t) => {
        const a = Number(e.a.queue_position || 0) || 999999,
          n = Number(t.a.queue_position || 0) || 999999;
        return a !== n ? a - n : e.i - t.i;
      }),
    t.map((e) => e.a)
  );
}
async function loadAccountsConfig(e = !1) {
  if (!e && Date.now() - configLoadedAt < 1500 && ACCOUNT_CONFIG.length)
    return ACCOUNT_CONFIG;
  const t = await api("/accounts");
  return (
    Array.isArray(t) && (ACCOUNT_CONFIG = t),
    (configLoadedAt = Date.now()),
    ACCOUNT_CONFIG
  );
}
function accountUserId(e) {
  const t = cfgFor(e.username);
  return String(
    e.user_id || e.cookie_user_id || t.user_id || t.cookie_user_id || "",
  ).trim();
}
function avatarHtml(e, t) {
  const a = accountUserId(e),
    n = a ? AVATARS[a] : "",
    r = esc(initials(t));
  return n
    ? `<span class="avatar-wrap"><img class="avatar-img" src="${esc(n)}" alt="" loading="lazy" onerror="this.hidden=true;this.nextElementSibling.hidden=false"><span class="avatar" hidden>${r}</span></span>`
    : `<span class="avatar-wrap"><span class="avatar">${r}</span></span>`;
}
async function loadAvatars() {
  const e = [...new Set(ACCOUNTS.map(accountUserId).filter(Boolean))].sort(),
    t = e.join(",");
  if (!e.length || t === avatarLoadedKey) return;
  avatarLoadedKey = t;
  const a = await api("/accounts/avatars?user_ids=" + encodeURIComponent(t));
  (a && a.avatars && (AVATARS = { ...AVATARS, ...a.avatars }),
    renderAccountsTable());
}
const CRONUS_BOOT_TOGGLES = [
  ["cronus-start-on-boot", "start_on_boot"],
  ["cronus-start-farming-on-boot", "start_farming_on_boot"],
  ["cronus-auto-update-on-boot", "auto_update_on_boot"],
  ["cronus-privacy-blur", "privacy_blur_usernames"],
  ["cronus-privacy-random", "privacy_randomize_usernames"],
];
function applyCronusBootToggles() {
  try {
    for (const [id, key] of CRONUS_BOOT_TOGGLES) {
      const el = $(id);
      if (el) el.checked = !!CONFIG[key];
    }
  } catch {}
}
function loadConfiguredGamePlace() {
  const place =
    CONFIG.game_place_id ||
    placeFromPrivate(CONFIG.game_private_server_url || "");
  if (place)
    loadGamePlace(place, !0).catch(() => {
      /* intentional: no-op */
    });
}
async function loadConfig() {
  CONFIG = await api("/config");
  applyCronusBootToggles();
  try {
    pullColWidthsFromConfig();
  } catch {}
  try {
    applyColWidths();
  } catch {}
  (renderSettings(),
    renderRam(),
    renderRamCleanup(),
    renderVirtualMemory(),
    renderWindowSize());
  loadConfiguredGamePlace();
  return CONFIG;
}
async function loadPerformance() {
  try {
    return (
      (PERF = await api("/performance/fps-limiter")),
      renderPerformance(),
      PERF
    );
  } catch (e) {
    return ((PERF = { ok: !1, msg: e.message }), renderPerformance(), PERF);
  }
}
async function loadGraphics() {
  try {
    return (
      (GRAPHICS = await api("/performance/graphics")),
      renderGraphics(),
      GRAPHICS
    );
  } catch (e) {
    return (
      (GRAPHICS = { ok: !1, msg: e.message }),
      renderGraphics(),
      GRAPHICS
    );
  }
}
async function loadWindowSize() {
  try {
    return (
      (WINDOW_SIZE = await api("/performance/window-size")),
      renderWindowSize(),
      WINDOW_SIZE
    );
  } catch (e) {
    return (
      (WINDOW_SIZE = { ok: !1, msg: e.message }),
      renderWindowSize(),
      WINDOW_SIZE
    );
  }
}
async function loadCpuLimiter() {
  try {
    return (
      (CPU_LIMITER = await api("/performance/cpu-limiter")),
      renderCpuLimiter(),
      CPU_LIMITER
    );
  } catch (e) {
    return (
      (CPU_LIMITER = { ok: !1, msg: e.message, rows: [] }),
      renderCpuLimiter(),
      CPU_LIMITER
    );
  }
}
async function loadRamCleanup() {
  try {
    return (
      (RAM_CLEANUP = await api("/performance/ram-cleanup")),
      renderRamCleanup(),
      RAM_CLEANUP
    );
  } catch (e) {
    return (
      (RAM_CLEANUP = { ok: !1, msg: e.message }),
      renderRamCleanup(),
      RAM_CLEANUP
    );
  }
}
async function loadVirtualMemory() {
  try {
    return (
      (VIRTUAL_MEMORY = await api("/performance/virtual-memory")),
      renderVirtualMemory(),
      VIRTUAL_MEMORY
    );
  } catch (e) {
    return (
      (VIRTUAL_MEMORY = { ok: !1, msg: e.message }),
      renderVirtualMemory(),
      VIRTUAL_MEMORY
    );
  }
}
async function loadTroubleshoot() {
  try {
    return (
      (TROUBLESHOOT = await api("/troubleshoot/roblox-install")),
      renderTroubleshoot(),
      TROUBLESHOOT
    );
  } catch (e) {
    return (
      (TROUBLESHOOT = {
        ok: !1,
        msg: e.message,
        job: { state: "Failed", progress: e.message },
      }),
      renderTroubleshoot(),
      TROUBLESHOOT
    );
  }
}
async function loadDeveloperLua(e = !1) {
  return (
    e && ((DEVELOPER_LUA_SOURCE = ""), (DEVELOPER_LUA_ERROR = "")),
    DEVELOPER_LUA_SOURCE && !e
      ? DEVELOPER_LUA_SOURCE
      : ((DEVELOPER_LUA_PROMISE && !e) ||
          ((DEVELOPER_LUA_LOADING = !0),
          (DEVELOPER_LUA_ERROR = ""),
          renderDeveloper(),
          (DEVELOPER_LUA_PROMISE = (async () => {
            try {
              const e = await api("/lua/executor-loader?ts=" + Date.now());
              return (
                (DEVELOPER_LUA_SOURCE = String(e.source || "")),
                DEVELOPER_LUA_SOURCE ||
                  (DEVELOPER_LUA_ERROR = "Lua source is empty"),
                DEVELOPER_LUA_SOURCE
              );
            } catch (e) {
              return (
                (DEVELOPER_LUA_ERROR = e.message || "Lua source unavailable"),
                ""
              );
            } finally {
              ((DEVELOPER_LUA_LOADING = !1),
                (DEVELOPER_LUA_PROMISE = null),
                renderDeveloper());
            }
          })())),
        DEVELOPER_LUA_PROMISE)
  );
}
function resetGameSettings() {
  return resetGamePanel(panelCtx());
}
function resetQueueSettings() {
  return resetQueuePanel(panelCtx());
}
function resetPerformanceSettings() {
  return resetPerformancePanel(panelCtx());
}
function resetRamSettings() {
  return resetRamPanel(panelCtx());
}
function resetRamCleanupSettings() {
  return resetRamCleanupPanel(panelCtx());
}
function resetVirtualMemorySettings() {
  clearDirty("virtual-memory");
  try {
    const n = document.getElementById("vm-notice");
    n && delete n.dataset.sticky;
  } catch (_) {}
  return renderVirtualMemory();
}
async function saveRamCleanup() {
  RAM_CLEANUP = await saveRamCleanupPanel(panelCtx());
}
async function applyVirtualMemory() {
  VIRTUAL_MEMORY = await applyVirtualMemoryPanel(panelCtx());
}
async function revertVirtualMemory() {
  VIRTUAL_MEMORY = await revertVirtualMemoryPanel(panelCtx());
}
async function cleanNowRam() {
  await cleanNowRamCleanup(panelCtx());
}
function resetGraphicsSettings() {
  return resetGraphicsPanel(panelCtx());
}
function resetWindowSizeSettings() {
  return resetWindowSizePanel(panelCtx());
}
function resetCronusSettings() {
  clearDirty("cronus");
  try {
    const c = CONFIG;
    $("cronus-start-on-boot").checked = !!c.start_on_boot;
    $("cronus-start-farming-on-boot").checked = !!c.start_farming_on_boot;
    $("cronus-auto-update-on-boot").checked = !!c.auto_update_on_boot;
    if ($("cronus-privacy-blur"))
      $("cronus-privacy-blur").checked = !!c.privacy_blur_usernames;
    if ($("cronus-privacy-random"))
      $("cronus-privacy-random").checked = !!c.privacy_randomize_usernames;
    if ($("cronus-discord-enabled"))
      $("cronus-discord-enabled").checked = !!c.discord_presence_enabled;
    if ($("cronus-discord-show-accounts"))
      $("cronus-discord-show-accounts").checked =
        c.discord_show_accounts !== !1;
    if ($("cronus-discord-show-game"))
      $("cronus-discord-show-game").checked = c.discord_show_game !== !1;
    renderAccountsTable();
  } catch (_) {}
  updateSaveState("cronus");
  syncToggleLabels();
}
async function saveCronusSettings() {
  const p = {
    start_on_boot: checked("cronus-start-on-boot"),
    start_farming_on_boot: checked("cronus-start-farming-on-boot"),
    auto_update_on_boot: checked("cronus-auto-update-on-boot"),
    privacy_blur_usernames: checked("cronus-privacy-blur"),
    privacy_randomize_usernames: checked("cronus-privacy-random"),
    discord_presence_enabled: checked("cronus-discord-enabled"),
    discord_show_accounts: checked("cronus-discord-show-accounts"),
    discord_show_game: checked("cronus-discord-show-game"),
  };
  try {
    await api("/config", "POST", p);
    try {
      await api("/startup/apply", "POST", { enabled: !!p.start_on_boot });
    } catch (_) {}
    CONFIG = { ...CONFIG, ...p };
    clearDirty("cronus");
    toast("Cronus Manager saved");
    try {
      renderAccountsTable();
    } catch (_) {}
    await manualSnapshot();
  } catch (e) {
    toast(e.message || "Cronus save failed");
  }
}
function resetDeveloperSettings() {
  clearDirty("developer");
  try {
    $("dev-lua-enabled").checked = !1 !== CONFIG.lua_enabled;
    $("dev-lua-timeout").value = String(CONFIG.lua_timeout_seconds ?? 60);
  } catch (_) {}
  updateSaveState("developer");
  renderDeveloper();
  try {
    document.dispatchEvent(
      new CustomEvent("cronus:lua-enabled", {
        detail: { enabled: !!CONFIG.lua_enabled },
      }),
    );
  } catch (_) {}
}
async function saveDeveloperSettings() {
  const g = $("dev-lua-enabled"),
    h = $("dev-lua-timeout");
  if (!g || !h) return;
  const t = {
    lua_enabled: g.checked,
    lua_timeout_seconds: Number(h.value) || 60,
  };
  try {
    (await api("/config", "POST", t),
      (CONFIG = {
        ...CONFIG,
        lua_enabled: t.lua_enabled,
        lua_timeout_seconds: t.lua_timeout_seconds,
      }));
    clearDirty("developer");
    toast("Lua Manager saved");
    try {
      document.dispatchEvent(
        new CustomEvent("cronus:lua-enabled", {
          detail: { enabled: t.lua_enabled },
        }),
      );
    } catch (_) {}
    await manualSnapshot();
  } catch (a) {
    toast(a.message || "Lua save failed");
  }
}
function resetCpuLimiterSettings() {
  return resetCpuLimiterPanel(panelCtx());
}
function resetLimiterSettings() {
  (resetPerformanceSettings(),
    resetCpuLimiterSettings(),
    updateLimiterSaveState());
}
function resetRamViewSettings() {
  (resetRamSettings(),
    resetRamCleanupSettings(),
    resetVirtualMemorySettings(),
    updateRamViewSaveState());
}
async function saveLimiter() {
  (dirty("performance") && (PERF = await savePerformancePanel(panelCtx())),
    dirty("cpu-limiter") &&
      (CPU_LIMITER = await saveCpuLimiterPanel(panelCtx())),
    updateLimiterSaveState());
}
async function saveRamView() {
  (dirty("ram") && (CONFIG = await saveRamPanel(panelCtx())),
    dirty("ram-cleanup") &&
      (RAM_CLEANUP = await saveRamCleanupPanel(panelCtx())),
    dirty("virtual-memory") &&
      (VIRTUAL_MEMORY = await saveVirtualMemoryStage(panelCtx())),
    updateRamViewSaveState());
}
function robloxInstallConfirm(e) {
  const t = {
    uninstall: [
      "Uninstall Roblox",
      "Removes all Roblox files from this PC",
      "Uninstall",
    ],
    latest: [
      "Download latest",
      "Wipes Roblox, then installs latest LIVE player",
      "Download",
    ],
  }[e];
  if (!t) return toast("Unknown action");
  (modal(
    t[0],
    `<div class="choice-grid" style="grid-template-columns:1fr"><div class="choice" style="cursor:default">${cardIcon("install")}<strong>${t[0]}</strong><span class="hint">${t[1]}</span></div></div>`,
    `<button class="btn ghost" id="install-back">Back</button><button class="btn ${"uninstall" === e ? "danger" : "good"}" id="install-confirm">${t[2]}</button>`,
  ),
    ($("install-back").onclick = closeModal),
    ($("install-confirm").onclick = () => runRobloxInstall(e)));
}
async function runRobloxInstall(e) {
  try {
    const t =
        "uninstall" === e
          ? "/troubleshoot/roblox-install/uninstall"
          : "/troubleshoot/roblox-install/latest",
      a = await api(t, "POST", {});
    if ((closeModal(), !a.ok)) return toast(a.msg || "Install blocked");
    (toast(a.msg || "Job started"), await loadTroubleshoot());
  } catch (e) {
    toast(e.message);
  }
}
function activateView(e) {
  const t = $("view-" + e);
  t &&
    document
      .querySelectorAll(".view")
      .forEach((e) => e.classList.toggle("active", e === t));
}
function setView(e) {
  const t =
    { performance: "limiter", "cpu-limiter": "limiter", graphics: "limiter" }[
      e
    ] || e;
  ((VIEW = {
    accounts: 1,
    game: 1,
    queue: 1,
    limiter: 1,
    ram: 1,
    graphics: 1,
    "window-size": 1,
    troubleshoot: 1,
    developer: 1,
    cronus: 1,
  }[t]
    ? t
    : "accounts"),
    window.__cronusStore("rg_view", VIEW),
    document
      .querySelectorAll("#nav button")
      .forEach((e) => e.classList.toggle("active", e.dataset.view === VIEW)),
    "troubleshoot" === VIEW && loadTroubleshoot(),
    "developer" === VIEW && loadDeveloperLua(),
    "limiter" === VIEW &&
      (loadPerformance(), loadConfig(), loadCpuLimiter(), loadGraphics()),
    "ram" === VIEW && (loadConfig(), loadRamCleanup(), loadVirtualMemory()),
    "window-size" === VIEW && loadWindowSize(),
    render(),
    activateView(VIEW));
}
function render() {
  ((ACCOUNTS = Array.isArray(STATUS.accounts) ? STATUS.accounts : []),
    !selectedUser && ACCOUNTS[0] && (selectedUser = ACCOUNTS[0].username),
    renderTop(),
    renderAccountsTable(),
    renderSettings(),
    renderPerformance(),
    renderRam(),
    renderRamCleanup(),
    renderVirtualMemory(),
    renderGraphics(),
    renderWindowSize(),
    renderCpuLimiter(),
    renderTroubleshoot(),
    renderDeveloper(),
    syncToggleLabels(),
    decorateNotices(),
    Object.keys(SETTINGS_DIRTY).forEach(updateSaveState));
}
function renderTop() {
  const e = counts(),
    t = !!STATUS.running;
  (($("nav-accounts-count").textContent = e.all),
    ($("h-online").textContent = e.online),
    ($("h-queued").textContent = e.queued),
    ($("h-finished").textContent = e.finished),
    ($("h-captcha").textContent = e.captcha),
    ($("h-banned").textContent = e.banned),
    ($("h-suspended").textContent = e.suspended),
    ($("h-locked").textContent = e.locked),
    ($("h-invalid").textContent = e.invalid),
    renderAccountFilters(e),
    syncRunningClock(),
    ($("guard-btn").innerHTML = t
      ? `${solarIcon("stopCircle", "guard-icon stop")}<span>STOP</span>`
      : `${solarIcon("play", "guard-icon start")}<span>START</span>`),
    ($("guard-btn").className = t
      ? "btn danger guard-action"
      : "btn good guard-action"),
    ($("guard-btn").disabled =
      commandBusy || (!STATUS.can_start && !STATUS.can_stop)));
}
function visibleAccountFilters(e = counts()) {
  return [
    ...ACCOUNT_FILTERS,
    ...CONDITIONAL_ACCOUNT_FILTERS.filter((t) => (e[t] || 0) > 0),
  ];
}
function resetColWidths() {
  _colWidths = {};
  try {
    localStorage.setItem(_colWidthsKey, "{}");
  } catch (e) {}
  try {
    api("/config", "POST", { ui_col_widths: {} }).catch(() => {
      /* intentional: no-op */
    });
  } catch (e) {}
  const table = document.querySelector(".accounts-table-card table");
  if (table) {
    table.style.tableLayout = "";
    table.querySelectorAll("thead th").forEach((th) => {
      th.style.width = "";
    });
  }
  renderAccountFilters();
}
function renderAccountFilters(e = counts()) {
  const t = $("account-filters");
  if (!t) return;
  const a = visibleAccountFilters(e);
  (a.includes(ACCOUNT_FILTER) || (ACCOUNT_FILTER = "all"),
    (t.innerHTML =
      a
        .map(
          (t) =>
            `<button class="account-filter ${ACCOUNT_FILTER === t ? "active" : ""}" data-account-filter="${esc(t)}"><span>${esc(bucketLabel(t))}</span><strong>${esc(e[t] ?? 0)}</strong></button>`,
        )
        .join("") +
      (Object.keys(_colWidths).length
        ? `<button class="account-filter col-reset" data-col-reset="1" title="Reset column widths"><span>Reset columns</span></button>`
        : "")));
  const rb = t.querySelector("[data-col-reset]");
  if (rb) rb.onclick = resetColWidths;
}
function setAccountFilter(e) {
  const t = visibleAccountFilters();
  ((ACCOUNT_FILTER = t.includes(e) ? e : "all"),
    window.__cronusStore("rg_account_filter", ACCOUNT_FILTER),
    renderAccountFilters(),
    renderAccountsTable());
}
function accountTableTextSelectionActive() {
  const e = $("accounts-table"),
    t = document.getSelection?.();
  return !!(
    e &&
    t &&
    !t.isCollapsed &&
    String(t).trim() &&
    e.contains(t.anchorNode)
  );
}
function accountSharedPlaceId() {
  return String(
    CONFIG.game_place_id ||
      placeFromPrivate(CONFIG.game_private_server_url || "") ||
      "",
  ).trim();
}
function accountConfiguredPlaceId(acc, cfg) {
  const fromCfg = cfg ? cfg.effective_place_id || cfg.place_id : "";
  const fromAcc = acc ? acc.place_id : "";
  return String(fromCfg || fromAcc || "").trim();
}
function observedPlaceId(acc) {
  return String((acc && acc.observed_place_id) || "").trim();
}
function gameModePerAccount() {
  return (
    "per_account" ===
    String(CONFIG.game_mode || "")
      .trim()
      .toLowerCase()
      .replace("-", "_")
  );
}
function accountGamePlaceId(acc, cfg) {
  const shared = accountSharedPlaceId();
  const configured = accountConfiguredPlaceId(acc, cfg);
  const live = observedPlaceId(acc);
  return String(gameModePerAccount() ? configured : shared || live).trim();
}
function gameCellImageMarkup(info) {
  return info.image_url
    ? `<img class="game-thumb" src="${esc(info.image_url)}" alt="" onerror="this.style.display='none'">`
    : '<span class="game-thumb game-thumb-empty">?</span>';
}
function gamePlaceCleanName(info, pid) {
  const rawName = String(info.name || "Place " + pid);
  return rawName.replace(
    /^\[([^\]]{1,12})\]\s*/,
    function (match, inner) {
      return /[A-Za-z0-9]/.test(inner) && !/[?\uFFFD]/.test(inner)
        ? match
        : "";
    },
  );
}
function gameHtmlFor(acc, cfg) {
  const pid = accountGamePlaceId(acc, cfg);
  if (!pid) return '<span class="game-empty">\u2014</span>';
  const info = GAME_PLACE_CACHE[pid];
  if (!info) {
    gamePlaceInfo(pid).then(() => {
      if (document.visibilityState !== "hidden") renderAccountsTable();
    });
    return '<span class="game-empty">\u2014</span>';
  }
  const img = gameCellImageMarkup(info);
  const name = esc(gamePlaceCleanName(info, pid));
  return `<div class="game-cell-inner">${img}<span class="game-name" title="${name}">${name}</span></div>`;
}
function saveSelectedUsers() {
  try {
    localStorage.setItem(
      "rg_selected_multi",
      JSON.stringify([...selectedUsers]),
    );
  } catch {}
}
function updateDeleteSelectedBtn() {
  const b = $("delete-selected-btn"),
    c = $("delete-selected-count");
  if (!b) return;
  const n = selectedUsers.size;
  b.hidden = n === 0;
  if (c) c.textContent = String(n);
  b.disabled = n === 0;
}
function isFinishedUser(u) {
  const a = ACCOUNTS.find(
    (a) => String(a.username).toLowerCase() === String(u).toLowerCase(),
  );
  if (!a) return !1;
  if ("finished" === String(a.manual_status || "").toLowerCase()) return !0;
  return Number(a.finished_at || 0) > 0;
}
function updateFinishedBtn() {
  const b = $("finish-toggle-btn"),
    c = $("finish-toggle-count");
  if (!b) return;
  const n = [...selectedUsers];
  b.hidden = 0 === n.length;
  if (c) c.textContent = String(n.length);
  if (!n.length) return;
  const f = n.every(isFinishedUser);
  try {
    b.childNodes[0].textContent = f ? "Unfinish (" : "Finished (";
  } catch {}
}
const BANNED_IMPORT_STATUSES = ["banned", "suspended"];
const BANNED_REASON_MARKERS = [
  "account banned",
  "suspended by roblox",
  "account suspended",
];
function accountByUsername(u) {
  return ACCOUNTS.find(
    (a) => String(a.username).toLowerCase() === String(u).toLowerCase(),
  );
}
function accountFlagBanned(a) {
  try {
    return !!a.banned || !!a.suspended;
  } catch {
    return !1;
  }
}
function isBannedUser(u) {
  const a = accountByUsername(u);
  if (!a) return !1;
  if (accountFlagBanned(a)) return !0;
  const b = String(a.import_status || "").toLowerCase();
  if (BANNED_IMPORT_STATUSES.includes(b)) return !0;
  const s = String(a.blocked_reason || a.manual_status || "").toLowerCase();
  return BANNED_REASON_MARKERS.some((marker) => s.includes(marker));
}
function updateUnbanBtn() {
  const b = $("unban-toggle-btn"),
    c = $("unban-toggle-count");
  if (!b) return;
  const n = [...selectedUsers].filter(isBannedUser);
  b.hidden = 0 === n.length;
  if (c) c.textContent = String(n.length);
}
function isCaptchaUser(u) {
  const a = ACCOUNTS.find(
    (a) => String(a.username).toLowerCase() === String(u).toLowerCase(),
  );
  if (!a) return !1;
  try {
    return !!(isCaptcha(a) || isLocked(a) || isAccountRecovery(a));
  } catch {
    return !1;
  }
}
function updateResumeBtn() {
  const b = $("resume-selected-btn"),
    c = $("resume-selected-count");
  if (!b) return;
  const n = [...selectedUsers].filter(isCaptchaUser);
  b.hidden = 0 === n.length;
  if (c) c.textContent = String(n.length);
}
async function resumeSelected() {
  const all = [...selectedUsers];
  if (!all.length) return;
  const caps = all.filter(isCaptchaUser);
  const skipped = all.filter((u) => !isCaptchaUser(u));
  if (!caps.length) {
    toast("No selected account needs resume (" + all.length + " skipped)");
    return;
  }
  commandBusy = !0;
  renderAccountsTable();
  try {
    const r = await api("/accounts/verify/resume", "POST", { usernames: caps });
    await loadAccountsConfig(!0);
    toast(
      (r.msg || "Resumed " + caps.length) +
        (skipped.length
          ? ", skipped " + skipped.length + " (not captcha/locked/recovery)"
          : ""),
    );
    await manualSnapshot();
  } catch (e) {
    toast(e.message);
  } finally {
    commandBusy = !1;
    renderAccountsTable();
  }
}
async function toggleFinishedAccounts() {
  const n = [...selectedUsers];
  if (!n.length) return;
  if (n.every(isFinishedUser)) {
    commandBusy = !0;
    renderAccountsTable();
    try {
      await api("/accounts/finish", "POST", { usernames: n, finished: !1 });
      await loadAccountsConfig(!0);
      toast("Back to normal");
      await manualSnapshot();
    } catch (e) {
      toast(e.message);
    } finally {
      commandBusy = !1;
      renderAccountsTable();
    }
    return;
  }
  const l =
    n.length === 1
      ? `Mark <b>1 account</b> Finished? Roblox closes and the account is skipped until you Unfinish.`
      : `Mark <b>${n.length} accounts</b> Finished? Roblox closes and they are skipped until you Unfinish.`;
  (modal(
    "Mark Finished",
    `<div style="color:#8d9099;font-size:13px;line-height:1.5">${l}</div>`,
    '<button class="btn ghost" id="fin-sel-cancel">Cancel</button><button class="btn good" id="fin-sel-confirm">Mark Finished</button>',
  ),
    ($("fin-sel-cancel").onclick = closeModal),
    ($("fin-sel-confirm").onclick = async () => {
      closeModal();
      commandBusy = !0;
      renderAccountsTable();
      try {
        const r = await api("/accounts/finish", "POST", {
          usernames: n,
          finished: !0,
        });
        await loadAccountsConfig(!0);
        toast(r.msg || "Marked Finished");
        await manualSnapshot();
      } catch (e) {
        toast(e.message);
      } finally {
        commandBusy = !1;
        renderAccountsTable();
      }
    }));
}
const _colWidthsKey = "rg_col_widths";
const _MINCOLW = { 0: 44, 1: 110, 2: 140, 3: 170, 4: 150 };
const _COLW_ALLOW = ["col1", "col2", "col3"];
let _colWidths = {};
function sanitizeColWidths(raw) {
  const out = {};
  if (!raw || typeof raw !== "object") return out;
  for (const k of _COLW_ALLOW) {
    try {
      const v = Math.round(Number(raw[k]));
      if (!Number.isFinite(v)) continue;
      const idx = Number(String(k).slice(3));
      out[k] = Math.max(_MINCOLW[idx] || 80, Math.min(520, v));
    } catch {}
  }
  return out;
}
try {
  _colWidths = sanitizeColWidths(
    JSON.parse(localStorage.getItem(_colWidthsKey) || "{}"),
  );
} catch {
  _colWidths = {};
}
let _isResizing = false;
let _colWidthsSaveTimer = 0;
function persistColWidths() {
  try {
    localStorage.setItem(_colWidthsKey, JSON.stringify(_colWidths));
  } catch {}
  try {
    if (_colWidthsSaveTimer) clearTimeout(_colWidthsSaveTimer);
  } catch {}
  _colWidthsSaveTimer = setTimeout(() => {
    try {
      api("/config", "POST", { ui_col_widths: _colWidths }).catch(() => {
        /* intentional: no-op */
      });
    } catch {}
  }, 400);
}
function savedServerColWidths() {
  try {
    if (!CONFIG || !CONFIG.ui_col_widths) return {};
    return sanitizeColWidths(CONFIG.ui_col_widths);
  } catch (_) {
    return {};
  }
}
function storeLocalColWidths() {
  try {
    localStorage.setItem(_colWidthsKey, JSON.stringify(_colWidths));
  } catch {}
}
function pushColWidthsToServer() {
  try {
    api("/config", "POST", { ui_col_widths: _colWidths }).catch(() => {
      /* intentional: no-op */
    });
  } catch {}
}
function applyServerColWidths(clean) {
  _colWidths = clean;
  storeLocalColWidths();
}
function pullColWidthsFromConfig() {
  try {
    if (_isResizing) return;
    const clean = savedServerColWidths();
    if (Object.keys(clean).length) {
      applyServerColWidths(clean);
    } else if (Object.keys(_colWidths).length) {
      pushColWidthsToServer();
    }
  } catch {}
}
function applyColWidths() {
  const table = document.querySelector(".accounts-table-card table");
  if (!table) return;
  const ths = table.querySelectorAll("thead th");
  const has = Object.keys(_colWidths).length > 0;
  table.style.tableLayout = has ? "fixed" : "";
  ths.forEach((th, i) => {
    const k = "col" + i;
    if (i === ths.length - 1 || i === 0) {
      delete _colWidths[k];
      th.style.width = "";
      return;
    }
    const v = _colWidths[k];
    if (v)
      th.style.width = Math.max(_MINCOLW[i] || 80, Math.min(520, v)) + "px";
  });
}
function applyColumnWidth(table, th, key, width) {
  th.style.width = width + "px";
  table.style.tableLayout = "fixed";
  _colWidths[key] = width;
}
function beginColumnResize(table, th, i, handle, event) {
  event.preventDefault();
  _isResizing = !0;
  const key = "col" + i;
  const startX = event.clientX;
  const startW = th.offsetWidth;
  const onMove = (moveEvent) => {
    const width = Math.max(
      _MINCOLW[i] || 80,
      Math.min(520, startW + (moveEvent.clientX - startX)),
    );
    applyColumnWidth(table, th, key, width);
  };
  const onUp = () => {
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
    handle.classList.remove("dragging");
    _isResizing = !1;
    persistColWidths();
    applyColWidths();
    renderAccountFilters();
  };
  document.addEventListener("mousemove", onMove);
  document.addEventListener("mouseup", onUp);
  handle.classList.add("dragging");
}
function resetColumnWidth(table, th, i) {
  delete _colWidths["col" + i];
  th.style.width = "";
  if (!Object.keys(_colWidths).length) table.style.tableLayout = "";
  persistColWidths();
  renderAccountFilters();
}
function wireColumnResizer(table, th, i) {
  th.style.position = "relative";
  const handle = document.createElement("div");
  handle.className = "col-resizer";
  handle.dataset.col = String(i);
  handle.addEventListener("mousedown", (event) =>
    beginColumnResize(table, th, i, handle, event),
  );
  th.appendChild(handle);
  handle.addEventListener("dblclick", (event) => {
    event.preventDefault();
    event.stopPropagation();
    resetColumnWidth(table, th, i);
  });
}
function initColumnResizers() {
  const table = document.querySelector(".accounts-table-card table");
  if (!table) return;
  const ths = table.querySelectorAll("thead th");
  applyColWidths();
  ths.forEach((th, i) => {
    if (i === ths.length - 1 || i === 0) return;
    if (th.querySelector(".col-resizer")) return;
    wireColumnResizer(table, th, i);
  });
}
function privacyBlurOn() {
  try {
    const el = $("cronus-privacy-blur");
    if (el) return !!el.checked;
  } catch (_) {}
  return !!CONFIG.privacy_blur_usernames;
}
function privacyRandomOn() {
  try {
    const el = $("cronus-privacy-random");
    if (el) return !!el.checked;
  } catch (_) {}
  return !!CONFIG.privacy_randomize_usernames;
}
function privacyHash(s) {
  let h = 2166136261 >>> 0;
  const str = String(s || "");
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}
const PRIVACY_NAME_A = [
  "Shadow",
  "Silent",
  "Rapid",
  "Cosmic",
  "Neon",
  "Frost",
  "Blaze",
  "Ghost",
  "Turbo",
  "Pixel",
  "Storm",
  "Echo",
  "Nova",
  "Drift",
  "Onyx",
  "Vivid",
  "Crimson",
  "Azure",
  "Lunar",
  "Solar",
];
const PRIVACY_NAME_B = [
  "Fox",
  "Wolf",
  "Tiger",
  "Panda",
  "Falcon",
  "Viper",
  "Raven",
  "Bear",
  "Hawk",
  "Lion",
  "Otter",
  "Cobra",
  "Eagle",
  "Shark",
  "Ant",
  "Byte",
  "Cloud",
  "Dash",
  "Flux",
  "Zap",
];
function fakeNameCandidate(h1, h2, offset) {
  return (
    PRIVACY_NAME_A[(h1 + offset) % PRIVACY_NAME_A.length] +
    PRIVACY_NAME_B[(h2 + offset * 3) % PRIVACY_NAME_B.length] +
    "_" +
    (1000 + ((h1 + offset * 137) % 9000))
  );
}
function privacyTakenNames() {
  try {
    return new Set(
      (ACCOUNTS || []).map((a) => String(a.username || "").toLowerCase()),
    );
  } catch (_) {
    return null;
  }
}
function privacyFakeName(username) {
  const real = String(username || "").trim() || "Player";
  const h1 = privacyHash(real.toLowerCase()),
    h2 = privacyHash("#2:" + real.toLowerCase());
  let name = fakeNameCandidate(h1, h2, 0);
  if (name.toLowerCase() === real.toLowerCase())
    name =
      "Mystery" +
      PRIVACY_NAME_B[(h2 + 7) % PRIVACY_NAME_B.length] +
      "_" +
      (1000 + ((h1 + 13) % 9000));
  const taken = privacyTakenNames();
  if (!taken) return name;
  let guard = 0;
  while (taken.has(name.toLowerCase()) && guard < 9) {
    guard++;
    name = fakeNameCandidate(h1, h2, guard);
  }
  return name;
}
function privacyDisplayName(username) {
  return privacyRandomOn() ? privacyFakeName(username) : String(username || "");
}
function renderAccountsTable() {
  if (
    document.activeElement?.classList.contains("desc-textarea") ||
    accountTableTextSelectionActive()
  )
    return;
  renderAccountFilters();
  const e = filtered();
  $("accounts-table").innerHTML = renderAccountRows({
    rows: e,
    selectedUser: selectedUsers,
    editingDescUser: EDITING_DESC_USER,
    cfgFor: cfgFor,
    bucket: bucket,
    isCaptcha: isCaptcha,
    rowStatusLabel: rowStatusLabel,
    avatarHtml: avatarHtml,
    esc: esc,
    gameHtmlFor: gameHtmlFor,
    privacyBlur: privacyBlurOn(),
    displayNameFor: privacyDisplayName,
  });
  updateDeleteSelectedBtn();
  updateFinishedBtn();
  updateUnbanBtn();
  updateResumeBtn();
  initColumnResizers();
  if (EDITING_DESC_USER) {
    const ta = document.querySelector(
      `.desc-textarea[data-user="${EDITING_DESC_USER}"]`,
    );
    if (ta && document.activeElement !== ta) {
      ta.focus();
      ta.setSelectionRange(ta.value.length, ta.value.length);
    }
  }
}
function placeFromPrivate(e) {
  const t = String(e || "").match(/roblox\.com\/games\/(\d+)/i);
  return t ? t[1] : "";
}
function compactCount(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) return "";
  if (n >= 1e6) {
    const v = (n / 1e6).toFixed(n >= 1e7 ? 1 : 2).replace(/\.0$/, "");
    return v.replace(/\.?0+$/, "") + "M";
  }
  if (n >= 1e3) {
    const v = (n / 1e3).toFixed(n >= 1e5 ? 0 : 1).replace(/\.0$/, "");
    return v.replace(/\.?0+$/, "") + "K";
  }
  return String(n);
}
const MAP_STAT_ICONS = {
  playing:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/></svg>',
  visits:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
  favorite:
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>',
};
function appendMapStat(list, value, label, icon) {
  if (!Number.isFinite(Number(value)) || Number(value) < 0) return;
  list.push(
    `<span class="map-stat">${icon}${esc(compactCount(value))} ${label}</span>`,
  );
}
function appendMapFavoriteStat(list, value) {
  if (!Number.isFinite(Number(value)) || Number(value) < 0) return;
  list.push(
    `<span class="map-stat">${MAP_STAT_ICONS.favorite}${esc(compactCount(value))}</span>`,
  );
}
function appendMapMaxPlayersStat(list, value) {
  if (!Number.isFinite(Number(value)) || Number(value) <= 0) return;
  list.push(`<span class="map-stat">${esc(String(value))} per server</span>`);
}
function mapStatsHtml(info) {
  const stats = [];
  appendMapStat(stats, info.playing, "playing", MAP_STAT_ICONS.playing);
  appendMapStat(stats, info.visits, "visits", MAP_STAT_ICONS.visits);
  appendMapFavoriteStat(stats, info.favorited_count);
  appendMapMaxPlayersStat(stats, info.max_players);
  return stats.length ? `<div class="map-stats">${stats.join("")}</div>` : "";
}
function mapCardMarkup(placeId, info) {
  const genre = String(info.genre || "").trim(),
    by = String(info.builder || "").trim();
  const genreHtml = genre
    ? `<span class="map-genre">${esc(genre)}</span>`
    : "";
  const byline = by ? `by ${esc(by)}` : "Roblox experience";
  return `<img class="map-thumb" src="${esc(info.image_url || "")}" alt="" onerror="this.style.visibility=\'hidden\'"><div class="map-copy"><div class="map-name">${esc(info.name || "Place " + placeId)}</div><div class="map-byline"><span>${byline}</span>${genreHtml}</div>${mapStatsHtml(info)}</div>`;
}
function renderGameMap(e = "", t = null, a = "") {
  const card = $("game-map-card");
  if (!card) return;
  const placeId = String(e || "").trim(),
    info = t || GAME_PLACE_CACHE[placeId];
  if (info) {
    (card.className = "map-card has-stats"),
      (card.innerHTML = mapCardMarkup(placeId, info));
    return;
  }
  ((card.className = "map-card empty"),
    (card.innerHTML = `<div>${esc(a || (placeId ? "Loading Roblox map details" : "Enter Place ID"))}</div>`));
}
async function loadGamePlace(e, t = !1) {
  const a = String(e || "").trim();
  if (!a) return (renderGameMap("", null), null);
  renderGameMap(a, null, "Loading Roblox map details");
  const n = await api("/game/place/" + encodeURIComponent(a));
  return (
    (GAME_PLACE_CACHE[a] = n),
    renderGameMap(a, n),
    t || toast(n.name || "place loaded"),
    n
  );
}
async function gamePlaceInfo(e) {
  const t = String(e || "").trim();
  if (!t) return null;
  if (GAME_PLACE_CACHE[t]) return GAME_PLACE_CACHE[t];
  try {
    const a = await api("/game/place/" + encodeURIComponent(t));
    return ((GAME_PLACE_CACHE[t] = a), a);
  } catch (e) {
    return null;
  }
}
let _statusHoverPop = null;
let _statusHoverTimer = 0;
function statusHoverHide() {
  _statusHoverTimer = 0;
  if (_statusHoverPop) {
    _statusHoverPop.remove();
    _statusHoverPop = null;
  }
}
function onlinePopoverMarkup(acc, place, info) {
  const user = esc(privacyDisplayName(acc.display || acc.username)),
    name = info?.name
      ? esc(info.name)
      : place
        ? `Place ${esc(place)}`
        : "Unknown game",
    by = info?.builder ? esc(info.builder) : "",
    playSeconds = uptimeSeconds(acc.uptime),
    playTime =
      playSeconds > 0
        ? `<div class="ogame-time">Playing ${esc(shortDuration(playSeconds))}</div>`
        : "",
    img = info?.image_url
      ? `<img class="ogame-thumb" src="${esc(info.image_url)}" alt="" loading="lazy" onerror="this.hidden=true">`
      : "",
    thumbs =
      img ||
      `<span class="ogame-thumb ogame-thumb-empty" aria-hidden="true">${esc(initials(user))}</span>`;
  return `<div class="ogame-card">${thumbs}<div class="ogame-copy"><div class="ogame-user">${user}</div><div class="ogame-name">${name}</div>${by ? `<div class="ogame-by">by ${by}</div>` : ""}${playTime}</div></div>`;
}
function positionStatusPopover(pop, table, username, span) {
  const q = 'tr[data-user="' + CSS.escape(String(username)) + '"] .status',
    anchor = table.querySelector(q) || span,
    rect = anchor?.getBoundingClientRect?.(),
    pw = Math.min(pop.offsetWidth || 320, 360);
  let left = (rect?.right ?? 0) + 8;
  left + pw > window.innerWidth - 8 &&
    (left = Math.max(8, window.innerWidth - pw - 8));
  const top = Math.min(
    rect?.top ?? 0,
    window.innerHeight - pop.offsetHeight - 8,
  );
  ((pop.style.left = left + "px"),
    (pop.style.top = Math.max(8, top) + "px"),
    pop.addEventListener("mouseenter", () => clearTimeout(_statusHoverTimer)),
    pop.addEventListener("mouseleave", statusHoverHide));
}
async function statusHoverShow(username, span, table) {
  if (!username) return statusHoverHide();
  const acc = ACCOUNTS.find((a) => String(a.username) === String(username));
  if (!acc || bucket(acc) !== "online") return statusHoverHide();
  const place = String(
      acc.observed_place_id || cfgFor(username).place_id || "",
    ).trim(),
    info = place ? await gamePlaceInfo(place) : null;
  if (_statusHoverPop) {
    if (_statusHoverPop.dataset.user === username) return;
    statusHoverHide();
  }
  _statusHoverPop = document.createElement("div");
  _statusHoverPop.className = "online-popover";
  _statusHoverPop.dataset.user = username;
  _statusHoverPop.innerHTML = onlinePopoverMarkup(acc, place, info);
  document.body.appendChild(_statusHoverPop);
  positionStatusPopover(_statusHoverPop, table, username, span);
}
function initStatusHover() {
  const table = $("accounts-table");
  if (!table) return;
  table.addEventListener("mouseover", (e) => {
    const span = e.target.closest(".status");
    if (!span) return;
    const tr = span.closest("tr[data-user]");
    tr && statusHoverShow(tr.dataset.user, span, table);
  });
  table.addEventListener("mouseleave", () => {
    (clearTimeout(_statusHoverTimer),
      (_statusHoverTimer = setTimeout(statusHoverHide, 140)));
  });
}
function panelCtx() {
  return {
    $: $,
    api: api,
    CONFIG: CONFIG,
    PERF: PERF,
    GRAPHICS: GRAPHICS,
    WINDOW_SIZE: WINDOW_SIZE,
    CPU_LIMITER: CPU_LIMITER,
    RAM_CLEANUP: RAM_CLEANUP,
    VIRTUAL_MEMORY: VIRTUAL_MEMORY,
    modal: modal,
    closeModal: closeModal,
    TROUBLESHOOT: TROUBLESHOOT,
    WINDOW_SIZE_PRESETS: WINDOW_SIZE_PRESETS,
    dirty: dirty,
    updateSaveState: updateSaveState,
    clearDirty: clearDirty,
    toast: toast,
    esc: esc,
    loadConfig: loadConfig,
    loadGraphics: loadGraphics,
    manualSnapshot: manualSnapshot,
    renderPerformance: renderPerformance,
    renderRam: renderRam,
    renderRamCleanup: renderRamCleanup,
    renderVirtualMemory: renderVirtualMemory,
    renderGraphics: renderGraphics,
    renderWindowSize: renderWindowSize,
    renderCpuLimiter: renderCpuLimiter,
  };
}
function renderSettings() {
  return renderSettingsPanel(panelCtx());
}
function renderPerformance(e) {
  return (e && (PERF = e), renderPerformancePanel(panelCtx()));
}
function renderRam() {
  return renderRamPanel(panelCtx());
}
function renderRamCleanup(e) {
  return (e && (RAM_CLEANUP = e), renderRamCleanupPanel(panelCtx()));
}
function getTotalRamGb() {
  try {
    return Number((VIRTUAL_MEMORY && VIRTUAL_MEMORY.total_ram_gb) || 0) || 0;
  } catch (_) {
    return 0;
  }
}
function renderVirtualMemory(e) {
  return (e && (VIRTUAL_MEMORY = e), renderVirtualMemoryPanel(panelCtx()));
}
function renderGraphics(e) {
  return (e && (GRAPHICS = e), renderGraphicsPanel(panelCtx()));
}
function renderWindowSize(e) {
  return (e && (WINDOW_SIZE = e), renderWindowSizePanel(panelCtx()));
}
function pickWindowSizeGrid(e, t) {
  const a = $("window-grid-cols"),
    r = $("window-grid-rows");
  if (!a || !r) return;
  ((a.value = String(e)),
    (r.value = String(t)),
    markDirty("window-size"),
    renderWindowSize());
}
function renderCpuLimiter(e) {
  return (e && (CPU_LIMITER = e), renderCpuLimiterPanel(panelCtx()));
}
function renderTroubleshoot() {
  return renderTroubleshootPanel(panelCtx());
}
function highlightLua(src) {
  const esc = (s) =>
    String(s).replace(
      /[&<>]/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c],
    );
  const re =
    /(--\[[\s\S]*?\]\]|--[^\n]*)|("(?:\\.|[^"\\\n])*")|\b(local|function|end|if|then|elseif|else|return|for|while|do|break|repeat|until|in|and|or|not|true|false|nil)\b|\b(\d+(?:\.\d+)?)\b|([.:])([A-Za-z_]\w*)|([A-Za-z_]\w*)(\s*\()?/g;
  let out = "",
    last = 0,
    m;
  while ((m = re.exec(src))) {
    out += esc(src.slice(last, m.index));
    if (m[1]) out += '<span class="tok-com">' + esc(m[1]) + "</span>";
    else if (m[2]) out += '<span class="tok-str">' + esc(m[2]) + "</span>";
    else if (m[3]) out += '<span class="tok-kw">' + esc(m[3]) + "</span>";
    else if (m[4]) out += '<span class="tok-num">' + esc(m[4]) + "</span>";
    else if (m[5])
      out += esc(m[5]) + '<span class="tok-prop">' + esc(m[6]) + "</span>";
    else if (m[7])
      out += m[8]
        ? '<span class="tok-fn">' + esc(m[7]) + "</span>" + esc(m[8])
        : esc(m[7]);
    else out += esc(m[0]);
    last = re.lastIndex;
  }
  return out + esc(src.slice(last));
}
function developerLuaMarkup() {
  if (DEVELOPER_LUA_SOURCE) return highlightLua(DEVELOPER_LUA_SOURCE);
  return DEVELOPER_LUA_LOADING
    ? '<span class="tok-com">Loading Lua...</span>'
    : "";
}
function renderDeveloperCopyButton(btn) {
  btn.disabled = !1;
  const ok = Date.now() - (+btn.dataset.copiedAt || 0) < 1600;
  btn.classList.toggle("is-ok", !!ok);
  btn.innerHTML = `${solarIcon("copy", "btn-icon")}<span>${DEVELOPER_LUA_LOADING && !DEVELOPER_LUA_SOURCE ? "Loading" : ok ? "Copied" : "Copy Loader"}</span>`;
}
function renderDeveloperGateFields() {
  const enabled = $("dev-lua-enabled"),
    timeout = $("dev-lua-timeout"),
    field = $("dev-lua-timeout-field");
  if (!enabled || !timeout || !field) return;
  const active = document.activeElement?.id;
  if (!dirty("developer") && "dev-lua-enabled" !== active)
    enabled.checked = !1 !== CONFIG.lua_enabled;
  if (!dirty("developer") && "dev-lua-timeout" !== active)
    timeout.value = String(CONFIG.lua_timeout_seconds ?? 60);
  setReveal(field, enabled.checked);
}
function renderDeveloper() {
  const code = $("developer-lua-code"),
    notice = $("developer-lua-notice");
  if (code) code.innerHTML = developerLuaMarkup();
  if (notice) {
    notice.textContent = DEVELOPER_LUA_ERROR;
    notice.hidden = !DEVELOPER_LUA_ERROR;
  }
  const copyBtn = $("developer-copy-lua");
  if (copyBtn) renderDeveloperCopyButton(copyBtn);
  const snippet = $("developer-copy-lua-snippet");
  if (snippet) {
    snippet.querySelector("code").textContent = DEVELOPER_LUA_COPY_SNIPPET;
    snippet.onclick = () => {
      copyDeveloperLua();
    };
  }
  renderDeveloperGateFields();
  syncToggleLabels();
}
let DEV_LUA_SAVE_TIMER = 0;
function queueDeveloperLuaSave() {
  markDirty("developer");
}
async function saveDeveloperLua() {
  return saveDeveloperSettings();
}
function selectCopyText(e, t) {
  if (!t) return !1;
  const a = t.hasAttribute("readonly");
  try {
    return (
      a && t.removeAttribute("readonly"),
      (t.value = e),
      t.focus(),
      t.select(),
      t.setSelectionRange(0, t.value.length),
      !0
    );
  } catch (e) {
    return !1;
  } finally {
    a && t.setAttribute("readonly", "readonly");
  }
}
async function copyTextToClipboard(e, t) {
  if (selectCopyText(e, t))
    try {
      if (document.execCommand && document.execCommand("copy")) return !0;
    } catch (e) {}
  const a = document.createElement("textarea");
  ((a.value = e),
    a.setAttribute("readonly", ""),
    (a.style.cssText = "position:fixed;left:-9999px;top:0"),
    document.body.appendChild(a));
  try {
    (a.focus(), a.select(), a.setSelectionRange(0, a.value.length));
    if (document.execCommand && document.execCommand("copy")) return !0;
  } catch (e) {
  } finally {
    a.remove();
  }
  if (!navigator.clipboard?.writeText) return !1;
  try {
    return await Promise.race([
      navigator.clipboard
        .writeText(e)
        .then(() => !0)
        .catch(() => !1),
      new Promise((e) => setTimeout(() => e(!1), 1500)),
    ]);
  } catch (e) {
    return !1;
  }
}
const DEVELOPER_LUA_COPY_SNIPPET =
  'loadstring(game:HttpGet("https://raw.githubusercontent.com/xspww/Loader/refs/heads/master/Main/main.lua"))()';
async function copyDeveloperLua() {
  const b = $("developer-copy-lua");
  toast("Copying loader...");
  const a = await copyTextToClipboard(DEVELOPER_LUA_COPY_SNIPPET, null);
  b &&
    ((b.dataset.copiedAt = a ? String(Date.now()) : ""),
    renderDeveloper(),
    a &&
      setTimeout(() => {
        renderDeveloper();
      }, 1700));
  toast(a ? "Loader copied to clipboard" : "Copy failed");
}
function setStreamState(e, t = "stream") {
  const a = $("stream-state");
  a && ((a.textContent = e), (a.className = t));
}
const statusRuntime = createStatusRuntime({
  api: api,
  nextSequence: () => ++STATUS_REQUEST_SEQ,
  acceptStatus: acceptStatus,
  setStreamState: setStreamState,
  loadAccountsConfig: loadAccountsConfig,
  render: render,
  loadAvatars: loadAvatars,
  toast: toast,
});
async function manualSnapshot() {
  return statusRuntime.manualSnapshot();
}
const PREFLIGHT_DEFAULT_REASON = "Private server unavailable";
function toastPreflightFailure(failure) {
  const name = failure.game_name || "",
    place = failure.place_id || "",
    reason = failure.reason || PREFLIGHT_DEFAULT_REASON;
  if (failure.fallback_public) {
    toast(
      `No VIP for ${name || "game"}${place ? ` (${place})` : ""} - joining public server`,
      "warning",
    );
    return;
  }
  toast(
    `Auto Create failed${name ? ": " + name : ""}${place ? ` (${place})` : ""} - ${reason}`,
    "error",
  );
}
function toastPreflightCreated(entry) {
  const name = entry.game_name || entry.place_id || "game";
  toast(`Private server ready for ${name} - link saved`, "success");
}
function privateServerPreflightToasts(e) {
  const preflight = e?.private_server_preflight || {},
    failures = Array.isArray(preflight.failures) ? preflight.failures : [],
    created = Array.isArray(preflight.created) ? preflight.created : [];
  failures.forEach(toastPreflightFailure);
  created.forEach(toastPreflightCreated);
}
async function toggleGuard() {
  if (!commandBusy && (STATUS.running || requireSavedSettings())) {
    ((commandBusy = !0), renderTop());
    try {
      const e = await api(STATUS.running ? "/stop" : "/start", "POST");
      (toast(e.msg || "command sent", e && e.ok === false ? "error" : void 0),
        privateServerPreflightToasts(e),
        await manualSnapshot());
    } catch (e) {
      toast(e.message);
    } finally {
      ((commandBusy = !1), renderTop());
    }
  }
}
function versionManagerMark() {
  const m = document.querySelector("#modal-backdrop .modal");
  m && m.classList.add("vmanager");
}
async function openVersionManager() {
  (versionManagerMark(),
    modal(
      "Join game",
      '<div class="v-sub">Select a Roblox version to join with</div><div class="vchoices"><div class="v-loading">Loading versions...</div></div>',
      `<button class="btn ghost has-icon" id="vmanage-btn">${solarIcon("settings", "btn-icon")}<span>Manage</span></button><button class="btn ghost" id="vcancel-btn">Cancel</button>`,
    ));
  const a = $("vcancel-btn"),
    b = $("vmanage-btn");
  (a && (a.onclick = closeModal),
    b &&
      (b.onclick = () => {
        (closeModal(), setView("troubleshoot"));
      }));
  let d;
  try {
    d = await api("/troubleshoot/roblox-install/versions");
  } catch (e) {
    return toast(e.message);
  }
  renderVersionChoices(d);
}
function collectRobloxVersions(d) {
  const raw = Array.isArray(d?.versions) ? d.versions : [],
    seen = new Set(),
    vs = [];
  for (const v of raw) {
    const ver = String(v.version || "").trim();
    if (!ver) continue;
    const src =
      String(v.source || "roblox")
        .trim()
        .toLowerCase() || "roblox";
    if (src !== "roblox") continue;
    const key = ver.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    vs.push(v);
  }
  return vs;
}
function versionChoiceHtml(v) {
  const ver = esc(String(v.version || "").trim()),
    launchVer = String(v.version || "").trim(),
    launchEsc = esc(launchVer),
    badge = v.is_latest
      ? '<span class="vbadge last">Last</span>'
      : '<span class="vbadge old">Old</span>';
  return `<div class="vchoice" data-source="roblox" data-version="${ver}"><div class="vchoice-copy"><div class="vchoice-name">Roblox ${badge}</div><div class="vchoice-version">${ver}</div></div><button type="button" class="vplay" data-source="roblox" data-version="${launchEsc}" title="Join with this version" aria-label="Join with ${ver}">${solarIcon("play", "")}</button><button type="button" class="vfolder" data-version="${launchEsc}" title="Open install folder" aria-label="Open folder for ${ver}">${solarIcon("folder", "")}</button></div>`;
}
function addStartAnywayButton(hasVersions) {
  const foot = $("modal-foot");
  if (hasVersions || !foot || $("vstart-btn")) return;
  const button = document.createElement("button");
  button.id = "vstart-btn";
  button.className = "btn good";
  button.textContent = "Start Anyway";
  button.onclick = () => startWithVersion("");
  foot.appendChild(button);
}
function renderVersionChoices(d) {
  const body = $("modal-body");
  if (!body) return;
  const vs = collectRobloxVersions(d);
  const html = [
    '<div class="v-sub">Select a Roblox version to join with</div><div class="vchoices">',
  ];
  if (vs.length) html.push(...vs.map(versionChoiceHtml));
  else
    html.push(
      '<div class="v-empty">No Roblox versions detected on this PC.</div>',
    );
  html.push("</div>");
  if (!vs.length) html.push('<div class="v-hint">No versions found ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â use Manage to repair or reinstall Roblox.</div>');
  body.innerHTML = html.join("");
  body.querySelectorAll(".vplay").forEach((btn) => {
    btn.onclick = () => startWithVersion(btn.dataset.version || "");
  });
  body.querySelectorAll(".vfolder").forEach((btn) => {
    btn.onclick = () => openVersionFolder(btn.dataset.version || "");
  });
  addStartAnywayButton(vs.length);
}
async function openVersionFolder(version) {
  try {
    const r = await api("/troubleshoot/roblox-install/open-location", "POST", {
      version: version || "",
      source: "roblox",
    });
    toast(r.ok === false ? r.msg || "Could not open folder" : "Folder opened");
  } catch (e) {
    toast(e.message);
  }
}
async function startWithVersion(version) {
  closeModal();
  if (commandBusy) return;
  ((commandBusy = !0), renderTop());
  try {
    const e = await api("/start", "POST", { roblox_version: version || "" });
    (toast(e.msg || "command sent", e && e.ok === false ? "error" : void 0),
      privateServerPreflightToasts(e),
      await manualSnapshot());
  } catch (e) {
    toast(e.message);
  } finally {
    ((commandBusy = !1), renderTop());
  }
}
async function onGuardPress() {
  if (STATUS.running || commandBusy) return toggleGuard();
  if (!requireSavedSettings()) return;
  await openVersionManager();
}
async function saveAccounts(e, t = "accounts saved") {
  const a = await api("/accounts", "POST", e);
  if (!a.ok) throw new Error(a.msg || "Save failed");
  ((ACCOUNT_CONFIG = e),
    (configLoadedAt = Date.now()),
    toast(t),
    await manualSnapshot());
}
async function saveDescription(e, t) {
  const a = await loadAccountsConfig(!0),
    n = lower(e),
    r = a.map((e) => (lower(e.username) === n ? { ...e, description: t } : e));
  await saveAccounts(r, "Saved");
}
async function reloadCookies() {
  if (!commandBusy) {
    ((commandBusy = !0), renderTop());
    try {
      const e = await api("/accounts/reload", "POST", {});
      ((ACCOUNT_CONFIG = []),
        (configLoadedAt = 0),
        toast(e.msg || "Reloaded cookies"),
        await loadAccountsConfig(!0),
        await manualSnapshot());
    } catch (e) {
      toast(e.message);
    } finally {
      ((commandBusy = !1), renderTop());
    }
  }
}
async function deleteSelectedAccounts() {
  const n = [...selectedUsers];
  if (!n.length) return;
  const label =
    n.length === 1
      ? `Delete <b>1 account</b>? This cannot be undone.`
      : `Delete <b>${n.length} accounts</b>? This cannot be undone.`;
  (modal(
    "Delete accounts",
    `<div style="color:#8d9099;font-size:13px;line-height:1.5">${label}</div>`,
    '<button class="btn ghost" id="del-sel-cancel">Cancel</button><button class="btn danger" id="del-sel-confirm">Delete</button>',
  ),
    ($("del-sel-cancel").onclick = closeModal),
    ($("del-sel-confirm").onclick = async () => {
      closeModal();
      try {
        const keep = ACCOUNT_CONFIG.filter(
          (a) =>
            !n.some(
              (x) =>
                String(x).toLowerCase() === String(a.username).toLowerCase(),
            ),
        );
        await saveAccounts(keep, n.length + " account(s) deleted");
        selectedUsers.clear();
        saveSelectedUsers();
        updateDeleteSelectedBtn();
        updateFinishedBtn();
        updateUnbanBtn();
        updateResumeBtn();
        await manualSnapshot();
      } catch (e) {
        toast(e.message);
      }
    }));
}
function deleteAccount(e) {
  if (!e) return;
  const t = ACCOUNTS.find((t) => t.username === e) || {},
    a = cfgFor(e),
    n = t.display || a.alias || e;
  (modal(
    "Delete Account",
    `<div class="choice-grid" style="grid-template-columns:1fr"><div class="choice" style="cursor:default">${cardIcon("delete")}<strong>Delete <span class="danger-name">${esc(n)}</span></strong><span class="hint">Removes this account and cookie</span></div></div>`,
    '<button class="btn ghost" id="delete-back">Back</button><button class="btn danger" id="delete-confirm">Delete Account</button>',
  ),
    ($("delete-back").onclick = closeModal),
    ($("delete-confirm").onclick = () => confirmDeleteAccount(e)));
}
async function confirmDeleteAccount(e) {
  if (e) {
    ((commandBusy = !0), renderAccountsTable());
    try {
      const t = await loadAccountsConfig(!0),
        a = lower(e),
        n = t.filter((e) => lower(e.username) !== a);
      (await saveAccounts(n, "Deleted"),
        lower(selectedUser) === a &&
          ((selectedUser = n[0]?.username || ""),
          selectedUser
            ? window.__cronusStore("rg_selected", selectedUser)
            : window.__cronusRemove("rg_selected")),
        closeModal());
    } catch (e) {
      toast(e.message);
    } finally {
      ((commandBusy = !1), renderAccountsTable());
    }
  }
}
function launchBlockedAccountMessage(account) {
  if (isSuspended(account))
    return (
      account.blocked_reason ||
      "Account suspended by Roblox. Unmark to allow rejoin."
    );
  if (isBanned(account))
    return (
      account.blocked_reason ||
      "Account banned (Roblox). Unmark to allow rejoin."
    );
  if (isLocked(account))
    return (
      account.blocked_reason ||
      "Account locked - verify face/ID manually, then click Resume"
    );
  if (isAccountRecovery(account))
    return (
      account.blocked_reason ||
      "Account recovery - complete passkey/2FA manually, then click Resume"
    );
  return "";
}
function launchBlockedReason(account) {
  if (!account) return "";
  const message = launchBlockedAccountMessage(account);
  if (message) return message;
  if (isCaptcha(account))
    return "Solve CAPTCHA manually, then Resume or Reload Cookies";
  if (isBlocked(account))
    return account.blocked_reason || "Reimport correct cookie first";
  return "";
}
function launchPayload(username) {
  const config = cfgFor(username);
  return {
    place_id: config.place_id || "",
    game_id: config.game_id || "",
    global_vip_link: CONFIG.game_private_server_url || "",
    auto_create_private_server_enabled:
      !!CONFIG.auto_create_private_server_enabled,
    auto_create_private_server_free_only:
      !1 !== CONFIG.auto_create_private_server_free_only,
    multi_roblox: !1 !== CONFIG.multi_roblox_enabled,
  };
}
async function launchAccount(e) {
  if (!e || !requireSavedSettings()) return;
  const blockedReason = launchBlockedReason(
    ACCOUNTS.find((account) => account.username === e),
  );
  if (blockedReason) return toast(blockedReason);
  try {
    const t = await api(
      "/account/" + encodeURIComponent(e) + "/launch",
      "POST",
      launchPayload(e),
    );
    (toast(t.msg || "launch requested"), await manualSnapshot());
  } catch (e) {
    toast(e.message);
  }
}
async function unbanMarkAccount(e) {
  if (!e) return;
  try {
    const t = await api(
      "/account/" + encodeURIComponent(e) + "/unban-mark",
      "POST",
      {},
    );
    toast(t.msg || "Cleared Banned mark");
    await loadAccountsConfig(!0);
    await manualSnapshot();
  } catch (e) {
    toast(e.message);
  }
}
async function unbanMarkSelected() {
  const n = [...selectedUsers];
  if (!n.length) return;
  try {
    const t = await api("/accounts/unban-mark", "POST", { usernames: n });
    toast(t.msg || "Cleared Banned");
    await loadAccountsConfig(!0);
    await manualSnapshot();
  } catch (e) {
    toast(e.message);
  }
}
async function dedupeAccount(e) {
  if (e)
    try {
      const t = await api(
        "/account/" + encodeURIComponent(e) + "/kill-duplicate",
        "POST",
        {},
      );
      (toast("duplicates closed: " + (t.count || 0)), await manualSnapshot());
    } catch (e) {
      toast(e.message);
    }
}
async function focusCaptcha(e) {
  if (e) {
    ((commandBusy = !0), renderAccountsTable());
    try {
      const t = await api(
        "/account/" + encodeURIComponent(e) + "/captcha/focus",
        "POST",
        {},
      );
      toast(t.msg || "Roblox window focused");
    } catch (e) {
      toast(e.message);
    } finally {
      ((commandBusy = !1), renderAccountsTable());
    }
  }
}
async function openCaptchaLogin(e) {
  if (e) {
    ((commandBusy = !0), renderAccountsTable());
    try {
      const t = await api(
        "/account/" + encodeURIComponent(e) + "/captcha/open-login",
        "POST",
        {},
      );
      toast(t.msg || "Roblox login opened");
    } catch (e) {
      toast(e.message);
    } finally {
      ((commandBusy = !1), renderAccountsTable());
    }
  }
}
async function resumeCaptcha(e) {
  if (e) {
    ((commandBusy = !0), renderAccountsTable());
    try {
      let t;
      try {
        t = await api(
          "/account/" + encodeURIComponent(e) + "/verify/resume",
          "POST",
          {},
        );
      } catch (_e) {
        t = await api(
          "/account/" + encodeURIComponent(e) + "/captcha/resume",
          "POST",
          {},
        );
      }
      (toast(t.msg || "Resume requested"),
        await loadAccountsConfig(!0),
        await manualSnapshot());
    } catch (e) {
      toast(e.message);
    } finally {
      ((commandBusy = !1), renderAccountsTable());
    }
  }
}
function closeAllRoblox() {
  commandBusy ||
    (modal(
      "Close All Roblox",
      `<div class="choice-grid" style="grid-template-columns:1fr"><div class="choice" style="cursor:default">${cardIcon("close")}<strong>Close Roblox only</strong><span class="hint">Closes every Roblox window, Cronus stays running</span></div></div>`,
      '<button class="btn ghost" id="close-all-back">Back</button><button class="btn danger" id="close-all-confirm">Close All Roblox</button>',
    ),
    ($("close-all-back").onclick = closeModal),
    ($("close-all-confirm").onclick = confirmCloseAllRoblox));
}
async function confirmCloseAllRoblox() {
  if (!commandBusy) {
    ((commandBusy = !0), renderTop());
    try {
      const e = await api("/roblox/close-all", "POST", {});
      (closeModal(), toast(e.msg || "Roblox closed"), await manualSnapshot());
    } catch (e) {
      toast(e.message);
    } finally {
      ((commandBusy = !1), renderTop());
    }
  }
}
let manualPollTimer = 0;
function openAdd() {
  openAddChoice();
}
function openAddChoice() {
  modal(
    "Add Accounts",
    `<div class="choice-grid"><button type="button" class="choice" id="add-choice-manual" style="cursor:pointer;font:inherit;text-align:left">${cardIcon("userAdd")}<strong>Manual Login</strong><span class="hint">Sign in via a popup browser. 2FA, captcha, passkey - all you.</span></button><button type="button" class="choice" id="add-choice-cookie" style="cursor:pointer;font:inherit;text-align:left"><span class="choice-icon">${solarIcon("copy")}</span><strong>Cookie Import</strong><span class="hint">Paste user:pass:cookie or cookie-only lines.</span></button></div>`,
    '<div class="cookie-modal-actions single-col"><button type="button" class="cookie-btn-back" id="add-choice-cancel">Cancel</button></div>',
  );
  (($("add-choice-manual").onclick = openManualChoice),
    ($("add-choice-cookie").onclick = openCookie),
    ($("add-choice-cancel").onclick = closeModal));
}
function openManualChoice() {
  modal(
    "Manual Login",
    `<div class="choice-grid"><button type="button" class="choice" id="manual-choice-single" style="cursor:pointer;font:inherit;text-align:left">${cardIcon("userAdd")}<strong>Single</strong><span class="hint">One account. Enter optional credentials, sign in once.</span></button><button type="button" class="choice" id="manual-choice-multi" style="cursor:pointer;font:inherit;text-align:left"><span class="choice-icon">${solarIcon("list")}</span><strong>Multi</strong><span class="hint">Paste user:pass per line. Popups open one after another.</span></button></div>`,
    '<div class="cookie-modal-actions single-col"><button type="button" class="cookie-btn-back" id="manual-choice-back">Back</button></div>',
  );
  (($("manual-choice-single").onclick = openManualSingle),
    ($("manual-choice-multi").onclick = openManualMulti),
    ($("manual-choice-back").onclick = openAddChoice));
}
function openManualSingle() {
  modal(
    "Manual Login - Single",
    `<div class="cookie-import-container"><div class="cookie-hint-banner">Credentials are <strong>optional</strong> - they only prefill the popup. <strong>2FA, captcha and passkey</strong> are completed by you, then the cookie is captured automatically.</div><label class="manual-label" for="manual-username">Username (optional)</label><input id="manual-username" class="manual-input" autocomplete="username" placeholder="roblox username"><label class="manual-label" for="manual-password">Password (optional, never stored)</label><input id="manual-password" type="password" class="manual-input" autocomplete="current-password" placeholder="password"></div>`,
    '<div class="cookie-modal-actions"><button type="button" class="cookie-btn-back" id="manual-single-back">Back</button><button type="button" class="cookie-btn-submit active" id="manual-single-start">Start</button></div>',
  );
  (($("manual-single-back").onclick = openManualChoice),
    ($("manual-single-start").onclick = startManualSingle));
}
async function startManualSingle() {
  const u = $("manual-username")?.value || "",
    p = $("manual-password")?.value || "";
  try {
    const r = await api("/manual-login/single", "POST", {
      username: u,
      password: p,
    });
    openManualProgress(r.session_id);
  } catch (e) {
    toast(e.message || "Manual login failed");
  }
}
function openManualMulti() {
  modal(
    "Manual Login - Multi",
    `<div class="cookie-import-container"><div class="cookie-meta-row"><span class="cookie-detected-count">user:pass per line</span></div><div class="cookie-textarea-wrap"><textarea id="manual-multi-lines" class="cookie-textarea" placeholder="user1:pass1\nuser2:pass2\nuser3:pass3"></textarea></div><div class="cookie-meta-row"><span id="manual-multi-count" class="cookie-detected-count">0 accounts detected</span></div></div>`,
    '<div class="cookie-modal-actions"><button type="button" class="cookie-btn-back" id="manual-multi-back">Back</button><button type="button" class="cookie-btn-submit" id="manual-multi-start" disabled>Start</button></div>',
  );
  const ta = $("manual-multi-lines"),
    cnt = $("manual-multi-count"),
    btn = $("manual-multi-start");
  function upd() {
    const n = ta.value
      .split(/\r?\n/)
      .map((e) => e.trim())
      .filter((e) => e && e.includes(":") && !e.startsWith(":")).length;
    cnt && (cnt.textContent = `${n} account${n === 1 ? "" : "s"} detected`);
    if (btn) {
      btn.disabled = n === 0;
      btn.classList.toggle("active", n > 0);
    }
  }
  (ta && ((ta.oninput = upd), ta.focus()),
    ($("manual-multi-back").onclick = openManualChoice),
    ($("manual-multi-start").onclick = startManualMulti),
    upd());
}
async function startManualMulti() {
  const ta = $("manual-multi-lines");
  if (!ta) return;
  try {
    const r = await api("/manual-login/multi", "POST", { lines: ta.value });
    openManualProgress(r.session_id);
  } catch (e) {
    toast(e.message || "Manual login failed");
  }
}
function stopManualPoll() {
  if (manualPollTimer) {
    clearTimeout(manualPollTimer);
    manualPollTimer = 0;
  }
}
function manualProgressRow(entry) {
  const detail = entry.detail
    ? `<div class="manual-progress-detail">${esc(entry.detail)}</div>`
    : "";
  return `<div class="manual-progress-row"><span class="manual-progress-user">${esc(entry.username || "Waiting...")}</span><span class="manual-pill manual-${esc(entry.status)}">${esc(entry.status)}</span></div>${detail}`;
}
function renderManualProgressCount(s) {
  const label = $("manual-progress-count");
  if (!label) return;
  label.textContent =
    `${s.done || 0} done / ${s.failed || 0} failed / ${s.total || 0} total` +
    (s.note ? " - " + s.note : "");
}
function renderManualProgress(s) {
  const list = $("manual-progress-list");
  if (!list) return;
  const items = s.entries || [];
  list.innerHTML = items.length
    ? items.map(manualProgressRow).join("")
    : '<div class="cookie-detected-empty">Opening popup browser...</div>';
  renderManualProgressCount(s);
}
const MANUAL_FINISHED_STATES = ["done", "cancelled", "error"];
async function closeManualProgress(sid) {
  try {
    await api(
      "/manual-login/" + encodeURIComponent(sid) + "/cancel",
      "POST",
      {},
    );
  } catch (_) {}
  stopManualPoll();
  closeModal();
}
async function skipManualProgress(sid) {
  try {
    await api(
      "/manual-login/" + encodeURIComponent(sid) + "/skip",
      "POST",
      {},
    );
    toast("Skipped to next account");
  } catch (e) {
    toast(e.message || "Skip failed");
  }
}
async function finishManualProgress(s) {
  stopManualPoll();
  const closeBtn = $("manual-progress-close");
  if (closeBtn) closeBtn.textContent = "Close";
  try {
    await loadAccountsConfig(!0);
    await manualSnapshot();
  } catch (_) {}
  if (s.state === "done")
    toast(`Manual Login finished (${s.done || 0}/${s.total || 0} imported)`);
  else if (s.state === "error") toast(s.note || "Manual login failed");
}
async function pollManualProgress(sid) {
  if (!$("manual-progress-list")) {
    stopManualPoll();
    return;
  }
  try {
    const s = await api("/manual-login/" + encodeURIComponent(sid), "GET");
    renderManualProgress(s);
    if (MANUAL_FINISHED_STATES.includes(s.state)) {
      await finishManualProgress(s);
      return;
    }
    manualPollTimer = setTimeout(() => pollManualProgress(sid), 2000);
  } catch (e) {
    manualPollTimer = setTimeout(() => pollManualProgress(sid), 3000);
  }
}
async function openManualProgress(sid) {
  stopManualPoll();
  modal(
    "Manual Login",
    `<div class="cookie-import-container"><div class="manual-progress-list" id="manual-progress-list"><div class="cookie-detected-empty">Opening popup browser...</div></div><div class="cookie-meta-row"><span id="manual-progress-count" class="cookie-detected-count"></span></div></div>`,
    '<div class="cookie-modal-actions"><button type="button" class="cookie-btn-back" id="manual-progress-skip">Skip</button><button type="button" class="cookie-btn-back" id="manual-progress-close">Cancel</button></div>',
  );
  const closeBtn = $("manual-progress-close");
  if (closeBtn) closeBtn.onclick = () => closeManualProgress(sid);
  const skipBtn = $("manual-progress-skip");
  if (skipBtn) skipBtn.onclick = () => skipManualProgress(sid);
  manualPollTimer = setTimeout(() => pollManualProgress(sid), 500);
}
const COOKIE_RE = /_\|WARNING[^\s'"]+/;
function extractCookie(line) {
  const text = String(line || "").trim();
  if (!text) return "";
  const match = text.match(COOKIE_RE);
  if (match?.[0]) return match[0];
  if (text.length > 80 && !text.includes(" ")) return text;
  const parts = text.split(":");
  for (let index = parts.length - 1; index >= 0; index--) {
    if (parts[index].includes("_|WARNING"))
      return parts.slice(index).join(":").trim();
  }
  return text.includes("_|WARNING") ? text : "";
}
function cookiePrefix(line, cookie) {
  if (!cookie || !line.includes(cookie)) return "";
  return line
    .slice(0, line.indexOf(cookie))
    .replace(/^:+|:+$/g, "")
    .trim()
    .replace(/:+$/, "");
}
function cookieImportItems(value) {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line, index) => {
      const cookie = extractCookie(line);
      if (!cookie) return null;
      const prefix = cookiePrefix(line, cookie);
      const username = prefix
        ? prefix.split(":")[0].trim()
        : !line.includes("_|WARNING") && line.includes(":")
          ? line.split(":")[0].trim()
          : "";
      const password = prefix.includes(":")
        ? prefix.split(":").slice(1).join(":").trim()
        : "";
      return { line, cookie, hint: username, pass: password, index };
    })
    .filter(Boolean);
}
const COOKIE_STATE_LABELS = [
  ["suspended", "Suspended"],
  ["banned", "Banned"],
  ["locked", "Locked"],
  ["recovery", "Recovery"],
  ["captcha", "Captcha"],
  ["bad_password", "Wrong password"],
  ["refreshed", "OK"],
];
function cookieStateLabel(result) {
  const hit = COOKIE_STATE_LABELS.find(([key]) => result[key]);
  return hit ? hit[1] : "";
}
function cookiePreviewLabel(result, hint) {
  const username =
    result.username ||
    hint ||
    (result.banned || result.suspended
      ? "Unknown"
      : "Unknown — add username: prefix");
  const state = cookieStateLabel(result);
  return state ? `${username} (${state})` : username;
}
let _cookieSeq = 0;
function syncCookieDetectedChrome(countLabel, addBtn, count) {
  if (countLabel)
    countLabel.textContent = `${count} account${count === 1 ? "" : "s"} detected`;
  if (addBtn) {
    addBtn.disabled = count === 0;
    addBtn.classList.toggle("active", count > 0);
  }
}
function cookieDetectedRowHtml(item) {
  return `<div class="cookie-detected-item"><span class="cookie-detected-user">${esc(item.hint || "Checking...")}</span><span class="cookie-detected-id">...</span></div>`;
}
function cookieRowNodes(listEl, index) {
  const row = listEl.children[index];
  if (!row) return null;
  return {
    user: row.querySelector(".cookie-detected-user"),
    id: row.querySelector(".cookie-detected-id"),
  };
}
function showCookieCheckedRow(listEl, index, result, item) {
  const row = cookieRowNodes(listEl, index);
  if (!row) return;
  if (row.user) row.user.textContent = cookiePreviewLabel(result, item.hint);
  if (row.id) row.id.textContent = result.user_id || "";
}
function showCookieInvalidRow(listEl, index, item) {
  const row = cookieRowNodes(listEl, index);
  if (!row) return;
  if (row.user) row.user.textContent = item.hint || "Invalid";
  if (row.id) row.id.textContent = "\u2014";
}
async function checkCookieItems(items, listEl, seq) {
  for (let i = 0; i < items.length; i++) {
    if (seq !== _cookieSeq) return;
    const item = items[i];
    try {
      const result = await api("/test-cookie", "POST", {
        cookie: item.cookie,
        username: item.hint || "",
        password: item.pass || "",
      });
      if (seq !== _cookieSeq) return;
      showCookieCheckedRow(listEl, i, result, item);
    } catch (e) {
      if (seq !== _cookieSeq) return;
      showCookieInvalidRow(listEl, i, item);
    }
  }
}
async function refreshCookieDetection() {
  const ta = $("cookie-lines");
  if (!ta) return;
  const items = cookieImportItems(ta.value);
  const countLabel = $("cookie-detected-count");
  const listEl = $("cookie-detected-list");
  const addBtn = $("cookie-add");
  syncCookieDetectedChrome(countLabel, addBtn, items.length);
  if (!listEl) return;
  if (!items.length) {
    listEl.innerHTML =
      '<div class="cookie-detected-empty">No accounts detected yet</div>';
    return;
  }
  const seq = ++_cookieSeq;
  listEl.innerHTML = items.map(cookieDetectedRowHtml).join("");
  await checkCookieItems(items, listEl, seq);
}
function openCookie() {
  const p =
    "user1:pass1:_|WARNING...\nuser2:pass2:_|WARNING...\n_|WARNING:-cookie-only-...";
  modal(
    "Import Cookies",
    `<div class="cookie-import-container"><div class="cookie-hint-banner">Accepts <strong>user:pass:cookie</strong> or <strong>cookie only</strong> (one per line). Cookie-only works, but a dead cookie cannot be matched to a username — prefix with <strong>username:</strong> so the ban check can run. A dead cookie pasted as <strong>user:pass:cookie</strong> also runs a live login check (Locked / Recovery / Captcha / Wrong password).</div><div class="cookie-textarea-wrap"><textarea id="cookie-lines" class="cookie-textarea" placeholder="${esc(p)}"></textarea></div><div class="cookie-detected-section"><div class="cookie-detected-label">DETECTED</div><div id="cookie-detected-list" class="cookie-detected-list"><div class="cookie-detected-empty">No accounts detected yet</div></div></div><div class="cookie-meta-row"><span id="cookie-detected-count" class="cookie-detected-count">0 accounts detected</span></div></div>`,
    '<div class="cookie-modal-actions"><button type="button" class="cookie-btn-back" id="cookie-cancel">Back</button><button type="button" class="cookie-btn-submit" id="cookie-add" disabled>Add Accounts</button></div>',
  );
  const ta = $("cookie-lines");
  if (ta) {
    ta.oninput = refreshCookieDetection;
    ta.focus();
  }
  $("cookie-cancel").onclick = openAddChoice;
  $("cookie-add").onclick = submitCookies;
  refreshCookieDetection();
}
async function submitCookies() {
  const ta = $("cookie-lines");
  if (!ta) return;
  const e = ta.value
    .split(/\r?\n/)
    .map((e) => e.trim())
    .filter(Boolean);
  if (!e.length) return toast("No cookies entered");
  try {
    const t = await api("/accounts/import", "POST", {
      kind: "cookies",
      lines: e,
    });
    const errs = Array.isArray(t.errors) ? t.errors.filter(Boolean) : [];
    (toast(
      (errs.length ? errs.slice(0, 2).join(" | ") + " | " : "") +
        `Imported (${t.imported || 0})`,
    ),
      closeModal(),
      await loadAccountsConfig(!0),
      await manualSnapshot());
  } catch (err) {
    toast(err.message || "Import failed");
  }
}
async function saveGame() {
  return saveGamePanel(panelCtx());
}
async function saveQueue() {
  return saveQueuePanel(panelCtx());
}
async function saveGraphics() {
  GRAPHICS = await saveGraphicsPanel(panelCtx());
}
async function saveWindowSize() {
  WINDOW_SIZE = await saveWindowSizePanel(panelCtx());
}
async function rearrangeWindowSize() {
  WINDOW_SIZE = await rearrangeWindowSizePanel(panelCtx());
}
function connectStream() {
  return statusRuntime.connectStream();
}
(initStatusHover(),
  applySolarStaticIcons(document),
  bindDashboardEvents({
    $: $,
    document: document,
    syncToggleLabels: syncToggleLabels,
    setView: setView,
    toggleGuard: toggleGuard,
    onGuardPress: onGuardPress,
    openAdd: openAdd,
    reloadCookies: reloadCookies,
    closeAllRoblox: closeAllRoblox,
    closeModal: closeModal,
    renderAccountsTable: renderAccountsTable,
    setAccountFilter: setAccountFilter,
    resetGameSettings: resetGameSettings,
    saveGame: saveGame,
    resetLimiterSettings: resetLimiterSettings,
    saveLimiter: saveLimiter,
    resetRamViewSettings: resetRamViewSettings,
    saveRamView: saveRamView,
    renderRamCleanup: renderRamCleanup,
    renderVirtualMemory: renderVirtualMemory,
    saveRamCleanup: saveRamCleanup,
    applyVirtualMemory: applyVirtualMemory,
    revertVirtualMemory: revertVirtualMemory,
    cleanNowRam: cleanNowRam,
    loadRamCleanup: loadRamCleanup,
    loadVirtualMemory: loadVirtualMemory,
    getTotalRamGb: getTotalRamGb,
    resetGraphicsSettings: resetGraphicsSettings,
    saveGraphics: saveGraphics,
    resetWindowSizeSettings: resetWindowSizeSettings,
    saveWindowSize: saveWindowSize,
    rearrangeWindowSize: rearrangeWindowSize,
    pickWindowSizeGrid: pickWindowSizeGrid,
    resetQueueSettings: resetQueueSettings,
    saveQueue: saveQueue,
    saveCronusSettings: saveCronusSettings,
    resetCronusSettings: resetCronusSettings,
    saveDeveloperSettings: saveDeveloperSettings,
    resetDeveloperSettings: resetDeveloperSettings,
    renderDeveloper: renderDeveloper,
    robloxInstallConfirm: robloxInstallConfirm,
    copyDeveloperLua: copyDeveloperLua,
    saveDeveloperLua: saveDeveloperLua,
    queueDeveloperLuaSave: queueDeveloperLuaSave,
    markDirty: markDirty,
    renderSettings: renderSettings,
    renderPerformance: renderPerformance,
    renderRam: renderRam,
    renderGraphics: renderGraphics,
    renderWindowSize: renderWindowSize,
    renderCpuLimiter: renderCpuLimiter,
    gameLookupTimer: () => gameLookupTimer,
    setGameLookupTimer: (e) => {
      gameLookupTimer = e;
    },
    renderGameMap: renderGameMap,
    loadGamePlace: loadGamePlace,
    placeFromPrivate: placeFromPrivate,
    deleteAccount: deleteAccount,
    deleteSelectedAccounts: deleteSelectedAccounts,
    toggleFinishedAccounts: toggleFinishedAccounts,
    unbanMarkSelected: unbanMarkSelected,
    unbanMarkAccount: unbanMarkAccount,
    launchAccount: launchAccount,
    dedupeAccount: dedupeAccount,
    focusCaptcha: focusCaptcha,
    openCaptchaLogin: openCaptchaLogin,
    resumeCaptcha: resumeCaptcha,
    resumeSelected: resumeSelected,
    selectUser: (e) => {
      if (!e) return;
      const k = String(e);
      if (selectedUsers.has(k)) selectedUsers.delete(k);
      else selectedUsers.add(k);
      saveSelectedUsers();
      updateDeleteSelectedBtn();
      updateFinishedBtn();
      updateUnbanBtn();
      updateResumeBtn();
    },
    saveDescription: saveDescription,
    startEditDescription: (e) => {
      ((EDITING_DESC_USER = e), renderAccountsTable());
    },
    cancelEditDescription: () => {
      ((EDITING_DESC_USER = ""), renderAccountsTable());
    },
    toast: toast,
  }),
  document.addEventListener("cronus:games-changed", () => {
    try {
      Object.keys(GAME_PLACE_CACHE).forEach((k) => delete GAME_PLACE_CACHE[k]);
      loadAccountsConfig(!0)
        .then(() => renderAccountsTable())
        .catch(() => {
          /* intentional: no-op */
        });
    } catch {}
  }),
  (async function () {
    (setView(VIEW),
      loadDeveloperLua().catch(() => {
        /* intentional: no-op */
      }),
      await loadConfig(),
      await loadPerformance(),
      await loadRamCleanup(),
      await loadVirtualMemory(),
      await loadGraphics(),
      await loadWindowSize(),
      await loadCpuLimiter(),
      await loadTroubleshoot(),
      await loadAccountsConfig(!0),
      connectStream(),
      setInterval(() => {
        if (!document.hidden) renderRunningClock();
      }, 1e3),
      setInterval(() => {
        if (!document.hidden && "troubleshoot" === VIEW) loadTroubleshoot();
        if (!document.hidden && "limiter" === VIEW) loadCpuLimiter();
        if (!document.hidden && "ram" === VIEW) loadRamCleanup();
      }, 3500));
  })());
