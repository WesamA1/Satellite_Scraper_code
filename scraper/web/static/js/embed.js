const embedCard = document.getElementById('card-embed');
document.getElementById('btn-embed').addEventListener('click', async () => {
  embedCard.querySelector('[data-log]').textContent = '';
  const { job_id } = await postJSON('/api/jobs/embed', {});
  streamJob(embedCard, job_id, { activityLabel: 'Embed', target: 'embed' });
});

reattachIfRunning(embedCard, 'embed', {});
