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
