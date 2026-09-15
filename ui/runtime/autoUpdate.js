/* Product-grade self-update button (classic script, no build step).
 * Single-download story: background check -> background download ->
 * one-click Restart (auto-stops farm, swaps exe, probes, rolls back).
 * Source runs never show anything. Program Files installs fall back to
 * a manual download link. Mandatory releases block START via banner.
 */
(function () {
  "use strict";

  var POLL_IDLE_MS = 60000;
  var POLL_BUSY_MS = 2000;
  var timer = 0;
  var lastErrorShown = "";
  var lastInstallShown = "";
  var lastInstallToastAt = 0;
  var autoPopupVersion = "";

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

  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function toast(msg, isError) {
    try {
      var host = document.getElementById("toast");
      if (!host) return;
      while (host.children.length >= 4) host.firstElementChild.remove();
      var item = document.createElement("div");
      item.className = "toast-item toast-" + (isError ? "error" : "success");
      var icon = document.createElement("span");
      icon.className = "toast-icon";
      icon.innerHTML = isError
        ? '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#f87171"/><path d="M15 9l-6 6M9 9l6 6" stroke="white" stroke-width="1.9" stroke-linecap="round"/></svg>'
        : '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#6366f1"/><path d="M8.2 12.2l2.6 2.6 4.5-5" stroke="white" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      var label = document.createElement("span");
      label.className = "toast-text";
      label.textContent = msg;
      item.append(icon, label);
      host.appendChild(item);
      requestAnimationFrame(function () { item.classList.add("show"); });
      setTimeout(function () {
        item.classList.remove("show");
        item.classList.add("hide");
        setTimeout(function () { item.remove(); }, 200);
      }, 3800);
    } catch (e) {}
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

  function shortNotes(notes, maxLen) {
    var t = String(notes || "").replace(/\r/g, "").trim();
    if (!t) return "";
    maxLen = maxLen || 280;
    if (t.length > maxLen) return t.slice(0, maxLen - 1).trim() + "…";
    return t;
  }

  function confirmInstall(snap) {
    var version = snap.latest_version || "";
    var mandatory = !!snap.mandatory;
    var willStop = !!snap.farm_running;
    var back = document.createElement("div");
    back.className = "cu-overlay";
    back.id = "cu-overlay";
    var box = document.createElement("div");
    box.className = "cu-modal";
    var notes = shortNotes(snap.latest_notes);
    var html = "<h3>" + (mandatory ? "Required update v" : "Install update v") + esc(version) + "?</h3>";
    if (mandatory) html += '<p class="cu-mandatory">This update is marked required.</p>';
    html += "<p>App will exit now, install, then relaunch on the new version. Data and accounts are kept.</p>";
    if (willStop) html += '<p class="cu-warn">Auto Rejoin is running — it will stop and all Roblox windows will close.</p>';
    if (notes) html += '<pre class="cu-notes">' + esc(notes) + "</pre>";
    if (snap.latest_url) html += '<p><a class="cu-link" href="' + esc(snap.latest_url) + '" target="_blank" rel="noopener">View release notes</a></p>';
    html += '<div class="cu-modal-actions"><button class="cu-btn" id="cu-cancel">Later</button>'
      + '<button class="cu-btn primary" id="cu-ok">Install &amp; Restart</button></div>';
    box.innerHTML = html;
    back.appendChild(box);
    back.addEventListener("click", function (e) { if (e.target === back) back.remove(); });
    document.body.appendChild(back);
    box.querySelector("#cu-cancel").addEventListener("click", function () { back.remove(); });
    box.querySelector("#cu-ok").addEventListener("click", function () {
      back.remove();
      req("/update/install", "POST", {}).then(function (s) {
        toast((s && s.msg) || "Installing, app will exit…", false);
      }).catch(function (e) { toast("Install failed: " + e.message, true); refresh(); });
    });
  }

  function showManualButton(snap) {
    var b = ensureButton();
    b.disabled = false;
    b.classList.add("is-manual");
    b.innerHTML = "<span>⇩ Update v" + esc(snap.latest_version || "") + " (manual)</span>";
    b.title = "Install folder is not writable — download manually";
    b.onclick = function () {
      var url = snap.latest_url || snap.manual_url || "";
      if (url) { try { window.open(url, "_blank", "noopener"); } catch (e) {} }
      else toast("Open the Releases page to download the new exe.", true);
    };
  }

  function announceLastInstall(snap) {
    try {
      var li = snap.last_install;
      if (!li || typeof li !== "object") return;
      var key = JSON.stringify([li.ok, li.action, li.version]);
      if (!key || key === lastInstallShown) return;
      // Only announce real install/rollback outcomes, not empty {}.
      if (!li.action) return;
      lastInstallShown = key;
      var now = Date.now();
      if (now - lastInstallToastAt < 5000) return;
      lastInstallToastAt = now;
      if (li.ok) toast("Updated to v" + (li.version || "") + " — relaunch complete.", false);
      else if (li.action === "rolled_back") toast("Update v" + (li.version || "") + " failed health check — rolled back safely.", true);
      else toast("Last update: " + (li.action || "failed"), true);
    } catch (e) {}
  }

  function render(snap) {
    announceLastInstall(snap);
    if (!snap || !snap.compiled) { removeButton(); return; }
    // Manual fallback wins: exe folder not writable (e.g. Program Files).
    if (snap.needs_manual_install && snap.update_available) { showManualButton(snap); return; }
    if (!snap.update_available) { removeButton(); return; }
    var st = snap.state || "idle";
    if (st === "downloaded" || snap.pending_restart || snap.can_install) {
      var b = ensureButton();
      b.disabled = false;
      b.classList.toggle("is-mandatory", !!snap.mandatory);
      b.innerHTML = "<span>" + (snap.mandatory ? "⚠ Restart to update v" : "↻ Update v") + esc(snap.latest_version || "") + "</span>";
      b.title = snap.farm_running
        ? "Install update and restart (will stop Auto Rejoin)"
        : "Install update and restart";
      b.onclick = function () { confirmInstall(snap); };
      // Product: auto_install_update=True pops the confirm once per version
      // (never force-exits). Default False = badge only, user clicks.
      try {
        var v = String(snap.latest_version || "");
        if (snap.auto_install_update && v && autoPopupVersion !== v && !document.getElementById("cu-overlay")) {
          autoPopupVersion = v;
          confirmInstall(snap);
        }
      } catch (e) {}
    } else if (st === "downloading" || st === "verifying" || st === "available" || st === "checking" || st === "installing") {
      var p = ensureButton();
      p.disabled = true;
      p.classList.remove("is-mandatory");
      var pct = Math.max(0, Math.min(100, Number(snap.progress_percent) || 0));
      var label = "↓ " + pct + "%";
      if (st === "available" || st === "checking") label = "↓ preparing…";
      else if (st === "verifying") label = "✓ verifying…";
      else if (st === "installing") label = "↻ installing…";
      p.innerHTML = "<span>" + esc(label) + "</span>";
      p.title = "Downloading update…";
      p.onclick = null;
      if (st === "available") {
        req("/update/download", "POST", {}).catch(function () {});
      }
    } else if (st === "error") {
      if (snap.error && snap.error !== lastErrorShown) {
        lastErrorShown = snap.error;
        toast("Update failed: " + snap.error, true);
      }
      // Keep a retry entry when an update is still available.
      if (snap.update_available && snap.latest_url) { showManualButton(snap); }
      else removeButton();
    } else {
      removeButton();
    }
  }

  function schedule(ms) {
    if (timer) clearTimeout(timer);
    timer = setTimeout(refresh, ms);
  }

  function refresh() {
    req("/update/status").then(function (snap) {
      render(snap);
      var busy = snap && (snap.state === "downloading" || snap.state === "verifying" || snap.state === "checking" || snap.state === "installing");
      schedule(busy ? POLL_BUSY_MS : POLL_IDLE_MS);
    }).catch(function () { schedule(POLL_IDLE_MS); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    try { refresh(); } catch (e) {}
  });
})();
