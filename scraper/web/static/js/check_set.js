// Check set: label a fixed sample of images (satellite count, recognizable)
// so screening models can be scored against a person's answers.

const LABELER_KEY = 'groundstation.annotator';
const cs = { items: [], index: 0, pendingCount: null };
const csEls = {
  create: document.getElementById('check-set-create'),
  work: document.getElementById('check-set-work'),
  image: document.getElementById('check-set-image'),
  name: document.getElementById('check-set-name'),
  progress: document.getElementById('check-set-progress'),
  labeler: document.getElementById('check-set-labeler'),
  recognizable: document.getElementById('check-set-recognizable'),
  done: document.getElementById('check-set-done'),
};

try { csEls.labeler.value = localStorage.getItem(LABELER_KEY) || ''; } catch { /* starts empty */ }

function labeledCount() {
  return cs.items.filter((item) => item.satellites).length;
}

function answerText(item) {
  if (!item.satellites) return 'not labeled yet';
  if (item.satellites === '1') return `one satellite, ${item.recognizable === 'yes' ? 'recognizable' : 'not recognizable'}`;
  return item.satellites === '0' ? 'no satellite' : 'several satellites';
}

function render() {
  const item = cs.items[cs.index];
  cs.pendingCount = null;
  csEls.recognizable.hidden = true;
  csEls.work.querySelectorAll('[data-count], [data-recognizable]').forEach((b) => b.classList.remove('picked'));
  const labeled = labeledCount();
  csEls.done.hidden = labeled < cs.items.length;
  csEls.done.textContent = `All ${cs.items.length} labeled. The set is ready for comparing models.`;
  if (!item) return;
  csEls.image.src = `/api/check-set/image?sha256=${encodeURIComponent(item.sha256)}`;
  csEls.name.textContent = item.image.split('/').pop();
  csEls.progress.textContent = `Image ${cs.index + 1} of ${cs.items.length} · ${labeled} labeled · this one: ${answerText(item)}`;
  if (item.satellites) csEls.work.querySelector(`[data-count="${item.satellites}"]`)?.classList.add('picked');
}

function goTo(index) {
  cs.index = Math.max(0, Math.min(cs.items.length - 1, index));
  render();
}

function nextUnlabeled(from) {
  const after = cs.items.findIndex((item, i) => i > from && !item.satellites);
  return after === -1 ? Math.min(from + 1, cs.items.length - 1) : after;
}

async function save(satellites, recognizable = '') {
  const item = cs.items[cs.index];
  const labeledBy = csEls.labeler.value.trim();
  try { localStorage.setItem(LABELER_KEY, labeledBy); } catch { /* not remembered */ }
  const resp = await fetch('/api/check-set/label', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sha256: item.sha256, satellites, recognizable, labeled_by: labeledBy }),
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    toast(body.detail || `Couldn't save (HTTP ${resp.status}).`, 'fail');
    return;
  }
  Object.assign(item, { satellites, recognizable: satellites === '1' ? recognizable : '' });
  goTo(nextUnlabeled(cs.index));
}

function pickCount(count) {
  if (!cs.items[cs.index]) return;
  if (count === '1') {
    cs.pendingCount = '1';  // one satellite: ask the second question before saving
    csEls.recognizable.hidden = false;
    csEls.work.querySelectorAll('[data-count]').forEach((b) => b.classList.toggle('picked', b.dataset.count === '1'));
    return;
  }
  save(count);
}

function pickRecognizable(answer) {
  if (cs.pendingCount === '1') save('1', answer);
}

csEls.work.querySelectorAll('[data-count]').forEach((b) => b.addEventListener('click', () => pickCount(b.dataset.count)));
csEls.work.querySelectorAll('[data-recognizable]').forEach((b) => {
  b.addEventListener('click', () => pickRecognizable(b.dataset.recognizable));
});
document.getElementById('btn-check-set-prev').addEventListener('click', () => goTo(cs.index - 1));
document.getElementById('btn-check-set-next').addEventListener('click', () => goTo(cs.index + 1));

document.addEventListener('keydown', (e) => {
  if (csEls.work.hidden || ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return;
  if (e.key === '0') pickCount('0');
  else if (e.key === '1') pickCount('1');
  else if (e.key === '2') pickCount('2+');
  else if (e.key === 'y' || e.key === 'Y') pickRecognizable('yes');
  else if (e.key === 'n' || e.key === 'N') pickRecognizable('no');
  else if (e.key === 'ArrowLeft') goTo(cs.index - 1);
  else if (e.key === 'ArrowRight') goTo(cs.index + 1);
});

document.getElementById('btn-check-set-create').addEventListener('click', async () => {
  const size = Number(document.getElementById('check-set-size').value) || 200;
  const resp = await fetch('/api/check-set/create', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ size }),
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    toast(body.detail || `Couldn't make the check set (HTTP ${resp.status}).`, 'fail');
    return;
  }
  load(body);
});

function load(data) {
  cs.items = data.items || [];
  csEls.create.hidden = data.exists;
  csEls.work.hidden = !data.exists;
  if (data.exists) goTo(Math.max(0, cs.items.findIndex((item) => !item.satellites)));
}

(async () => load(await (await fetch('/api/check-set')).json()))();
