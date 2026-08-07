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

// Web build only, set once at boot from /api/me. The desktop app has no such
// route, so WEB stays false and every desktop path below is untouched.
let WEB = false;
let ME = null;

// ── navigation ────────────────────────────────────────────────────────────

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => show(tab.dataset.screen));
});

function show(name) {
  document.querySelectorAll('.tab').forEach((t) => {
    t.setAttribute('aria-current', String(t.dataset.screen === name));
  });
  ['drop', 'jobs', 'report', 'compare', 'watch', 'spending', 'prompts',
   'settings', 'admin'].forEach((s) => {
    $(`screen-${s}`).hidden = s !== name;
  });
  if (name === 'jobs') refreshJobs({ tick: true });
  if (name === 'settings') loadSettings();
  if (name === 'prompts') loadPrompts();
  if (name === 'spending') loadSpending();
  if (name === 'watch') loadWatch();
  if (name === 'admin') loadAdmin();
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
  showStaging(files.length);
  try {
    const { files: staged } = await api('/api/files', { method: 'POST', body });
    state.files = state.files.concat(staged);
    renderFiles();
  } catch (err) {
    fail(err.message);
  } finally {
    // Hide the instant the documents are read and shown — not after
    // loadModels, which is a separate, slower fetch that enriches the cost
    // figures. Leaving it up through that made the spinner linger beside the
    // list that had already appeared.
    hideStaging();
  }
  await loadModels();
}

// The spinner that fills the gap between a drop and the list below it. The
// server stages and preflights every file — and may convert one first — so
// this can run for a few seconds with otherwise nothing on screen.
function showStaging(count) {
  $('staging-text').textContent = count === 1
    ? 'Reading your document…'
    : `Reading your ${count} documents…`;
  $('staging').hidden = false;
}

function hideStaging() {
  $('staging').hidden = true;
}

