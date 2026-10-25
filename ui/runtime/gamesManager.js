// Game library UI: the pool card in Settings, the per-row game picker and
// the bulk "Set game" bar. Loaded as a module (<script type="module"> in
// desktop/ui_dashboard.py) so the state below stays private to this file.

let GAMES = [];
let GAME_MODE = "shared"; // GAME card switch; pool UI only lives in per-account
let GAME_MODE_PREVIEW = "";
let ACCOUNT_GAMES = {}; // lower(username) -> game_id
let PLACE_NAMES = {}; // place_id -> name
let PLACE_THUMBS = {}; // place_id -> image_url
let scheduled = false;
let pop = null; // game picker popover element
let popUser = ""; // lower(username) the popover is open for
let globalWired = false;

// ── Small helpers ───────────────────────────────────────────────────

// Signature of the games list. Every render helper compares against it
// and writes to the DOM ONLY when something actually changed — otherwise
// the MutationObserver below would ping-pong on our own writes forever
// and freeze the page.
function gamesSig() {
  return GAMES.map((g) =>
    [
      g.id,
      g.name,
      g.place_id,
      g.private_server_url,
      !!g.auto_create_private_server_enabled,
    ].join("|"),
  ).join(";");
}

function esc(value) {
  const node = document.createElement("span");
  node.textContent = String(value || "");
  return node.innerHTML;
}

function apiHeaders(json) {
  const headers = {};
  const token =
    document.querySelector('meta[name="cronus-api-token"]')?.content || "";
  if (token) headers["X-Cronus-Token"] = token;
  if (json) headers["Content-Type"] = "application/json";
  return headers;
}

