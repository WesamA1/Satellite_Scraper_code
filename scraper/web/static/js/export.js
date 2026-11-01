const exportForm = document.getElementById('form-export');
const exportCard = document.getElementById('card-export');
exportForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  exportCard.querySelector('[data-log]').textContent = '';
  const body = formToObject(exportForm);
  const { job_id } = await postJSON('/api/jobs/export', body);
  streamJob(exportCard, job_id, { activityLabel: 'Export', target: 'export' });
});

reattachIfRunning(exportCard, 'export', {});
