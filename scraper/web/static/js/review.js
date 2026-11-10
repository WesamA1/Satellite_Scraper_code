const reviewCard = document.getElementById('card-review');
let reviewQueue = [];
let reviewIndex = 0;

// ---- Dedupe scan ----

const dedupeForm = document.getElementById('form-dedupe');
const dedupeCard = dedupeForm.closest('.discovery-box');
dedupeForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  dedupeCard.querySelector('[data-log]').textContent = '';
  const body = formToObject(dedupeForm);
  const { job_id } = await postJSON('/api/jobs/dedupe', body);
  streamJob(dedupeCard, job_id, { activityLabel: 'Dedupe', target: 'review', onSettled: refreshReviewQueue });
});

reattachIfRunning(dedupeCard, 'review', {});

// ---- Review queue ----

// Two ways through the queue: a page of thumbnails decided in one go (the
// default: most of a scrape is junk, and ticking the few keepers is faster
// than a click per image), or one image at a time with every control.
const VIEW_KEY = 'review-view';
const viewRadios = document.querySelectorAll('input[name="review-view"]');

function savedView() {
  try {
    return localStorage.getItem(VIEW_KEY) === 'single' ? 'single' : 'grid';
  } catch {
    return 'grid';
  }
}

let reviewView = savedView();
viewRadios.forEach((radio) => {
  radio.checked = radio.value === reviewView;
  radio.addEventListener('change', () => setReviewView(radio.value));
});

function setReviewView(view) {
  reviewView = view;
  viewRadios.forEach((radio) => { radio.checked = radio.value === view; });
  try {
    localStorage.setItem(VIEW_KEY, view);
  } catch { /* private window: the choice just isn't remembered */ }
  renderQueue();
}

async function refreshReviewQueue() {
  const family = document.getElementById('review-family-filter').value.trim();
  const params = family ? `?family=${encodeURIComponent(family)}` : '';
  const resp = await fetch(`/api/review/queue${params}`);
  const body = await resp.json();
  reviewQueue = body.queue;
  reviewIndex = 0;
  renderScreenNotice(body);
  renderScreenerStatus(body.screener);
  renderScreenedOutCount(body.screened_out);
  renderQueue(body.waiting);
}
document.getElementById('btn-refresh-review-queue').addEventListener('click', refreshReviewQueue);

function currentItem() {
  return reviewQueue[reviewIndex] || null;
}

function scoreLabel(score) {
  return score === null || score === undefined ? 'not screened' : `score ${score.toFixed(2)}`;
}

let waitingTotal = 0;

function renderQueue(waiting) {
  if (waiting !== undefined) waitingTotal = waiting;
  const empty = document.getElementById('review-empty');
  const gridWrap = document.getElementById('review-grid-wrap');
  const itemEl = document.getElementById('review-item');

  if (reviewQueue.length === 0) {
    empty.hidden = false;
    empty.textContent = 'Nothing waiting for review. Try Refresh queue, or run a dedupe scan first.';
    gridWrap.hidden = true;
    itemEl.hidden = true;
    return;
  }
  empty.hidden = true;
  gridWrap.hidden = reviewView !== 'grid';
  itemEl.hidden = reviewView !== 'single';
  if (reviewView === 'grid') renderGrid();
  else renderCurrentItem();
}

// ---- A page at once ----

const gridEl = document.getElementById('review-grid');
const bulkEls = {
  count: document.getElementById('review-bulk-count'),
  keep: document.getElementById('btn-bulk-keep'),
  apply: document.getElementById('btn-bulk-apply'),
  reason: document.getElementById('review-bulk-reason'),
};

