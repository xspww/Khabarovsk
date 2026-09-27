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
    var MAX_POINTS = 60;
    // Static mode: no animation anywhere. New data redraws instantly.
    var history = { cpu: [], ram: [], virt: [] };
    var timer = 0;
    var inFlight = false;
    function byId(id) { return document.getElementById(id); }
    function clampPct(v) {
      v = Number(v);
      if (!isFinite(v)) return 0;
      return Math.max(0, Math.min(100, v));
    }
    function shouldPoll() {
      return !cards.hidden && !document.hidden && !!document.querySelector("#view-accounts.active");
    }
    function setStale(value) { cards.classList.toggle("is-stale", !!value); }
    function push(key, value) {
      var values = history[key];
      var v = clampPct(value);
      if (!values.length) {
        // Seed two identical points so a flat line shows on first sample.
        values.push(v, v);
        return;
      }
      values.push(v);
      while (values.length > MAX_POINTS) values.shift();
    }
    function layoutPoints(values, width, height) {
      var n = values.length;
      var pts = new Array(n);
      var span = height - 10;
      for (var i = 0; i < n; i++) {
        var x = n <= 1 ? width - 2 : (i / (n - 1)) * (width - 4) + 2;
        var y = height - 4 - clampPct(values[i]) / 100 * span;
        pts[i] = [x, y];
      }
      return pts;
    }
    function strokeSmooth(context, pts) {
      var n = pts.length;
      context.moveTo(pts[0][0], pts[0][1]);
      if (n === 2) {
        context.lineTo(pts[1][0], pts[1][1]);
        return;
      }
      for (var i = 0; i < n - 1; i++) {
        var p0 = pts[Math.max(0, i - 1)];
        var p1 = pts[i];
        var p2 = pts[i + 1];
        var p3 = pts[Math.min(n - 1, i + 2)];
        var c1x = p1[0] + (p2[0] - p0[0]) / 6;
        var c1y = p1[1] + (p2[1] - p0[1]) / 6;
        var c2x = p2[0] - (p3[0] - p1[0]) / 6;
        var c2y = p2[1] - (p3[1] - p1[1]) / 6;
        context.bezierCurveTo(c1x, c1y, c2x, c2y, p2[0], p2[1]);
      }
    }
    function hexToRgba(hex, alpha) {
      try {
        var r = parseInt(hex.slice(1, 3), 16);
        var g = parseInt(hex.slice(3, 5), 16);
        var b = parseInt(hex.slice(5, 7), 16);
        return "rgba(" + r + "," + g + "," + b + "," + alpha + ")";
      } catch (e) { return hex; }
    }
    function drawSpark(id, key) {
      var canvas = byId(id);
      if (!canvas) return;
      var values = history[key];
      var dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
      var width = canvas.clientWidth || (canvas.parentElement && canvas.parentElement.clientWidth) || 200;
      var height = 36;
      var pixelWidth = Math.round(width * dpr);
      var pixelHeight = Math.round(height * dpr);
      if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
        canvas.width = pixelWidth;
        canvas.height = pixelHeight;
      }
      var context = canvas.getContext("2d");
      context.setTransform(dpr, 0, 0, dpr, 0, 0);
      context.clearRect(0, 0, width, height);
      if (values.length < 2) return;
      var pts = layoutPoints(values, width, height);
      var color = colors[key];
      // Soft area fill under the line (product look, no blink).
      try {
        context.beginPath();
        strokeSmooth(context, pts);
        context.lineTo(pts[pts.length - 1][0], height + 2);
        context.lineTo(pts[0][0], height + 2);
        context.closePath();
        var grad = context.createLinearGradient(0, 0, 0, height);
        grad.addColorStop(0, hexToRgba(color, 0.22));
        grad.addColorStop(1, hexToRgba(color, 0));
        context.fillStyle = grad;
        context.fill();
      } catch (e) {}
      // Main line — redrawn instantly on each update.
      context.beginPath();
      strokeSmooth(context, pts);
      context.strokeStyle = color;
      context.lineWidth = 1.8;
      context.lineJoin = "round";
      context.lineCap = "round";
      context.stroke();
    }
    function drawAll() {
      drawSpark("res-cpu-spark", "cpu");
      drawSpark("res-ram-spark", "ram");
      drawSpark("res-virt-spark", "virt");
    }
    // No animation loop — static redraw on data only.
    function setBar(barId, pct) {
      var bar = byId(barId);
      if (!isFinite(pct)) pct = 0;
      var clamped = Math.max(0, Math.min(100, pct));
      if (bar) bar.style.width = clamped + "%";
    }
    function render(data) {
      var cpu = clampPct(data.cpu_percent);
      var ram = clampPct(data.ram_percent);
      var virtual = clampPct(data.virt_percent);
      push("cpu", cpu);
      push("ram", ram);
      push("virt", virtual);
      var cpuPct = byId("res-cpu-pct");
      var ramPct = byId("res-ram-pct");
      var virtPct = byId("res-virt-pct");
      if (cpuPct) cpuPct.textContent = Math.round(cpu) + "%";
      if (ramPct) ramPct.textContent = Math.round(ram) + "%";
      if (virtPct) virtPct.textContent = Math.round(virtual) + "%";
      setBar("res-cpu-bar", cpu);
      setBar("res-ram-bar", ram);
      setBar("res-virt-bar", virtual);
      var cpuSub = byId("res-cpu-sub");
      var ramSub = byId("res-ram-sub");
      var virtSub = byId("res-virt-sub");
      if (cpuSub) cpuSub.textContent = data.cpu_threads + " threads";
      if (ramSub) ramSub.textContent = data.ram_used_gb + " / " + data.ram_total_gb + " GB";
      if (virtSub) virtSub.textContent = data.virt_used_gb + " / " + data.virt_total_gb + " GB";
      setStale(false);
      drawAll();
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
    window.addEventListener("resize", drawAll);
    // First paint so cards never look dead.
    drawAll();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initResourceMonitor);
  else initResourceMonitor();
})();