async function api(path, method, body) {
  const response = await fetch("/api" + path, {
    method: method || "GET",
    headers: apiHeaders(body !== undefined),
    cache: "no-store",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new Error(
      payload.detail || payload.msg || response.statusText || "Request failed",
    );
  return payload;
}

// ── Toasts ──────────────────────────────────────────────────────────

// Project-style notifications: same stacked .toast-item cards as
// components/feedback.js (success/warning/error/info + icons, max 4, 3.2s).
const TOAST_ICONS = {
  success:
    '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#22c55e"/><path d="M8.2 12.2l2.6 2.6 4.5-5" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  warning:
    '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#f59e0b"/><path d="M12 7.5v5.2" stroke="white" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="16.2" r="1.2" fill="white"/></svg>',
  error:
    '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#ef4444"/><path d="M15 9l-6 6M9 9l6 6" stroke="white" stroke-width="2" stroke-linecap="round"/></svg>',
  info: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#3b82f6"/><path d="M12 11v5" stroke="white" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="8" r="1.2" fill="white"/></svg>',
};

const TOAST_ERROR_RE =
  /error|failed|invalid|cannot|denied|missing|not found|required|0 valid/i;
const FARM_START_WARNINGS = ["blocked", "skipped", "unavailable"];
const WARNING_PHRASES = ["unsaved", "save changes"];

function includesAny(text, needles) {
  return needles.some((needle) => text.includes(needle));
}

function isBlockedLaunchable(text) {
  return text.includes("blocked") && text.includes("launchable");
}

function farmStartedKind(text) {
  return includesAny(text, FARM_START_WARNINGS) ? "warning" : "success";
}

function pickToastKind(message) {
  const text = String(message || "").toLowerCase();
  if (text.includes("farm started")) return farmStartedKind(text);
  if (includesAny(text, WARNING_PHRASES)) return "warning";
  if (TOAST_ERROR_RE.test(text))
    return isBlockedLaunchable(text) ? "warning" : "error";
  return isBlockedLaunchable(text) ? "warning" : "success";
}

function dropOldestToast(box) {
  while (box.children.length >= 4) {
    if (!box.firstElementChild) break;
    box.firstElementChild.remove();
  }
}

function toastMarkup(message, kind) {
  const icon = TOAST_ICONS[kind] || TOAST_ICONS.info;
  return `<span class="toast-icon">${icon}</span><span class="toast-text">${esc(message)}</span>`;
}

function dismissToast(node) {
  node.classList.remove("show");
  node.classList.add("hide");
  setTimeout(() => node.remove(), 200);
}

function toast(message, type) {
  const box = document.querySelector("#toast");
  if (!box) return;
  const kind = type || pickToastKind(message);
  dropOldestToast(box);
  const node = document.createElement("div");
  node.className = "toast-item toast-" + kind;
  node.innerHTML = toastMarkup(message, kind);
  box.appendChild(node);
  requestAnimationFrame(() => node.classList.add("show"));
  setTimeout(() => dismissToast(node), 3200);
}

// ── Games list + place metadata ─────────────────────────────────────

function gameById(id) {
  const wanted = String(id || "")
    .trim()
    .toLowerCase();
  return (
    GAMES.find(
      (g) =>
        String(g.id || "")
          .trim()
          .toLowerCase() === wanted,
    ) || null
  );
}

async function refreshGames(retry) {
  try {
    const data = await api("/games");
    GAMES = Array.isArray(data.games) ? data.games : [];
    setGameMode(GAME_MODE_PREVIEW || data.game_mode, !!GAME_MODE_PREVIEW);
  } catch (_) {
    // Keep the last good list: a single failed tick must not wipe the UI.
    if (!retry) setTimeout(() => refreshGames(true), 4000);
    return;
  }
  renderGamesCard();
  refreshRowBadges();
}

function setGameMode(mode, preview = false) {
  const next =
    String(mode || "")
      .trim()
      .toLowerCase()
      .replace("-", "_") === "per_account"
      ? "per_account"
      : "shared";
  GAME_MODE_PREVIEW = preview ? next : "";
  GAME_MODE = next;
  applyGameMode();
}

function setHidden(node, hidden) {
  if (node) node.hidden = hidden;
}

// Shared mode: the pool is ignored, so its UI goes away entirely —
// pool row inside the Game card hidden, bulk button removed, row picker locked.
function applyGameMode() {
  try {
    document.body.dataset.gameMode = GAME_MODE;
  } catch (_) {}
  if (GAME_MODE !== "per_account") {
    closePop();
    setHidden(document.querySelector("#game-pool-row"), true);
    document.querySelector("#cronus-bulk-game")?.remove();
    return;
  }
  setHidden(document.querySelector("#game-pool-row"), false);
  ensureBulkBar();
}

function accountGameMap(list) {
  const next = {};
  list.forEach((acc) => {
    const user = String(acc.username || "")
      .trim()
      .toLowerCase();
    if (user) next[user] = String(acc.game_id || "");
  });
  return next;
}

async function refreshAccountGames(retry) {
  try {
    const data = await api("/accounts");
    ACCOUNT_GAMES = accountGameMap(Array.isArray(data) ? data : []);
  } catch (_) {
    // Keep the last good map; retry soon instead of waiting a full tick.
    if (!retry) setTimeout(() => refreshAccountGames(true), 4000);
    return;
  }
  refreshRowBadges();
}

function cachedPlace(pid) {
  return { name: PLACE_NAMES[pid], image_url: PLACE_THUMBS[pid] || "" };
}

async function placeInfo(placeId) {
  const pid = String(placeId || "").trim();
  if (!pid) return { name: "", image_url: "" };
  if (PLACE_NAMES[pid] !== undefined) return cachedPlace(pid);
  PLACE_NAMES[pid] = "";
  PLACE_THUMBS[pid] = "";
  try {
    const info = await api("/game/place/" + encodeURIComponent(pid));
    PLACE_NAMES[pid] = String(info.name || "");
    PLACE_THUMBS[pid] = String(info.image_url || "");
  } catch (_) {
    PLACE_NAMES[pid] = "";
    PLACE_THUMBS[pid] = "";
  }
  return { name: PLACE_NAMES[pid], image_url: PLACE_THUMBS[pid] };
}

// ── Game pool inside the Game card (screenshot layout) ──────────────
// Collection card removed by design: pool lives in #game-pool-list.
function notifyGamesChanged() {
  try {
    document.dispatchEvent(new CustomEvent("cronus:games-changed"));
  } catch (_) {}
}

function removeLegacyCollectionCard() {
  [
    "#cronus-games-panel",
    "#cronus-games-card",
    "#cronus-game-modal-overlay",
  ].forEach((sel) => {
    try {
      document.querySelector(sel)?.remove();
    } catch (_) {}
  });
}

function ensurePoolWiring() {
  const input = document.querySelector("#game-pool-input");
  const btn = document.querySelector("#game-pool-add-btn");
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = "1";
    btn.addEventListener("click", onAddPoolGame);
  }
  if (input && !input.dataset.wired) {
    input.dataset.wired = "1";
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        onAddPoolGame();
      }
    });
  }
}

