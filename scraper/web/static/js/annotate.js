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
// "Can't tell from this image" -- every non-radio field gets a toggle for it
// (radio fields already have Unsure, which is stored as the same value).
const UNKNOWN = 'Unknown';
const RANGE_DEFAULTS = { Number: [0, 100, 1], Slider: [0, 100, 1], 'Rating Scale': [1, 5, 1] };
const ANNOTATOR_KEY = 'groundstation.annotator';

// "folder" = the old tool's setup: images from a folder, rows appended to its
// CSV, finished images moved aside (see scraper/stages/annotate_folder.py).
const FOLDER_MODE = (window.ANNOTATE_SOURCE || {}).mode === 'folder';

const state = {
  schema: {},
  version: null,
  pages: [],
  queue: [],
  remaining: 0,
  done: 0,       // images submitted this session
  total: 0,      // images in the queue when the session started
  index: 0,
  page: 0,
  values: {},
  unusable: false,
  annotator: '',
  family: '',
  reviewedOnly: true,
  skipOthers: true,
  submitting: false,
};

const els = {
  setup: document.getElementById('annotate-setup'),
  annotator: document.getElementById('annotate-annotator'),
  family: document.getElementById('annotate-family'),
  reviewedOnly: document.getElementById('annotate-reviewed-only'),
  skipOthers: document.getElementById('annotate-skip-others'),
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

// Dropdowns start on a blank choice rather than their first option, so a
// field nobody touched can't quietly save an answer (the old tool's
// preselected first option likely inflated answers like "No Frame of Reference").
function dropdownOptions(spec) {
  return `<option value="">Pick one…</option>${spec[1].map((o) => `<option value="${esc(o)}">${esc(o)}</option>`).join('')}`;
}

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
    return `<select id="${id}">${dropdownOptions(spec)}</select>`;
  }
  if (type === 'DropdownOther') {
    return `<div class="other-row">
      <select id="${id}">${dropdownOptions(spec)}</select>
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
    const toggle = RADIO_TYPES.includes(spec[0]) ? '' : `<button type="button" class="unknown-toggle"
      aria-pressed="false" title="Can't tell from this image">Unknown</button>`;
    return `<div class="attr-field" data-index="${i}">
      <div class="attr-label"><span>${esc(name)}${hint ? ` <small>${hint}</small>` : ''}</span>${modelTag(name)}${toggle}</div>
      ${fieldControl(name, spec, i)}
      <div class="attr-error" hidden></div>
    </div>`;
  }).join('');

  page.attrs.forEach(([name, spec], i) => restoreField(spec, i, state.values[name]));

  els.fields.querySelectorAll('.unknown-toggle').forEach((btn) => {
    const index = Number(btn.closest('.attr-field').dataset.index);
    btn.addEventListener('click', () => {
      setUnknown(index, btn.getAttribute('aria-pressed') !== 'true');
      showFieldError(index, null);
    });
  });

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
// minimum for numbers and sliders). Dropdowns stay on their blank choice.
function restoreField(spec, i, value) {
  const type = spec[0];
  const id = `attr-${i}`;
  const el = document.getElementById(id);
  if (!RADIO_TYPES.includes(type)) {
    setUnknown(i, isUnknown(value));
    if (isUnknown(value)) value = undefined;  // the control underneath starts at its default
  }
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

function isUnknown(value) {
  return value === UNKNOWN || (Array.isArray(value) && value.length === 1 && value[0] === UNKNOWN);
}

// Marks a field "can't tell": its control is greyed out and ignored, and the
// field reads as Unknown until the toggle is pressed again.
function setUnknown(i, on) {
  const field = els.fields.querySelector(`.attr-field[data-index="${i}"]`);
  const btn = field && field.querySelector('.unknown-toggle');
  if (!btn) return;
  btn.setAttribute('aria-pressed', String(on));
  field.classList.toggle('is-unknown', on);
  field.querySelectorAll('input, select, textarea').forEach((el) => { el.disabled = on; });
}

function readField(spec, i) {
  const type = spec[0];
  const id = `attr-${i}`;
  const el = document.getElementById(id);

  if (els.fields.querySelector(`.attr-field[data-index="${i}"]`)?.classList.contains('is-unknown')) {
    return type === 'Multi-Select' ? [UNKNOWN] : UNKNOWN;
  }

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
  if (isUnknown(value)) return null;
  if (RADIO_TYPES.includes(type)) return value ? null : 'Pick Present, Unsure, or Not present.';
  if (type === 'Rating Scale') return value === null ? 'Pick a rating.' : null;
  if (type === 'Text Box' || type === 'Long Text') return value ? null : 'Required.';
  if (type === 'Number' || type === 'Slider') {
    const [lo, hi] = rangeParams(spec);
    if (value === null || !Number.isInteger(value)) return 'Enter a whole number.';
    return value < lo || value > hi ? `Must be between ${lo} and ${hi}.` : null;
  }
  if (type === 'Dropdown') return value ? null : 'Pick an option.';
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
  // Counts up against a total fixed at session start; grows only if new
  // images show up mid-session.
  const imageTotal = Math.max(state.total, state.done + state.remaining);
  els.progress.textContent = `Image ${(state.done + 1).toLocaleString()} of ${imageTotal.toLocaleString()} · ${where}`;

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

  const scored = !state.unusable && Object.keys(autofill.suggestions).length > 0;
  els.summary.classList.toggle('with-model', scored);
  els.summaryBody.innerHTML = Object.entries(state.schema).map(([category, attributes]) => `
    <tr class="summary-category"><td colspan="3">${esc(category)}</td></tr>
    ${Object.keys(attributes).map((name) => `
      <tr><td>${esc(name)}</td><td class="summary-value">${esc(state.unusable ? 'UNUSABLE' : formatValue(state.values[name]))}</td>
        <td class="summary-model-col">${scored ? modelVerdict(name) : ''}</td></tr>
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
  if (FOLDER_MODE) {
    const resp = await fetch('/api/annotate/folder/queue?limit=50');
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
    state.queue = data.queue;
    state.remaining = data.remaining;
    state.index = 0;
    return;
  }
  const params = new URLSearchParams({
    annotator: state.annotator,
    reviewed_only: state.reviewedOnly,
    skip_annotated_by_others: state.skipOthers,
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
  if (FOLDER_MODE) return 'The image folder is empty: everything in it has been annotated.';
  if (state.reviewedOnly) {
    return 'Nothing left to annotate here. Only images already kept on the Review page are included. '
      + 'Review more images, or untick that box to annotate unreviewed ones too.';
  }
  return state.skipOthers
    ? 'Nothing left to annotate: every matching image has been annotated by someone.'
    : 'Nothing left to annotate for this annotator and family.';
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
  els.image.src = FOLDER_MODE
    ? `/api/annotate/folder/image?name=${encodeURIComponent(item.name)}`
    : `/api/images/${item.id}/file`;
  els.image.alt = item.family ? `${item.family}: ${item.name}` : item.name;
  autofill.token += 1;  // stops a fill still running for the previous image
  autofill.suggestions = {};
  renderPage();
  renderAutofillStatus();
  if (afEls.every.checked && autofill.model?.available) runAutofill();
}

async function beginAnnotation() {
  const annotator = els.annotator.value.trim();
  if (!annotator && !FOLDER_MODE) {
    toast('Enter an annotator name first.', 'fail');
    els.annotator.focus();
    return;
  }
  state.annotator = annotator;
  state.family = els.family.value.trim();
  state.reviewedOnly = els.reviewedOnly.checked;
  state.skipOthers = els.skipOthers.checked;
  if (annotator) {
    try { localStorage.setItem(ANNOTATOR_KEY, annotator); } catch { /* private window -- just don't remember it */ }
  }

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

  try {
    await fetchQueue();
  } catch (e) {
    toast(`Couldn't read the image folder: ${e.message}`, 'fail');
    return;
  }
  state.done = 0;
  state.total = state.remaining;
  await loadModelInfo();
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
    const payload = { values: state.values, unusable: state.unusable, schema_version: state.version };
    const resp = FOLDER_MODE
      ? await fetch('/api/annotate/folder/submit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: item.name, ...payload }),
      })
      : await fetch(`/api/annotate/${item.id}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ annotator: state.annotator, ...payload }),
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

    toast(`Saved ${item.name}${state.unusable ? ' as unusable' : ''}.${scoreSubmission()}`, 'pass');
    state.queue.splice(state.index, 1);
    state.remaining = Math.max(0, state.remaining - 1);
    state.done += 1;

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
  if (FOLDER_MODE) {
    try {
      const resp = await fetch('/api/annotate/folder/queue?limit=1');
      const data = await resp.json();
      els.queueStatus.hidden = false;
      els.queueStatus.textContent = resp.ok
        ? `${data.remaining} image${data.remaining === 1 ? '' : 's'} left in the image folder`
        : data.detail;
    } catch {
      els.queueStatus.hidden = true;
    }
    return;
  }
  const annotator = els.annotator.value.trim();
  if (!annotator) {
    els.queueStatus.hidden = true;
    return;
  }
  const params = new URLSearchParams({
    annotator,
    reviewed_only: els.reviewedOnly.checked,
    skip_annotated_by_others: els.skipOthers.checked,
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
els.skipOthers.addEventListener('change', refreshQueueStatus);

document.addEventListener('keydown', (e) => {
  if (els.workspace.hidden) return;
  if (['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName)) return;
  if (e.key === 'ArrowRight' && !els.page.hidden) goNext();
  else if (e.key === 'ArrowLeft' && !els.page.hidden) goPrevious();
});

// ---- Setup (image source) ----

const sourceForm = document.getElementById('form-annotate-source');
const folderFields = sourceForm.querySelector('[data-source-folder]');

function selectedMode() {
  const checked = sourceForm.querySelector('input[name="mode"]:checked');
  return checked ? checked.value : 'database';
}

sourceForm.querySelectorAll('input[name="mode"]').forEach((radio) => {
  radio.addEventListener('change', () => { folderFields.hidden = selectedMode() !== 'folder'; });
});

document.getElementById('btn-old-tool-fill').addEventListener('click', async () => {
  const location = document.getElementById('old-tool-location').value.trim();
  if (!location) {
    toast("Enter the old tool's folder first.", 'fail');
    return;
  }
  const resp = await fetch('/api/annotate/source/old-tool', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ location }),
  });
  const body = await resp.json();
  if (!resp.ok) {
    toast(body.detail || `HTTP ${resp.status}`, 'fail');
    return;
  }
  for (const key of ['imageSource', 'imageDestination', 'CSVDest', 'attributesPath']) {
    sourceForm.elements[key].value = body[key];
  }
  toast('Filled in from the old tool. Check the paths, then Save setup.', 'pass');
});

sourceForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = { mode: selectedMode() };
  for (const key of ['imageSource', 'imageDestination', 'CSVDest', 'attributesPath']) {
    body[key] = sourceForm.elements[key].value.trim();
  }
  const resp = await fetch('/api/annotate/source', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const result = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    toast(result.detail || `HTTP ${resp.status}`, 'fail');
    return;
  }
  window.location.reload();
});

// ---- Import from the old tool ----

const importForm = document.getElementById('form-annotate-import');
const importButton = importForm.querySelector('button[type="submit"]');
const importDryRun = importForm.elements.dry_run;
importDryRun.addEventListener('change', () => {
  importButton.textContent = importDryRun.checked ? 'Check import' : 'Import annotations';
});

function renderImportResult(r) {
  const verb = r.dry_run ? 'Would import' : 'Imported';
  const rows = [
    [verb, r.imported],
    ['Skipped: no image with that file name', r.unmatched],
    ['Skipped: file name matches several images', r.ambiguous],
    ['Skipped: values in the wrong column', r.misaligned],
    [`Skipped: already annotated by ${r.annotator}`, r.already_annotated],
  ];
  const renamed = Object.entries(r.column_mapping).filter(([from, to]) => from !== to);
  const notes = [];
  if (renamed.length) notes.push(`Columns renamed: ${renamed.map(([f, t]) => `${f} → ${t}`).join(', ')}`);
  if (r.unmapped_columns.length) notes.push(`Columns not imported (no matching attribute): ${r.unmapped_columns.join(', ')}`);
  if (r.missing_attributes.length) notes.push(`Left blank (not in the old CSV): ${r.missing_attributes.join(', ')}`);
  const offSchema = Object.entries(r.off_schema);
  if (offSchema.length) {
    notes.push(`Kept as written but not in today's options: ${offSchema.map(([a, n]) => `${a} (${n})`).join(', ')}`);
  }
  if (r.review_path) notes.push(`Skipped rows, with reasons: ${r.review_path}`);

  const el = document.getElementById('annotate-import-result');
  el.hidden = false;
  el.innerHTML = `
    <div class="table-wrap"><table><tbody>
      <tr><th colspan="2">${esc(r.rows)} rows in the old CSV${r.dry_run ? ' · dry run, nothing written' : ''}</th></tr>
      ${rows.map(([label, n]) => `<tr><td>${esc(label)}</td><td>${esc(n)}</td></tr>`).join('')}
    </tbody></table></div>
    ${notes.map((n) => `<p class="muted import-note">${esc(n)}</p>`).join('')}`;
}

importForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = formToObject(importForm);
  if (!body.csv_path) {
    toast('Enter the path to the old CSV first.', 'fail');
    return;
  }
  importButton.disabled = true;
  try {
    const resp = await fetch('/api/annotate/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const result = await resp.json();
    if (!resp.ok) {
      toast(result.detail || `Import failed (HTTP ${resp.status}).`, 'fail');
      return;
    }
    renderImportResult(result);
    if (!result.dry_run) {
      toast(`Imported ${result.imported} annotations.`, 'pass');
      refreshQueueStatus();
    }
  } finally {
    importButton.disabled = false;
  }
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

// ---- Import a data folder ----

const importDataCard = document.getElementById('card-import-data');
const importDataForm = document.getElementById('form-import-data');
const importDataButton = importDataForm.querySelector('button[type="submit"]');

function onImportDataSettled() {
  importDataButton.disabled = false;
}

importDataForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  importDataButton.disabled = true;
  importDataCard.querySelector('[data-log]').textContent = '';
  try {
    const { job_id } = await postJSON('/api/jobs/import-data', formToObject(importDataForm));
    streamJob(importDataCard, job_id, {
      activityLabel: 'Import data',
      target: 'review',
      onDone: () => refreshQueueStatus(),
      onSettled: onImportDataSettled,
    });
  } catch (err) {
    onImportDataSettled();
    toast(err.message.includes('409') ? 'A duplicate scan or import is already running.' : 'Could not start the import.', 'fail');
  }
});

