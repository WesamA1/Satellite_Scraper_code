// Attributes page -- the web port of the old tool's Admin Panel ("Attribute
// Configuration Editor"). Categories and their attributes on the left, an
// editor for whichever one is selected on the right, and every change saved
// straight to attributes.json (validated server-side, so a bad edit is
// rejected and rolled back here instead of corrupting the file).

const attributesCard = document.getElementById('card-attributes');

const OPTION_TYPES = ['Dropdown', 'DropdownOther', 'Multi-Select'];
const RANGE_TYPES = ['Number', 'Slider', 'Rating Scale'];
const TYPE_HELP = {
  'Text Box': 'A short free-text answer. Required.',
  'Long Text': 'A multi-line free-text answer. Required.',
  Dropdown: 'Pick exactly one option.',
  DropdownOther: 'Pick one option, or type a different answer that overrides it.',
  'Single-Select': 'Present / Unsure / Not present.',
  'Check Button': 'Present / Unsure / Not present.',
  'Multi-Select': 'Tick any number of options. Ticking none saves "None".',
  Number: 'A whole number between min and max.',
  Slider: 'A whole number between min and max, picked on a slider.',
  'Rating Scale': 'One rating from min to max. Required.',
  Date: 'A calendar date (defaults to today).',
  Time: 'A time of day (defaults to now).',
  'Date-Time': 'A date and time (defaults to now).',
  Boolean: 'A yes/no checkbox.',
};

let schema = {};
let selection = null; // { kind: 'category' | 'attribute', category, attribute? }

const els = {
  tree: document.getElementById('attr-tree'),
  summary: document.getElementById('attributes-summary'),
  path: document.getElementById('attributes-path'),
  empty: document.getElementById('editor-empty'),
  categoryEditor: document.getElementById('editor-category'),
  attributeEditor: document.getElementById('editor-attribute'),
  categoryName: document.getElementById('category-name'),
  attributeHeading: document.getElementById('attribute-heading'),
  attributeName: document.getElementById('attribute-name'),
  attributeType: document.getElementById('attribute-type'),
  optionsWrap: document.getElementById('attribute-options-wrap'),
  options: document.getElementById('attribute-options'),
  rangeWrap: document.getElementById('attribute-range-wrap'),
  min: document.getElementById('attribute-min'),
  max: document.getElementById('attribute-max'),
  stepWrap: document.getElementById('attribute-step-wrap'),
  step: document.getElementById('attribute-step'),
  typeHelp: document.getElementById('attribute-type-help'),
};

