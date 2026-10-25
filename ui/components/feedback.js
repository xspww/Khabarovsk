import { solarIcon } from "./icons.js";
const TOAST_ICONS = {
  success:
    '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#5e8bff"/><path d="M8.2 12.2l2.6 2.6 4.5-5" stroke="white" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  warning:
    '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#f59e0b"/><path d="M12 7.5v5.2" stroke="white" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="16.2" r="1.2" fill="white"/></svg>',
  error:
    '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#ef4444"/><path d="M15 9l-6 6M9 9l6 6" stroke="white" stroke-width="2" stroke-linecap="round"/></svg>',
  info: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" fill="#3b82f6"/><path d="M12 11v5" stroke="white" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="8" r="1.2" fill="white"/></svg>',
};
const FARM_BLOCKED_MARKERS = ["blocked", "skipped", "unavailable"];
const TOAST_WARNING_MARKERS = ["unsaved", "save changes"];
const TOAST_ERROR_MARKERS = [
  "error",
  "failed",
  "invalid",
  "cannot",
  "denied",
  "missing",
  "not found",
  "required",
  "0 valid",
];
const TOAST_SUCCESS_MARKERS = [
  "reloaded",
  "imported",
  "saved",
  "deleted",
  "success",
];
function normalizeToastInput(msg, _type, _opts) {
  let explicit = null;
  let opts = {};
  if (_opts && typeof _opts === "object") opts = _opts;
  if (typeof _type === "object" && _type !== null) {
    opts = { ..._type, ...opts };
    explicit = opts.type || null;
  } else if (typeof _type === "string" && _type) explicit = _type;
  if (opts.type && typeof opts.type === "string") explicit = opts.type;
  return { explicit, opts };
}
function textHasAny(text, markers) {
  return markers.some((marker) => text.includes(marker));
}
function farmStartToastType(text) {
  return textHasAny(text, FARM_BLOCKED_MARKERS) ? "warning" : "success";
}
function errorToastType(text) {
  const launchBlocked = text.includes("blocked") && text.includes("launchable");
  return launchBlocked ? "warning" : "error";
}
function saysSuccess(text) {
  if (textHasAny(text, TOAST_SUCCESS_MARKERS)) return true;
  return text.includes("valid") && !text.includes("0 valid");
}
function keywordToastType(text) {
  if (textHasAny(text, TOAST_WARNING_MARKERS)) return "warning";
  if (textHasAny(text, TOAST_ERROR_MARKERS)) return errorToastType(text);
  if (saysSuccess(text)) return "success";
  if (text.includes("valid")) return "info";
  return "success";
}
function pickToastType(msg, explicit) {
  if (typeof explicit === "string" && explicit) return explicit;
  const text = String(msg || "").toLowerCase();
  if (text.includes("farm started")) return farmStartToastType(text);
  return keywordToastType(text);
}
function flipToastShift(h, s) {
  try {
    const k = [...h.children].filter((n) => n !== s),
      f = new Map(k.map((n) => [n, n.getBoundingClientRect().top]));
    return () => {
      k.forEach((n) => {
        if (!n.isConnected) return;
        const a = f.get(n),
          b = n.getBoundingClientRect().top,
          d = a - b;
        if (a === undefined || !d) return;
        n.style.transition = "none";
        n.style.transform = "translateY(" + d + "px)";
        void n.offsetHeight;
        n.style.transition = "";
        n.style.transform = "";
      });
    };
  } catch (_) {
    return () => {
      /* intentional: no-op */
    };
  }
}
function dropOldestToast(container) {
  while (container.children.length >= 4) {
    const oldest = container.firstElementChild;
    try {
      oldest && clearTimeout(oldest._timer);
    } catch (e) {}
    if (oldest) {
      const shift = flipToastShift(container, oldest);
      oldest.remove();
      shift();
    }
  }
}
function makeToastClickable(node, opts) {
  node.style.cursor = "pointer";
  node.title = opts.title || "Click to review";
  node.addEventListener("click", () => {
    try {
      opts.onClick();
    } catch (_) {}
  });
}
function scheduleToastDismiss(container, node, duration) {
  return setTimeout(() => {
    node.classList.remove("show");
    node.classList.add("hide");
    setTimeout(() => {
      const shift = flipToastShift(container, node);
      node.remove();
      shift();
    }, 200);
  }, duration);
}
function toastDuration(opts) {
  return Number(opts.duration) || (opts.onClick ? 6500 : 3200);
}
function buildToast(container, esc, msg, _type, _opts) {
  const norm = normalizeToastInput(msg, _type, _opts);
  let kind = pickToastType(msg, norm.explicit);
  if (!TOAST_ICONS[kind]) kind = "success";
  const opts = norm.opts || {};
  dropOldestToast(container);
  const node = document.createElement("div");
  node.className = "toast-item toast-" + kind;
  if (opts.onClick) node.classList.add("toast-clickable");
  const duration = toastDuration(opts);
  node.innerHTML = `<span class="toast-icon">${TOAST_ICONS[kind]}</span><span class="toast-text">${esc(msg)}</span>`;
  if (opts.onClick) makeToastClickable(node, opts);
  const shift = flipToastShift(container);
  container.appendChild(node);
  shift();
  requestAnimationFrame(() => {
    node.classList.add("show");
  });
  node._timer = scheduleToastDismiss(container, node, duration);
  return node;
}
export function createFeedback({ $: o, esc: t }) {
  return {
    toast: function (msg, type, opts) {
      const container = o("toast");
      if (!container) return;
      return buildToast(container, t, msg, type, opts);
    },
    modal: function (t, s, e) {
      ((o("modal-title").textContent = t),
        (o("modal-body").innerHTML = s),
        (o("modal-foot").innerHTML = e),
        (o("modal-backdrop").hidden = !1));
    },
    closeModal: function () {
      o("modal-backdrop").hidden = !0;
      const m = o("modal-backdrop").querySelector(".modal");
      m && m.classList.remove("vmanager");
    },
    cardIcon: function (o) {
      const t = {
        cookie: "userAdd",
        delete: "trash",
        close: "trash",
        install: "downloadSquare",
      };
      return `<span class="choice-icon">${solarIcon(t[o] || t.cookie)}</span>`;
    },
  };
}
