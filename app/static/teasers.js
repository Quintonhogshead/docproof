/* Cloud teaser status in the formatting workflow. All dynamic copy is text. */
(() => {
  'use strict';
  const card = document.getElementById('author-teasers-card');
  const toggle = document.getElementById('author-teasers-enabled');
  const note = document.getElementById('author-teasers-note');
  const jobs = document.getElementById('author-teasers-jobs');
  if (!card) return;
  let saving = false;
  async function request(path, options) {
    const response = await fetch(path, options);
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || 'Could not load author teasers.');
    return body;
  }
  async function refresh() {
    if (saving || !card.getClientRects().length) return;
    try {
      const data = await request('/api/teasers');
      toggle.checked = data.settings.enabled === true;
      note.textContent = toggle.checked ? 'Runs automatically, even while your computer is off.' : 'Automatic teaser generation is off.';
      jobs.replaceChildren();
      for (const job of data.tasks.slice(0, 12)) {
        const row = document.createElement('p');
        const title = document.createElement('strong');
        title.textContent = job.book_label;
        row.append(title, document.createElement('br'));
        if (job.document_url && /^https:\/\/docs\.google\.com\//.test(job.document_url)) {
          const link = document.createElement('a');
          link.href = job.document_url;
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
          link.textContent = 'Open five teasers';
          row.append(link);
          if (job.guide_url && /^https:\/\/(drive|docs)\.google\.com\//.test(job.guide_url)) {
            const guide = document.createElement('a');
            guide.href = job.guide_url;
            guide.target = '_blank';
            guide.rel = 'noopener noreferrer';
            guide.textContent = 'Open two-page dos and donts';
            row.append(document.createTextNode(' · '), guide);
          }
        } else {
          row.append(document.createTextNode(job.progress || 'Waiting for cloud processing'));
        }
        jobs.append(row);
      }
    } catch (error) { note.textContent = error.message; }
  }
  toggle.addEventListener('change', async () => {
    saving = true;
    toggle.disabled = true;
    try {
      await request('/api/teasers/settings', {method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled: toggle.checked})});
    } catch (error) { note.textContent = error.message; toggle.checked = !toggle.checked; }
    finally { saving = false; toggle.disabled = false; }
    await refresh();
  });
  setInterval(refresh, 10000);
  new MutationObserver(refresh).observe(document.getElementById('wf-config-prep'), {attributes: true, attributeFilter: ['hidden']});
  refresh();
})();
