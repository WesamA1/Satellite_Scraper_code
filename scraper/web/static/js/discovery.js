const discoveryCard = document.getElementById('card-discovery');
const codeReviewPanel = document.getElementById('candidate-code-review');
let reviewingCandidateId = null;
let candidateRows = [];
const CANDIDATES_CAP = 200;

// ---- Auto-discovery (primary action on this page) ----

async function refreshQueueStatus() {
  const resp = await fetch('/api/discovery/queue-status');
  const status = await resp.json();
  const line = document.getElementById('queue-status-line');
  if (status.remaining === 0) {
    line.textContent = `All ${status.total} families searched.`;
  } else {
    line.textContent = `${status.searched} of ${status.total} families searched · ${status.remaining} remaining.`;
  }
}

const autoDiscoverForm = document.getElementById('form-auto-discover');
autoDiscoverForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  discoveryCard.querySelector('[data-log]').textContent = '';
  const body = formToObject(autoDiscoverForm);
  const { job_id } = await postJSON('/api/jobs/discover/auto', body);
  streamJob(discoveryCard, job_id, {
    onDone: () => { refreshCandidates(); refreshQueueStatus(); },
    activityLabel: 'Auto-discovery',
    target: 'discovery',
  });
});

refreshQueueStatus();

// ---- Load known sources (bulk, from the hand-maintained seed list) ----

document.getElementById('btn-load-seed-candidates').addEventListener('click', async () => {
  const { added } = await postJSON('/api/candidates/seed', {});
  toast(added > 0 ? `Added ${added} candidate(s) from the seed list.` : 'Nothing new. Already loaded.', 'pass');
  refreshCandidates();
});

// ---- Add a known source (manual, no search involved) ----

const manualCandidateForm = document.getElementById('form-manual-candidate');
manualCandidateForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = formToObject(manualCandidateForm);
  try {
    await postJSON('/api/candidates/manual', body);
    toast('Candidate added. Review it below.', 'pass');
    manualCandidateForm.reset();
    refreshCandidates();
  } catch {
    toast('Could not add that candidate. Check the URL and try again.', 'fail');
  }
});

// ---- Manual (advanced) discovery ----

const discoverForm = document.getElementById('form-discover');
discoverForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  discoveryCard.querySelector('[data-log]').textContent = '';
  const body = formToObject(discoverForm);
  const { job_id } = await postJSON('/api/jobs/discover', body);
  streamJob(discoveryCard, job_id, { onDone: refreshCandidates, activityLabel: 'Discovery', target: 'discovery' });
});

// ---- Candidates table ----

const CANDIDATE_STATUS_VARIANT = {
  pending: 'pending',
  approved: 'pass',
  rejected: 'fail',
  code_generated: 'pass',
  promoted: 'done',
};

function candidateActionButtons(candidate) {
  const id = candidate.id;
  if (candidate.status === 'pending') {
    return `
      <button class="action-btn" data-action="approve" data-id="${id}">Approve</button>
      <button class="action-btn secondary" data-action="reject" data-id="${id}">Reject</button>
    `;
  }
  if (candidate.status === 'approved') {
    return `<button class="action-btn" data-action="generate" data-id="${id}">Generate integration</button>`;
  }
  if (candidate.status === 'code_generated') {
    return `
      <button class="action-btn" data-action="review" data-id="${id}">Review code</button>
      <button class="action-btn secondary" data-action="generate" data-id="${id}">Regenerate</button>
    `;
  }
  if (candidate.status === 'promoted') {
    return `<button class="action-btn secondary" data-action="review" data-id="${id}">View code</button>`;
  }
  return '';
}

function renderCandidatesTable() {
  const query = document.getElementById('candidates-search').value.trim().toLowerCase();
  const tbody = document.querySelector('#candidates-table tbody');
  const rows = query
    ? candidateRows.filter((c) => c.name.toLowerCase().includes(query) || c.url.toLowerCase().includes(query))
    : candidateRows;

  if (rows.length === 0) {
    const pendingOnly = document.getElementById('candidates-pending-only').checked;
    tbody.innerHTML = emptyRow(6, pendingOnly ? 'Nothing pending review.' : 'No candidates yet. Try a search above.');
    return;
  }

  const shown = rows.slice(0, CANDIDATES_CAP);
  let html = shown.map((c) => `
    <tr>
      <td>${c.name}</td>
      <td><a href="${c.url}" target="_blank" rel="noopener">${c.url}</a></td>
      <td>${c.license_model || '--'}</td>
      <td>${c.license_verifiable ? pill('yes', 'pass') : pill('no', 'neutral')}</td>
      <td>${pill(c.status, CANDIDATE_STATUS_VARIANT[c.status] || 'neutral')}</td>
      <td>${candidateActionButtons(c)}</td>
    </tr>
  `).join('');
  if (rows.length > CANDIDATES_CAP) {
    html += emptyRow(6, `Showing ${CANDIDATES_CAP} of ${rows.length}. Narrow your search to see more.`);
  }
  tbody.innerHTML = html;
}

