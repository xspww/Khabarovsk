export function renderSettingsPanel(ctx){
  const {$,CONFIG,dirty,setCompactToggle,updateSaveState}=ctx;
  if(!CONFIG)return;

  const active=document.activeElement?.id||'';
  const gameIds=['game-private','game-place','game-auto-private-enabled','game-multi-roblox'];
  const queueIds=[
    'queue-max',
    'queue-duration',
    'queue-delay',
    'queue-autoclose-enabled',
    'queue-autoclose-minutes',
    'popup-disconnected-enabled',
    'popup-scan-interval',
    'popup-scan-max-parallel'
  ];

  if(!dirty('game')&&!gameIds.includes(active)){
    $('game-private').value=CONFIG.game_private_server_url||'';
    $('game-place').value=CONFIG.game_place_id||'';
    $('game-auto-private-enabled').checked=!!CONFIG.auto_create_private_server_enabled;
    $('game-multi-roblox').checked=CONFIG.multi_roblox_enabled!==false;
  }

  if(!dirty('queue')&&!queueIds.includes(active)){
    $('queue-max').value=CONFIG.max_concurrent_accounts??40;
    $('queue-duration').value=CONFIG.queue_duration_seconds??15;
    $('queue-delay').value=CONFIG.queue_delay_seconds??CONFIG.launch_rate_interval??15;
    $('queue-autoclose-enabled').checked=!!CONFIG.auto_close_enabled;
    $('queue-autoclose-minutes').value=CONFIG.auto_close_minutes??0;
    $('popup-disconnected-enabled').checked=CONFIG.popup_disconnected_enabled!==false;
    $('popup-scan-interval').value=CONFIG.popup_scan_interval_seconds??30;
    $('popup-scan-max-parallel').value=CONFIG.popup_scan_max_parallel??2;
  }

  const popupOn=!!$('popup-disconnected-enabled').checked;
  setCompactToggle('queue-autoclose-enabled','autoclose-inline-label','autoclose-controls','Every');
  $('popup-disconnected-controls').hidden=!popupOn;
  updateSaveState('game');
  updateSaveState('queue');
}

export function renderPerformancePanel(ctx){
  const {$,PERF,dirty,setCompactToggle,updateSaveState}=ctx;
  if(!$('fps-enabled'))return;

  const active=document.activeElement?.id||'';
  if(!dirty('performance')&&!['fps-enabled','fps-limit'].includes(active)){
    $('fps-enabled').checked=!!PERF.enabled;
    $('fps-limit').value=PERF.fps_limit??PERF.framerate_cap??240;
  }

  setCompactToggle('fps-enabled','fps-inline-label','fps-limit-field','Limit');
  const notice=$('fps-notice'),msg=PERF.warning||PERF.msg||'';
  notice.textContent=msg;
  notice.classList.toggle('show',!!msg&&msg!=='ok');
  updateSaveState('performance');
}

export function renderGraphicsPanel(ctx){
  const {$,GRAPHICS,dirty,setCompactToggle,updateSaveState}=ctx;
  if(!$('graphics-auto-enabled'))return;

  const active=document.activeElement?.id||'';
  const graphicsEnabled=!!(GRAPHICS.graphics_low_enabled??GRAPHICS.graphics_auto_enabled);
  if(!dirty('graphics')&&!['graphics-auto-enabled','graphics-quality','priority-enabled','process-priority'].includes(active)){
    $('graphics-auto-enabled').checked=graphicsEnabled;
    $('graphics-quality').value=GRAPHICS.graphics_quality_level??GRAPHICS.graphics_quality_level_current??1;
    $('priority-enabled').checked=!!GRAPHICS.auto_process_priority_enabled;
    $('process-priority').value=GRAPHICS.process_priority||'low';
  }

  setCompactToggle('graphics-auto-enabled','graphics-quality-label','graphics-quality-controls','Level');
  $('priority-controls').hidden=!$('priority-enabled').checked;
  const notice=$('graphics-notice'),msg=GRAPHICS.warning||GRAPHICS.msg||'';
  notice.textContent=msg;
  notice.classList.toggle('show',!!msg&&msg!=='ok');
  updateSaveState('graphics');
}

