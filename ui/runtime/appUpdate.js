/* Cronus Launcher self-update UI (classic script, no build step).
 * Talks to GET /api/update/status + POST /api/update/{check,download,install}.
 * Rendered into the sidebar next to #guard-btn; polls quietly in background.
 */
(function () {
  "use strict";

  var POLL_IDLE_MS = 60000;
  var POLL_BUSY_MS = 1000;
  var state = { snap: null, timer: 0, busy: false, settings: null };

  function token() {
    try {
      var m = document.querySelector('meta[name="cronus-api-token"]');
      return (m && m.content) || "";
    } catch (e) { return ""; }
  }

  function req(path, method, body) {
    var opt = { method: method || "GET", headers: {} };
    var t = token();
    if (t) opt.headers["X-Cronus-Token"] = t;
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    return fetch("/api" + path, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error((j && (j.detail || j.msg)) || r.statusText);
        return j;
      });
    });
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function ensureBar() {
    var bar = document.getElementById("cronus-update-bar");
    if (bar) return bar;
    bar = el("div", "cronus-update-bar");
    bar.id = "cronus-update-bar";
    bar.innerHTML =
      '<button class="cu-ver" id="cu-ver" title="Check for updates">…</button>' +
      '<span class="cu-dot" id="cu-dot"></span>' +
      '<div class="cu-progress" id="cu-progress"><div class="cu-progress-fill" id="cu-progress-fill"></div></div>' +
      '<div class="cu-actions" id="cu-actions"></div>';
    var anchor = document.querySelector(".side-launch");
    if (anchor && anchor.parentNode) {
      anchor.parentNode.insertBefore(bar, anchor.nextSibling);
    } else {
      var side = document.querySelector(".sidebar") || document.body;
      side.appendChild(bar);
    }
    document.getElementById("cu-ver").addEventListener("click", function () { manualCheck(); });
    return bar;
  }

  function clearActions() {
    var box = document.getElementById("cu-actions");
    if (box) box.innerHTML = "";
    return box;
  }

  function addButton(label, cls, handler, title) {
    var box = document.getElementById("cu-actions");
    if (!box) return null;
    var b = el("button", "cu-btn " + (cls || ""), label);
    if (title) b.title = title;
    b.addEventListener("click", handler);
    box.appendChild(b);
    return b;
  }

  function setDot(mode) {
    var d = document.getElementById("cu-dot");
    if (d) d.setAttribute("data-mode", mode || "idle");
  }

  function setProgress(percent, visible) {
    var wrap = document.getElementById("cu-progress");
    var fill = document.getElementById("cu-progress-fill");
    if (!wrap || !fill) return;
    wrap.style.display = visible ? "block" : "none";
    fill.style.width = Math.max(0, Math.min(100, Number(percent) || 0)) + "%";
  }

  function overlay(html) {
    closeOverlay();
    var back = el("div", "cu-overlay");
    back.id = "cu-overlay";
    var box = el("div", "cu-modal");
    box.innerHTML = html;
    back.appendChild(box);
    back.addEventListener("click", function (e) { if (e.target === back) closeOverlay(); });
    document.body.appendChild(back);
    return box;
  }

  function closeOverlay() {
    var o = document.getElementById("cu-overlay");
    if (o && o.parentNode) o.parentNode.removeChild(o);
  }

  function confirmInstall(snap, farmRunning) {
    var v = snap.latest_version || "";
    var warn = farmRunning
      ? '<p class="cu-warn">Farm is running. Installing will stop Auto Rejoin and close all Roblox windows.</p>'
      : '<p>App will exit now and relaunch on the new version.</p>';
    var box = overlay(
      '<h3>Install update v' + esc(v) + '?</h3>' + warn +
      '<div class="cu-modal-actions">' +
      '<button class="cu-btn" id="cu-cancel">Cancel</button>' +
      '<button class="cu-btn primary" id="cu-ok">Install &amp; Restart</button>' +
      '</div>'
    );
    box.querySelector("#cu-cancel").addEventListener("click", closeOverlay);
    box.querySelector("#cu-ok").addEventListener("click", function () {
      closeOverlay();
      doInstall();
    });
  }

  function openSettings() {
    var s = state.settings || {};
    var auto = s.auto_check_update !== false;
    var channel = (s.update_channel === "beta") ? "beta" : "stable";
    var box = overlay(
      '<h3>Update settings</h3>' +
      '<label class="cu-row"><input type="checkbox" id="cu-set-auto"' + (auto ? " checked" : "") + '> Check automatically</label>' +
      '<label class="cu-row">Channel <select id="cu-set-channel">' +
      '<option value="stable"' + (channel === "stable" ? " selected" : "") + '>stable</option>' +
      '<option value="beta"' + (channel === "beta" ? " selected" : "") + '>beta</option>' +
      '</select></label>' +
      '<div class="cu-modal-actions">' +
      '<button class="cu-btn" id="cu-cancel">Cancel</button>' +
      '<button class="cu-btn primary" id="cu-save">Save</button>' +
      '</div>'
    );
    box.querySelector("#cu-cancel").addEventListener("click", closeOverlay);
    box.querySelector("#cu-save").addEventListener("click", function () {
      var payload = {
        auto_check_update: !!box.querySelector("#cu-set-auto").checked,
        update_channel: box.querySelector("#cu-set-channel").value
      };
      req("/config", "POST", payload).then(function () {
        closeOverlay();
        refreshSettings().then(refresh);
      }).catch(function (e) { toast("Save failed: " + e.message); });
    });
  }

  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function toast(msg) {
    try {
      var t = document.getElementById("toast");
      if (!t) { alert(msg); return; }
      var n = el("div", "toast-item", msg);
      t.appendChild(n);
      setTimeout(function () { if (n.parentNode) n.parentNode.removeChild(n); }, 3200);
    } catch (e) { try { alert(msg); } catch (_) {} }
  }

  function render(snap) {
    ensureBar();
    state.snap = snap;
    var ver = document.getElementById("cu-ver");
    if (ver) ver.textContent = "v" + (snap.current_version || "?");
    clearActions();
    var st = snap.state || "idle";
    var available = !!snap.update_available;

    if (st === "downloading" || st === "verifying") {
      setDot("busy");
      setProgress(snap.progress_percent, true);
      var pct = el("span", "cu-note", (st === "verifying" ? "verifying… " : "") + (snap.progress_percent || 0) + "%");
      document.getElementById("cu-actions").appendChild(pct);
    } else {
      setProgress(0, false);
      setDot(available ? "available" : (st === "error" ? "error" : "idle"));
    }

    if (st === "checking") {
      document.getElementById("cu-actions").appendChild(el("span", "cu-note", "checking…"));
    } else if (st === "downloaded") {
      if (snap.compiled) {
        if (snap.can_install) {
          addButton("Restart", "primary", function () { confirmInstall(snap, !!snap.farm_running); }, "Install update and restart");
        } else if (snap.farm_running) {
          addButton("Restart", "", function () { toast("Stop Auto Rejoin first, then install."); }, "Stop Auto Rejoin first");
        } else {
          addButton("Restart", "primary", function () { confirmInstall(snap, false); }, "Install update and restart");
        }
      } else {
        addButton("Open file", "", function () { window.open(snap.latest_url || "/api/update/status", "_blank"); }, "Open downloaded release");
      }
    } else if (available) {
      if (snap.compiled) {
        addButton("Download", "primary", doDownload, "Download v" + (snap.latest_version || ""));
      } else {
        addButton("Download", "primary", function () { window.open(snap.latest_url || "#", "_blank"); }, "Open release page");
      }
      if (snap.latest_version) {
        var note = el("span", "cu-note", "v" + snap.latest_version + " ready");
        note.title = snap.latest_notes || "";
        document.getElementById("cu-actions").appendChild(note);
      }
    } else if (st === "error" && snap.error) {
      var en = el("span", "cu-note err", "update error");
      en.title = snap.error;
      document.getElementById("cu-actions").appendChild(en);
      addButton("Retry", "", manualCheck, "Check again");
    }
    addButton("⚙", "", openSettings, "Update settings");
  }

  function schedule(ms) {
    if (state.timer) clearTimeout(state.timer);
    state.timer = setTimeout(refresh, ms);
  }

  function refresh() {
    req("/update/status").then(function (snap) {
      render(snap);
      var st = (snap && snap.state) || "idle";
      schedule((st === "downloading" || st === "verifying" || st === "checking") ? POLL_BUSY_MS : POLL_IDLE_MS);
    }).catch(function () {
      schedule(POLL_IDLE_MS);
    });
  }

  function refreshSettings() {
    return req("/config").then(function (cfg) {
      state.settings = cfg || {};
      return state.settings;
    }).catch(function () { return {}; });
  }

  function manualCheck() {
    setDot("busy");
    req("/update/check", "POST", {}).then(function (snap) {
      render(snap);
      if (snap.update_available) toast("Update available: v" + snap.latest_version);
      else toast(snap.msg || "Up to date");
      schedule(POLL_IDLE_MS);
    }).catch(function (e) { toast("Check failed: " + e.message); schedule(POLL_IDLE_MS); });
  }

  function doDownload() {
    setDot("busy");
    setProgress(0, true);
    req("/update/download", "POST", {}).then(function (snap) {
      render(snap);
      if (!snap.ok) toast(snap.msg || "Download failed");
      refresh();
    }).catch(function (e) { toast("Download failed: " + e.message); refresh(); });
  }

  function doInstall() {
    req("/update/install", "POST", {}).then(function (snap) {
      toast((snap && snap.msg) || "Installing, app will exit…");
    }).catch(function (e) { toast("Install failed: " + e.message); refresh(); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    try {
      ensureBar();
      refreshSettings().then(refresh);
    } catch (e) { /* never break dashboard */ }
  });
})();
