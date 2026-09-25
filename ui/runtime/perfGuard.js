/* Cronus perf guard (no build step).
 * - Dedupes /game/place fetches with LRU(100) + 10min TTL so table
 *   re-renders don't refetch + re-decode the same thumbnails.
 * - Forces lazy + fixed size on game/avatar imgs to cut layout thrash.
 * - Caps stray popovers to 1. No visual change.
 * - Shared modern helpers for legacy scripts (no imports needed).
 */
(function () {
  "use strict";
  // __cronusStore(k,v): quota/private-mode-safe localStorage write.
  window.__cronusStore = function (k, v) {
    try { localStorage.setItem(k, v); } catch (e) {}
  };
  window.__cronusRemove = function (k) {
    try { localStorage.removeItem(k); } catch (e) {}
  };
  // __cronusDebounce(fn,ms): trailing-edge debounce for input handlers.
  window.__cronusDebounce = function (fn, ms) {
    var t = 0;
    return function () {
      var self = this, args = arguments;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(self, args); }, ms || 150);
    };
  };
  function cronusToken() {
    try {
      var m = document.querySelector('meta[name="cronus-api-token"]');
      return (m && m.content) || "";
    } catch (e) { return ""; }
  }
  try {
    var PLACE_TTL = 10 * 60 * 1000;
    var PLACE_MAX = 100;
    var placeCache = new Map();
    var inflight = new Map();
    function placeKey(url) {
      try {
        var u = new URL(url, location.origin);
        var m = u.pathname.match(/\/game\/place\/([^/?#]+)/);
        return m ? m[1] : "";
      } catch (e) { return ""; }
    }
    function prune() {
      var now = Date.now();
      for (var [k, v] of placeCache) {
        if (now - v.ts > PLACE_TTL) placeCache.delete(k);
      }
      while (placeCache.size > PLACE_MAX) {
        var first = placeCache.keys().next();
        if (first.done) break;
        placeCache.delete(first.value);
      }
    }
    var origFetch = window.fetch.bind(window);
    window.fetch = function (url, opt) {
      try {
        var s = String((typeof url === "string" ? url : (url && url.url)) || "");
        // Converge the 6 per-file api() copies: same-origin /api/* calls that
        // forgot X-Cronus-Token get it here. Never overrides an explicit one.
        if (s.indexOf("/api/") >= 0) {
          try {
            var u = new URL(s, location.origin);
            if (u.origin === location.origin) {
              var hasTok = false;
              if (opt && opt.headers) {
                if (typeof opt.headers.has === "function") hasTok = opt.headers.has("X-Cronus-Token");
                else hasTok = !!opt.headers["X-Cronus-Token"];
              }
              if (!hasTok) {
                var tok = cronusToken();
                if (tok) {
                  opt = opt || {};
                  if (opt.headers && typeof opt.headers.set === "function") {
                    opt.headers.set("X-Cronus-Token", tok);
                  } else {
                    opt.headers = Object.assign({}, opt.headers, { "X-Cronus-Token": tok });
                  }
                }
              }
            }
          } catch (e) {}
        }
        var pid = s.indexOf("/game/place/") >= 0 ? placeKey(s) : "";
        var method = String((opt && opt.method) || "GET").toUpperCase();
        if (!pid || method !== "GET") return origFetch(url, opt);
        prune();
        var hit = placeCache.get(pid);
        if (hit && Date.now() - hit.ts < PLACE_TTL) {
          return Promise.resolve(new Response(JSON.stringify(hit.data), {
            status: 200,
            headers: { "Content-Type": "application/json" }
          }));
        }
        if (inflight.has(pid)) return inflight.get(pid).then(function (d) {
          return new Response(JSON.stringify(d), {
            status: 200, headers: { "Content-Type": "application/json" }
          });
        });
        // Single parse: one clone().json() feeds both cache and waiters.
        var dp = origFetch(url, opt).then(function (r) {
          return r.clone().json().catch(function () { return null; }).then(function (d) {
            inflight.delete(pid);
            if (d) {
              placeCache.set(pid, { data: d, ts: Date.now() });
              prune();
            }
            return { resp: r, data: d };
          });
        }).catch(function (e) { inflight.delete(pid); throw e; });
        inflight.set(pid, dp.then(function (x) { return x.data; }));
        return dp.then(function (x) { return x.resp; });
      } catch (e) {
        return origFetch(url, opt);
      }
    };
  } catch (e) {}
  function handleAdded(root) {
    try {
      var imgs = root.querySelectorAll
        ? root.querySelectorAll("img.game-thumb,img.avatar-img,img.map-thumb,img.ogame-thumb")
        : [];
      for (var im of imgs) {
        try {
          if (im.loading !== "lazy") im.loading = "lazy";
          if (im.decoding !== "async") im.decoding = "async";
          if (!im.width) im.width = 27;
          if (!im.height) im.height = 27;
        } catch (e) {}
      }
    } catch (e) {}
  }
  function capPopovers() {
    try {
      var existing = document.querySelectorAll(".online-popover");
      for (var i = 1; i < existing.length; i++) {
        try { existing[i].remove(); } catch (e) {}
      }
    } catch (e) {}
  }
  try {
    var _pgQueued = false;
    var mo = new MutationObserver(function () {
      if (_pgQueued || document.hidden) return;
      _pgQueued = true;
      (window.requestAnimationFrame || setTimeout)(function () {
        _pgQueued = false;
        try {
          if (document.hidden) return;
          handleAdded(document.documentElement);
          capPopovers();
        } catch (e) {}
      });
    });
    mo.observe(document.documentElement, { childList: true, subtree: true });
  } catch (e) {}
  function initResourceMonitor() {
    var cards = document.getElementById("res-cards");
    var toggle = document.getElementById("res-cards-toggle");
    if (!cards || !toggle || toggle.dataset.resMonitorWired === "1") return;
    toggle.dataset.resMonitorWired = "1";
    var colors = { cpu: "#8b93f8", ram: "#22c55e", virt: "#60a5fa" };
    var history = { cpu: [], ram: [], virt: [] };
    var timer = 0;
    var inFlight = false;
    function byId(id) { return document.getElementById(id); }
    function shouldPoll() {
      return !cards.hidden && !document.hidden && !!document.querySelector("#view-accounts.active");
    }
    function setStale(value) { cards.classList.toggle("is-stale", !!value); }
    function push(key, value) {
      var values = history[key];
      values.push(value);
      while (values.length > 60) values.shift();
    }
    function drawSpark(id, key) {
      var canvas = byId(id);
      if (!canvas) return;
      var values = history[key];
      var dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
      var width = canvas.clientWidth || canvas.parentElement && canvas.parentElement.clientWidth || 200;
      var height = 36;
      var pixelWidth = Math.round(width * dpr);
      var pixelHeight = Math.round(height * dpr);
      if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
        canvas.width = pixelWidth;
        canvas.height = pixelHeight;
      }
      var context = canvas.getContext("2d");
      context.clearRect(0, 0, canvas.width, canvas.height);
      context.scale(dpr, dpr);
      if (values.length < 2) return;
      var step = width / 59;
      var offset = (60 - values.length) * step;
      context.beginPath();
      values.forEach(function (value, index) {
        var x = offset + index * step;
        var y = height - 3 - Math.max(0, Math.min(100, value)) / 100 * (height - 8);
        if (index === 0) context.moveTo(x, y);
        else context.lineTo(x, y);
      });
      context.strokeStyle = colors[key];
      context.lineWidth = 1.6;
      context.lineJoin = "round";
      context.lineCap = "round";
      context.stroke();
      var lastX = offset + (values.length - 1) * step;
      var lastY = height - 3 - Math.max(0, Math.min(100, values[values.length - 1])) / 100 * (height - 8);
      context.beginPath();
      context.arc(lastX - 1, lastY, 2.4, 0, Math.PI * 2);
      context.fillStyle = colors[key];
      context.fill();
    }
    function render(data) {
      var cpu = Number(data.cpu_percent);
      var ram = Number(data.ram_percent);
      var virtual = Number(data.virt_percent);
      push("cpu", cpu);
      push("ram", ram);
      push("virt", virtual);
      byId("res-cpu-pct").textContent = Math.round(cpu) + "%";
      byId("res-ram-pct").textContent = Math.round(ram) + "%";
      byId("res-virt-pct").textContent = Math.round(virtual) + "%";
      byId("res-cpu-bar").style.transform = "scaleX(" + Math.max(0, Math.min(100, cpu)) / 100 + ")";
      byId("res-ram-bar").style.transform = "scaleX(" + Math.max(0, Math.min(100, ram)) / 100 + ")";
      byId("res-virt-bar").style.transform = "scaleX(" + Math.max(0, Math.min(100, virtual)) / 100 + ")";
      byId("res-cpu-sub").textContent = data.cpu_threads + " threads";
      byId("res-ram-sub").textContent = data.ram_used_gb + " / " + data.ram_total_gb + " GB";
      byId("res-virt-sub").textContent = data.virt_used_gb + " / " + data.virt_total_gb + " GB";
      drawSpark("res-cpu-spark", "cpu");
      drawSpark("res-ram-spark", "ram");
      drawSpark("res-virt-spark", "virt");
      setStale(false);
    }
    function poll() {
      if (inFlight || !shouldPoll()) return;
      inFlight = true;
      fetch("/api/system/resources")
        .then(function (response) {
          if (!response.ok) throw new Error("HTTP " + response.status);
          return response.json();
        })
        .then(function (data) {
          if (data && data.ok !== false) render(data);
          else setStale(true);
        })
        .catch(function () { setStale(true); })
        .finally(function () { inFlight = false; });
    }
    function apply(visible) {
      cards.hidden = !visible;
      toggle.classList.toggle("active", visible);
      toggle.setAttribute("aria-pressed", String(visible));
      try { localStorage.setItem("rg_res_cards", visible ? "1" : "0"); } catch (e) {}
      if (visible) setTimeout(poll, 50);
    }
    apply(localStorage.getItem("rg_res_cards") !== "0");
    toggle.addEventListener("click", function () { apply(cards.hidden); });
    timer = setInterval(function () { if (shouldPoll()) poll(); }, 2000);
    window.addEventListener("resize", function () {
      drawSpark("res-cpu-spark", "cpu");
      drawSpark("res-ram-spark", "ram");
      drawSpark("res-virt-spark", "virt");
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initResourceMonitor);
  else initResourceMonitor();
})();
