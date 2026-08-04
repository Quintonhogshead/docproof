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

// selected: file id → Set of section ids the user kept. A file starts with
// every section in it, so "do nothing" means "review the whole document".
const state = { files: [], models: [], pollTimer: null, selected: new Map(),
                outputGuess: 600 };

// ── navigation ────────────────────────────────────────────────────────────

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => show(tab.dataset.screen));
});

function show(name) {
  document.querySelectorAll('.tab').forEach((t) => {
    t.setAttribute('aria-current', String(t.dataset.screen === name));
  });
  ['drop', 'jobs', 'report', 'prompts', 'settings'].forEach((s) => {
    $(`screen-${s}`).hidden = s !== name;
  });
  if (name === 'jobs') refreshJobs({ tick: true });
  if (name === 'settings') loadSettings();
  if (name === 'prompts') loadPrompts();
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
    meta.textContent = f.ok ? fileSummary(f) : f.error;
    const drop = document.createElement('button');
    drop.textContent = 'Remove';
    drop.addEventListener('click', () => {
      state.selected.delete(f.id);
      state.files.splice(i, 1); renderFiles(); loadModels();
    });
    li.append(name, meta, drop);
    if (f.ok && f.chunks && f.chunks.length > 1) li.append(sectionPicker(f));
    list.append(li);
  });
  $('staged').hidden = state.files.length === 0;
  $('start').disabled = usableIds().length === 0;
}

function fileSummary(f) {
  const kept = keptFor(f).size;
  const all = f.chunks ? f.chunks.length : f.sections;
  const sections = kept === all
    ? `${all} section${all === 1 ? '' : 's'}`
    : `${kept} of ${all} sections`;
  return `${f.paragraphs} paragraphs, ${sections} to review`;
}

// ── picking sections ──────────────────────────────────────────────────────

function keptFor(f) {
  if (!state.selected.has(f.id)) {
    state.selected.set(f.id, new Set((f.chunks || []).map((c) => c.chunk_id)));
  }
  return state.selected.get(f.id);
}

function sectionPicker(f) {
  const kept = keptFor(f);
  const box = document.createElement('details');
  box.className = 'sections';

  const summary = document.createElement('summary');
  summary.textContent = 'Choose which parts to review';
  box.append(summary);

  const tools = document.createElement('div');
  tools.className = 'job-actions';
  ['All', 'None'].forEach((label) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.addEventListener('click', () => {
      kept.clear();
      if (label === 'All') f.chunks.forEach((c) => kept.add(c.chunk_id));
      renderFiles(); renderCost();
      // Keep it open: the user is mid-decision.
      [...document.querySelectorAll('details.sections')].forEach(
        (d) => { d.open = true; });
    });
    tools.append(b);
  });
  box.append(tools);

  const ul = document.createElement('ul');
  ul.className = 'section-list';
  f.chunks.forEach((c) => {
    const item = document.createElement('li');
    const label = document.createElement('label');
    label.className = 'checkbox';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = kept.has(c.chunk_id);
    cb.addEventListener('change', () => {
      if (cb.checked) kept.add(c.chunk_id); else kept.delete(c.chunk_id);
      $('file-list').querySelectorAll('.file-meta')[
        state.files.indexOf(f)].textContent = fileSummary(f);
      renderCost();
    });
    const text = document.createElement('span');
    const quote = document.createElement('em');
    quote.textContent = c.preview || '(no text)';
    const meta = document.createElement('small');
    meta.className = 'muted';
    meta.textContent = ` ${c.paragraphs} paragraph${c.paragraphs === 1 ? '' : 's'}`;
    text.append(quote, meta);
    label.append(cb, text);
    item.append(label);
    ul.append(item);
  });
  box.append(ul);
  return box;
}

const usableFiles = () => state.files.filter((f) => f.ok);
const usableIds = () => usableFiles().map((f) => f.id);

// Files with nothing ticked are simply left out of the run.
const filesToRun = () => usableFiles().filter((f) => keptFor(f).size > 0);

function selectionPayload() {
  const out = {};
  filesToRun().forEach((f) => {
    const kept = keptFor(f);
    const all = (f.chunks || []).length;
    out[f.id] = kept.size === all ? null : [...kept];
  });
  return out;
}