// ── compare tracked changes ────────────────────────────────────────────────
// Two .docx in, a diff of their tracked changes out. Independent of the review
// flow above: its own upload endpoint (/api/compare), which — unlike
// /api/files — accepts documents that already carry tracked changes.
(() => {
  const slots = { a: null, b: null };

  function wire(key) {
    const zone = $(`cmp-zone-${key}`);
    const input = $(`cmp-input-${key}`);
    const name = $(`cmp-name-${key}`);
    const set = (file) => {
      slots[key] = file || null;
      name.textContent = file ? file.name
                              : 'Drop a .docx, or click to choose';
      name.classList.toggle('muted', !file);
      $('cmp-run').disabled = !(slots.a && slots.b);
    };
    zone.addEventListener('click', () => input.click());
    zone.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
    });
    input.addEventListener('change', () => set(input.files[0]));
    ['dragenter', 'dragover'].forEach((evt) =>
      zone.addEventListener(evt, (e) => {
        e.preventDefault(); zone.classList.add('hot');
      }));
    ['dragleave', 'drop'].forEach((evt) =>
      zone.addEventListener(evt, (e) => {
        e.preventDefault(); zone.classList.remove('hot');
      }));
    zone.addEventListener('drop', (e) => {
      const f = [...e.dataTransfer.files]
        .find((x) => x.name.toLowerCase().endsWith('.docx'));
      if (f) set(f);
    });
  }
  wire('a');
  wire('b');

  const mode = () =>
    document.querySelector('input[name="cmp-mode"]:checked').value;

  // The slot hints and the results panel are the same two files either way; only
  // the framing changes, so the labels follow the mode.
  function syncHints() {
    const fmt = mode() === 'formatting';
    $('cmp-hint-a').textContent = fmt ? '— e.g. hand-formatted for InDesign'
                                      : '— e.g. the human proofread';
    $('cmp-hint-b').textContent = "— e.g. DocProof's output";
    // A previous comparison is about the old mode; clear it so the two can't
    // be read side by side and mistaken for each other.
    $('cmp-results').hidden = true;
    $('cmp-error').hidden = true;
  }
  document.querySelectorAll('input[name="cmp-mode"]').forEach((r) =>
    r.addEventListener('change', syncHints));
  syncHints();

  $('cmp-run').addEventListener('click', runCompare);

  async function runCompare() {
    if (!(slots.a && slots.b)) return;
    const chosen = mode();
    $('cmp-error').hidden = true;
    $('cmp-results').hidden = true;
    $('cmp-run').disabled = true;
    $('cmp-busy').hidden = false;
    const body = new FormData();
    body.append('doc_a', slots.a);
    body.append('doc_b', slots.b);
    body.append('mode', chosen);
    try {
      const d = await api('/api/compare', { method: 'POST', body });
      if (chosen === 'formatting') renderFormatCompare(d);
      else renderCompare(d);
    } catch (err) {
      $('cmp-error').textContent = err.message;
      $('cmp-error').hidden = false;
    } finally {
      $('cmp-busy').hidden = true;
      $('cmp-run').disabled = false;
    }
  }

  const pct = (x) =>
    (x === null || x === undefined ? '—' : `${Math.round(x * 100)}%`);

  function tile(n, label) {
    const el = document.createElement('div');
    el.className = 'tile';
    const num = document.createElement('span');
    num.className = 'tile-num';
    num.textContent = String(n);
    const lab = document.createElement('span');
    lab.className = 'tile-label';
    lab.textContent = label;
    el.append(num, lab);
    return el;
  }

  function renderCompare(d) {
    const t = d.totals;
    const tiles = $('cmp-tiles');
    tiles.replaceChildren(
      tile(t.agree, 'agree'),
      tile(t.different_fix, 'different fix'),
      tile(t.only_a, `only ${d.label_a}`),
      tile(t.only_b, `only ${d.label_b}`));

    const s = d.score_a_as_truth;
    $('cmp-score').textContent =
      `Against ${d.label_a}: found ${pct(s.located_recall)} of its changes `
      + `(${pct(s.exact_recall)} with the same fix), precision `
      + `${pct(s.precision)}.`;
    $('cmp-caveat').textContent = d.unaligned_paragraphs.length
      ? `${d.unaligned_paragraphs.length} paragraph(s) skipped — their text `
        + `differs between the two files, so their edits can't be lined up.`
      : '';

    const misses = [];
    const differ = [];
    const extras = [];
    d.paragraphs.forEach((p) => {
      p.only_a.forEach((e) => misses.push(e.text));
      p.different_fix.forEach((e) =>
        differ.push(`${d.label_a}: ${e.a.text}   ·   ${d.label_b}: ${e.b.text}`));
      p.only_b.forEach((e) => extras.push(e.text));
    });

    const groups = $('cmp-groups');
    groups.replaceChildren(
      cmpGroup(`Missed by ${d.label_b}`, misses,
               `${d.label_a} changed these; ${d.label_b} did not.`),
      cmpGroup('Fixed differently', differ,
               'Both changed the same place, to different text.'),
      cmpGroup(`Only ${d.label_b}`, extras,
               `${d.label_b} changed these; ${d.label_a} did not — worth a `
               + `look for false positives.`));
    $('cmp-results').hidden = false;
  }

  // The formatting comparison: same two files, but the diff is over the
  // InDesign paragraph-style NAME each one gave every paragraph, not the
  // tracked changes. A is the human's tagging, B is DocProof's prep pass.
  function renderFormatCompare(d) {
    const t = d.totals;
    const tiles = $('cmp-tiles');
    tiles.replaceChildren(
      tile(t.agree, 'same style'),
      tile(t.different, 'different style'),
      tile(t.only_a, `only ${d.label_a}`),
      tile(t.only_b, `only ${d.label_b}`));

    $('cmp-score').textContent =
      `${t.agree} of ${d.aligned_paragraphs} paragraph(s) got the same style `
      + `(${pct(d.agreement)} agreement).`;
    const skipped = t.only_a + t.only_b;
    $('cmp-caveat').textContent = skipped
      ? `${skipped} paragraph(s) exist in only one file — a blank line kept or `
        + `dropped, a paragraph split — so they have no partner to compare.`
      : '';

    const differ = d.differences.map((e) =>
      `${e.text}   ·   ${d.label_a}: ${e.style_a}   ·   ${d.label_b}: ${e.style_b}`);

    const groups = $('cmp-groups');
    groups.replaceChildren(
      cmpGroup('Styled differently', differ,
               `Both files styled these; ${d.label_a} and ${d.label_b} chose `
               + `different paragraph styles.`));
    $('cmp-results').hidden = false;
  }

  function cmpGroup(title, items, blurb) {
    const card = document.createElement('div');
    card.className = 'card';
    const h = document.createElement('h3');
    h.textContent = `${title} (${items.length})`;
    card.append(h);
    const note = document.createElement('p');
    note.className = 'muted small';
    note.textContent = items.length ? blurb : 'None.';
    card.append(note);
    if (items.length) {
      const ul = document.createElement('ul');
      ul.className = 'cmp-list';
      items.forEach((x) => {
        const li = document.createElement('li');
        li.textContent = x;               // untrusted document text — never HTML
        ul.append(li);
      });
      card.append(ul);
    }
    return card;
  }
})();

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
  // The box sits above the staged list; whoever failed — a drop with nothing
  // staged yet, or Start at the bottom of the page — should still see it.
  box.scrollIntoView({ block: 'nearest' });
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
        WEB ? 'Download reviewed document'
            : (job.format ? `Open in ${job.format.app}` : 'Open reviewed document'),
        note);
      const read = document.createElement('button');
      read.textContent = 'See what changed';
      read.addEventListener('click', () => openReport(job));
      actions.append(doc, read);
      // "Show in Finder" only means something on the Mac the file lives on.
      if (!WEB) {
        actions.append(
          openButton(job, 'document', 'Show in Finder', note, { reveal: true }));
      }
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

      // A review that failed only the integrity check has all its work done
      // and paid for — offer to hand the file over anyway, clearly flagged.
      if (job.audit_failed) {
        const note = actionNote();
        const anyway = document.createElement('button');
        anyway.textContent = 'Download anyway';
        anyway.addEventListener('click', async () => {
          anyway.disabled = true;
          note.hidden = true;
          try {
            // Writes the .docx from the review already done, then hands it over
            // the same way a finished review does — otherwise the file lands on
            // the server and the button looks like it did nothing.
            await api(`/api/jobs/${job.id}/download-anyway`, { method: 'POST' });
            handOver(job, 'docx', note);
            refreshJobs();
          } catch (err) {
            note.textContent = err.message || 'Could not write the document.';
            note.hidden = false;
            anyway.disabled = false;
          }
        });
        actions.append(anyway);
        li.append(why, actions, note);
      } else {
        li.append(why, actions);
      }
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
    // In the browser build there is no local app to hand the file to, and no
    // Finder to reveal it in — the honest thing is to download it.
    if (WEB) { window.location.href = `/api/jobs/${job.id}/file/${which}`; return; }
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