function renderGrid() {
  const more = waitingTotal > reviewQueue.length ? ` (${waitingTotal} waiting in all)` : '';
  document.getElementById('review-grid-progress').textContent = `${reviewQueue.length} on this page${more}`;
  gridEl.innerHTML = reviewQueue.map((item, index) => {
    // A file filed under several families needs its family picked first: one at a time.
    const locked = Boolean(item.family_choice);
    return `
      <div class="review-tile${locked ? ' locked' : ''}">
        <label>
          <input type="checkbox" data-keep="${item.id}"${locked ? ' disabled' : ''}>
          <img src="/api/images/${item.id}/thumb?size=320" loading="lazy" alt="Image ${item.id}, ${esc(item.family)}">
          <span class="review-tile-tick" aria-hidden="true">Keep</span>
        </label>
        <div class="review-tile-meta">
          <span title="${esc(item.family)}">${esc(item.family)}</span>
          <span>${locked ? 'in several families' : scoreLabel(item.filter_score)}</span>
          <button type="button" class="link-btn" data-open="${index}">Open</button>
        </div>
      </div>`;
  }).join('');
  updateBulkBar();
}

function tickedIds() {
  return [...gridEl.querySelectorAll('input[data-keep]:checked')].map((box) => Number(box.dataset.keep));
}

function unlockedIds() {
  return reviewQueue.filter((item) => !item.family_choice).map((item) => item.id);
}

function updateBulkBar() {
  const keep = tickedIds().length;
  const rest = unlockedIds().length - keep;
  bulkEls.count.textContent = `${keep} ticked`;
  bulkEls.keep.disabled = keep === 0;
  bulkEls.apply.disabled = keep + rest === 0;
  bulkEls.apply.textContent = keep > 0 ? `Keep ${keep}, reject the other ${rest}` : `Reject all ${rest}`;
}

gridEl.addEventListener('change', updateBulkBar);
gridEl.addEventListener('click', (e) => {
  const open = e.target.closest('[data-open]');
  if (!open) return;
  reviewIndex = Number(open.dataset.open);
  setReviewView('single');
});

async function decidePage(rejectRest) {
  const keep = tickedIds();
  const reject = rejectRest ? unlockedIds().filter((id) => !keep.includes(id)) : [];
  if (keep.length + reject.length === 0) return;
  if (rejectRest && keep.length === 0 && !window.confirm(`Reject all ${reject.length} images on this page?`)) return;

  bulkEls.keep.disabled = true;
  bulkEls.apply.disabled = true;
  const resp = await fetch('/api/review/bulk', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ keep, reject, reason: bulkEls.reason.value }),
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    toast(body.detail || `Couldn't save those decisions (HTTP ${resp.status}).`, 'fail');
    updateBulkBar();
    return;
  }
  const skipped = body.skipped ? `, ${body.skipped} left for one at a time` : '';
  toast(`Kept ${body.kept}, rejected ${body.rejected}${skipped}.`, 'pass');
  await refreshReviewQueue();
}

bulkEls.keep.addEventListener('click', () => decidePage(false));
bulkEls.apply.addEventListener('click', () => decidePage(true));

// ---- Screening (the filter, run from here) ----

const screenEls = {
  box: document.getElementById('review-screen'),
  count: document.getElementById('review-screen-count'),
  button: document.getElementById('btn-review-screen'),
};
let screeningRunning = false;

function renderScreenNotice({ unscreened }) {
  screenEls.box.hidden = unscreened === 0 && !screeningRunning;
  screenEls.count.textContent = `${unscreened} image${unscreened === 1 ? '' : 's'}`;
  screenEls.button.disabled = screeningRunning;
}

function onScreeningSettled() {
  screeningRunning = false;
  refreshReviewQueue();
}

function onScreeningDone(result) {
  if (result && result.rejected !== undefined) {
    toast(`Screening done: ${result.rejected} set aside, ${result.kept} left to review.`, 'pass');
  }
}

screenEls.button.addEventListener('click', async () => {
  const family = document.getElementById('review-family-filter').value.trim();
  screeningRunning = true;
  screenEls.button.disabled = true;
  try {
    const { job_id } = await postJSON('/api/jobs/filter', family ? { family } : {});
    streamJob(screenEls.box, job_id, {
      activityLabel: 'Filter', target: 'filter', onDone: onScreeningDone, onSettled: onScreeningSettled,
    });
  } catch (err) {
    screeningRunning = false;
    screenEls.button.disabled = false;
    toast(err.message.includes('409') ? 'Screening is already running. Wait for it to finish.' : 'Could not start screening.', 'fail');
  }
});

