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
})();