// Hand a finished file to the user directly (not via a button press): download
// it in the browser build, open it in Word on the desktop — falling back to a
// download when there's no application to open it with.
function handOver(job, which, note) {
  if (WEB) { window.location.href = `/api/jobs/${job.id}/file/${which}`; return; }
  api(`/api/jobs/${job.id}/open/${which}`, { method: 'POST' }).catch((err) => {
    if (err.status === 501) {
      window.location.href = `/api/jobs/${job.id}/file/${which}`;
    } else if (note) {
      note.textContent = err.message;
      note.hidden = false;
    }
  });
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
    actions.append(openButton(job, 'indesign',
      WEB ? 'Download the InDesign-ready file' : 'Open the file for InDesign',
      note));
  }
  if (job.prep_output !== 'indesign') {
    actions.append(openButton(job, 'tracked',
      WEB ? 'Download the tracked-changes file' : 'Open the tracked-changes file',
      note));
  }
  const read = document.createElement('button');
  read.textContent = 'Read the prep notes';
  read.addEventListener('click', () => openPrepReport(job));
  actions.append(read);
  if (!WEB) {
    actions.append(
      openButton(job, first, 'Show in Finder', note, { reveal: true }));
    // Placing needs InDesign on a Mac; there is none behind a web build.
    if (job.prep_output !== 'tracked') actions.append(placeButton(job, note));
  }

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
  recent.append(headRow(['Document', 'What for', 'Started by', 'Reviewer',
                         'Words', 'Cost']));
  d.recent.forEach((r) => recent.append(bodyRow([
    r.filename, r.kind === 'prep' ? 'Prepared for layout' : 'Reviewed',
    startedBy(r.source), r.display, count(r.words), money(r.cost)])));
  if (!d.recent.length) {
    recent.append(bodyRow(['Nothing yet', '', '', '', '', '']));
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

// Two job stores, one bill — so a figure has to be able to say which half it
// came from.
const startedBy = (source) => (source === 'watch' ? 'DocWatch' : 'You');

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

// ── DocWatch ──────────────────────────────────────────────────────────────

async function loadWatch({ quiet = false } = {}) {
  const body = await api('/api/watch');
  // Refreshed on every deliberate load, kept on the five-second poll: a key
  // saved or deleted in Settings must show here the next time the tab is
  // opened, not after a full page reload.
  if (!state.watchModels || !quiet) {
    try {
      state.watchModels = (await api('/api/models')).models;
    } catch (_) { state.watchModels = state.watchModels || []; }
  }
  renderWatch(body, quiet);
}

// The inputs are filled only on a deliberate load — opening the tab, or
// finishing a save. The five-second poll redraws everything else, and a poll
// that rewrote the folder field would eat a paste mid-keystroke.
function renderWatch(body, quiet) {
  const w = body.watch;
  renderWatchSignIn(body);
  renderWatchRun(body);
  renderWatchFiles(w.files);
  if (quiet) return;

  $('watch-folder').value = w.folder_id || '';
  $('watch-output').value = w.prep_output || 'indesign';
  $('watch-notes').checked = w.upload_notes;
  $('watch-failure-note').checked = w.upload_failure_note;
  $('watch-auto').checked = w.auto_ticks;
  $('watch-agent').checked = w.times.length > 0;
  if (w.times.length) $('watch-times').value = w.times.join(',');
  $('watch-client-id').value = '';
  $('watch-client-secret').value = '';
  $('watch-client').open = !w.has_client;

  const picker = $('watch-model');
  picker.innerHTML = '';
  (state.watchModels || []).forEach((m) => {
    const option = document.createElement('option');
    option.value = m.id;
    option.textContent = m.available ? m.display : `${m.display} — no key yet`;
    picker.append(option);
  });
  picker.value = w.model;
}

function renderWatchSignIn(body) {
  const w = body.watch;
  const line = $('watch-signin');
  const actions = $('watch-signin-actions');
  actions.innerHTML = '';

  const signing = body.sign_in && body.sign_in.state === 'waiting';
  if (signing) {
    line.className = 'muted';
    line.textContent = 'A browser has opened so Google can ask whether '
      + 'DocProof may read this Drive. Nothing is typed into DocProof — your '
      + 'password stays with Google.';
    return;
  }
  if (body.sign_in && body.sign_in.state === 'failed') {
    watchNote($('watch-signin-note'), body.sign_in.message, 'error');
  }

  line.className = w.signed_in ? 'ok' : 'muted';
  line.textContent = w.signed_in
    ? (w.token_source === 'environment'
      ? 'Signed in — the sign-in came from your environment.'
      : 'Signed in. The sign-in is in your Keychain, not in a file.')
    : 'Not signed in to Google yet.';

  const go = document.createElement('button');
  go.className = w.signed_in ? 'quiet' : 'primary';
  go.textContent = w.signed_in ? 'Sign in again' : 'Sign in to Google';
  go.addEventListener('click', () => signInToGoogle(go));
  actions.append(go);

  if (w.signed_in) {
    const out = document.createElement('button');
    out.className = 'quiet';
    out.textContent = 'Forget this sign-in';
    out.addEventListener('click', async () => {
      renderWatch(await api('/api/watch/auth', { method: 'DELETE' }));
    });
    actions.append(out);
  }
}

async function signInToGoogle(button) {
  const note = $('watch-signin-note');
  note.hidden = true;
  button.disabled = true;
  try {
    const payload = {};
    if ($('watch-client-id').value) {
      payload.client_id = $('watch-client-id').value.trim();
    }
    if ($('watch-client-secret').value) {
      payload.client_secret = $('watch-client-secret').value.trim();
    }
    renderWatch(await api('/api/watch/auth', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    }));
  } catch (err) {
    watchNote(note, err.message, 'error');
    $('watch-client').open = true;
  } finally {
    button.disabled = false;
  }
}

