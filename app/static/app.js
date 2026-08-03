'use strict';

// Language rule for everything the user reads: no "chunks", no "tokens", no
// "batch", no "API" outside the Settings key fields. Sections, reviews, cost.

const $ = (id) => document.getElementById(id);
const api = async (path, options) => {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = `Something went wrong (${res.status}).`;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
};

const state = { files: [], models: [], pollTimer: null };

// ── navigation ────────────────────────────────────────────────────────────

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => show(tab.dataset.screen));
});

function show(name) {
  document.querySelectorAll('.tab').forEach((t) => {
    t.setAttribute('aria-current', String(t.dataset.screen === name));
  });
  ['drop', 'jobs', 'settings'].forEach((s) => {
    $(`screen-${s}`).hidden = s !== name;
  });
  if (name === 'jobs') refreshJobs({ tick: true });
  if (name === 'settings') loadSettings();
}

// ── dropping files ────────────────────────────────────────────────────────

const zone = $('dropzone');
const input = $('file-input');

$('pick').addEventListener('click', (e) => { e.stopPropagation(); input.click(); });
zone.addEventListener('click', () => input.click());
zone.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
});
input.addEventListener('change', () => upload([...input.files]));

['dragenter', 'dragover'].forEach((evt) =>
  zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.add('hot'); }));
['dragleave', 'drop'].forEach((evt) =>
  zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.remove('hot'); }));
zone.addEventListener('drop', (e) => upload([...e.dataTransfer.files]));

async function upload(files) {
  if (!files.length) return;
  const body = new FormData();
  files.forEach((f) => body.append('files', f));
  $('drop-error').hidden = true;
  try {
    const { files: staged } = await api('/api/files', { method: 'POST', body });
    state.files = state.files.concat(staged);
    renderFiles();
    await loadModels();
  } catch (err) {
    fail(err.message);
  }
}

function renderFiles() {
  const list = $('file-list');
  list.innerHTML = '';
  state.files.forEach((f, i) => {
    const li = document.createElement('li');
    if (!f.ok) li.className = 'file-bad';
    const name = document.createElement('div');
    name.className = 'file-name';
    name.textContent = f.filename;
    const meta = document.createElement('div');
    meta.className = 'file-meta';
    meta.textContent = f.ok
      ? `${f.paragraphs} paragraphs, ${f.sections} section${f.sections === 1 ? '' : 's'} to review`
      : f.error;
    const drop = document.createElement('button');
    drop.textContent = 'Remove';
    drop.addEventListener('click', () => {
      state.files.splice(i, 1); renderFiles(); loadModels();
    });
    li.append(name, meta, drop);
    list.append(li);
  });
  $('staged').hidden = state.files.length === 0;
  $('start').disabled = usableIds().length === 0;
}

const usableIds = () => state.files.filter((f) => f.ok).map((f) => f.id);

// ── models and cost ───────────────────────────────────────────────────────

async function loadModels() {
  const ids = usableIds().join(',');
  const { models } = await api(`/api/models?file_ids=${encodeURIComponent(ids)}`);
  state.models = models;

  const select = $('model');
  const previous = select.value;
  select.innerHTML = '';
  models.forEach((m) => {
    const opt = document.createElement('option');
    opt.value = m.id;
    opt.textContent = m.available ? m.display : `${m.display} — add a key first`;
    opt.disabled = !m.available;
    select.append(opt);
  });
  // Prefer a usable model, but always select something: the cost estimate is
  // useful before a key exists, and a blank dropdown looks broken.
  const fallback = models.find((m) => m.available) || models[0];
  select.value = models.some((m) => m.id === previous && m.available)
    ? previous
    : (fallback ? fallback.id : '');
  renderCost();
}

$('model').addEventListener('change', renderCost);

function renderCost() {
  const m = state.models.find((x) => x.id === $('model').value);
  $('model-blurb').textContent = m ? m.blurb : '';
  const money = (v) => (typeof v === 'number'
    ? `about $${v < 0.01 ? v.toFixed(3) : v.toFixed(2)}` : '');
  $('cost-now').textContent = m ? money(m.cost_now) : '';
  $('cost-batch').textContent = m ? money(m.cost_batch) : '';

  const ready = m && m.available && usableIds().length > 0;
  $('start').disabled = !ready;

  // A disabled button with no explanation is the worst first-run experience
  // there is. Say what's missing and where to fix it.
  const hint = $('start-hint');
  if (m && !m.available) {
    hint.innerHTML = '';
    hint.append(`${m.display} needs an API key. `);
    const go = document.createElement('button');
    go.className = 'link';
    go.textContent = 'Add one in Settings';
    go.addEventListener('click', () => show('settings'));
    hint.append(go, ' — or pick a reviewer you already have a key for.');
    hint.hidden = false;
  } else {
    hint.hidden = true;
  }
}

// ── starting a review ─────────────────────────────────────────────────────

$('schedule-on').addEventListener('change', () => {
  $('schedule-at').disabled = !$('schedule-on').checked;
});
document.querySelectorAll('input[name="mode"]').forEach((r) =>
  r.addEventListener('change', () => {
    $('schedule-wrap').hidden = mode() !== 'batch';
  }));

const mode = () => document.querySelector('input[name="mode"]:checked').value;