// ---- Auto-fill: the trained model's guesses ----
// Fills fields with the model's answers for the annotator to check -- it
// never submits. "Show it filling in" walks through the pages filling one
// field at a time; "Auto-fill every image" does it as each image opens.

const AUTOFILL_KEY = 'groundstation.autofill';
const FILL_STEP_MS = 180;
const PAGE_PAUSE_MS = 350;
const autofill = { model: null, suggestions: {}, token: 0, running: false, hurry: false, tally: { matched: 0, total: 0 } };
const afEls = {
  button: document.getElementById('btn-annotate-autofill'),
  watch: document.getElementById('annotate-autofill-watch'),
  every: document.getElementById('annotate-autofill-every'),
  status: document.getElementById('annotate-autofill-status'),
};

try {
  const saved = JSON.parse(localStorage.getItem(AUTOFILL_KEY) || '{}');
  if (typeof saved.watch === 'boolean') afEls.watch.checked = saved.watch;
  if (typeof saved.every === 'boolean') afEls.every.checked = saved.every;
} catch { /* private window -- defaults are fine */ }
for (const box of [afEls.watch, afEls.every]) {
  box.addEventListener('change', () => {
    try {
      localStorage.setItem(AUTOFILL_KEY, JSON.stringify({ watch: afEls.watch.checked, every: afEls.every.checked }));
    } catch { /* not remembered, that's all */ }
  });
}

