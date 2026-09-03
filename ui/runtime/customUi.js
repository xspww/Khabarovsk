/* Custom UI enhancements - extracted from ui_dashboard.py for cache/perf.
   Optimized: reduced polling intervals, use MutationObserver where possible */
(() => {
  const icons = [
    ['#nav [data-nav-group="launcher"] .nav-group-icon', '/assets/nav-launcher.png'],
    ['#nav button[data-view="accounts"]', '/assets/nav-dashboard.png'],
    ['#nav [data-nav-group="performance"] .nav-group-icon', '/assets/nav-performance.png'],
    ['#nav [data-nav-group="tool"] .nav-group-icon', '/assets/nav-tool.png'],
  ];
  if (!document.getElementById('nav-image-icon-style')) {
    const style = document.createElement('style');
    style.id = 'nav-image-icon-style';
    style.textContent = '.nav-image-icon{width:18px;height:18px;flex:0 0 18px;display:block;object-fit:contain;filter:invert(1) opacity(.58)}.nav button:hover .nav-image-icon,.nav button.active .nav-image-icon{filter:invert(1) opacity(1)}.guard-start-image,.guard-stop-image{width:15px;height:15px;display:block;background:#818cf8;mask:url("/assets/nav-start.png") center/contain no-repeat;-webkit-mask:url("/assets/nav-start.png") center/contain no-repeat}.guard-stop-image{background:#f43f5e;mask-image:url("/assets/nav-stop.png");-webkit-mask-image:url("/assets/nav-stop.png")}';
    document.head.appendChild(style);
  }
  icons.forEach(([selector, src]) => {
    const root = document.querySelector(selector);
    const old = root?.querySelector('svg, img');
    if (!root || !old || old.classList.contains('nav-image-icon')) return;
    const image = document.createElement('img');
    image.className = 'nav-image-icon';
    image.src = src;
    image.alt = '';
    image.setAttribute('aria-hidden', 'true');
    old.replaceWith(image);
  });

  const applyGuardIcon = () => {
    const button = document.getElementById('guard-btn');
    if (!button) return;
    const stopping = button.classList.contains('danger');
    const iconClass = stopping ? 'guard-stop-image' : 'guard-start-image';
    const old = button.querySelector('svg, img, .guard-start-image, .guard-stop-image');
    if (!old || old.classList.contains(iconClass)) return;
    const image = document.createElement('span');
    image.className = iconClass;
    image.setAttribute('aria-hidden', 'true');
    old.replaceWith(image);
  };
  applyGuardIcon();
  const guardButton = document.getElementById('guard-btn');
  if (guardButton) {
    new MutationObserver(applyGuardIcon).observe(guardButton, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] });
  }

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

  const replaceMaskedIcon = (selector, src, className) => {
    const root = document.querySelector(selector);
    const old = root?.querySelector('svg, img');
    if (!root || !old || old.classList.contains(className)) return;
    const icon = document.createElement('span');
    icon.className = className;
    icon.style.setProperty('--icon-mask', `url("${src}")`);
    icon.setAttribute('aria-hidden', 'true');
    old.replaceWith(icon);
  };
  if (!document.getElementById('custom-ui-icon-style')) {
    const style = document.createElement('style');
    style.id = 'custom-ui-icon-style';
    style.textContent = '.custom-search-icon,.custom-running-icon,.custom-logo-icon{display:block;background:#cdced4;mask:var(--icon-mask) center/contain no-repeat;-webkit-mask:var(--icon-mask) center/contain no-repeat}.custom-search-icon{position:absolute;left:14px;top:50%;width:15px;height:15px;transform:translateY(-50%);background:#696d76;pointer-events:none}.custom-running-icon{width:15px;height:15px;background:#696d76}.status-running svg{stroke:#696d76}.custom-logo-icon{width:15px;height:15px;background:#656972}.app-started .custom-logo-icon{background:#2596be}';
    document.head.appendChild(style);
  }
  replaceMaskedIcon('.search-wrap', '/assets/icon-search.png', 'custom-search-icon');
  replaceMaskedIcon('.status-running', '/assets/icon-running.png', 'custom-running-icon');
  const favicon = document.querySelector('link[rel="icon"]');
  if (favicon) favicon.href = '/assets/cronus_icon.png';
  const syncAppState = () => document.documentElement.classList.toggle('app-started', document.getElementById('guard-btn')?.classList.contains('danger'));
  syncAppState();
  const guard = document.getElementById('guard-btn');
  if (guard) new MutationObserver(syncAppState).observe(guard, { attributes: true, attributeFilter: ['class'] });

  const closeButton = document.getElementById('close-all-roblox-btn');
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
      title.textContent = 'Close Roblox';
      body.innerHTML = `<div class="v-sub">Select the accounts whose Roblox clients should close.</div><div class="selected-close-list">${rows.length ? rows.map((row) => `<label class="selected-close-row"><input type="checkbox" data-close-user="${row.dataset.user}"><span>${row.querySelector('.name')?.textContent?.trim() || row.dataset.user}</span></label>`).join('') : '<div class="v-empty">No accounts found.</div>'}</div>`;
      foot.innerHTML = '<button class="btn ghost" id="close-select-all">Select All</button><button class="btn ghost" id="close-clear">Clear</button><button class="btn danger" id="close-selected-confirm">Close Selected</button>';
      backdrop.hidden = false;
      document.getElementById('close-select-all')?.addEventListener('click', () => body.querySelectorAll('[data-close-user]').forEach((input) => { input.checked = true; }));
      document.getElementById('close-clear')?.addEventListener('click', () => body.querySelectorAll('[data-close-user]').forEach((input) => { input.checked = false; }));
      document.getElementById('close-selected-confirm')?.addEventListener('click', async () => {
        const usernames = Array.from(body.querySelectorAll('[data-close-user]:checked')).map((input) => input.dataset.closeUser).filter(Boolean);
        if (!usernames.length) return;
        const response = await fetch('/api/roblox/close-selected', { method: 'POST', headers: { ...apiHeaders(), 'Content-Type': 'application/json' }, body: JSON.stringify({ usernames }) });
        const result = await response.json();
        backdrop.hidden = true;
        document.getElementById('toast').textContent = result.msg || 'Roblox closed';
        document.getElementById('toast').classList.add('show');
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
    document.querySelectorAll('#modal-backdrop .vchoice:not(.exploitstrap-choice) .rb-logo').forEach((logo) => {
      const rb = document.createElement('div');
      rb.className = 'choice-icon roblox-rb-icon';
      rb.textContent = 'RB';
      rb.setAttribute('aria-label', 'Roblox');
      logo.replaceWith(rb);
    });
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
})();