$('start').addEventListener('click', async () => {
  const button = $('start');
  button.disabled = true;
  button.textContent = 'Starting…';
  try {
    await api('/api/jobs', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        file_ids: usableIds(),
        model: $('model').value,
        mode: mode(),
        schedule_at: (mode() === 'batch' && $('schedule-on').checked)
          ? $('schedule-at').value : null,
        min_confidence: $('confidence').value,
      }),
    });
    state.files = [];
    renderFiles();
    show('jobs');
  } catch (err) {
    fail(err.message);
  } finally {
    button.disabled = false;
    button.textContent = 'Start review';
  }
});

function fail(message) {
  const box = $('drop-error');
  box.textContent = message;
  box.hidden = false;
}

// ── jobs ──────────────────────────────────────────────────────────────────

async function refreshJobs({ tick = false } = {}) {
  try {
    const { jobs } = tick
      ? await api('/api/tick', { method: 'POST' })
      : await api('/api/jobs');
    renderJobs(jobs);
  } catch (_) { /* transient; the next poll will catch up */ }
}

function renderJobs(jobs) {
  const list = $('job-list');
  list.innerHTML = '';
  $('jobs-empty').hidden = jobs.length > 0;

  const active = jobs.filter((j) => !['done', 'failed'].includes(j.state)).length;
  const badge = $('jobs-badge');
  badge.hidden = active === 0;
  badge.textContent = String(active);

  jobs.forEach((job) => {
    const li = document.createElement('li');

    const head = document.createElement('div');
    head.className = 'job-head';
    const name = document.createElement('span');
    name.className = 'file-name';
    name.textContent = job.filename;
    const status = document.createElement('span');
    status.className = 'job-state'
      + (job.state === 'done' ? ' is-done' : job.state === 'failed' ? ' is-failed' : '');
    status.textContent = job.plain_state;
    head.append(name, status);
    li.append(head);

    if (job.state === 'running' && job.total) {
      const bar = document.createElement('div');
      bar.className = 'bar';
      const fill = document.createElement('i');
      fill.style.width = `${Math.round((job.done / job.total) * 100)}%`;
      bar.append(fill);
      li.append(bar);
    }

    if (job.ready) {
      const actions = document.createElement('div');
      actions.className = 'job-actions';
      const doc = link(`/api/jobs/${job.id}/file/docx`, 'Open reviewed document');
      const sum = link(`/api/jobs/${job.id}/file/summary`, 'View summary');
      actions.append(doc, sum);
      if (typeof job.applied === 'number') {
        const count = document.createElement('span');
        count.className = 'file-meta';
        count.textContent = `${job.applied} change${job.applied === 1 ? '' : 's'} suggested`;
        actions.append(count);
      }
      li.append(actions);
    }

    if (job.state === 'failed') {
      const why = document.createElement('div');
      why.className = 'job-error error';
      why.textContent = job.error || 'Something went wrong.';
      const retry = document.createElement('button');
      retry.textContent = 'Try again';
      retry.addEventListener('click', async () => {
        await api(`/api/jobs/${job.id}/retry`, { method: 'POST' });
        refreshJobs();
      });
      const actions = document.createElement('div');
      actions.className = 'job-actions';
      actions.append(retry);
      li.append(why, actions);
    }

    list.append(li);
  });
}

function link(href, text) {
  const button = document.createElement('button');
  button.textContent = text;
  button.addEventListener('click', () => { window.location.href = href; });
  return button;
}

// ── settings ──────────────────────────────────────────────────────────────

async function loadSettings() {
  const { settings, keys } = await api('/api/settings');
  $('output-dir').value = settings.output_dir;
  $('comments').checked = settings.comments;
  Object.entries(keys).forEach(([provider, info]) => {
    const el = $(`status-${provider}`);
    if (!el) return;
    el.textContent = info.configured
      ? (info.source === 'environment'
        ? 'Set from your environment.'
        : 'Saved in your Keychain.')
      : 'Not set up yet.';
    el.className = info.configured ? 'status ok' : 'status muted';
  });
}

$('save-settings').addEventListener('click', async () => {
  const payload = {
    output_dir: $('output-dir').value,
    comments: $('comments').checked,
  };
  const anthropic = $('key-anthropic').value;
  const openai = $('key-openai').value;
  if (anthropic) payload.anthropic_key = anthropic;
  if (openai) payload.openai_key = openai;

  await api('/api/settings', {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  });
  $('key-anthropic').value = '';
  $('key-openai').value = '';
  $('settings-saved').hidden = false;
  setTimeout(() => { $('settings-saved').hidden = true; }, 2500);
  loadSettings();
  loadModels();
});

document.querySelectorAll('[data-test]').forEach((button) => {
  button.addEventListener('click', async () => {
    const provider = button.dataset.test;
    const el = $(`status-${provider}`);
    el.textContent = 'Checking…';
    el.className = 'status muted';
    try {
      const { ok, message } = await api(`/api/settings/test/${provider}`,
        { method: 'POST' });
      el.textContent = ok ? 'Key works.' : `Not working: ${message}`;
      el.className = ok ? 'status ok' : 'status error';
    } catch (err) {
      el.textContent = err.message;
      el.className = 'status error';
    }
  });
});

// ── boot ──────────────────────────────────────────────────────────────────

loadModels().catch(() => {});
refreshJobs();
state.pollTimer = setInterval(() => {
  if (!$('screen-jobs').hidden) refreshJobs();
}, 5000);
