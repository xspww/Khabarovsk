export function bindDashboardEvents(a){
  const $=a.$,doc=a.document;
  const NAV_BRANCH_ANIMATION_MS=980;
  const syncBranchHeight=group=>{
    const branch=group?.querySelector?.('.nav-branch');
    if(branch)branch.style.setProperty('--branch-height',`${Math.max(1,branch.scrollHeight)}px`);
  };
  const animateBranch=(group,open)=>{
    const branch=group?.querySelector?.('.nav-branch');
    if(!branch){
      group?.classList.toggle('collapsed',!open);
      group?.classList.toggle('expanded',open);
      return;
    }
    const height=Math.max(1,branch.scrollHeight);
    window.clearTimeout(branch._navAnimationTimer);
    branch.style.setProperty('--branch-height',`${height}px`);
    branch.style.maxHeight=open?'0px':`${height}px`;
    branch.style.opacity=open?'0':'1';
    branch.style.transform=open?'translateY(-10px)':'translateY(0)';
    branch.style.visibility='visible';
    branch.offsetHeight;
    group.classList.toggle('collapsed',!open);
    group.classList.toggle('expanded',open);
    requestAnimationFrame(()=>{
      branch.style.maxHeight=open?`${height}px`:'0px';
      branch.style.opacity=open?'1':'0';
      branch.style.transform=open?'translateY(0)':'translateY(-10px)';
    });
    branch._navAnimationTimer=window.setTimeout(()=>{
      branch.style.removeProperty('max-height');
      branch.style.removeProperty('opacity');
      branch.style.removeProperty('transform');
      branch.style.removeProperty('visibility');
    },NAV_BRANCH_ANIMATION_MS);
  };
  doc.querySelectorAll('#nav button[data-view]').forEach(b=>b.onclick=()=>a.setView(b.dataset.view));
  doc.querySelectorAll('[data-nav-toggle]').forEach(b=>{
    const group=b.closest('.nav-group');
    if(!group)return;
    syncBranchHeight(group);
    const expanded=!group.classList.contains('collapsed');
    b.setAttribute('aria-expanded',String(expanded));
    b.onclick=()=>{
      syncBranchHeight(group);
      const open=group.classList.contains('collapsed');
      animateBranch(group,open);
      b.setAttribute('aria-expanded',String(open));
    };
  });
  doc.addEventListener('change',e=>{if(e.target?.matches?.('.toggle-row input[type="checkbox"]'))a.syncToggleLabels()});
  $('guard-btn').onclick=a.toggleGuard;$('add-btn').onclick=a.openAdd;$('reload-cookies-btn').onclick=a.reloadCookies;$('close-all-roblox-btn').onclick=a.closeAllRoblox;$('modal-close').onclick=a.closeModal;$('account-search').oninput=a.renderAccountsTable;$('game-reset').onclick=a.resetGameSettings;$('game-save').onclick=a.saveGame;$('game-lookup').onclick=a.lookupGame;$('fps-reset').onclick=a.resetPerformanceSettings;$('fps-save').onclick=a.savePerformance;if($('ram-reset'))$('ram-reset').onclick=a.resetRamSettings;if($('ram-save'))$('ram-save').onclick=a.saveRam;$('graphics-reset').onclick=a.resetGraphicsSettings;$('graphics-save').onclick=a.saveGraphics;$('window-size-reset').onclick=a.resetWindowSizeSettings;$('window-size-save').onclick=a.saveWindowSize;$('cpu-reset').onclick=a.resetCpuLimiterSettings;$('cpu-save').onclick=a.saveCpuLimiter;$('queue-reset').onclick=a.resetQueueSettings;$('queue-save').onclick=a.saveQueue;$('roblox-uninstall').onclick=()=>a.robloxInstallConfirm('uninstall');$('roblox-latest').onclick=()=>a.robloxInstallConfirm('latest');
  $('account-filters')?.addEventListener('click',e=>{const btn=e.target.closest('[data-account-filter]');if(btn)a.setAccountFilter(btn.dataset.accountFilter)});
  $('game-auto-private-enabled').onchange=()=>{a.markDirty('game');a.renderSettings()};$('game-multi-roblox').onchange=()=>{a.markDirty('game');a.renderSettings()};$('fps-enabled').onchange=()=>{a.markDirty('performance');a.renderPerformance()};$('fps-limit').oninput=()=>a.markDirty('performance');if($('ram-enabled'))$('ram-enabled').onchange=()=>{a.markDirty('ram');a.renderRam()};if($('ram-limit-preset'))$('ram-limit-preset').onchange=()=>{a.markDirty('ram');a.renderRam()};if($('ram-limit-custom'))$('ram-limit-custom').oninput=()=>{a.markDirty('ram');a.renderRam()};$('priority-enabled').onchange=()=>{a.markDirty('graphics');a.renderGraphics()};$('graphics-auto-enabled').onchange=()=>{a.markDirty('graphics');a.renderGraphics()};$('graphics-quality').oninput=()=>a.markDirty('graphics');$('process-priority').onchange=()=>a.markDirty('graphics');$('window-size-enabled').onchange=()=>{a.markDirty('window-size');a.renderWindowSize()};$('window-size-preset').onchange=()=>{a.markDirty('window-size');a.renderWindowSize()};$('window-size-width').oninput=()=>a.markDirty('window-size');$('window-size-height').oninput=()=>a.markDirty('window-size');$('window-arrange-enabled').onchange=()=>{a.markDirty('window-size');a.renderWindowSize()};$('window-arrange-columns').oninput=()=>a.markDirty('window-size');$('window-arrange-gap').oninput=()=>a.markDirty('window-size');$('cpu-enabled').onchange=()=>{a.markDirty('cpu-limiter');a.renderCpuLimiter()};$('cpu-mode').onchange=()=>a.markDirty('cpu-limiter');$('cpu-default-limit').oninput=()=>{a.markDirty('cpu-limiter');a.renderCpuLimiter()};$('cpu-apply-all').onchange=()=>{a.markDirty('cpu-limiter');a.renderCpuLimiter()};$('cpu-account-rows').addEventListener('input',()=>a.markDirty('cpu-limiter'));$('cpu-account-rows').addEventListener('change',()=>a.markDirty('cpu-limiter'));$('queue-autoclose-enabled').onchange=()=>{a.markDirty('queue');a.renderSettings()};$('queue-autoclose-minutes').oninput=()=>a.markDirty('queue');$('popup-disconnected-enabled').onchange=()=>{a.markDirty('queue');a.renderSettings()};$('popup-scan-interval').oninput=()=>a.markDirty('queue');$('popup-scan-max-parallel').oninput=()=>a.markDirty('queue');$('queue-max').oninput=()=>a.markDirty('queue');$('queue-duration').oninput=()=>a.markDirty('queue');$('queue-delay').oninput=()=>a.markDirty('queue');
  $('game-place').addEventListener('input',()=>{a.markDirty('game');clearTimeout(a.gameLookupTimer());const place=$('game-place').value.trim();a.renderGameMap(place);if(/^\d{3,}$/.test(place))a.setGameLookupTimer(setTimeout(()=>a.loadGamePlace(place,true).catch(e=>a.renderGameMap(place,null,e.message)),650))});
  $('game-private').addEventListener('input',()=>a.markDirty('game'));
  $('game-private').addEventListener('blur',()=>{if($('game-place').value.trim())return;const place=a.placeFromPrivate($('game-private').value.trim());if(place){$('game-place').value=place;a.markDirty('game');a.loadGamePlace(place,true).catch(e=>a.renderGameMap(place,null,e.message))}});
  $('accounts-table').addEventListener('click',e=>{const btn=e.target.closest('[data-action]');if(btn){e.stopPropagation();if(btn.dataset.action==='delete')a.deleteAccount(btn.dataset.user);if(btn.dataset.action==='launch')a.launchAccount(btn.dataset.user);if(btn.dataset.action==='dedupe')a.dedupeAccount(btn.dataset.user);if(btn.dataset.action==='focus-captcha')a.focusCaptcha(btn.dataset.user);if(btn.dataset.action==='open-captcha-login')a.openCaptchaLogin(btn.dataset.user);if(btn.dataset.action==='resume-captcha')a.resumeCaptcha(btn.dataset.user);return}const row=e.target.closest('tr[data-user]');if(row){a.selectUser(row.dataset.user);a.renderAccountsTable()}});
  $('accounts-table').addEventListener('keydown',e=>{if(e.target.classList.contains('desc-input')&&e.key==='Enter'){e.preventDefault();e.target.blur()}});
  $('accounts-table').addEventListener('focusout',e=>{if(e.target.classList.contains('desc-input'))a.saveDescription(e.target.dataset.user,e.target.value).catch(err=>a.toast(err.message))});
}
