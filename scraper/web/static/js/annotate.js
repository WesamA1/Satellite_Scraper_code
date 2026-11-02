// Annotate page -- the web port of the standalone Attribute Annotation Tool
// (MIAT). Same flow: each category's attributes shown a few at a time,
// Previous/Next between pages, "Mark unusable" to label every attribute
// UNUSABLE, then a check-before-submitting summary with the same three
// choices the old SubmitMenu offered. Values are validated here for quick
// feedback and again server-side (scraper/stages/annotate.py) before saving.

const annotateCard = document.getElementById('card-annotate');

// The old AnnotationHandler's chunk_size -- five attributes per screen keeps
// each page short enough to see next to the image without scrolling.
const CHUNK_SIZE = 5;

const RADIO_CHOICES = [
  ['Present', 'Present'],
  ['Unknown', 'Unsure'],
  ['Not Present', 'Not present'],
];
const RADIO_TYPES = ['Check Button', 'Single-Select'];
const RANGE_DEFAULTS = { Number: [0, 100, 1], Slider: [0, 100, 1], 'Rating Scale': [1, 5, 1] };
const ANNOTATOR_KEY = 'groundstation.annotator';

const state = {
  schema: {},
  version: null,
  pages: [],
  queue: [],
  remaining: 0,
  index: 0,
  page: 0,
  values: {},
  unusable: false,
  annotator: '',
  family: '',
  reviewedOnly: true,
  submitting: false,
};

const els = {
  setup: document.getElementById('annotate-setup'),
  annotator: document.getElementById('annotate-annotator'),
  family: document.getElementById('annotate-family'),
  reviewedOnly: document.getElementById('annotate-reviewed-only'),
  queueStatus: document.getElementById('annotate-queue-status'),
  empty: document.getElementById('annotate-empty'),
  workspace: document.getElementById('annotate-workspace'),
  image: document.getElementById('annotate-image'),
  imageName: document.getElementById('annotate-image-name'),
  imageFamily: document.getElementById('annotate-image-family'),
  progress: document.getElementById('annotate-progress'),
  steps: document.getElementById('annotate-steps'),
  page: document.getElementById('annotate-page'),
  heading: document.getElementById('annotate-heading'),
  fields: document.getElementById('annotate-fields'),
  summary: document.getElementById('annotate-summary'),
  summaryNote: document.getElementById('annotate-summary-note'),
  summaryBody: document.querySelector('#annotate-summary-table tbody'),
};

function esc(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function rangeParams(spec) {
  const [lo, hi, step] = RANGE_DEFAULTS[spec[0]];
  return [spec[1] ?? lo, spec[2] ?? hi, spec[3] ?? step];
}

function pad(n) {
  return String(n).padStart(2, '0');
}

function nowParts() {
  const d = new Date();
  return {
    date: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`,
    time: `${pad(d.getHours())}:${pad(d.getMinutes())}`,
  };
}

// ---- Schema -> pages ----

function buildPages(schema) {
  const pages = [];
  const categories = Object.entries(schema);
  categories.forEach(([category, attributes], catIndex) => {
    const entries = Object.entries(attributes);
    const chunkCount = Math.max(1, Math.ceil(entries.length / CHUNK_SIZE));
    for (let i = 0; i < entries.length; i += CHUNK_SIZE) {
      pages.push({
        category,
        catIndex,
        catCount: categories.length,
        chunkIndex: i / CHUNK_SIZE,
        chunkCount,
        attrs: entries.slice(i, i + CHUNK_SIZE),
      });
    }
  });
  return pages;
}

async function loadSchema() {
  const resp = await fetch('/api/annotate/schema');
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${resp.status}`);
  }
  const data = await resp.json();
  state.schema = data.schema;
  state.version = data.version;
  state.pages = buildPages(data.schema);
}

// ---- Field rendering ----

