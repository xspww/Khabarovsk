/* Notify-only update notice (opencode-style, no build step).
 * The old self-updater (download + exe swap via visible batch script) was
 * removed: spawning console/curl windows looked like malware.
 * This script only checks and shows a button that opens the Releases page.
 * Failures are silent — never annoy the user.
 */
(function () {
  "use strict";

  var POLL_MS = 6 * 60 * 60 * 1000;
  var timer = 0;

  function token() {
    try {
      var m = document.querySelector('meta[name="cronus-api-token"]');
      return (m && m.content) || "";
    } catch (e) { return ""; }
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

  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function openReleases(fallbackUrl) {
    // The desktop window (QWebEngineView) ignores window.open(), so ask the
    // backend to open the OS browser (it re-checks the URL itself, the
    // client-supplied value is only a fallback for real-browser mode).
    var opt = { method: "POST", headers: { "Content-Type": "application/json" } };
    var t = token();
    if (t) opt.headers["X-Cronus-Token"] = t;
    fetch("/api/update/open", opt).catch(function () {});
    if (fallbackUrl) {
      try { window.open(fallbackUrl, "_blank", "noopener"); } catch (e) {}
    }
  }

  function render(snap) {
    if (!snap || !snap.update_available || !snap.latest_version || !snap.latest_url) {
      removeButton();
      return;
    }
    var b = ensureButton();
    b.disabled = false;
    b.innerHTML = "<span>&#8659; v" + esc(snap.latest_version) + "</span>";
    b.title = "Open the Releases page to download v" + snap.latest_version;
    b.onclick = function () {
      openReleases(snap.latest_url);
    };
  }

  function schedule() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(refresh, POLL_MS);
  }

  function refresh() {
    var opt = { headers: {} };
    var t = token();
    if (t) opt.headers["X-Cronus-Token"] = t;
    fetch("/api/update/check", opt).then(function (r) {
      return r.json().catch(function () { return {}; });
    }).then(function (snap) {
      try { render(snap); } catch (e) {}
      schedule();
    }).catch(function () { schedule(); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    try { refresh(); } catch (e) {}
  });
})();
