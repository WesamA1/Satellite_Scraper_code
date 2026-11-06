// Ground Station web GUI -- shared helpers, loaded on every page.
//
// Each page is now a real server-rendered route (see scraper/web/app.py and
// scraper/web/templates/) instead of one hash-routed single-page shell, so
// there's no more client-side panel switching here. What's shared across
// pages is: toasts, form/job plumbing, and keeping the sidebar's nav badges
// and the activity strip in sync with jobs that might be running on a
// *different* page than the one currently open -- a real page navigation
// throws away any in-memory JS state, so that has to be re-learned from the
// server on every load and kept fresh with a short poll.

// ---- Activity strip: keeps any running job visible no matter which page is open ----

const activityStripEl = document.getElementById('activity-strip');
const activities = new Map(); // jobId -> { label, target, completed, total, status, source }

function renderActivityStrip() {
  if (activities.size === 0) {
    activityStripEl.hidden = true;
    activityStripEl.innerHTML = '';
    return;
  }
  activityStripEl.hidden = false;
  activityStripEl.innerHTML = Array.from(activities.entries()).map(([id, a]) => `
    <div class="activity-chip activity-${a.status}">
      <span class="activity-dot"></span>
      <span>${a.label}</span>
      ${a.total ? `<span class="activity-progress">${a.completed}/${a.total}</span>` : ''}
      <a class="activity-jump" href="/${a.target}">View</a>
    </div>
  `).join('');
}

function activityStart(id, label, target) {
  activities.set(id, { label, target, completed: 0, total: 0, status: 'running', source: 'local' });
  renderActivityStrip();
}

function activityProgress(id, completed, total) {
  const a = activities.get(id);
  if (!a) return;
  a.completed = completed;
  a.total = total;
  renderActivityStrip();
}

function activityFinish(id, ok) {
  const a = activities.get(id);
  if (!a) return;
  a.status = ok ? 'done' : 'error';
  renderActivityStrip();
  setTimeout(() => {
    activities.delete(id);
    renderActivityStrip();
  }, 4000);
}

// ---- Nav badges: running-job dots + the Discovery pending-candidate count ----

function updateNavBadges(badges) {
  const runningTargets = new Set(badges.jobs.map((j) => j.target));
  document.querySelectorAll('.nav-item').forEach((link) => {
    const target = link.dataset.target;
    let dot = link.querySelector('.nav-dot');
    if (runningTargets.has(target)) {
      if (!dot) {
        dot = document.createElement('span');
        dot.className = 'nav-dot';
        link.appendChild(dot);
      }
    } else if (dot) {
      dot.remove();
    }

    if (target === 'discovery') {
      let count = link.querySelector('.nav-count');
      if (badges.pending_candidates > 0) {
        if (!count) {
          count = document.createElement('span');
          count.className = 'nav-count';
          link.appendChild(count);
        }
        count.textContent = badges.pending_candidates;
      } else if (count) {
        count.remove();
      }
    }
  });
}

// A job running on another page has no open SSE connection here, so it gets
// a plain chip (no live progress) rather than a progress bar -- the "View"
// link is what you'd click to go watch it directly.
function reconcileRemoteJobs(jobs) {
  for (const [id, a] of activities.entries()) {
    if (a.source === 'remote' && !jobs.some((j) => j.job_id === id)) {
      activities.delete(id);
    }
  }
  for (const job of jobs) {
    if (!activities.has(job.job_id)) {
      activities.set(job.job_id, {
        label: job.label, target: job.target, completed: 0, total: 0, status: 'running', source: 'remote',
      });
    }
  }
  renderActivityStrip();
}

async function pollActiveJobs() {
  try {
    const resp = await fetch('/api/jobs/active');
    const badges = await resp.json();
    updateNavBadges(badges);
    reconcileRemoteJobs(badges.jobs);
  } catch {
    // transient network hiccup -- next poll tick will retry
  }
}

if (window.NAV_BADGES) {
  reconcileRemoteJobs(window.NAV_BADGES.jobs);
}
setInterval(pollActiveJobs, 5000);