export function renderWindowSizePanel(ctx){
  const {$,WINDOW_SIZE,WINDOW_SIZE_PRESETS,dirty,updateSaveState}=ctx;
  if(!$('window-size-enabled'))return;

  const active=document.activeElement?.id||'';
  const ids=[
    'window-size-enabled',
    'window-size-preset',
    'window-size-width',
    'window-size-height',
    'window-arrange-enabled',
    'window-arrange-columns',
    'window-arrange-gap'
  ];

  if(!dirty('window-size')&&!ids.includes(active)){
    $('window-size-enabled').checked=!!WINDOW_SIZE.enabled;
    $('window-size-preset').value=WINDOW_SIZE.preset||'640x480';
    $('window-size-width').value=WINDOW_SIZE.width??640;
    $('window-size-height').value=WINDOW_SIZE.height??480;
    $('window-arrange-enabled').checked=!!WINDOW_SIZE.arrange_enabled;
    $('window-arrange-columns').value=WINDOW_SIZE.arrange_columns??6;
    $('window-arrange-gap').value=WINDOW_SIZE.arrange_gap??2;
  }

  const enabled=!!$('window-size-enabled').checked;
  const preset=$('window-size-preset').value;
  const arrange=!!$('window-arrange-enabled').checked;
  $('window-size-controls').hidden=!enabled;
  $('window-size-custom').hidden=!enabled||preset!=='custom';
  $('window-arrange-controls').hidden=!enabled||!arrange;

  if(enabled&&preset!=='custom'&&WINDOW_SIZE_PRESETS[preset]&&!['window-size-width','window-size-height'].includes(active)){
    const pair=WINDOW_SIZE_PRESETS[preset];
    $('window-size-width').value=pair[0];
    $('window-size-height').value=pair[1];
  }

  const notice=$('window-size-notice'),msg=WINDOW_SIZE.msg||'';
  notice.textContent=msg;
  notice.classList.toggle('show',!!msg&&msg!=='ok');
  updateSaveState('window-size');
}

export function renderCpuLimiterPanel(ctx){
  const {$,CPU_LIMITER,dirty,updateSaveState,esc}=ctx;
  if(!$('cpu-enabled'))return;

  const active=document.activeElement?.id||'';
  const ids=['cpu-enabled','cpu-mode','cpu-default-limit','cpu-apply-all'];
  if(!dirty('cpu-limiter')&&!ids.includes(active)&&!active.startsWith('cpu-row-')){
    $('cpu-enabled').checked=!!CPU_LIMITER.enabled;
    $('cpu-mode').value=CPU_LIMITER.mode||'hard';
    $('cpu-default-limit').value=CPU_LIMITER.default_limit_percent??20;
    $('cpu-apply-all').checked=CPU_LIMITER.apply_all!==false;
  }

  const enabled=!!$('cpu-enabled').checked;
  const applyAll=!!$('cpu-apply-all').checked;
  const defaultLimit=Number($('cpu-default-limit').value)||Number(CPU_LIMITER.default_limit_percent)||20;
  $('cpu-controls').hidden=!enabled;

  const rows=Array.isArray(CPU_LIMITER.rows)?CPU_LIMITER.rows:[];
  $('cpu-account-rows').innerHTML=rows.map(row=>{
    const user=row.username||'';
    const disabled=!enabled||applyAll;
    const pid=row.pid||'-';
    const status=row.status||'Pending';
    const msg=row.message||'';
    const rowEnabled=applyAll||!!row.enabled;
    const rowLimit=applyAll?defaultLimit:(row.limit_percent??defaultLimit);
    return `<tr><td><div class="name">${esc(row.display||user)}</div><div class="handle">${esc(user)}</div></td><td>${esc(pid)}</td><td><input id="cpu-row-enabled-${esc(user)}" class="cpu-row-enabled" data-user="${esc(user)}" type="checkbox" ${rowEnabled?'checked':''} ${disabled?'disabled':''}></td><td><input id="cpu-row-limit-${esc(user)}" class="input cpu-row-limit" data-user="${esc(user)}" type="number" min="5" max="95" step="0.01" value="${esc(Number(rowLimit).toFixed(2))}" ${disabled?'disabled':''}></td><td><strong>${esc(status)}</strong><div class="handle">${esc(msg)}</div></td></tr>`;
  }).join('')||'<tr><td colspan="5" style="height:90px;text-align:center;color:var(--muted)">No accounts.</td></tr>';

  const notice=$('cpu-notice');
  const failed=rows.filter(r=>r.status==='Failed').length;
  const fallback=rows.filter(r=>r.status==='Fallback').length;
  const msg=CPU_LIMITER.msg||(failed?`${failed} failed`:fallback?`${fallback} fallback`:'');
  notice.textContent=msg;
  notice.classList.toggle('show',!!msg);
  updateSaveState('cpu-limiter');
}

