/* Cronus zoom lock (no build step).
 * Disables Ctrl+scroll / Ctrl +/-/0 / pinch zoom in WebView2 + browser fallback.
 * Normal scroll, Ctrl+C/V/A/X/Z and inputs are untouched.
 */
(function () {
  "use strict";

  function isZoomKey(e) {
    var k = e.key;
    if (k === "+" || k === "-" || k === "=" || k === "_" || k === "0") return true;
    // Numpad Add/Sub/0 + legacy keyCodes (107/109/96, 187/189/48)
    var c = e.keyCode || e.which || 0;
    if (c === 107 || c === 109 || c === 96 || c === 187 || c === 189 || c === 48) return true;
    var code = e.code || "";
    if (code === "NumpadAdd" || code === "NumpadSubtract" || code === "Numpad0" ||
        code === "Equal" || code === "Minus" || code === "Digit0") return true;
    return false;
  }

  // 1. Ctrl/Cmd + wheel (mouse scroll zoom) — the reported case.
  document.addEventListener("wheel", function (e) {
    try {
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        e.stopPropagation();
      }
    } catch (err) {}
  }, { passive: false, capture: true });

  // Legacy IE-style wheel event (some WebView2 builds still fire it).
  document.addEventListener("mousewheel", function (e) {
    try {
      if (e.ctrlKey || e.metaKey) e.preventDefault();
    } catch (err) {}
  }, { passive: false, capture: true });

  // 2. Ctrl/Cmd + (+/-/0) keyboard zoom.
  document.addEventListener("keydown", function (e) {
    try {
      if (!(e.ctrlKey || e.metaKey)) return;
      // Never break plain shortcuts: only swallow actual zoom keys.
      if (isZoomKey(e)) {
        e.preventDefault();
        e.stopPropagation();
      }
    } catch (err) {}
  }, true);

  // 3. Trackpad pinch (Safari/WebKit gesture events).
  ["gesturestart", "gesturechange", "gestureend"].forEach(function (t) {
    document.addEventListener(t, function (e) {
      try { e.preventDefault(); } catch (err) {}
    }, { passive: false, capture: true });
  });

  // 4. Touch pinch (two-finger): block scale, keep single-finger scroll.
  document.addEventListener("touchmove", function (e) {
    try {
      if (e.touches && e.touches.length > 1) e.preventDefault();
    } catch (err) {}
  }, { passive: false, capture: true });

  // 5. Safety: if zoom slipped through (browser fallback Ctrl+0 race),
  // reset page zoom factor where the API exists.
  function resetZoom() {
    try {
      if (document.body && document.body.style && document.body.style.zoom !== undefined) {
        if (document.body.style.zoom !== "" && document.body.style.zoom !== "1") {
          document.body.style.zoom = "1";
        }
      }
    } catch (err) {}
  }
  window.addEventListener("resize", resetZoom, { passive: true });
})();
