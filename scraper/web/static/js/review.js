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

  await loadVariantControl(item);
}

async function loadVariantControl(item) {
  const select = document.getElementById('review-variant-select');
  const textInput = document.getElementById('review-variant-text');
  const resp = await fetch(`/api/review/variants?family=${encodeURIComponent(item.family)}`);
  const { variants } = await resp.json();

  if (variants.length > 0) {
    select.innerHTML = '<option value="">(no variant selected)</option>'
      + variants.map((v) => `<option value="${v}">${v}</option>`).join('');
    select.hidden = false;
    textInput.hidden = true;
  } else {
    select.hidden = true;
    textInput.hidden = false;
    textInput.value = '';
  }
}

document.getElementById('btn-save-variant').addEventListener('click', async () => {
  const item = currentItem();
  if (!item) return;
  const select = document.getElementById('review-variant-select');
  const textInput = document.getElementById('review-variant-text');
  const variant = select.hidden ? textInput.value.trim() : select.value;
  if (!variant) {
    toast('Pick or type a variant first.', 'fail');
    return;
  }
  await postJSON(`/api/review/${item.id}/variant`, { variant });
  toast(`Saved variant: ${variant}`, 'pass');
});

async function decideCurrent(decision) {
  const item = currentItem();
  if (!item) return;
  await postJSON(`/api/review/${item.id}/decide`, { decision });
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
