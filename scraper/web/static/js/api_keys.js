// API Keys: reuses /api/doctor for status (never a value), and a dedicated
// /api/keys endpoint to actually write one -- the server rejects that write
// unless the request comes from localhost, regardless of what --host the
// server was started with.
//
// Not auto-loaded on page open: /api/doctor also pings Wikimedia/NASA for
// the reachability checks, and that should stay an explicit action, same as
// the Doctor page.

function toWriteKey(doctorCheckName) {
  const name = doctorCheckName.replace('credential:', '');
  if (!name.startsWith('source:')) return name;
  const sourceName = name.slice('source:'.length);
  const normalized = sourceName.toUpperCase().replace(/[^A-Z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  return `SOURCE_${normalized}_API_KEY`;
}

// Where to actually get a key for each promoted source that needs one --
// there's no way to derive this from the site's URL alone, so it's a
// hand-maintained lookup. Verified against each provider's current signup
// flow (checked 2026-10-31) since these pages move -- e.g. Flickr added a
// Pro-subscription requirement, Europeana moved key requests behind an
// account login.
const SOURCE_API_KEY_HELP = {
  flickr: {
    url: 'https://www.flickr.com/services/apps/create/',
    note: 'Requires an active Flickr Pro subscription. Click "Request an API Key" on the App Garden page.',
  },
  europeana: {
    url: 'https://pro.europeana.eu/page/get-api',
    note: 'Needs a free Europeana account first, then request a personal key from your account settings.',
  },
  smithsonian: {
    url: 'https://api.data.gov/signup/',
    note: 'Same api.data.gov program as NASA\'s. Instant, no approval, no form.',
  },
  dpla: {
    url: 'https://pro.dp.la/developers/api-basics',
    note: 'Self-service: DPLA emails you a key within a minute or two of requesting one.',
  },
};

function sourceApiKeyHelp(doctorCheckName) {
  const name = doctorCheckName.replace('credential:', '');
  if (!name.startsWith('source:')) return null;
  return SOURCE_API_KEY_HELP[name.slice('source:'.length)] || null;
}

function keyRowHtml(r) {
  const label = r.name.replace('credential:', '');
  const writeKey = toWriteKey(r.name);
  const help = sourceApiKeyHelp(r.name);
  const helpHtml = help
    ? `<div class="key-help"><a href="${help.url}" target="_blank" rel="noopener">Get a key &rarr;</a> <small>${help.note}</small></div>`
    : '';
  return `
    <tr data-write-key="${writeKey}">
      <td>${label}</td>
      <td>${pill(r.status, r.status)}</td>
      <td>${r.detail}${helpHtml}</td>
      <td>
        <button class="action-btn secondary" data-action="edit">Edit</button>
        <span class="key-edit-row" hidden>
          <input type="password" placeholder="New value" autocomplete="off">
          <button class="action-btn" data-action="save">Save</button>
          <button class="action-btn secondary" data-action="cancel">Cancel</button>
        </span>
      </td>
    </tr>
  `;
}

async function refreshApiKeys() {
  const resp = await fetch('/api/doctor');
  const { results } = await resp.json();
  const keyResults = results.filter((r) => r.name.startsWith('credential:'));
  const tbody = document.querySelector('#api-keys-table tbody');
  tbody.innerHTML = keyResults.length
    ? keyResults.map(keyRowHtml).join('')
    : emptyRow(4, 'No credential keys recognized.');
}
document.getElementById('btn-refresh-keys').addEventListener('click', refreshApiKeys);

document.querySelector('#api-keys-table tbody').addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-action]');
  if (!btn) return;
  const row = btn.closest('tr');
  const editBtn = row.querySelector('[data-action="edit"]');
  const editRow = row.querySelector('.key-edit-row');
  const input = editRow.querySelector('input');

  if (btn.dataset.action === 'edit') {
    editBtn.hidden = true;
    editRow.hidden = false;
    input.value = '';
    input.focus();
  } else if (btn.dataset.action === 'cancel') {
    editRow.hidden = true;
    editBtn.hidden = false;
  } else if (btn.dataset.action === 'save') {
    const value = input.value.trim();
    if (!value) {
      toast('Enter a value before saving.', 'fail');
      return;
    }
    const resp = await fetch('/api/keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: row.dataset.writeKey, value }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      toast(`Could not save: ${data.detail}`, 'fail');
      return;
    }
    toast(`${row.dataset.writeKey} saved.`, 'pass');
    refreshApiKeys();
  }
});