// Fallback inline styles: external queue-card.css may be cached stale
// (dashboard.css version pin). These critical rules guarantee the
// screenshot layout even then; external CSS only refines it.
function ensurePoolStyles() {
  if (document.querySelector("#game-pool-styles")) return;
  const st = document.createElement("style");
  st.id = "game-pool-styles";
  st.textContent = [
    "#game-pool-box{border:1px solid #23252d;border-radius:12px;background:#14151a;padding:16px;display:grid;gap:12px}",
    "#game-pool-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}",
    "#game-pool-title{font-size:11px;font-weight:800;letter-spacing:.06em;color:#8d9099}",
    "#game-pool-add{display:flex;align-items:center;gap:8px;margin-left:auto}",
    "#game-pool-input{width:200px;max-width:40vw}",
    "#game-pool-add-btn{white-space:nowrap;height:33px}",
    "#game-pool-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px}",
    ".game-pool-empty{font-size:12.5px;color:#787774}",
    ".game-pool-item{display:flex;align-items:center;gap:12px;border:1px solid #23252d;border-radius:10px;background:#101116;padding:10px 12px}",
    ".game-pool-thumb{width:48px;height:48px;flex:none;border-radius:8px;overflow:hidden;background:#0b0c10;border:1px solid #22242b;display:grid;place-items:center;font-weight:800;color:#787774}",
    ".game-pool-thumb img{width:100%;height:100%;object-fit:cover;display:block}",
    ".game-pool-copy{min-width:0;flex:1;display:grid;gap:2px}",
    ".game-pool-name{font-size:13.5px;font-weight:700;color:#f2f3f5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
    ".game-pool-sub{font-family:monospace;font-size:11.5px;color:#787774;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
    ".game-pool-del{flex:none;width:30px;height:30px;display:grid;place-items:center;background:transparent;border:1px solid #2a2d36;border-radius:6px;color:#9aa0ab;cursor:pointer;padding:0}",
    ".game-pool-del svg{width:15px;height:15px}",
    ".game-pool-del.armed{background:#4c1d24;border-color:#f43f5e;color:#fda4af}",
    "#cronus-games-panel,#cronus-games-card{display:none!important}",
    ".cronus-game-pop{padding:10px}",
    ".cronus-game-pop .cronus-gpop-head{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:2px 4px 8px;font-size:12px;font-weight:700;color:#f2f3f5}",
    ".cronus-game-pop .cronus-gpop-head button{background:none;border:none;color:#8d9099;cursor:pointer;font-size:14px;line-height:1;padding:2px 6px}",
    ".cronus-gitem{display:grid;grid-template-columns:40px minmax(0,1fr) auto;gap:10px;align-items:center;width:100%;text-align:left;background:#101116;border:1px solid #1f2128;border-radius:10px;padding:8px;margin-bottom:6px;cursor:pointer;color:#f2f3f5}",
    ".cronus-gitem.current{border-color:#1f6feb}",
    ".cronus-gitem .ogame-thumb{width:40px;height:40px;font-size:14px;flex:none}",
    ".cronus-gitem img{width:40px;height:40px;border-radius:8px;object-fit:cover;background:#0b0c10;border:1px solid #22242b;flex:none}",
    ".cronus-gitem .t{min-width:0}",
    ".cronus-gitem .n{font-size:13px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
    ".cronus-gitem .p{font-size:11px;color:#7f838c;font-family:monospace}",
    ".cronus-gitem .tick{color:#4ade80;font-weight:800;padding:0 4px}",
    ".cronus-bulkbar{display:flex;gap:8px;align-items:center;min-width:0}",
    "#cronus-bulk-btn{height:33px;white-space:nowrap;flex:none;display:inline-flex;align-items:center;gap:7px}",
    "#cronus-bulk-btn .btn-icon{width:14px;height:14px;flex:0 0 14px}",
  ].join("\n");
  document.head.appendChild(st);
}

function poolDisplayName(game) {
  const pid = String(game?.place_id || "").trim();
  return (
    String(PLACE_NAMES[pid] || "").trim() ||
    String(game?.name || "").trim() ||
    (pid ? `Place ${pid}` : "New game")
  );
}

function poolItemName(game, pid) {
  const placeName = String(PLACE_NAMES[pid] || "").trim();
  if (placeName) return placeName;
  const ownName = String(game.name || "").trim();
  return ownName || (pid ? `Place ${pid}` : "Game");
}

