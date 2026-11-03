// Train model page: build the training set, embed, train + score the heads.
// Each step is a background job (same streaming as the pipeline pages); the
// latest result of each comes back from /api/train/status on load.

const els = {
  datasetCard: document.getElementById('card-train-dataset'),
  embedCard: document.getElementById('card-train-embed'),
  headsCard: document.getElementById('card-train-heads'),
  csvList: document.getElementById('train-csv-list'),
  imageDirs: document.getElementById('train-image-dirs'),
  includeDb: document.getElementById('train-include-db'),
  embedModel: document.getElementById('train-embed-model'),
  headsModel: document.getElementById('train-heads-model'),
  embedStatus: document.getElementById('train-embed-status'),
  datasetResult: document.getElementById('train-dataset-result'),
  headsResult: document.getElementById('train-heads-result'),
  buildBtn: document.getElementById('btn-train-build'),
  embedBtn: document.getElementById('btn-train-embed'),
  headsBtn: document.getElementById('btn-train-heads'),
};

let status = null;

function pct(value) {
  return value === null || value === undefined ? '–' : `${Math.round(value * 100)}%`;
}

// ---- Step 1: CSV rows ----

function csvRow(row = {}) {
  const div = document.createElement('div');
  div.className = 'train-csv-row';
  div.innerHTML = `
    <label>Annotator <small>optional with one CSV</small>
      <input type="text" data-field="annotator" autocomplete="off" placeholder="e.g. alice">
    </label>
    <label class="train-csv-path">Old-tool CSV
      <input type="text" data-field="path" autocomplete="off" placeholder="e.g. C:/Users/Biometrics/Desktop/satellite_folder/annotatedInfo.csv">
    </label>
    <button type="button" class="secondary train-csv-remove" title="Remove this CSV">Remove</button>`;
  div.querySelector('[data-field="annotator"]').value = row.annotator || '';
  div.querySelector('[data-field="path"]').value = row.path || '';
  div.querySelector('.train-csv-remove').addEventListener('click', () => {
    div.remove();
    if (!els.csvList.children.length) els.csvList.appendChild(csvRow());
  });
  return div;
}

function readCsvRows() {
  return Array.from(els.csvList.querySelectorAll('.train-csv-row')).map((row) => ({
    annotator: row.querySelector('[data-field="annotator"]').value.trim(),
    path: row.querySelector('[data-field="path"]').value.trim(),
  }));
}

document.getElementById('btn-train-add-csv').addEventListener('click', () => {
  els.csvList.appendChild(csvRow());
});

// ---- Results ----