function renderWatchRun(body) {
  const run = body.run;
  const line = $('watch-run-state');
  const bar = $('watch-progress');
  $('watch-run').disabled = run.busy;

  if (run.busy) {
    const doing = run.progress[0];
    line.className = 'muted';
    line.textContent = doing
      ? `${doing.filename} — ${doing.plain_state.toLowerCase()}`
      : 'Looking in the folder…';
    bar.hidden = !(doing && doing.total);
    if (doing && doing.total) {
      bar.firstElementChild.style.width =
        `${Math.round((doing.done / doing.total) * 100)}%`;
    }
    return;
  }

  bar.hidden = true;
  const last = run.last;
  if (!last) {
    line.className = 'muted';
    line.textContent = 'It hasn’t looked yet.';
    return;
  }
  if (last.skipped) {
    line.className = 'muted';
    line.textContent = 'A pass was already working on this folder, so the last '
      + 'one stood aside and let it finish.';
    return;
  }
  if (!last.ok) {
    line.className = 'error';
    line.textContent = watchTrouble(last);
    return;
  }
  line.className = 'muted';
  line.textContent = last.prepped.length
    ? `Prepared ${last.prepped.join(', ')}.`
    : `Nothing new — ${last.listed} file(s) looked at.`;
  if (last.deferred) {
    line.textContent += ` ${last.deferred} more waiting for the next pass.`;
  }
}

// The library writes for somebody holding a terminal. In here there are cards.
function watchTrouble(last) {
  if (last.error_kind === 'auth_expired') {
    return 'DocProof is no longer signed in to Google — the sign-in may have '
      + 'been revoked. Sign in again above.';
  }
  if (last.error_kind === 'not_configured') {
    return 'Something above still needs setting up before a pass can run.';
  }
  return last.error;
}

function renderWatchFiles(files) {
  const table = $('watch-files');
  table.innerHTML = '';
  $('watch-files-empty').hidden = files.length > 0;
  if (!files.length) return;

  table.append(headRow(['Manuscript', 'What happened', 'Put back', 'Cost']));
  files.forEach((f) => {
    table.append(bodyRow([f.name, f.plain_state,
                          f.uploaded.join(', ') || '—', money(f.cost)]));
  });
}

function renderWatchPlan(rows) {
  const holder = $('watch-plan');
  holder.innerHTML = '';
  if (!rows.length) return;
  const table = document.createElement('table');
  table.className = 'table';
  table.append(headRow(['In the folder', 'What a pass would do']));
  rows.forEach((r) => table.append(bodyRow([r.name, r.label])));
  holder.append(table);
}

function watchNote(el, message, kind) {
  el.className = `action-note ${kind}`;
  el.textContent = message;
  el.hidden = false;
}