function poolThumbHtml(thumb, name) {
  if (thumb) {
    return `<img src="${esc(thumb)}" alt="" loading="lazy" onerror="this.remove()">`;
  }
  return `<span class="game-pool-glyph" aria-hidden="true">${esc(name.trim().charAt(0).toUpperCase() || "?")}</span>`;
}

function applyPoolName(nameNode, name) {
  const freshName = String(name || "").trim();
  if (!freshName || !nameNode || nameNode.textContent === freshName) return;
  nameNode.textContent = freshName;
}

function applyPoolThumb(thumb, imageUrl) {
  if (!imageUrl || !thumb) return;
  const image = thumb.querySelector("img");
  if (!image)
    thumb.innerHTML = `<img src="${esc(imageUrl)}" alt="" loading="lazy" onerror="this.remove()">`;
  else if (image.getAttribute("src") !== imageUrl) image.src = imageUrl;
}

function updateGamePoolMetadata(item, placeId) {
  if (!placeId) return;
  placeInfo(placeId).then(({ name, image_url }) => {
    if (!document.contains(item)) return;
    applyPoolName(item.querySelector('[data-role="name"]'), name);
    applyPoolThumb(item.querySelector('[data-role="thumb"]'), image_url);
  });
}

function createPoolItem(game) {
  const gid = String(game.id || "");
  const pid = String(game.place_id || "").trim();
  const item = document.createElement("div");
  item.className = "game-pool-item";
  item.dataset.gameId = gid;
  const name = poolItemName(game, pid);
  const img = poolThumbHtml(PLACE_THUMBS[pid] || "", name);
  item.innerHTML = `
        <span class="game-pool-thumb" data-role="thumb">${img}</span>
        <span class="game-pool-copy"><span class="game-pool-name" data-role="name">${esc(name)}</span><span class="game-pool-sub" data-role="sub">${esc(pid || "No place set")}</span></span>
        <button type="button" class="game-pool-del" data-action="delete" title="Delete game"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg></button>`;
  item
    .querySelector('[data-action="delete"]')
    .addEventListener("click", (e) => {
      e.stopPropagation();
      onDeletePoolGame(gid, item);
    });
  return item;
}

function renderPoolItems(list) {
  GAMES.forEach((game) => {
    const item = createPoolItem(game);
    list.appendChild(item);
    updateGamePoolMetadata(item, String(game.place_id || "").trim());
  });
}

function renderEmptyPool(list) {
  const empty = document.createElement("div");
  empty.className = "game-pool-empty";
  empty.textContent = "No games yet — add a Place ID above.";
  list.appendChild(empty);
  refreshAssignCounts();
}

function renderGamesCard() {
  removeLegacyCollectionCard();
  ensurePoolStyles();
  ensurePoolWiring();
  applyGameMode();
  const list = document.querySelector("#game-pool-list");
  if (!list) return;
  const sig = gamesSig();
  if (list.dataset.sig === sig) {
    refreshAssignCounts();
    return;
  }
  list.dataset.sig = sig;
  list.innerHTML = "";
  if (!GAMES.length) {
    renderEmptyPool(list);
    return;
  }
  renderPoolItems(list);
  refreshAssignCounts();
}

// ── Pool add / delete ───────────────────────────────────────────────

function poolAddRejection(pid) {
  if (!pid)
    return { message: "Enter a Place ID first", kind: "info", focus: true };
  if (!/^\d+$/.test(pid))
    return { message: "Place ID must be numeric", kind: "error", focus: true };
  const duplicate = GAMES.some((g) => String(g.place_id || "").trim() === pid);
  if (duplicate) return { message: "Place ID already in pool", kind: "info" };
  return null;
}

