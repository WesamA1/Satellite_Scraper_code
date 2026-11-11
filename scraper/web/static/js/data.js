// Data page: the master set (ledger, sorting check, fixes), adding images from
// an outside source, importing a data folder (or just an annotations CSV),
// and exporting annotations.

// ---- Master set ----

const masterEls = {
  summary: document.getElementById('master-summary'),
  issues: document.querySelector('#master-issues tbody'),
  details: document.getElementById('master-details'),
  refresh: document.getElementById('btn-master-refresh'),
};

const FIX_LABELS = {
  wrong_folder: 'Sort into family folders',
  outside_home: 'Move into data/images/',
  stored_twice: 'Set extra copies aside',
  untracked: 'Add them (import)',
  not_in_ledger: 'Record them',
};

function itemLine(kind, item) {
  if (kind === 'two_families') {
    const buttons = item.families.map((f) =>
      `<button type="button" class="secondary" data-family="${esc(f)}" data-sha="${esc(item.sha256)}">${esc(f)}</button>`).join(' ');
    return `<li>${item.paths.map(esc).join(' &middot; ')}<br><small class="muted">Which family?</small> ${buttons}</li>`;
  }
  return `<li>${esc(item.path || item.sha256)}${item.family ? ` <small class="muted">(${esc(item.family)})</small>` : ''}</li>`;
}

async function loadMaster() {
  masterEls.summary.textContent = 'Checking\u2026';
  let data;
  try {
    data = await (await fetch('/api/master')).json();
  } catch {
    masterEls.summary.textContent = "Couldn't check the master set.";
    return;
  }
  const l = data.ledger;
  const n = (v) => (v || 0).toLocaleString();
  masterEls.summary.innerHTML = `Ledger: <strong>${n(l.kept)}</strong> kept &middot; ${n(l.waiting)} waiting for review &middot; `
    + `${n(l.rejected)} rejected &middot; ${n(l.license_unrecorded)} kept with no license on record`;
  const rows = Object.entries(data.check).filter(([, issue]) => issue.count);
  masterEls.issues.innerHTML = rows.length ? rows.map(([kind, issue]) => `<tr>
      <td>${esc(issue.label)}</td><td class="num">${n(issue.count)}</td>
      <td>${issue.fixable ? `<button type="button" class="secondary" data-fix="${kind}">${FIX_LABELS[kind] || 'Fix'}</button>` : ''}</td></tr>`).join('')
    : emptyRow(3, 'Nothing to fix: one copy of each image, each in its family folder.');
  masterEls.details.innerHTML = rows.map(([kind, issue]) => `<details class="advanced">
      <summary>${esc(issue.label)} (${n(issue.count)}${issue.count > issue.items.length ? `, first ${issue.items.length} shown` : ''})</summary>
      <ul class="master-list">${issue.items.map((item) => itemLine(kind, item)).join('')}</ul></details>`).join('');
}

masterEls.refresh.addEventListener('click', loadMaster);
document.getElementById('btn-ledger-csv').addEventListener('click', () => { window.location.href = '/api/master/ledger.csv'; });

document.getElementById('card-master').addEventListener('click', async (e) => {
  const fixBtn = e.target.closest('[data-fix]');
  const famBtn = e.target.closest('[data-family]');
  if (fixBtn) {
    const kind = fixBtn.dataset.fix;
    if (!window.confirm(`${FIX_LABELS[kind] || 'Fix'}? Files are moved or set aside; nothing is deleted.`)) return;
    fixBtn.disabled = true;
    try {
      const { result } = await postJSON('/api/master/fix', { kind });
      toast(`Done: ${JSON.stringify(result)}`, 'pass');
    } catch (err) {
      toast(err.message.includes('409') ? 'Wait for the duplicate scan or import to finish.' : `Couldn't fix: ${err.message}`, 'fail');
    }
    loadMaster();
  } else if (famBtn) {
    famBtn.disabled = true;
    try {
      await postJSON('/api/master/family', { sha256: famBtn.dataset.sha, family: famBtn.dataset.family });
      toast(`Filed under ${famBtn.dataset.family}; the other copy was set aside.`, 'pass');
    } catch (err) {
      toast(`Couldn't change the family: ${err.message}`, 'fail');
    }
    loadMaster();
  }
});