export function renderTroubleshootPanel(ctx){
  const {$,TROUBLESHOOT}=ctx;
  if(!$('roblox-installed-version'))return;

  const installed=TROUBLESHOOT.installed||{};
  const job=TROUBLESHOOT.job||{};
  const blocked=!!TROUBLESHOOT.running_blocked;
  const active=!!job.active;
  $('roblox-installed-version').textContent=installed.installed?(installed.version||'Installed'):'Not installed';
  $('roblox-install-state').textContent=job.state||'Ready';
  $('roblox-install-progress').textContent=job.progress||job.msg||'-';

  const notice=$('roblox-install-notice');
  const msg=TROUBLESHOOT.msg||job.error||job.msg||'';
  notice.textContent=blocked?(TROUBLESHOOT.block_msg||'Stop Cronus and close Roblox first.'):msg;
  notice.classList.toggle('show',blocked||!!(job.error||TROUBLESHOOT.msg));
  ['roblox-uninstall','roblox-latest'].forEach(id=>{if($(id))$(id).disabled=blocked||active});
}

export function resetGamePanel(ctx){
  ctx.clearDirty('game');
  renderSettingsPanel(ctx);
}

export function resetQueuePanel(ctx){
  ctx.clearDirty('queue');
  renderSettingsPanel(ctx);
}

export function resetPerformancePanel(ctx){
  ctx.clearDirty('performance');
  renderPerformancePanel(ctx);
}

export function resetGraphicsPanel(ctx){
  ctx.clearDirty('graphics');
  renderGraphicsPanel(ctx);
}

export function resetWindowSizePanel(ctx){
  ctx.clearDirty('window-size');
  renderWindowSizePanel(ctx);
}

export function resetCpuLimiterPanel(ctx){
  ctx.clearDirty('cpu-limiter');
  renderCpuLimiterPanel(ctx);
}

export async function saveGamePanel(ctx){
  const {$,api,clearDirty,toast,loadConfig,manualSnapshot}=ctx;
  const body={
    game_private_server_url:$('game-private').value.trim(),
    game_place_id:$('game-place').value.trim(),
    auto_create_private_server_enabled:$('game-auto-private-enabled').checked,
    auto_create_private_server_free_only:true,
    multi_roblox_enabled:$('game-multi-roblox').checked
  };
  const r=await api('/config','POST',body);
  clearDirty('game');
  toast('Game saved'+(r.game_defaults_applied?` (${r.game_defaults_applied} defaulted)`:''));
  await loadConfig();
  await manualSnapshot();
}

export async function saveQueuePanel(ctx){
  const {$,api,clearDirty,toast,loadConfig,manualSnapshot}=ctx;
  const body={
    max_concurrent_accounts:Number($('queue-max').value)||1,
    queue_duration_seconds:Number($('queue-duration').value)||0,
    queue_delay_seconds:Number($('queue-delay').value)||0,
    auto_close_enabled:$('queue-autoclose-enabled').checked,
    auto_close_minutes:Number($('queue-autoclose-minutes').value)||0,
    popup_disconnected_enabled:$('popup-disconnected-enabled').checked,
    popup_scan_interval_seconds:Number($('popup-scan-interval').value)||30,
    popup_scan_max_parallel:Number($('popup-scan-max-parallel').value)||2
  };
  await api('/config','POST',body);
  clearDirty('queue');
  toast('Queue saved');
  await loadConfig();
  await manualSnapshot();
}