async function addPoolGame(pid, btn, input) {
  try {
    if (btn) btn.disabled = true;
    const autoName = String(PLACE_NAMES[pid] || "").trim() || `Place ${pid}`;
    const payload = await api("/games", "POST", {
      game: { place_id: pid, name: autoName },
    });
    GAMES = Array.isArray(payload.games) ? payload.games : GAMES;
    if (input) input.value = "";
    const list = document.querySelector("#game-pool-list");
    if (list) delete list.dataset.sig;
    renderGamesCard();
    refreshAccountGames();
    notifyGamesChanged();
    toast("Game added");
  } catch (error) {
    toast(error.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function requirePoolPlaceId() {
  const input = document.querySelector("#game-pool-input");
  const btn = document.querySelector("#game-pool-add-btn");
  const pid = String(input?.value || "").trim();
  const rejection = poolAddRejection(pid);
  if (!rejection) return { pid, btn, input };
  toast(rejection.message, rejection.kind);
  if (rejection.focus) input?.focus?.();
  return null;
}

async function onAddPoolGame() {
  const pending = requirePoolPlaceId();
  if (!pending) return;
  await addPoolGame(pending.pid, pending.btn, pending.input);
}

async function deletePoolGame(gid) {
  try {
    const payload = await api("/games/delete", "POST", {
      id: gid,
      force: true,
    });
    GAMES = Array.isArray(payload.games) ? payload.games : GAMES;
    const list = document.querySelector("#game-pool-list");
    if (list) delete list.dataset.sig;
    renderGamesCard();
    refreshAccountGames();
    notifyGamesChanged();
    toast("Game deleted");
  } catch (error) {
    toast(error.message);
  }
}

function armPoolDelete(rowEl, btn, game) {
  rowEl.dataset.armed = "1";
  if (btn) btn.classList.add("armed");
  toast(`Click trash again to delete "${poolDisplayName(game)}"`, "info");
  clearTimeout(rowEl._armTimer);
  rowEl._armTimer = setTimeout(() => {
    delete rowEl.dataset.armed;
    if (btn && document.contains(btn)) btn.classList.remove("armed");
  }, 4000);
}

async function onDeletePoolGame(id, rowEl) {
  const gid = String(id || "").trim();
  if (!gid) return;
  const game = gameById(gid);
  const btn = rowEl?.querySelector?.('[data-action="delete"]');
  if (rowEl && !rowEl.dataset.armed) {
    armPoolDelete(rowEl, btn, game);
    return;
  }
  await deletePoolGame(gid);
}

// ── Bulk game picker: one click applies to ALL accounts ─────────────
// No row selection needed. The button opens the game list and the
// picked game is assigned to every account via /accounts/assign-game.
const BULK_ICON_SVG = `<svg class="btn-icon" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="m23 5h-1c-.552 0-1 .448-1 1v5c0 .551-.449 1-1 1h-1v-5c0-2.757-2.243-5-5-5h-9c-2.757 0-5 2.243-5 5v10c0 2.757 2.243 5 5 5h9c2.757 0 5-2.243 5-5v-3h1c1.654 0 3-1.346 3-3v-4c.552 0 1-.448 1-1s-.448-1-1-1zm-6 12c0 1.654-1.346 3-3 3h-9c-1.654 0-3-1.346-3-3v-10c0-1.654 1.346-3 3-3h9c1.654 0 3 1.346 3 3zm-7.5-1.5c0 .553-.448 1-1 1h-1v1c0 .553-.448 1-1 1s-1-.447-1-1v-1h-1c-.552 0-1-.447-1-1s.448-1 1-1h1v-1c0-.552.448-1 1-1s1 .448 1 1v1h1c.552 0 1 .447 1 1zm5.5 0c0 .828-.672 1.5-1.5 1.5s-1.5-.672-1.5-1.5.672-1.5 1.5-1.5 1.5.672 1.5 1.5zm-1.5-9.5h-8c-.827 0-1.5.673-1.5 1.5v2c0 .827.673 1.5 1.5 1.5h8c.827 0 1.5-.673 1.5-1.5v-2c0-.827-.673-1.5-1.5-1.5zm-.5 3h-7v-1h7z"/></svg>`;

function allUsernames() {
  // Union DOM rows + ACCOUNT_GAMES map: filter/search may hide rows
  // from the DOM, but "all" must still cover every account.
  // Dedupe case-insensitively, preferring the DOM's original casing.
  const fromRows = Array.from(
    document.querySelectorAll("#accounts-table tr[data-user]"),
  )
    .map((row) => String(row.dataset.user || "").trim())
    .filter(Boolean);
  const fromMap = Object.keys(ACCOUNT_GAMES || {})
    .map((u) => String(u || "").trim())
    .filter(Boolean);
  const seen = new Set();
  const out = [];
  for (const u of fromRows.concat(fromMap)) {
    const key = u.toLowerCase();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(u);
  }
  return out;
}

function bulkButtonHtml() {
  return `${BULK_ICON_SVG}<span>Set game</span>`;
}

function ensureBulkBar() {
  // Shared mode has no per-account assignment: never (re)create the
  // button here. The MutationObserver calls decorateRows() after every
  // DOM write, so without this gate it would resurrect the button right
  // after applyGameMode() removes it.
  if (GAME_MODE !== "per_account") return;
  const tools = document.querySelector(".account-tools");
  if (!tools || document.querySelector("#cronus-bulk-game")) return;
  const bar = document.createElement("div");
  bar.className = "cronus-bulkbar";
  bar.id = "cronus-bulk-game";
  bar.innerHTML = `<button type="button" class="btn ghost has-icon" id="cronus-bulk-btn" title="Set game for all accounts">${bulkButtonHtml()}</button>`;
  tools.appendChild(bar);
  bar.querySelector("#cronus-bulk-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    openBulkPop(bar.querySelector("#cronus-bulk-btn"));
  });
  updateBulkLabel();
}

function updateBulkLabel() {
  // Label is now static ("Set game") — no selection count.
  // Kept as a function because refresh paths call it after every fetch.
  // Repair legacy buttons rendered without the icon (cached HTML).
  const btn = document.querySelector("#cronus-bulk-btn");
  if (!btn) return;
  if (!btn.querySelector("svg")) {
    btn.classList.add("has-icon");
    btn.title = "Set game for all accounts";
    btn.innerHTML = bulkButtonHtml();
  }
}

function currentGameId(user) {
  return String(
    ACCOUNT_GAMES[
      String(user || "")
        .trim()
        .toLowerCase()
    ] || "",
  );
}

async function assignGame(user, gameId) {
  const payload = await api(
    "/account/" + encodeURIComponent(user) + "/game",
    "POST",
    { game_id: String(gameId || "") },
  );
  ACCOUNT_GAMES[String(user).toLowerCase()] = String(gameId || "");
  return payload;
}

// ── Game picker popover ─────────────────────────────────────────────

function closePop() {
  if (pop) {
    pop.remove();
    pop = null;
  }
  popUser = "";
}

function displayGameName(g) {
  const pid = String(g?.place_id || "").trim();
  return (
    String(PLACE_NAMES[pid] || "").trim() ||
    String(g?.name || "").trim() ||
    String(g?.id || "Game")
  );
}

function gameItemHtml(g, isCurrent) {
  const gid = String(g.id || "");
  const thumb = PLACE_THUMBS[String(g.place_id || "")] || "";
  const dname = displayGameName(g);
  const img = thumb
    ? `<img src="${esc(thumb)}" alt="" loading="lazy" onerror="this.hidden=true">`
    : `<span class="ogame-thumb ogame-thumb-empty" aria-hidden="true">${esc(dname.trim().charAt(0).toUpperCase() || "?")}</span>`;
  return `<button type="button" class="cronus-gitem${isCurrent ? " current" : ""}" data-game-id="${esc(gid)}">${img}<span class="t"><span class="n">${esc(dname)}</span><br><span class="p">${esc(g.place_id ? "Place " + g.place_id : "No place set")}</span></span><span class="tick">${isCurrent ? "✓" : ""}</span></button>`;
}

function noGameHtml(isCurrent) {
  return `<button type="button" class="cronus-gitem${isCurrent ? " current" : ""}" data-game-id=""><span class="ogame-thumb ogame-thumb-empty" aria-hidden="true">—</span><span class="t"><span class="n">(No game)</span><br><span class="p">No launch target — Start is blocked until assigned</span></span><span class="tick">${isCurrent ? "✓" : ""}</span></button>`;
}

function popHeadHtml(title) {
  return `<div class="cronus-gpop-head"><span>${title}</span><button type="button" data-close="1" title="Close">✕</button></div>`;
}

function isSameGameId(game, other) {
  const gid = String(game.id || "");
  return !!gid && gid.toLowerCase() === String(other).toLowerCase();
}

function gameListHtml(current) {
  return GAMES.map((g) => gameItemHtml(g, isSameGameId(g, current))).join("");
}

function positionPop(anchor) {
  if (!pop) return;
  const rect = anchor?.getBoundingClientRect?.();
  const pw = Math.min(pop.offsetWidth || 320, 360);
  let left = (rect?.right ?? 0) + 8;
  if (left + pw > window.innerWidth - 8)
    left = Math.max(8, window.innerWidth - pw - 8);
  let top = Math.min(
    rect?.top ?? 0,
    window.innerHeight - pop.offsetHeight - 8,
  );
  pop.style.left = left + "px";
  pop.style.top = Math.max(8, top) + "px";
}

function preloadThumbs(key, reopen) {
  GAMES.forEach((g) => {
    const pid = String(g.place_id || "").trim();
    if (!pid || PLACE_NAMES[pid] !== undefined) return;
    placeInfo(pid).then(() => {
      if (!pop || popUser !== key || !document.contains(pop)) return;
      reopen();
    });
  });
}

function wirePopItems(onPick) {
  pop
    .querySelector('[data-close="1"]')
    .addEventListener("click", (e) => {
      e.stopPropagation();
      closePop();
    });
  pop.querySelectorAll(".cronus-gitem").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const gid = String(btn.dataset.gameId || "");
      btn.style.opacity = "0.5";
      try {
        await onPick(gid);
        closePop();
      } catch (error) {
        btn.style.opacity = "";
        toast(error.message);
      }
    });
  });
}