async function refreshCandidates() {
  const pendingOnly = document.getElementById('candidates-pending-only').checked;
  const resp = await fetch(`/api/candidates?pending=${pendingOnly}`);
  const data = await resp.json();
  candidateRows = data.candidates;
  renderCandidatesTable();
}

document.querySelector('#candidates-table tbody').addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-action]');
  if (!btn) return;
  const id = btn.dataset.id;
  const action = btn.dataset.action;

  if (action === 'approve') {
    await fetch(`/api/candidates/${id}/approve`, { method: 'POST' });
    toast('Candidate approved.', 'pass');
    refreshCandidates();
  } else if (action === 'reject') {
    await fetch(`/api/candidates/${id}/reject`, { method: 'POST' });
    refreshCandidates();
  } else if (action === 'generate') {
    discoveryCard.querySelector('[data-log]').textContent = '';
    const { job_id } = await postJSON('/api/jobs/codegen', { candidate_id: Number(id) });
    streamJob(discoveryCard, job_id, { onDone: refreshCandidates, activityLabel: 'Codegen', target: 'discovery' });
  } else if (action === 'review') {
    const candidateName = btn.closest('tr').children[0].textContent;
    const resp = await fetch(`/api/candidates/${id}/code`);
    const data = await resp.json();
    if (!resp.ok) {
      toast(`Could not load code: ${data.detail}`, 'fail');
      return;
    }
    reviewingCandidateId = id;
    codeReviewPanel.querySelector('[data-candidate-name]').textContent = candidateName;
    codeReviewPanel.querySelector('[data-candidate-code]').textContent = data.code;
    codeReviewPanel.hidden = false;
    codeReviewPanel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
});

// Promote is the one action that writes a file into scraper/sources/ and
// registers it -- the only place in the app that gets a confirm step. Click
// once to arm it, click again within 3s to actually do it.
const promoteBtn = document.getElementById('btn-promote-candidate');
let promoteConfirmTimeout = null;

function resetPromoteButton() {
  promoteBtn.classList.remove('confirming');
  promoteBtn.textContent = 'Promote';
}

promoteBtn.addEventListener('click', async () => {
  if (!promoteBtn.classList.contains('confirming')) {
    promoteBtn.classList.add('confirming');
    promoteBtn.textContent = 'Click again to confirm';
    promoteConfirmTimeout = setTimeout(resetPromoteButton, 3000);
    return;
  }

  clearTimeout(promoteConfirmTimeout);
  resetPromoteButton();

  const resp = await fetch(`/api/candidates/${reviewingCandidateId}/promote`, { method: 'POST' });
  const data = await resp.json();
  if (!resp.ok) {
    toast(`Could not promote: ${data.detail}`, 'fail');
    return;
  }
  toast(`Promoted to ${data.promoted_to}. Restart scrape/serve for it to be available.`, 'pass');
  codeReviewPanel.hidden = true;
  refreshCandidates();
});

document.getElementById('btn-close-code-review').addEventListener('click', () => {
  codeReviewPanel.hidden = true;
  reviewingCandidateId = null;
  resetPromoteButton();
});

document.getElementById('candidates-pending-only').addEventListener('change', refreshCandidates);
document.getElementById('btn-refresh-candidates').addEventListener('click', refreshCandidates);
document.getElementById('candidates-search').addEventListener('input', renderCandidatesTable);
refreshCandidates(); // local DB read only, safe to auto-load

// Auto-discovery, manual discovery, and codegen all share this page's one
// target ('discovery') -- whichever of the three is actually running,
// refreshing both of these covers it.
reattachIfRunning(discoveryCard, 'discovery', {
  onDone: () => { refreshCandidates(); refreshQueueStatus(); },
});
