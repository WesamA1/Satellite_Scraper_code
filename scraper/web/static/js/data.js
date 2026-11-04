// Data page: import a data folder (or just an annotations CSV) and export annotations.

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