function assignedToast(user, payload) {
  refreshRowBadges();
  notifyGamesChanged();
  if (
    Array.isArray(payload && payload.game_stopped) &&
    payload.game_stopped.length
  ) {
    toast(`No game set - stopped ${user} now`, "info");
    return;
  }
  if (
    Array.isArray(payload && payload.game_rejoined) &&
    payload.game_rejoined.length
  ) {
    toast(`Game changed - switching ${user} now`, "info");
  }
}

function reopenUserPop(user, anchor) {
  closePop();
  const anchorNow = document.querySelector(
    `#accounts-table tr[data-user="${CSS.escape(user)}"] .game-cell`,
  );
  if (anchorNow) openPop(user, anchorNow);
}

function openPop(user, anchor) {
  const key = String(user || "")
    .trim()
    .toLowerCase();
  if (!key) return;
  if (pop && popUser === key) {
    closePop();
    return;
  } // toggle
  closePop();
  popUser = key;
  const current = currentGameId(key);
  pop = document.createElement("div");
  pop.className = "online-popover cronus-game-pop";
  pop.dataset.user = key;
  pop.innerHTML =
    popHeadHtml(`Game · ${esc(user)}`) +
    gameListHtml(current) +
    noGameHtml(!gameById(current));
  wirePopItems(async (gid) => {
    const payload = await assignGame(user, gid);
    assignedToast(user, payload);
  });
  preloadThumbs(key, () => reopenUserPop(user, anchor));
  document.body.appendChild(pop);
  positionPop(anchor);
}

