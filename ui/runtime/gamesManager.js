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
  let MODAL_GAME = null; // game object currently open in center modal; {id:""} = new draft; null = closed

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
          #cronus-games-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px;margin-top:4px}
          #cronus-games-card .cronus-game-item{border:1px solid #23252d;border-radius:12px;background:#101116;overflow:hidden;padding:0;cursor:pointer;animation:cronus-card-in 500ms cubic-bezier(.16,1,.3,1) backwards;animation-delay:calc(var(--index,0)*60ms)}
          @keyframes cronus-card-in{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
          #cronus-games-card .cronus-game-item:hover{border-color:#3a3d47}
          #cronus-games-card .cronus-game-cover{position:relative;aspect-ratio:16/10;background:#0b0c10;display:grid;place-items:center;overflow:hidden;border-bottom:1px solid #1d1f26}
          #cronus-games-card .cronus-game-cover img{width:100%;height:100%;object-fit:cover;object-position:center 22%;display:block}
          #cronus-games-card .cronus-game-glyph-lg{font-family:'Kanit','Helvetica Neue',sans-serif;font-size:40px;font-weight:700;color:#3f4350}
          #cronus-games-card .cronus-game-cover-hover{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.55);opacity:0;transition:opacity 200ms ease;pointer-events:none}
          #cronus-games-card .cronus-game-item:hover .cronus-game-cover-hover{opacity:1}
          #cronus-games-card .cronus-game-editpill{display:inline-flex;align-items:center;gap:7px;background:#5855ea;color:#fff;font-size:12.5px;font-weight:800;letter-spacing:-.01em;padding:9px 20px;border-radius:6px;transform:translateY(4px) scale(.98);transition:transform 200ms cubic-bezier(.16,1,.3,1)}
          #cronus-games-card .cronus-game-item:hover .cronus-game-editpill{transform:none}
          #cronus-games-card .cronus-game-cardbody{padding:16px 18px 14px}
          #cronus-games-card .cronus-game-cardname{display:block;font-family:'Kanit','Helvetica Neue',sans-serif;font-size:14px;font-weight:700;letter-spacing:0;line-height:1.3;color:#f2f3f5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
          #cronus-games-card .cronus-game-cardsub{display:block;margin-top:4px;font-family:var(--mono,monospace);font-size:11px;color:#787774;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
          #cronus-games-card .cronus-game-cardfoot,#cronus-games-card .cronus-game-cardbadges,#cronus-games-card .cronus-game-assignline{display:none}
          #cronus-games-card .cronus-game-badge{font-size:10px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;padding:3px 9px;border-radius:9999px;border:1px solid #2b2e37;color:#9aa0ab;background:#17181d}
          #cronus-games-card .cronus-game-badge.vip{background:rgba(99,102,241,.16);border-color:rgba(99,102,241,.5);color:#a5b4fc}
          #cronus-games-card .cronus-game-badge.auto{background:rgba(52,211,153,.12);border-color:rgba(52,211,153,.4);color:#6ee7b7}
          #cronus-games-card .cronus-game-addcard{border:1px dashed #2e313b;border-radius:12px;min-height:190px;display:grid;place-items:center;cursor:pointer;color:#787774;background:transparent}
          #cronus-games-card .cronus-game-addcard:hover{border-color:#3a3d47;color:#caccd2}
          #cronus-games-card .cronus-game-addinner{display:grid;gap:6px;justify-items:center;padding:24px}
          #cronus-games-card .cronus-game-addplus{font-size:26px;font-weight:300;line-height:1}
          #cronus-games-card .cronus-game-addtext{font-size:12.5px;font-weight:700}
          /* center modal */
          #cronus-game-modal-overlay{position:fixed;inset:0;z-index:9990;display:flex;align-items:center;justify-content:center;padding:20px;background:rgba(0,0,0,.62)}
          #cronus-game-modal-overlay[hidden]{display:none}
          #cronus-game-modal{width:min(520px,calc(100vw - 32px));max-height:calc(100vh - 60px);overflow:auto;background:#14151a;border:1px solid #2a2d36;border-radius:12px;padding:20px 20px 16px;animation:cronus-modal-in 220ms cubic-bezier(.16,1,.3,1)}
          @keyframes cronus-modal-in{from{opacity:0;transform:translateY(12px) scale(.99)}to{opacity:1;transform:none}}
          #cronus-game-modal .cronus-modal-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:4px}
          #cronus-game-modal .cronus-modal-title{font-family:'Kanit','Helvetica Neue',sans-serif;font-size:17px;font-weight:700;letter-spacing:0;line-height:1.3;color:#f2f3f5}
          #cronus-game-modal .cronus-modal-sub{font-size:12px;color:#787774;margin-top:4px;line-height:1.6}
          #cronus-game-modal .cronus-modal-head-actions{display:flex;gap:8px;flex:none}
          #cronus-game-modal .cronus-modal-close,#cronus-game-modal .cronus-modal-trash{background:none;border:1px solid #2a2d36;border-radius:6px;color:#9aa0ab;width:30px;height:30px;cursor:pointer;font-size:14px;flex:none;display:inline-grid;place-items:center;padding:0}
          #cronus-game-modal .cronus-modal-close:hover{color:#fff;border-color:#3a3d47}
          #cronus-game-modal .cronus-modal-trash:hover{color:#fda4af;border-color:#f43f5e}
          #cronus-game-modal .cronus-modal-trash svg{width:15px;height:15px}
          #cronus-game-modal .cronus-modal-trash.armed{background:#4c1d24;border-color:#f43f5e;color:#fda4af}
          #cronus-game-modal .cronus-modal-cover{display:none}
          #cronus-game-modal .cronus-modal-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}
          #cronus-game-modal .cronus-modal-field{display:grid;gap:7px;align-content:start}
          #cronus-game-modal .cronus-modal-field.full{grid-column:1/-1}
          #cronus-game-modal .cronus-modal-field>label{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#787774}
          #cronus-game-modal .cronus-modal-field>label span{font-weight:400;text-transform:none;letter-spacing:0}
          #cronus-game-modal .input{width:100%;font-size:13px;background:#0c0d12;border:1px solid #2a2d36;border-radius:6px;color:#f2f3f5;padding:9px 12px}
          #cronus-game-modal .input.mono{font-family:var(--mono,monospace);font-size:12px}
          #cronus-game-modal .input:focus{border-color:#6366f1;outline:none;box-shadow:0 0 0 3px rgba(99,102,241,.22)}
          #cronus-game-modal .cronus-modal-map{display:flex;gap:10px;align-items:center;min-height:48px;font-size:12.5px;color:#caccd2}
          #cronus-game-modal .cronus-modal-map .cronus-map-thumb{width:48px;height:48px;flex:none;border-radius:8px;object-fit:cover;object-position:center 20%}
          #cronus-game-modal .cronus-modal-auto{display:flex;gap:10px;align-items:flex-start;margin-top:14px;padding-top:14px;border-top:1px solid #23252d;cursor:pointer;font-size:12.5px;color:#caccd2;line-height:1.6}
          #cronus-game-modal .cronus-modal-auto input{appearance:none;-webkit-appearance:none;width:17px;height:17px;margin:2px 0 0;border-radius:6px;border:1.5px solid #434653;background:transparent;cursor:pointer;flex:none;display:grid;place-items:center}
          #cronus-game-modal .cronus-modal-auto input:checked{background:#5855ea;border-color:#6366f1}
          #cronus-game-modal .cronus-modal-auto input:checked::after{content:"";width:11px;height:11px;background:#fff;-webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M5 12.5l4.5 4.5L19 7.5' fill='none' stroke='black' stroke-width='3.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center/contain no-repeat;mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M5 12.5l4.5 4.5L19 7.5' fill='none' stroke='black' stroke-width='3.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center/contain no-repeat}
          #cronus-game-modal .cronus-modal-actions{display:flex;gap:8px;align-items:center;justify-content:flex-end;margin-top:20px;padding-top:16px;border-top:1px solid #23252d}
          #cronus-game-modal .btn-primary{background:#5855ea;color:#fff;border:1px solid #5855ea;border-radius:6px;font-size:13px;font-weight:800;padding:9px 20px;cursor:pointer}
          #cronus-game-modal .btn-primary:hover{background:#6366f1;border-color:#6366f1}
          #cronus-game-modal .btn-primary:active{transform:scale(.98)}
          #cronus-game-modal .btn-ghost2{background:none;border:1px solid #2a2d36;color:#caccd2;border-radius:6px;font-size:13px;padding:9px 16px;cursor:pointer}
          #cronus-game-modal .btn-ghost2:hover{border-color:#3a3d47;color:#fff}
          #cronus-game-modal .btn-danger2{background:#FDEBEC;border:1px solid #FDEBEC;color:#9F2F2D;border-radius:6px;font-size:13px;font-weight:700;padding:9px 14px;cursor:pointer;margin-left:auto}
          #cronus-game-modal .btn-danger2.armed{background:#9F2F2D;border-color:#9F2F2D;color:#fff}
          #cronus-game-modal.is-dirty .btn-primary{outline:2px solid rgba(99,102,241,.5);outline-offset:2px}
          #cronus-game-modal .cronus-modal-assign{font-size:11.5px;color:#787774;margin-top:10px}
          @media(max-width:560px){#cronus-game-modal .cronus-modal-grid{grid-template-columns:1fr}#cronus-game-modal{padding:20px}}
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
          <div class="queue-card-head"><div class="queue-card-title">Game Collection<div class="hint">Cards — select one to edit in the center panel</div></div></div>
          <div class="queue-card-body">
            <div id="cronus-games-list"></div>
            <div class="cronus-game-meta">
              <span class="cronus-game-count" id="cronus-games-count"></span>
            </div>
            <div id="cronus-games-notice" class="notice"></div>
          </div>
        </section></div>`;
      const anchor = view.querySelector(".panel");
      if (anchor) anchor.insertAdjacentElement("afterend", card);
      else view.appendChild(card);
      // Add lives on the dashed grid tile only (old bottom button removed).
      // Sync visibility at creation: on cold start applyGameMode() already
      // ran before this card existed, so without this it stays visible
      // until the next poll.
      card.hidden = (GAME_MODE !== "per_account");
    }
    ensureModalShell();
    const list = card.querySelector("#cronus-games-list");
    const count = card.querySelector("#cronus-games-count");
    const countText = GAMES.length ? `${GAMES.length} game(s)` : "No games yet — add one below";
    if (count && count.textContent !== countText) count.textContent = countText;
    const sig = gamesSig();
    if (list.dataset.sig === sig) { refreshAssignCounts(); return; }
    list.dataset.sig = sig;
    list.innerHTML = "";
    GAMES.forEach((game, idx) => {
      const gid = String(game.id || "");
      const item = document.createElement("div");
      item.className = "cronus-game-item";
      item.dataset.gameId = gid;
      item.style.setProperty("--index", String(idx % 12));
      const pid0 = String(game.place_id || "").trim();
      const mapName0 = String(PLACE_NAMES[pid0] || "").trim();
      const title0 = mapName0 || String(game.name || "").trim() || (pid0 ? `Place ${pid0}` : "New game");
      const glyphChar = title0.trim().charAt(0).toUpperCase() || "?";
      item.innerHTML = `
        <div class="cronus-game-cover" data-role="cover">
          <span class="cronus-game-glyph-lg" data-role="cover-glyph" aria-hidden="true">${esc(glyphChar)}</span>
          <span class="cronus-game-cover-hover" aria-hidden="true"><span class="cronus-game-editpill">Edit</span></span>
        </div>
        <div class="cronus-game-cardbody">
          <span class="cronus-game-cardname" data-role="card-name">${esc(title0)}</span>
          <span class="cronus-game-cardsub" data-role="card-sub">${pid0 ? `Place ${esc(pid0)}` : "No place set"}</span>
        </div>`;
      item.addEventListener("click", () => openGameModal(game));
      list.appendChild(item);
      const cover = item.querySelector('[data-role="cover"]');
      const glyphEl = item.querySelector('[data-role="cover-glyph"]');
      const cardNameEl = item.querySelector('[data-role="card-name"]');
      const pid = String(game.place_id || "").trim();
      if (pid) {
        placeInfo(pid).then(({ name, image_url }) => {
          if (!document.contains(cover)) return;
          if (image_url) {
            let img = cover.querySelector("img");
            if (!img) {
              img = document.createElement("img");
              img.alt = "";
              img.loading = "lazy";
              img.onerror = () => img.remove();
              cover.insertBefore(img, cover.firstChild);
            }
            if (img.getAttribute("src") !== image_url) img.src = image_url;
            if (glyphEl) glyphEl.remove();
          }
          const fresh = String(name || "").trim();
          if (fresh && document.contains(cardNameEl) && cardNameEl.textContent !== fresh) cardNameEl.textContent = fresh;
        });
      }
    });
    // Add-card tile
    {
      const add = document.createElement("div");
      add.className = "cronus-game-addcard";
      add.setAttribute("role", "button");
      add.setAttribute("tabindex", "0");
      add.title = "Add game";
      add.innerHTML = `<span class="cronus-game-addinner"><span class="cronus-game-addplus">+</span><span class="cronus-game-addtext">Add game</span></span>`;
      add.addEventListener("click", onAddGame);
      add.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onAddGame(); } });
      list.appendChild(add);
    }
    refreshAssignCounts();
  }

  function ensureModalShell() {
    if (document.querySelector("#cronus-game-modal-overlay")) return;
    const ov = document.createElement("div");
    ov.id = "cronus-game-modal-overlay";
    ov.hidden = true;
    ov.innerHTML = `<div id="cronus-game-modal" role="dialog" aria-modal="true"></div>`;
    ov.addEventListener("mousedown", (e) => { if (e.target === ov) closeGameModal(); });
    document.body.appendChild(ov);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && MODAL_GAME) closeGameModal();
    });
  }

  function modalNotice(msg, isError) {
    notice(msg, isError);
    const m = document.querySelector("#cronus-game-modal .cronus-modal-err");
    if (m) { m.textContent = String(msg || ""); m.style.display = msg ? "block" : "none"; }
  }

  function openGameModal(game) {
    ensureModalShell();
    MODAL_GAME = { ...(game || {}) };
    const isNew = !String(MODAL_GAME.id || "");
    const ov = document.querySelector("#cronus-game-modal-overlay");
    const modal = document.querySelector("#cronus-game-modal");
    const startName = String(PLACE_NAMES[String(MODAL_GAME.place_id || "").trim()] || MODAL_GAME.name || "").trim();
    modal.classList.remove("is-dirty");
    delete modal.dataset.armed; delete modal.dataset.force;
    modal.innerHTML = `
      <div class="cronus-modal-head">
        <div><div class="cronus-modal-title" data-role="modal-title">${esc(isNew ? "New game" : (startName || "Edit game"))}</div>
        <div class="cronus-modal-sub">${isNew ? "Put a Place ID, pick VIP if needed, then save." : "Adjust the fields below, then save."}</div></div>
        <div class="cronus-modal-head-actions">
          ${isNew ? "" : `<button type="button" class="cronus-modal-trash" data-action="delete" title="Delete game"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg></button>`}
          <button type="button" class="cronus-modal-close" data-action="close" title="Close">✕</button>
        </div>
      </div>
      <div class="cronus-modal-grid">
        <div class="cronus-modal-field full"><label>Place ID</label><input class="input mono" data-field="place_id" value="${esc(MODAL_GAME.place_id || "")}" placeholder="123456789" inputmode="numeric"></div>
        <div class="cronus-modal-field full"><label>Map preview</label><div class="cronus-modal-map" data-role="map-name">—</div></div>
        <div class="cronus-modal-field full"><label>Private Server URL <span>optional · per-game VIP</span></label><input class="input" data-field="private_server_url" value="${esc(MODAL_GAME.private_server_url || "")}" placeholder="Private server URL" inputmode="url"></div>
      </div>
      <label class="cronus-modal-auto"><input type="checkbox" data-field="auto_create_private_server_enabled" ${MODAL_GAME.auto_create_private_server_enabled ? "checked" : ""}><span><strong>Auto Create Private Server</strong> (this game)</span></label>
      <div class="cronus-modal-err" style="display:none;font-size:12px;color:#f87171;margin-top:10px"></div>
      <div class="cronus-modal-actions">
        <button type="button" class="btn-primary" data-action="save">Save</button>
        <button type="button" class="btn-ghost2" data-action="cancel">Cancel</button>
      </div>`;
    ov.hidden = false;
    document.body.style.overflow = "hidden";
    const get = (f) => modal.querySelector(`[data-field="${f}"]`);
    const mapEl = modal.querySelector('[data-role="map-name"]');
    const titleEl = modal.querySelector('[data-role="modal-title"]');
    const paintMap = (pid) => {
      placeInfo(pid).then(({ name, image_url }) => {
        if (!document.contains(mapEl)) return;
        if (String(get("place_id")?.value || "").trim() !== pid) return;
        mapEl.innerHTML = mapCellHtml(pid, name, image_url);
        const fresh = String(name || "").trim();
        if (fresh && document.contains(titleEl) && titleEl.textContent !== fresh) titleEl.textContent = fresh;
      });
    };
    const startPid = String(MODAL_GAME.place_id || "").trim();
    if (startPid) { mapEl.innerHTML = `<span class="cronus-map-loading">Loading…</span>`; paintMap(startPid); }
    else mapEl.innerHTML = `<span class="cronus-map-loading">Enter a Place ID to preview</span>`;
    const markDirty = () => modal.classList.add("is-dirty");
    modal.querySelectorAll("[data-field]").forEach((inp) => {
      inp.addEventListener("input", markDirty);
      inp.addEventListener("change", markDirty);
    });
    get("place_id")?.addEventListener("input", () => {
      clearTimeout(modal._previewTimer);
      modal._previewTimer = setTimeout(() => {
        const pid = String(get("place_id")?.value || "").trim();
        if (!pid) { mapEl.innerHTML = `<span class="cronus-map-loading">Enter a Place ID to preview</span>`; return; }
        if (!/^\d+$/.test(pid)) { mapEl.innerHTML = `<span class="cronus-map-loading">Place ID must be numeric</span>`; return; }
        mapEl.innerHTML = `<span class="cronus-map-loading">Loading…</span>`;
        paintMap(pid);
      }, 500);
    });
    modal.querySelector('[data-action="close"]').addEventListener("click", () => closeGameModal());
    modal.querySelector('[data-action="cancel"]').addEventListener("click", () => closeGameModal());
    modal.querySelector('[data-action="save"]').addEventListener("click", onSaveModal);
    modal.querySelector('[data-action="delete"]')?.addEventListener("click", onDeleteModal);
    setTimeout(() => get("place_id")?.focus?.(), 30);
  }

  function closeGameModal() {
    MODAL_GAME = null;
    const ov = document.querySelector("#cronus-game-modal-overlay");
    if (ov) ov.hidden = true;
    document.body.style.overflow = "";
    modalNotice("");
  }

  function readModalForm() {
    const modal = document.querySelector("#cronus-game-modal");
    const get = (f) => modal?.querySelector(`[data-field="${f}"]`);
    const placeId = String(get("place_id")?.value || "").trim();
    // Game Name field removed from UI: keep the key for backend compat,
    // auto-filled from the live map name so Dashboard + cards always match.
    const autoName = String(PLACE_NAMES[placeId] || "").trim() || String(MODAL_GAME?.name || "").trim() || (placeId ? `Place ${placeId}` : "New game");
    return {
      id: String(MODAL_GAME?.id || ""),
      name: autoName,
      place_id: placeId,
      private_server_url: String(get("private_server_url")?.value || "").trim(),
      auto_create_private_server_enabled: !!get("auto_create_private_server_enabled")?.checked,
    };
  }

  function notifyGamesChanged() { try { document.dispatchEvent(new CustomEvent("cronus:games-changed")); } catch (_) {} }
  function notice(message, isError) {
    const node = document.querySelector("#cronus-games-notice");
    if (node) {
      node.textContent = String(message || "");
      node.classList.toggle("show", !!message);
      node.classList.toggle("notice-error", !!isError);
    }
  }

  async function onAddGame() {
    openGameModal({ id: "", name: `Game ${GAMES.length + 1}`, place_id: "", private_server_url: "", auto_create_private_server_enabled: false });
  }

  async function onSaveModal() {
    const form = readModalForm();
    if (form.place_id && !/^\d+$/.test(form.place_id)) { modalNotice("place_id must be numeric", true); return; }
    if (!form.id && !form.place_id && !form.private_server_url) { modalNotice("place_id or Private Server URL required", true); return; }
    try {
      const payload = await api("/games", "POST", { game: form });
      GAMES = Array.isArray(payload.games) ? payload.games : GAMES;
      closeGameModal();
      const list = document.querySelector("#cronus-games-list");
      if (list) delete list.dataset.sig;
      renderGamesCard();
      refreshAccountGames();
      notifyGamesChanged();
      toast("Game saved");
      notice("");
    } catch (error) { modalNotice(error.message, true); }
  }

  async function onDeleteModal() {
    const modal = document.querySelector("#cronus-game-modal");
    const id = String(MODAL_GAME?.id || "");
    if (!id) { closeGameModal(); return; }
    const btn = modal?.querySelector('[data-action="delete"]');
    const game = gameById(id);
    const pid = String(game?.place_id || "").trim();
    const label = String(PLACE_NAMES[pid] || "").trim() || (game ? game.name : id);
    if (!modal.dataset.armed) {
      modal.dataset.armed = "1";
      if (btn) { btn.classList.add("armed"); btn.title = "Click again to confirm delete"; }
      modalNotice(`Click the trash again to delete "${label}" — accounts on it fall back to (No game)`, true);
      clearTimeout(modal._armTimer);
      modal._armTimer = setTimeout(() => {
        delete modal.dataset.armed;
        if (btn && document.contains(btn)) { btn.classList.remove("armed"); btn.title = "Delete game"; }
      }, 4000);
      return;
    }
    // Force delete at once: assigned accounts fall back to (No game).
    try {
      const payload = await api("/games/delete", "POST", { id, force: true });
      GAMES = Array.isArray(payload.games) ? payload.games : GAMES;
      closeGameModal();
      const list = document.querySelector("#cronus-games-list");
      if (list) delete list.dataset.sig;
      renderGamesCard();
      refreshAccountGames();
      notifyGamesChanged();
      toast("Game deleted");
      notice("");
    } catch (error) {
      modalNotice(error.message, true);
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

  function displayGameName(g) {
    const pid = String(g?.place_id || "").trim();
    return String(PLACE_NAMES[pid] || "").trim() || String(g?.name || "").trim() || String(g?.id || "Game");
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
      notifyGamesChanged();
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
      notifyGamesChanged();
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

  // Per-game counts for grid cards.
  function refreshAssignCounts() {
    const counts = {};
    Object.values(ACCOUNT_GAMES).forEach((id) => {
      const key = String(id || "").trim().toLowerCase();
      if (key) counts[key] = (counts[key] || 0) + 1;
    });
    document.querySelectorAll('#cronus-games-list .cronus-game-item').forEach((item) => {
      const gid = String(item.dataset.gameId || "").trim().toLowerCase();
      const game = GAMES.find((g) => String(g.id || "").toLowerCase() === gid);
      const sub = item.querySelector('[data-role="card-sub"]');
      if (sub && game) {
        const pid = String(game.place_id || "").trim();
        const full = pid ? `Place ${pid}` : "No place set";
        if (sub.textContent !== full) sub.textContent = full;
        sub.title = full;
      }
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
