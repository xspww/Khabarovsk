/* Custom UI enhancements - extracted from ui_dashboard.py for cache/perf.
   Optimized: reduced polling intervals, use MutationObserver where possible */
(() => {
  // NOTE: nav/guard icons are inline SVG (icons.js + currentColor) — no image files.
  const initGridPicker = () => {
    const select = document.getElementById('window-grid-preset');
    if (!select) return;
    const host = select.parentElement;
    if (!host) return;
    host.querySelector('.custom-select')?.classList.add('grid-picker-native-hidden');
    if (select.dataset.gridPickerReady === '1') return;
    select.dataset.gridPickerReady = '1';
    const picker = document.createElement('div');
    picker.className = 'window-grid-picker';
    picker.setAttribute('role', 'grid');
    const summary = document.createElement('div');
    summary.className = 'window-grid-summary';
    const cells = [];
    for (let row = 1; row <= 12; row += 1) {
      for (let column = 1; column <= 12; column += 1) {
        const cell = document.createElement('button');
        cell.type = 'button';
        cell.className = 'window-grid-cell';
        cell.dataset.column = String(column);
        cell.dataset.row = String(row);
        cell.setAttribute('role', 'gridcell');
        cell.setAttribute('aria-label', `${column} columns by ${row} rows`);
        cell.addEventListener('click', () => {
          const value = `${column}x${row}`;
          let option = Array.from(select.options).find((item) => item.value === value);
          if (!option) {
            option = new Option(`${column} x ${row} (${column * row})`, value);
            select.add(option);
          }
          select.value = value;
          select.dispatchEvent(new Event('change', { bubbles: true }));
          syncGridPicker();
        });
        picker.appendChild(cell);
        cells.push(cell);
      }
    }
    host.append(picker, summary);
    const syncGridPicker = () => {
      const [columns, rows] = String(select.value || '6x4').split('x').map(Number);
      const safeColumns = Math.max(1, Math.min(12, columns || 6));
      const safeRows = Math.max(1, Math.min(12, rows || 4));
      cells.forEach((cell) => {
        const active = Number(cell.dataset.column) <= safeColumns && Number(cell.dataset.row) <= safeRows;
        cell.classList.toggle('is-selected', active);
      });
      summary.textContent = `${safeColumns} x ${safeRows} (${safeColumns * safeRows} windows)`;
    };
    select.addEventListener('change', syncGridPicker);
    syncGridPicker();
  };
  if (!document.getElementById('window-grid-picker-style')) {
    const style = document.createElement('style');
    style.id = 'window-grid-picker-style';
    style.textContent = '.grid-picker-native-hidden{display:none!important}.window-grid-picker{display:grid;grid-template-columns:repeat(12,minmax(10px,1fr));gap:2px;width:198px;padding:7px;background:#0d1019;border:1px solid #282a31;border-radius:5px}.window-grid-cell{width:100%;aspect-ratio:1;border:1px solid #282a31;border-radius:2px;background:#131419;padding:0;cursor:pointer}.window-grid-cell:hover,.window-grid-cell.is-selected{background:#5962a9;border-color:#8290ef}.window-grid-cell:hover{box-shadow:0 0 0 1px #a5b4fc inset}.window-grid-summary{margin-top:5px;color:#cdced4;font:700 11px var(--mono)}';
    document.head.appendChild(style);
  }
  initGridPicker();
  // Replaced 500ms polling with observer + lazy retry (was heavy layout thrash)
  let _gridPickerObserved = false;
  const _ensureGridPicker = () => {
    if (document.getElementById('window-grid-preset')?.dataset.gridPickerReady === '1') return;
    initGridPicker();
  };
  if (!_gridPickerObserved) {
    _gridPickerObserved = true;
    new MutationObserver(_ensureGridPicker).observe(document.body, { childList: true, subtree: true });
    // fallback single retry after dashboard render
    setTimeout(_ensureGridPicker, 1500);
  }

  const apiHeaders = () => {
    const token = document.querySelector('meta[name="cronus-api-token"]')?.content || '';
    return token ? { 'X-Cronus-Token': token } : {};
  };

  // Same look as the feedback toast (icon + auto type), without importing it.
  const showResultToast = (message) => {
    const host = document.getElementById('toast');
    if (!host) return;
    const text = String(message || '');
    const lower = text.toLowerCase();
    let type = 'success';
    if (lower.includes('error') || lower.includes('failed') || lower.includes('invalid')
      || lower.includes('cannot') || lower.includes('blocked') || lower.includes('denied')
      || lower.includes('missing') || lower.includes('not found') || lower.includes('required')) {
      type = 'error';
    }
    while (host.children.length >= 4) host.firstElementChild?.remove();
    const icons = {
      success: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#6366f1"/><path d="M8.2 12.2l2.6 2.6 4.5-5" stroke="white" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>',
      error: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#f87171"/><path d="M15 9l-6 6M9 9l6 6" stroke="white" stroke-width="1.9" stroke-linecap="round"/></svg>',
    };
    const item = document.createElement('div');
    item.className = `toast-item toast-${type}`;
    const icon = document.createElement('span');
    icon.className = 'toast-icon';
    icon.innerHTML = icons[type] || icons.success;
    const label = document.createElement('span');
    label.className = 'toast-text';
    label.textContent = text;
    item.append(icon, label);
    host.appendChild(item);
    requestAnimationFrame(() => item.classList.add('show'));
    setTimeout(() => {
      item.classList.remove('show');
      item.classList.add('hide');
      setTimeout(() => item.remove(), 200);
    }, 3200);
  };

  const moveWindowControlsTop = () => {
    const rows = document.querySelector('#window-settings-card .queue-rows');
    if (!rows) return;
    const ids = ['window-grid-preset', 'window-size-preset', 'window-rearrange-now'];
    const controls = ids.map((id) => document.getElementById(id)?.closest('.queue-row')).filter(Boolean);
    controls.reverse().forEach((row) => rows.prepend(row));
  };
  moveWindowControlsTop();

  document.querySelector('#nav button[data-view="troubleshoot"] .nav-text')?.replaceChildren('Exploits Manager');
  document.querySelector('#view-troubleshoot .page-head .title')?.replaceChildren('Exploits Manager');
  const currentVersionLabel = document.querySelector('#view-troubleshoot .install-status');
  if (currentVersionLabel) currentVersionLabel.childNodes.forEach((node) => {
    if (node.nodeType === Node.TEXT_NODE && node.textContent.includes('Currently installed:')) node.textContent = 'Current Version: ';
  });

  // Search + running indicators are inline SVG (magnifer icon, status clock) — no image files.
  const favicon = document.querySelector('link[rel="icon"]');
  if (favicon) favicon.href = '/assets/cronus_icon.png';

  const closeButton = document.getElementById('close-all-roblox-btn');
  const escPick = (value) => {
    const node = document.createElement('span');
    node.textContent = String(value || '');
    return node.innerHTML;
  };
  if (closeButton && closeButton.dataset.selectedCloseReady !== '1') {
    closeButton.dataset.selectedCloseReady = '1';
    closeButton.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopImmediatePropagation();
      const backdrop = document.getElementById('modal-backdrop');
      const title = document.getElementById('modal-title');
      const body = document.getElementById('modal-body');
      const foot = document.getElementById('modal-foot');
      if (!backdrop || !title || !body || !foot) return;
      const rows = Array.from(document.querySelectorAll('#accounts-table tr[data-user]'));
      const statusOf = (row) => {
        const pill = row.querySelector('.status');
        const label = pill?.textContent?.trim() || 'Unknown';
        const cls = String(pill?.className || '').toLowerCase();
        const key = ['online', 'captcha', 'invalid', 'blocked', 'queued', 'launching', 'rejoining', 'cooldown', 'lua', 'checking', 'disconnected'].find((k) => cls.includes(k)) || (/idle/.test(cls) ? 'idle' : 'unknown');
        return { label, key };
      };
      // Pre-check accounts that actually have a live client (less clicking).
      const isRunning = (key) => key === 'online' || key === 'lua';
      title.textContent = 'Close Roblox';
      body.innerHTML = `<div class="v-sub">Select the accounts whose Roblox clients should close.</div><div class="selected-close-list">${rows.length ? rows.map((row) => {
        const user = row.dataset.user;
        const st = statusOf(row);
        const checked = isRunning(st.key) ? ' checked' : '';
        return `<label class="close-pick${checked ? ' is-checked' : ''}"><input type="checkbox" data-close-user="${user}"${checked}><span class="close-check" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="M5 12.5l4.5 4.5L19 7.5" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg></span><span class="close-name">${escPick(row.querySelector('.name')?.textContent?.trim() || user)}</span><span class="close-st st-${st.key}">${escPick(st.label)}</span></label>`;
      }).join('') : '<div class="v-empty">No accounts found.</div>'}</div>`;
      foot.innerHTML = '<div class="close-pick-foot"><div class="close-pick-tools"><button class="btn ghost" id="close-select-all">Select All</button><button class="btn ghost" id="close-clear">Clear</button></div><button class="btn danger" id="close-selected-confirm">Close Selected</button></div>';
      backdrop.hidden = false;
      const confirmBtn = document.getElementById('close-selected-confirm');
      const syncPick = () => {
        const boxes = Array.from(body.querySelectorAll('[data-close-user]'));
        const n = boxes.filter((input) => input.checked).length;
        boxes.forEach((input) => input.closest('.close-pick')?.classList.toggle('is-checked', input.checked));
        if (confirmBtn) {
          confirmBtn.textContent = n ? `Close Selected (${n})` : 'Close Selected';
          confirmBtn.disabled = !n;
        }
      };
      body.querySelector('.selected-close-list')?.addEventListener('change', syncPick);
      document.getElementById('close-select-all')?.addEventListener('click', () => { body.querySelectorAll('[data-close-user]').forEach((input) => { input.checked = true; }); syncPick(); });
      document.getElementById('close-clear')?.addEventListener('click', () => { body.querySelectorAll('[data-close-user]').forEach((input) => { input.checked = false; }); syncPick(); });
      syncPick();
      document.getElementById('close-selected-confirm')?.addEventListener('click', async () => {
        const usernames = Array.from(body.querySelectorAll('[data-close-user]:checked')).map((input) => input.dataset.closeUser).filter(Boolean);
        if (!usernames.length) return;
        const response = await fetch('/api/roblox/close-selected', { method: 'POST', headers: { ...apiHeaders(), 'Content-Type': 'application/json' }, body: JSON.stringify({ usernames }) });
        const result = await response.json();
        backdrop.hidden = true;
        showResultToast(result.msg || 'Roblox closed');
      });
    }, true);
  }

  const refreshCurrentVersion = async () => {
    const target = document.getElementById('roblox-installed-version');
    if (!target) return;
    try {
      const response = await fetch('/api/troubleshoot/roblox-install', { headers: apiHeaders() });
      const status = await response.json();
      target.textContent = status.latest_version || status.installed_version || 'Unknown';
    } catch (_) {}
  };
  refreshCurrentVersion();
  // Poll latest version every 30s instead of 5s (reduces API load)
  setInterval(refreshCurrentVersion, 30000);
  document.querySelector('#roblox-latest span')?.replaceChildren('Update to Latest');
  const latestOnly = () => {
    document.querySelectorAll('#modal-backdrop .vchoice').forEach((choice) => {
      const old = choice.querySelector('.vbadge.old');
      if (old) choice.remove();
    });
  };
  const modalBody = document.getElementById('modal-body');
  if (modalBody) new MutationObserver(latestOnly).observe(modalBody, { childList: true, subtree: true });

  const syncLimiterActions = () => {
    const limiterSave = document.getElementById('limiter-save');
    const limiterReset = document.getElementById('limiter-reset');
    const graphicsSave = document.getElementById('graphics-save');
    const graphicsReset = document.getElementById('graphics-reset');
    if (!limiterSave || !limiterReset || !graphicsSave || !graphicsReset) return;
    const graphicsDirty = graphicsSave.classList.contains('settings-dirty');
    const limiterDirty = limiterSave.classList.contains('settings-dirty');
    const anyDirty = graphicsDirty || limiterDirty;
    limiterSave.hidden = !anyDirty;
    limiterReset.hidden = !anyDirty;
    graphicsSave.hidden = true;
    graphicsReset.hidden = true;
    if (limiterSave.dataset.unifiedActions === '1') return;
    limiterSave.dataset.unifiedActions = '1';
    limiterSave.addEventListener('click', () => {
      if (graphicsSave.classList.contains('settings-dirty')) {
        setTimeout(() => graphicsSave.click(), 0);
      }
    }, true);
    limiterReset.addEventListener('click', () => {
      if (graphicsSave.classList.contains('settings-dirty')) {
        setTimeout(() => graphicsReset.click(), 0);
      }
    }, true);
  };
  syncLimiterActions();
  // Replaced 100ms polling with MutationObserver (perf)
  const _limiterRoot = document.getElementById('limiter-save')?.closest('.card') || document.body;
  new MutationObserver(syncLimiterActions).observe(_limiterRoot, { attributes: true, subtree: true, attributeFilter: ['class'] });

  // Floating description editor: the dashboard renders the editor inline in
  // the row and handles save/cancel/toolbar itself. We only lift the editor
  // node visually (position:fixed) so it looks like a popup menu.
  // Dashboard handlers stopPropagation on the table, so listen in capture.
  const FLOAT_W = 360;
  const FLOAT_H = 320;
  const floatDescEditor = (x, y) => {
    const editor = document.querySelector('#accounts-table .desc-editor');
    if (!editor) return;
    editor.classList.add('desc-floating');
    const left = Math.max(8, Math.min(Math.round(x) + 8, window.innerWidth - FLOAT_W - 8));
    let top = Math.round(y) + 8;
    if (top + FLOAT_H > window.innerHeight - 8) top = Math.max(8, Math.round(y) - FLOAT_H - 8);
    editor.style.left = `${left}px`;
    editor.style.top = `${top}px`;
  };
  const refocusDescTextarea = () => {
    const ta = document.querySelector('#accounts-table .desc-editor.desc-floating .desc-textarea');
    if (ta && document.activeElement !== ta) {
      try { ta.focus({ preventScroll: true }); } catch (_) { ta.focus(); }
    }
  };
  document.addEventListener('click', (event) => {
    if (!(event.target instanceof Element)) return;
    if (event.target.closest('[data-action="edit-desc"]')) {
      const x = event.clientX || 0, y = event.clientY || 0;
      setTimeout(() => floatDescEditor(x, y), 0);
      return;
    }
    if (event.target.closest('.desc-editor.desc-floating .desc-tool-btn')
      || event.target.closest('.desc-editor.desc-floating .desc-color-dot')) {
      // Keep focus in the textarea so live re-renders skip the editor.
      try { refocusDescTextarea(); } catch (_) {}
    }
  }, true);
  document.addEventListener('click', (event) => {
    // Neutral clicks outside the floating editor cancel it via its own button.
    if (!(event.target instanceof Element)) return;
    const floating = document.querySelector('#accounts-table .desc-editor.desc-floating');
    if (!floating) return;
    if (event.target.closest('.desc-editor')) return;
    const cancel = floating.querySelector('[data-action="cancel-desc"]');
    cancel?.click();
  });
})();