// ── Bulk popover ────────────────────────────────────────────────────

function allUsersMatchGame(users, gid) {
  return (
    !!gid &&
    users.every(
      (u) => currentGameId(u).toLowerCase() === String(gid).toLowerCase(),
    )
  );
}

function bulkNoneSet(users) {
  return users.every((u) => !gameById(currentGameId(u)));
}

function bulkToastMessage(gid, users, payload) {
  const switching = Array.isArray(payload && payload.game_rejoined)
    ? payload.game_rejoined.length
    : 0;
  const stopping = Array.isArray(payload && payload.game_stopped)
    ? payload.game_stopped.length
    : 0;
  const label = gid
    ? `Game set for ${users.length} accounts`
    : `Cleared game for ${users.length} accounts`;
  const stopped = stopping ? ` - stopped ${stopping} now` : "";
  const switched = switching ? ` - switching ${switching} now` : "";
  return label + stopped + switched;
}

async function assignBulkGame(users, gid) {
  const payload = await api("/accounts/assign-game", "POST", {
    usernames: users,
    game_id: gid,
  });
  users.forEach((u) => {
    ACCOUNT_GAMES[String(u).toLowerCase()] = String(gid || "");
  });
  refreshRowBadges();
  notifyGamesChanged();
  toast(bulkToastMessage(gid, users, payload));
}

function reopenBulkPop() {
  closePop();
  const btn = document.querySelector("#cronus-bulk-btn");
  if (btn) openBulkPop(btn);
}

function openBulkPop(anchor) {
  if (GAME_MODE !== "per_account") return;
  const users = allUsernames();
  if (!users.length) {
    toast("No accounts yet", "info");
    return;
  }
  if (pop && popUser === "bulk") {
    closePop();
    return;
  } // toggle
  closePop();
  popUser = "bulk";
  pop = document.createElement("div");
  pop.className = "online-popover cronus-game-pop";
  pop.dataset.user = "bulk";
  pop.innerHTML =
    popHeadHtml(`Set game · All ${users.length}`) +
    GAMES.map((g) => gameItemHtml(g, allUsersMatchGame(users, String(g.id || "")))).join(
      "",
    ) +
    noGameHtml(bulkNoneSet(users));
  wirePopItems((gid) => assignBulkGame(users, gid));
  preloadThumbs("bulk", reopenBulkPop);
  document.body.appendChild(pop);
  positionPop(anchor);
}