function fieldControl(name, spec, i) {
  const type = spec[0];
  const id = `attr-${i}`;

  if (RADIO_TYPES.includes(type)) {
    return `<div class="seg" role="radiogroup">${RADIO_CHOICES.map(([value, label]) => `
      <label class="seg-option"><input type="radio" name="${id}" value="${value}"><span>${label}</span></label>
    `).join('')}</div>`;
  }
  if (type === 'Rating Scale') {
    const [lo, hi] = rangeParams(spec);
    const options = [];
    for (let v = lo; v <= hi; v += 1) {
      options.push(`<label class="seg-option"><input type="radio" name="${id}" value="${v}"><span>${v}</span></label>`);
    }
    return `<div class="seg" role="radiogroup">${options.join('')}</div>`;
  }
  if (type === 'Text Box') return `<input type="text" id="${id}" autocomplete="off">`;
  if (type === 'Long Text') return `<textarea id="${id}" rows="3"></textarea>`;
  if (type === 'Boolean') {
    return `<label class="checkbox"><input type="checkbox" id="${id}"> Yes</label>`;
  }
  if (type === 'Number') {
    const [lo, hi, step] = rangeParams(spec);
    return `<input type="number" id="${id}" min="${lo}" max="${hi}" step="${step}">`;
  }
  if (type === 'Slider') {
    const [lo, hi, step] = rangeParams(spec);
    return `<div class="slider-row"><input type="range" id="${id}" min="${lo}" max="${hi}" step="${step}">
      <output class="slider-value" for="${id}"></output></div>`;
  }
  if (type === 'Dropdown') {
    return `<select id="${id}">${spec[1].map((o) => `<option value="${esc(o)}">${esc(o)}</option>`).join('')}</select>`;
  }
  if (type === 'DropdownOther') {
    return `<div class="other-row">
      <select id="${id}">${spec[1].map((o) => `<option value="${esc(o)}">${esc(o)}</option>`).join('')}</select>
      <input type="text" id="${id}-other" placeholder="Other (overrides the list)" autocomplete="off">
    </div>`;
  }
  if (type === 'Multi-Select') {
    return `<div class="chip-group">${spec[1].map((o) => `
      <label class="chip-option"><input type="checkbox" name="${id}" value="${esc(o)}"><span>${esc(o)}</span></label>
    `).join('')}</div>`;
  }
  if (type === 'Date') return `<input type="date" id="${id}">`;
  if (type === 'Time') return `<input type="time" id="${id}">`;
  if (type === 'Date-Time') return `<input type="datetime-local" id="${id}">`;
  return `<span class="muted">Unsupported type: ${esc(type)}</span>`;
}

function fieldHint(spec) {
  const type = spec[0];
  if (type === 'Multi-Select') return 'select all that apply, none = "None"';
  if (type === 'DropdownOther') return 'pick one, or type your own';
  if (RADIO_TYPES.includes(type)) return 'is it in this image?';
  return '';
}

function renderPage() {
  const page = state.pages[state.page];
  els.page.hidden = false;
  els.summary.hidden = true;
  els.heading.innerHTML = `Select all <span class="accent">${esc(page.category)}</span> traits shown in this image`;
  els.fields.innerHTML = page.attrs.map(([name, spec], i) => {
    const hint = fieldHint(spec);
    return `<div class="attr-field" data-index="${i}">
      <div class="attr-label">${esc(name)}${hint ? ` <small>${hint}</small>` : ''}</div>
      ${fieldControl(name, spec, i)}
      <div class="attr-error" hidden></div>
    </div>`;
  }).join('');

  page.attrs.forEach(([name, spec], i) => restoreField(spec, i, state.values[name]));

  els.fields.querySelectorAll('input[type="range"]').forEach((range) => {
    const out = range.parentElement.querySelector('output');
    const sync = () => { out.textContent = range.value; };
    range.addEventListener('input', sync);
    sync();
  });

  document.getElementById('btn-annotate-prev').disabled = state.page === 0;
  renderProgress();
  const first = els.fields.querySelector('input:not([type="radio"]):not([type="checkbox"]), textarea, select');
  if (first && first.type === 'text') first.focus();
}

