export function healthSeverity(health){
  if(!health||health.ok===false)return 'danger';
  const warnings=health.warnings||[];
  if(warnings.length)return 'warning';
  return 'ok';
}

export function createStatusRuntime({api,nextSequence,acceptStatus,setStreamState,loadAccountsConfig,render,loadAvatars,toast}){
  let live=false, reconcileTimer=0, fallbackTimer=0;
  async function manualSnapshot(){
    const seq=nextSequence();
    try{
      const nextStatus=await api('/status');
      if(!acceptStatus(nextStatus,seq))return;
      setStreamState('synced','stream online');
      await loadAccountsConfig();
      render();
      loadAvatars().catch(()=>{});
    }catch(e){
      toast(e.message);
    }
  }
  function ensureSlowReconcile(){
    if(reconcileTimer)return;
    reconcileTimer=setInterval(()=>{if(live)manualSnapshot()},15000);
  }
  function startFallbackPolling(){
    if(fallbackTimer)return;
    fallbackTimer=setInterval(()=>{if(!live)manualSnapshot()},2500);
  }
  function connectStream(){
    if(!window.EventSource){
      setStreamState('polling');
      fallbackTimer=setInterval(manualSnapshot,1500);
      manualSnapshot();
      return null;
    }
    const es=new EventSource('/api/stream');
    setStreamState('connecting');
    es.addEventListener('snapshot',async ev=>{
      live=true;
      const nextStatus=JSON.parse(ev.data);
      if(!acceptStatus(nextStatus,0))return;
      setStreamState('live','stream online');
      await loadAccountsConfig();
      render();
      loadAvatars().catch(()=>{});
      ensureSlowReconcile();
    });
    es.onerror=()=>{live=false;setStreamState('reconnecting');startFallbackPolling()};
    manualSnapshot();
    return es;
  }
  return {manualSnapshot,connectStream};
}
