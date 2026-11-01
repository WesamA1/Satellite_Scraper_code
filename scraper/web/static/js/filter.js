const filterForm = document.getElementById('form-filter');
const filterCard = document.getElementById('card-filter');
const filterSubmitBtn = filterForm.querySelector('button[type="submit"]');

function onFilterSettled() {
  filterSubmitBtn.disabled = false;
}

filterForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  filterSubmitBtn.disabled = true;
  filterCard.querySelector('[data-log]').textContent = '';
  try {
    const body = formToObject(filterForm);
    const { job_id } = await postJSON('/api/jobs/filter', body);
    streamJob(filterCard, job_id, { activityLabel: 'Filter', target: 'filter', onSettled: onFilterSettled });
  } catch (err) {
    onFilterSettled();
    toast(err.message.includes('409') ? 'A filter run is already in progress. Wait for it to finish.' : 'Could not start filter.', 'fail');
  }
});

reattachIfRunning(filterCard, 'filter', { onSettled: onFilterSettled }).then((jobId) => {
  if (jobId) filterSubmitBtn.disabled = true;
});