function esc(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function logHistory(message, ok = true) {
  logLine(attributesCard, `[${ok ? 'SUCCESS' : 'ERROR'}] ${message}`);
}

// ---- Load / save ----

async function reload(announce = true) {
  try {
    const resp = await fetch('/api/annotate/schema');
    const body = await resp.json();
    if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
    schema = body.schema;
    els.path.textContent = body.path;
    if (announce) logHistory(`Loaded ${body.path}.`);
  } catch (e) {
    logHistory(`Couldn't load the attribute list: ${e.message}`, false);
  }
  if (selection && !selectionExists(selection)) selection = null;
  render();
}

// Every edit goes through here: send the whole new schema, and only adopt it
// once the server has validated and written it.
async function save(next, message) {
  const resp = await fetch('/api/annotate/schema', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(next),
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = typeof body.detail === 'string' ? body.detail : `HTTP ${resp.status}`;
    logHistory(detail, false);
    toast(detail, 'fail');
    return false;
  }
  schema = body.schema;
  logHistory(message);
  return true;
}

// Object key order is the order annotators see things in, so a rename has
// to rebuild the object with the new key in the old key's position.
function renameKey(obj, oldKey, newKey, newValue) {
  const out = {};
  for (const [key, value] of Object.entries(obj)) {
    if (key === oldKey) out[newKey] = newValue === undefined ? value : newValue;
    else out[key] = value;
  }
  return out;
}

function clone(obj) {
  return JSON.parse(JSON.stringify(obj));
}

function selectionExists(sel) {
  if (!(sel.category in schema)) return false;
  return sel.kind === 'category' || sel.attribute in schema[sel.category];
}

// ---- Rendering ----

function render() {
  const categories = Object.entries(schema);
  const attrCount = categories.reduce((n, [, attrs]) => n + Object.keys(attrs).length, 0);
  els.summary.textContent = `${categories.length} categories · ${attrCount} attributes`;

  els.tree.innerHTML = categories.length === 0
    ? '<div class="muted attr-tree-empty">No categories yet. Add one below.</div>'
    : categories.map(([category, attributes]) => {
      const catSelected = selection && selection.kind === 'category' && selection.category === category;
      return `<div class="attr-tree-group">
        <button type="button" class="attr-tree-item category${catSelected ? ' selected' : ''}" data-category="${esc(category)}">
          <span>${esc(category)}</span><span class="attr-tree-count">${Object.keys(attributes).length}</span>
        </button>
        ${Object.entries(attributes).map(([name, spec]) => {
          const selected = selection && selection.kind === 'attribute'
            && selection.category === category && selection.attribute === name;
          return `<button type="button" class="attr-tree-item attribute${selected ? ' selected' : ''}"
            data-category="${esc(category)}" data-attribute="${esc(name)}">
            <span>${esc(name)}</span><span class="attr-tree-type">${esc(spec[0])}</span>
          </button>`;
        }).join('')}
      </div>`;
    }).join('');

  renderEditor();
}

function resetConfirms() {
  attributesCard.querySelectorAll('button.confirming').forEach((btn) => {
    btn.classList.remove('confirming');
    btn.textContent = btn.dataset.label;
  });
}

function renderEditor() {
  resetConfirms();
  els.empty.hidden = Boolean(selection);
  els.categoryEditor.hidden = !selection || selection.kind !== 'category';
  els.attributeEditor.hidden = !selection || selection.kind !== 'attribute';
  if (!selection) return;

  if (selection.kind === 'category') {
    els.categoryName.value = selection.category;
    return;
  }

  const spec = schema[selection.category][selection.attribute];
  els.attributeHeading.textContent = `Attribute: ${selection.attribute}`;
  els.attributeName.value = selection.attribute;
  els.attributeType.value = spec[0];
  els.options.value = OPTION_TYPES.includes(spec[0]) ? spec[1].join('\n') : '';
  els.min.value = RANGE_TYPES.includes(spec[0]) && spec[1] !== undefined ? spec[1] : '';
  els.max.value = RANGE_TYPES.includes(spec[0]) && spec[2] !== undefined ? spec[2] : '';
  els.step.value = RANGE_TYPES.includes(spec[0]) && spec[3] !== undefined ? spec[3] : '';
  syncTypeFields();
}

function syncTypeFields() {
  const type = els.attributeType.value;
  els.optionsWrap.hidden = !OPTION_TYPES.includes(type);
  els.rangeWrap.hidden = !RANGE_TYPES.includes(type);
  els.stepWrap.hidden = type === 'Rating Scale';
  const defaults = type === 'Rating Scale' ? '1 to 5' : '0 to 100';
  els.min.placeholder = type === 'Rating Scale' ? '1' : '0';
  els.max.placeholder = type === 'Rating Scale' ? '5' : '100';
  els.step.placeholder = '1';
  els.typeHelp.textContent = TYPE_HELP[type] + (RANGE_TYPES.includes(type) ? ` Leave blank for ${defaults}.` : '');
}

// ---- Selection ----

els.tree.addEventListener('click', (e) => {
  const item = e.target.closest('.attr-tree-item');
  if (!item) return;
  selection = item.dataset.attribute !== undefined
    ? { kind: 'attribute', category: item.dataset.category, attribute: item.dataset.attribute }
    : { kind: 'category', category: item.dataset.category };
  render();
});

els.attributeType.addEventListener('change', syncTypeFields);

// Two-click confirm for deletes -- same pattern as Discovery's Promote.
function confirmClick(btn, action) {
  if (!btn.dataset.label) btn.dataset.label = btn.textContent;
  if (!btn.classList.contains('confirming')) {
    btn.classList.add('confirming');
    btn.textContent = 'Click again to delete';
    return;
  }
  resetConfirms();
  action();
}

// ---- Category operations ----

document.getElementById('form-add-category').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = e.target.elements.name;
  const name = input.value.trim();
  if (!name) return logHistory('Category name cannot be empty.', false);
  if (name in schema) return logHistory(`Category '${name}' already exists.`, false);

  const next = clone(schema);
  next[name] = {};
  if (await save(next, `Category '${name}' added.`)) {
    input.value = '';
    selection = { kind: 'category', category: name };
    render();
  }
});