// When a refusal has somewhere to go, offer the way there.
function watchNoteWithFix(el, message) {
  watchNote(el, message, 'error');
  if (/key/i.test(message)) {
    const go = document.createElement('button');
    go.className = 'link';
    go.textContent = 'Open Settings';
    go.addEventListener('click', () => show('settings'));
    el.append(' ', go);
  }
}

$('watch-save').addEventListener('click', async () => {
  const button = $('watch-save');
  const note = $('watch-save-note');
  note.hidden = true;
  button.disabled = true;
  try {
    const body = await api('/api/watch', {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        // null, not '': an empty box is "no change", and the server agrees.
        folder: $('watch-folder').value.trim() || null,
        model: $('watch-model').value,
        prep_output: $('watch-output').value,
        upload_notes: $('watch-notes').checked,
        upload_failure_note: $('watch-failure-note').checked,
      }),
    });
    renderWatch(body);
    watchNote(note, 'Saved.', 'ok');
  } catch (err) {
    watchNote(note, err.message, 'error');
  } finally {
    button.disabled = false;
  }
});

$('watch-run').addEventListener('click', async () => {
  const button = $('watch-run');
  const note = $('watch-run-note');
  note.hidden = true;
  button.disabled = true;
  button.textContent = 'Looking…';
  try {
    const body = await api('/api/watch/run', { method: 'POST' });
    renderWatch(body, true);
    if (!body.started) {
      watchNote(note, 'A pass is already running.', 'muted');
    }
  } catch (err) {
    watchNoteWithFix(note, err.message);
  } finally {
    button.textContent = 'Look now';
    button.disabled = false;
  }
});

$('watch-preview').addEventListener('click', async () => {
  const button = $('watch-preview');
  const note = $('watch-run-note');
  note.hidden = true;
  button.disabled = true;
  button.textContent = 'Looking…';
  try {
    const body = await api('/api/watch/preview', { method: 'POST' });
    renderWatchPlan(body.plan);
    watchNote(note, `A pass would prepare ${body.new} manuscript(s). Nothing `
      + 'was downloaded, prepared or uploaded.', 'muted');
  } catch (err) {
    watchNoteWithFix(note, err.message);
  } finally {
    button.textContent = 'Show me what a pass would do';
    button.disabled = false;
  }
});

$('watch-agent').addEventListener('change', async () => {
  const note = $('watch-schedule-note');
  note.hidden = true;
  const on = $('watch-agent').checked;
  try {
    const body = on
      ? await api('/api/watch/schedule', {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ times: $('watch-times').value.trim() }),
      })
      : await api('/api/watch/schedule', { method: 'DELETE' });
    renderWatch(body);
    watchNote(note, on
      ? `DocProof will look at ${body.watch.times.join(', ')}, every day.`
      : 'DocProof will only look when you ask it to.', 'ok');
  } catch (err) {
    $('watch-agent').checked = !on;
    watchNote(note, err.message, err.status === 501 ? 'muted' : 'error');
  }
});

$('watch-auto').addEventListener('change', async () => {
  const note = $('watch-schedule-note');
  note.hidden = true;
  try {
    await api('/api/watch', {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ auto_ticks: $('watch-auto').checked }),
    });
  } catch (err) {
    $('watch-auto').checked = !$('watch-auto').checked;
    watchNote(note, err.message, 'error');
  }
});

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

// ── a newer version is ready ──────────────────────────────────────────────

// Asked once, quietly, at launch — and only when this is a packaged build,
// because the source has nothing to install over. Failure of any kind is
// silence: no token, no internet, no releases. The banner is the only output,
// and nothing installs without its button being pressed.
async function offerUpdateIfBehind() {
  try {
    const v = await api('/api/version');
    if (!v.frozen) return;
    const r = await api('/api/version/check');
    if (!(r.ok && !r.current)) return;
    // Two ways to be behind, and the banner offers whichever this machine
    // actually has: a published release to install, or a checkout to rebuild
    // from. Anything else — a release with no disk image attached — has
    // nothing for the button to do, so there is no banner.
    if (r.asset) {
      $('update-banner-text').textContent =
        `${r.release_name || r.tag} is ready.`;
    } else if (r.can_rebuild) {
      state.rebuildOffered = true;
      const n = Number(r.behind);
      $('update-banner-text').textContent = n
        ? `${n} change${n === 1 ? '' : 's'} since this build.`
        : 'There are changes since this build.';
      $('update-now').textContent = 'Rebuild and update';
    } else {
      return;
    }
    $('update-banner').hidden = false;
  } catch (_) { /* the manual check in Settings still exists */ }
}

$('update-later').addEventListener('click', () => {
  $('update-banner').hidden = true;          // until the next launch
});

