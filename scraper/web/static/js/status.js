let statusRows = [];
const STATUS_CAP = 200;

function renderStatusTable() {
  const query = document.getElementById('status-search').value.trim().toLowerCase();
  const tbody = document.querySelector('#status-table tbody');
  const rows = query ? statusRows.filter((r) => r.family.toLowerCase().includes(query)) : statusRows;

  if (rows.length === 0) {
    tbody.innerHTML = emptyRow(4, statusRows.length === 0 ? 'Click Refresh to load.' : 'No families match that search.');
    return;
  }

  const shown = rows.slice(0, STATUS_CAP);
  let html = shown.map((row) => `
    <tr>
      <td>${row.family}</td>
      <td>${pill(row.state, row.state)}</td>
      <td>${row.image_count}</td>
      <td>${row.untried_sources.join(', ') || '--'}</td>
    </tr>
  `).join('');
  if (rows.length > STATUS_CAP) {
    html += emptyRow(4, `Showing ${STATUS_CAP} of ${rows.length}. Narrow your search to see more.`);
  }
  tbody.innerHTML = html;
}

async function refreshStatus() {
  const resp = await fetch('/api/status');
  const { rows } = await resp.json();
  statusRows = rows;
  renderStatusTable();
}
document.getElementById('btn-refresh-status').addEventListener('click', refreshStatus);
document.getElementById('status-search').addEventListener('input', renderStatusTable);

// ---- Overview (top of the page) ----

const PIPELINE_STAGES = [
  ['waiting_for_filter', 'Waiting for the filter', '/filter'],
  ['waiting_for_review', 'Waiting for review', '/review'],
  ['kept_not_annotated', 'Kept, not annotated', '/annotate'],
  ['annotated', 'Annotated', '/annotate'],
  ['training_examples', 'Training examples', '/train'],
];
const RUN_RESULT = {
  finished: ['finished', 'pass'],
  stopped: ['stopped', 'warn'],
  failed: ['failed', 'fail'],
  didnt_finish: ["didn't finish", 'fail'],
  running: ['running', 'neutral'],
};

function fmtDate(iso) {
  if (!iso) return '–';
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function n(value) {
  return Number(value || 0).toLocaleString();
}

function renderOverview(o) {
  const alerts = document.getElementById('overview-alerts');
  alerts.innerHTML = o.alerts.length
    ? o.alerts.map((a) => `<li class="overview-alert alert-${a.level}">${esc(a.text)}${
      a.link ? ` <a class="inline-link" href="${a.link}">Go &rarr;</a>` : ''}</li>`).join('')
    : '<li class="overview-alert alert-ok">Nothing needs attention.</li>';

  document.getElementById('overview-pipeline').innerHTML = PIPELINE_STAGES.map(([key, label, href]) => `
    <a class="overview-stage" href="${href}"><span class="overview-stage-n">${n(o.pipeline[key])}</span>
      <span class="overview-stage-label">${label}</span></a>`).join('<span class="overview-arrow" aria-hidden="true">&rarr;</span>');
  const reasons = {
    filter: 'by the filter', duplicate: 'as duplicates', not_an_image: 'not images', several_satellites: 'several satellites',
    not_recognizable: 'not recognizable', not_a_satellite: 'not a satellite', other: 'other', unrecorded: 'before reasons were kept',
    off_limits_source: 'from an off-limits site',
    rejected_before: 'rejected before', already_in_master_set: 'already in the master set',
  };
  const aside = Object.entries(o.pipeline.set_aside).map(([r, c]) => `${n(c)} ${reasons[r] || r}`);
  document.getElementById('overview-set-aside').textContent = aside.length ? `Set aside: ${aside.join(' · ')}` : '';

  const c = o.coverage;
  document.getElementById('overview-coverage-bar').style.width = c.families ? `${(100 * c.with_images) / c.families}%` : '0%';
  document.getElementById('overview-coverage').innerHTML = `<strong>${n(c.with_images)}</strong> of ${n(c.families)}
    satellite families have images · ${n(c.searched_nothing_kept)} searched with nothing kept · ${n(c.never_searched)} never searched`;

  document.querySelector('#overview-runs tbody').innerHTML = o.runs.length ? o.runs.map((r) => {
    const empty = Object.values(r.summary).reduce((t, s) => t + (s.empty_searches || 0), 0);
    const [label, variant] = RUN_RESULT[r.status] || [r.status, 'neutral'];
    const names = r.families.slice(0, 3).join(', ') + (r.families.length > 3 ? ` +${r.families.length - 3} more` : '');
    return `<tr><td>${fmtDate(r.started_at)}</td><td>${esc(names)} <small class="muted">(${r.families_done}/${r.families.length} done)</small></td>
      <td class="num">${n(r.images_added)}</td><td class="num">${n(empty)}</td>
      <td>${pill(label, variant)}${r.error ? ` <small class="muted">${esc(r.error)}</small>` : ''}</td></tr>`;
  }).join('') : emptyRow(5, 'No scrapes recorded yet. Runs show up here from now on.');

  document.querySelector('#overview-sources tbody').innerHTML = o.sources.map((s) => {
    let state = s.enabled ? pill('on', 'pass') : pill('off', 'neutral');
    if (s.off_limits) state = s.enabled ? pill('on: off-limits!', 'fail') : pill('off (off-limits)', 'neutral');
    else if (s.enabled && s.needs_key) state = pill('needs API key', 'warn');
    const share = s.empty_share === null ? '' : ` <small class="muted">(${Math.round(s.empty_share * 100)}%)</small>`;
    return `<tr><td>${esc(s.source)}</td><td>${state}</td><td class="num">${n(s.active)} / ${n(s.images)}</td>
      <td>${fmtDate(s.last_download)}</td><td class="num">${n(s.empty_searches)}${share}</td><td class="num">${s.not_an_image ? n(s.not_an_image) : '–'}</td></tr>`;
  }).join('');
}

async function loadOverview() {
  try {
    renderOverview(await (await fetch('/api/overview')).json());
  } catch {
    document.getElementById('overview-alerts').innerHTML = '<li class="overview-alert alert-warn">Couldn\'t load the overview.</li>';
  }
}

loadOverview();