reattachIfRunning(screenEls.box, 'filter', { onSettled: onScreeningSettled }).then((jobId) => {
  if (!jobId) return;
  screeningRunning = true;
  screenEls.box.hidden = false;
  screenEls.button.disabled = true;
});

// ---- The trained screener: a second opinion that learns from Review decisions ----

function screenerStatusText(info) {
  const { state, needs, check } = info;
  if (state === 'learning') {
    return `learning from your decisions. It has ${info.rejected} of the ${needs.rejected} "Not a satellite" `
      + `rejections it needs, from ${info.rejected_families} of ${needs.rejected_families} families. `
      + 'Until then, screening uses the built-in checks alone.';
  }
  if (state === 'untrained') {
    return 'has enough decisions to learn from, and trains the next time screening runs.';
  }
  const trained = `Trained on ${info.trained_on.kept} kept and ${info.trained_on.rejected} rejected images. `
    + `Checked on families it wasn't trained on, it caught ${check.junk_caught_pct}% of the junk `
    + `and set aside ${check.kept_lost_pct}% of the images you kept`;
  if (state === 'on') {
    return `on, next to the built-in checks. ${trained}.`;
  }
  return `off: it didn't pass its check. ${trained} (it needs at least ${needs.junk_caught_pct}% and `
    + `at most ${needs.kept_lost_pct}%). It tries again as you review more.`;
}

function renderScreenerStatus(info) {
  const line = document.getElementById('review-screener');
  line.hidden = !info;
  if (!info) return;
  line.innerHTML = `<strong>Trained screener:</strong> ${esc(screenerStatusText(info))}`;
}

// ---- Set aside by screening: look through it, undo a wrong call ----

const screenedEls = {
  box: document.getElementById('review-screened-out'),
  count: document.getElementById('review-screened-count'),
  grid: document.getElementById('review-screened-grid'),
};

function renderScreenedOutCount(count) {
  screenedEls.box.hidden = count === 0;
  screenedEls.count.textContent = count;
  if (screenedEls.box.open) loadScreenedOut();
}

async function loadScreenedOut() {
  const family = document.getElementById('review-family-filter').value.trim();
  const params = family ? `?family=${encodeURIComponent(family)}` : '';
  const resp = await fetch(`/api/review/screened-out${params}`);
  const { images } = await resp.json();
  screenedEls.grid.innerHTML = images.map((image) => `
    <div class="review-tile">
      <img src="/api/images/${image.id}/thumb?size=320" loading="lazy" alt="Image ${image.id}, ${esc(image.family)}">
      <div class="review-tile-meta">
        <span title="${esc(image.family)}">${esc(image.family)}</span>
        <span>${image.by_trained_screener ? 'trained screener' : scoreLabel(image.filter_score)}</span>
        <button type="button" class="link-btn" data-keep-after-all="${image.id}">Keep after all</button>
      </div>
    </div>`).join('');
}

screenedEls.box.addEventListener('toggle', () => {
  if (screenedEls.box.open) loadScreenedOut();
});

screenedEls.grid.addEventListener('click', async (e) => {
  const button = e.target.closest('[data-keep-after-all]');
  if (!button) return;
  button.disabled = true;
  const resp = await fetch(`/api/review/${button.dataset.keepAfterAll}/keep-after-all`, { method: 'POST' });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    button.disabled = false;
    toast(body.detail || `Couldn't keep that image (HTTP ${resp.status}).`, 'fail');
    return;
  }
  toast('Kept: it is in the master set now.', 'pass');
  await refreshReviewQueue();
});

// ---- One at a time ----