$('update-now').addEventListener('click', async () => {
  const button = $('update-now');
  const text = $('update-banner-text');
  button.disabled = true;
  $('update-later').hidden = true;

  // Behind its own checkout: build it here rather than fetching a release
  // that does not exist yet.
  if (state.rebuildOffered) {
    await startRebuild(button, (message) => { text.textContent = message; });
    $('update-later').hidden = false;
    return;
  }

  text.textContent = 'Downloading and installing — DocProof will reopen '
    + 'itself in a moment…';
  try {
    const r = await api('/api/version/update', { method: 'POST' });
    text.textContent = r.message;
    // The server exits right after replying; the window going away IS the
    // success path. Nothing more to do here.
  } catch (err) {
    // A refusal (mid-review, running from a disk image) or a real failure.
    // Either way the installed app is untouched and says why.
    text.textContent = err.message;
    button.disabled = false;
    $('update-later').hidden = false;
  }
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

// Rebuilding from the checkout takes about a minute, so the server answers at
// once and this asks how it is going. The window disappearing is the success
// path: the app replaces itself and reopens.
async function watchRebuild(say) {
  for (;;) {
    let r;
    try {
      r = await api('/api/version/rebuild');
    } catch (_) {
      return;                    // the server is already going away
    }
    say(r.message, r.state === 'failed' ? 'error' : 'muted');
    if (r.state === 'failed' || r.state === 'done' || r.state === 'idle') return;
    await new Promise((done) => setTimeout(done, 1000));
  }
}

async function startRebuild(button, say) {
  button.disabled = true;
  say('Starting…', 'muted');
  try {
    await api('/api/version/rebuild', { method: 'POST' });
    await watchRebuild(say);
  } catch (err) {
    say(err.message, 'error');
  } finally {
    button.disabled = false;
  }
}

$('version-rebuild').addEventListener('click', () => {
  startRebuild($('version-rebuild'), versionNote);
});

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
    // On the machine DocProof is written on there is no release to fetch —
    // the newest DocProof is the checkout, and the app can build it itself.
    $('version-rebuild').hidden = !(r.ok && !r.current && r.can_rebuild);
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

// ── sign-in and mode (web build) ───────────────────────────────────────────

async function resolveSession() {
  // /api/me is a web-only route: 200 signed in, 401 signed out, and — in the
  // desktop app, where it doesn't exist — a 404 that leaves WEB false.
  try {
    ME = await api('/api/me');
    WEB = true;
  } catch (err) {
    WEB = err.status === 401;
    ME = null;
  }
}

function applyMode() {
  if (!WEB) return;
  $('user-area').hidden = false;
  $('user-email').textContent = ME.email;
  $('tab-admin').hidden = !ME.is_admin;
  // The desktop-only corners have no place in a shared web build: the Google
  // Drive watcher, and the local Settings (keys live in the server's
  // environment, and its file paths mean nothing to a browser). The detection
  // prompts are shared server config, so only an admin edits them — hide the
  // tab from everyone else.
  const hide = ['watch', 'settings'];
  if (!ME.is_admin) hide.push('prompts');
  for (const screen of hide) {
    const tab = document.querySelector(`.tab[data-screen="${screen}"]`);
    if (tab) tab.hidden = true;
  }
  // Both jobs are available on the web: review, and preparing a manuscript for
  // layout. Prep hands back Word files — a tagged .docx whose paragraph styles
  // are the house template's own — which is document work the server does
  // itself. Only the last step, placing that file into a native .indd, needs
  // InDesign on a Mac, and its button is already kept off the web build.
  $('update-banner').hidden = true;
}

function startApp() {
  applyMode();
  loadFormats().catch(() => {});
  loadModels().catch(() => {});
  loadStyleSheet().catch(() => {});
  applyDefaults().catch(() => {});
  if (!WEB) offerUpdateIfBehind();
  renderKind();
  refreshJobs();
  state.pollTimer = setInterval(() => {
    if (!$('screen-jobs').hidden) refreshJobs();
    if (WEB) return;
    if (!$('screen-watch').hidden) loadWatch({ quiet: true }).catch(() => {});
  }, 5000);
}

function showLogin() {
  $('login').hidden = false;
  addReveal($('login-password'));
  $('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const error = $('login-error');
    const button = e.target.querySelector('button');
    error.hidden = true;
    button.disabled = true;
    try {
      ME = await api('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: $('login-email').value,
                               password: $('login-password').value }),
      });
      $('login').hidden = true;
      startApp();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    } finally {
      button.disabled = false;
    }
  });
}

// The build's version, from the one route open before sign-in. Shown on the
// login screen and in the header once inside.
async function showVersion() {
  try {
    const { version } = await api('/healthz');
    if (!version) return;
    const header = $('app-version');
    if (header) header.textContent = 'v' + version;
    const login = $('login-version');
    if (login) login.textContent = 'DocProof v' + version;
  } catch (_) { /* the version line just stays empty */ }
}

async function boot() {
  showVersion();
  await resolveSession();
  if (WEB && !ME) { showLogin(); return; }
  startApp();
}

