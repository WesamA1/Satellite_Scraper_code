const embedCard = document.getElementById('card-embed');
const embedButton = document.getElementById('btn-embed');
const embedModel = document.getElementById('embed-model');

function onEmbedSettled() {
  embedButton.disabled = false;
}

embedButton.addEventListener('click', async () => {
  embedButton.disabled = true;
  embedCard.querySelector('[data-log]').textContent = '';
  try {
    const { job_id } = await postJSON('/api/jobs/embed', { model: embedModel.value });
    streamJob(embedCard, job_id, { activityLabel: 'Embed', target: 'embed', onSettled: onEmbedSettled });
  } catch (err) {
    onEmbedSettled();
    toast(err.message.includes('409') ? 'An embed run is already in progress. Wait for it to finish.' : 'Could not start embed.', 'fail');
  }
});

reattachIfRunning(embedCard, 'embed', { onSettled: onEmbedSettled }).then((jobId) => {
  if (jobId) embedButton.disabled = true;
});