async function renderCurrentItem() {
  const item = currentItem();
  const empty = document.getElementById('review-empty');
  const itemEl = document.getElementById('review-item');

  if (!item) {
    empty.hidden = false;
    empty.textContent = 'All caught up for this queue. Refresh queue to check for more.';
    itemEl.hidden = true;
    return;
  }

  empty.hidden = true;
  itemEl.hidden = false;
  document.getElementById('review-progress').textContent =
    `${reviewIndex + 1} of ${reviewQueue.length}: ${item.family} · ${scoreLabel(item.filter_score)}`;
  document.getElementById('review-image').src = `/api/images/${item.id}/file`;

  const dupWrap = document.getElementById('review-duplicates');
  const dupThumbs = document.getElementById('review-duplicates-thumbs');
  if (item.duplicate_siblings.length > 0) {
    dupWrap.hidden = false;
    dupThumbs.innerHTML = item.duplicate_siblings.map((siblingId) => `
      <img src="/api/images/${siblingId}/thumb?size=160" class="review-dup-thumb" alt="Possible duplicate">
    `).join('');
  } else {
    dupWrap.hidden = true;
    dupThumbs.innerHTML = '';
  }

  renderFamilyChoice(item);
  await loadVariantControl(item);
}

// ---- Which family? (the same file downloaded under several families) ----

const OTHER_FAMILY = '__other__';
const homeEls = {
  wrap: document.getElementById('review-home'),
  count: document.getElementById('review-home-count'),
  options: document.getElementById('review-home-options'),
  other: document.getElementById('review-home-other'),
  search: document.getElementById('review-home-search'),
  list: document.getElementById('review-home-list'),
  confirm: document.getElementById('btn-review-home'),
};

function renderFamilyChoice(item) {
  const choice = item.family_choice;
  homeEls.wrap.hidden = !choice;
  // Keep/Duplicate wait for the family; Reject still works (it rejects every copy).
  document.getElementById('btn-decide-keep').disabled = Boolean(choice);
  document.getElementById('btn-decide-duplicate').disabled = Boolean(choice);
  if (!choice) return;

  homeEls.count.textContent = choice.families.length;
  homeEls.options.innerHTML = [...choice.families, OTHER_FAMILY].map((family) => `
    <label class="seg-option"><input type="radio" name="review-home" value="${esc(family)}">
      <span>${family === OTHER_FAMILY ? 'Other family…' : esc(family)}</span></label>
  `).join('');
  homeEls.other.hidden = true;
  homeEls.search.value = '';
  homeEls.list.innerHTML = '';
  // An imported copy (from data/) wins, so its family starts selected.
  if (choice.preselected) {
    const radio = homeEls.options.querySelector(`input[value="${CSS.escape(choice.preselected)}"]`);
    if (radio) radio.checked = true;
  }
}

homeEls.options.addEventListener('change', () => {
  const picked = homeEls.options.querySelector('input:checked');
  homeEls.other.hidden = !picked || picked.value !== OTHER_FAMILY;
  if (!homeEls.other.hidden) homeEls.search.focus();
});

let familySearchTimer = null;
homeEls.search.addEventListener('input', () => {
  clearTimeout(familySearchTimer);
  familySearchTimer = setTimeout(async () => {
    const resp = await fetch(`/api/families?q=${encodeURIComponent(homeEls.search.value)}&limit=30`);
    const { families } = await resp.json();
    homeEls.list.innerHTML = families.map((f) => `<option value="${esc(f)}"></option>`).join('');
  }, 150);
});

homeEls.confirm.addEventListener('click', async () => {
  const item = currentItem();
  const picked = homeEls.options.querySelector('input:checked');
  if (!item || !picked) {
    toast('Pick the family this image belongs to first.', 'fail');
    return;
  }
  const family = picked.value === OTHER_FAMILY ? homeEls.search.value.trim() : picked.value;
  if (!family) {
    toast('Type the family name, then pick it from the list.', 'fail');
    return;
  }
  const resp = await fetch(`/api/review/${item.id}/family`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ family }),
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    toast(body.detail || `Couldn't set the family (HTTP ${resp.status}).`, 'fail');
    return;
  }
  toast(`Filed under ${body.family}; ${body.quarantined} other copy(ies) moved to quarantine.`, 'pass');
  await refreshReviewQueue();  // the other copies left the queue; the kept one comes up to keep or reject
});

// Variants: the catalog's own plus any added to known_variants.json. "Other
// variant…" types a new one, which can be added to the family's list.
const OTHER_VARIANT = '__other__';
const variantEls = {
  select: document.getElementById('review-variant-select'),
  text: document.getElementById('review-variant-text'),
  rememberRow: document.getElementById('review-variant-remember-row'),
  remember: document.getElementById('review-variant-remember'),
};

