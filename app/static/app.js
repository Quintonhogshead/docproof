'use strict';

// Language rule for everything the user reads: no "chunks", no "tokens", no
// "batch", no "API" outside the Settings key fields. Sections, reviews, cost.

const $ = (id) => document.getElementById(id);
const api = async (path, options) => {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = `Something went wrong (${res.status}).`;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.json();
};

// selected: file id → Set of section ids the user kept. A file starts with
// every section in it, so "do nothing" means "review the whole document".
const state = { files: [], models: [], pollTimer: null, selected: new Map(),
                outputGuess: 600, sheet: null,
                // Which kind of document the user said they were starting
                // with: a format suffix, or "all" for both.
                formatChoice: 'all', formats: [], extraSuffixes: [] };

// ── navigation ────────────────────────────────────────────────────────────

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => show(tab.dataset.screen));
});

function show(name) {
  document.querySelectorAll('.tab').forEach((t) => {
    t.setAttribute('aria-current', String(t.dataset.screen === name));
  });
  ['drop', 'jobs', 'report', 'spending', 'prompts', 'settings'].forEach((s) => {
    $(`screen-${s}`).hidden = s !== name;
  });
  if (name === 'jobs') refreshJobs({ tick: true });
  if (name === 'settings') loadSettings();
  if (name === 'prompts') loadPrompts();
  if (name === 'spending') loadSpending();
}

// ── what we're doing with these documents ─────────────────────────────────

const kind = () => document.querySelector('input[name="kind"]:checked').value;
const prepOutput = () =>
  document.querySelector('input[name="prep-output"]:checked').value;
const isPrep = () => kind() === 'prep';

document.querySelectorAll('input[name="kind"]').forEach((r) =>
  r.addEventListener('change', () => { renderFiles(); renderKind(); }));
document.querySelectorAll('input[name="prep-output"]').forEach((r) =>
  r.addEventListener('change', renderCost));

// Everything the two jobs disagree about: which options are on screen, what
// the button says, and which files can go at all.
function renderKind() {
  const prep = isPrep();
  document.querySelectorAll('.review-only').forEach((el) => {
    el.hidden = prep;
  });
  $('prep-options').hidden = !prep;
  $('prep-cost').hidden = !prep;
  $('model-label').textContent = prep ? 'Which model should read it?'
                                      : 'Which reviewer?';
  $('start').textContent = prep ? 'Prepare for layout' : 'Start review';
  $('staged-title').textContent = prep ? 'Ready to prepare' : 'Ready to review';
  document.querySelectorAll('details.sections').forEach((el) => {
    el.hidden = prep;                 // prep always reads the whole manuscript
  });

  const blocked = usableFiles().filter((f) => !canRun(f));
  const warning = $('kind-warning');
  warning.hidden = blocked.length === 0;
  if (blocked.length) {
    warning.textContent = blocked
      .map((f) => `${f.filename}: ${reasonBlocked(f)}`).join(' · ');
  }
  renderCost();
}

const canRun = (f) => (isPrep() ? f.can_prep !== false : f.can_review !== false);
const reasonBlocked = (f) =>
  (isPrep() ? f.prep_error : f.review_error) || 'cannot be used for this.';

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
  $('drop-error').hidden = true;

  // A drop ignores the picker's filter, so the choice above the drop zone is
  // applied here as well. Only what the user asked for; the server still
  // preflights whatever gets through.
  const allowed = allowedSuffixes();
  const suffix = (f) => f.name.slice(f.name.lastIndexOf('.')).toLowerCase();
  const skipped = files.filter((f) => !allowed.includes(suffix(f)));
  files = files.filter((f) => allowed.includes(suffix(f)));
  if (skipped.length) {
    fail(`Not what you asked for, so left alone: `
         + `${skipped.map((f) => f.name).join(', ')}.`);
  }
  if (!files.length) return;

  const body = new FormData();
  files.forEach((f) => body.append('files', f));
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
    // A stack can mix manuscripts and layouts; the badge says which is which
    // without making the reader parse file extensions.
    if (f.format) name.append(' ', formatBadge(f.format.name));
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
    if (f.ok && !isPrep() && f.chunks && f.chunks.length > 1) {
      li.append(sectionPicker(f));
    }
    if (f.note) {
      const note = document.createElement('p');
      note.className = 'where';
      note.textContent = f.note;
      li.append(note);
    }
    list.append(li);
  });
  $('staged').hidden = state.files.length === 0;
  $('start').disabled = usableIds().length === 0;
}

