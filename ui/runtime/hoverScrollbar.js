/* Cronus overlay scrollbars (no build step).
 * Native scrollbars are fully hidden (zero layout space, zero gutter).
 * This draws a floating thumb at the pane's right edge, shown only when
 * the cursor is at that edge, while scrolling, or while dragging it.
 * Targets: sidebar .nav and .main. Thumb is draggable.
 */
(function () {
  "use strict";
  var EDGE_PX = 14;
  var SCROLL_HIDE_MS = 900;
  var SELECTORS = [".nav", ".main"];
  var instances = [];

  function mount(el) {
    if (!el || el.__edgeScrollBound) return;
    el.__edgeScrollBound = true;
    var parent = el.parentElement;
    if (!parent) return;
    try {
      if (getComputedStyle(parent).position === "static") parent.style.position = "relative";
    } catch (e) {}
    var track = document.createElement("div");
    track.className = "edge-scroll-track";
    track.setAttribute("aria-hidden", "true");
    var thumb = document.createElement("div");
    thumb.className = "edge-scroll-thumb";
    track.appendChild(thumb);
    parent.appendChild(track);

    var inst = { el: el, track: track, thumb: thumb, hideTimer: 0, dragging: false, dragStartY: 0, dragStartTop: 0 };
    instances.push(inst);

    function showEdge(on) {
      try { track.classList.toggle("show-edge", !!on); } catch (e) {}
    }
    function pokeScroll() {
      try {
        track.classList.add("show-scroll");
        clearTimeout(inst.hideTimer);
        inst.hideTimer = setTimeout(function () { track.classList.remove("show-scroll"); }, SCROLL_HIDE_MS);
      } catch (e) {}
    }

    function place() {
      try {
        var h = el.clientHeight - 12;
        if (h < 40) { track.style.display = "none"; return; }
        track.style.display = "";
        track.style.top = el.offsetTop + 6 + "px";
        track.style.height = h + "px";
      } catch (e) {}
    }

    function refresh() {
      place();
      try {
        var sh = el.scrollHeight, ch = el.clientHeight;
        if (sh <= ch + 1) { track.style.display = "none"; return; }
        if (track.style.display === "none") place();
        if (track.style.display === "none") return;
        var th = track.clientHeight || 1;
        var h = Math.max(24, Math.min(th, (ch / sh) * th));
        var maxTop = Math.max(1, th - h);
        var ratio = sh - ch > 0 ? el.scrollTop / (sh - ch) : 0;
        ratio = Math.min(1, Math.max(0, ratio));
        thumb.style.height = h + "px";
        thumb.style.top = ratio * maxTop + "px";
      } catch (e) {}
    }
    inst.refresh = refresh;

    el.addEventListener("mousemove", function (e) {
      if (inst.dragging) return;
      try {
        var r = el.getBoundingClientRect();
        showEdge(e.clientX >= r.right - EDGE_PX);
      } catch (err) {}
    });
    el.addEventListener("mouseleave", function () {
      if (!inst.dragging) showEdge(false);
    });
    el.addEventListener("scroll", function () { refresh(); pokeScroll(); }, { passive: true });

    thumb.addEventListener("mouseenter", function () { showEdge(true); });
    thumb.addEventListener("mouseleave", function () { if (!inst.dragging) showEdge(false); });
    thumb.addEventListener("mousedown", function (e) {
      try {
        e.preventDefault();
        inst.dragging = true;
        inst.dragStartY = e.clientY;
        inst.dragStartTop = parseFloat(thumb.style.top) || 0;
        pokeScroll();
        document.addEventListener("mousemove", onDrag);
        document.addEventListener("mouseup", endDrag);
      } catch (err) {}
    });
    function onDrag(e) {
      if (!inst.dragging) return;
      try {
        var th = track.clientHeight || 1;
        var h = thumb.offsetHeight || 24;
        var maxTop = Math.max(1, th - h);
        var t = Math.min(maxTop, Math.max(0, inst.dragStartTop + (e.clientY - inst.dragStartY)));
        var sh = el.scrollHeight, ch = el.clientHeight;
        el.scrollTop = (t / maxTop) * (sh - ch);
      } catch (err) {}
    }
    function endDrag() {
      try {
        inst.dragging = false;
        document.removeEventListener("mousemove", onDrag);
        document.removeEventListener("mouseup", endDrag);
        showEdge(false);
        pokeScroll();
      } catch (err) {}
    }

    try {
      if (window.ResizeObserver) {
        var ro = new ResizeObserver(function () { refresh(); });
        ro.observe(el);
        ro.observe(parent);
      }
    } catch (e) {}
    refresh();
    setTimeout(refresh, 500);
  }

  function bindAll() {
    for (var i = 0; i < SELECTORS.length; i++) {
      try {
        var nodes = document.querySelectorAll(SELECTORS[i]);
        for (var j = 0; j < nodes.length; j++) mount(nodes[j]);
      } catch (err) {}
    }
    for (var k = 0; k < instances.length; k++) {
      try { instances[k].refresh(); } catch (err) {}
    }
  }

  bindAll();
  try {
    setInterval(bindAll, 1500);
    window.addEventListener("resize", bindAll);
  } catch (err) {}
})();