export async function savePerformancePanel(ctx){
  const {$,api,clearDirty,toast,loadConfig,loadGraphics,renderPerformance}=ctx;
  const body={
    enabled:$('fps-enabled').checked,
    fps_limit:Number($('fps-limit').value)||240
  };

  try{
    const next=await api('/performance/fps-limiter','POST',body);
    ctx.PERF=next;
    clearDirty('performance');
    renderPerformance(next);
    toast(next.warning||'FPS saved');
    if(next.warning)$('fps-notice').textContent=next.warning;
    await loadConfig();
    await loadGraphics();
    return next;
  }catch(e){
    $('fps-notice').textContent=e.message;
    $('fps-notice').classList.add('show');
    toast(e.message);
    return ctx.PERF;
  }
}

export async function saveGraphicsPanel(ctx){
  const {$,api,clearDirty,toast,loadConfig,renderGraphics}=ctx;
  const enabled=$('graphics-auto-enabled').checked;
  const body={
    graphics_low_enabled:enabled,
    graphics_auto_enabled:enabled,
    graphics_quality_level:Number($('graphics-quality').value)||1,
    auto_process_priority_enabled:$('priority-enabled').checked,
    process_priority:$('process-priority').value
  };

  try{
    const next=await api('/performance/graphics','POST',body);
    ctx.GRAPHICS=next;
    clearDirty('graphics');
    renderGraphics(next);
    toast(next.warning||'Graphics saved');
    if(next.warning)$('graphics-notice').textContent=next.warning;
    await loadConfig();
    return next;
  }catch(e){
    $('graphics-notice').textContent=e.message;
    $('graphics-notice').classList.add('show');
    toast(e.message);
    return ctx.GRAPHICS;
  }
}

export async function saveWindowSizePanel(ctx){
  const {$,api,clearDirty,toast,loadConfig,renderWindowSize}=ctx;
  const preset=$('window-size-preset').value;
  const body={
    enabled:$('window-size-enabled').checked,
    preset:preset,
    width:Number($('window-size-width').value)||640,
    height:Number($('window-size-height').value)||480,
    arrange_enabled:$('window-arrange-enabled').checked,
    arrange_columns:Number($('window-arrange-columns').value)||6,
    arrange_gap:Number($('window-arrange-gap').value)||2
  };

  try{
    const next=await api('/performance/window-size','POST',body);
    ctx.WINDOW_SIZE=next;
    clearDirty('window-size');
    await loadConfig();
    renderWindowSize(next);
    toast(next.msg||'Window size saved');
    return next;
  }catch(e){
    $('window-size-notice').textContent=e.message;
    $('window-size-notice').classList.add('show');
    toast(e.message);
    return ctx.WINDOW_SIZE;
  }
}

export function collectCpuRowsPanel(ctx){
  const {$}=ctx;
  if($('cpu-apply-all').checked)return[];
  return Array.from(document.querySelectorAll('#cpu-account-rows tr')).map(row=>{
    const enabled=row.querySelector('.cpu-row-enabled');
    const limit=row.querySelector('.cpu-row-limit');
    const user=enabled?.dataset.user||limit?.dataset.user||'';
    return user?{
      username:user,
      enabled:!!enabled?.checked,
      limit_percent:Number(limit?.value)||20
    }:null;
  }).filter(Boolean);
}

export async function saveCpuLimiterPanel(ctx){
  const {$,api,clearDirty,toast,renderCpuLimiter}=ctx;
  const body={
    enabled:$('cpu-enabled').checked,
    mode:$('cpu-mode').value,
    default_limit_percent:Number($('cpu-default-limit').value)||20,
    apply_all:$('cpu-apply-all').checked,
    accounts:collectCpuRowsPanel(ctx)
  };

  try{
    const next=await api('/performance/cpu-limiter','POST',body);
    ctx.CPU_LIMITER=next;
    clearDirty('cpu-limiter');
    renderCpuLimiter(next);
    toast('CPU limiter saved');
    return next;
  }catch(e){
    $('cpu-notice').textContent=e.message;
    $('cpu-notice').classList.add('show');
    toast(e.message);
    return ctx.CPU_LIMITER;
  }
}