// ── Global wiring ───────────────────────────────────────────────────

function gameCellFrom(target) {
  return target?.closest?.("#accounts-table .game-cell") || null;
}

function closePopOnOutsideClick(event) {
  if (pop && !pop.contains(event.target)) closePop();
}

function handleGameCellClick(tr, cell, type, event) {
  if (GAME_MODE !== "per_account") {
    if (type === "click") {
      event.preventDefault();
      toast(
        "Shared game mode — switch Game Mode to Per-account to assign games",
        "info",
      );
    }
    return;
  }
  if (type !== "click") return;
  event.preventDefault();
  openPop(tr.dataset.user, cell);
}

function handleGlobalPointer(type, event) {
  const cell = gameCellFrom(event.target);
  if (!cell) {
    if (type === "click") closePopOnOutsideClick(event);
    return;
  }
  const tr = cell.closest("tr[data-user]");
  if (!tr || !tr.dataset.user) return;
  event.stopPropagation();
  handleGameCellClick(tr, cell, type, event);
}

function wireGlobal() {
  if (globalWired) return;
  globalWired = true;
  // Clicking anywhere in the GAME cell opens the picker (the per-row
  // button is gone by design). Stop propagation so the row is not
  // selected/deselected as a side effect.
  ["mousedown", "click"].forEach((type) =>
    document.addEventListener(
      type,
      (e) => handleGlobalPointer(type, e),
      true,
    ),
  );
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closePop();
  });
  // No selection tracking needed (bulk = all accounts), but keep the
  // hook so legacy buttons without the icon get repaired on row clicks.
  document.querySelector("#accounts-table")?.addEventListener("click", () => {
    setTimeout(updateBulkLabel, 0);
  });
}

function decorateRows() {
  wireGlobal();
  ensureBulkBar();
  updateBulkLabel();
}

// The GAME cell itself opens the picker, so repaints only refresh the
// bulk button. Map display is rendered natively by the dashboard from
// effective_place_id (single render path = no flicker/duel).
// Kept as a function because refresh paths call it after every fetch.
function refreshRowBadges() {
  updateBulkLabel();
  refreshAssignCounts();
}

// Per-game sub-line sync for pool rows.
function refreshAssignCounts() {
  document
    .querySelectorAll("#game-pool-list .game-pool-item")
    .forEach((item) => {
      const gid = String(item.dataset.gameId || "")
        .trim()
        .toLowerCase();
      const game = GAMES.find(
        (g) => String(g.id || "").toLowerCase() === gid,
      );
      const sub = item.querySelector('[data-role="sub"]');
      if (sub && game) {
        const pid = String(game.place_id || "").trim();
        const full = pid || "No place set";
        if (sub.textContent !== full) sub.textContent = full;
        sub.title = full;
      }
    });
}

// ── Bootstrap ───────────────────────────────────────────────────────

// Debounced + self-muted: dashboard re-renders the table on every status
// tick, so run at most once per burst and never observe our own writes.
const observer = new MutationObserver(() => {
  if (scheduled || document.hidden) return;
  scheduled = true;
  setTimeout(() => {
    scheduled = false;
    observer.disconnect();
    try {
      decorateRows();
    } finally {
      observer.observe(document.documentElement, {
        childList: true,
        subtree: true,
      });
    }
  }, 120);
});
observer.observe(document.documentElement, {
  childList: true,
  subtree: true,
});

refreshGames();
refreshAccountGames();
decorateRows();
// Keep the Game Pool preview in sync with the unsaved mode switch;
// the 30s poll below remains the persisted-state fallback.
try {
  document.addEventListener("cronus:game-mode", (e) => {
    const mode = e && e.detail && e.detail.mode;
    if (!mode) {
      refreshGames();
      return;
    }
    setGameMode(mode, !!(e.detail && e.detail.preview));
  });
} catch (_) {}
setInterval(() => {
  if (document.hidden) return;
  refreshGames();
  refreshAccountGames();
}, 30000);