// If "currently on" hasn't moved in a while, flag it -- a stuck download
// (confirmed in production: a CDN that half-closes a connection without
// requests' own timeout ever firing) otherwise looks identical to a slow
// but healthy one from the outside, with no visible sign anything is wrong.
const STALE_STATUS_MS = 90000;
setInterval(() => {
  document.querySelectorAll('[data-current-status]:not([hidden])').forEach((el) => {
    const updatedAt = Number(el.dataset.updatedAt || 0);
    el.classList.toggle('stale', updatedAt > 0 && Date.now() - updatedAt > STALE_STATUS_MS);
  });
}, 10000);

// ---- Toasts (replaces alert()) ----

const toastStack = document.getElementById('toast-stack');

function toast(message, variant = 'neutral') {
  const el = document.createElement('div');
  el.className = `toast toast-${variant}`;
  el.textContent = message;
  el.addEventListener('click', () => el.remove());
  toastStack.appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

// ---- Shared helpers ----

// Escapes text for interpolating into innerHTML.
function esc(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function pill(text, variant) {
  return `<span class="pill pill-${variant}">${text}</span>`;
}

function emptyRow(colspan, message) {
  return `<tr class="empty-row"><td colspan="${colspan}">${message}</td></tr>`;
}

function logLine(card, text) {
  const el = card.querySelector('[data-log]');
  el.textContent += (el.textContent ? '\n' : '') + text;
  el.scrollTop = el.scrollHeight;
}

function setProgress(card, completed, total) {
  const wrap = card.querySelector('.progress');
  const bar = card.querySelector('[data-progress]');
  if (!wrap || !bar) return;
  wrap.classList.add('active');
  const pct = total > 0 ? Math.round((completed / total) * 100) : 0;
  bar.style.width = pct + '%';

  const label = card.querySelector('[data-progress-label]');
  const count = card.querySelector('[data-progress-count]');
  if (label && count) {
    label.hidden = false;
    count.textContent = `${completed} / ${total}`;
  }
}

function setStat(card, name, value) {
  const el = card.querySelector(`[data-stat="${name}"]`);
  if (el) el.textContent = value;
}

// Scrape lanes: one row per site, replacing the single "currently on" line.
const LANE_STATES = {
  waiting: ['waiting', 'neutral'],
  running: ['running', 'pass'],
  pausing: ['paused after an error', 'warn'],
  done: ['done', 'neutral'],
  stopped: ['stopped', 'neutral'],
  gave_up: ['gave up', 'fail'],
};

function renderLanes(card, lanes) {
  const box = card.querySelector('[data-lanes]');
  if (!box) return;
  box.hidden = false;
  const single = card.querySelector('[data-current-status]');
  if (single) single.hidden = true;
  const now = Date.now() / 1000;
  box.dataset.lanes = JSON.stringify(lanes);
  box.querySelector('tbody').innerHTML = lanes.map((lane) => {
    const [label, variant] = LANE_STATES[lane.state] || [lane.state, 'neutral'];
    const stale = lane.state === 'running' && now - lane.updated_at > STALE_STATUS_MS / 1000;
    const shared = lane.sources.length > 1 ? `<small class="muted lane-members">${lane.sources.map(esc).join(', ')}</small>` : '';
    const on = lane.state === 'running' || lane.state === 'pausing'
      ? [lane.family, lane.source !== lane.name || lane.sources.length > 1 ? lane.source : '', lane.item ? `item ${lane.item}` : '']
        .filter(Boolean).map(esc).join(' · ')
      : '';
    const error = lane.last_error ? ` <small class="muted" title="${esc(lane.last_error)}">${lane.errors} error(s)</small>` : '';
    return `<tr class="${stale ? 'stale' : ''}"><td>${esc(lane.name)}${shared}</td>
      <td class="num">${lane.families_done.toLocaleString()} / ${lane.total.toLocaleString()}</td>
      <td>${on}${stale ? ' <span class="lane-stale">no update in 90s+</span>' : ''}</td>
      <td class="num">${lane.images.toLocaleString()}</td><td>${pill(label, variant)}${error}</td></tr>`;
  }).join('');
}

// Re-check lane rows for "no update in a while" between events.
setInterval(() => {
  document.querySelectorAll('[data-lanes]:not([hidden])').forEach((box) => {
    const card = box.closest('section, .panel') || document;
    if (box.dataset.lanes) renderLanes(card, JSON.parse(box.dataset.lanes));
  });
}, 10000);

function streamJob(card, jobId, { onDone, onSettled, activityLabel, target } = {}) {
  if (activityLabel) activityStart(jobId, activityLabel, target);

  const source = new EventSource(`/api/jobs/${jobId}/stream`);
  source.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === 'log') {
      logLine(card, data.message);
    } else if (data.type === 'progress') {
      setProgress(card, data.completed, data.total);
      if (activityLabel) activityProgress(jobId, data.completed, data.total);
      if (data.images_found !== undefined) setStat(card, 'images-found', data.images_found);
    } else if (data.type === 'status') {
      const el = card.querySelector('[data-current-status]');
      if (el) {
        el.hidden = false;
        el.querySelector('[data-current-family]').textContent = data.family;
        el.querySelector('[data-current-source]').textContent = data.source;
        // source_index/source_total only arrive when a new source starts;
        // item_index/item_total only arrive per item within it -- each
        // event updates whichever it carries and leaves the other alone,
        // since item ticks are far more frequent than source-start ticks.
        if (data.source_index !== undefined) {
          el.querySelector('[data-current-source-count]').textContent = `${data.source_index} / ${data.source_total}`;
        }
        const itemEl = el.querySelector('[data-current-item]');
        if (data.item_index !== undefined && itemEl) {
          itemEl.hidden = false;
          itemEl.querySelector('[data-current-item-count]').textContent = `${data.item_index} / ${data.item_total}`;
        }
        el.dataset.updatedAt = Date.now();
      }
    } else if (data.type === 'lanes') {
      renderLanes(card, data.lanes);
    } else if (data.type === 'done') {
      logLine(card, '-- done --');
      source.close();
      if (activityLabel) activityFinish(jobId, true);
      if (onDone) onDone(data.result);
      if (onSettled) onSettled();
    } else if (data.type === 'error') {
      logLine(card, `-- error: ${data.message} --`);
      source.close();
      if (activityLabel) activityFinish(jobId, false);
      toast(`${activityLabel || 'Job'} failed. See its log for details.`, 'fail');
      if (onSettled) onSettled();
    }
  };
  source.onerror = () => {
    source.close();
    if (activityLabel) activityFinish(jobId, false);
    if (onSettled) onSettled();
  };
  return source;
}