const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

async function loadModelInfo() {
  try {
    autofill.model = await (await fetch('/api/annotate/model')).json();
  } catch {
    autofill.model = { available: false, message: "Couldn't check for a trained model." };
  }
  afEls.button.disabled = !autofill.model.available;
  afEls.button.title = autofill.model.available
    ? `Fill in this image with ${autofill.model.label}'s guesses`
    : autofill.model.message;
  renderAutofillStatus();
}

function renderAutofillStatus(message) {
  if (message) {
    afEls.status.textContent = message;
    return;
  }
  if (!autofill.model?.available) {
    afEls.status.textContent = autofill.model ? autofill.model.message : '';
    return;
  }
  const { matched, total } = autofill.tally;
  afEls.status.textContent = total
    ? `Model agreed on ${matched} of ${total} fields this session (${Math.round((matched / total) * 100)}%)`
    : `Model: ${autofill.model.label}`;
}

function modelTag(name) {
  const s = autofill.suggestions[name];
  if (!s) return '';
  const pct = Math.round(s.confidence * 100);
  return `<span class="model-tag${s.unsure ? ' unsure' : ''}" title="${s.unsure ? 'The model is unsure about this one' : 'The model\'s guess'}">model · ${pct}%</span>`;
}

// Same answer, for scoring: lists compare as sets, and an empty multi-select is "None".
function sameValue(a, b) {
  const norm = (v) => {
    if (!Array.isArray(v)) return JSON.stringify(v ?? null);
    const items = v.filter((x) => x !== 'None').sort();
    return JSON.stringify(items);
  };
  return norm(a) === norm(b);
}