// ── models and cost ───────────────────────────────────────────────────────

async function loadModels() {
  const ids = usableIds().join(',');
  const body = await api(`/api/models?file_ids=${encodeURIComponent(ids)}`);
  const models = body.models;
  state.models = models;
  state.outputGuess = body.output_token_guess || state.outputGuess;

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

// What the picked sections would cost on this model. The server prices the
// whole document; once sections are deselected only the client knows what is
// left, so it re-does the same arithmetic with the rates the server sent.
function priceSelection(m) {
  let tokens = 0;
  let requests = 0;
  filesToRun().forEach((f) => {
    const kept = keptFor(f);
    const passes = f.passes || 1;
    (f.chunks || []).forEach((c) => {
      if (!kept.has(c.chunk_id)) return;
      tokens += c.est_tokens * passes;
      requests += passes;
    });
  });
  if (!requests) return { now: null, batch: null };
  const full = (tokens * m.input_per_mtok
    + requests * state.outputGuess * m.output_per_mtok) / 1e6;
  return { now: full, batch: full * m.batch_discount };
}

function renderCost() {
  const m = state.models.find((x) => x.id === $('model').value);
  $('model-blurb').textContent = m ? m.blurb : '';
  const money = (v) => (typeof v === 'number'
    ? `about $${v < 0.01 ? v.toFixed(3) : v.toFixed(2)}` : '');
  const price = m ? priceSelection(m) : { now: null, batch: null };
  $('cost-now').textContent = money(price.now);
  $('cost-batch').textContent = money(price.batch);

  const ready = m && m.available && filesToRun().length > 0;
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
        file_ids: filesToRun().map((f) => f.id),
        model: $('model').value,
        mode: mode(),
        schedule_at: (mode() === 'batch' && $('schedule-on').checked)
          ? $('schedule-at').value : null,
        min_confidence: $('confidence').value,
        selections: selectionPayload(),
      }),
    });
    state.files = [];
    state.selected.clear();
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
      const read = document.createElement('button');
      read.textContent = 'See what changed';
      read.addEventListener('click', () => openReport(job));
      actions.append(doc, read);
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

// ── the report ────────────────────────────────────────────────────────────

$('report-back').addEventListener('click', () => show('jobs'));

async function openReport(job) {
  show('report');
  $('report-title').textContent = job.filename;
  $('report-headline').textContent = 'Reading the review…';
  $('report-groups').innerHTML = '';
  $('report-aside').innerHTML = '';
  try {
    renderReport(await api(`/api/jobs/${job.id}/report`));
  } catch (err) {
    $('report-headline').textContent = err.message;
  }
}

// Before/after with the altered words marked. A correction is usually a
// character or two inside a long sentence; printing the sentence twice
// without marking it makes the reader hunt.
function diffLine(segments, kind) {
  const p = document.createElement('p');
  p.className = `diff diff-${kind}`;
  segments.forEach((seg, i) => {
    const el = document.createElement(seg.changed ? 'mark' : 'span');
    el.textContent = (i ? ' ' : '') + seg.text;
    p.append(el);
  });
  return p;
}

function findingCard(f, { showStatus = false } = {}) {
  const card = document.createElement('div');
  card.className = 'finding';
  card.append(diffLine(f.before, 'before'), diffLine(f.after, 'after'));

  const foot = document.createElement('p');
  foot.className = 'finding-foot muted';
  const bits = [];
  if (f.explanation) bits.push(f.explanation);
  bits.push(f.confidence_word);
  if (showStatus && f.status_word) bits.push(f.status_word);
  foot.textContent = bits.join(' · ');
  card.append(foot);
  return card;
}