loadMaster();

// ---- Add images from an outside source ----

const addCard = document.getElementById('card-add-images');
const addForm = document.getElementById('form-add-images');
const addButton = addForm.querySelector('button[type="submit"]');
addForm.elements.dry_run.addEventListener('change', () => {
  addButton.textContent = addForm.elements.dry_run.checked ? 'Check folder' : 'Add images';
});
addButton.textContent = 'Check folder';

addForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  addButton.disabled = true;
  addCard.querySelector('[data-log]').textContent = '';
  try {
    const { job_id } = await postJSON('/api/jobs/add-images', formToObject(addForm));
    streamJob(addCard, job_id, {
      activityLabel: 'Add images',
      target: 'review',
      onSettled: () => { addButton.disabled = false; loadMaster(); },
    });
  } catch (err) {
    addButton.disabled = false;
    toast(err.message.includes('409') ? 'A duplicate scan or import is already running.' : `Couldn't start: ${err.message}`, 'fail');
  }
});

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
      onSettled: onImportDataSettled,
    });
  } catch (err) {
    onImportDataSettled();
    toast(err.message.includes('409') ? 'A duplicate scan or import is already running.' : 'Could not start the import.', 'fail');
  }
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
    }
  } finally {
    importButton.disabled = false;
  }
});

// ---- Fix an old CSV's shifted rows ----

const fixForm = document.getElementById('form-csv-fix');
const fixButton = fixForm.querySelector('button[type="submit"]');
const fixDryRun = fixForm.elements.dry_run;
fixDryRun.addEventListener('change', () => {
  fixButton.textContent = fixDryRun.checked ? 'Check CSV' : 'Write fixed copy';
});

function renderFixResult(r) {
  const rows = [
    [r.dry_run ? 'Would be fixed' : 'Fixed', r.fixed],
    ['Look shifted, but no known fix explains them (left as they were)', r.left],
    ['Needed nothing', r.unchanged],
  ];
  const notes = [];
  const fixes = Object.entries(r.by_fix);
  if (fixes.length) notes.push(`Fixes used: ${fixes.map(([name, n]) => `${name} (${n})`).join(', ')}`);
  if (!r.fixes_tried.length) {
    notes.push('No known fix is for a CSV with these columns, so nothing could be tried.');
  }
  if (r.out_path) {
    notes.push(`Fixed copy: ${r.out_path}. Nothing uses it until you import it or put it in place of the original.`);
  }
  if (r.report_path) notes.push(`Report: ${r.report_path}`);
  const left = r.left_rows.map((row) => `
      <tr><td>${esc(row.row)}</td><td>${esc(row.name)}</td><td>${esc(row.detail)}</td></tr>`).join('');

  const el = document.getElementById('csv-fix-result');
  el.hidden = false;
  el.innerHTML = `
    <div class="table-wrap"><table><tbody>
      <tr><th colspan="2">${esc(r.rows)} rows in the CSV${r.dry_run ? ' · dry run, nothing written' : ''}</th></tr>
      ${rows.map(([label, n]) => `<tr><td>${esc(label)}</td><td>${esc(n)}</td></tr>`).join('')}
    </tbody></table></div>
    ${notes.map((n) => `<p class="muted import-note">${esc(n)}</p>`).join('')}
    ${left ? `<div class="table-wrap"><table>
      <thead><tr><th>Row</th><th>Image</th><th>Left as it was because</th></tr></thead>
      <tbody>${left}</tbody></table></div>` : ''}`;
}

fixForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = formToObject(fixForm);
  if (!body.csv_path) {
    toast('Enter the path to the old CSV first.', 'fail');
    return;
  }
  fixButton.disabled = true;
  try {
    const resp = await fetch('/api/annotate/fix-csv', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const result = await resp.json();
    if (!resp.ok) {
      toast(result.detail || `Could not fix the CSV (HTTP ${resp.status}).`, 'fail');
      return;
    }
    renderFixResult(result);
    if (!result.dry_run) toast(`Wrote a fixed copy with ${result.fixed} rows repaired.`, 'pass');
  } catch (err) {
    toast(`Could not fix the CSV: ${err.message}`, 'fail');
  } finally {
    fixButton.disabled = false;
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