// Pre-fills a field from this image's in-progress values, or with the same
// defaults the old tool's widgets started at (today/now for dates, the
// minimum for numbers and sliders, the first option for dropdowns).
function restoreField(spec, i, value) {
  const type = spec[0];
  const id = `attr-${i}`;
  const el = document.getElementById(id);
  const has = value !== undefined && value !== null && value !== 'UNUSABLE';

  if (RADIO_TYPES.includes(type) || type === 'Rating Scale') {
    if (!has) return;
    const radio = els.fields.querySelector(`input[name="${id}"][value="${CSS.escape(String(value))}"]`);
    if (radio) radio.checked = true;
  } else if (type === 'Text Box' || type === 'Long Text') {
    el.value = has ? value : '';
  } else if (type === 'Boolean') {
    el.checked = has ? Boolean(value) : false;
  } else if (type === 'Number' || type === 'Slider') {
    el.value = has ? value : rangeParams(spec)[0];
  } else if (type === 'Dropdown') {
    if (has && spec[1].includes(value)) el.value = value;
  } else if (type === 'DropdownOther') {
    const other = document.getElementById(`${id}-other`);
    if (has && spec[1].includes(value)) el.value = value;
    else if (has) other.value = value;
  } else if (type === 'Multi-Select') {
    const selected = has && Array.isArray(value) ? value : [];
    els.fields.querySelectorAll(`input[name="${id}"]`).forEach((cb) => { cb.checked = selected.includes(cb.value); });
  } else if (type === 'Date') {
    el.value = has ? value : nowParts().date;
  } else if (type === 'Time') {
    el.value = has ? value : nowParts().time;
  } else if (type === 'Date-Time') {
    const now = nowParts();
    el.value = has ? value : `${now.date}T${now.time}`;
  }
}

function readField(spec, i) {
  const type = spec[0];
  const id = `attr-${i}`;
  const el = document.getElementById(id);

  if (RADIO_TYPES.includes(type)) {
    const checked = els.fields.querySelector(`input[name="${id}"]:checked`);
    return checked ? checked.value : null;
  }
  if (type === 'Rating Scale') {
    const checked = els.fields.querySelector(`input[name="${id}"]:checked`);
    return checked ? Number(checked.value) : null;
  }
  if (type === 'Text Box' || type === 'Long Text') return el.value.trim();
  if (type === 'Boolean') return el.checked;
  if (type === 'Number' || type === 'Slider') return el.value === '' ? null : Number(el.value);
  if (type === 'Dropdown') return el.value;
  if (type === 'DropdownOther') {
    const other = document.getElementById(`${id}-other`).value.trim();
    return other || el.value;
  }
  if (type === 'Multi-Select') {
    const selected = Array.from(els.fields.querySelectorAll(`input[name="${id}"]:checked`)).map((cb) => cb.value);
    return selected.length ? selected : ['None'];
  }
  if (type === 'Date' || type === 'Time' || type === 'Date-Time') return el.value;
  return null;
}

function validateValue(spec, value) {
  const type = spec[0];
  if (RADIO_TYPES.includes(type)) return value ? null : 'Pick Present, Unsure, or Not present.';
  if (type === 'Rating Scale') return value === null ? 'Pick a rating.' : null;
  if (type === 'Text Box' || type === 'Long Text') return value ? null : 'Required.';
  if (type === 'Number' || type === 'Slider') {
    const [lo, hi] = rangeParams(spec);
    if (value === null || !Number.isInteger(value)) return 'Enter a whole number.';
    return value < lo || value > hi ? `Must be between ${lo} and ${hi}.` : null;
  }
  if (type === 'DropdownOther') return value ? null : 'Pick an option or type one in.';
  if (type === 'Date' || type === 'Time' || type === 'Date-Time') return value ? null : 'Required.';
  return null;
}

function showFieldError(index, message) {
  const field = els.fields.querySelector(`.attr-field[data-index="${index}"]`);
  if (!field) return;
  const errEl = field.querySelector('.attr-error');
  field.classList.toggle('invalid', Boolean(message));
  errEl.hidden = !message;
  errEl.textContent = message || '';
}

// Stores the current page's values; returns false (and flags the fields)
// if any are missing or invalid and `validate` was asked for.
function collectPage(validate) {
  const page = state.pages[state.page];
  let ok = true;
  page.attrs.forEach(([name, spec], i) => {
    const value = readField(spec, i);
    state.values[name] = value;
    if (!validate) return;
    const error = validateValue(spec, value);
    showFieldError(i, error);
    if (error) ok = false;
  });
  return ok;
}

// ---- Progress ----

function renderProgress() {
  const item = state.queue[state.index];
  const onSummary = !els.summary.hidden;
  const page = state.pages[state.page];
  const where = onSummary
    ? 'Review and submit'
    : `${page.category} ${page.chunkCount > 1 ? `${page.chunkIndex + 1}/${page.chunkCount}` : ''}`.trim();
  els.progress.textContent = `Image ${state.index + 1} of ${state.remaining} remaining · ${where}`;

  const total = state.pages.length + 1;
  const current = onSummary ? state.pages.length : state.page;
  els.steps.innerHTML = Array.from({ length: total }, (_, i) => {
    const cls = i < current ? 'done' : i === current ? 'current' : '';
    return `<span class="annotate-step ${cls}"></span>`;
  }).join('');

  els.imageName.textContent = item ? item.name : '';
  els.imageFamily.textContent = item ? item.family : '';
}

