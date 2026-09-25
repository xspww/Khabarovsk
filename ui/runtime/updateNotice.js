/* One-click in-app updater (no build step).
 * Button flow: check farm state -> confirm modal if running -> POST
 * /api/update/apply -> poll /api/update/status for progress -> the app
 * exits and a hidden swap script relaunches the new exe.
 * Source runs refuse apply; then we fall back to opening Releases.
 * Failures are quiet except on the button itself - never annoy the user.
 */
(function () {
  "use strict";

  var POLL_MS = 15 * 60 * 1000;
  var STATUS_POLL_MS = 1000;
  var timer = 0;
  var statusTimer = 0;
  var working = false;
  var lastRefreshAt = 0;

  function token() {
    try {
      var m = document.querySelector('meta[name="cronus-api-token"]');
      return (m && m.content) || "";
    } catch (e) { return ""; }
  }

  function apiHeaders(json) {
    var h = {};
    if (json) h["Content-Type"] = "application/json";
    var t = token();
    if (t) h["X-Cronus-Token"] = t;
    return h;
  }

  function get(path) {
    return fetch(path, { headers: apiHeaders(false) }).then(function (r) {
      return r.json().catch(function () { return {}; });
    });
  }

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: apiHeaders(true),
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; });
    });
  }

  function button() { return document.getElementById("cronus-update-btn"); }

  function removeButton() {
    var b = button();
    if (!b) return;
    // Keep static placeholder but hide it so CSS stays loaded and
    // the next ensureButton does not need to recreate the node.
    try {
      b.hidden = true;
      b.style.display = "none";
      b.classList.remove("is-available", "is-downloading", "is-failed");
    } catch (e) {
      if (b && b.parentNode) b.parentNode.removeChild(b);
    }
  }

  function ensureButton() {
    var b = button();
    if (b) {
      try { b.hidden = false; b.style.display = ""; } catch (e) {}
      return b;
    }
    b = document.createElement("button");
    b.id = "cronus-update-btn";
    b.className = "cronus-update-btn";
    var anchor = document.querySelector(".side-launch");
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(b, anchor.nextSibling);
    else (document.querySelector(".sidebar") || document.body).appendChild(b);
    return b;
  }

  function setLabel(text, title, disabled) {
    var b = ensureButton();
    b.disabled = !!disabled;
    // Preserve arrow icon when the label is the update-available state;
    // render() sets innerHTML with the icon, so text updates use textContent.
    if (b.classList.contains("is-available") && text.charAt(0) !== "\u21E9") {
      b.innerHTML = "<span>&#8659; " + esc(text) + "</span>";
    } else {
      b.textContent = text;
    }
    if (title) b.title = title;
  }

  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function closeModal() {
    try {
      var m = document.getElementById("modal-backdrop");
      if (m) m.hidden = true;
    } catch (e) {}
  }

  function confirmModal(version, onYes) {
    closeModal();
    try {
      document.getElementById("modal-title").textContent = "Update to v" + version + "?";
      document.getElementById("modal-body").innerHTML =
        "<div>The farm is running. Updating will <b>stop the farm</b> and restart the app into the new version.</div>";
      var foot = document.getElementById("modal-foot");
      foot.innerHTML = "";
      var no = document.createElement("button");
      no.className = "btn ghost";
      no.textContent = "Cancel";
      no.onclick = closeModal;
      var yes = document.createElement("button");
      yes.className = "btn good";
      yes.textContent = "Update";
      yes.onclick = function () { closeModal(); onYes(); };
      foot.appendChild(no);
      foot.appendChild(yes);
      document.getElementById("modal-backdrop").hidden = false;
    } catch (e) {
      onYes();
    }
  }

  function openReleases(fallbackUrl) {
    var opt = { method: "POST", headers: apiHeaders(true) };
    fetch("/api/update/open", opt).catch(function () {});
    if (fallbackUrl) {
      try { window.open(fallbackUrl, "_blank", "noopener"); } catch (e) {}
    }
  }

  var overlayVersion = "";
  function setProgress(pct) {
    // Sidebar button fill + overlay bar share one value.
    try {
      var b = button();
      if (b && pct != null) {
        b.style.setProperty("--p", pct + "%");
        b.classList.add("is-downloading");
      }
      var fill = document.getElementById("cronus-updating-fill");
      if (fill && pct != null) fill.style.width = pct + "%";
      var pctEl = document.getElementById("cronus-updating-pct");
      if (pctEl && pct != null) pctEl.textContent = pct + "%";
    } catch (e) {}
  }
  function progressPct(job) {
    var m = /(\d{1,3})\s*%/.exec(job.progress || "");
    if (m) return Math.max(0, Math.min(100, parseInt(m[1], 10)));
    if (job.state === "verifying") return 100;
    return null;
  }
  function showUpdatingOverlay(version) {
    if (version) overlayVersion = version;
    if (document.getElementById("cronus-updating-overlay")) return;
    try {
      var st = document.createElement("style");
      st.textContent = "#cronus-updating-overlay{position:fixed;inset:0;z-index:99999;display:flex;align-items:center;justify-content:center;background:rgba(11,12,16,.94);animation:cronus-ov-in .25s ease}"
        + "@keyframes cronus-ov-in{from{opacity:0}to{opacity:1}}"
        + "#cronus-updating-overlay .cronus-updating-box{width:min(420px,calc(100vw - 48px));background:#0f1117;border:1px solid rgba(99,102,241,.35);border-radius:14px;padding:28px 30px;animation:cronus-box-in .35s cubic-bezier(.16,1,.3,1)}"
        + "@keyframes cronus-box-in{from{opacity:0;transform:translateY(14px) scale(.99)}to{opacity:1;transform:none}}"
        + "#cronus-updating-overlay .cronus-updating-prompt{font-family:var(--mono,monospace);font-size:12px;color:#6366f1;margin-bottom:10px}"
        + "#cronus-updating-overlay .cronus-updating-title{font-size:17px;font-weight:800;color:#f2f3f5;letter-spacing:-.01em}"
        + "#cronus-updating-overlay .cronus-updating-track{position:relative;height:8px;border-radius:99px;background:#23252d;margin-top:18px;overflow:hidden}"
        + "#cronus-updating-overlay .cronus-updating-fill{height:100%;width:0%;border-radius:99px;background:#6366f1;transition:width .4s ease;overflow:hidden;position:relative}"
        + "#cronus-updating-overlay .cronus-updating-fill:after{content:'';position:absolute;inset:0;background:linear-gradient(100deg,transparent 20%,rgba(255,255,255,.35) 50%,transparent 80%);animation:cronus-shimmer 1.4s linear infinite}"
        + "@keyframes cronus-shimmer{from{transform:translateX(-100%)}to{transform:translateX(100%)}}"
        + "#cronus-updating-overlay .cronus-updating-row{display:flex;align-items:center;justify-content:space-between;margin-top:10px;font-family:var(--mono,monospace);font-size:11.5px;color:#8d9099}"
        + "#cronus-updating-overlay .cronus-updating-dots i{display:inline-block;width:5px;height:5px;border-radius:50%;background:#6366f1;margin-left:4px;animation:cronus-blink 1.2s infinite}"
        + "#cronus-updating-overlay .cronus-updating-dots i:nth-child(2){animation-delay:.2s}"
        + "#cronus-updating-overlay .cronus-updating-dots i:nth-child(3){animation-delay:.4s}"
        + "@keyframes cronus-blink{0%,100%{opacity:.25}50%{opacity:1}}"
        + "#cronus-updating-overlay .cronus-updating-sub{font-size:12px;color:#6c7079;margin-top:12px;line-height:1.6}";
      document.head.appendChild(st);
      var ov = document.createElement("div");
      ov.id = "cronus-updating-overlay";
      ov.innerHTML = '<div class="cronus-updating-box">'
        + '<div class="cronus-updating-prompt">&gt; cronus update</div>'
        + '<div class="cronus-updating-title">Updating to v' + esc(overlayVersion || "") + '…</div>'
        + '<div class="cronus-updating-track"><div class="cronus-updating-fill" id="cronus-updating-fill"></div></div>'
        + '<div class="cronus-updating-row"><span id="cronus-updating-pct">0%</span><span class="cronus-updating-dots"><i></i><i></i><i></i></span></div>'
        + '<div class="cronus-updating-sub">The app restarts itself when the download lands. A terminal window shows progress until it is back.</div></div>';
      document.body.appendChild(ov);
    } catch (e) {}
  }

  function pollStatus() {
    if (statusTimer) clearTimeout(statusTimer);
    statusTimer = 0;
    get("/api/update/status").then(function (snap) {
      var job = (snap && snap.job) || {};
      if (job.state === "restarting") showUpdatingOverlay(job.version);
      if (!job.active) {
        if (job.state === "failed") {
          setLabel("Update failed - retry", job.error || job.msg, false);
          try { button().classList.add("is-failed"); } catch (e) {}
          working = false;
          schedule();
          return;
        }
        if (job.state === "done") {
          setLabel("Updated", "", true);
          working = false;
          return;
        }
        working = false;
        schedule();
        return;
      }
      var label = job.progress || job.state || "updating";
      setLabel(label.charAt(0).toUpperCase() + label.slice(1) + "…", job.msg || "", true);
      setProgress(progressPct(job));
      // Hidden tab: 5s cadence is enough for a progress bar nobody sees.
      statusTimer = setTimeout(pollStatus, document.hidden ? 5000 : STATUS_POLL_MS);
    }).catch(function () {
      // Server gone mid-poll: either restarting into the new version (good)
      // or something died. Assume reboot, show the loader, stop polling.
      showUpdatingOverlay("");
      setLabel("Restarting…", "The app is restarting into the new version", true);
      working = false;
    });
  }

  function applyUpdate(version, latestUrl, confirmStop) {
    if (working) return;
    working = true;
    setLabel("Starting…", "", true);
    post("/api/update/apply", { confirm_stop_farm: !!confirmStop }).then(function (res) {
      res = res || {};
      if (res.need_confirm_stop) {
        working = false;
        confirmModal(version, function () { applyUpdate(version, latestUrl, true); });
        renderLatest();
        return;
      }
      if (!res.ok || !res.accepted) {
        working = false;
        if (/source run/i.test(res.msg || "")) {
          openReleases(latestUrl); // dev runs keep the old open-releases path
          renderLatest();
          return;
        }
        setLabel("Update failed - retry", res.msg || "", false);
        try { button().classList.add("is-failed"); } catch (e) {}
        schedule();
        return;
      }
      try { button().classList.remove("is-failed"); } catch (e) {}
      showUpdatingOverlay(version);
      pollStatus();
    }).catch(function () {
      working = false;
      setLabel("Update failed - retry", "", false);
      schedule();
    });
  }

  var latestSnap = null;
  function renderLatest() {
    if (latestSnap) render(latestSnap);
  }

  function onButton(version, latestUrl) {
    get("/api/status/lite").then(function (s) {
      if (s && s.running) {
        confirmModal(version, function () { applyUpdate(version, latestUrl, true); });
      } else {
        applyUpdate(version, latestUrl, false);
      }
    }).catch(function () {
      applyUpdate(version, latestUrl, false);
    });
  }

  function render(snap) {
    latestSnap = snap;
    if (working) return;
    if (!snap || !snap.update_available || !snap.latest_version) {
      removeButton();
      // Surface the check_error in console and as a tooltip on the
      // hidden placeholder so power users can diagnose why no update
      // is shown (offline / rate-limited). The button stays hidden.
      if (snap && snap.check_error) {
        try { console.warn("[update] check_error:", snap.check_error); } catch (e) {}
        var ph = button();
        if (ph) ph.title = snap.check_error;
      }
      return;
    }
    var b = ensureButton();
    b.disabled = false;
    b.hidden = false;
    b.style.display = "";
    b.classList.remove("is-downloading", "is-failed");
    b.classList.add("is-available");
    b.style.setProperty("--p", "0%");
    b.innerHTML = "<span>&#8659; v" + esc(snap.latest_version) + "</span>";
    b.title = "Update to v" + snap.latest_version + " (downloads and restarts the app)";
    b.onclick = function () { onButton(snap.latest_version, snap.latest_url); };
  }

  var failCount = 0;
  function schedule() {
    if (timer) clearTimeout(timer);
    // A failed check retries soon (1m, 2m, ... capped at 15m); successes
    // re-check every POLL_MS (15m) so a new release shows within minutes.
    var wait = failCount > 0 ? Math.min(15 * 60 * 1000, 60000 * failCount) : POLL_MS;
    timer = setTimeout(refresh, wait);
  }

  function refresh() {
    lastRefreshAt = Date.now();
    get("/api/update/check").then(function (snap) {
      failCount = 0;
      try { render(snap); } catch (e) {}
      schedule();
    }).catch(function () {
      failCount++;
      schedule();
    });
  }

  function bootRefresh() {
    try { refresh(); } catch (e) {}
  }
  // DOMContentLoaded may have already fired (script injected at end of body
  // after the module script). Handle both cases so the first check is never
  // missed on WebView2 where readyState can already be interactive/complete.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootRefresh);
  } else {
    // Defer one tick so ensureButton finds .side-launch reliably.
    setTimeout(bootRefresh, 50);
  }
  // App left open across a release: re-check when the window is focused
  // or becomes visible again instead of waiting for the next poll.
  // Previously this only fired when the button was missing, so a transient
  // failure would never recover without a reload. Now we re-check whenever
  // at least 60s has passed since the last attempt.
  document.addEventListener("visibilitychange", function () {
    try {
      if (!document.hidden && !working) {
        var age = Date.now() - lastRefreshAt;
        if (!button() || age > 60000) refresh();
        else if (latestSnap && latestSnap.check_error) refresh();
      }
    } catch (e) {}
  });
  window.addEventListener("focus", function () {
    try {
      if (!working) {
        var age2 = Date.now() - lastRefreshAt;
        if (age2 > 60000) refresh();
      }
    } catch (e) {}
  });
})();
