const reviewCard = document.getElementById('card-review');
let reviewQueue = [];
let reviewIndex = 0;

// ---- Dedupe scan ----

const dedupeForm = document.getElementById('form-dedupe');
dedupeForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  reviewCard.querySelector('[data-log]').textContent = '';
  const body = formToObject(dedupeForm);
  const { job_id } = await postJSON('/api/jobs/dedupe', body);
  streamJob(reviewCard, job_id, { activityLabel: 'Dedupe', target: 'review' });
});

reattachIfRunning(reviewCard, 'review', {});

// ---- Review queue ----

async function refreshReviewQueue() {
  const family = document.getElementById('review-family-filter').value.trim();
  const params = family ? `?family=${encodeURIComponent(family)}` : '';
  const resp = await fetch(`/api/review/queue${params}`);
  const { queue } = await resp.json();
  reviewQueue = queue;
  reviewIndex = 0;
  renderCurrentItem();
}
document.getElementById('btn-refresh-review-queue').addEventListener('click', refreshReviewQueue);

function currentItem() {
  return reviewQueue[reviewIndex] || null;
}

async function renderCurrentItem() {
  const item = currentItem();
  const empty = document.getElementById('review-empty');
  const itemEl = document.getElementById('review-item');

  if (!item) {
    empty.hidden = false;
    empty.textContent = reviewQueue.length === 0
      ? 'Nothing waiting for review. Try Refresh queue, or run a dedupe scan first.'
      : 'All caught up for this queue. Refresh queue to check for more.';
    itemEl.hidden = true;
    return;
  }

  empty.hidden = true;
  itemEl.hidden = false;
  document.getElementById('review-progress').textContent = `${reviewIndex + 1} of ${reviewQueue.length}: ${item.family}`;
  document.getElementById('review-image').src = `/api/images/${item.id}/file`;

  const dupWrap = document.getElementById('review-duplicates');
  const dupThumbs = document.getElementById('review-duplicates-thumbs');
  if (item.duplicate_siblings.length > 0) {
    dupWrap.hidden = false;
    dupThumbs.innerHTML = item.duplicate_siblings.map((siblingId) => `
      <img src="/api/images/${siblingId}/file" class="review-dup-thumb" alt="Possible duplicate">
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

async function decideCurrent(decision) {
  const item = currentItem();
  if (!item) return;
  if (item.family_choice && decision !== 'reject') {
    toast('This file is in several families. Choose its family first.', 'fail');
    return;
  }
  try {
    await postJSON(`/api/review/${item.id}/decide`, { decision });
  } catch {
    toast('Could not save that decision.', 'fail');
    return;
  }
  if (item.family_choice) {
    await refreshReviewQueue();  // a reject took every copy with it
    return;
  }
  reviewQueue.splice(reviewIndex, 1);
  if (reviewIndex >= reviewQueue.length) reviewIndex = Math.max(0, reviewQueue.length - 1);
  renderCurrentItem();
}

document.getElementById('btn-decide-keep').addEventListener('click', () => decideCurrent('keep'));
document.getElementById('btn-decide-reject').addEventListener('click', () => decideCurrent('reject'));
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
  else if (e.key === 'r' || e.key === 'R') decideCurrent('reject');
  else if (e.key === 'd' || e.key === 'D') decideCurrent('duplicate');
  else if (e.key === 'ArrowRight') document.getElementById('btn-decide-skip').click();
});

document.getElementById('review-family-filter').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') refreshReviewQueue();
});