document.getElementById('btn-category-rename').addEventListener('click', async () => {
  const oldName = selection.category;
  const newName = els.categoryName.value.trim();
  if (!newName) return logHistory('Category name cannot be empty.', false);
  if (newName === oldName) return;
  if (newName in schema) return logHistory(`Category '${newName}' already exists.`, false);

  if (await save(renameKey(schema, oldName, newName), `Category '${oldName}' renamed to '${newName}'.`)) {
    selection = { kind: 'category', category: newName };
    render();
  }
});

document.getElementById('btn-category-delete').addEventListener('click', (e) => {
  confirmClick(e.currentTarget, async () => {
    const name = selection.category;
    const next = clone(schema);
    delete next[name];
    if (await save(next, `Category '${name}' deleted.`)) {
      selection = null;
      render();
    }
  });
});

document.getElementById('form-add-attribute').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = e.target.elements.name;
  const name = input.value.trim();
  const category = selection.category;
  if (!name) return logHistory('Attribute name cannot be empty.', false);

  // New attributes start as a Text Box, then get their real type in the
  // editor -- the same flow the old Admin Panel used.
  const next = clone(schema);
  next[category][name] = ['Text Box'];
  if (await save(next, `Attribute '${name}' added to '${category}'.`)) {
    input.value = '';
    selection = { kind: 'attribute', category, attribute: name };
    render();
  }
});

// ---- Attribute operations ----

function readRangeParams() {
  const params = [els.min.value, els.max.value];
  if (els.attributeType.value !== 'Rating Scale') params.push(els.step.value);
  // Trailing blanks fall back to the server-side defaults; a blank in the
  // middle (max set, min blank) needs the default filled in to keep its slot.
  while (params.length && params[params.length - 1] === '') params.pop();
  const defaults = els.attributeType.value === 'Rating Scale' ? [1, 5] : [0, 100, 1];
  return params.map((v, i) => (v === '' ? defaults[i] : Number(v)));
}

document.getElementById('btn-attribute-save').addEventListener('click', async () => {
  const { category, attribute: oldName } = selection;
  const newName = els.attributeName.value.trim();
  const type = els.attributeType.value;
  if (!newName) return logHistory('Attribute name cannot be empty.', false);
  if (newName !== oldName && newName in schema[category]) {
    return logHistory(`Attribute '${newName}' already exists.`, false);
  }

  let spec = [type];
  if (OPTION_TYPES.includes(type)) {
    spec = [type, els.options.value.split('\n').map((line) => line.trim()).filter(Boolean)];
  } else if (RANGE_TYPES.includes(type)) {
    spec = [type, ...readRangeParams()];
  }

  const next = clone(schema);
  next[category] = renameKey(next[category], oldName, newName, spec);
  if (await save(next, `Attribute '${oldName}' updated.`)) {
    selection = { kind: 'attribute', category, attribute: newName };
    render();
  }
});

document.getElementById('btn-attribute-delete').addEventListener('click', (e) => {
  confirmClick(e.currentTarget, async () => {
    const { category, attribute } = selection;
    const next = clone(schema);
    delete next[category][attribute];
    if (await save(next, `Attribute '${attribute}' deleted from '${category}'.`)) {
      selection = { kind: 'category', category };
      render();
    }
  });
});

document.getElementById('btn-attributes-reload').addEventListener('click', () => reload(true));

reload(true);
