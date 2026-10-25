/* Cronus zoom lock (no build step).
 * Disables Ctrl+scroll / Ctrl +/-/0 / pinch zoom in WebView2 + browser fallback.
 * Normal scroll, Ctrl+C/V/A/X/Z and inputs are untouched.
 */
"use strict";
const zoomLock = {};

// Key names, Numpad Add/Sub/0, and legacy keyCodes (107/109/96, 187/189/48).
zoomLock.KEY_NAMES = ["+", "-", "=", "_", "0"];
zoomLock.KEY_CODES = [107, 109, 96, 187, 189, 48];
zoomLock.KEY_IDS = [
  "NumpadAdd",
  "NumpadSubtract",
  "Numpad0",
  "Equal",
  "Minus",
  "Digit0",
];

zoomLock.isZoomKey = function (e) {
  if (zoomLock.KEY_NAMES.includes(e.key)) return true;
  const code = e.keyCode || e.which || 0;
  if (zoomLock.KEY_CODES.includes(code)) return true;
  return zoomLock.KEY_IDS.includes(e.code || "");
};

// 1. Ctrl/Cmd + wheel (mouse scroll zoom) — the reported case.
zoomLock.onWheel = function (e) {
  try {
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      e.stopPropagation();
    }
  } catch (err) {}
};

// Legacy IE-style wheel event (some WebView2 builds still fire it).
zoomLock.onMouseWheel = function (e) {
  try {
    if (e.ctrlKey || e.metaKey) e.preventDefault();
  } catch (err) {}
};

// 2. Ctrl/Cmd + (+/-/0) keyboard zoom.
zoomLock.onKeyDown = function (e) {
  try {
    if (!(e.ctrlKey || e.metaKey)) return;
    // Never break plain shortcuts: only swallow actual zoom keys.
    if (zoomLock.isZoomKey(e)) {
      e.preventDefault();
      e.stopPropagation();
    }
  } catch (err) {}
};

// 3. Trackpad pinch (Safari/WebKit gesture events).
zoomLock.blockGestures = function () {
  ["gesturestart", "gesturechange", "gestureend"].forEach(function (type) {
    document.addEventListener(
      type,
      function (e) {
        try {
          e.preventDefault();
        } catch (err) {}
      },
      { passive: false, capture: true },
    );
  });
};

// 4. Touch pinch (two-finger): block scale, keep single-finger scroll.
zoomLock.onTouchMove = function (e) {
  try {
    if (e.touches && e.touches.length > 1) e.preventDefault();
  } catch (err) {}
};

// 5. Safety: if zoom slipped through (browser fallback Ctrl+0 race),
// reset page zoom factor where the API exists.
zoomLock.resetZoom = function () {
  try {
    const style = document.body && document.body.style;
    if (!style || style.zoom === undefined) return;
    if (style.zoom !== "" && style.zoom !== "1") style.zoom = "1";
  } catch (err) {}
};

document.addEventListener("wheel", zoomLock.onWheel, {
  passive: false,
  capture: true,
});
document.addEventListener("mousewheel", zoomLock.onMouseWheel, {
  passive: false,
  capture: true,
});
document.addEventListener("keydown", zoomLock.onKeyDown, true);
zoomLock.blockGestures();
document.addEventListener("touchmove", zoomLock.onTouchMove, {
  passive: false,
  capture: true,
});
window.addEventListener("resize", zoomLock.resetZoom, { passive: true });
