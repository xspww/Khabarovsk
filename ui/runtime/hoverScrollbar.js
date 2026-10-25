/* Cronus overlay scrollbars (no build step).
 * Native scrollbars are fully hidden (zero layout space, zero gutter).
 * .nav/.main: draws a floating thumb at the pane's right edge, shown only
 *   when the cursor is at that edge or while dragging it. Scroll events
 *   only reposition the thumb, they never show or hide it.
 * Inner panes (.table-wrap, .modal-body, ...): native scrollbar kept for
 *   layout, but thumb is transparent until JS adds .show-bar when the cursor
 *   is at the scrollbar edge. So it shows only on edge-hover, never on
 *   plain pane-hover and never while scrolling elsewhere.
 * Thumb is draggable (for .nav/.main).
 */
"use strict";
const edgeScroll = {};

edgeScroll.EDGE_PX = 14;
edgeScroll.SELECTORS = [".nav", ".main"];
// Inner scroll areas: show native thumb only when cursor is at the
// vertical (right 14px) or horizontal (bottom 14px) scrollbar edge.
edgeScroll.INNER_SELECTORS = [
  ".table-wrap",
  ".custom-select-menu",
  ".modal-body",
  ".lua-copy-box",
  ".lua-code-box",
  ".online-popover",
  ".manual-progress-list",
  ".selected-close-list",
  ".account-filters"
];
edgeScroll.instances = [];

edgeScroll.showEdge = function (inst, on) {
  try { inst.track.classList.toggle("show-edge", !!on); } catch (e) {}
};

edgeScroll.makeParentRelative = function (parent) {
  try {
    if (getComputedStyle(parent).position === "static") parent.style.position = "relative";
  } catch (e) {}
};

edgeScroll.place = function (inst) {
  try {
    let h = inst.el.clientHeight - 12;
    if (h < 40) { inst.track.style.display = "none"; return; }
    inst.track.style.display = "";
    inst.track.style.top = inst.el.offsetTop + 6 + "px";
    inst.track.style.height = h + "px";
  } catch (e) {}
};

edgeScroll.refresh = function (inst) {
  edgeScroll.place(inst);
  try {
    let sh = inst.el.scrollHeight, ch = inst.el.clientHeight;
    if (sh <= ch + 1) { inst.track.style.display = "none"; return; }
    if (inst.track.style.display === "none") edgeScroll.place(inst);
    if (inst.track.style.display === "none") return;
    let th = inst.track.clientHeight || 1;
    let h = Math.max(24, Math.min(th, (ch / sh) * th));
    let maxTop = Math.max(1, th - h);
    let ratio = sh - ch > 0 ? inst.el.scrollTop / (sh - ch) : 0;
    ratio = Math.min(1, Math.max(0, ratio));
    inst.thumb.style.height = h + "px";
    inst.thumb.style.top = ratio * maxTop + "px";
  } catch (e) {}
};

edgeScroll.bindElement = function (inst) {
  inst.el.addEventListener("mousemove", function (e) {
    if (inst.dragging) return;
    try {
      let r = inst.el.getBoundingClientRect();
      edgeScroll.showEdge(inst, e.clientX >= r.right - edgeScroll.EDGE_PX);
    } catch (err) {}
  });
  inst.el.addEventListener("mouseleave", function () {
    if (!inst.dragging) edgeScroll.showEdge(inst, false);
  });
  inst.el.addEventListener("scroll", function () { edgeScroll.refresh(inst); }, { passive: true });
};

edgeScroll.bindThumb = function (inst) {
  inst.dragHandler = function (e) { edgeScroll.onDrag(inst, e); };
  inst.upHandler = function () { edgeScroll.endDrag(inst); };
  inst.thumb.addEventListener("mouseenter", function () { edgeScroll.showEdge(inst, true); });
  inst.thumb.addEventListener("mouseleave", function () {
    if (!inst.dragging) edgeScroll.showEdge(inst, false);
  });
  inst.thumb.addEventListener("mousedown", function (e) {
    edgeScroll.startDrag(inst, e);
  });
};

edgeScroll.startDrag = function (inst, e) {
  try {
    e.preventDefault();
    inst.dragging = true;
    inst.dragStartY = e.clientY;
    inst.dragStartTop = parseFloat(inst.thumb.style.top) || 0;
    edgeScroll.showEdge(inst, true);
    document.addEventListener("mousemove", inst.dragHandler);
    document.addEventListener("mouseup", inst.upHandler);
  } catch (err) {}
};

edgeScroll.onDrag = function (inst, e) {
  if (!inst.dragging) return;
  try {
    let th = inst.track.clientHeight || 1;
    let h = inst.thumb.offsetHeight || 24;
    let maxTop = Math.max(1, th - h);
    let t = Math.min(maxTop, Math.max(0, inst.dragStartTop + (e.clientY - inst.dragStartY)));
    let sh = inst.el.scrollHeight, ch = inst.el.clientHeight;
    inst.el.scrollTop = (t / maxTop) * (sh - ch);
  } catch (err) {}
};

edgeScroll.endDrag = function (inst) {
  try {
    inst.dragging = false;
    document.removeEventListener("mousemove", inst.dragHandler);
    document.removeEventListener("mouseup", inst.upHandler);
    edgeScroll.showEdge(inst, false);
  } catch (err) {}
};

