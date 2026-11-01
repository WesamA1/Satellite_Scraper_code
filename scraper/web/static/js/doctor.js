async function runDoctor() {
  const resp = await fetch('/api/doctor');
  const { results } = await resp.json();
  const tbody = document.querySelector('#doctor-table tbody');
  tbody.innerHTML = results.map((r) => `
    <tr>
      <td>${r.name}</td>
      <td>${pill(r.status, r.status)}</td>
      <td>${r.detail}</td>
    </tr>
  `).join('');
}
document.getElementById('btn-run-doctor').addEventListener('click', runDoctor);

// ---- Danger zone: wipe images + database, then restart ----

const wipeInput = document.getElementById('wipe-confirm-input');
const wipeBtn = document.getElementById('btn-wipe-data');
const wipeStatus = document.getElementById('wipe-status');

wipeInput.addEventListener('input', () => {
  wipeBtn.disabled = wipeInput.value !== wipeInput.dataset.phrase;
});

// Waits for the server to actually come back up (the restart briefly closes
// the port) before reloading -- reloading too early just lands on a
// connection-refused error instead of the fresh, wiped state.
async function waitForServerThenReload() {
  for (let attempt = 0; attempt < 30; attempt++) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    try {
      const resp = await fetch('/api/jobs/active', { cache: 'no-store' });
      if (resp.ok) {
        location.reload();
        return;
      }
    } catch {
      // still down -- keep waiting
    }
  }
  wipeStatus.textContent = 'Still waiting for the server to come back. Reload this page manually once it does.';
}

wipeBtn.addEventListener('click', async () => {
  wipeBtn.disabled = true;
  wipeInput.disabled = true;
  wipeStatus.hidden = false;
  wipeStatus.textContent = 'Wiping...';
  try {
    const resp = await fetch('/api/wipe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: wipeInput.value }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      toast(`Could not wipe: ${data.detail}`, 'fail');
      wipeBtn.disabled = false;
      wipeInput.disabled = false;
      wipeStatus.hidden = true;
      return;
    }
    const cleared = Object.entries(data.table_counts).filter(([, n]) => n > 0).map(([t, n]) => `${t}: ${n}`);
    wipeStatus.textContent = `Wiped (${cleared.join(', ') || 'nothing to clear'}). Restarting...`;
    waitForServerThenReload();
  } catch {
    toast('Could not reach the server to wipe.', 'fail');
    wipeBtn.disabled = false;
    wipeInput.disabled = false;
    wipeStatus.hidden = true;
  }
});