function renderDataset(report) {
  if (!report) {
    els.datasetResult.hidden = true;
    return;
  }
  const notes = [];
  if (report.annotators && Object.keys(report.annotators).length) {
    notes.push(`Examples per annotator: ${Object.entries(report.annotators).map(([a, n]) => `${a} ${n}`).join(', ')}`);
  }
  if (report.images_with_several_annotators) notes.push(`${report.images_with_several_annotators} image(s) labeled by more than one annotator`);
  for (const [annotator, reasons] of Object.entries(report.skipped_rows_by_annotator || {})) {
    notes.push(`Left out (${annotator}): ${Object.entries(reasons).map(([r, n]) => `${n} ${r}`).join(', ')}`);
  }
  if (report.left_out_attributes?.length) notes.push(`Not learnable from an image: ${report.left_out_attributes.join(', ')}`);

  const rows = Object.entries(report.attributes).map(([name, info]) => {
    const ignored = Object.entries(info.ignored).map(([r, n]) => `${n} ${r.replace(/_/g, ' ')}`).join(', ') || '–';
    const flag = info.trainable ? '' : ' <span class="pill pill-warn">not enough data</span>';
    return `<tr>
      <td>${esc(name)}${flag}</td>
      <td class="num">${info.labeled.train} / ${info.labeled.test}</td>
      <td>${esc(ignored)}</td>
      <td>${esc(info.rare_options.join(', ') || '–')}</td>
    </tr>`;
  }).join('');

  els.datasetResult.hidden = false;
  els.datasetResult.innerHTML = `
    <p class="train-summary"><strong>${report.examples.train}</strong> train / <strong>${report.examples.test}</strong> test examples
      from ${report.families.train + report.families.test} satellite families (test families are never trained on).</p>
    ${notes.map((n) => `<p class="muted train-note">${esc(n)}</p>`).join('')}
    <div class="table-wrap"><table class="train-table">
      <thead><tr><th>Attribute</th><th class="num">Usable (train / test)</th><th>Left out</th><th>Too rare yet</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
}

function renderHeads(report) {
  if (!report) {
    els.headsResult.hidden = false;
    els.headsResult.innerHTML = '<p class="muted">Not trained with this image model yet.</p>';
    return;
  }
  const rows = Object.entries(report.attributes).map(([name, a]) => {
    const gain = a.accuracy - a.baseline_accuracy;
    const verdict = gain >= 0.05 ? 'better' : gain <= 0 ? 'worse' : '';
    const p8 = a.prefill['0.8'];
    const p9 = a.prefill['0.9'];
    let detail = '';
    if (a.kind === 'single') {
      detail = `balanced ${pct(a.balanced_accuracy)} · baseline says “${esc(a.baseline_answer)}”`;
    } else {
      detail = Object.entries(a.per_option)
        .filter(([, v]) => v.auroc !== null)
        .map(([o, v]) => `${esc(o)} ${pct(v.auroc)}`)
        .join(' · ');
      detail = detail ? `ranking (50% = guessing): ${detail}` : '';
    }
    if (a.left_out_options.length) detail += `${detail ? '<br>' : ''}too rare: ${esc(a.left_out_options.join(', '))}`;
    return `<tr>
      <td>${esc(name)}<small class="train-detail">${detail}</small></td>
      <td class="num train-score ${verdict}">${pct(a.accuracy)}</td>
      <td class="num">${pct(a.baseline_accuracy)}</td>
      <td class="num">${pct(p8.coverage)} filled · ${pct(p8.accuracy)} right</td>
      <td class="num">${pct(p9.coverage)} filled · ${pct(p9.accuracy)} right</td>
    </tr>`;
  }).join('');
  const skipped = Object.entries(report.skipped_attributes || {})
    .map(([name, why]) => `<p class="muted train-note">${esc(name)}: not trained (${esc(why)})</p>`).join('');
  const ex = report.examples;

  els.headsResult.hidden = false;
  els.headsResult.innerHTML = `
    <p class="train-summary">${esc(status?.backbones?.[report.backbone] || report.backbone)} · ${ex.train} train / ${ex.test} test images
      ${ex.no_embedding ? `· <span class="pill pill-warn">${ex.no_embedding} not embedded yet</span>` : ''}</p>
    <div class="table-wrap"><table class="train-table">
      <thead><tr><th>Attribute</th><th class="num">Model</th><th class="num">Baseline</th>
        <th class="num">Pre-fill at 80%</th><th class="num">Pre-fill at 90%</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    ${skipped}`;
}

function renderEmbedStatus() {
  const n = status?.embedded?.[els.embedModel.value] ?? 0;
  els.embedStatus.textContent = `${n.toLocaleString()} image${n === 1 ? '' : 's'} embedded with this model`;
}

async function refreshStatus() {
  const resp = await fetch('/api/train/status');
  status = await resp.json();
  renderEmbedStatus();
  return status;
}

// ---- Jobs ----

async function startJob(url, body) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (resp.status === 409) throw new Error('Another training step is already running. Wait for it to finish.');
  if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
  return data.job_id;
}

function setBusy(busy) {
  els.buildBtn.disabled = busy;
  els.headsBtn.disabled = busy;
}

function watchDataset(jobId) {
  setBusy(true);
  streamJob(els.datasetCard, jobId, {
    activityLabel: 'Training set',
    target: 'train',
    onDone: (report) => { renderDataset(report); refreshStatus(); },
    onSettled: () => setBusy(false),
  });
}

function watchEmbed(jobId) {
  els.embedBtn.disabled = true;
  streamJob(els.embedCard, jobId, {
    activityLabel: 'Embed',
    target: 'embed',
    onDone: refreshStatus,
    onSettled: () => { els.embedBtn.disabled = false; },
  });
}

function watchHeads(jobId) {
  setBusy(true);
  streamJob(els.headsCard, jobId, {
    activityLabel: 'Train model',
    target: 'train',
    onDone: async (report) => { await refreshStatus(); renderHeads(report); },
    onSettled: () => setBusy(false),
  });
}

els.buildBtn.addEventListener('click', async () => {
  els.datasetCard.querySelector('[data-log]').textContent = '';
  try {
    const jobId = await startJob('/api/jobs/training-set', {
      csvs: readCsvRows(),
      image_dirs: els.imageDirs.value.split('\n').map((d) => d.trim()).filter(Boolean),
      include_database: els.includeDb.checked,
    });
    watchDataset(jobId);
  } catch (e) {
    toast(e.message, 'fail');
  }
});

els.embedBtn.addEventListener('click', async () => {
  els.embedCard.querySelector('[data-log]').textContent = '';
  try {
    watchEmbed(await startJob('/api/jobs/embed', { model: els.embedModel.value }));
  } catch (e) {
    toast(e.message.includes('already running') ? 'An embed run is already in progress.' : e.message, 'fail');
  }
});

els.headsBtn.addEventListener('click', async () => {
  els.headsCard.querySelector('[data-log]').textContent = '';
  try {
    watchHeads(await startJob('/api/jobs/train-heads', { model: els.headsModel.value }));
  } catch (e) {
    toast(e.message, 'fail');
  }
});

els.embedModel.addEventListener('change', () => {
  els.headsModel.value = els.embedModel.value;
  renderEmbedStatus();
  renderHeads(status?.heads?.[els.headsModel.value]);
});
els.headsModel.addEventListener('change', () => {
  els.embedModel.value = els.headsModel.value;
  renderEmbedStatus();
  renderHeads(status?.heads?.[els.headsModel.value]);
});

// ---- Load ----

async function init() {
  await refreshStatus();
  const { settings, backbones } = status;
  const options = Object.entries(backbones)
    .map(([name, label]) => `<option value="${esc(name)}">${esc(label)}</option>`)
    .join('');
  els.embedModel.innerHTML = options;
  els.headsModel.innerHTML = options;
  els.embedModel.value = settings.model;
  els.headsModel.value = settings.model;

  (settings.csvs.length ? settings.csvs : [{}]).forEach((row) => els.csvList.appendChild(csvRow(row)));
  els.imageDirs.value = settings.image_dirs.join('\n');
  els.includeDb.checked = settings.include_database;

  renderEmbedStatus();
  renderDataset(status.dataset);
  if (status.heads[settings.model]) renderHeads(status.heads[settings.model]);

  // Pick back up a job that was already running when the page loaded.
  try {
    const { jobs } = await (await fetch('/api/jobs/active')).json();
    for (const job of jobs) {
      if (job.target === 'embed') watchEmbed(job.job_id);
      else if (job.target === 'train' && job.label === 'Training set') watchDataset(job.job_id);
      else if (job.target === 'train') watchHeads(job.job_id);
    }
  } catch { /* no running jobs to reattach to */ }
}

init();
