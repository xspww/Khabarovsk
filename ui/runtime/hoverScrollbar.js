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
(function () {
  "use strict";
  let EDGE_PX = 14;
  let SELECTORS = [".nav", ".main"];
  // Inner scroll areas: show native thumb only when cursor is at the
  // vertical (right 14px) or horizontal (bottom 14px) scrollbar edge.
  let INNER_SELECTORS = [
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
  let instances = [];

  function mount(el) {
    if (!el || el.__edgeScrollBound) return;
    el.__edgeScrollBound = true;
    let parent = el.parentElement;
    if (!parent) return;
    try {
      if (getComputedStyle(parent).position === "static") parent.style.position = "relative";
    } catch (e) {}
    let track = document.createElement("div");
    track.className = "edge-scroll-track";
    track.setAttribute("aria-hidden", "true");
    let thumb = document.createElement("div");
    thumb.className = "edge-scroll-thumb";
    track.appendChild(thumb);
    parent.appendChild(track);

    let inst = { el: el, track: track, thumb: thumb, dragging: false, dragStartY: 0, dragStartTop: 0 };
    instances.push(inst);

    function showEdge(on) {
      try { track.classList.toggle("show-edge", !!on); } catch (e) {}
    }

    function place() {
      try {
        let h = el.clientHeight - 12;
        if (h < 40) { track.style.display = "none"; return; }
        track.style.display = "";
        track.style.top = el.offsetTop + 6 + "px";
        track.style.height = h + "px";
      } catch (e) {}
    }

    function refresh() {
      place();
      try {
        let sh = el.scrollHeight, ch = el.clientHeight;
        if (sh <= ch + 1) { track.style.display = "none"; return; }
        if (track.style.display === "none") place();
        if (track.style.display === "none") return;
        let th = track.clientHeight || 1;
        let h = Math.max(24, Math.min(th, (ch / sh) * th));
        let maxTop = Math.max(1, th - h);
        let ratio = sh - ch > 0 ? el.scrollTop / (sh - ch) : 0;
        ratio = Math.min(1, Math.max(0, ratio));
        thumb.style.height = h + "px";
        thumb.style.top = ratio * maxTop + "px";
      } catch (e) {}
    }
    inst.refresh = refresh;

    el.addEventListener("mousemove", function (e) {
      if (inst.dragging) return;
      try {
        let r = el.getBoundingClientRect();
        showEdge(e.clientX >= r.right - EDGE_PX);
      } catch (err) {}
    });
    el.addEventListener("mouseleave", function () {
      if (!inst.dragging) showEdge(false);
    });
    el.addEventListener("scroll", function () { refresh(); }, { passive: true });

    thumb.addEventListener("mouseenter", function () { showEdge(true); });
    thumb.addEventListener("mouseleave", function () { if (!inst.dragging) showEdge(false); });
    thumb.addEventListener("mousedown", function (e) {
      try {
        e.preventDefault();
        inst.dragging = true;
        inst.dragStartY = e.clientY;
        inst.dragStartTop = parseFloat(thumb.style.top) || 0;
        showEdge(true);
        document.addEventListener("mousemove", onDrag);
        document.addEventListener("mouseup", endDrag);
      } catch (err) {}
    });
    function onDrag(e) {
      if (!inst.dragging) return;
      try {
        let th = track.clientHeight || 1;
        let h = thumb.offsetHeight || 24;
        let maxTop = Math.max(1, th - h);
        let t = Math.min(maxTop, Math.max(0, inst.dragStartTop + (e.clientY - inst.dragStartY)));
        let sh = el.scrollHeight, ch = el.clientHeight;
        el.scrollTop = (t / maxTop) * (sh - ch);
      } catch (err) {}
    }
    function endDrag() {
      try {
        inst.dragging = false;
        document.removeEventListener("mousemove", onDrag);
        document.removeEventListener("mouseup", endDrag);
        showEdge(false);
      } catch (err) {}
    }

    try {
      if (window.ResizeObserver) {
        let ro = new ResizeObserver(function () { refresh(); });
        ro.observe(el);
        ro.observe(parent);
      }
    } catch (e) {}
    refresh();
    setTimeout(refresh, 500);
  }

  function bindAll() {
    for (let i = 0; i < SELECTORS.length; i++) {
      try {
        let nodes = document.querySelectorAll(SELECTORS[i]);
        for (let j = 0; j < nodes.length; j++) mount(nodes[j]);
      } catch (err) {}
    }
    for (let k = 0; k < instances.length; k++) {
      try { instances[k].refresh(); } catch (err) {}
    }
    bindInner();
  }

  function bindInner() {
    for (let i = 0; i < INNER_SELECTORS.length; i++) {
      let nodes;
      try { nodes = document.querySelectorAll(INNER_SELECTORS[i]); } catch (err) { continue; }
      for (let j = 0; j < nodes.length; j++) mountInner(nodes[j]);
    }
  }

  function atScrollEdge(el, e) {
    try {
      let r = el.getBoundingClientRect();
      let nearRight = e.clientX >= r.right - EDGE_PX && e.clientX <= r.right + 2;
      let nearBottom = e.clientY >= r.bottom - EDGE_PX && e.clientY <= r.bottom + 2;
      let inside = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
      return inside && (nearRight || nearBottom);
    } catch (err) { return false; }
  }

  function hasOverflow(el) {
    try {
      let cs = getComputedStyle(el);
      let oy = cs.overflowY, ox = cs.overflowX;
      let needY = (oy === "auto" || oy === "scroll") && el.scrollHeight > el.clientHeight + 1;
      let needX = (ox === "auto" || ox === "scroll") && el.scrollWidth > el.clientWidth + 1;
      // overflow:auto shorthand case
      if (cs.overflow === "auto" || cs.overflow === "scroll") {
        needY = needY || (el.scrollHeight > el.clientHeight + 1);
        needX = needX || (el.scrollWidth > el.clientWidth + 1);
      }
      return needY || needX;
    } catch (err) { return true; }
  }

  function mountInner(el) {
    if (!el || el.__edgeBarBound) return;
    // Skip panes already handled by the overlay thumb (.nav/.main).
    try {
      if (el.matches && (el.matches(".nav") || el.matches(".main"))) return;
    } catch (err) {}
    el.__edgeBarBound = true;
    el.addEventListener("mousemove", function (e) {
      try {
        let show = hasOverflow(el) && atScrollEdge(el, e);
        el.classList.toggle("show-bar", show);
      } catch (err) {}
    });
    el.addEventListener("mouseleave", function () {
      try { el.classList.remove("show-bar"); } catch (err) {}
    });
    // NOTE: no scroll listener here on purpose — scrolling with the wheel
    // while the cursor sits in the middle of the pane must NOT show the bar.
  }

  bindAll();
  try {
    setInterval(bindAll, 1500);
    window.addEventListener("resize", bindAll);
  } catch (err) {}
})();