function fileSummary(f) {
  if (isPrep()) {
    if (!f.prep) return f.prep_error || 'cannot be prepared.';
    const p = f.prep;
    return `${p.paragraphs} paragraphs, ${p.words.toLocaleString()} words`
      + `, ${p.blank_lines} blank line${p.blank_lines === 1 ? '' : 's'} to sort out`;
  }
  if (!f.can_review) return f.review_error || 'cannot be reviewed.';
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

function formatBadge(text) {
  const el = document.createElement('span');
  el.className = 'tag';
  el.textContent = text;
  return el;
}

const usableFiles = () => state.files.filter((f) => f.ok);
const usableIds = () => usableFiles().map((f) => f.id);

// Files with nothing ticked are simply left out of the run — as are files this
// job can't be done to at all, like an InDesign layout you asked to prep.
const filesToRun = () => usableFiles().filter(
  (f) => canRun(f) && (isPrep() || keptFor(f).size > 0));

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

// Prep sends the whole manuscript once, so its price is simply what the files
// add up to on this model.
function pricePrep(m) {
  let cost = 0;
  filesToRun().forEach((f) => {
    if (!f.prep) return;
    cost += (f.prep.input_tokens * m.input_per_mtok
      + f.prep.output_tokens * m.output_per_mtok) / 1e6;
  });
  return cost;
}

function renderCost() {
  const m = state.models.find((x) => x.id === $('model').value);
  $('model-blurb').textContent = m ? m.blurb : '';
  const money = (v) => (typeof v === 'number'
    ? `about $${v < 0.01 ? v.toFixed(3) : v.toFixed(2)}` : '');

  if (isPrep()) {
    const files = filesToRun();
    const note = $('prep-cost');
    note.textContent = m && files.length
      ? `Reading ${files.length} manuscript${files.length === 1 ? '' : 's'} on `
        + `${m.display} costs ${money(pricePrep(m))}. Asking for both files `
        + `costs no more than asking for one.`
      : '';
    const ready = m && m.available && files.length > 0;
    $('start').disabled = !ready;
    modelHint(m);
    return;
  }

  const price = m ? priceSelection(m) : { now: null, batch: null };
  $('cost-now').textContent = money(price.now);
  $('cost-batch').textContent = money(price.batch);

  const ready = m && m.available && filesToRun().length > 0;
  $('start').disabled = !ready;
  modelHint(m);
}

// A disabled button with no explanation is the worst first-run experience
// there is. Say what's missing and where to fix it.
function modelHint(m) {
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
  const label = button.textContent;
  button.disabled = true;
  button.textContent = 'Starting…';
  try {
    await api('/api/jobs', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        file_ids: filesToRun().map((f) => f.id),
        model: $('model').value,
        kind: kind(),
        prep_output: prepOutput(),
        mode: isPrep() ? 'now' : mode(),
        schedule_at: (!isPrep() && mode() === 'batch' && $('schedule-on').checked)
          ? $('schedule-at').value : null,
        min_confidence: $('confidence').value,
        selections: isPrep() ? {} : selectionPayload(),
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
    button.textContent = label;
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

  const active = jobs.filter(
    (j) => !['done', 'failed', 'cancelled'].includes(j.state)).length;
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
    if (job.format) name.append(' ', formatBadge(job.format.name));
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

    // Only offered before anything has actually started: once a review is
    // running, waiting overnight, or writing its files, there is nothing
    // local left to pull back — the work is billed or already in progress.
    if (job.state === 'queued' || job.state === 'scheduled') {
      const actions = document.createElement('div');
      actions.className = 'job-actions';
      const note = actionNote();
      const cancel = document.createElement('button');
      cancel.textContent = 'Cancel';
      cancel.addEventListener('click', async () => {
        cancel.disabled = true;
        note.hidden = true;
        try {
          await api(`/api/jobs/${job.id}/cancel`, { method: 'POST' });
          refreshJobs();
        } catch (err) {
          // The one way this fails: it started in the moment between opening
          // this screen and the click landing. Say so where it happened,
          // not on a screen the user isn't looking at.
          note.textContent = err.message;
          note.hidden = false;
          cancel.disabled = false;
        }
      });
      actions.append(cancel, note);
      li.append(actions);
    }

    if (job.ready && job.is_prep) {
      li.append(prepActions(job));
    } else if (job.ready) {
      const actions = document.createElement('div');
      actions.className = 'job-actions';
      const note = actionNote();
      const doc = openButton(job, 'document',
        job.format ? `Open in ${job.format.app}` : 'Open reviewed document',
        note);
      const read = document.createElement('button');
      read.textContent = 'See what changed';
      read.addEventListener('click', () => openReport(job));
      actions.append(doc, read,
        openButton(job, 'document', 'Show in Finder', note, { reveal: true }));
      const bits = [];
      if (typeof job.applied === 'number') {
        bits.push(`${job.applied} change${job.applied === 1 ? '' : 's'} suggested`);
      }
      // Reviewing one document twice leaves two entries that read alike; the
      // folder name is what tells them apart on disk.
      if (job.results_name) bits.push(`saved in “${job.results_name}”`);
      if (bits.length) {
        const meta = document.createElement('span');
        meta.className = 'file-meta';
        meta.textContent = bits.join(' · ');
        actions.append(meta);
      }
      li.append(actions, note);
      // Tracked changes are invisible until you know which panel shows them,
      // and that panel is in a different place in each application.
      if (job.format) {
        const where = document.createElement('p');
        where.className = 'where';
        where.textContent = job.format.where_to_look;
        li.append(where);
      }
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

// Where a result button says why it couldn't do what it said.
function actionNote() {
  const p = document.createElement('p');
  p.className = 'action-note error';
  p.hidden = true;
  return p;
}

// The window this app runs in cannot display a Word file and will not download
// one, so "Open in Word" asks the app to hand the file to Word — it is sitting
// in the user's own Documents folder already. Run in an ordinary browser, the
// app says so and the file is downloaded instead.
function openButton(job, which, text, note, { reveal = false } = {}) {
  const button = document.createElement('button');
  button.textContent = text;
  if (reveal) button.className = 'quiet';
  button.addEventListener('click', async () => {
    button.disabled = true;
    if (note) note.hidden = true;
    const query = reveal ? '?reveal=true' : '';
    try {
      await api(`/api/jobs/${job.id}/open/${which}${query}`, { method: 'POST' });
    } catch (err) {
      if (err.status === 501) {
        window.location.href = `/api/jobs/${job.id}/file/${which}`;
      } else if (note) {
        note.textContent = err.message;
        note.hidden = false;
      }
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

// The last manual step, done for you: open the house template, flow the tagged
// manuscript in, save the result beside everything else this job wrote.
function placeButton(job, note) {
  const button = document.createElement('button');
  button.textContent = 'Place into the InDesign template';
  button.addEventListener('click', async () => {
    button.disabled = true;
    note.className = 'action-note muted';
    note.textContent = 'Placing — this takes a minute, longer if InDesign is '
      + 'still starting up. InDesign will open.';
    note.hidden = false;
    try {
      const { filename } = await api(`/api/jobs/${job.id}/place`,
                                     { method: 'POST' });
      note.className = 'action-note ok';
      note.textContent = `Placed. ${filename} is in the same folder, and it is `
        + 'showing in the Finder.';
    } catch (err) {
      note.className = 'action-note error';
      note.textContent = err.message;
      // The one failure with somewhere to go: no template chosen yet.
      if (/Settings/.test(err.message)) {
        const go = document.createElement('button');
        go.textContent = 'Open Settings';
        go.className = 'link';
        go.addEventListener('click', () => show('settings'));
        note.append(' ', go);
      }
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

// A finished prep job: whichever files it wrote, the notes, and the one number
// that matters — whether the author's words came through untouched.
function prepActions(job) {
  const wrap = document.createElement('div');
  const actions = document.createElement('div');
  actions.className = 'job-actions';
  const note = actionNote();
  const first = job.prep_output === 'tracked' ? 'tracked' : 'indesign';
  if (job.prep_output !== 'tracked') {
    actions.append(openButton(job, 'indesign', 'Open the file for InDesign',
                              note));
  }
  if (job.prep_output !== 'indesign') {
    actions.append(openButton(job, 'tracked', 'Open the tracked-changes file',
                              note));
  }
  const read = document.createElement('button');
  read.textContent = 'Read the prep notes';
  read.addEventListener('click', () => openPrepReport(job));
  actions.append(read,
    openButton(job, first, 'Show in Finder', note, { reveal: true }));
  if (job.prep_output !== 'tracked') actions.append(placeButton(job, note));

  const bits = [];
  if (typeof job.tagged === 'number') bits.push(`${job.tagged} paragraphs tagged`);
  if (job.flags) bits.push(`${job.flags} flag${job.flags === 1 ? '' : 's'} for the designer`);
  if (job.results_name) bits.push(`saved in “${job.results_name}”`);
  if (bits.length) {
    const meta = document.createElement('span');
    meta.className = 'file-meta';
    meta.textContent = bits.join(' · ');
    actions.append(meta);
  }
  wrap.append(actions, note);

  const where = document.createElement('p');
  where.className = 'where';
  where.textContent = job.verified
    ? 'Checked word for word against your manuscript: nothing the author '
      + 'wrote was changed. Place the tagged file in InDesign — the paragraph '
      + 'style names in it are the template\'s own.'
    : 'Heads up: this file has not been confirmed word-for-word against the '
      + 'manuscript. Read the prep notes before placing it.';
  wrap.append(where);
  return wrap;
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
    renderReport(await api(`/api/jobs/${job.id}/report`), job.format);
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

function renderReport(r, format) {
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
  // Nothing below is applied to the document by this screen — it is a reading
  // of what is waiting in the file — so say where that file is reviewed first.
  if (format && h.applied) {
    const where = document.createElement('p');
    where.className = 'where';
    where.textContent = format.where_to_look;
    groups.append(where);
  }
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

// ── the prep notes ────────────────────────────────────────────────────────

async function openPrepReport(job) {
  show('report');
  $('report-title').textContent = job.filename;
  $('report-headline').textContent = 'Reading the notes…';
  $('report-groups').innerHTML = '';
  $('report-aside').innerHTML = '';
  try {
    renderPrepReport(await api(`/api/jobs/${job.id}/prep`));
  } catch (err) {
    $('report-headline').textContent = err.message;
  }
}

function renderPrepReport(d) {
  const c = d.counts;
  $('report-headline').textContent =
    `${c.tagged.toLocaleString()} paragraphs tagged · `
    + `${c.words.toLocaleString()} words · `
    + `${c.scene_breaks_inserted} scene break${c.scene_breaks_inserted === 1 ? '' : 's'} written · `
    + `${d.flags.length} flag${d.flags.length === 1 ? '' : 's'}`
    + (typeof d.cost === 'number'
      ? ` · cost ${d.cost < 0.01 ? '<$0.01' : '$' + d.cost.toFixed(2)}` : '');

  const groups = $('report-groups');
  groups.innerHTML = '';

  const check = document.createElement('p');
  check.className = d.verified ? 'where ok-line' : 'error';
  check.textContent = d.verified
    ? `Word for word, this says exactly what the manuscript said — checked on `
      + `the finished ${d.verification.length > 1 ? 'files' : 'file'}, not `
      + `assumed. Style set: ${d.style_sheet.name}.`
    : 'The check against the author\'s text did not pass. Nothing was handed '
      + 'over.';
  groups.append(check);

  groups.append(prepTable('Style mapping', ['InDesign paragraph style', 'Paragraphs'],
    Object.entries(d.styles).map(([name, n]) => [name, String(n)])));

  const cleanup = [
    [`${c.blank_lines_removed} blank line(s) removed`,
      'InDesign takes its vertical spacing from the styles, not from empty paragraphs.'],
    [`${c.paragraphs_trimmed} paragraph(s) trimmed`,
      'Typed spaces and tabs at the ends of paragraphs. First-line indents were left alone — those come from the style.'],
    [`${c.scene_breaks_inserted} scene break(s) written, ${c.scene_breaks_from_author} the author had typed`,
      'Only where the author signalled one. None were invented.'],
    [`${c.italic_paragraphs} paragraph(s) keep their italics`,
      'Inline emphasis is left exactly where it was.'],
  ];
  const card = document.createElement('section');
  card.className = 'card';
  const h = document.createElement('h3');
  h.textContent = 'What it did';
  card.append(h);
  cleanup.forEach(([title, blurb]) => {
    const p = document.createElement('p');
    const strong = document.createElement('strong');
    strong.textContent = title;
    const small = document.createElement('small');
    small.className = 'muted';
    small.textContent = ` ${blurb}`;
    p.append(strong, small);
    card.append(p);
  });
  groups.append(card);

  const aside = $('report-aside');
  aside.innerHTML = '';
  if (d.flags.length) {
    const box = document.createElement('section');
    box.className = 'card';
    const head = document.createElement('h3');
    head.textContent = `For the designer — ${d.flags.length}`;
    const note = document.createElement('p');
    note.className = 'muted';
    note.textContent = 'These are raised, not fixed. Each one is a judgment a '
      + 'person should make.';
    box.append(head, note);
    d.flags.forEach((f) => {
      const p = document.createElement('p');
      p.className = 'finding-foot';
      const where = document.createElement('code');
      where.textContent = f.para_id;
      p.append(where, ` ${f.message}`);
      if (f.preview) {
        const em = document.createElement('em');
        em.className = 'muted';
        em.textContent = ` “${f.preview}”`;
        p.append(em);
      }
      box.append(p);
    });
    aside.append(box);
  }
}

function prepTable(title, headers, rows) {
  const card = document.createElement('section');
  card.className = 'card';
  const h = document.createElement('h3');
  h.textContent = title;
  const table = document.createElement('table');
  table.className = 'table';
  table.append(headRow(headers));
  rows.forEach((cells) => table.append(bodyRow(cells)));
  card.append(h, table);
  return card;
}

function headRow(cells) {
  const tr = document.createElement('tr');
  cells.forEach((text) => {
    const th = document.createElement('th');
    th.textContent = text;
    tr.append(th);
  });
  return tr;
}

function bodyRow(cells) {
  const tr = document.createElement('tr');
  cells.forEach((text) => {
    const td = document.createElement('td');
    td.textContent = text;
    tr.append(td);
  });
  return tr;
}

// ── spending ──────────────────────────────────────────────────────────────

const money = (v) => (typeof v !== 'number' ? '—'
  : v === 0 ? '$0.00' : v < 0.01 ? '<$0.01' : `$${v.toFixed(2)}`);
const count = (v) => (v || 0).toLocaleString();

async function loadSpending() {
  let d;
  try {
    d = await api('/api/usage');
  } catch (err) {
    $('spend-tiles').textContent = err.message;
    return;
  }
  const t = d.totals;
  const tiles = [
    ['Spent, all time', money(t.cost), `${count(t.jobs)} document${t.jobs === 1 ? '' : 's'}`],
    [`Last ${d.window.days} days`, money(d.window.cost),
      `${count(d.window.api_calls)} request${d.window.api_calls === 1 ? '' : 's'}`],
    ['Words processed', count(t.words), 'across every finished document'],
    ['Tokens in', count(t.input_tokens),
      `${count(t.cache_read_tokens)} read from cache`],
    ['Tokens out', count(t.output_tokens), 'what the model wrote back'],
    ['Requests', count(t.api_calls), d.unfinished ? `${d.unfinished} still running` : 'all finished'],
  ];
  const box = $('spend-tiles');
  box.innerHTML = '';
  tiles.forEach(([label, value, sub]) => {
    const tile = document.createElement('div');
    tile.className = 'tile';
    const l = document.createElement('small');
    l.className = 'muted';
    l.textContent = label;
    const v = document.createElement('strong');
    v.textContent = value;
    const s = document.createElement('small');
    s.className = 'muted';
    s.textContent = sub;
    tile.append(l, v, s);
    box.append(tile);
  });

  renderMonths(d.by_month);

  const models = $('spend-models');
  models.innerHTML = '';
  models.append(headRow(['Reviewer', 'Documents', 'Tokens in', 'Tokens out', 'Cost']));
  d.by_model.forEach((m) => models.append(bodyRow([
    m.display, count(m.api_calls) + ' requests', count(m.input_tokens),
    count(m.output_tokens), money(m.cost)])));
  if (!d.by_model.length) {
    models.append(bodyRow(['Nothing yet', '', '', '', '']));
  }

  const recent = $('spend-recent');
  recent.innerHTML = '';
  recent.append(headRow(['Document', 'What for', 'Reviewer', 'Words', 'Cost']));
  d.recent.forEach((r) => recent.append(bodyRow([
    r.filename, r.kind === 'prep' ? 'Prepared for layout' : 'Reviewed',
    r.display, count(r.words), money(r.cost)])));
  if (!d.recent.length) {
    recent.append(bodyRow(['Nothing yet', '', '', '', '']));
  }

  let note = $('spend-note');
  if (!note) {
    note = document.createElement('p');
    note.id = 'spend-note';
    note.className = 'muted small';
    $('screen-spending').append(note);
  }
  note.textContent = d.note;
}

function renderMonths(months) {
  const box = $('spend-months');
  box.innerHTML = '';
  if (!months.length) {
    box.className = 'muted';
    box.textContent = 'Nothing spent yet.';
    return;
  }
  box.className = 'months';
  const top = Math.max(...months.map((m) => m.cost), 0.0001);
  months.forEach((m) => {
    const row = document.createElement('div');
    row.className = 'month';
    const label = document.createElement('small');
    label.className = 'muted';
    label.textContent = m.month;
    const bar = document.createElement('div');
    bar.className = 'bar';
    const fill = document.createElement('i');
    fill.style.width = `${Math.max(2, Math.round((m.cost / top) * 100))}%`;
    bar.append(fill);
    const value = document.createElement('small');
    value.textContent = money(m.cost);
    row.append(label, bar, value);
    box.append(row);
  });
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
  $('indesign-template').value = settings.indesign_template || '';
  $('comments').checked = settings.comments;
  $('explanations').checked = settings.explanations;
  $('prep-output-default').value = settings.prep_output || 'indesign';
  loadStyleSheet().catch(() => {});
  loadVersion().catch(() => {});
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
    indesign_template: $('indesign-template').value.trim(),
    comments: $('comments').checked,
    explanations: $('explanations').checked,
    prep_output: $('prep-output-default').value,
  };
  [['anthropic', 'key-anthropic'], ['openai', 'key-openai'],
   ['gemini', 'key-gemini']].forEach(([provider, field]) => {
    if ($(field).value) payload[`${provider}_key`] = $(field).value;
  });
  // Not an AI key and not billed for, but it is still a secret, so it goes the
  // same way: to the Keychain, and never back to this page.
  if ($('key-github').value) payload.github_token = $('key-github').value;

  await api('/api/settings', {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  });
  ['key-anthropic', 'key-openai', 'key-gemini', 'key-github'].forEach((f) => {
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

// ── which build this is ───────────────────────────────────────────────────

// A build is identified by more than its version number: two .apps can both
// say 0.1.0 and be a month apart, and the commit is what tells them apart.
async function loadVersion() {
  const v = await api('/api/version');
  const bits = [`Version ${v.version}`];
  if (v.built) {
    bits.push(`built ${new Date(v.built).toLocaleDateString(undefined,
      { year: 'numeric', month: 'short', day: 'numeric' })}`);
  } else {
    bits.push('running from the source');
  }
  if (v.commit) bits.push(`commit ${v.commit}`);
  if (v.branch && v.branch !== 'main') bits.push(`on ${v.branch}`);
  $('version-summary').textContent = `${bits.join(' · ')}.`;
  // The token is only ever used to read published releases, which is only how
  // a build somebody was *sent* can check. Run from the source there is a
  // checkout to ask instead, and the field would be a puzzle.
  $('version-token-field').hidden = !v.frozen;
}

function versionNote(message, kind) {
  const note = $('version-status');
  note.className = `action-note ${kind}`;
  note.textContent = message;
  note.hidden = false;
}

$('version-check').addEventListener('click', async () => {
  const button = $('version-check');
  const download = $('version-download');
  button.disabled = true;
  download.hidden = true;
  versionNote('Looking…', 'muted');
  try {
    const r = await api('/api/version/check');
    versionNote(r.message, r.ok && r.current ? 'ok' : r.ok ? '' : 'error');
    // Only offered when there is actually a disk image to fetch — a release
    // published without one, or a build sitting behind its own checkout, has
    // nothing for this button to do.
    if (r.ok && !r.current && r.asset) {
      const mb = r.asset.size ? ` (${Math.round(r.asset.size / 1e6)} MB)` : '';
      download.textContent = `Download ${r.release_name || r.tag}${mb}`;
      download.hidden = false;
    }
    if (r.needs_token) $('key-github').focus();
  } catch (err) {
    versionNote(err.message, 'error');
  } finally {
    button.disabled = false;
  }
});

$('version-download').addEventListener('click', async () => {
  const button = $('version-download');
  button.disabled = true;
  versionNote('Downloading — this is a large file, so give it a moment.',
              'muted');
  try {
    const r = await api('/api/version/download', { method: 'POST' });
    versionNote(r.message, 'ok');
    button.hidden = true;
  } catch (err) {
    versionNote(err.message, 'error');
  } finally {
    button.disabled = false;
  }
});

// ── the house style guide ─────────────────────────────────────────────────

// Shown in Settings, and named on the drop screen, because which style set is
// in force is the single most important thing about a prep run.
async function loadStyleSheet() {
  const d = await api('/api/prep/styles');
  state.sheet = d;
  $('sheet-override').textContent = d.override_path;
  $('prep-sheet-note').textContent = d.ok ? `— ${d.name}` : '';
  if (!d.ok) {
    $('sheet-summary').textContent = d.error;
    $('sheet-summary').className = 'error';
    return;
  }
  $('sheet-summary').className = 'muted';
  $('sheet-summary').textContent =
    `In force: ${d.name}, version ${d.version}`
    + (d.trim ? `, trim ${d.trim}` : '')
    + ` — ${d.styles.length} paragraph styles, scene breaks written as `
    + `${d.glyph}. ${d.using_override
      ? 'This is your own file.' : 'This is the one DocProof ships with.'}`;

  $('sheet-reset').hidden = !d.using_override;
  renderStyleEditor(d);
}

// The values a designer actually picks between. Offered as a fixed list rather
// than a free number, because "18 or 20 point" is a decision and "18.3" is a
// typo — and because these are the sizes the template was drawn around.
const SIZES = [8, 9, 10, 11, 12, 13, 14, 16, 18, 20, 24, 26, 28];
const SPACES = [0, 3, 6, 12, 18, 24, 36, 48];
const INDENTS = [-18, 0, 12, 18, 24];
const SWITCHES = [['bold', 'Bold'], ['italic', 'Italic'],
                  ['page_break_before', 'Starts a page'],
                  ['keep_next', 'Stays with what follows']];

function choice(key, values, current, format) {
  const select = document.createElement('select');
  select.dataset.key = key;
  select.append(new Option('—', ''));
  // A sheet may name a value that is not on the list. Keep it rather than
  // quietly snapping somebody's own file to the nearest option we offer.
  const all = current == null || values.includes(current)
    ? values : [...values, current].sort((a, b) => a - b);
  all.forEach((v) => select.append(new Option(format(v), String(v))));
  select.value = current == null ? '' : String(current);
  return select;
}

function labelled(text, control) {
  const label = document.createElement('label');
  label.className = 'style-knob';
  const span = document.createElement('span');
  span.textContent = text;
  label.append(span, control);
  return label;
}

const points = (v) => `${v} pt`;

function renderStyleEditor(d) {
  const editor = $('sheet-editor');
  editor.innerHTML = '';

  const sheetLevel = document.createElement('div');
  sheetLevel.className = 'style-row';
  const trim = document.createElement('input');
  trim.type = 'text';
  trim.id = 'sheet-trim';
  trim.value = d.trim || '';
  const glyph = document.createElement('input');
  glyph.type = 'text';
  glyph.id = 'sheet-glyph';
  glyph.value = d.glyph || '';
  sheetLevel.append(labelled('Trim size', trim),
                    labelled('Scene breaks are written as', glyph));
  editor.append(sheetLevel);

  d.styles.forEach((s, i) => {
    const row = document.createElement('div');
    row.className = 'style-row';
    row.dataset.style = String(i);

    const head = document.createElement('p');
    head.className = 'style-name';
    head.textContent = s.name;
    const who = document.createElement('small');
    who.className = 'muted';
    who.textContent = ` · Word style ${s.id} · applied by `
      + (s.assign === 'model' ? 'reading the manuscript' : 'the rules');
    head.append(who);
    row.append(head);

    const knobs = document.createElement('div');
    knobs.className = 'style-knobs';
    knobs.append(
      labelled('Size', choice('size', SIZES, s.format.size ?? null, points)),
      labelled('Space above',
               choice('space_before', SPACES, s.format.space_before ?? null,
                      points)),
      labelled('Space below',
               choice('space_after', SPACES, s.format.space_after ?? null,
                      points)),
      labelled('First line',
               choice('indent', INDENTS, s.format.indent ?? null,
                      (v) => (v < 0 ? `${-v} pt hanging` : `${v} pt`))));

    const align = document.createElement('select');
    align.dataset.key = 'align';
    [['', '—'], ['left', 'Left'], ['center', 'Centred'], ['right', 'Right']]
      .forEach(([v, text]) => align.append(new Option(text, v)));
    align.value = s.format.align || '';
    knobs.append(labelled('Aligned', align));

    SWITCHES.forEach(([key, text]) => {
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.dataset.key = key;
      box.checked = Boolean(s.format[key]);
      knobs.append(labelled(text, box));
    });

    row.append(knobs);
    editor.append(row);
  });
}

// Only what the user actually moved. Sending the whole sheet back would work
// too, but it would rewrite values nobody touched — and this file is somebody
// else's, not ours.
function collectStyleChanges() {
  const styles = {};
  state.sheet.styles.forEach((s, i) => {
    const row = $('sheet-editor').querySelector(`[data-style="${i}"]`);
    if (!row) return;
    const update = {};
    const clear = [];
    row.querySelectorAll('[data-key]').forEach((control) => {
      const key = control.dataset.key;
      if (control.type === 'checkbox') {
        const was = Boolean(s.format[key]);
        if (control.checked === was) return;
        if (control.checked) update[key] = true; else clear.push(key);
        return;
      }
      const raw = control.value;
      const now = raw === '' ? null : (key === 'align' ? raw : Number(raw));
      const was = s.format[key] ?? null;
      if (now === was) return;
      if (now === null) clear.push(key); else update[key] = now;
    });
    if (Object.keys(update).length || clear.length) {
      styles[s.name] = clear.length ? { ...update, clear } : update;
    }
  });

  const body = { styles };
  if ($('sheet-trim').value !== (state.sheet.trim || '')) {
    body.trim = $('sheet-trim').value;
  }
  if ($('sheet-glyph').value !== (state.sheet.glyph || '')) {
    body.scene_break_glyph = $('sheet-glyph').value;
  }
  return body;
}

function sheetStatus(message, ok = false) {
  const note = $('sheet-status');
  note.textContent = message;
  note.className = `action-note ${ok ? 'ok' : 'error'}`;
  note.hidden = !message;
}

$('sheet-pick').addEventListener('click', () => $('sheet-file').click());

$('sheet-file').addEventListener('change', async () => {
  const file = $('sheet-file').files[0];
  if (!file) return;
  const body = new FormData();
  body.append('file', file);
  sheetStatus('');
  try {
    await api('/api/prep/styles/sheet', { method: 'POST', body });
    await loadStyleSheet();
    sheetStatus(`Now using ${state.sheet.name}. Any adjustments you had made `
                + 'to the previous style guide went with it.', true);
  } catch (err) {
    // The style sheet loader's messages are written for whoever is editing the
    // YAML, so they are worth showing exactly as they came.
    sheetStatus(err.message);
  }
  $('sheet-file').value = '';
});

$('sheet-reset').addEventListener('click', async () => {
  sheetStatus('');
  try {
    await api('/api/prep/styles/sheet', { method: 'DELETE' });
    await loadStyleSheet();
    sheetStatus('Back to the style guide DocProof ships with.', true);
  } catch (err) {
    sheetStatus(err.message);
  }
});

$('sheet-save').addEventListener('click', async () => {
  const body = collectStyleChanges();
  const saved = $('sheet-saved');
  saved.hidden = true;
  sheetStatus('');
  if (!Object.keys(body.styles).length && body.trim === undefined
      && body.scene_break_glyph === undefined) {
    sheetStatus('Nothing has been changed yet.', true);
    return;
  }
  try {
    await api('/api/prep/styles/format',
              { method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body) });
    await loadStyleSheet();
    saved.hidden = false;
  } catch (err) {
    sheetStatus(err.message);
  }
});

// ── boot ──────────────────────────────────────────────────────────────────

// The drop zone advertises whatever the server can actually read, so adding a
// format server-side never leaves the front door describing the old list.
async function loadFormats() {
  const d = await api('/api/formats');
  state.formats = d.formats;
  state.extraSuffixes = d.prep_extra_suffixes || [];

  const choices = $('format-choice');
  choices.innerHTML = '';
  // One button per format the server reads, plus the answer most people want,
  // which is "I have both and I would rather not sort them".
  [...d.formats.map((f) => ({ value: f.suffix, label: `${f.kind}s` })),
   { value: 'all', label: 'Both' }]
    .forEach(({ value, label }) => {
      const wrap = document.createElement('label');
      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = 'format-choice';
      radio.value = value;
      radio.checked = value === 'all';
      radio.addEventListener('change', () => applyFormatChoice(value));
      const span = document.createElement('span');
      span.textContent = label;
      wrap.append(radio, span);
      choices.append(wrap);
    });
  applyFormatChoice('all');
}

// Which suffixes the picker and the drop zone accept right now. The convertible
// manuscript formats ride with Word: a .rtf becomes a .docx at drop time, and
// there is no sense in which it is an InDesign file.
function allowedSuffixes() {
  const choice = state.formatChoice;
  if (choice === '.idml') return ['.idml'];
  if (choice === 'all') {
    return [...state.formats.map((f) => f.suffix), ...state.extraSuffixes];
  }
  return [choice, ...state.extraSuffixes];
}

function applyFormatChoice(choice) {
  state.formatChoice = choice;
  input.accept = allowedSuffixes().join(',');

  const format = state.formats.find((f) => f.suffix === choice);
  const converts = ` — plus ${state.extraSuffixes.slice(0, -1).join(', ')} and `
    + `${state.extraSuffixes.slice(-1)} manuscripts, if LibreOffice is installed`;
  if (format) {
    $('drop-formats').textContent = `${format.kind}s (${format.suffix})`
      + (choice === '.idml' ? '' : converts);
  } else {
    const names = state.formats.map((f) => `${f.kind}s (${f.suffix})`);
    $('drop-formats').textContent =
      (names.length > 1
        ? `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
        : names[0]) + converts;
  }

  // A layout cannot be prepped — prep is the step that gets a manuscript INTO
  // InDesign — so choosing one answers the next question too.
  if (choice === '.idml' && isPrep()) {
    document.querySelector('input[name="kind"][value="review"]').checked = true;
    renderKind();
  }
  renderFiles();
}

// Which output the user last chose is a preference, so it comes from Settings
// rather than resetting to the default on every launch.
async function applyDefaults() {
  const { settings } = await api('/api/settings');
  const choice = settings.prep_output || 'indesign';
  const radio = document.querySelector(
    `input[name="prep-output"][value="${choice}"]`);
  if (radio) radio.checked = true;
}

loadFormats().catch(() => {});
loadModels().catch(() => {});
loadStyleSheet().catch(() => {});
applyDefaults().catch(() => {});
renderKind();
refreshJobs();
state.pollTimer = setInterval(() => {
  if (!$('screen-jobs').hidden) refreshJobs();
}, 5000);
