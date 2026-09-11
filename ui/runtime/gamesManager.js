(function () {
  "use strict";

  let GAMES = [];
  let GAME_MODE = "shared"; // GAME card switch; pool UI only lives in per-account
  let ACCOUNT_GAMES = {}; // lower(username) -> game_id
  let PLACE_NAMES = {}; // place_id -> name
  let PLACE_THUMBS = {}; // place_id -> image_url
  let scheduled = false;
  let pop = null; // game picker popover element
  let popUser = ""; // lower(username) the popover is open for
  let globalWired = false;

  // Signature of the games list. Every render helper compares against it
  // and writes to the DOM ONLY when something actually changed — otherwise
  // the MutationObserver below would ping-pong on our own writes forever
  // and freeze the page.
  function gamesSig() {
    return GAMES.map((g) => [g.id, g.name, g.place_id, g.private_server_url, !!g.auto_create_private_server_enabled].join("|")).join(";");
  }

  function esc(value) {
    const node = document.createElement("span");
    node.textContent = String(value || "");
    return node.innerHTML;
  }

  function apiHeaders(json) {
    const headers = {};
    const token = document.querySelector('meta[name="cronus-api-token"]')?.content || "";
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
    if (!response.ok) throw new Error(payload.detail || payload.msg || response.statusText || "Request failed");
    return payload;
  }

  // Project-style notifications: same stacked .toast-item cards as
  // components/feedback.js (success/error/info + icons, max 4, 3.2s).
  const TOAST_ICONS = {
    success: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#6366f1"/><path d="M8.2 12.2l2.6 2.6 4.5-5" stroke="white" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    error: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#f87171"/><path d="M15 9l-6 6M9 9l6 6" stroke="white" stroke-width="1.9" stroke-linecap="round"/></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none"><path d="M6 13c0 3 1.5 5 6 5s6-2 6-5V8a6 6 0 0 0-12 0v5z" stroke="#60a5fa" stroke-width="1.7" stroke-linejoin="round"/><path d="M9 16a3 3 0 0 0 6 0" stroke="#60a5fa" stroke-width="1.7" stroke-linecap="round"/><circle cx="12" cy="8" r="1.5" fill="#60a5fa"/></svg>',
  };

  function toast(message, type) {
    const box = document.querySelector("#toast");
    if (!box) return;
    let kind = type;
    if (!kind) {
      kind = /error|failed|invalid|cannot|blocked|denied|missing|not found/i.test(String(message || "")) ? "error" : "success";
    }
    while (box.children.length >= 4) {
      if (box.firstElementChild) box.firstElementChild.remove();
      else break;
    }
    const node = document.createElement("div");
    node.className = "toast-item toast-" + kind;
    node.innerHTML = `<span class="toast-icon">${TOAST_ICONS[kind] || TOAST_ICONS.info}</span><span class="toast-text">${esc(message)}</span>`;
    box.appendChild(node);
    requestAnimationFrame(() => node.classList.add("show"));
    setTimeout(() => {
      node.classList.remove("show");
      node.classList.add("hide");
      setTimeout(() => node.remove(), 200);
    }, 3200);
  }

  function gameById(id) {
    const wanted = String(id || "").trim().toLowerCase();
    return GAMES.find((g) => String(g.id || "").trim().toLowerCase() === wanted) || null;
  }

  function gameLabel(game) {
    if (!game) return "No game";
    const name = String(game.name || game.id || "Game");
    const place = String(game.place_id || "");
    return place ? `${name} · ${place}` : name;
  }

  function mapCellHtml(placeId, name, thumb) {
    const pid = String(placeId || "").trim();
    const label = String(name || "").trim() || (pid ? `Place ${pid}` : "—");
    const img = thumb
      ? `<img class="cronus-map-thumb" src="${esc(thumb)}" alt="" loading="lazy" onerror="this.remove()">`
      : "";
    return `${img}<span class="cronus-map-name" title="${esc(label)}">${esc(label)}</span>`;
  }

  // (buttonLabel below renders the per-row picker text.)
  async function refreshGames(retry) {
    try {
      const data = await api("/games");
      GAMES = Array.isArray(data.games) ? data.games : [];
      setGameMode(data.game_mode);
    } catch (_) {
      // Keep the last good list: a single failed tick must not wipe the UI.
      if (!retry) setTimeout(() => refreshGames(true), 4000);
      return;
    }
    renderGamesCard();
    refreshRowBadges();
  }

  function setGameMode(mode) {
    const next = String(mode || "").trim().toLowerCase().replace("-", "_") === "per_account"
      ? "per_account"
      : "shared";
    GAME_MODE = next;
    applyGameMode();
  }

  // Shared mode: the pool is ignored, so its UI goes away entirely —
  // pool card hidden, bulk button removed, row picker locked.
  function applyGameMode() {
    try {
      document.body.dataset.gameMode = GAME_MODE;
    } catch (_) {}
    if (GAME_MODE !== "per_account") {
      closePop();
      const panel = document.querySelector("#cronus-games-panel");
      if (panel) panel.hidden = true;
      const bulk = document.querySelector("#cronus-bulk-game");
      if (bulk) bulk.remove();
      return;
    }
    const panel = document.querySelector("#cronus-games-panel");
    if (panel) panel.hidden = false;
    ensureBulkBar();
  }

  async function refreshAccountGames(retry) {
    try {
      const data = await api("/accounts");
      const next = {};
      (Array.isArray(data) ? data : []).forEach((acc) => {
        const user = String(acc.username || "").trim().toLowerCase();
        if (user) next[user] = String(acc.game_id || "");
      });
      ACCOUNT_GAMES = next;
    } catch (_) {
      // Keep the last good map; retry soon instead of waiting a full tick.
      if (!retry) setTimeout(() => refreshAccountGames(true), 4000);
      return;
    }
    refreshRowBadges();
  }

  async function placeInfo(placeId) {
    const pid = String(placeId || "").trim();
    if (!pid) return { name: "", image_url: "" };
    if (PLACE_NAMES[pid] !== undefined) return { name: PLACE_NAMES[pid], image_url: PLACE_THUMBS[pid] || "" };
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

  // ── Games CRUD card in the Game view ──────────────────────────────
  function renderGamesCard() {
    const view = document.querySelector("#view-game");
    if (!view) return;
    let card = document.querySelector("#cronus-games-card");
    if (!card) {
      card = document.createElement("section");
      card.className = "panel panel-body-only";
      card.id = "cronus-games-panel";
      card.innerHTML = `
        <style>
          #cronus-games-card .cronus-game-item{border:1px solid #23252d;border-radius:12px;padding:14px 16px;margin-bottom:12px;background:#101116;box-shadow:inset 0 1px 0 rgba(255,255,255,.03);}
          #cronus-games-card .cronus-game-item:hover{border-color:#2e313b}
          #cronus-games-card .cronus-game-head{display:flex;gap:8px;align-items:center;margin-bottom:12px}
          #cronus-games-card .cronus-game-glyph{width:32px;height:32px;flex:none;display:grid;place-items:center;border-radius:9px;background:rgba(99,102,241,.14);border:1px solid rgba(99,102,241,.35);color:#a5b4fc;font-size:14px;font-weight:800}
          #cronus-games-card .cronus-game-head .input{flex:1;min-width:0;font-weight:700}
          #cronus-games-card .cronus-game-head .btn.good{padding:7px 16px;font-size:12px;flex:none}
          #cronus-games-card .cronus-game-head .icon-btn{flex:none}
          #cronus-games-card .cronus-game-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px 14px}
          #cronus-games-card .cronus-game-field{display:grid;gap:6px;align-content:start;min-width:0}
          #cronus-games-card .cronus-game-field.full{grid-column:1/-1}
          #cronus-games-card .cronus-game-field>label{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#6c7079}
          #cronus-games-card .cronus-game-field>label span{font-weight:400;text-transform:none;letter-spacing:0;color:#53565e}
          #cronus-games-card .cronus-game-grid .input{width:100%;font-size:12.5px}
          #cronus-games-card .cronus-game-grid .input.mono{font-family:var(--mono,monospace);font-size:12px}
          #cronus-games-card .cronus-game-grid .input:focus{border-color:#6366f1!important;box-shadow:0 0 0 3px rgba(99,102,241,.22);outline:none}
          #cronus-games-card .cronus-game-map{display:flex;gap:10px;align-items:center;min-width:0;padding:1px 0}
          #cronus-games-card .cronus-map-thumb{width:40px;height:40px;flex:none;border-radius:8px;object-fit:cover;background:#0b0c10;border:1px solid #22242b}
          #cronus-games-card .cronus-map-name{font-size:12.5px;font-weight:600;color:#e6e7eb;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
          #cronus-games-card .cronus-map-loading{font-size:12px;color:#53565e}
          #cronus-games-card .cronus-game-auto{display:flex;gap:10px;align-items:flex-start;grid-column:1/-1;margin-top:2px;padding-top:12px;border-top:1px solid rgba(140,144,156,.12);cursor:pointer;color:#caccd2;font-size:12.5px}
          #cronus-games-card .cronus-game-auto input{appearance:none;-webkit-appearance:none;width:17px;height:17px;margin:1px 0 0;border-radius:6px;border:1.5px solid #434653;background:transparent;cursor:pointer;flex:none;display:grid;place-items:center}
          #cronus-games-card .cronus-game-auto input:checked{background:#5855ea;border-color:#6366f1}
          #cronus-games-card .cronus-game-auto input:checked::after{content:"";width:11px;height:11px;background:#fff;-webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M5 12.5l4.5 4.5L19 7.5' fill='none' stroke='black' stroke-width='3.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center/contain no-repeat;mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M5 12.5l4.5 4.5L19 7.5' fill='none' stroke='black' stroke-width='3.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center/contain no-repeat}
          #cronus-games-card .cronus-game-auto input:focus-visible{outline:2px solid #818cf8;outline-offset:2px}
          #cronus-games-card .cronus-game-auto strong{font-weight:700}
          #cronus-games-card .cronus-game-auto em{font-style:normal;color:#53565e}
          @media(max-width:560px){#cronus-games-card .cronus-game-grid{grid-template-columns:1fr}}
          #cronus-games-card .cronus-game-meta{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px}
          #cronus-games-card .cronus-game-count{font-size:12px;opacity:.75}
          #accounts-table .game-cell{cursor:pointer}
          body[data-game-mode="shared"] #accounts-table .game-cell{cursor:default}
          body[data-game-mode="shared"] #cronus-games-panel,body[data-game-mode="shared"] #cronus-bulk-game{display:none!important}
          .cronus-game-pop{padding:10px}
          .cronus-game-pop .cronus-gpop-head{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:2px 4px 8px;font-size:12px;font-weight:700;color:#f2f3f5}
          .cronus-game-pop .cronus-gpop-head button{background:none;border:none;color:#8d9099;cursor:pointer;font-size:14px;line-height:1;padding:2px 6px}
          .cronus-game-pop .cronus-gpop-head button:hover{color:#fff}
          .cronus-gitem{display:grid;grid-template-columns:40px minmax(0,1fr) auto;gap:10px;align-items:center;width:100%;text-align:left;background:#101116;border:1px solid #1f2128;border-radius:10px;padding:8px;margin-bottom:6px;cursor:pointer;color:#f2f3f5}
          .cronus-gitem:hover{border-color:#32343d;background:#17181d}
          .cronus-gitem.current{border-color:#1f6feb}
          .cronus-gitem .ogame-thumb{width:40px;height:40px;font-size:14px;flex:none}
          .cronus-gitem img{width:40px;height:40px;border-radius:8px;object-fit:cover;background:#0b0c10;border:1px solid #22242b;flex:none}
          .cronus-gitem .t{min-width:0}
          .cronus-gitem .n{font-size:13px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
          .cronus-gitem .p{font-size:11px;color:#7f838c;font-family:var(--mono,monospace)}
          .cronus-gitem .tick{color:#4ade80;font-weight:800;padding:0 4px}
          #view-accounts .account-tools{grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:10px}
          .cronus-bulkbar{display:flex;gap:8px;align-items:center;min-width:0}
          #cronus-bulk-btn{height:33px;white-space:nowrap;flex:none}
          .cronus-game-map{min-height:40px}
          .cronus-game-assign{font-size:11.5px;color:#7f838c;margin-top:10px}
          .cronus-game-item.is-dirty{border-color:rgba(99,102,241,.5)}
          .cronus-game-item.is-dirty [data-action="save"]{box-shadow:0 0 0 3px rgba(99,102,241,.18)}
          #cronus-games-card [data-action="delete"].armed{background:#4c1d24;border-color:#f43f5e;color:#fda4af}
        </style>
        <div class="settings-card cards-mode"><section class="queue-card" id="cronus-games-card">
          <div class="queue-card-head"><div class="queue-card-title">Game Collection<div class="hint">Multiple Place IDs — assign accounts to a game</div></div></div>
          <div class="queue-card-body">
            <div id="cronus-games-list"></div>
            <div class="cronus-game-meta">
              <button type="button" class="btn ghost" id="cronus-game-add">+ Add game</button>
              <span class="cronus-game-count" id="cronus-games-count"></span>
            </div>
            <div id="cronus-games-notice" class="notice"></div>
          </div>
        </section></div>`;
      const anchor = view.querySelector(".panel");
      if (anchor) anchor.insertAdjacentElement("afterend", card);
      else view.appendChild(card);
      card.querySelector("#cronus-game-add").addEventListener("click", onAddGame);
      // Sync visibility at creation: on cold start applyGameMode() already
      // ran before this card existed, so without this it stays visible
      // until the next poll.
      card.hidden = (GAME_MODE !== "per_account");
    }
    const list = card.querySelector("#cronus-games-list");
    const count = card.querySelector("#cronus-games-count");
    const countText = GAMES.length ? `${GAMES.length} game(s)` : "No games yet — add one below";
    if (count && count.textContent !== countText) count.textContent = countText;
    // Never rebuild while the user is editing a field, and never rebuild
    // when the data is unchanged (both would steal focus / loop the observer).
    if (list.contains(document.activeElement)) return;
    const sig = gamesSig();
    if (list.dataset.sig === sig) return;
    list.dataset.sig = sig;
    list.innerHTML = "";
    GAMES.forEach((game) => {
      const item = document.createElement("div");
      item.className = "cronus-game-item";
      item.dataset.gameId = String(game.id || "");
      item.innerHTML = `
        <div class="cronus-game-head">
          <span class="cronus-game-glyph" data-role="glyph" aria-hidden="true">?</span>
          <input class="input" data-field="name" value="${esc(game.name || "")}" placeholder="Game name">
          <button type="button" class="btn good" data-action="save">Save</button>
          <button type="button" class="icon-btn" data-action="delete" title="Delete game">✕</button>
        </div>
        <div class="cronus-game-grid">
          <div class="cronus-game-field"><label>Place ID</label><input class="input mono" data-field="place_id" value="${esc(game.place_id || "")}" placeholder="123456789" inputmode="numeric"></div>
          <div class="cronus-game-field"><label>Map</label><div class="cronus-game-map" data-role="map-name">—</div></div>
          <div class="cronus-game-field full"><label>Private Server URL <span>optional · per-game VIP</span></label><input class="input" data-field="private_server_url" value="${esc(game.private_server_url || "")}" placeholder="Private server URL" inputmode="url"></div>
          <label class="cronus-game-auto"><input type="checkbox" data-field="auto_create_private_server_enabled" ${game.auto_create_private_server_enabled ? "checked" : ""}><span><strong>Auto Create Private Server</strong> <em>(this game)</em></span></label>
        </div><div class="cronus-game-assign" data-role="assign"></div>`;
      list.appendChild(item);
      const glyphEl = item.querySelector('[data-role="glyph"]');
      const paintGlyph = () => {
        if (glyphEl) glyphEl.textContent = String(item.querySelector('[data-field="name"]')?.value || "?").trim().charAt(0).toUpperCase() || "?";
      };
      paintGlyph();
      const mapEl = item.querySelector('[data-role="map-name"]');
      const paintMap = (pid) => {
        placeInfo(pid).then(({ name, image_url }) => {
          if (!document.contains(mapEl)) return;
          if (String(item.querySelector('[data-field="place_id"]')?.value || "").trim() !== pid) return;
          if (document.contains(mapEl)) mapEl.innerHTML = mapCellHtml(pid, name, image_url);
        });
      };
      if (game.place_id) {
        paintMap(String(game.place_id));
        mapEl.innerHTML = `<span class="cronus-map-loading">Loading…</span>`;
      } else {
        mapEl.innerHTML = `<span class="cronus-map-loading">Enter a Place ID</span>`;
      }
      const markDirty = () => item.classList.add("is-dirty");
      item.querySelector('[data-field="name"]')?.addEventListener("input", () => { paintGlyph(); markDirty(); });
      item.querySelector('[data-field="place_id"]')?.addEventListener("input", () => {
        markDirty();
        clearTimeout(item._previewTimer);
        item._previewTimer = setTimeout(() => {
          const pid = String(item.querySelector('[data-field="place_id"]')?.value || "").trim();
          if (!pid) { mapEl.innerHTML = `<span class="cronus-map-loading">Enter a Place ID</span>`; return; }
          if (!/^\d+$/.test(pid)) { mapEl.innerHTML = `<span class="cronus-map-loading">Place ID must be numeric</span>`; return; }
          mapEl.innerHTML = `<span class="cronus-map-loading">Loading…</span>`;
          paintMap(pid);
        }, 600);
      });
      item.querySelector('[data-field="private_server_url"]')?.addEventListener("input", markDirty);
      item.querySelector('[data-field="auto_create_private_server_enabled"]')?.addEventListener("change", markDirty);
      item.querySelector('[data-action="save"]').addEventListener("click", () => onSaveGame(item));
      item.querySelector('[data-action="delete"]').addEventListener("click", () => onDeleteGame(item));
      refreshAssignCounts();
    });
  }

  function readGameForm(item) {
    const get = (field) => item.querySelector(`[data-field="${field}"]`);
    return {
      id: String(item.dataset.gameId || ""),
      name: String(get("name")?.value || "").trim(),
      place_id: String(get("place_id")?.value || "").trim(),
      private_server_url: String(get("private_server_url")?.value || "").trim(),
      auto_create_private_server_enabled: !!get("auto_create_private_server_enabled")?.checked,
    };
  }

  function notice(message, isError) {
    const node = document.querySelector("#cronus-games-notice");
    if (node) {
      node.textContent = String(message || "");
      node.classList.toggle("show", !!message);
      node.classList.toggle("notice-error", !!isError);
    }
  }

  async function onAddGame() {
    const draft = { id: "", name: `Game ${GAMES.length + 1}`, place_id: "", private_server_url: "", auto_create_private_server_enabled: false };
    GAMES = GAMES.concat([draft]);
    const list = document.querySelector("#cronus-games-list");
    if (list) delete list.dataset.sig; // force rebuild for the new row
    renderGamesCard();
    const items = document.querySelectorAll("#cronus-games-list .cronus-game-item");
    const last = items[items.length - 1];
    last?.querySelector('[data-field="place_id"]')?.focus();
  }

  async function onSaveGame(item) {
    const form = readGameForm(item);
    if (form.place_id && !/^\d+$/.test(form.place_id)) {
      notice("place_id must be numeric", true);
      return;
    }
    if (!form.id && !form.place_id && !form.private_server_url) {
      notice("place_id or Private Server URL required", true);
      return;
    }
    try {
      const payload = await api("/games", "POST", { game: form });
      GAMES = Array.isArray(payload.games) ? payload.games : GAMES;
      if (document.activeElement) document.activeElement.blur();
      renderGamesCard();
      refreshAccountGames();
      toast("Game saved");
      notice("");
    } catch (error) {
      notice(error.message, true);
    }
  }

  async function onDeleteGame(item) {
    const id = String(item.dataset.gameId || "");
    if (!id) {
      GAMES = GAMES.filter((g) => String(g.id || "") !== "");
      if (document.activeElement) document.activeElement.blur();
      renderGamesCard();
      return;
    }
    const arm = (msg) => {
      const btn = item.querySelector('[data-action="delete"]');
      item.dataset.armed = "1";
      if (btn) { btn.classList.add("armed"); btn.title = msg; }
      notice(msg, true);
      clearTimeout(item._armTimer);
      item._armTimer = setTimeout(() => {
        delete item.dataset.armed;
        delete item.dataset.force;
        const live = item.querySelector('[data-action="delete"]');
        if (live && document.contains(live)) { live.classList.remove("armed"); live.title = "Delete game"; }
      }, 4000);
    };
    const game = gameById(id);
    const label = game ? game.name : id;
    if (!item.dataset.armed) {
      arm(`Click ✕ again to delete "${label}"`);
      return;
    }
    const reqBody = { id };
    if (item.dataset.force) reqBody.force = true;
    try {
      const payload = await api("/games/delete", "POST", reqBody);
      GAMES = Array.isArray(payload.games) ? payload.games : GAMES;
      if (document.activeElement) document.activeElement.blur();
      renderGamesCard();
      refreshAccountGames();
      toast("Game deleted");
      notice("");
    } catch (error) {
      if (!item.dataset.force && /assigned to \d+ account/.test(String(error.message || ""))) {
        item.dataset.force = "1";
        arm(`${error.message} — click ✕ again to force delete and unassign them`);
        return;
      }
      notice(error.message, true);
    }
  }

  // ── Per-row game picker + badge in the accounts table ─────────────
  function selectedUsernames() {
    return Array.from(document.querySelectorAll('#accounts-table tr.selected[data-user]'))
      .map((row) => String(row.dataset.user || "").trim())
      .filter(Boolean);
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
    bar.innerHTML = `<button type="button" class="btn ghost" id="cronus-bulk-btn" title="Assign selected accounts to a game">Set game</button>`;
    tools.appendChild(bar);
    bar.querySelector("#cronus-bulk-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      openBulkPop(bar.querySelector("#cronus-bulk-btn"));
    });
    updateBulkLabel();
  }

  function updateBulkLabel() {
    const btn = document.querySelector("#cronus-bulk-btn");
    if (!btn) return;
    const n = selectedUsernames().length;
    const label = n > 0 ? `Set game (${n})` : "Set game";
    if (btn.textContent !== label) btn.textContent = label;
  }

  function currentGameId(user) {
    return String(ACCOUNT_GAMES[String(user || "").trim().toLowerCase()] || "");
  }

  async function assignGame(user, gameId) {
    const payload = await api("/account/" + encodeURIComponent(user) + "/game", "POST", { game_id: String(gameId || "") });
    ACCOUNT_GAMES[String(user).toLowerCase()] = String(gameId || "");
    return payload;
  }

  function closePop() {
    if (pop) { pop.remove(); pop = null; }
    popUser = "";
  }

  function gameItemHtml(g, isCurrent) {
    const gid = String(g.id || "");
    const thumb = PLACE_THUMBS[String(g.place_id || "")] || "";
    const img = thumb
      ? `<img src="${esc(thumb)}" alt="" loading="lazy" onerror="this.hidden=true">`
      : `<span class="ogame-thumb ogame-thumb-empty" aria-hidden="true">${esc(String(g.name || "?").trim().charAt(0).toUpperCase() || "?")}</span>`;
    return `<button type="button" class="cronus-gitem${isCurrent ? " current" : ""}" data-game-id="${esc(gid)}">${img}<span class="t"><span class="n">${esc(g.name || gid)}</span><br><span class="p">${esc(g.place_id ? "Place " + g.place_id : "No place set")}</span></span><span class="tick">${isCurrent ? "✓" : ""}</span></button>`;
  }

  function noGameHtml(isCurrent) {
    return `<button type="button" class="cronus-gitem${isCurrent ? " current" : ""}" data-game-id=""><span class="ogame-thumb ogame-thumb-empty" aria-hidden="true">—</span><span class="t"><span class="n">(No game)</span><br><span class="p">START will skip this account</span></span><span class="tick">${isCurrent ? "✓" : ""}</span></button>`;
  }

  function positionPop(anchor) {
    if (!pop) return;
    const rect = anchor?.getBoundingClientRect?.();
    const pw = Math.min(pop.offsetWidth || 320, 360);
    let left = (rect?.right ?? 0) + 8;
    if (left + pw > window.innerWidth - 8) left = Math.max(8, window.innerWidth - pw - 8);
    let top = Math.min(rect?.top ?? 0, window.innerHeight - pop.offsetHeight - 8);
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
    pop.querySelector('[data-close="1"]').addEventListener("click", (e) => { e.stopPropagation(); closePop(); });
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

  function openPop(user, anchor) {
    const key = String(user || "").trim().toLowerCase();
    if (!key) return;
    if (pop && popUser === key) { closePop(); return; } // toggle
    closePop();
    popUser = key;
    const current = currentGameId(key);
    pop = document.createElement("div");
    pop.className = "online-popover cronus-game-pop";
    pop.dataset.user = key;
    pop.innerHTML = `<div class="cronus-gpop-head"><span>Game · ${esc(user)}</span><button type="button" data-close="1" title="Close">✕</button></div>`
      + GAMES.map((g) => gameItemHtml(g, !!String(g.id || "") && String(g.id).toLowerCase() === String(current).toLowerCase())).join("")
      + noGameHtml(!gameById(current));
    wirePopItems(async (gid) => {
      await assignGame(user, gid);
      refreshRowBadges();
    });
    preloadThumbs(key, () => {
      closePop();
      const anchorNow = document.querySelector(`#accounts-table tr[data-user="${CSS.escape(user)}"] .game-cell`);
      if (anchorNow) openPop(user, anchorNow);
    });
    document.body.appendChild(pop);
    positionPop(anchor);
  }

  function openBulkPop(anchor) {
    if (GAME_MODE !== "per_account") return;
    const users = selectedUsernames();
    if (!users.length) {
      toast("Select accounts first (click rows)", "info");
      return;
    }
    if (pop && popUser === "bulk") { closePop(); return; } // toggle
    closePop();
    popUser = "bulk";
    const allSame = (gid) => !!gid && users.every((u) => currentGameId(u).toLowerCase() === String(gid).toLowerCase());
    const noneSet = users.every((u) => !gameById(currentGameId(u)));
    pop = document.createElement("div");
    pop.className = "online-popover cronus-game-pop";
    pop.dataset.user = "bulk";
    pop.innerHTML = `<div class="cronus-gpop-head"><span>Set game · ${users.length} selected</span><button type="button" data-close="1" title="Close">✕</button></div>`
      + GAMES.map((g) => gameItemHtml(g, allSame(String(g.id || "")))).join("")
      + noGameHtml(noneSet);
    wirePopItems(async (gid) => {
      await api("/accounts/assign-game", "POST", { usernames: users, game_id: gid });
      users.forEach((u) => { ACCOUNT_GAMES[String(u).toLowerCase()] = gid; });
      refreshRowBadges();
    });
    preloadThumbs("bulk", () => {
      closePop();
      const btn = document.querySelector("#cronus-bulk-btn");
      if (btn) openBulkPop(btn);
    });
    document.body.appendChild(pop);
    positionPop(anchor);
  }

  function wireGlobal() {
    if (globalWired) return;
    globalWired = true;
    // Clicking anywhere in the GAME cell opens the picker (the per-row
    // button is gone by design). Stop propagation so the row is not
    // selected/deselected as a side effect.
    ["mousedown", "click"].forEach((type) => document.addEventListener(type, (e) => {
      const cell = e.target?.closest?.("#accounts-table .game-cell");
      if (!cell) {
        if (type === "click" && pop && !pop.contains(e.target)) closePop();
        return;
      }
      const tr = cell.closest("tr[data-user]");
      if (!tr || !tr.dataset.user) return;
      e.stopPropagation();
      if (GAME_MODE !== "per_account") {
        if (type === "click") {
          e.preventDefault();
          toast("Shared game mode — switch Game Mode to Per-account to assign games", "info");
        }
        return;
      }
      if (type === "click") {
        e.preventDefault();
        openPop(tr.dataset.user, cell);
      }
    }, true));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closePop();
    });
    // Row selection toggles only classes (no re-render), so refresh the
    // bulk button count on row clicks directly.
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

  // Per-game account counts under each Games-card entry.
  function refreshAssignCounts() {
    const counts = {};
    Object.values(ACCOUNT_GAMES).forEach((id) => {
      const key = String(id || "").trim().toLowerCase();
      if (key) counts[key] = (counts[key] || 0) + 1;
    });
    document.querySelectorAll('#cronus-games-list .cronus-game-item').forEach((item) => {
      const slot = item.querySelector('[data-role="assign"]');
      if (!slot) return;
      const gid = String(item.dataset.gameId || "").trim().toLowerCase();
      const text = !gid
        ? "Not saved yet"
        : (counts[gid] ? `${counts[gid]} account${counts[gid] > 1 ? "s" : ""} assigned` : "No accounts assigned");
      if (slot.textContent !== text) slot.textContent = text;
    });
  }

  // Debounced + self-muted: dashboard re-renders the table on every status
  // tick, so run at most once per burst and never observe our own writes.
  const observer = new MutationObserver(() => {
    if (scheduled) return;
    scheduled = true;
    setTimeout(() => {
      scheduled = false;
      observer.disconnect();
      try {
        decorateRows();
      } finally {
        observer.observe(document.documentElement, { childList: true, subtree: true });
      }
    }, 120);
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });

  refreshGames();
  refreshAccountGames();
  decorateRows();
  // Instant reaction to the GAME card mode switch (saveGamePanel
  // dispatches this); the 30s poll below stays as the fallback.
  try {
    document.addEventListener("cronus:game-mode", (e) => {
      const mode = e && e.detail && e.detail.mode;
      if (!mode) {
        refreshGames();
        return;
      }
      setGameMode(mode);
    });
  } catch (_) {}
  setInterval(() => {
    refreshGames();
    refreshAccountGames();
  }, 30000);
})();