function showVariantText(show) {
  variantEls.text.hidden = !show;
  variantEls.rememberRow.hidden = !show;
  if (show) {
    variantEls.text.value = '';
    variantEls.remember.checked = true;
  }
}

async function loadVariantControl(item, selected = '') {
  const resp = await fetch(`/api/review/variants?family=${encodeURIComponent(item.family)}`);
  const { variants } = await resp.json();

  if (variants.length > 0) {
    variantEls.select.innerHTML = '<option value="">(no variant selected)</option>'
      + variants.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join('')
      + `<option value="${OTHER_VARIANT}">Other variant…</option>`;
    variantEls.select.value = selected;
    variantEls.select.hidden = false;
    showVariantText(false);
  } else {
    variantEls.select.hidden = true;
    showVariantText(true);
  }
}

variantEls.select.addEventListener('change', () => {
  showVariantText(variantEls.select.value === OTHER_VARIANT);
  if (!variantEls.text.hidden) variantEls.text.focus();
});

document.getElementById('btn-save-variant').addEventListener('click', async () => {
  const item = currentItem();
  if (!item) return;
  const typed = !variantEls.text.hidden;
  const variant = typed ? variantEls.text.value.trim() : variantEls.select.value;
  if (!variant) {
    toast('Pick or type a variant first.', 'fail');
    return;
  }
  const resp = await fetch(`/api/review/${item.id}/variant`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ variant, remember: typed && variantEls.remember.checked }),
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    toast(body.detail || `Couldn't save the variant (HTTP ${resp.status}).`, 'fail');
    return;
  }
  toast(body.added_to_known ? `Saved variant: ${variant}, now a known variant of ${item.family}` : `Saved variant: ${variant}`, 'pass');
  if (body.added_to_known) await loadVariantControl(item, variant);
});

async function decideCurrent(decision, reason = null) {
  const item = currentItem();
  if (!item) return;
  if (item.family_choice && decision !== 'reject') {
    toast('This file is in several families. Choose its family first.', 'fail');
    return;
  }
  try {
    await postJSON(`/api/review/${item.id}/decide`, { decision, reason });
  } catch {
    toast('Could not save that decision.', 'fail');
    return;
  }
  if (item.family_choice) {
    await refreshReviewQueue();  // a reject took every copy with it
    return;
  }
  reviewQueue.splice(reviewIndex, 1);
  if (reviewQueue.length === 0) {
    await refreshReviewQueue();  // the next page, if there is one
    return;
  }
  if (reviewIndex >= reviewQueue.length) reviewIndex = reviewQueue.length - 1;
  renderCurrentItem();
}

document.getElementById('btn-decide-keep').addEventListener('click', () => decideCurrent('keep'));
// Each reject says why (saved with the image): the reasons are what a future
// screening classifier learns from.
const rejectButtons = document.querySelectorAll('[data-reason]');
rejectButtons.forEach((btn) => btn.addEventListener('click', () => decideCurrent('reject', btn.dataset.reason)));
const rejectByKey = Object.fromEntries([...rejectButtons].map((btn) => [btn.dataset.key, btn.dataset.reason]));
document.getElementById('btn-decide-duplicate').addEventListener('click', () => decideCurrent('duplicate'));
document.getElementById('btn-decide-skip').addEventListener('click', () => {
  if (reviewIndex < reviewQueue.length - 1) {
    reviewIndex += 1;
    renderCurrentItem();
  }
});

document.addEventListener('keydown', (e) => {
  if (!document.getElementById('review-item') || document.getElementById('review-item').hidden) return;
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;

  if (e.key === 'k' || e.key === 'K') decideCurrent('keep');
  else if (rejectByKey[e.key.toUpperCase()]) decideCurrent('reject', rejectByKey[e.key.toUpperCase()]);
  else if (e.key === 'd' || e.key === 'D') decideCurrent('duplicate');
  else if (e.key === 'ArrowRight') document.getElementById('btn-decide-skip').click();
});

document.getElementById('review-family-filter').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') refreshReviewQueue();
});

refreshReviewQueue();
