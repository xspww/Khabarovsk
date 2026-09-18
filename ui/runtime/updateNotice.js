/* One-click in-app updater (no build step).
 * Button flow: check farm state -> confirm modal if running -> POST
 * /api/update/apply -> poll /api/update/status for progress -> the app
 * exits and a hidden swap script relaunches the new exe.
 * Source runs refuse apply; then we fall back to opening Releases.
 * Failures are quiet except on the button itself - never annoy the user.
 */
(function () {
  "use strict";

  var POLL_MS = 6 * 60 * 60 * 1000;
  var STATUS_POLL_MS = 1000;
  var timer = 0;
  var statusTimer = 0;
  var working = false;

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
    if (b && b.parentNode) b.parentNode.removeChild(b);
  }

  function ensureButton() {
    var b = button();
    if (b) return b;
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
    b.textContent = text;
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

  function pollStatus() {
    if (statusTimer) clearTimeout(statusTimer);
    statusTimer = 0;
    get("/api/update/status").then(function (snap) {
      var job = (snap && snap.job) || {};
      if (!job.active) {
        if (job.state === "failed") {
          setLabel("Update failed - retry", job.error || job.msg, false);
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
      statusTimer = setTimeout(pollStatus, STATUS_POLL_MS);
    }).catch(function () {
      // Server gone mid-poll: either restarting into the new version (good)
      // or something died. Assume reboot, stop polling quietly.
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
        schedule();
        return;
      }
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
    get("/api/status").then(function (s) {
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
      return;
    }
    var b = ensureButton();
    b.disabled = false;
    b.innerHTML = "<span>&#8659; v" + esc(snap.latest_version) + "</span>";
    b.title = "Update to v" + snap.latest_version + " (downloads and restarts the app)";
    b.onclick = function () { onButton(snap.latest_version, snap.latest_url); };
  }

  function schedule() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(refresh, POLL_MS);
  }

  function refresh() {
    get("/api/update/check").then(function (snap) {
      try { render(snap); } catch (e) {}
      schedule();
    }).catch(function () { schedule(); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    try { refresh(); } catch (e) {}
  });
})();