// ── God Mode ────────────────────────────────────────────────────────────────

$('logout').addEventListener('click', async () => {
  try { await api('/api/logout', { method: 'POST' }); } catch (_) {}
  window.location.reload();
});

$('admin-create').addEventListener('click', async () => {
  const note = $('admin-new-note');
  note.hidden = true;
  const cap = $('admin-new-cap').value.trim();
  const body = { email: $('admin-new-email').value.trim(),
                 password: $('admin-new-password').value,
                 is_admin: $('admin-new-admin').checked };
  if (cap !== '') body.monthly_cap = Number(cap);
  try {
    await api('/api/admin/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    for (const id of ['admin-new-email', 'admin-new-password', 'admin-new-cap']) {
      $(id).value = '';
    }
    $('admin-new-admin').checked = false;
    note.className = 'action-note ok';
    note.textContent = 'Added.';
    note.hidden = false;
    loadAdmin();
  } catch (err) {
    note.className = 'action-note error';
    note.textContent = err.message;
    note.hidden = false;
  }
});

async function adminUpdate(id, patch) {
  try {
    await api(`/api/admin/users/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
  } catch (err) {
    alert(err.message);
  }
  loadAdmin();
}

// A Show/Hide toggle for a password or key field. Wraps the input in a little
// flex row and drops a button beside it. Safe to call more than once.
function addReveal(input) {
  if (!input || input.dataset.reveal) return input;
  input.dataset.reveal = '1';
  const row = document.createElement('span');
  row.className = 'pw-row';
  input.parentNode.insertBefore(row, input);
  row.appendChild(input);
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'reveal';
  btn.textContent = 'Show';
  btn.addEventListener('click', () => {
    const hidden = input.type === 'password';
    input.type = hidden ? 'text' : 'password';
    btn.textContent = hidden ? 'Hide' : 'Show';
  });
  row.appendChild(btn);
  return input;
}

async function loadKeys() {
  if (!WEB || !ME || !ME.is_admin) return;
  let data;
  try { data = await api('/api/admin/keys'); } catch (_) { return; }
  const wrap = $('admin-keys');
  wrap.innerHTML = '';
  for (const k of data.keys) {
    const field = document.createElement('div');
    field.className = 'key-field';

    const label = document.createElement('div');
    label.className = 'key-label';
    const name = document.createElement('strong');
    name.textContent = k.display;
    const status = document.createElement('span');
    status.className = 'muted small';
    status.textContent = k.configured
      ? (k.source === 'portal' ? ' · set here' : ' · from the server environment')
      : ' · not set';
    label.append(name, status);

    const input = document.createElement('input');
    input.type = 'password';
    // Not 'off': browsers ignore that on password fields and fill in the
    // site's saved sign-in password — which then gets saved as "the key".
    // 'new-password' is the value they actually honor by leaving it alone.
    input.autocomplete = 'new-password';
    input.name = `${k.provider}-api-key`;
    input.placeholder = k.configured
      ? 'saved — paste a new key to replace' : 'paste a key';

    const save = document.createElement('button');
    save.className = 'primary';
    save.textContent = 'Save';
    const test = document.createElement('button');
    test.textContent = 'Test';
    const remove = document.createElement('button');
    remove.className = 'quiet';
    remove.textContent = 'Remove';
    const note = actionNote();

    save.addEventListener('click', async () => {
      const key = input.value.trim();
      if (!key) {
        note.className = 'action-note error';
        note.textContent = 'Paste a key first.';
        note.hidden = false;
        return;
      }
      save.disabled = true;
      try {
        await api(`/api/admin/keys/${k.provider}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key }),
        });
        loadKeys();
      } catch (err) {
        note.className = 'action-note error';
        note.textContent = err.message;
        note.hidden = false;
      } finally { save.disabled = false; }
    });

    test.addEventListener('click', async () => {
      note.className = 'action-note muted';
      note.textContent = 'Testing…';
      note.hidden = false;
      try {
        const r = await api(`/api/settings/test/${k.provider}`, { method: 'POST' });
        note.className = 'action-note ' + (r.ok ? 'ok' : 'error');
        note.textContent = r.message;
      } catch (err) {
        note.className = 'action-note error';
        note.textContent = err.message;
      }
    });

    remove.addEventListener('click', () => api(`/api/admin/keys/${k.provider}`,
      { method: 'DELETE' }).then(loadKeys).catch((err) => {
        note.className = 'action-note error';
        note.textContent = err.message;
        note.hidden = false;
      }));

    const row = document.createElement('div');
    row.className = 'job-actions key-row';
    row.append(input, save, test);
    if (k.configured && k.source === 'portal') row.append(remove);

    field.append(label, row, note);
    addReveal(input);
    wrap.append(field);
  }
}

