export function healthSeverity(health){
  if(!health||health.ok===false)return 'danger';
  const warnings=health.warnings||[];
  if(warnings.length)return 'warning';
  return 'ok';
}

export function createStatusRuntime({api,nextSequence,acceptStatus,setStreamState,loadAccountsConfig,render,loadAvatars,toast}){
  let live=false, reconcileTimer=0, fallbackTimer=0, eventSource=null;
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
  function stopFallbackPolling(){
    if(!fallbackTimer)return;
    clearInterval(fallbackTimer);
    fallbackTimer=0;
  }
  function startFallbackPolling(){
    if(fallbackTimer)return;
    fallbackTimer=setInterval(()=>{if(!live)manualSnapshot()},2500);
  }
  function closeStream(){
    if(eventSource){eventSource.close();eventSource=null}
    stopFallbackPolling();
    live=false;
  }
  function connectStream(){
    if(eventSource&&eventSource.readyState!==EventSource.CLOSED)return eventSource;
    if(!window.EventSource){
      setStreamState('polling');
      startFallbackPolling();
      manualSnapshot();
      return null;
    }
    const es=new EventSource('/api/stream');
    eventSource=es;
    setStreamState('connecting');
    es.addEventListener('snapshot',async ev=>{
      live=true;
      stopFallbackPolling();
      const nextStatus=JSON.parse(ev.data);
      if(!acceptStatus(nextStatus,0))return;
      setStreamState('live','stream online');
      await loadAccountsConfig();
      render();
      loadAvatars().catch(()=>{});
      ensureSlowReconcile();
    });
    es.onerror=()=>{live=false;setStreamState('reconnecting');startFallbackPolling()};
    window.addEventListener('beforeunload',closeStream,{once:true});
    manualSnapshot();
    return es;
  }
  return {manualSnapshot,connectStream,closeStream};
}
