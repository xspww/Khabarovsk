const SELECTORS = [
  "#window-grid-preset",
  "#process-priority",
  "#cpu-mode",
  "#ram-limit-preset",
];
const enhanced = new WeakMap();

function selectedOption(select) {
  return select.options[select.selectedIndex] || select.options[0] || null;
}

function optionLabel(option) {
  return String(option?.textContent || "").trim();
}

function closeSelect(state) {
  state.root.classList.remove("is-open");
  state.button.setAttribute("aria-expanded", "false");
}

function closeOthers(current) {
  document.querySelectorAll(".custom-select.is-open").forEach((root) => {
    if (root !== current.root) {
      root.classList.remove("is-open");
      root.querySelector(".custom-select-button")?.setAttribute("aria-expanded", "false");
    }
  });
}

function syncSelect(select) {
  const state = enhanced.get(select);
  if (!state) return;
  const option = selectedOption(select);
  state.label.textContent = optionLabel(option);
  state.options.forEach((button) => {
    const active = button.dataset.value === select.value;
    button.classList.toggle("is-selected", active);
    button.setAttribute("aria-selected", String(active));
  });
}

function chooseValue(select, value) {
  if (select.value !== value) {
    select.value = value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
  }
  syncSelect(select);
}

function buildOptions(select, state) {
  state.menu.innerHTML = "";
  state.options = Array.from(select.options).map((option) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "custom-select-option";
    button.dataset.value = option.value;
    button.setAttribute("role", "option");
    button.textContent = optionLabel(option);
    button.addEventListener("click", () => {
      chooseValue(select, option.value);
      closeSelect(state);
      state.button.focus();
    });
    state.menu.appendChild(button);
    return button;
  });
}

function moveSelection(select, state, direction) {
  const options = Array.from(select.options);
  if (!options.length) return;
  const current = Math.max(0, select.selectedIndex);
  const next = Math.max(0, Math.min(options.length - 1, current + direction));
  chooseValue(select, options[next].value);
}

function enhanceSelect(select) {
  if (!select || enhanced.has(select)) return;

  const root = document.createElement("div");
  root.className = "custom-select";
  root.dataset.nativeSelect = select.id || "";

  const button = document.createElement("button");
  button.type = "button";
  button.className = "custom-select-button";
  button.setAttribute("aria-haspopup", "listbox");
  button.setAttribute("aria-expanded", "false");

  const label = document.createElement("span");
  label.className = "custom-select-label";

  const caret = document.createElement("span");
  caret.className = "custom-select-caret";
  caret.setAttribute("aria-hidden", "true");

  const menu = document.createElement("div");
  menu.className = "custom-select-menu";
  menu.setAttribute("role", "listbox");

  button.append(label, caret);
  root.append(button, menu);
  select.insertAdjacentElement("afterend", root);
  select.classList.add("native-select-hidden");
  select.setAttribute("aria-hidden", "true");
  select.tabIndex = -1;

  const state = { root, button, label, caret, menu, options: [] };
  enhanced.set(select, state);
  buildOptions(select, state);
  syncSelect(select);

  button.addEventListener("click", (event) => {
    event.stopPropagation();
    const open = root.classList.contains("is-open");
    closeOthers(state);
    root.classList.toggle("is-open", !open);
    button.setAttribute("aria-expanded", String(!open));
    syncSelect(select);
  });

  button.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeSelect(state);
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      moveSelection(select, state, event.key === "ArrowDown" ? 1 : -1);
      root.classList.add("is-open");
      button.setAttribute("aria-expanded", "true");
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      button.click();
    }
  });

  select.addEventListener("change", () => syncSelect(select));
}

function syncAll() {
  SELECTORS.forEach((selector) => {
    document.querySelectorAll(selector).forEach((select) => {
      enhanceSelect(select);
      syncSelect(select);
    });
  });
}

document.addEventListener("click", () => {
  document.querySelectorAll(".custom-select.is-open").forEach((root) => {
    root.classList.remove("is-open");
    root.querySelector(".custom-select-button")?.setAttribute("aria-expanded", "false");
  });
});

window.CronusCustomSelectSync = (id) => {
  const select = document.getElementById(id);
  if (!select) return;
  enhanceSelect(select);
  syncSelect(select);
};

syncAll();
setInterval(syncAll, 400);