edgeScroll.observeResize = function (inst) {
  try {
    if (window.ResizeObserver) {
      let ro = new ResizeObserver(function () { edgeScroll.refresh(inst); });
      ro.observe(inst.el);
      ro.observe(inst.parent);
    }
  } catch (e) {}
};

edgeScroll.mount = function (el) {
  if (!el || el.__edgeScrollBound) return;
  el.__edgeScrollBound = true;
  let parent = el.parentElement;
  if (!parent) return;
  edgeScroll.makeParentRelative(parent);
  let track = document.createElement("div");
  track.className = "edge-scroll-track";
  track.setAttribute("aria-hidden", "true");
  let thumb = document.createElement("div");
  thumb.className = "edge-scroll-thumb";
  track.appendChild(thumb);
  parent.appendChild(track);

  let inst = {
    el: el,
    parent: parent,
    track: track,
    thumb: thumb,
    dragging: false,
    dragStartY: 0,
    dragStartTop: 0,
    refresh: null,
    dragHandler: null,
    upHandler: null
  };
  inst.refresh = function () { edgeScroll.refresh(inst); };
  edgeScroll.instances.push(inst);
  edgeScroll.bindElement(inst);
  edgeScroll.bindThumb(inst);
  edgeScroll.observeResize(inst);
  inst.refresh();
  setTimeout(inst.refresh, 500);
};

edgeScroll.bind = function (selector, callback) {
  try {
    let nodes = document.querySelectorAll(selector);
    for (let j = 0; j < nodes.length; j++) callback(nodes[j]);
  } catch (err) {}
};

edgeScroll.refreshAll = function () {
  for (let k = 0; k < edgeScroll.instances.length; k++) {
    try { edgeScroll.instances[k].refresh(); } catch (err) {}
  }
};

edgeScroll.atScrollEdge = function (el, e) {
  try {
    let r = el.getBoundingClientRect();
    let nearRight = e.clientX >= r.right - edgeScroll.EDGE_PX && e.clientX <= r.right + 2;
    let nearBottom = e.clientY >= r.bottom - edgeScroll.EDGE_PX && e.clientY <= r.bottom + 2;
    let inside = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
    return inside && (nearRight || nearBottom);
  } catch (err) { return false; }
};

edgeScroll.needsVerticalScroll = function (cs, el) {
  return (cs.overflowY === "auto" || cs.overflowY === "scroll") && el.scrollHeight > el.clientHeight + 1;
};

edgeScroll.needsHorizontalScroll = function (cs, el) {
  return (cs.overflowX === "auto" || cs.overflowX === "scroll") && el.scrollWidth > el.clientWidth + 1;
};

edgeScroll.hasOverflow = function (el) {
  try {
    let cs = getComputedStyle(el);
    let needY = edgeScroll.needsVerticalScroll(cs, el);
    let needX = edgeScroll.needsHorizontalScroll(cs, el);
    // overflow:auto shorthand case
    if (cs.overflow === "auto" || cs.overflow === "scroll") {
      needY = needY || el.scrollHeight > el.clientHeight + 1;
      needX = needX || el.scrollWidth > el.clientWidth + 1;
    }
    return needY || needX;
  } catch (err) { return true; }
};

edgeScroll.markBarBound = function (el) {
  try {
    if (el.matches && (el.matches(".nav") || el.matches(".main"))) return false;
  } catch (err) {}
  el.__edgeBarBound = true;
  return true;
};

edgeScroll.mountInner = function (el) {
  if (!el || el.__edgeBarBound) return;
  // Skip panes already handled by the overlay thumb (.nav/.main).
  if (!edgeScroll.markBarBound(el)) return;
  el.addEventListener("mousemove", function (e) {
    try {
      let show = edgeScroll.hasOverflow(el) && edgeScroll.atScrollEdge(el, e);
      el.classList.toggle("show-bar", show);
    } catch (err) {}
  });
  el.addEventListener("mouseleave", function () {
    try { el.classList.remove("show-bar"); } catch (err) {}
  });
  // NOTE: no scroll listener here on purpose - scrolling with the wheel
  // while the cursor sits in the middle of the pane must NOT show the bar.
};

edgeScroll.bindInnerSelector = function (selector) {
  let nodes;
  try { nodes = document.querySelectorAll(selector); } catch (err) { return; }
  for (let j = 0; j < nodes.length; j++) edgeScroll.mountInner(nodes[j]);
};

edgeScroll.bindInner = function () {
  for (let i = 0; i < edgeScroll.INNER_SELECTORS.length; i++) {
    edgeScroll.bindInnerSelector(edgeScroll.INNER_SELECTORS[i]);
  }
};

edgeScroll.bindAll = function () {
  for (let i = 0; i < edgeScroll.SELECTORS.length; i++) {
    edgeScroll.bind(edgeScroll.SELECTORS[i], edgeScroll.mount);
  }
  edgeScroll.refreshAll();
  edgeScroll.bindInner();
};

edgeScroll.bindAll();
try {
  setInterval(edgeScroll.bindAll, 1500);
  window.addEventListener("resize", edgeScroll.bindAll);
} catch (err) {}