function renderReport(r) {
  const h = r.headline;
  const parts = [];
  if (h.applied) {
    parts.push(`${h.applied} correction${h.applied === 1 ? '' : 's'} `
      + `in ${h.paragraphs} paragraph${h.paragraphs === 1 ? '' : 's'}`);
    if (h.top_count) parts.push(`most common: ${h.top_name} (${h.top_count})`);
  } else {
    parts.push('Nothing needed changing');
  }
  if (typeof r.cost === 'number') {
    parts.push(`cost ${r.cost < 0.01 ? '<$0.01' : '$' + r.cost.toFixed(2)}`);
  }
  $('report-headline').textContent = parts.join(' · ');

  const groups = $('report-groups');
  groups.innerHTML = '';
  r.groups.forEach((g) => {
    const section = document.createElement('section');
    section.className = 'card';
    const head = document.createElement('h3');
    head.textContent = `${g.error_name} — ${g.count}`;
    section.append(head);
    g.findings.forEach((f) => section.append(findingCard(f)));
    groups.append(section);
  });

  const aside = $('report-aside');
  aside.innerHTML = '';
  addAside(aside, 'Left for you to judge', r.low_confidence,
    'These looked deliberate, or the model was unsure. Nothing in your '
    + 'document was changed for them.');
  addAside(aside, 'Not applied', r.not_applied,
    'These were found but could not be placed in the document safely.');
}

function addAside(parent, title, findings, blurb) {
  if (!findings.length) return;
  const box = document.createElement('details');
  box.className = 'card';
  const summary = document.createElement('summary');
  summary.textContent = `${title} (${findings.length})`;
  const note = document.createElement('p');
  note.className = 'muted';
  note.textContent = blurb;
  box.append(summary, note);
  findings.forEach((f) => box.append(findingCard(f, { showStatus: true })));
  parent.append(box);
}

// ── prompts ───────────────────────────────────────────────────────────────

async function loadPrompts() {
  const data = await api('/api/prompts');
  const list = $('prompt-list');
  list.innerHTML = '';
  data.types.forEach((t) => list.append(promptCard(t)));

  const passes = $('assembled-passes');
  passes.innerHTML = '';
  data.passes.forEach((p) => {
    const box = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = `Message ${p.keys.length > 1
      ? `covering ${p.keys.length} kinds of mistake` : `for ${p.keys[0]}`}`;
    const pre = document.createElement('pre');
    pre.className = 'prompt-preview';
    pre.textContent = p.system_prompt;
    box.append(summary, pre);
    passes.append(box);
  });
  $('assembled-sample').textContent = data.sample_user_turn;
}

function promptCard(t) {
  const card = document.createElement('details');
  card.className = 'card';

  const summary = document.createElement('summary');
  summary.textContent = t.name;
  if (t.edited) {
    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.textContent = 'edited';
    summary.append(' ', tag);
  }
  card.append(summary);

  const area = document.createElement('textarea');
  area.className = 'prompt-edit';
  area.rows = 12;
  area.value = t.detection_prompt;
  card.append(area);

  const status = document.createElement('p');
  status.className = 'status muted';

  const actions = document.createElement('div');
  actions.className = 'job-actions';

  const save = document.createElement('button');
  save.className = 'primary';
  save.textContent = 'Save';
  save.addEventListener('click', async () => {
    status.textContent = 'Saving…';
    try {
      await api(`/api/prompts/${t.key}`, {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ detection_prompt: area.value }),
      });
      status.textContent = 'Saved. Reviews from now on will use this.';
      status.className = 'status ok';
      loadPrompts();
    } catch (err) {
      status.textContent = err.message;
      status.className = 'status error';
    }
  });
  actions.append(save);

  if (t.edited) {
    const reset = document.createElement('button');
    reset.textContent = 'Reset to original';
    reset.addEventListener('click', async () => {
      await api(`/api/prompts/${t.key}`, { method: 'DELETE' });
      loadPrompts();
    });
    actions.append(reset);
  }

  card.append(actions, status);
  return card;
}

// ── settings ──────────────────────────────────────────────────────────────

async function loadSettings() {
  const { settings, keys } = await api('/api/settings');
  $('output-dir').value = settings.output_dir;
  $('comments').checked = settings.comments;
  $('explanations').checked = settings.explanations;
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
    explanations: $('explanations').checked,
  };
  [['anthropic', 'key-anthropic'], ['openai', 'key-openai'],
   ['gemini', 'key-gemini']].forEach(([provider, field]) => {
    if ($(field).value) payload[`${provider}_key`] = $(field).value;
  });

  await api('/api/settings', {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  });
  ['key-anthropic', 'key-openai', 'key-gemini'].forEach((f) => {
    $(f).value = '';
  });
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
