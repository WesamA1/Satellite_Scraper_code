const scrapeForm = document.getElementById('form-scrape');
const scrapeCard = document.getElementById('card-scrape');
let activeScrapeJobId = null;

const scrapeSubmitBtn = scrapeForm.querySelector('button[type="submit"]');
const scratchBtn = document.getElementById('btn-scrape-from-scratch');
const retryFailedBtn = document.getElementById('btn-retry-failed');

function onScrapeSettled() {
  scrapeSubmitBtn.disabled = false;
  scratchBtn.disabled = false;
}

async function runScrape(fromScratch) {
  scrapeSubmitBtn.disabled = true;
  scratchBtn.disabled = true;
  scrapeCard.querySelector('[data-log]').textContent = '';
  try {
    const body = formToObject(scrapeForm);
    // formToObject omits empty text fields -- but an explicitly cleared domain_hint
    // (fully generic prompt) is meaningfully different from "not provided" (defaults
    // to "satellite" server-side), so always send it, empty or not.
    body.domain_hint = scrapeForm.elements.domain_hint.value;
    body.from_scratch = fromScratch;
    const { job_id, families } = await postJSON('/api/jobs/scrape', body);
    activeScrapeJobId = job_id;
    logLine(scrapeCard, `Started job ${job_id} for ${families.length} family(ies).`);
    streamJob(scrapeCard, job_id, {
      activityLabel: fromScratch ? 'Scrape (from scratch)' : 'Scrape',
      target: 'scrape',
      onSettled: onScrapeSettled,
    });
  } catch (err) {
    onScrapeSettled();
    toast(err.message.includes('409') ? 'A scrape is already running. Wait for it to finish.' : 'Could not start scrape.', 'fail');
  }
}

scrapeForm.addEventListener('submit', (e) => {
  e.preventDefault();
  runScrape(false);
});

scratchBtn.addEventListener('click', () => runScrape(true));

retryFailedBtn.addEventListener('click', async () => {
  retryFailedBtn.disabled = true;
  try {
    const body = { family: scrapeForm.elements.family.value };
    const { cleared, families } = await postJSON('/api/retry-failed-searches', body);
    toast(
      cleared > 0
        ? `Cleared ${cleared} failed search(es) across ${families} family(ies). They'll be searched again next run.`
        : `Nothing to clear. No failed searches found for ${families} family(ies).`,
      'pass',
    );
  } catch {
    toast('Could not clear failed searches.', 'fail');
  } finally {
    retryFailedBtn.disabled = false;
  }
});

scrapeForm.querySelector('[data-cancel]').addEventListener('click', async () => {
  if (!activeScrapeJobId) return;
  await fetch(`/api/jobs/${activeScrapeJobId}/cancel`, { method: 'POST' });
  toast('Cancel requested. The current family will finish, then it stops.', 'neutral');
});

reattachIfRunning(scrapeCard, 'scrape', { onSettled: onScrapeSettled }).then((jobId) => {
  if (!jobId) return;
  activeScrapeJobId = jobId; // so Stop works after landing here via the activity strip's View link
  scrapeSubmitBtn.disabled = true;
  scratchBtn.disabled = true;
});