// The activity strip's "View" link (and just navigating to a page directly)
// only takes you to the right page -- a full page load throws away any
// in-memory job_id, so without this the job keeps running server-side but
// the page sits there looking idle. Each page calls this once on load with
// its own target and the same onDone/onSettled it'd use for a fresh submit,
// so reattaching behaves exactly like having watched it from the start,
// picking up wherever the live queue currently is.
async function reattachIfRunning(card, target, options = {}) {
  let jobs;
  try {
    const resp = await fetch('/api/jobs/active');
    ({ jobs } = await resp.json());
  } catch {
    return null;
  }
  const running = jobs.find((j) => j.target === target);
  if (!running) return null;

  logLine(card, `-- reattached to already-running job: ${running.label} --`);
  streamJob(card, running.job_id, { activityLabel: running.label, target, ...options });
  return running.job_id;
}

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  if (!resp.ok) throw new Error(`${url} -> HTTP ${resp.status}`);
  return resp.json();
}

function formToObject(form) {
  const data = {};
  const checkboxGroups = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === 'checkbox') {
      (checkboxGroups[el.name] ||= []).push(el);
    } else if (el.multiple) {
      data[el.name] = Array.from(el.selectedOptions).map((o) => o.value).join(',');
    } else if (el.value !== '') {
      data[el.name] = el.type === 'number' ? Number(el.value) : el.value;
    }
  }
  // A single checkbox (e.g. "dev mode") is a plain boolean -- several sharing
  // one name (e.g. the Scrape page's source picker) collect checked values
  // into a comma-joined string, same convention as a <select multiple>.
  for (const [name, els] of Object.entries(checkboxGroups)) {
    data[name] = els.length === 1 ? els[0].checked : els.filter((el) => el.checked).map((el) => el.value).join(',');
  }
  return data;
}
