function createStatusState() {
  return {
    streaming: false,
    liveTimer: 0,
    tickTimer: 0,
    source: null,
    fails: 0,
    reconnectTimer: 0,
    listenersBound: false,
  };
}

function stopTickTimer(rt) {
  const state = rt.state;
  if (!state.tickTimer) return;
  clearInterval(state.tickTimer);
  state.tickTimer = 0;
}

async function manualStatusSnapshot(rt) {
  if (document.hidden) return;
  const seq = rt.nextSequence();
  try {
    const payload = await rt.api("/status");
    if (!rt.acceptStatus(payload, seq)) return;
    rt.setStreamState("synced", "stream online");
    await rt.loadAccountsConfig();
    rt.render();
    rt.loadAvatars().catch(() => {
      /* intentional: no-op */
    });
    rt.state.fails = 0;
  } catch (err) {
    rt.toast(err.message);
  }
}

function startPollingFallback(rt) {
  const state = rt.state;
  if (state.tickTimer) return;
  state.tickTimer = setInterval(() => {
    if (!state.streaming && !document.hidden) manualStatusSnapshot(rt);
  }, 15e3);
}

function scheduleReconnect(rt) {
  const state = rt.state;
  if (state.reconnectTimer) return;
  const wait = Math.min(30e3, 1e3 * Math.pow(2, state.fails));
  state.fails++;
  state.reconnectTimer = setTimeout(() => {
    state.reconnectTimer = 0;
    if (!state.streaming && !document.hidden) connectStatusStream(rt);
  }, wait);
}

function closeStatusStream(rt) {
  const state = rt.state;
  if (state.source) {
    state.source.close();
    state.source = null;
  }
  stopTickTimer(rt);
  state.streaming = false;
  if (state.liveTimer) {
    clearInterval(state.liveTimer);
    state.liveTimer = 0;
  }
  if (state.reconnectTimer) {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = 0;
  }
}

async function handleStatusSnapshot(rt, event) {
  if (document.hidden) return;
  const state = rt.state;
  state.streaming = true;
  stopTickTimer(rt);
  state.fails = 0;
  try {
    const payload = JSON.parse(event.data);
    if (!rt.acceptStatus(payload, 0)) return;
    rt.setStreamState("live", "stream online");
    await rt.loadAccountsConfig();
    rt.render();
    rt.loadAvatars().catch(() => {
      /* intentional: no-op */
    });
    if (!state.liveTimer) {
      state.liveTimer = setInterval(() => {
        if (state.streaming && !document.hidden) manualStatusSnapshot(rt);
      }, 12e4);
    }
  } catch (_) {
    /* intentional: malformed snapshot */
  }
}

function handleStreamError(rt, source) {
  const state = rt.state;
  state.streaming = false;
  try {
    source.close();
  } catch (_) {}
  if (state.source === source) state.source = null;
  rt.setStreamState("reconnecting");
  startPollingFallback(rt);
  scheduleReconnect(rt);
}

function connectStatusStream(rt) {
  const state = rt.state;
  if (state.source && state.source.readyState !== EventSource.CLOSED)
    return state.source;
  if (!window.EventSource) {
    rt.setStreamState("polling");
    startPollingFallback(rt);
    manualStatusSnapshot(rt);
    return null;
  }
  const source = new EventSource("/api/stream");
  state.source = source;
  rt.setStreamState("connecting");
  source.addEventListener("snapshot", (event) => {
    handleStatusSnapshot(rt, event);
  });
  source.onerror = () => {
    handleStreamError(rt, source);
  };
  manualStatusSnapshot(rt);
  return source;
}

function bindStatusListeners(rt) {
  const state = rt.state;
  if (state.listenersBound) return;
  state.listenersBound = true;
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.streaming) manualStatusSnapshot(rt);
  });
  window.addEventListener("beforeunload", () => closeStatusStream(rt), {
    once: true,
  });
}

export function createStatusRuntime({
  api,
  nextSequence,
  acceptStatus,
  setStreamState,
  loadAccountsConfig,
  render,
  loadAvatars,
  toast,
}) {
  const rt = {
    api,
    nextSequence,
    acceptStatus,
    setStreamState,
    loadAccountsConfig,
    render,
    loadAvatars,
    toast,
    state: createStatusState(),
  };
  return {
    manualSnapshot: () => manualStatusSnapshot(rt),
    connectStream: function () {
      bindStatusListeners(rt);
      return connectStatusStream(rt);
    },
    closeStream: () => closeStatusStream(rt),
  };
}