// ---- Navigation ----

function goNext() {
  if (!collectPage(true)) {
    toast('Fill in the highlighted attributes first.', 'fail');
    return;
  }
  if (state.page < state.pages.length - 1) {
    state.page += 1;
    renderPage();
  } else {
    showSummary();
  }
}

function goPrevious() {
  if (state.page === 0) return;
  collectPage(false);
  state.page -= 1;
  renderPage();
}

function markUnusable() {
  collectPage(false);
  state.unusable = true;
  showSummary();
}

function formatValue(value) {
  if (Array.isArray(value)) return value.join(', ');
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  return value ?? '';
}

function showSummary() {
  els.page.hidden = true;
  els.summary.hidden = false;
  els.summaryNote.textContent = state.unusable
    ? 'Marked unusable: every attribute will be saved as UNUSABLE.'
    : 'Make sure everything matches the image. "Return to annotation" goes back to the first page with these values kept.';

  els.summaryBody.innerHTML = Object.entries(state.schema).map(([category, attributes]) => `
    <tr class="summary-category"><td colspan="2">${esc(category)}</td></tr>
    ${Object.keys(attributes).map((name) => `
      <tr><td>${esc(name)}</td><td class="summary-value">${esc(state.unusable ? 'UNUSABLE' : formatValue(state.values[name]))}</td></tr>
    `).join('')}
  `).join('');
  renderProgress();
  document.getElementById('btn-annotate-submit-next').focus();
}

function returnToAnnotation() {
  state.unusable = false;
  state.page = 0;
  renderPage();
}

// ---- Queue ----

async function fetchQueue() {
  const params = new URLSearchParams({
    annotator: state.annotator,
    reviewed_only: state.reviewedOnly,
    limit: 50,
  });
  if (state.family) params.set('family', state.family);
  const resp = await fetch(`/api/annotate/queue?${params}`);
  const data = await resp.json();
  state.queue = data.queue;
  state.remaining = data.remaining;
  state.index = 0;
}

function showEmpty(message) {
  els.workspace.hidden = true;
  els.setup.hidden = false;
  els.empty.hidden = false;
  els.empty.textContent = message;
}

function emptyMessage() {
  if (state.reviewedOnly) {
    return 'Nothing left to annotate here. Only images already kept on the Review page are included. '
      + 'Review more images, or untick that box to annotate unreviewed ones too.';
  }
  return 'Nothing left to annotate for this annotator and family.';
}

function openCurrentImage() {
  const item = state.queue[state.index];
  if (!item) {
    showEmpty(emptyMessage());
    return;
  }
  state.values = {};
  state.unusable = false;
  state.page = 0;
  els.empty.hidden = true;
  els.setup.hidden = true;
  els.workspace.hidden = false;
  els.image.src = `/api/images/${item.id}/file`;
  els.image.alt = `${item.family}: ${item.name}`;
  renderPage();
}

async function beginAnnotation() {
  const annotator = els.annotator.value.trim();
  if (!annotator) {
    toast('Enter an annotator name first.', 'fail');
    els.annotator.focus();
    return;
  }
  state.annotator = annotator;
  state.family = els.family.value.trim();
  state.reviewedOnly = els.reviewedOnly.checked;
  try { localStorage.setItem(ANNOTATOR_KEY, annotator); } catch { /* private window -- just don't remember it */ }

  try {
    await loadSchema();
  } catch (e) {
    toast(`Couldn't load the attribute list: ${e.message}`, 'fail');
    return;
  }
  if (state.pages.length === 0) {
    showEmpty('There are no attributes to annotate yet. Add some on the Attributes page.');
    return;
  }

  await fetchQueue();
  openCurrentImage();
}

function endSession() {
  els.workspace.hidden = true;
  els.setup.hidden = false;
  refreshQueueStatus();
}

// ---- Submit ----