function modelVerdict(name) {
  const s = autofill.suggestions[name];
  if (!s) return '<span class="muted">–</span>';
  if (isUnknown(state.values[name])) return '<span class="muted">not scored (Unknown)</span>';
  return sameValue(state.values[name], s.value)
    ? '<span class="model-match">✓</span>'
    : `<span class="model-miss">✗ said ${esc(formatValue(s.value))}</span>`;
}

// After a submit: how many of the model's guesses the annotator kept.
function scoreSubmission() {
  if (state.unusable) return '';
  const scored = Object.entries(autofill.suggestions).filter(([name]) => !isUnknown(state.values[name]));
  if (!scored.length) return '';
  const matched = scored.filter(([name, s]) => sameValue(state.values[name], s.value)).length;
  autofill.tally.matched += matched;
  autofill.tally.total += scored.length;
  renderAutofillStatus();
  return ` Model matched ${matched} of ${scored.length} fields.`;
}

function firstPageToCheck() {
  const index = state.pages.findIndex((page) => page.attrs.some(([name]) => !(name in autofill.suggestions)));
  return index === -1 ? 0 : index;
}

async function runAutofill() {
  const item = state.queue[state.index];
  if (!item || !autofill.model?.available) return;
  if (autofill.running) {
    autofill.hurry = true;  // the button reads "Finish now" while it's filling
    return;
  }
  const token = ++autofill.token;
  collectPage(false);
  afEls.button.disabled = true;
  renderAutofillStatus('Looking at the image…');

  let data;
  try {
    const resp = FOLDER_MODE
      ? await fetch('/api/annotate/folder/suggest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: item.name }),
      })
      : await fetch(`/api/annotate/${item.id}/suggest`, { method: 'POST' });
    data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
  } catch (e) {
    if (token === autofill.token) {
      toast(`Auto-fill failed: ${e.message}`, 'fail');
      renderAutofillStatus();
    }
    afEls.button.disabled = !autofill.model?.available;
    return;
  }
  if (token !== autofill.token) return;  // moved on to another image meanwhile

  autofill.suggestions = data.suggestions;
  const filled = Object.keys(data.suggestions).length;
  const total = state.pages.reduce((n, page) => n + page.attrs.length, 0);
  if (afEls.watch.checked) {
    await watchFill(token);
  } else {
    for (const [name, s] of Object.entries(data.suggestions)) state.values[name] = s.value;
  }
  if (token !== autofill.token) return;
  state.page = firstPageToCheck();
  renderPage();
  afEls.button.disabled = false;
  renderAutofillStatus(`Filled ${filled} of ${total} fields. Check them, fill in the rest, then submit.`);
}

// Walks the pages filling one field at a time, so you can watch the model work.
async function watchFill(token) {
  autofill.running = true;
  autofill.hurry = false;
  afEls.button.disabled = false;
  afEls.button.textContent = 'Finish now';
  try {
    for (let p = 0; p < state.pages.length; p += 1) {
      if (token !== autofill.token) return;
      const fills = state.pages[p].attrs.map(([name, spec], i) => [name, spec, i]).filter(([name]) => autofill.suggestions[name]);
      if (!fills.length) continue;
      state.page = p;
      renderPage();
      for (const [name, spec, i] of fills) {
        if (!autofill.hurry) await sleep(FILL_STEP_MS);
        if (token !== autofill.token) return;
        state.values[name] = autofill.suggestions[name].value;
        restoreField(spec, i, state.values[name]);
        const field = els.fields.querySelector(`.attr-field[data-index="${i}"]`);
        field?.classList.add('just-filled');
      }
      if (!autofill.hurry) await sleep(PAGE_PAUSE_MS);
    }
  } finally {
    autofill.running = false;
    afEls.button.textContent = 'Auto-fill';
  }
}

afEls.button.addEventListener('click', runAutofill);
