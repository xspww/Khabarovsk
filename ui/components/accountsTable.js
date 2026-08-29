const COLOR_MAP = {
  red: "#f87171",
  orange: "#fb923c",
  yellow: "#facc15",
  green: "#4ade80",
  cyan: "#22d3ee",
  blue: "#60a5fa",
  purple: "#c084fc",
  pink: "#f472b6"
};

export function formatDescriptionHtml(raw, esc) {
  if (!raw) return "";
  let safe = esc(String(raw));

  // Replace <color:name>...</> or <color:name>...</color>
  safe = safe.replace(/&lt;color:([a-zA-Z0-9#]+)&gt;([\s\S]*?)&lt;\/(?:color)?&gt;/gi, (_, col, txt) => {
    const hex = COLOR_MAP[col.toLowerCase()] || col;
    return `<span class="desc-tag-color" style="color:${esc(hex)}">${txt}</span>`;
  });
  // Replace <color:name>...<>
  safe = safe.replace(/&lt;color:([a-zA-Z0-9#]+)&gt;([\s\S]*?)&lt;&gt;/gi, (_, col, txt) => {
    const hex = COLOR_MAP[col.toLowerCase()] || col;
    return `<span class="desc-tag-color" style="color:${esc(hex)}">${txt}</span>`;
  });
  // <b>...</b>
  safe = safe.replace(/&lt;b&gt;([\s\S]*?)&lt;\/b&gt;/gi, "<b>$1</b>");
  // <i>...</i>
  safe = safe.replace(/&lt;i&gt;([\s\S]*?)&lt;\/i&gt;/gi, "<i>$1</i>");
  // <u>...</u>
  safe = safe.replace(/&lt;u&gt;([\s\S]*?)&lt;\/u&gt;/gi, "<u>$1</u>");
  // <s>...</s>
  safe = safe.replace(/&lt;s&gt;([\s\S]*?)&lt;\/s&gt;/gi, "<s>$1</s>");
  // <big>...</big>
  safe = safe.replace(/&lt;(?:big|size:lg)&gt;([\s\S]*?)&lt;\/(?:big|size)?&gt;/gi, '<span class="desc-tag-big">$1</span>');
  // <star>...</star> or <mark>...</mark>
  safe = safe.replace(/&lt;(?:star|mark)&gt;([\s\S]*?)&lt;\/(?:star|mark)?&gt;/gi, '<span class="desc-tag-mark">$1</span>');
  // <block>...</block> or <badge>...</badge>
  safe = safe.replace(/&lt;(?:block|badge)&gt;([\s\S]*?)&lt;\/(?:block|badge)?&gt;/gi, '<span class="desc-tag-badge">$1</span>');
  // <code>...</code>
  safe = safe.replace(/&lt;code&gt;([\s\S]*?)&lt;\/code&gt;/gi, '<code class="desc-tag-code">$1</code>');

  return safe;
}

export function renderAccountRows({
  rows: t,
  selectedUser: e,
  editingDescUser: editingUser,
  cfgFor: a,
  bucket: s,
  isCaptcha: n,
  rowStatusLabel: r,
  avatarHtml: u,
  esc: d,
  gameHtmlFor: g
}) {
  if (!t.length) {
    return '<tr><td colspan="5" style="height:100px;text-align:center;color:var(--muted)">No accounts match the current filter</td></tr>';
  }

  return t.map((t, i) => {
    const l = a(t.username),
      m = s(t),
      p = t.description ?? l.description ?? "",
      v = r(t, m),
      h = "Waiting For Lua" === v ? `${m} lua` : "Launching" === v ? `${m} launching` : m;

    const isEditing = editingUser && editingUser.toLowerCase() === String(t.username).toLowerCase();

    let descCell = "";
    if (isEditing) {
      descCell = `
        <div class="desc-editor" data-user="${d(t.username)}">
          <div class="desc-toolbar">
            <div class="desc-formats">
              <button type="button" class="desc-tool-btn" data-tool="bold" title="Bold">B</button>
              <button type="button" class="desc-tool-btn italic" data-tool="italic" title="Italic">I</button>
              <button type="button" class="desc-tool-btn underline" data-tool="underline" title="Underline">U</button>
              <button type="button" class="desc-tool-btn strike" data-tool="strike" title="Strikethrough">S</button>
              <button type="button" class="desc-tool-btn" data-tool="big" title="Large text">A+</button>
              <button type="button" class="desc-tool-btn" data-tool="star" title="Highlight">✦</button>
              <button type="button" class="desc-tool-btn" data-tool="block" title="Block">▮</button>
              <button type="button" class="desc-tool-btn code" data-tool="code" title="Code">&lt;/&gt;</button>
              <button type="button" class="desc-tool-btn" data-tool="badge" title="Badge">▄</button>
            </div>
            <div class="desc-colors">
              <button type="button" class="desc-color-dot" data-color="red" style="--c:#f87171" title="Red"></button>
              <button type="button" class="desc-color-dot" data-color="orange" style="--c:#fb923c" title="Orange"></button>
              <button type="button" class="desc-color-dot" data-color="yellow" style="--c:#facc15" title="Yellow"></button>
              <button type="button" class="desc-color-dot" data-color="green" style="--c:#4ade80" title="Green"></button>
              <button type="button" class="desc-color-dot" data-color="cyan" style="--c:#22d3ee" title="Cyan"></button>
              <button type="button" class="desc-color-dot" data-color="blue" style="--c:#60a5fa" title="Blue"></button>
              <button type="button" class="desc-color-dot" data-color="purple" style="--c:#c084fc" title="Purple"></button>
              <button type="button" class="desc-color-dot" data-color="pink" style="--c:#f472b6" title="Pink"></button>
            </div>
            <div class="desc-editor-actions">
              <span class="desc-char-count">${String(p).length}</span>
              <button type="button" class="desc-action-btn save" data-action="save-desc" data-user="${d(t.username)}" title="Save">&#x2713;</button>
              <button type="button" class="desc-action-btn cancel" data-action="cancel-desc" data-user="${d(t.username)}" title="Cancel">&times;</button>
            </div>
          </div>
          <textarea class="desc-textarea" data-user="${d(t.username)}" placeholder="Description... &lt;color:red&gt;flagged&lt;&gt;">${d(p)}</textarea>
        </div>
      `;
    } else if (p && String(p).trim()) {
      descCell = `<div class="desc-preview" data-action="edit-desc" data-user="${d(t.username)}" title="Click to edit">${formatDescriptionHtml(p, d)}</div>`;
    } else {
      descCell = `<button type="button" class="desc-add-btn" data-action="edit-desc" data-user="${d(t.username)}"><span class="desc-add-plus">+</span> Add description</button>`;
    }

    const gameCell = g ? g(t, l) : '<span class="game-empty">—</span>';

    const isSelected=e instanceof Set?e.has(t.username):t.username===e;const selClass=isSelected?"selected":"";
    return `<tr class="${selClass}${isEditing ? " editing-desc" : ""}" data-user="${d(t.username)}">
      <td class="num">${i + 1}</td>
      <td><span class="status ${d(h)}">${d(v)}</span></td>
      <td><div class="user-simple"><span class="user-name" title="${d(t.username)}">${d(t.username)}</span></div></td>
      <td class="game-cell">${gameCell}</td>
      <td class="desc-cell">${descCell}</td>
    </tr>`;
  }).join("");
}