async function submitCurrent(thenNext) {
  const item = state.queue[state.index];
  if (!item || state.submitting) return;
  state.submitting = true;

  try {
    const resp = await fetch(`/api/annotate/${item.id}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        annotator: state.annotator,
        values: state.values,
        unusable: state.unusable,
        schema_version: state.version,
      }),
    });
    const body = await resp.json().catch(() => ({}));

    if (resp.status === 409) {
      // Someone saved a new attribute list mid-image: rebuild the pages from
      // it and start this image's pages over, keeping whatever still applies.
      toast('The attribute list was just changed. Reloaded it, please check this image again.', 'fail');
      await loadSchema();
      returnToAnnotation();
      return;
    }
    if (resp.status === 422 && body.detail && body.detail.errors) {
      const names = Object.keys(body.detail.errors);
      toast(`Fix these first: ${names.join(', ')}`, 'fail');
      const pageIndex = state.pages.findIndex((p) => p.attrs.some(([name]) => names.includes(name)));
      state.unusable = false;
      state.page = Math.max(0, pageIndex);
      renderPage();
      state.pages[state.page].attrs.forEach(([name], i) => {
        if (body.detail.errors[name]) showFieldError(i, body.detail.errors[name]);
      });
      return;
    }
    if (!resp.ok) {
      toast(body.detail || `Submit failed (HTTP ${resp.status}).`, 'fail');
      return;
    }

    toast(`Saved ${item.name}${state.unusable ? ' as unusable' : ''}.`, 'pass');
    state.queue.splice(state.index, 1);
    state.remaining = Math.max(0, state.remaining - 1);

    if (!thenNext) {
      endSession();
      return;
    }
    if (state.queue.length === 0 && state.remaining > 0) await fetchQueue();
    if (state.index >= state.queue.length) state.index = 0;
    openCurrentImage();
  } finally {
    state.submitting = false;
  }
}

// ---- Queue status (setup box) ----

async function refreshQueueStatus() {
  const annotator = els.annotator.value.trim();
  if (!annotator) {
    els.queueStatus.hidden = true;
    return;
  }
  const params = new URLSearchParams({
    annotator,
    reviewed_only: els.reviewedOnly.checked,
    limit: 1,
  });
  const family = els.family.value.trim();
  if (family) params.set('family', family);
  try {
    const resp = await fetch(`/api/annotate/queue?${params}`);
    const { remaining } = await resp.json();
    els.queueStatus.hidden = false;
    els.queueStatus.textContent = `${remaining} image${remaining === 1 ? '' : 's'} waiting for ${annotator}`;
  } catch {
    els.queueStatus.hidden = true;
  }
}

// ---- Wiring ----

document.getElementById('btn-annotate-start').addEventListener('click', beginAnnotation);
document.getElementById('btn-annotate-next').addEventListener('click', goNext);
document.getElementById('btn-annotate-prev').addEventListener('click', goPrevious);
document.getElementById('btn-annotate-unusable').addEventListener('click', markUnusable);
document.getElementById('btn-annotate-back').addEventListener('click', returnToAnnotation);
document.getElementById('btn-annotate-submit-next').addEventListener('click', () => submitCurrent(true));
document.getElementById('btn-annotate-submit-exit').addEventListener('click', () => submitCurrent(false));
document.getElementById('btn-annotate-end').addEventListener('click', endSession);

els.fields.addEventListener('submit', (e) => {
  e.preventDefault();
  goNext();
});

[els.annotator, els.family].forEach((input) => {
  input.addEventListener('change', refreshQueueStatus);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') beginAnnotation();
  });
});
els.reviewedOnly.addEventListener('change', refreshQueueStatus);

document.addEventListener('keydown', (e) => {
  if (els.workspace.hidden) return;
  if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) return;
  if (e.key === 'ArrowRight' && !els.page.hidden) goNext();
  else if (e.key === 'ArrowLeft' && !els.page.hidden) goPrevious();
});

const exportForm = document.getElementById('form-annotate-export');
exportForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const status = document.getElementById('annotate-export-status');
  try {
    const { rows, path } = await postJSON('/api/annotate/export', formToObject(exportForm));
    status.hidden = false;
    status.textContent = `Wrote ${rows} row${rows === 1 ? '' : 's'} to ${path}`;
    toast(`Exported ${rows} annotations.`, 'pass');
  } catch (err) {
    toast(`Export failed: ${err.message}`, 'fail');
  }
});

try {
  els.annotator.value = localStorage.getItem(ANNOTATOR_KEY) || '';
} catch { /* storage blocked -- the field just starts empty */ }
refreshQueueStatus();