async function loadReviewDefaults() {
  if (!WEB || !ME || !ME.is_admin) return;
  let data;
  try { data = await api('/api/settings'); } catch (_) { return; }
  $('admin-comments').checked = data.settings.comments;
  $('admin-explanations').checked = data.settings.explanations;
}

$('admin-settings-save').addEventListener('click', async () => {
  const saved = $('admin-settings-saved');
  const btn = $('admin-settings-save');
  saved.hidden = true;
  btn.disabled = true;
  try {
    await api('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ comments: $('admin-comments').checked,
                             explanations: $('admin-explanations').checked }),
    });
    saved.hidden = false;
  } catch (err) {
    alert(err.message);
  } finally { btn.disabled = false; }
});

// -- house style guide (prep tags manuscripts into it) ------------------------

async function loadHouseStyle() {
  if (!WEB || !ME || !ME.is_admin) return;
  let data;
  try { data = await api('/api/prep/styles'); } catch (_) { return; }
  const summary = $('admin-sheet-summary');
  const reset = $('admin-sheet-reset');
  if (!data.ok) {
    summary.className = 'error';
    summary.textContent = data.error
      || 'The current style guide could not be read.';
    reset.hidden = !data.using_override;
    return;
  }
  const count = (data.styles || []).length;
  summary.className = 'muted';
  summary.textContent = data.using_override
    ? `Using your uploaded guide “${data.name}” (v${data.version}) — ${count} styles.`
    : `Using the shipped default “${data.name}” (v${data.version}) — ${count} `
      + 'styles. Upload your own to replace it for everyone.';
  reset.hidden = !data.using_override;
}

$('admin-sheet-pick').addEventListener('click',
  () => $('admin-sheet-file').click());

$('admin-sheet-file').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const note = $('admin-sheet-note');
  note.className = 'action-note muted';
  note.textContent = 'Checking the style guide…';
  note.hidden = false;
  const fd = new FormData();
  fd.append('file', file);              // field name matches the route param
  try {
    // No Content-Type header: the browser sets the multipart boundary.
    await api('/api/prep/styles/sheet', { method: 'POST', body: fd });
    note.className = 'action-note ok';
    note.textContent = 'Installed. Prep uses it from the next document on.';
    loadHouseStyle();
  } catch (err) {
    note.className = 'action-note error';
    note.textContent = err.message;
  } finally {
    e.target.value = '';                // let the same file be re-picked
  }
});

$('admin-sheet-reset').addEventListener('click', async () => {
  const note = $('admin-sheet-note');
  try {
    await api('/api/prep/styles/sheet', { method: 'DELETE' });
    note.className = 'action-note ok';
    note.textContent = 'Back to the shipped style guide.';
    note.hidden = false;
    loadHouseStyle();
  } catch (err) {
    note.className = 'action-note error';
    note.textContent = err.message;
    note.hidden = false;
  }
});

async function loadAdmin() {
  if (!WEB || !ME || !ME.is_admin) return;
  addReveal($('admin-new-password'));
  let data;
  try { data = await api('/api/admin/users'); } catch (_) { return; }
  const money = (n) => `$${(n || 0).toFixed(2)}`;
  const table = $('admin-users');
  table.innerHTML = '';
  const head = document.createElement('tr');
  head.innerHTML = '<th>Email</th><th>Role</th><th>This month</th>'
    + '<th>Monthly limit</th><th></th>';
  table.append(head);

  for (const u of data.users) {
    const tr = document.createElement('tr');
    if (u.disabled) tr.className = 'row-disabled';

    const email = document.createElement('td');
    email.textContent = u.email;
    const role = document.createElement('td');
    role.innerHTML = u.is_admin
      ? '<span class="pill">admin</span>' : '<span class="pill off">user</span>';
    const spent = document.createElement('td');
    spent.textContent = money(u.spent_this_month);

    const capCell = document.createElement('td');
    const cap = document.createElement('input');
    cap.type = 'number'; cap.min = '0'; cap.step = '1';
    cap.className = 'admin-cap-input';
    cap.value = u.monthly_cap ?? '';
    cap.placeholder = u.is_admin ? '∞'
      : (data.default_cap != null ? String(data.default_cap) : '∞');
    cap.disabled = u.is_admin;              // admins are never capped
    cap.addEventListener('change', () => {
      const v = cap.value.trim();
      adminUpdate(u.id, { monthly_cap: v === '' ? null : Number(v) });
    });
    capCell.append(cap);

    const actions = document.createElement('td');
    const toggle = document.createElement('button');
    toggle.className = 'link';
    toggle.textContent = u.disabled ? 'Enable' : 'Disable';
    if (u.id === ME.id) toggle.disabled = true;   // no locking yourself out
    toggle.addEventListener('click',
      () => adminUpdate(u.id, { disabled: !u.disabled }));
    actions.append(toggle);

    tr.append(email, role, spent, capCell, actions);
    table.append(tr);
  }
  loadKeys();
  loadReviewDefaults();
  loadHouseStyle();
}

boot();
