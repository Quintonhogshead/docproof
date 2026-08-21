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

// `api`, but survives a transient blip: a dropped connection ("Failed to fetch",
// which fetch throws with no HTTP status) or a 502/503/504 from the edge while a
// slow model call ran. Retried a few times with growing backoff; a real error —
// a 400 on a bad batch — carries a status and is not retried, so it still
// surfaces at once. `onRetry(attempt, max, err)` lets the caller say it's
// waiting rather than stuck. This is what keeps one flaky batch of a long
// marked-PDF read from aborting the whole thing with hundreds of edits already
// read.
const apiRetry = async (path, options, { tries = 4, onRetry } = {}) => {
  let delay = 600;
  for (let attempt = 1; ; attempt += 1) {
    try {
      return await api(path, options);
    } catch (e) {
      const transient = e.status === undefined
        || e.status === 502 || e.status === 503 || e.status === 504;
      if (!transient || attempt >= tries) throw e;
      if (onRetry) onRetry(attempt, tries, e);
      // eslint-disable-next-line no-await-in-loop
      await new Promise((r) => setTimeout(r, delay));
      delay *= 2;
    }
  }
};

// selected: file id → Set of section ids the user kept. A file starts with
// every section in it, so "do nothing" means "review the whole document".
const state = { files: [], models: [], pollTimer: null, selected: new Map(),
                outputGuess: 600, sheet: null,
                // The reasoning-effort → output-cost factors and the house
                // default model, both sent by /api/models so the picker never
                // hardcodes a server-owned number.
                effortMultipliers: {}, defaultModel: null,
                // The shipped catalog default, served alongside default_model so
                // a stale persisted value on the volume can't surface Sonnet.
                catalogDefaultModel: null, defaultGlossaryModel: null,
                defaultJudgeModel: null, defaultMeaningModel: null,
                defaultFixModel: null, defaultChapterContinuityModel: null,
                defaultContinuityModel: null,
                // The effort tiers served by /api/presets (id → {controls,
                // features, sapling policy}), the currently selected tier id
                // ('light'|'standard'|'hard'|'hammer'|'custom'|null before the
                // first apply), and whether a Sapling key is configured — the
                // one signal that decides Hard's Sapling and the Hammer note.
                presets: {}, tier: null, saplingKeyed: false,
                // The per-run pass switches, as sent by /api/features: [{id,
                // label, blurb, group, heavy, default}]. The live on/off state
                // lives in the rendered checkboxes; collectFeatures() reads it.
                features: [],
                // Which kind of document the user said they were starting
                // with: a format suffix, or "all" for both.
                formatChoice: 'all', formats: [], extraSuffixes: [],
                // A corrections job's proof — the marked-up PDF or redlined Word
                // file dropped alongside the InDesign book. Held here (not staged
                // as a job file: the manuscript preflight rejects it) until the
                // one-button "read and apply" reads it into the edit list.
                correctionsSource: null,
                // How many reads of that proof (or of a typed list) are in
                // flight. A PDF is read a batch at a time and each batch fills
                // the edit list as it lands, so a half-read list is indistin-
                // guishable from a finished one by its contents alone: this is
                // what keeps Apply greyed out until the model has read them all.
                correctionsReading: 0,
                // And whether the last read ended in an error, which leaves a
                // part-read list behind. Apply stays locked on that too: the
                // corrections past the failure were never read at all.
                correctionsReadFailed: false,
                // Raised while a read writes into the edit-list box, so the
                // box's own input handler can tell that fill from a hand-edit.
                correctionsFilling: false,
                // Set while the Start button's own click is running, so a
                // re-render mid-run (a read filling the list, files clearing)
                // can't hand the button back before the job is away.
                startBusy: false,
                // The Promo tab stages its file on selection (not at run time)
                // so it can price it before the run; this holds that staged
                // entry, with its preflight token counts, until the run uses it.
                // The marketing-plan card on the same tab stages its own file
                // the same way, kept separately so the two cards never collide.
                promoStaged: null, planStaged: null };

// Web build only, set once at boot from /api/me. The desktop app has no such
// route, so WEB stays false and every desktop path below is untouched.
let WEB = false;
let ME = null;

// ── navigation ────────────────────────────────────────────────────────────

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => show(tab.dataset.screen));
});

// The working-now banner jumps to the DocWatch tab, where the full run detail
// and per-file table live.
$('watch-banner').addEventListener('click', () => show('watch'));

function show(name) {
  // The report and fix screens have no tab of their own; both are reached from
  // Results, so keep that tab lit while either is open rather than leaving the
  // nav with nothing current.
  const current = (name === 'report' || name === 'fix') ? 'jobs' : name;
  document.querySelectorAll('.tab').forEach((t) => {
    t.setAttribute('aria-current', String(t.dataset.screen === current));
  });
  ['drop', 'jobs', 'report', 'fix', 'compare', 'sapling', 'watch', 'promo',
   'spending', 'prompts', 'settings', 'admin'].forEach((s) => {
    $(`screen-${s}`).hidden = s !== name;
  });
  if (name === 'jobs') refreshJobs({ tick: true });
  if (name === 'settings') loadSettings();
  if (name === 'prompts') loadPrompts();
  if (name === 'spending') loadSpending();
  if (name === 'watch') loadWatch();
  if (name === 'promo') loadPromo();
  if (name === 'admin') loadAdmin();
}

// ── what we're doing with these documents ─────────────────────────────────

const kind = () => document.querySelector('input[name="kind"]:checked').value;
const prepOutput = () =>
  document.querySelector('input[name="prep-output"]:checked').value;
const isPrep = () => kind() === 'prep';
const isPromo = () => kind() === 'promo';
const isCorrections = () => kind() === 'corrections';

document.querySelectorAll('input[name="kind"]').forEach((r) =>
  r.addEventListener('change', () => { renderFiles(); renderKind(); }));
document.querySelectorAll('input[name="prep-output"]').forEach((r) =>
  r.addEventListener('change', () => { renderBookOptions(); renderCost(); }));
// The last tier is on by default with the second look — checking one checks the
// other — but the two are separable: once the second look is on, the operator can
// turn the frontier reads off on their own without losing the cheaper pass.
(() => {
  const second = $('corrections-second-look');
  const escalate = $('corrections-escalate');
  if (second && escalate) {
    second.addEventListener('change', () => { escalate.checked = second.checked; });
  }
})();
// The corrections list gates its own Start button — enable it the moment there
// is something to apply — and every change to it redraws the readable review
// list, which is what a person actually checks (the JSON itself lives under
// Advanced).
(() => {
  const corr = $('corrections-input');
  if (corr) corr.addEventListener('input', () => {
    // A hand-edit of the JSON is someone taking the list in hand, so it clears
    // the lock a failed read left. The reads' own fills dispatch this same
    // event to re-gate Start, and must not unlock anything — they raise the
    // flag below for the length of the dispatch, which is synchronous.
    if (!state.correctionsFilling) state.correctionsReadFailed = false;
    renderCost();
    renderCorrectionsReview();
    renderCorrectionsSource();   // the line under the drop zone follows the read
  });
  // Typing a plain list arms the button too (it reads, then applies), so the
  // gate and its label follow the box as it fills or empties.
  const typed = $('corrections-list-text');
  if (typed) typed.addEventListener('input', () => { renderKind(); renderCost(); });
})();

// Reading a marked-up PDF proof, an author's redlined Word file, or a plain
// list into the corrections textarea. Each produces a draft edit list; the model
// (PDF and list paths) proposes, and nothing is applied here — the applied result
// is reviewed in the corrections report. The status card reports the read as it
// happens, prominently. `readCorrectionsSource` and `readCorrectionsList` are
// lifted to module scope so the one-button "read and apply" flow drives the
// same reads as the panel buttons.
let readCorrectionsSource = null;
let readCorrectionsList = null;
(() => {
  const statusEl = $('corrections-extract-status');
  const note = $('corrections-extract-note');
  if (!statusEl || !note) return;
  const progressEl = $('corrections-extract-progress');
  const bar = $('corrections-extract-bar');

  // The status card: a headline line, and (during a batched read) a progress
  // bar. Tone colours the left edge — '' working, 'done', 'error' — so the copy
  // itself needn't carry the whole signal.
  const show = (msg, tone = '') => {
    note.textContent = msg;
    statusEl.hidden = false;
    statusEl.classList.toggle('is-done', tone === 'done');
    statusEl.classList.toggle('is-error', tone === 'error');
  };
  const setProgress = (done, total) => {
    if (!progressEl || !bar) return;
    if (!total) { progressEl.hidden = true; return; }
    progressEl.hidden = false;
    bar.style.width = `${Math.round((done / total) * 100)}%`;
  };
  const hideProgress = () => { if (progressEl) progressEl.hidden = true; };

  // Hold the Start gate for the whole of a read. Each batch of a PDF read fills
  // the textarea as it lands, which on its own would arm Start — and label it
  // "Apply corrections" — over a list the model is still only part way through.
  // Counted rather than boolean so overlapping reads can't unlock it early.
  // A read that dies partway leaves behind exactly what it had read — useful to
  // look at, but not a list anyone should apply: the corrections after the
  // failure would be dropped silently. So a failure locks Apply too, until the
  // proof is read again (or the list is taken in hand under Advanced).
  const duringRead = async (task) => {
    state.correctionsReading += 1;
    state.correctionsReadFailed = false;
    renderKind();
    renderCost();
    try {
      return await task();
    } catch (e) {
      // Only a failure that left something behind locks Apply. One that read
      // nothing at all is just the unread state again — Start goes back to
      // offering the read, so a retry is one click rather than a hunt for the
      // panel's own button.
      state.correctionsReadFailed =
        !!(($('corrections-input') || {}).value || '').trim();
      throw e;
    } finally {
      state.correctionsReading -= 1;
      renderKind();
      renderCost();
    }
  };

  const setEdits = (edits) => {
    const ta = $('corrections-input');
    ta.value = JSON.stringify(edits, null, 2);
    // Flagged for the length of the dispatch so the listener can tell this fill
    // from a person editing the JSON by hand — only the latter clears the lock
    // a failed read leaves behind.
    state.correctionsFilling = true;
    try {
      ta.dispatchEvent(new Event('input', { bubbles: true }));   // re-gate Start
    } finally {
      state.correctionsFilling = false;
    }
  };
  const plural = (n) => (n === 1 ? '' : 's');
  const summarise = (count, issues, noun) => {
    const bits = [`${count} correction${plural(count)} read${noun ? ` ${noun}` : ''}`];
    if (issues && issues.length) {
      bits.push(`${issues.length} couldn’t be read (`
        + issues.map((i) => i.reason).slice(0, 2).join('; ') + ')');
    }
    bits.push('review below or in the report, then Apply');
    show(bits.join(' · '), 'done');
    hideProgress();
  };

  // A single-call read (Word file or plain list): drop the list in and summarise.
  // These sources carry no PDF comments and no pages, so clear anything left from
  // an earlier read — stale pages would narrow the new edits to the wrong book.
  const fillFromBody = (body) => {
    const ta = $('corrections-input');
    if (ta) ta.dataset.comments = '';
    if (ta) ta.dataset.pages = '';
    setEdits(JSON.parse(body.json));
    summarise(body.count, body.issues, '');
  };

  // Reading a marked-up PDF is two-phase: the server pulls the comments
  // (instant, free) and hands back bounded batches; we read each batch into
  // edits in turn, filling the textarea and climbing the bar as they land. A big
  // proof that once hung on one silent call — and, past the model's output
  // ceiling, truncated and lost every edit — now fills in steadily and cannot
  // truncate. A batch that fails leaves the edits read so far in place and
  // re-raises, so a one-button apply stops rather than applying a partial read.
  const readPdfFile = async (file) => {
    const edits = [];
    const issues = [];
    try {
      show('Reading the proof…');
      setProgress(0, 1);
      const form = new FormData();
      form.append('file', file);
      // The staged IDML, so the server can show the model the book's own words for
      // each marked page instead of the PDF's rendering of them. Optional: without
      // it the read still works, the anchors are just quoted from the proof.
      const staged = filesToRun()[0];
      if (staged) form.append('file_id', staged.id);
      const {
        count, batches, comments, pages, resolved,
      } = await api('/api/corrections/read-pdf', { method: 'POST', body: form });
      // Keep the reviewer comments beside the edit list they produced, so the
      // finished change log can account for every one — including any the model
      // turns into no edit. Cleared by the Word/list paths, which have none.
      const ta0 = $('corrections-input');
      if (ta0) ta0.dataset.comments = JSON.stringify(comments || []);
      // And the text of every proof page, which is what lets a mark on page 49 be
      // narrowed to the text page 49 set. An InDesign file has no pages, so
      // without this a correction to a bare comma has every comma in the book to
      // choose between and can only be flagged.
      if (ta0) ta0.dataset.pages = JSON.stringify(pages || []);
      // Marks that are a function of the text they sit on are already resolved,
      // exactly and for nothing, so the list starts filled and only the notes that
      // genuinely need reading go to the model.
      edits.push(...(resolved || []));
      if (edits.length) setEdits(edits);
      const done = edits.length;
      for (let i = 0; i < batches.length; i += 1) {
        setProgress(i, batches.length);
        show(`${count} comment${plural(count)} · ${done} read exactly · `
          + `batch ${i + 1} of ${batches.length} · `
          + `${edits.length} edit${plural(edits.length)} so far`);
        // Sequential on purpose: the bar climbs a batch at a time, and each
        // small call is safe on its own — and idempotent, so a batch that failed
        // to fetch (a dropped connection, an edge timeout on a slow model call)
        // is retried rather than abandoning the whole read.
        // eslint-disable-next-line no-await-in-loop
        const part = await apiRetry('/api/corrections/extract-list', {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ text: batches[i] }),
        }, { onRetry: (attempt, max) => show(
          `${count} comment${plural(count)} · ${edits.length} edit`
          + `${plural(edits.length)} so far · batch ${i + 1} of ${batches.length}`
          + ` hit a network snag — retrying (${attempt}/${max - 1})…`) });
        edits.push(...JSON.parse(part.json));
        if (part.issues) issues.push(...part.issues);
        setEdits(edits);                      // fill live so the count is seen to grow
      }
      setProgress(batches.length || 1, batches.length || 1);
      summarise(edits.length, issues, `from ${count} comment${plural(count)}`);
    } catch (e) {
      // A network failure carries no HTTP status; say so as a sentence rather
      // than surfacing the bare "Failed to fetch", and make clear the edits
      // already read are kept — the read can be resumed by reading the proof
      // again, and the good edits below still apply.
      const why = e.status === undefined
        ? 'The connection dropped while reading the proof.' : e.message;
      const kept = edits.length
        ? ` The ${edits.length} edit${plural(edits.length)} read so far are `
          + 'below and still apply; read the proof again to finish the rest.'
        : '';
      show(`${why}${kept}`, 'error');
      throw e;
    }
  };

  // A redlined Word file: deterministic, one call, no cost.
  const readDocxFile = async (file) => {
    try {
      show('Reading the Word file…');
      const form = new FormData();
      form.append('file', file);
      fillFromBody(await api('/api/corrections/extract-docx',
        { method: 'POST', body: form }));
    } catch (e) { show(e.message, 'error'); throw e; }
  };

  // The one read both the panel button and the one-button apply drive: pick the
  // reader by the file's suffix. Throws on failure so a caller that means to
  // apply next can stop instead of applying a half-read list.
  readCorrectionsSource = (file) => duringRead(() =>
    ((file.name || '').toLowerCase().endsWith('.pdf')
      ? readPdfFile(file) : readDocxFile(file)));

  // The panel's own PDF button previews the list into the textarea (does not
  // apply), so a designer who wants to eyeball it first still can. It reads the
  // dropped source when there is one, else the file chosen in the field.
  const pdfBtn = $('extract-pdf');
  if (pdfBtn) pdfBtn.addEventListener('click', () => withBusy(pdfBtn, 'Reading…',
    async () => {
      const file = state.correctionsSource
        || (($('corrections-pdf') || {}).files || [])[0];
      if (!file) { show('Choose a PDF proof first.', 'error'); return; }
      await readCorrectionsSource(file).catch(() => {});   // already surfaced
    }));

  const docxBtn = $('extract-docx');
  if (docxBtn) docxBtn.addEventListener('click', () => withBusy(docxBtn, 'Reading…',
    async () => {
      const file = (($('corrections-docx') || {}).files || [])[0];
      if (!file) { show('Choose a Word file first.', 'error'); return; }
      await duringRead(() => readDocxFile(file)).catch(() => {});
    }));

  // The typed plain-English list, read by the house model into an edit list.
  // Throws on failure like the source readers, so the one-button apply stops.
  const readListText = async () => {
    const text = (($('corrections-list-text') || {}).value || '').trim();
    if (!text) {
      show('Type or paste a list of corrections first.', 'error');
      throw new Error('no list to read');
    }
    try {
      show('Reading the list…');
      fillFromBody(await api('/api/corrections/extract-list', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ text }),
      }));
    } catch (e) { show(e.message, 'error'); throw e; }
  };
  readCorrectionsList = () => duringRead(readListText);

  const listBtn = $('extract-list');
  if (listBtn) listBtn.addEventListener('click', () => withBusy(listBtn, 'Reading…',
    () => readCorrectionsList().catch(() => {})));   // already surfaced
})();

// Disable a button, swap its label, run an async task, then restore it — the
// small busy dance the corrections read buttons share.
async function withBusy(btn, busyLabel, task) {
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = busyLabel;
  try { await task(); }
  finally { btn.disabled = false; btn.textContent = label; }
}

// ── the corrections review list ─────────────────────────────────────────────
// The edit list the run is submitted with is JSON in #corrections-input, but no
// human reviews JSON: this renders the same list as readable rows — the text to
// change struck through, its replacement beside it, a badge for anything that
// isn't a plain swap — each with a ✕ to drop it before applying. The field
// names mirror the server parser's aliases, so whatever a reader (or a hand in
// the Advanced box) produced is shown the way it will be understood.
const CORR_FIELDS = {
  find: ['find', 'original', 'original_text', 'from', 'old', 'before', 'search',
         'text'],
  replace: ['replace', 'replacement', 'corrected', 'correction', 'to', 'new',
            'after'],
  instruction: ['instruction', 'note', 'comment', 'reason', 'message'],
  kind: ['kind', 'type', 'category'],
  occurrence: ['occurrence', 'occ', 'nth', 'instance'],
  context: ['context', 'near', 'within', 'around'],
  page: ['page', 'page_number', 'pageno', 'p'],
  format: ['format', 'formatting', 'style', 'character_style'],
  paragraph: ['paragraph', 'paragraph_op', 'layout', 'para'],
  paragraph_style: ['paragraph_style', 'para_style', 'applied_style'],
};

function corrField(entry, name) {
  const keys = CORR_FIELDS[name];
  for (let i = 0; i < keys.length; i += 1) {
    const v = entry[keys[i]];
    if (v !== undefined && v !== null) return v;
  }
  return undefined;
}

// Plain words for the op vocabularies (model.PARA_OPS / model.FORMATS). An op
// these don't know renders as its raw name, so a new engine op is shown, just
// not yet in prose.
const CORR_PARA_LABELS = {
  'page-break': 'start on a new page',
  recto: 'start on a right-hand page',
  verso: 'start on a left-hand page',
  'column-break': 'start in the next column',
  'frame-break': 'start in the next frame',
  'no-page-break': 'clear the forced page break',
  'keep-with-next': 'keep with the next paragraph',
  'keep-together': 'keep the lines together',
  'allow-break': 'let the paragraph break across pages',
  'delete-paragraph': 'delete the paragraph',
  'insert-after': 'insert a paragraph after',
  'insert-before': 'insert a paragraph before',
  'merge-next': 'join with the next paragraph',
  'split-at': 'start a new paragraph here',
};
const CORR_FORMAT_LABELS = {
  italic: 'set in italics',
  roman: 'clear italics (roman)',
  swash: 'set as a swash (flourished) letterform',
  'no-swash': 'clear the swash letterform',
};

// One entry — an object with aliased fields, or a terse [find, replace] pair —
// read into the handful of things the row shows. Anything unreadable is kept
// and said so, never silently dropped.
function corrView(entry) {
  if (Array.isArray(entry)) {
    return {
      find: String(entry[0] ?? ''), replace: String(entry[1] ?? ''),
      instruction: entry.length > 2 ? String(entry[2] ?? '') : '',
      kind: 'mechanical', occurrence: 0, context: '', page: 0,
      format: '', paragraph: '', paragraphStyle: '',
    };
  }
  if (!entry || typeof entry !== 'object') return null;
  const str = (name) => {
    const v = corrField(entry, name);
    return v === undefined ? '' : String(v);
  };
  const int = (name) => {
    const v = Number(corrField(entry, name));
    return Number.isFinite(v) && v > 0 ? Math.floor(v) : 0;
  };
  const kind = (str('kind') || 'mechanical').trim().toLowerCase();
  return {
    find: str('find'), replace: str('replace'),
    instruction: str('instruction'), kind,
    occurrence: int('occurrence'), context: str('context'), page: int('page'),
    format: str('format'),
    paragraph: str('paragraph'),
    paragraphStyle: str('paragraph_style'),
  };
}

const ordinal = (n) => {
  const s = ['th', 'st', 'nd', 'rd'];
  const v = n % 100;
  return `${n}${s[(v - 20) % 10] || s[v] || s[0]}`;
};

function renderCorrectionsReview() {
  const wrap = $('corrections-review');
  const head = $('corrections-review-head');
  const list = $('corrections-review-list');
  const foot = $('corrections-review-foot');
  if (!wrap || !head || !list) return;
  const ta = $('corrections-input');
  const raw = ((ta || {}).value || '').trim();
  list.textContent = '';
  wrap.classList.remove('is-error');
  if (foot) foot.hidden = false;
  if (!raw) { wrap.hidden = true; return; }
  wrap.hidden = false;

  let entries;
  try {
    entries = JSON.parse(raw);
    if (!Array.isArray(entries)) throw new Error('not a list');
  } catch {
    wrap.classList.add('is-error');
    head.textContent = 'This edit list isn’t readable as JSON — fix it under '
      + 'Advanced below, or clear it and read the proof, Word file, or typed '
      + 'list again.';
    if (foot) foot.hidden = true;
    const adv = $('corrections-json');
    if (adv) adv.open = true;
    return;
  }

  // Dropping a row rewrites the JSON minus that entry; the input event redraws
  // this list and re-gates Start, exactly as a hand-edit would.
  const removeAt = (i) => {
    entries.splice(i, 1);
    ta.value = entries.length ? JSON.stringify(entries, null, 2) : '';
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  };

  const tag = (text, cls = '') => {
    const el = document.createElement('span');
    el.className = `corr-tag${cls ? ` ${cls}` : ''}`;
    el.textContent = text;
    return el;
  };
  const span = (cls, text) => {
    const el = document.createElement('span');
    el.className = cls;
    el.textContent = text;
    return el;
  };

  let designs = 0;
  let unread = 0;
  entries.forEach((entry, i) => {
    const v = corrView(entry);
    const li = document.createElement('li');
    li.className = 'corr-item';
    const body = document.createElement('div');
    body.className = 'corr-body';

    if (!v) {
      unread += 1;
      body.append(tag('couldn’t read', 'warn'),
                  span('corr-note', JSON.stringify(entry)));
    } else {
      const design = v.kind === 'design';
      if (design) designs += 1;
      const change = document.createElement('div');
      change.className = 'corr-change';
      if (design) change.append(tag('InDesign — flagged, not applied', 'accent'));
      if (v.paragraph && !design) {
        change.append(tag(CORR_PARA_LABELS[v.paragraph] || v.paragraph, 'accent'));
      }
      if (v.paragraphStyle && !design) {
        change.append(tag(`paragraph style: ${v.paragraphStyle}`, 'accent'));
      }
      if (v.format) {
        change.append(tag(CORR_FORMAT_LABELS[v.format] || v.format, 'accent'));
      }
      if (v.kind === 'judgment') change.append(tag('judgment call'));
      if (v.find && v.replace && v.find !== v.replace) {
        change.append(span('corr-del', v.find), span('corr-arrow', '→'),
                      span('corr-ins', v.replace));
      } else if (v.find && !v.replace && !design && !v.format && !v.paragraph
                 && !v.paragraphStyle) {
        change.append(tag('delete'), span('corr-del', v.find));
      } else if (!v.find && v.replace) {
        change.append(tag('insert'), span('corr-ins', v.replace));
      } else if (v.find) {
        change.append(span('corr-find', v.find));
      }
      body.append(change);
      const hints = [];
      if (v.occurrence) hints.push(`the ${ordinal(v.occurrence)} match`);
      if (v.page) hints.push(`page ${v.page}`);
      if (v.context) {
        const c = v.context.length > 70 ? `${v.context.slice(0, 70)}…` : v.context;
        hints.push(`near “${c}”`);
      }
      const note = [v.instruction, hints.join(' · ')].filter(Boolean).join(' · ');
      if (note) body.append(span('corr-note', note));
    }

    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'corr-remove';
    x.title = 'Drop this correction';
    x.setAttribute('aria-label', `Drop correction ${i + 1}`);
    x.textContent = '✕';
    x.addEventListener('click', () => removeAt(i));
    li.append(span('corr-num', String(i + 1)), body, x);
    list.append(li);
  });

  const n = entries.length - designs - unread;
  const plural = (c) => (c === 1 ? '' : 's');
  const bits = [`${n} correction${plural(n)} ready to apply`];
  if (designs) {
    bits.push(`${designs} layout request${plural(designs)} for the InDesign `
      + 'checklist');
  }
  if (unread) bits.push(`${unread} entr${unread === 1 ? 'y' : 'ies'} unreadable`);
  head.textContent = bits.join(' · ');
}
// Some browsers restore the textarea across a reload; draw whatever's there.
renderCorrectionsReview();

// The subject/title/author boxes only matter when a book-styled copy is
// among the outputs.
function renderBookOptions() {
  const el = $('book-options');
  if (el) el.hidden = !['book', 'all'].includes(prepOutput());
}

// Everything the two jobs disagree about: which options are on screen, what
// the button says, and which files can go at all.
function renderKind() {
  const prep = isPrep();
  const promo = isPromo();
  const corrections = isCorrections();
  // Review's options (confidence, sections, batch schedule) belong only to a
  // review; prep's output options belong only to prep; corrections has its own
  // panel and no model at all.
  document.querySelectorAll('.review-only').forEach((el) => {
    el.hidden = prep || promo || corrections;
  });
  // Two review-only fields have a second condition on top of the kind — the
  // between-round judge needs 2+ rounds, the meaning gate needs its switch on —
  // and the sweep above has just un-hidden both. Put them back.
  syncRounds();
  syncJudgeGates();
  syncExaminationJudgment();
  $('prep-options').hidden = !prep;
  const corr = $('corrections-options');
  if (corr) corr.hidden = !corrections;
  renderBookOptions();
  $('prep-cost').hidden = !prep;
  $('promo-cost').hidden = !promo;
  $('model-label').textContent = promo ? 'Which model should write it?'
    : prep ? 'Which model should read it?' : 'Which reviewer?';
  // With a proof attached — or a plain list typed — but not yet read into the
  // edit list, the button does both: read it, then apply. Once a list is
  // present (previewed or hand-written), it applies.
  const corrNeedsRead = corrections
    && !(($('corrections-input') || {}).value || '').trim()
    && (state.correctionsSource
        || (($('corrections-list-text') || {}).value || '').trim());
  // While the model is still reading, the button says so rather than offering
  // to apply a list that is only part read — renderCost keeps it greyed out to
  // match.
  $('start').textContent = promo ? 'Write promo copy'
    : prep ? 'Format the manuscript'
    : corrections ? (state.correctionsReading ? 'Reading corrections…'
      : corrNeedsRead ? 'Read corrections & apply' : 'Apply corrections')
    : 'Start review';
  $('staged-title').textContent = promo ? 'Ready to write copy'
    : prep ? 'Ready to prepare'
    : corrections ? 'Ready to correct' : 'Ready to review';
  document.querySelectorAll('details.sections').forEach((el) => {
    el.hidden = prep || promo || corrections;   // all read the whole document
  });

  // The custom drawer: for a review it is collapsed behind the Customize toggle
  // (the tier cards are the primary path); for prep/promo, which have no tiers,
  // it is shown flat — no tab strip, panels stacked — so the model, effort and
  // glossary pickers it now holds are visible, the .review-only sweep above
  // having culled everything else. Corrections runs no model, so the whole
  // drawer stays hidden for it, like a review with the drawer collapsed.
  const adv = $('advanced-options');
  if (adv) {
    if (prep || promo) {
      adv.classList.add('flat');
      adv.hidden = false;
    } else {
      adv.classList.remove('flat');
      adv.hidden = true;
      const toggle = $('customize-toggle');
      if (toggle) toggle.setAttribute('aria-expanded', 'false');
    }
  }

  const blocked = usableFiles().filter((f) => !canRun(f));
  const warning = $('kind-warning');
  warning.hidden = blocked.length === 0;
  if (blocked.length) {
    warning.textContent = blocked
      .map((f) => `${f.filename}: ${reasonBlocked(f)}`).join(' · ');
  }
  renderCost();
}

const canRun = (f) => {
  if (isPromo()) return f.can_promo !== false;
  if (isCorrections()) return f.can_correct !== false;
  return isPrep() ? f.can_prep !== false : f.can_review !== false;
};
const reasonBlocked = (f) =>
  (isPromo() ? f.promo_error : isCorrections() ? f.correct_error
   : isPrep() ? f.prep_error : f.review_error)
  || 'cannot be used for this.';

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

// ── corrections: the proof rides in the same drop ──────────────────────────
// A corrections job is an InDesign book plus a proof — a marked-up PDF or a
// redlined Word file. The proof is not a job file (the manuscript preflight
// refuses tracked changes and does not read PDFs at all), so a corrections drop
// is split here: the proof is held on state.correctionsSource for the one-button
// read-and-apply, and the book goes on to stage the normal way.
function routeCorrectionsDrop(files) {
  const suffix = (f) => f.name.slice(f.name.lastIndexOf('.')).toLowerCase();
  const pdfs = files.filter((f) => suffix(f) === '.pdf');
  const docxs = files.filter((f) => suffix(f) === '.docx');
  const idmls = files.filter((f) => suffix(f) === '.idml');
  const bookPresent = idmls.length > 0
    || usableFiles().some((f) => f.can_correct);
  // A PDF is unambiguous — nothing else uses one, so it is always the proof. A
  // Word file is claimed as a proof only when a book is present to correct;
  // even under the corrections kind, a lone .docx must stay a staged file so it
  // shows in the list with the kind picker live — otherwise it vanishes into the
  // proof slot and the run is stuck behind a missing IDML with no way to reroute
  // it to review, prep, or promo. Once a book lands, a Word drop is a proof again.
  const intent = pdfs.length > 0 || (bookPresent && docxs.length > 0);
  if (!intent) return files;
  const source = pdfs[0] || (bookPresent ? docxs[0] : null);
  if (!source) return files;
  if (pdfs.length > 1) {
    fail(`One proof at a time — reading ${source.name}, ignoring the other PDF(s).`);
  }
  setCorrectionsKind();
  attachCorrectionsSource(source);
  // Keep the book and anything else for normal staging; take out the proof and
  // any stray PDFs, which cannot stage anyway.
  return files.filter((f) => f !== source && suffix(f) !== '.pdf');
}

function setCorrectionsKind() {
  const radio = document.querySelector('input[name="kind"][value="corrections"]');
  if (radio && !radio.checked) { radio.checked = true; renderKind(); }
}

function attachCorrectionsSource(file) {
  state.correctionsSource = file;
  // A fresh proof supersedes any earlier draft in the box, so it is read anew —
  // and clears the lock an earlier failed read left on Apply.
  state.correctionsReadFailed = false;
  const ta = $('corrections-input');
  if (ta && ta.value.trim()) {
    ta.value = '';
    ta.dataset.comments = '';
    ta.dataset.pages = '';
    renderCorrectionsReview();
  }
  renderCorrectionsSource();
  renderKind();          // relabel Start → "Read corrections & apply"
  renderCost();          // re-gate Start
  autoReadCorrectionsSource(file);
}

// A dropped Word file is read into the edit list there and then, rather than
// waiting on the button: a redline is deterministic and free, and a file that
// is really a typed list goes to the model the same way a pasted one does — so
// either way, dropping it is enough to get JSON the applicator can take. The
// review list fills in under the panel as it lands, and the status card carries
// any failure. A PDF proof is left to the button on purpose: a big proof is a
// batched model read, and that spend stays a decision someone makes.
function autoReadCorrectionsSource(file) {
  if (!file || !(file.name || '').toLowerCase().endsWith('.docx')) return;
  if ((($('corrections-input') || {}).value || '').trim()) return;  // already read
  if (!readCorrectionsSource) return;                 // panel absent (older page)
  readCorrectionsSource(file).catch(() => {});        // surfaced on the status card
}

function clearCorrectionsSource() {
  state.correctionsSource = null;
  state.correctionsReadFailed = false;
  renderCorrectionsSource();
}

// The always-visible line under the drop zone, and the summary inside the
// corrections panel: which proof is attached and — until the book lands — a nudge
// to drop it. Text only; the Start button's label and gate are set elsewhere
// (renderKind / renderCost), so this can be called freely without a re-render loop.
function renderCorrectionsSource() {
  const src = state.correctionsSource;
  const bookStaged = usableFiles().some((f) => f.can_correct);
  // A Word file is read on arrival, so the nudge is about what is still
  // missing, not about a button that has already done its work.
  const read = (($('corrections-input') || {}).value || '').trim().length > 0;
  const note = $('corrections-source-note');
  if (note) {
    note.hidden = !src;
    if (src) {
      note.textContent = !bookStaged
        ? `Proof attached: ${src.name}. Now drop the InDesign file (.idml) to correct.`
        : read
          ? `Proof read: ${src.name}. Review the corrections below, then Apply.`
          : `Proof attached: ${src.name}. Click “Read corrections & apply” below.`;
    }
  }
  const summary = $('corrections-source');
  if (summary) {
    summary.hidden = !src;
    if (src) summary.textContent = `Proof to read: ${src.name}`;
  }
}

// Choosing a proof in the panel's own file field attaches it the same as a drop,
// so the one-button read-and-apply picks it up.
(() => {
  const pdfField = $('corrections-pdf');
  if (pdfField) pdfField.addEventListener('change', () => {
    const f = (pdfField.files || [])[0];
    if (f) attachCorrectionsSource(f);
  });
})();

async function upload(files) {
  if (!files.length) return;
  $('drop-error').hidden = true;

  // A marked-up PDF or a redlined Word file is a corrections *source*, not a job
  // file, so pull it out of the drop here (held for the read-and-apply) and let
  // the InDesign book and anything else stage normally below.
  files = routeCorrectionsDrop(files);
  if (!files.length) { renderCorrectionsSource(); return; }

  // A drop ignores the picker's filter, so the choice above the drop zone is
  // applied here as well. Only what the user asked for; the server still
  // preflights whatever gets through. A corrections book is always an IDML, even
  // when the picker is set to Word, so it is never filtered out of that drop.
  const allowed = isCorrections()
    ? [...allowedSuffixes(), '.idml'] : allowedSuffixes();
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
  // Once a review file is staged, open on Standard (the first stage only, so a
  // later tier or Custom choice survives a re-stage); a re-stage just resyncs
  // the highlight and price, in case model availability moved.
  if (kind() === 'review') {
    if (state.tier === null) maybeInitTier();
    else reEvaluateTier();
  }
  // The book just landed — refresh the proof note (its wording depends on whether
  // a correctable file is staged) and re-sync the corrections panel/button.
  renderCorrectionsSource();
  if (isCorrections()) renderKind();
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

// ── printable report styling ───────────────────────────────────────────────
// Shared by the compare report below and the designer-notes report further
// down. Both build a self-contained HTML document — DocProof header, summary
// cards, clean tables — that opens in any browser and prints to PDF. Every
// value that came from a document is escaped: these strings become files the
// user may send on.
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const REPORT_CSS = `
  :root{--ink:#1e1c1a;--muted:#6a6560;--line:#e2ded8;--accent:#7c4a2d;
    --accent-soft:#f3ece6;--warn:#9a3412;--bg:#fbfaf8;--panel:#fff}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
  .wrap{max-width:820px;margin:0 auto;padding:48px 32px 72px}
  header.rep{border-bottom:2px solid var(--accent);padding-bottom:20px;margin-bottom:26px}
  .brand{font:600 .74rem/1 ui-monospace,SFMono-Regular,Menlo,monospace;
    letter-spacing:.16em;text-transform:uppercase;color:var(--accent)}
  h1{font:600 1.85rem/1.2 "Iowan Old Style",Palatino,Georgia,serif;margin:.35em 0 .1em}
  .sub{color:var(--muted);margin:.15em 0}
  .files{display:flex;gap:12px 28px;flex-wrap:wrap;margin-top:14px;font-size:.9rem}
  .files b{color:var(--accent);font-weight:600}
  .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:26px 0 8px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
    padding:15px 12px;text-align:center}
  .card .n{display:block;font:600 1.9rem/1 "Iowan Old Style",Palatino,Georgia,serif}
  .card .l{display:block;color:var(--muted);font-size:.8rem;margin-top:6px}
  .headline{font-size:1.08rem;background:var(--accent-soft);border-radius:10px;
    padding:15px 18px;margin:14px 0 2px}
  .headline b{color:var(--accent)}
  .caveat{color:var(--muted);font-size:.9rem;margin:.4em 0 0}
  h2{font:600 1.1rem/1.3 "Iowan Old Style",Palatino,Georgia,serif;
    margin:32px 0 4px;border-bottom:1px solid var(--line);padding-bottom:6px}
  .blurb{color:var(--muted);font-size:.9rem;margin:.2em 0 12px}
  table{width:100%;border-collapse:collapse;font-size:.94rem;margin:6px 0}
  th{text-align:left;color:var(--muted);font-weight:600;font-size:.74rem;
    text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid var(--line);
    padding:7px 10px}
  td{border-bottom:1px solid var(--line);padding:9px 10px;vertical-align:top}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.86rem}
  .metrics td:last-child{font-weight:600;text-align:right;
    font-variant-numeric:tabular-nums}
  .style-a{color:var(--warn);font-weight:600}
  .style-b{color:var(--accent);font-weight:600}
  .none{color:var(--muted);font-style:italic;margin:6px 0}
  .foot{margin-top:44px;color:var(--muted);font-size:.8rem;
    border-top:1px solid var(--line);padding-top:14px}
  @media print{body{background:#fff}.wrap{padding:0}
    .card,.headline{border:1px solid #ccc}}
`;

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

  // The result last shown, kept so "Download report" can rebuild it as a
  // standalone HTML file without re-uploading or re-comparing anything.
  let lastReport = null;      // { mode, data, nameA, nameB }

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
    lastReport = null;
  }
  document.querySelectorAll('input[name="cmp-mode"]').forEach((r) =>
    r.addEventListener('change', syncHints));
  syncHints();

  $('cmp-run').addEventListener('click', runCompare);
  $('cmp-open').addEventListener('click', openReport);
  $('cmp-download').addEventListener('click', downloadReport);

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
      lastReport = { mode: chosen, data: d,
                     nameA: slots.a.name, nameB: slots.b.name };
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

  // ── the downloadable report ───────────────────────────────────────────────
  // A self-contained HTML file built from the JSON already on screen: no second
  // upload, no server round-trip. It opens in any browser and prints to PDF, and
  // it reads as a document — a titled header, summary cards, and clean tables —
  // rather than the working panel above. Escaping and the shared print styling
  // (esc, REPORT_CSS) live at module scope above, alongside the designer-notes
  // report that reuses them.

  const shell = (title, sub, files, body) =>
    `<!doctype html><html lang="en"><head><meta charset="utf-8">`
    + `<meta name="viewport" content="width=device-width,initial-scale=1">`
    + `<title>${esc(title)}</title><style>${REPORT_CSS}</style></head><body>`
    + `<div class="wrap"><header class="rep"><div class="brand">DocProof</div>`
    + `<h1>${esc(title)}</h1>${sub}${files}</header>${body}`
    + `<div class="foot">Generated ${esc(new Date().toLocaleString())} · `
    + `read locally — nothing was uploaded to a reviewer or billed.</div>`
    + `</div></body></html>`;

  const subLine = (d) =>
    `<p class="sub">${esc(d.label_a)} vs ${esc(d.label_b)} · `
    + `${d.aligned_paragraphs} paragraph(s) compared</p>`;

  const filesLine = (d, nameA, nameB) =>
    `<div class="files"><span><b>${esc(d.label_a)}</b> — `
    + `${esc(nameA || 'document A')}</span><span><b>${esc(d.label_b)}</b> — `
    + `${esc(nameB || 'document B')}</span></div>`;

  const cards = (items) =>
    `<div class="cards">${items.map((c) =>
      `<div class="card"><span class="n">${esc(c.n)}</span>`
      + `<span class="l">${esc(c.l)}</span></div>`).join('')}</div>`;

  const oneCol = (items) => items.length
    ? `<table><tbody>${items.map((x) =>
        `<tr><td class="mono">${esc(x)}</td></tr>`).join('')}</tbody></table>`
    : '<p class="none">None.</p>';

  function changesReport(d, nameA, nameB) {
    const t = d.totals;
    const s = d.score_a_as_truth;
    const misses = [];
    const differ = [];
    const extras = [];
    d.paragraphs.forEach((p) => {
      p.only_a.forEach((e) => misses.push(e.text));
      p.different_fix.forEach((e) => differ.push([e.a.text, e.b.text]));
      p.only_b.forEach((e) => extras.push(e.text));
    });
    const differTable = differ.length
      ? `<table><thead><tr><th>${esc(d.label_a)}</th><th>${esc(d.label_b)}</th>`
        + `</tr></thead><tbody>${differ.map((r) =>
          `<tr><td class="mono">${esc(r[0])}</td>`
          + `<td class="mono">${esc(r[1])}</td></tr>`).join('')}</tbody></table>`
      : '<p class="none">None.</p>';
    const caveat = d.unaligned_paragraphs.length
      ? `<p class="caveat">${d.unaligned_paragraphs.length} paragraph(s) skipped `
        + `— their text differs between the two files, so their edits can't be `
        + `lined up.</p>` : '';
    const body =
      cards([{ n: t.agree, l: 'agree' },
             { n: t.different_fix, l: 'different fix' },
             { n: t.only_a, l: `only ${d.label_a}` },
             { n: t.only_b, l: `only ${d.label_b}` }])
      + `<div class="headline">Against <b>${esc(d.label_a)}</b>: found `
      + `${pct(s.located_recall)} of its changes (${pct(s.exact_recall)} with `
      + `the same fix), precision ${pct(s.precision)}.</div>${caveat}`
      + `<h2>Scored against ${esc(d.label_a)} as ground truth</h2>`
      + `<table class="metrics"><tbody>`
      + `<tr><td>Located recall (found the spot)</td><td>${pct(s.located_recall)}</td></tr>`
      + `<tr><td>Exact recall (same fix too)</td><td>${pct(s.exact_recall)}</td></tr>`
      + `<tr><td>Precision</td><td>${pct(s.precision)}</td></tr>`
      + `<tr><td>F1</td><td>${pct(s.f1)}</td></tr></tbody></table>`
      + `<p class="caveat">Precision counts an <em>only in ${esc(d.label_b)}</em> `
      + `edit against ${esc(d.label_b)}, but such an edit may be a real error `
      + `${esc(d.label_a)} missed rather than a false alarm. Treat it as a floor `
      + `and eyeball the “Only in ${esc(d.label_b)}” list below.</p>`
      + `<h2>Missed by ${esc(d.label_b)} (${misses.length})</h2>`
      + `<p class="blurb">${esc(d.label_a)} changed these; ${esc(d.label_b)} `
      + `did not.</p>${oneCol(misses)}`
      + `<h2>Fixed differently (${differ.length})</h2>`
      + `<p class="blurb">Both changed the same place, to different text.</p>`
      + `${differTable}`
      + `<h2>Only in ${esc(d.label_b)} (${extras.length})</h2>`
      + `<p class="blurb">${esc(d.label_b)} changed these; ${esc(d.label_a)} did `
      + `not — worth a look for false positives.</p>${oneCol(extras)}`;
    return shell('Tracked-changes comparison', subLine(d),
                 filesLine(d, nameA, nameB), body);
  }

  function formattingReport(d, nameA, nameB) {
    const t = d.totals;
    const skipped = t.only_a + t.only_b;
    const rows = d.differences;
    const diffTable = rows.length
      ? `<table><thead><tr><th>Paragraph</th><th>${esc(d.label_a)}</th>`
        + `<th>${esc(d.label_b)}</th></tr></thead><tbody>${rows.map((e) =>
          `<tr><td>${esc(e.text)}</td><td class="style-a">${esc(e.style_a)}</td>`
          + `<td class="style-b">${esc(e.style_b)}</td></tr>`).join('')}`
        + `</tbody></table>`
      : `<p class="none">None — the two files agreed on every paragraph's `
        + `style.</p>`;
    const caveat = skipped
      ? `<p class="caveat">${skipped} paragraph(s) exist in only one file — a `
        + `blank line kept or dropped, a paragraph split — so they have no `
        + `partner to compare.</p>` : '';
    const body =
      cards([{ n: t.agree, l: 'same style' },
             { n: t.different, l: 'different style' },
             { n: t.only_a, l: `only ${d.label_a}` },
             { n: t.only_b, l: `only ${d.label_b}` }])
      + `<div class="headline"><b>${t.agree}</b> of <b>${d.aligned_paragraphs}`
      + `</b> paragraph(s) got the same style — <b>${pct(d.agreement)}</b> `
      + `agreement.</div>${caveat}`
      + `<h2>Styled differently (${rows.length})</h2>`
      + `<p class="blurb">Both files styled these paragraphs; ${esc(d.label_a)} `
      + `and ${esc(d.label_b)} chose different paragraph styles.</p>${diffTable}`;
    return shell('InDesign-formatting comparison', subLine(d),
                 filesLine(d, nameA, nameB), body);
  }

  // Open the report in a new tab, where it can be read and — via the browser's
  // own Print — saved as a PDF. If a pop-up blocker (or a webview that won't
  // open tabs) stops the window, fall back to handing over the .html file so
  // the button never silently does nothing.
  // Build the report as a blob URL both buttons draw from. Returns the URL and
  // the mode (the mode names the download file). Returns null with nothing on
  // screen, so callers can no-op.
  function buildReportUrl() {
    if (!lastReport) return null;
    const { mode: m, data, nameA, nameB } = lastReport;
    const html = m === 'formatting'
      ? formattingReport(data, nameA, nameB)
      : changesReport(data, nameA, nameB);
    const url = URL.createObjectURL(new Blob([html], { type: 'text/html' }));
    return { url, m };
  }

  // Name the download after the book being compared — the first upload is the
  // subject — so a folder of reports is read at a glance. Drop the extension
  // and any characters a filesystem would choke on; fall back to a generic
  // name if, somehow, no filename came through.
  function reportFilename(m) {
    const book = (lastReport && (lastReport.nameA || lastReport.nameB)) || '';
    const stem = book.replace(/\.[^.]+$/, '').replace(/[\\/:*?"<>|]+/g, ' ').trim();
    const label = m === 'formatting' ? 'formatting comparison'
      : 'tracked-changes comparison';
    return stem ? `${stem} — ${label}.html` : `docproof-compare-${m}.html`;
  }

  // Hand the blob over as a file the user keeps. Same mechanism the pop-up
  // fallback below uses, only here it is the whole point rather than a rescue.
  function saveReportUrl(url, m) {
    const a = document.createElement('a');
    a.href = url;
    a.download = reportFilename(m);
    document.body.append(a);
    a.click();
    a.remove();
  }

  // Open the report in a new tab. If a pop-up blocker (or a webview that won't
  // open tabs) stops the window, fall back to saving the file so the button
  // never silently does nothing.
  function openReport() {
    const built = buildReportUrl();
    if (!built) return;
    const { url, m } = built;
    if (!window.open(url, '_blank')) saveReportUrl(url, m);
    // Give the new tab time to load the blob before releasing it.
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }

  // Save the report straight to a file — no tab, no pop-up to be blocked.
  function downloadReport() {
    const built = buildReportUrl();
    if (!built) return;
    saveReportUrl(built.url, built.m);
    // The download reads the blob synchronously; release it on the next tick.
    setTimeout(() => URL.revokeObjectURL(built.url), 0);
  }
})();

// ── Sapling grammar check (test panel) ──────────────────────────────────────
// A standalone surface for trying Sapling.ai on a passage. It is deliberately
// apart from the review flow: paste text, one POST to /api/sapling/check, its
// edits back. Nothing is uploaded to a reviewer, saved, or billed through
// DocProof. The Sapling key is admin-set (Admin → Provider API keys); with none
// set the route answers with a message this panel just shows.
(() => {
  const text = $('sap-text');
  const run = $('sap-run');
  if (!text || !run) return;                 // panel not in this build's HTML

  const count = $('sap-count');
  const sync = () => {
    const n = text.value.length;
    count.textContent = `${n.toLocaleString()} character${n === 1 ? '' : 's'}`;
    run.disabled = text.value.trim().length === 0;
  };
  text.addEventListener('input', sync);
  sync();

  run.addEventListener('click', async () => {
    $('sap-error').hidden = true;
    $('sap-results').hidden = true;
    run.disabled = true;
    $('sap-busy').hidden = false;
    try {
      const body = { text: text.value, variety: $('sap-variety').value };
      const d = await api('/api/sapling/check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      renderSapling(text.value, d.edits || []);
    } catch (err) {
      $('sap-error').textContent = err.message;
      $('sap-error').hidden = false;
    } finally {
      $('sap-busy').hidden = true;
      sync();                                 // re-enable per the current text
    }
  });

  // Everything below builds DOM nodes and sets textContent — the submitted text
  // and Sapling's strings are untrusted and must never become markup.
  function renderSapling(source, edits) {
    const summary = $('sap-summary');
    summary.textContent = edits.length
      ? `Sapling flagged ${edits.length} suggestion${edits.length === 1 ? '' : 's'}.`
      : 'Sapling found nothing to change in this text.';

    const list = $('sap-list');
    list.innerHTML = '';
    if (edits.length) list.append(preview(source, edits));
    edits.forEach((e) => list.append(editCard(e)));
    $('sap-results').hidden = false;
  }

  // The passage with each flagged span marked in place. Edits arrive sorted by
  // start; a later edit that opens before the previous one closed (overlapping
  // suggestions) is left unmarked here rather than mangling the offsets — its
  // card below still shows it.
  function preview(source, edits) {
    const box = document.createElement('div');
    box.className = 'sap-preview';
    let at = 0;
    for (const e of edits) {
      if (e.start < at || e.end > source.length || e.start > e.end) continue;
      if (e.start > at) box.append(document.createTextNode(source.slice(at, e.start)));
      const mark = document.createElement('mark');
      mark.className = 'sap-mark';
      mark.textContent = source.slice(e.start, e.end) || '∅';
      mark.title = e.replacement
        ? `${e.error_type || 'suggestion'} → ${e.replacement}`
        : `${e.error_type || 'suggestion'} → (delete)`;
      box.append(mark);
      at = e.end;
    }
    if (at < source.length) box.append(document.createTextNode(source.slice(at)));
    return box;
  }

  function editCard(e) {
    const card = document.createElement('div');
    card.className = 'sap-edit';

    const change = document.createElement('div');
    change.className = 'sap-change';
    const from = document.createElement('span');
    from.className = 'sap-from';
    from.textContent = e.original || '∅';
    const arrow = document.createElement('span');
    arrow.className = 'sap-arrow';
    arrow.textContent = '→';
    const to = document.createElement('span');
    to.className = 'sap-to';
    to.textContent = e.replacement || '(delete)';
    change.append(from, arrow, to);

    const label = e.general_error_type || e.error_type;
    card.append(change);
    if (label) {
      const tag = document.createElement('span');
      tag.className = 'tag sap-tag';
      tag.textContent = label;
      card.append(tag);
    }
    return card;
  }

  // ── run a whole .docx through Sapling → tracked changes ───────────────────
  const docZone = $('sap-doc-zone');
  if (docZone) {
    const docInput = $('sap-doc-input');
    const docName = $('sap-doc-name');
    const docRun = $('sap-doc-run');
    let chosen = null;                        // the picked File
    let lastDoc = null;                       // { blob, filename } for download

    const setFile = (file) => {
      chosen = file || null;
      docName.textContent = file ? file.name : 'Drop a .docx, or click to choose';
      docName.classList.toggle('muted', !file);
      docRun.disabled = !file;
    };
    docZone.addEventListener('click', () => docInput.click());
    docZone.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); docInput.click(); }
    });
    docInput.addEventListener('change', () => setFile(docInput.files[0]));
    ['dragenter', 'dragover'].forEach((evt) =>
      docZone.addEventListener(evt, (e) => {
        e.preventDefault(); docZone.classList.add('hot');
      }));
    ['dragleave', 'drop'].forEach((evt) =>
      docZone.addEventListener(evt, (e) => {
        e.preventDefault(); docZone.classList.remove('hot');
      }));
    docZone.addEventListener('drop', (e) => {
      const f = [...e.dataTransfer.files]
        .find((x) => x.name.toLowerCase().endsWith('.docx'));
      if (f) setFile(f);
    });

    docRun.addEventListener('click', async () => {
      if (!chosen) return;
      $('sap-doc-error').hidden = true;
      $('sap-doc-results').hidden = true;
      docRun.disabled = true;
      $('sap-doc-busy').hidden = false;
      try {
        const body = new FormData();
        body.append('file', chosen);
        body.append('variety', $('sap-variety').value);
        const d = await api('/api/sapling/docx', { method: 'POST', body });
        lastDoc = { blob: b64ToBlob(d.docx_base64), filename: d.filename };
        renderDocResults(d);
      } catch (err) {
        $('sap-doc-error').textContent = err.message;
        $('sap-doc-error').hidden = false;
      } finally {
        $('sap-doc-busy').hidden = true;
        docRun.disabled = !chosen;
      }
    });

    $('sap-doc-download').addEventListener('click', () => {
      if (!lastDoc) return;
      const url = URL.createObjectURL(lastDoc.blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = lastDoc.filename;
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    });

    function renderDocResults(d) {
      $('sap-doc-summary').textContent = d.applied
        ? `Sapling made ${d.applied} tracked change${d.applied === 1 ? '' : 's'} `
          + `across ${d.paragraphs} paragraph${d.paragraphs === 1 ? '' : 's'}.`
        : `Sapling found nothing to change across ${d.paragraphs} `
          + `paragraph${d.paragraphs === 1 ? '' : 's'}.`;
      const list = $('sap-doc-list');
      list.innerHTML = '';
      // Only the changes that actually landed as revisions — a suggestion the
      // document's own text no longer matched is dropped rather than shown as
      // applied.
      (d.edits || []).filter((e) => e.applied).forEach((e) =>
        list.append(editCard(e)));
      $('sap-doc-results').hidden = false;
    }
  }

  // Decode the base64 .docx the server returns into a Blob to download. Kept
  // simple — atob to a byte array, no streaming needed at this panel's size.
  function b64ToBlob(b64) {
    const bytes = atob(b64);
    const arr = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
    return new Blob([arr], {
      type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    });
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
    // Only a review picks sections. An .idml is preflighted for review too, so
    // it carries chunks — but under prep/promo/corrections it is read whole.
    if (f.ok && !isPrep() && !isPromo() && !isCorrections()
        && f.chunks && f.chunks.length > 1) {
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
  if (isCorrections()) {
    if (!f.can_correct) return f.correct_error || 'cannot be corrected.';
    return 'InDesign file — ready to apply corrections';
  }
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
  (f) => canRun(f) && (isPrep() || isPromo() || isCorrections()
                       || keptFor(f).size > 0));

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
  state.effortMultipliers = body.effort_multipliers || state.effortMultipliers;
  state.defaultModel = body.default_model || state.defaultModel;
  state.catalogDefaultModel = body.catalog_default_model
    || state.catalogDefaultModel;
  // Whether a Sapling key is configured — drives Hard's Sapling pass and the
  // Hammer card's missing-key note. Served in the /api/models keys block.
  state.saplingKeyed = !!(body.keys && body.keys.sapling
                          && body.keys.sapling.configured);
  state.defaultGlossaryModel = body.default_glossary_model
    || state.defaultGlossaryModel;
  state.defaultJudgeModel = body.default_judge_model || state.defaultJudgeModel;
  state.defaultMeaningModel = body.default_meaning_model
    || state.defaultMeaningModel;
  state.defaultFixModel = body.default_fix_model || state.defaultFixModel;
  state.defaultChapterContinuityModel = body.default_chapter_continuity_model
    || state.defaultChapterContinuityModel;
  state.defaultContinuityModel = body.default_continuity_model
    || state.defaultContinuityModel;

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
  // Open on the house default when it is usable; otherwise prefer any usable
  // model, but always select something — the cost estimate is useful before a
  // key exists, and a blank dropdown looks broken.
  const usable = (id) => models.some((m) => m.id === id && m.available);
  // The shipped catalog default (gpt-5.6-luna) steps in between the persisted
  // default and the first-usable sweep, so a stale volume value (a legacy
  // Sonnet) that is unusable never wins over the model this build ships with.
  const fallback = (usable(state.defaultModel) && state.defaultModel)
    || (usable(state.catalogDefaultModel) && state.catalogDefaultModel)
    || (models.find((m) => m.available) || models[0] || {}).id
    || '';
  select.value = usable(previous) ? previous : fallback;

  // The glossary reader: the same catalog, plus an "Off" choice, defaulting to
  // the house setting (Opus). Its own read is one whole-book call, so it is a
  // separate pick from the page-by-page reviewer above.
  const gloss = $('glossary-model');
  const gprev = gloss.value;
  gloss.innerHTML = '';
  models.forEach((m) => {
    const opt = document.createElement('option');
    opt.value = m.id;
    opt.textContent = m.available ? m.display : `${m.display} — add a key first`;
    opt.disabled = !m.available;
    gloss.append(opt);
  });
  const off = document.createElement('option');
  off.value = 'off';
  off.textContent = 'Off — skip the glossary read';
  gloss.append(off);
  const gdefault = (usable(state.defaultGlossaryModel) && state.defaultGlossaryModel)
    || 'off';
  gloss.value = usable(gprev) || gprev === 'off' ? gprev : gdefault;

  // The between-round judge (multi-round review): the same catalog, defaulting to
  // the house judge model. Only submitted when the run is 2+ rounds, but kept
  // populated so the picker is ready the moment the judge field is revealed.
  const judge = $('judge-model');
  if (judge) {
    const jprev = judge.value;
    judge.innerHTML = '';
    models.forEach((m) => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.available ? m.display : `${m.display} — add a key first`;
      opt.disabled = !m.available;
      judge.append(opt);
    });
    const jdefault = (usable(state.defaultJudgeModel) && state.defaultJudgeModel)
      || (models.find((m) => m.available) || models[0] || {}).id
      || '';
    judge.value = usable(jprev) ? jprev : jdefault;
  }

  // The meaning gate's judge: the same catalog again, defaulting to the house
  // choice (a frontier model — it is the last reader before the author, and it
  // makes one short call per paragraph that has changes, not one per chunk).
  const meaning = $('meaning-model');
  if (meaning) {
    const mprev = meaning.value;
    meaning.innerHTML = '';
    models.forEach((m) => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.available ? m.display : `${m.display} — add a key first`;
      opt.disabled = !m.available;
      meaning.append(opt);
    });
    const mdefault = (usable(state.defaultMeaningModel) && state.defaultMeaningModel)
      || (models.find((m) => m.available) || models[0] || {}).id
      || '';
    meaning.value = usable(mprev) ? mprev : mdefault;
  }

  // The fix gate's judge: the same catalog once more. It is a separate pass from
  // the meaning gate and can run on a different model, or on its own.
  const fixSel = $('fix-model');
  if (fixSel) {
    const fprev = fixSel.value;
    fixSel.innerHTML = '';
    models.forEach((m) => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.available ? m.display : `${m.display} — add a key first`;
      opt.disabled = !m.available;
      fixSel.append(opt);
    });
    const fdefault = (usable(state.defaultFixModel) && state.defaultFixModel)
      || (models.find((m) => m.available) || models[0] || {}).id
      || '';
    fixSel.value = usable(fprev) ? fprev : fdefault;
  }

  // The chapter-continuity reader (which also judges its own finds): the same
  // catalog, defaulting to the house continuity model. The reader is the limit
  // for this pass, so a strong model here is where the recall comes from.
  const chapterCont = $('chapter-continuity-model');
  if (chapterCont) {
    const cprev = chapterCont.value;
    chapterCont.innerHTML = '';
    models.forEach((m) => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.available ? m.display : `${m.display} — add a key first`;
      opt.disabled = !m.available;
      chapterCont.append(opt);
    });
    const cdefault = (usable(state.defaultChapterContinuityModel)
                      && state.defaultChapterContinuityModel)
      || (models.find((m) => m.available) || models[0] || {}).id
      || '';
    chapterCont.value = usable(cprev) ? cprev : cdefault;
  }

  // The whole-book continuity reader: the same catalog, defaulting to the house
  // continuity model (now the reviewer). A pick opts into a frontier whole-book
  // read without needing a key for any other pass.
  const wholeCont = $('continuity-model');
  if (wholeCont) {
    const wprev = wholeCont.value;
    wholeCont.innerHTML = '';
    models.forEach((m) => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.available ? m.display : `${m.display} — add a key first`;
      opt.disabled = !m.available;
      wholeCont.append(opt);
    });
    const wdefault = (usable(state.defaultContinuityModel)
                      && state.defaultContinuityModel)
      || (models.find((m) => m.available) || models[0] || {}).id
      || '';
    wholeCont.value = usable(wprev) ? wprev : wdefault;
  }
  renderCost();
}

// A governed control changing may move the run off the selected tier and onto
// "Custom", so these repaint the tier highlight as well as the price.
$('model').addEventListener('change', () => { renderCost(); reEvaluateTier(); });
$('glossary-model').addEventListener('change',
  () => { renderCost(); reEvaluateTier(); });
if ($('meaning-model')) $('meaning-model').addEventListener('change', renderCost);
if ($('fix-model')) $('fix-model').addEventListener('change', renderCost);
if ($('chapter-continuity-model'))
  $('chapter-continuity-model').addEventListener('change', renderCost);
if ($('continuity-model'))
  $('continuity-model').addEventListener('change', renderCost);
// The sensitivity dial is not a tier control and does not move the price; it only
// names the level it is on. 1 Cautious … 5 Exhaustive, matching the datalist.
if ($('chapter-continuity-sensitivity')) {
  const CC_SENS = { 1: 'Cautious', 2: 'Measured', 3: 'Thorough',
                    4: 'Searching', 5: 'Exhaustive' };
  const sens = $('chapter-continuity-sensitivity');
  const nm = $('chapter-continuity-sensitivity-name');
  const render = () => { if (nm) nm.textContent = CC_SENS[Number(sens.value)] || ''; };
  sens.addEventListener('input', render);
  render();
}
if ($('judge-model')) $('judge-model').addEventListener('change',
  () => { renderCost(); reEvaluateTier(); });
if ($('confidence')) $('confidence').addEventListener('change', reEvaluateTier);
// Continuity-only is not a tier control, but priceReview short-circuits on it
// (it skips every detector pass), so toggling it must reprice the sticky
// estimate — otherwise the price shown and the run billed disagree.
if ($('continuity-only')) $('continuity-only').addEventListener('change', renderCost);
$('rounds').addEventListener('change',
  () => { syncRounds(); renderCost(); reEvaluateTier(); });

// ── passes & features ─────────────────────────────────────────────────────
// The submission panel's switches, one per togglable pass, grouped and each
// opening at what the pipeline does today (the server sends that as `default`).
// The catalogue is the server's — this only renders it — so the panel can never
// offer a switch the config cannot honour. glossary is deliberately not here:
// the "First-pass glossary reader" picker above already carries its on/off.
const FEATURE_GROUPS = [
  ['pass', 'Passes'],
  ['output', 'What it writes'],
  ['safety', 'Safety nets — leave on unless you know why'],
];

async function loadFeatures() {
  try {
    const body = await api('/api/features');
    state.features = body.features || [];
    state.categories = body.categories || [];
    if (body.rounds) {
      // Prefill the rounds default and the judge-prompt placeholder. The
      // placeholder (not the value) carries the built-in default, so an
      // untouched submit stays empty and the engine falls back to it.
      if ($('rounds')) $('rounds').value = String(body.rounds.default || 1);
      if ($('judge-prompt')) {
        $('judge-prompt').placeholder = body.rounds.judge_prompt_default || '';
      }
      // Same idea for the continuity reader's prompt: the built-in default is
      // the placeholder, so an untouched submit stays empty and the engine
      // falls back to it.
      if ($('continuity-prompt') && body.continuity) {
        $('continuity-prompt').placeholder = body.continuity.prompt_default || '';
      }
      // The chapter-scoped reader's prompt, the same way.
      if ($('chapter-continuity-prompt') && body.chapter_continuity) {
        $('chapter-continuity-prompt').placeholder =
          body.chapter_continuity.prompt_default || '';
      }
      // And the meaning gate's, which reveals itself when its switch goes on.
      if ($('meaning-prompt') && body.meaning) {
        $('meaning-prompt').placeholder = body.meaning.prompt_default || '';
      }
      if ($('fix-prompt') && body.fix) {
        $('fix-prompt').placeholder = body.fix.prompt_default || '';
      }
      syncRounds();
    }
  } catch (_) {
    state.features = [];               // panel stays empty; the review still runs
    state.categories = [];
  }
  renderFeatures();
  renderCategoryKnobs();
}

function syncRounds() {
  // The judge model (Models tab) and instructions (Passes tab) are review-only
  // and only matter with 2+ rounds; reveal both only then, and only on a review
  // — otherwise a rounds value left ≥2 by a prior tier would re-show them in the
  // prep/promo flat drawer, which the .review-only sweep had just hidden.
  const show = kind() === 'review' && Number(($('rounds') || {}).value || 1) >= 2;
  const judge = $('judge-field');
  if (judge) judge.hidden = !show;
  const judgeModel = $('judge-model-field');
  if (judgeModel) judgeModel.hidden = !show;
}

// A judge gate's model (Models tab) and instructions (Passes tab) only matter
// when its switch is on, so each follows its switch the way the judge field
// follows the rounds. The switch may live in any of the three feature hosts, so
// the selector is scoped to the shared .features class, not one host id.
function syncJudgeGates() {
  // These fields are review-only, so keep them hidden for prep/promo even when
  // a gate switch was left on by a prior Hard/Hammer pick — otherwise they would
  // re-appear in the prep/promo flat drawer the .review-only sweep just cleared.
  const review = kind() === 'review';
  [['meaning-field', 'meaning_check'], ['fix-field', 'fix_check'],
   ['meaning-model-field', 'meaning_check'], ['fix-model-field', 'fix_check']]
    .forEach(([id, feature]) => {
      const field = $(id);
      if (!field) return;
      const sw = document.querySelector(
        `.features input[data-feature="${feature}"]`);
      field.hidden = !(review && sw && sw.checked);
    });
}

// The independent judge is intentionally synchronous in Phase 1B. Keep the
// timing control responsive to its switch instead of letting the user reach
// Start with a combination the API must reject. Turning it off restores the
// normal overnight choice immediately.
function syncExaminationJudgment() {
  const sw = document.querySelector(
    '.features input[data-feature="examination_judgment"]');
  const graph = document.querySelector(
    '.features input[data-feature="examination_graph"]');
  const batch = document.querySelector('input[name="mode"][value="batch"]');
  const now = document.querySelector('input[name="mode"][value="now"]');
  const rounds = $('rounds');
  if (!batch || !now) return;
  const active = kind() === 'review' && !!(sw && sw.checked);
  if (graph) {
    const graphRow = graph.closest('label');
    if (active) {
      if (!graph.disabled) graph.dataset.beforeJudgment = graph.checked ? '1' : '0';
      graph.checked = true;
      graph.disabled = true;
      if (graphRow) graphRow.title =
        'The shadow coverage ledger is required while independent judgment is on.';
    } else if (graph.disabled) {
      graph.disabled = false;
      if (graph.dataset.beforeJudgment) {
        graph.checked = graph.dataset.beforeJudgment === '1';
        delete graph.dataset.beforeJudgment;
      }
      if (graphRow) graphRow.title = '';
    }
  }
  batch.disabled = active;
  const batchLabel = batch.closest('label');
  if (batchLabel) {
    batchLabel.classList.toggle('choice-disabled', active);
    batchLabel.title = active
      ? 'The independent examination experiment runs right now only.' : '';
  }
  if (active && !now.checked) {
    now.checked = true;
    $('schedule-wrap').hidden = true;
  }
  if (rounds) {
    if (active) {
      if (!rounds.disabled) rounds.dataset.beforeExamination = rounds.value;
      rounds.value = '1';
      rounds.disabled = true;
      syncRounds();
    } else if (!active && rounds.disabled) {
      rounds.disabled = false;
      if (rounds.dataset.beforeExamination) {
        rounds.value = rounds.dataset.beforeExamination;
        delete rounds.dataset.beforeExamination;
      }
      syncRounds();
    }
  }
}

// Each group renders into its own tab's host: the passes under Passes, the
// output switches under Output, the safety nets under Safety. collectFeatures()
// reads them back across all three via the shared .features class.
const FEATURE_HOSTS = { pass: 'features-groups', output: 'features-output',
                        safety: 'features-safety' };

function renderFeatures() {
  Object.values(FEATURE_HOSTS).forEach((id) => {
    const h = $(id);
    if (h) h.innerHTML = '';
  });
  FEATURE_GROUPS.forEach(([group, title]) => {
    const host = $(FEATURE_HOSTS[group]);
    if (!host) return;
    const items = state.features.filter((f) => f.group === group);
    if (!items.length) return;
    const section = document.createElement('div');
    section.className = 'feature-group';
    const head = document.createElement('div');
    head.className = 'feature-group-title';
    head.textContent = title;
    section.append(head);
    items.forEach((f) => section.append(featureRow(f)));
    host.append(section);
  });
  syncJudgeGates();
  // Handle the boot race where the switches render only now, after a file was
  // staged: a tier already chosen re-asserts its switch values; if none was
  // chosen yet (an earlier maybeInitTier bailed because the switches were not
  // rendered), open on Standard now that they are.
  if (state.tier && state.tier !== 'custom' && state.presets[state.tier]) {
    applyPresetSwitches(state.tier);
  } else if (state.tier === null) {
    maybeInitTier();
  }
  syncExaminationJudgment();
  renderCost();
}

function featureRow(f) {
  const row = document.createElement('label');
  row.className = 'switch' + (f.group === 'safety' ? ' switch-safety' : '');
  const input = document.createElement('input');
  input.type = 'checkbox';
  input.setAttribute('role', 'switch');
  input.dataset.feature = f.id;
  if (f.heavy) input.dataset.heavy = '1';
  input.checked = !!f.default;
  const track = document.createElement('span');
  track.className = 'track';
  const thumb = document.createElement('span');
  thumb.className = 'thumb';
  track.append(thumb);
  const text = document.createElement('span');
  text.className = 'switch-text';
  const name = document.createElement('span');
  name.className = 'switch-label';
  name.textContent = f.label;
  const blurb = document.createElement('small');
  blurb.className = 'muted';
  blurb.textContent = f.blurb;
  text.append(name);
  if (f.id === 'examination_judgment' && f.cost) {
    const badge = document.createElement('span');
    badge.className = 'experiment-badge';
    badge.textContent = `Admin experiment · one round now · up to $${Number(
      f.cost.max_usd || 0).toFixed(2)} per manuscript`;
    text.append(badge);
  }
  text.append(blurb);
  input.addEventListener('change', () => {
    if (f.id === 'meaning_check' || f.id === 'fix_check') syncJudgeGates();
    if (f.id === 'examination_judgment' || f.id === 'examination_graph') {
      syncExaminationJudgment();
    }
    renderCost();
    reEvaluateTier();
  });
  row.append(input, track, text);
  return row;
}

// The {id: on} map the run sends. Reads the live switch state, so a toggle the
// user never touched is sent at its default — a harmless no-op on the server.
function collectFeatures() {
  const out = {};
  document.querySelectorAll('.features input[data-feature]')
    .forEach((el) => {
      out[el.dataset.feature] = el.dataset.feature === 'examination_judgment'
        ? el.checked && kind() === 'review' : el.checked;
    });
  return out;
}

// The per-category tuning rows: each defined category gets a "reads" and a
// "chunk" number input, pre-filled by placeholder with the shipped default so a
// blank field means "leave it". collectCategoryKnobs() reads only the fields the
// user actually filled, so an untouched panel sends {}.
function renderCategoryKnobs() {
  const host = $('category-knobs');
  if (!host) return;
  host.innerHTML = '';
  const cats = state.categories || [];
  const field = $('category-knobs-field');
  if (field) field.hidden = !cats.length;
  cats.forEach((c) => {
    const row = document.createElement('div');
    row.className = 'knob-row';
    row.dataset.categoryId = c.id;
    const label = document.createElement('span');
    label.className = 'knob-label';
    label.textContent = (c.names || c.keys).join(', ');
    row.append(
      label,
      knobInput('reads', 'knob-passes', c.passes,
                `Reads for ${label.textContent}`),
      knobInput('chunk size', 'knob-budget',
                c.token_budget || c.default_token_budget,
                `Chunk size for ${label.textContent}`, 'tokens'));
    host.append(row);
  });
}

// One captioned number input for a knob row. Its placeholder carries the shipped
// default, so a field left blank sends nothing and keeps that default — the grey
// number the user sees is exactly what they'd get. `unit`, when given, prints a
// small suffix beside the field so a bare "2500" reads as tokens, not a count.
// Repricing on `input` (not `change`) so the estimate moves as the user types.
function knobInput(caption, cls, placeholder, aria, unit) {
  const wrap = document.createElement('label');
  wrap.className = 'knob-input';
  const tag = document.createElement('small');
  tag.className = 'muted';
  tag.textContent = caption;
  const input = document.createElement('input');
  input.type = 'number';
  input.min = '1';
  input.className = cls;
  input.placeholder = String(placeholder);
  input.setAttribute('aria-label', aria);
  input.addEventListener('input', renderCost);
  wrap.append(tag);
  if (unit) {
    const box = document.createElement('span');
    box.className = 'knob-field';
    const u = document.createElement('small');
    u.className = 'muted knob-unit';
    u.textContent = unit;
    box.append(input, u);
    wrap.append(box);
  } else {
    wrap.append(input);
  }
  return wrap;
}

// The {category_id: {passes?, token_budget?}} map the run sends. Reads only the
// fields the user filled (a blank keeps the shipped default), so an untouched
// panel sends {} — a no-op the server leaves alone.
function collectCategoryKnobs() {
  const out = {};
  document.querySelectorAll('#category-knobs .knob-row').forEach((row) => {
    const knob = {};
    const passes = row.querySelector('.knob-passes').value.trim();
    const budget = row.querySelector('.knob-budget').value.trim();
    if (passes !== '') knob.passes = Number(passes);
    if (budget !== '') knob.token_budget = Number(budget);
    if (Object.keys(knob).length) out[row.dataset.categoryId] = knob;
  });
  return out;
}


// ── reasoning effort ──────────────────────────────────────────────────────
// A 1-based slider over these, cheapest → deepest, mirroring EFFORT_LEVELS on
// the server. Low is the shipped default: grammar detection is precise, so a
// low setting is both cheaper and no less accurate.
const EFFORT_LEVELS = ['low', 'medium', 'high', 'xhigh', 'max'];
const EFFORT_NAME = { low: 'Low', medium: 'Medium', high: 'High',
                      xhigh: 'Very high', max: 'Max' };
const EFFORT_BLURB = {
  low: 'Cheapest, and all that grammar detection needs.',
  medium: 'A little more deliberation for style-sensitive passes.',
  high: 'Slower and dearer — for the hardest judgment calls.',
  xhigh: 'Deeper still; rarely worth it for proofreading.',
  max: 'The deepest the model offers, and the most expensive.',
};

function effortValue() {
  return EFFORT_LEVELS[Number($('effort').value) - 1] || 'low';
}

function setEffort(level) {
  const i = EFFORT_LEVELS.indexOf(level);
  $('effort').value = String((i < 0 ? 0 : i) + 1);
  renderEffort();
}

function renderEffort() {
  const level = effortValue();
  $('effort-name').textContent = EFFORT_NAME[level];
  $('effort-blurb').textContent = EFFORT_BLURB[level];
}

// Both the description and the price move with the dial: a deeper effort costs
// more, and the estimate must say so as the slider slides.
// Effort is a tier-governed control (a tier sets it, currentMatchesTier compares
// it), so a drag reprices AND may move the run off the selected tier onto Custom.
$('effort').addEventListener('input',
  () => { renderEffort(); renderCost(); reEvaluateTier(); });
// The oversize override gates the promo run — re-price and re-check when it flips.
$('promo-oversize-ok').addEventListener('change', renderCost);
// Releasing the slider makes this the saved default. Fire-and-forget: on the
// web build only an administrator may change shared defaults, and the 403 there
// is harmless — the value still rides along with the job below either way.
$('effort').addEventListener('change', () => {
  api('/api/settings', {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ effort: effortValue() }),
  }).catch(() => {});
});
renderEffort();

// ── dark-mode toggle ───────────────────────────────────────────────────────
// The <head> already applied any saved theme before paint; here we only sync
// the switch to what's in effect and persist the user's choice. With no saved
// value the OS preference decides, so the switch reflects that via matchMedia.
(function themeToggle() {
  const box = $('theme-toggle');
  if (!box) return;
  const saved = (() => {
    try { return localStorage.getItem('docproof-theme'); } catch (e) { return null; }
  })();
  const prefersDark = window.matchMedia
    && window.matchMedia('(prefers-color-scheme: dark)').matches;
  box.checked = saved ? saved === 'dark' : !!prefersDark;
  box.addEventListener('change', () => {
    const theme = box.checked ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('docproof-theme', theme); } catch (e) { /* private mode */ }
  });
})();

// What the picked sections would cost on this model. The server prices the
// whole document; once sections are deselected only the client knows what is
// left, so it re-does the same arithmetic with the rates the server sent.
// The reasoning dial changes only what the model writes back, so it scales the
// output half of the estimate and never the input. Mirrors effort_multiplier
// on the server: models that ignore effort are never scaled.
function effortFactor(m, effort) {
  if (!m || !m.supports_effort) return 1;
  return state.effortMultipliers[effort] || 1;
}

// Output the model writes scales with whether it must explain each change: the
// explanations are most of what it emits, so turning them off drops the output
// roughly to the applied changes alone. A rough factor.
const EXPLANATIONS_OFF_OUTPUT = 0.35;
// A whole-book read (story sheet, glossary) answers with a compact structured
// summary, not prose, so its output is small next to the book it reads — the
// input dominates and a flat output guess is close enough.
const READ_OUTPUT_TOKENS = 2000;
// LanguageTool's confirm calls scale with how many candidates the book throws,
// which isn't known before the run, so price it as a small share of one read.
const LT_CONFIRM_SHARE = 0.15;
// The meaning gate reads one short call per paragraph that has a change: the
// paragraph, the changes, and its instructions on every call. How many
// paragraphs that is depends on what the book throws, so — like the confirm
// calls above — it is priced as a share of one read, a little larger because
// its instructions repeat per call and it usually runs on a dearer model.
const MEANING_SHARE = 0.25;

function modelById(id) {
  return state.models.find((x) => x.id === id) || null;
}

// The whole book once — every chunk, not the per-pass multiple. Only the
// whole-document passes read it, and only when the file is fully selected.
function bookTokensFor(f) {
  return (f.chunks || []).reduce((n, c) => n + c.est_tokens, 0);
}

// The extra batched cost the per-category knobs add to one file, over the flat
// per-pass base priceReview already charged. For each category the user tuned,
// its (reads, chunk size) is compared against the shipped default and only the
// difference is billed: extra reads resend `keptTok` input and `keptCount`
// requests; a smaller chunk multiplies that category's requests by
// default÷chunk (the same text, split finer). A category left at its defaults —
// and an untouched panel ({} or undefined) — contributes 0, so the base is
// unchanged until a knob is actually moved. `knobs` is collectCategoryKnobs()'s
// {category_id: {passes?, token_budget?}} map.
function categoryKnobCost(knobs, keptTok, keptCount, m, effort, outFactor) {
  if (!knobs || !keptCount) return 0;
  const byId = {};
  (state.categories || []).forEach((c) => { byId[c.id] = c; });
  let extraIn = 0, extraReq = 0;
  Object.keys(knobs).forEach((id) => {
    const c = byId[id];
    if (!c) return;                                   // stale id: ignore
    const knob = knobs[id];
    const glob = c.default_token_budget || 1;         // the global chunk budget
    const defBudget = c.token_budget || glob;         // this category's default
    const defReads = c.passes || 1;
    const newBudget = knob.token_budget || defBudget;
    const newReads = knob.passes || defReads;
    // Requests scale inversely with chunk size (smaller chunk → more requests);
    // input tokens (the text itself) do not, so only extra reads add input.
    const oldReq = defReads * keptCount * (glob / defBudget);
    const newReq = newReads * keptCount * (glob / newBudget);
    extraIn += keptTok * (newReads - defReads);
    extraReq += newReq - oldReq;
  });
  return (extraIn * m.input_per_mtok
    + extraReq * state.outputGuess * m.output_per_mtok
      * effortFactor(m, effort) * outFactor) / 1e6;
}

// The review estimate. Pure over an explicit settings bundle plus the staged
// files, so the same arithmetic prices the live controls (via bundleFromControls)
// and each effort-tier chip (via priceBundle) — one code path, no drift. The
// per-chunk detector passes and the rewrite retype ride the overnight batch (its
// discount); the whole-book reads, the confirm calls, the meaning/fix gates and
// the between-round judge are synchronous — full price in both columns. `approx`
// is set when a pass whose size the book decides (rewrite, LanguageTool, the
// gates, extra rounds) is on, so the panel can say the figure is rough.
//
// bundle: { model:<model object>, effort, glossary_model, rounds, judge_model,
//   meaning_model, fix_model, features:{id:bool,...}, continuity_only,
//   min_confidence, mode }.  files: filesToRun() with each file's kept Set
//   resolved (see stagedReviewFiles).  min_confidence is carried but never
//   priced — confidence does not move the estimate.
function priceReview(bundle, files) {
  const m = bundle.model;
  if (!m) return { now: null, batch: null, approx: false };

  // Continuity-only: the run skips every detector pass and does just the
  // whole-book contradiction read, so the estimate is that one read and nothing
  // else — no per-chunk work, no gates, no rounds multiply, no Sapling.
  if (bundle.continuity_only) {
    let full = 0, any = false;
    files.forEach((f) => {
      const chunks = f.chunks || [];
      if (!chunks.length || f.kept.size !== chunks.length) return;   // whole file only
      const spec = state.features.find((s) => s.id === 'continuity');
      const cm = modelById(spec && spec.cost && spec.cost.model) || m;
      const book = bookTokensFor(f);
      full += (book * cm.input_per_mtok
        + READ_OUTPUT_TOKENS * cm.output_per_mtok) / 1e6;
      any = true;
    });
    return any ? { now: full, batch: full, approx: false }
               : { now: null, batch: null, approx: false };
  }

  const feats = bundle.features;
  const glossaryId = bundle.glossary_model;
  const outFactor = feats.report_explanations === false
    ? EXPLANATIONS_OFF_OUTPUT : 1;
  let batched = 0;                 // rides the batch discount
  let full = 0;                    // synchronous, full price both columns
  let flat = 0;                    // per-run, not per-round (e.g. Sapling)
  let reviewedIn = 0;              // input tokens reviewed once — the judge base
  let approx = false;
  let any = false;

  files.forEach((f) => {
    const kept = f.kept;
    // The staged-file preflight counts the shipped categories. Detector-only
    // removes the terminal-mark query category, leaving seven edit-capable
    // focused reads. Price the card for what the server profile will run.
    const passes = bundle.profile === 'candidate-only' ? 0
      : (bundle.profile === 'detector-only' ? 7 : (f.passes || 1));
    const chunks = f.chunks || [];
    let keptTok = 0, keptCount = 0;
    chunks.forEach((c) => {
      if (!kept.has(c.chunk_id)) return;
      keptTok += c.est_tokens;
      keptCount += 1;
    });
    // The flat base: every default category reads the kept text once, so the
    // whole review is `passes` reads of it (passes = the file's category count).
    const inTok = keptTok * passes;
    const reqs = keptCount * passes;
    if (reqs) any = true;
    batched += (inTok * m.input_per_mtok
      + reqs * state.outputGuess * m.output_per_mtok
        * effortFactor(m, bundle.effort) * outFactor) / 1e6;
    // Per-category tuning rides on top of that base as a delta: an extra read
    // resends the kept text and its requests, a tighter chunk splits the same
    // text into more requests (more output). Priced against each category's
    // shipped default, so an untouched category — and the whole default panel —
    // adds exactly nothing.
    if (passes > 0) {
      batched += categoryKnobCost(bundle.category_knobs, keptTok, keptCount,
                                  m, bundle.effort, outFactor);
    }
    reviewedIn += keptTok;   // one read's worth — the judge base, knobs aside

    // Packet judges stop before their configured ceilings, but exact eligible
    // site counts are known only after the manuscript has been examined. Show
    // the defensible worst case: one cap per enabled detector per manuscript.
    state.features.forEach((spec) => {
      if (feats[spec.id] !== true || !spec.cost
          || spec.cost.kind !== 'budget_cap') return;
      flat += Number(spec.cost.max_usd || 0);
      approx = true;
      any = true;
    });

    // The meaning/fix gates are priced before the whole-document guard below,
    // because unlike those passes they read whatever changes a run produces —
    // including on a partial selection. Each enabled gate is its own pass over
    // the same changes, so two gates on is two bills.
    [['meaning_check', bundle.meaning_model],
     ['fix_check', bundle.fix_model]].forEach(([id, pickModel]) => {
      if (feats[id] !== true) return;
      const spec = state.features.find((s) => s.id === id);
      const jm = modelById(pickModel)
        || modelById(spec && spec.cost && spec.cost.model) || m;
      full += MEANING_SHARE * (inTok / (passes || 1))
        * (jm.input_per_mtok + jm.output_per_mtok) / 1e6;
      approx = true;
      any = true;
    });

    // The whole-document passes run only when the file is reviewed whole.
    if (kept.size !== chunks.length || !chunks.length) return;
    const book = bookTokensFor(f);

    // glossary is priced from its own reader picker, not a switch.
    if (glossaryId && glossaryId !== 'off') {
      const gm = modelById(glossaryId) || m;
      full += (book * gm.input_per_mtok
        + READ_OUTPUT_TOKENS * gm.output_per_mtok) / 1e6;
      any = true;
    }
    // The switch-driven passes carry their pricing model from /api/features; a
    // null model means the pass runs on the detector's own model.
    state.features.forEach((spec) => {
      if (!spec.cost || feats[spec.id] !== true) return;
      const pm = modelById(spec.cost.model) || m;
      any = true;
      if (spec.cost.kind === 'read') {
        full += (book * pm.input_per_mtok
          + READ_OUTPUT_TOKENS * pm.output_per_mtok) / 1e6;
      } else if (spec.cost.kind === 'retype') {
        const s = spec.cost.samples || 1;
        batched += s * (book * pm.input_per_mtok
          + book * pm.output_per_mtok * effortFactor(pm, bundle.effort)) / 1e6;
        approx = true;
      } else if (spec.cost.kind === 'confirm') {
        full += LT_CONFIRM_SHARE * book
          * (pm.input_per_mtok + pm.output_per_mtok) / 1e6;
        approx = true;
      } else if (spec.cost.kind === 'judge') {
        // Already priced above the whole-document guard — it is the one paid
        // pass that also runs on a partial selection.
      } else if (spec.cost.kind === 'grammar') {
        // Sapling bills per character of the manuscript, not per token, and
        // runs once — regardless of model, rounds, or the batch discount — so
        // it lands in `flat`, added after the per-round multiply below.
        flat += (f.chars || 0) * (spec.cost.rate_per_1k || 0) / 1000;
      }
    });
  });

  if (!any) return { now: null, batch: null, approx: false };

  // Multi-round review runs the whole review once per round, so scale the
  // per-round cost by the count. The between-round judge is separate: it runs
  // once per gap between rounds (rounds − 1 times), reading each round's
  // corrections on its own — usually stronger — model. Priced like the gates
  // (findings-scaled off the reviewed text), full price both columns, added
  // outside the per-round multiply.
  const rounds = bundle.rounds || 1;
  let judgeTotal = 0;
  if (rounds > 1 && bundle.judge_model) {
    const jm = modelById(bundle.judge_model) || m;
    judgeTotal = MEANING_SHARE * reviewedIn
      * (jm.input_per_mtok + jm.output_per_mtok) / 1e6 * (rounds - 1);
    approx = true;
  }
  return {
    now: (batched + full) * rounds + judgeTotal + flat,
    batch: (batched * (m.batch_discount || 1) + full) * rounds
      + judgeTotal + flat,
    approx: approx || rounds > 1,
  };
}

// Read the live controls into a priceReview bundle. renderCost uses this for the
// current-controls (Custom) price; the tier chips build their own bundles.
function bundleFromControls() {
  const active = resolveTier(state.tier);
  return {
    model: modelById($('model').value),
    effort: effortValue(),
    glossary_model: $('glossary-model').value,
    rounds: Number(($('rounds') || {}).value || 1),
    judge_model: ($('judge-model') || {}).value || null,
    meaning_model: ($('meaning-model') || {}).value || null,
    fix_model: ($('fix-model') || {}).value || null,
    features: collectFeatures(),
    category_knobs: collectCategoryKnobs(),
    continuity_only: !!(($('continuity-only') || {}).checked),
    min_confidence: ($('confidence') || {}).value,
    mode: mode(),
    profile: (active && active.profile) || '',
  };
}

// The staged review files with each file's kept-section Set resolved once, so
// priceReview stays pure (it reads f.kept, never the selection Map).
function stagedReviewFiles() {
  return filesToRun().map((f) => ({ ...f, kept: keptFor(f) }));
}

// ── effort tiers ────────────────────────────────────────────────────────────
// A tier is a client-side macro over the controls above: selecting one sets the
// reviewer, effort, glossary, rounds, the judge/meaning/fix pickers and the pass
// switches to a bundle served by /api/presets, then reprices. Any later manual
// change flips the selection to the implicit "Custom" state. The job payload is
// unchanged — the tier only pre-sets controls the Start handler already reads.

// The reviewer is gpt-5.6-luna at every tier by product decision: depth comes
// from effort, rounds and passes, never a dearer detector.
const REVIEWER = 'gpt-5.6-luna';
const TIER_ORDER = ['detector', 'candidate', 'light', 'standard', 'hard', 'hammer'];
// Display names for the job-card badge (a review carries the tier it ran at).
const TIER_LABELS = { detector: 'Detector only', candidate: 'Candidate detector',
                      light: 'Light touch',
                      standard: 'Standard', hard: 'Hard',
                      hammer: 'The Hammer', custom: 'Custom' };

// A model the picker can actually use — in the catalog and keyed.
function modelUsable(id) {
  return state.models.some((m) => m.id === id && m.available);
}

// Fall a bundle model back to something usable, remembering the substitution so
// the card can say so. Reviewer/glossary/judge are the only realistic misses
// (Opus with no Anthropic key); Luna is the house reviewer and normally keyed.
function resolveTierModel(preferred, subs, role) {
  if (!preferred || preferred === 'off') return preferred || null;
  if (modelUsable(preferred)) return preferred;
  const fb = modelUsable(REVIEWER) ? REVIEWER
    : ((state.models.find((m) => m.available) || {}).id || preferred);
  if (fb !== preferred) subs.push({ role, wanted: preferred, used: fb });
  return fb;
}

// The concrete values a tier resolves to right now, against live key state.
// The single source both applyPreset and currentMatchesTier read, so applying a
// tier never immediately reads back as "Custom". Returns null for an unknown id
// (e.g. /api/presets not loaded yet).
function resolveTier(tierId) {
  const p = state.presets && state.presets[tierId];
  if (!p) return null;
  const c = p.controls || {};
  const subs = [];
  const rounds = c.rounds || 1;
  // Sapling is a policy: "off" never; "always" unconditional; "if_keyed" only
  // when a key is configured. Hammer ("always") notes the graceful skip when
  // unkeyed; Hard ("if_keyed") silently leaves it off.
  const sapling = p.sapling === 'always'
    || (p.sapling === 'if_keyed' && state.saplingKeyed);
  const saplingMissing = p.sapling === 'always' && !state.saplingKeyed;
  return {
    tierId,
    model: resolveTierModel(REVIEWER, subs, 'reviewer'),
    effort: c.effort,
    glossary_model: resolveTierModel(c.glossary_model, subs, 'glossary'),
    rounds,
    judge_model: rounds > 1
      ? resolveTierModel(c.judge_model, subs, 'judge')
      : (c.judge_model || null),
    meaning_model: c.meaning_model || null,   // null => server default (frontier)
    fix_model: c.fix_model || null,
    min_confidence: c.min_confidence || 'medium',
    profile: c.profile || '',
    features: Object.assign({}, p.features, { sapling }),
    subs,
    saplingMissing,
  };
}

// A resolved tier as a priceReview bundle: model is a catalog object; the gate
// models fall back to the house defaults so the chip prices what the server
// would actually run. The house default is now the reviewer (gpt-5.6-luna);
// Hard/Hammer pin a frontier judge (claude-fable-5) explicitly instead.
function priceBundle(resolved) {
  return {
    model: modelById(resolved.model),
    effort: resolved.effort,
    glossary_model: resolved.glossary_model,
    rounds: resolved.rounds,
    judge_model: resolved.judge_model,
    meaning_model: resolved.meaning_model || state.defaultMeaningModel,
    fix_model: resolved.fix_model || state.defaultFixModel,
    features: resolved.features,
    continuity_only: false,
    min_confidence: resolved.min_confidence,
    mode: mode(),
    profile: resolved.profile || '',
  };
}

// Set only the pass switches a tier governs. Split out so renderFeatures can
// re-assert them if the switches render after a tier was already chosen.
function applyPresetSwitches(tierId) {
  const b = resolveTier(tierId);
  if (!b) return;
  Object.entries(b.features).forEach(([fid, on]) => {
    const sw = document.querySelector(`.features input[data-feature="${fid}"]`);
    if (sw) sw.checked = !!on;                 // programmatic — fires no event
  });
  syncJudgeGates();
}

// Apply a tier to every governed control, then reprice and repaint. All sets are
// programmatic (no change events fire), so this never re-enters reEvaluateTier.
function applyPreset(tierId) {
  const b = resolveTier(tierId);
  if (!b) return;
  if ($('model')) $('model').value = b.model;
  setEffort(b.effort);
  if ($('glossary-model')) $('glossary-model').value = b.glossary_model;
  if ($('rounds')) $('rounds').value = String(b.rounds);
  syncRounds();
  if (b.judge_model && $('judge-model')) $('judge-model').value = b.judge_model;
  if ($('confidence')) $('confidence').value = b.min_confidence;
  applyPresetSwitches(tierId);
  // The obvious full-novel path is overnight batch pricing. A small smoke test
  // may still be switched back to Run now after the card is selected.
  if (b.profile === 'detector-only') {
    const batch = document.querySelector('input[name="mode"][value="batch"]');
    if (batch && !batch.disabled) {
      batch.checked = true;
      $('schedule-wrap').hidden = false;
    }
  }
  syncExaminationJudgment();
  state.tier = tierId;
  renderCost();
  paintTierCards(b);
}

// Whether the live governed controls still equal a tier's resolved bundle. Only
// the controls a bundle speaks to are compared — editing variant, a section, the
// timing, a gate model or a prompt is not a deviation.
function currentMatchesTier(tierId) {
  const b = resolveTier(tierId);
  if (!b) return false;
  const featOn = (fid) => {
    const sw = document.querySelector(`.features input[data-feature="${fid}"]`);
    return !!(sw && sw.checked);
  };
  if (($('model') || {}).value !== b.model) return false;
  if (effortValue() !== b.effort) return false;
  if (($('glossary-model') || {}).value !== b.glossary_model) return false;
  if (Number(($('rounds') || {}).value || 1) !== b.rounds) return false;
  if (b.rounds > 1 && b.judge_model
      && ($('judge-model') || {}).value !== b.judge_model) return false;
  if (($('confidence') || {}).value !== b.min_confidence) return false;
  for (const [fid, want] of Object.entries(b.features)) {
    if (featOn(fid) !== !!want) return false;
  }
  return true;
}

// Recompute the selection from the live controls: the matching tier, or Custom.
function reEvaluateTier() {
  if (kind() !== 'review') return;
  const hit = TIER_ORDER.find((id) => currentMatchesTier(id)) || 'custom';
  state.tier = hit;
  paintTierCards(hit === 'custom' ? null : resolveTier(hit));
}

// Highlight the selected card (or none, for Custom) and show the Hammer's
// missing-key note. Pricing lives in updateTierPrices.
function paintTierCards(resolved) {
  const active = resolved ? resolved.tierId : null;
  const cards = [...document.querySelectorAll('.tier-card')];
  cards.forEach((card) => {
    const on = card.dataset.tier === active;
    card.setAttribute('aria-checked', on ? 'true' : 'false');
    card.tabIndex = on ? 0 : -1;                 // roving tabindex
  });
  // Custom (nothing checked) still needs one card reachable by Tab.
  if (active === null && cards[0]) cards[0].tabIndex = 0;
  const note = $('tier-hammer-sapling-note');
  if (note) {
    const hp = state.presets && state.presets.hammer;
    note.hidden = !(hp && hp.sapling === 'always' && !state.saplingKeyed);
  }
}

// Fill each card's price from the same estimate the sticky bar uses, echoing the
// chosen timing column. Blank while a file is still staging.
function updateTierPrices() {
  const files = stagedReviewFiles();
  document.querySelectorAll('.tier-card').forEach((card) => {
    const el = card.querySelector('[data-tier-price]');
    if (!el) return;
    const resolved = resolveTier(card.dataset.tier);
    if (!resolved || !files.length) {
      el.textContent = ''; el.classList.add('pending'); return;
    }
    const p = priceReview(priceBundle(resolved), files);
    const v = mode() === 'batch' ? p.batch : p.now;
    if (typeof v !== 'number') {
      el.textContent = ''; el.classList.add('pending'); return;
    }
    el.classList.remove('pending');
    const dollars = v < 0.01 ? v.toFixed(3) : v.toFixed(2);
    el.textContent = `${p.approx ? '≈' : '~'} $${dollars}`;
  });
}

// Apply Standard once, when models, presets, switches and a staged review file
// are all ready — whichever fetch finishes last triggers it. state.tier===null
// makes it idempotent, so it fires exactly once and never clobbers a later pick.
function maybeInitTier() {
  if (state.tier === null && kind() === 'review'
      && Object.keys(state.presets || {}).length
      && document.querySelector('.features input[data-feature]')
      && filesToRun().length) {
    applyPreset('standard');
  }
}

async function loadPresets() {
  try {
    const body = await api('/api/presets');
    const map = {};
    (body.tiers || []).forEach((t) => { map[t.id] = t; });
    state.presets = map;
  } catch (_) {
    state.presets = {};        // the tiers just won't apply; Custom still works
  }
  maybeInitTier();
  updateTierPrices();
}

// Tier cards select a tier; the Customize toggle reveals the tabbed drawer; the
// sub-tabs switch panels (a distinct class from the global .tab screen router).
(function wireTierUi() {
  // The next index for an arrow/Home/End keypress in a roving-tabindex group.
  const nextIndex = (key, i, len) => {
    if (key === 'ArrowRight' || key === 'ArrowDown') return (i + 1) % len;
    if (key === 'ArrowLeft' || key === 'ArrowUp') return (i - 1 + len) % len;
    if (key === 'Home') return 0;
    if (key === 'End') return len - 1;
    return -1;
  };

  // The tier picker is a radiogroup: click or arrow-key selects a card (arrow
  // moves and selects, per the radiogroup pattern). paintTierCards keeps the
  // roving tabindex in step with the checked card.
  const picker = $('tier-picker');
  if (picker) {
    picker.addEventListener('click', (e) => {
      const card = e.target.closest('[data-tier]');
      if (card) applyPreset(card.dataset.tier);
    });
    picker.addEventListener('keydown', (e) => {
      const cards = [...picker.querySelectorAll('.tier-card')];
      const i = cards.indexOf(document.activeElement);
      if (i < 0) return;
      const j = nextIndex(e.key, i, cards.length);
      if (j < 0) return;
      e.preventDefault();
      applyPreset(cards[j].dataset.tier);
      cards[j].focus();
    });
  }

  const toggle = $('customize-toggle');
  const adv = $('advanced-options');
  if (toggle && adv) {
    toggle.addEventListener('click', () => {
      const open = toggle.getAttribute('aria-expanded') === 'true';
      toggle.setAttribute('aria-expanded', open ? 'false' : 'true');
      adv.hidden = open;
    });
  }

  // The Custom sub-tabs are a tablist: aria-selected marks the active tab, a
  // roving tabindex keeps only it in the Tab order, and the arrow keys move
  // between them (selecting on move, the automatic-activation pattern).
  const tabs = $('custom-tabs');
  if (tabs && adv) {
    const subtabs = () => [...tabs.querySelectorAll('.subtab')];
    const activate = (btn, focus) => {
      subtabs().forEach((b) => {
        const on = b === btn;
        b.setAttribute('aria-selected', on ? 'true' : 'false');
        b.tabIndex = on ? 0 : -1;
      });
      adv.querySelectorAll('.tabpanel').forEach((p) =>
        p.classList.toggle('is-active', p.dataset.tab === btn.dataset.tab));
      if (focus) btn.focus();
    };
    tabs.addEventListener('click', (e) => {
      const btn = e.target.closest('.subtab');
      if (btn) activate(btn, false);
    });
    tabs.addEventListener('keydown', (e) => {
      const list = subtabs();
      const i = list.indexOf(document.activeElement);
      if (i < 0) return;
      const j = nextIndex(e.key, i, list.length);
      if (j < 0) return;
      e.preventDefault();
      activate(list[j], true);
    });
  }
})();

// Prep sends the whole manuscript once, so its price is simply what the files
// add up to on this model.
function pricePrep(m) {
  let cost = 0;
  const factor = effortFactor(m, effortValue());
  filesToRun().forEach((f) => {
    if (!f.prep) return;
    cost += (f.prep.input_tokens * m.input_per_mtok
      + f.prep.output_tokens * m.output_per_mtok * factor) / 1e6;
  });
  return cost;
}

// Promo is one call over the whole book — priced like prep (input plus a fixed
// teaser-and-posts output), with the reasoning dial scaling the output half. No
// batch column: promo is always a single synchronous call. The server has
// already folded the claim-check pass into these token figures when it is on.
function pricePromo(m) {
  let cost = 0;
  const factor = effortFactor(m, effortValue());
  filesToRun().forEach((f) => {
    if (!f.promo) return;
    cost += (f.promo.input_tokens * m.input_per_mtok
      + f.promo.output_tokens * m.output_per_mtok * factor) / 1e6;
  });
  return cost;
}

// The one canonical price, echoed beside the sticky Start button. Blank when
// nothing is staged; "pricing…" while a dropped file is still being preflighted
// (before its token counts land, so there is no figure yet); "~ $X" once priced,
// "≈ $X" when the estimate is rough.
function setStartPrice(value, { approx = false } = {}) {
  const el = $('start-price');
  if (!el) return;
  if (!filesToRun().length) {
    el.textContent = '';
    el.classList.remove('pending');
    return;
  }
  if (typeof value !== 'number') {
    el.textContent = 'pricing…';
    el.classList.add('pending');
    return;
  }
  el.classList.remove('pending');
  const dollars = value < 0.01 ? value.toFixed(3) : value.toFixed(2);
  el.textContent = `${approx ? '≈' : '~'} $${dollars}`;
}

// The Advanced drawer stays closed for most runs, so its summary line carries
// what — if anything — this run does differently. Effort and glossary are left
// out unless notable: changing them saves them as the new default, so they are
// never really "non-default". Empty diffs read "standard settings".
function updateAdvancedSummary() {
  const el = $('advanced-summary');
  if (!el) return;
  const parts = [];
  const rounds = Number(($('rounds') || {}).value || 1);
  if (rounds > 1) parts.push(`${rounds} rounds`);
  if (($('continuity-only') || {}).checked) parts.push('continuity only');
  const conf = ($('confidence') || {}).value;
  if (conf === 'high') parts.push('only sure changes');
  if (conf === 'low') parts.push('flags more');
  if (($('glossary-model') || {}).value === 'off') parts.push('no glossary read');
  const live = collectFeatures();
  state.features.forEach((f) => {
    if (!(f.id in live) || live[f.id] === !!f.default) return;
    const name = f.label.split(' — ')[0];
    parts.push(live[f.id] ? `${name} on` : `${name} off`);
  });
  el.textContent = ` — ${parts.length ? parts.join(' · ') : 'standard settings'}`;
}

// Anything toggled inside the drawer refreshes its summary — including a control
// like confidence that doesn't reprice the estimate and so never passed through
// renderCost. (continuity-only does reprice; it has its own renderCost listener
// above.)
(() => {
  const adv = $('advanced-options');
  if (adv) adv.addEventListener('change', updateAdvancedSummary);
})();

function renderCost() {
  updateAdvancedSummary();
  const m = state.models.find((x) => x.id === $('model').value);
  $('model-blurb').textContent = m ? m.blurb : '';
  const money = (v) => (typeof v === 'number'
    ? `about $${v < 0.01 ? v.toFixed(3) : v.toFixed(2)}` : '');

  if (isPromo()) {
    renderPromoCost(m, money);
    modelHint(m);
    return;
  }

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
    setStartPrice(m && files.length ? pricePrep(m) : null);
    modelHint(m);
    return;
  }

  if (isCorrections()) {
    // Deterministic and free — no model, no price (a PDF read is a small model
    // cost, taken on the button). The button waits on one correctable file plus
    // something to apply: an edit list, an attached proof, or a typed list —
    // the latter two read into a list on the button itself.
    const hasList = (($('corrections-input') || {}).value || '').trim().length > 0;
    const hasSource = !!state.correctionsSource;
    const hasTyped = (($('corrections-list-text') || {}).value || '')
      .trim().length > 0;
    // …and stays greyed out while a read is in flight: a PDF fills the list a
    // batch at a time, so until the last one lands "Apply" would mean applying
    // the corrections the model has read and silently dropping the rest.
    // A failed read locks it the same way, for the same reason.
    $('start').disabled = state.startBusy
                          || state.correctionsReading > 0
                          || state.correctionsReadFailed
                          || !(filesToRun().length > 0
                               && (hasList || hasSource || hasTyped));
    correctionsHint();
    setStartPrice(null);
    return;
  }

  const price = m ? priceReview(bundleFromControls(), stagedReviewFiles())
                  : { now: null, batch: null, approx: false };
  // The estimate includes the switched-on passes and the between-round judge;
  // the note only stays to flag that a pass whose size the book decides makes
  // the figure a rough one.
  const note = $('features-cost-note');
  if (note) note.hidden = !price.approx;
  // The four tier chips price off the same estimate, so they move with the
  // staged files, the chosen timing, and the Sapling key.
  updateTierPrices();

  const ready = m && m.available && filesToRun().length > 0;
  $('start').disabled = !ready;
  // The sticky echo follows the chosen timing — overnight or now — since that
  // is the price the button is about to spend.
  setStartPrice(mode() === 'batch' ? price.batch : price.now,
                { approx: price.approx });
  modelHint(m);
}

// The drop-time promo estimate: what this book costs to turn into a teaser and
// posts — on the chosen model, and across all of them so the comparison is a
// real cross-provider choice. Plus the human override when a book runs past the
// single-pass limit: until a person ticks the box, the run stays blocked.
function renderPromoCost(m, money) {
  const files = filesToRun().filter((f) => f.promo);
  const line = $('promo-cost-line');
  const compare = $('promo-cost-compare');
  const warn = $('promo-oversize');
  const ok = $('promo-oversize-ok');

  if (!files.length) {
    line.textContent = '';
    compare.hidden = true;
    warn.hidden = true;
    $('start').disabled = true;
    setStartPrice(null);
    return;
  }

  const words = files.reduce((n, f) => n + (f.promo.words || 0), 0);
  const claimCheck = files.some((f) => f.promo.verify_claims);
  line.textContent = m
    ? `About ${words.toLocaleString()} words on ${m.display} costs `
      + `${money(pricePromo(m))}`
      + `${claimCheck ? ' with the claim-check on' : ''}. One call over the `
      + `whole book — there is no overnight rate.`
    : '';

  // Every model, cheapest first, so the picker sees the full cross-provider
  // spread rather than only the model already chosen.
  const rows = $('promo-cost-rows');
  rows.innerHTML = '';
  state.models
    .map((x) => ({ x, cost: pricePromo(x) }))
    .sort((a, b) => a.cost - b.cost)
    .forEach(({ x, cost }) => {
      const tr = document.createElement('tr');
      if (m && x.id === m.id) tr.className = 'chosen';
      const name = document.createElement('td');
      name.textContent = x.display + (x.available ? '' : ' — no key yet');
      const price = document.createElement('td');
      price.textContent = money(cost);
      tr.append(name, price);
      rows.append(tr);
    });
  compare.hidden = false;

  // Over the single-pass limit: show the size and hold the run until a person
  // chooses. The limit is DocProof's caution, not a model ceiling.
  const over = files.find((f) => f.promo.over_limit);
  if (over) {
    $('promo-oversize-note').textContent =
      `This book is about ${over.promo.pass_tokens.toLocaleString()} tokens, `
      + `over the ${over.promo.max_input_tokens.toLocaleString()}-token `
      + `single-pass limit for promo.`;
    warn.hidden = false;
  } else {
    warn.hidden = true;
    ok.checked = false;
  }

  $('start').disabled = !(m && m.available && (!over || ok.checked));
  setStartPrice(m ? pricePromo(m) : null);
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

// Why a greyed-out Apply is greyed out. Corrections never reaches modelHint (it
// runs no model), so it says its own piece in the same place: locked while the
// model is still reading, and locked after a read that failed — with where to
// go from there, since a part-read list looks perfectly fine on screen.
function correctionsHint() {
  const hint = $('start-hint');
  if (!hint) return;
  if (state.correctionsReading > 0) {
    hint.textContent = 'The model is still reading the corrections — Apply '
      + 'unlocks when the last one is in.';
    hint.hidden = false;
  } else if (state.correctionsReadFailed) {
    hint.textContent = 'That read didn’t finish, so the list below is only part '
      + 'of the proof. Read it again — Apply stays locked until a read completes.';
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
    renderCost();          // the sticky price echoes the chosen timing
  }));

const mode = () => document.querySelector('input[name="mode"]:checked').value;

$('start').addEventListener('click', async () => {
  const button = $('start');
  const label = button.textContent;
  // The read below re-renders the button as it fills the edit list; this keeps
  // that from handing it back mid-flight.
  state.startBusy = true;
  button.disabled = true;
  button.textContent = 'Starting…';
  try {
    // One-button corrections: read the attached proof (a marked-up PDF or
    // redlined Word file) — or, with no proof, the typed plain-English list —
    // into the edit list first, unless it was already previewed, then apply
    // below. The readers surface their own failure and re-raise, so a bad read
    // aborts here — nothing half-read is ever applied.
    if (isCorrections() && !(($('corrections-input') || {}).value || '').trim()) {
      const typed = (($('corrections-list-text') || {}).value || '').trim();
      if (state.correctionsSource || typed) {
        button.textContent = 'Reading corrections…';
        if (state.correctionsSource) {
          await readCorrectionsSource(state.correctionsSource);
        } else {
          await readCorrectionsList();
        }
        button.textContent = 'Applying…';
      }
    }
    // A marked-PDF read attaches the proof's page texts (and its comments)
    // beside the list, but they live only on this page — a reload discards
    // them while the list itself can be re-supplied. Running a page-citing
    // list without them silently loses every mark's page narrowing, so a
    // repeated find can only be flagged — the flood that buried one run in
    // hundreds of ambiguous comma flags. Asked about, never assumed.
    if (isCorrections()) {
      const ta = $('corrections-input');
      let edits = [];
      try { edits = JSON.parse((ta || {}).value || '[]'); } catch (_) { /* gated below by the server */ }
      let pages = [];
      try { pages = JSON.parse(((ta || {}).dataset || {}).pages || '[]'); } catch (_) { /* as missing */ }
      const cites = Array.isArray(edits) && edits.some((e) => e && e.page);
      const attached = Array.isArray(pages) && pages.length > 0;
      if (cites && !attached && !confirm(
        'This list cites proof pages, but the proof’s page texts aren’t '
        + 'attached. They’re captured when the marked PDF is read, and a page '
        + 'reload discards them.\n\nWithout them, marks lose their page '
        + 'narrowing — a correction to repeated text can only be flagged, not '
        + 'applied.\n\nOK runs without them anyway. Cancel to go back — '
        + 're-read the marked proof to attach its pages.')) {
        return;
      }
    }
    // Promo is its own pipeline with its own page: the same dropped files, sent
    // to /api/promo/run, and the Promo tab shows them being written.
    const promoRun = isPromo();
    if (promoRun) {
      await api('/api/promo/run', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          file_ids: filesToRun().map((f) => f.id),
          model: $('model').value,
          effort: effortValue(),
          allow_oversize: $('promo-oversize-ok').checked,
        }),
      });
    } else {
      await api('/api/jobs', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          // Corrections run one InDesign file at a time — a list is specific to
          // its book — so only the first goes even if several are staged.
          file_ids: (isCorrections()
            ? filesToRun().slice(0, 1) : filesToRun()).map((f) => f.id),
          model: $('model').value,
          kind: kind(),
          corrections: isCorrections()
            ? (($('corrections-input') || {}).value || '') : '',
          // The reviewer comments the edits were read from (a marked-up PDF), so
          // the change log accounts for every one; empty for a typed/Word list.
          corrections_comments: isCorrections()
            ? ((($('corrections-input') || {}).dataset || {}).comments || '') : '',
          // The proof's page texts, so each mark is matched against the run of
          // book text its own page set.
          corrections_pages: isCorrections()
            ? ((($('corrections-input') || {}).dataset || {}).pages || '') : '',
          // The opt-in model gate that holds a doubtful edit back for a human.
          corrections_sanity: isCorrections()
            && !!(($('corrections-sanity') || {}).checked),
          // The opt-in second look that settles the delegated either/or notes.
          corrections_second_look: isCorrections()
            && !!(($('corrections-second-look') || {}).checked),
          // The last tier, its own flag now. Sent as a real boolean so it controls
          // the frontier reads on its own; the server still defaults a null to
          // follow the second look, for callers that do not send it.
          corrections_escalate: isCorrections()
            && !!(($('corrections-escalate') || {}).checked),
          prep_output: prepOutput(),
          prep_subject: isPrep() ? ($('prep-subject') || {}).value || '' : '',
          prep_title: isPrep() ? ($('prep-title') || {}).value.trim() : '',
          prep_author: isPrep() ? ($('prep-author') || {}).value.trim() : '',
          mode: (isPrep() || isCorrections()) ? 'now' : mode(),
          schedule_at: (!isPrep() && !isCorrections() && mode() === 'batch'
                        && $('schedule-on').checked)
            ? $('schedule-at').value : null,
          min_confidence: $('confidence').value,
          // A profile is a review-only concept, and the server refuses one on any
          // other kind ("review profiles can only run a review"). The tier picker
          // is always populated — including while a corrections or prep run is
          // being started — so a profile-bearing tier (the detector-only batch
          // profile) would otherwise ride onto a corrections "apply" and 400 it.
          // Send it only for a review; every other kind sends none.
          profile: (kind() === 'review'
            ? ((resolveTier(state.tier) || {}).profile || '') : ''),
          variant: ($('variant') || {}).value || '',
          effort: effortValue(),
          glossary_model: $('glossary-model').value,
          features: collectFeatures(),
          category_knobs: collectCategoryKnobs(),
          rounds: Number($('rounds').value),
          judge_prompt: $('judge-prompt').value,
          judge_model: ($('judge-model') || {}).value || null,
          continuity_prompt: ($('continuity-prompt') || {}).value || '',
          continuity_only: !!(($('continuity-only') || {}).checked),
          continuity_model: ($('continuity-model') || {}).value || null,
          chapter_continuity_prompt:
            ($('chapter-continuity-prompt') || {}).value || '',
          chapter_continuity_model:
            ($('chapter-continuity-model') || {}).value || null,
          chapter_continuity_sensitivity:
            Number(($('chapter-continuity-sensitivity') || {}).value) || null,
          meaning_model: ($('meaning-model') || {}).value || null,
          meaning_prompt: ($('meaning-prompt') || {}).value || '',
          fix_model: ($('fix-model') || {}).value || null,
          fix_prompt: ($('fix-prompt') || {}).value || '',
          // The chosen effort tier, stamped on the job card. A macro over the
          // controls above, so it changes nothing the server runs — only the
          // label. "custom" once anything is edited off a tier.
          preset: kind() === 'review' ? (state.tier || '') : '',
          proposer_restraint: ($('proposer-restraint') || {}).value || 'restrained',
          judge_harshness: ($('judge-harshness') || {}).value || 'strict',
          selections: isPrep() ? {} : selectionPayload(),
        }),
      });
    }
    state.files = [];
    state.selected.clear();
    clearCorrectionsSource();
    renderFiles();
    show(promoRun ? 'promo' : 'jobs');
  } catch (err) {
    fail(err.message);
  } finally {
    state.startBusy = false;
    button.disabled = false;
    button.textContent = label;
    // Re-gate off the real state: a failed corrections read leaves a part-read
    // list behind, and the label/greying should say so rather than sit on
    // whatever it read as the click began.
    if (isCorrections()) { renderKind(); renderCost(); }
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

// Re-judging a finished review: which gates to run, and which model reads for
// each. It opens on the card the review is already on, because that is what it
// is about — and each gate carries its own picker, since the two ask different
// questions and are worth different models (a cheap one for the fix check, a
// frontier one for meaning, or the reverse).
function judgeModelSelect(id, chosen) {
  const sel = document.createElement('select');
  sel.id = id;
  state.models.forEach((m) => {
    const opt = document.createElement('option');
    opt.value = m.id;
    opt.textContent = m.available ? m.display : `${m.display} — add a key first`;
    opt.disabled = !m.available;
    sel.append(opt);
  });
  const usable = (x) => state.models.some((m) => m.id === x && m.available);
  sel.value = usable(chosen) ? chosen
    : ((state.models.find((m) => m.available) || {}).id || '');
  return sel;
}

function rejudgeGateRow(key, label, blurb, chosen, on_ = true) {
  const row = document.createElement('div');
  row.className = 'rejudge-gate';
  const on = document.createElement('label');
  on.className = 'field checkbox';
  const box = document.createElement('input');
  box.type = 'checkbox';
  box.dataset.gate = key;
  box.checked = on_;
  const name = document.createElement('span');
  name.append(box, document.createTextNode(` ${label}`));
  const small = document.createElement('small');
  small.className = 'muted';
  small.textContent = blurb;
  on.append(name, small);
  const pick = document.createElement('label');
  pick.className = 'field';
  const pickName = document.createElement('span');
  pickName.textContent = 'Model';
  const sel = judgeModelSelect(`rejudge-${key}-model`, chosen);
  // The picker only means anything while its gate is on.
  const sync = () => { pick.hidden = !box.checked; };
  box.addEventListener('change', sync);
  pick.append(pickName, sel);
  sync();
  row.append(on, pick);
  return row;
}

// Which cards have the re-judge form open, and what has been picked in each.
// The results list redraws on a timer and rebuilds every card from scratch, so
// without this the form would vanish mid-choice — well before anyone could read
// two blurbs and pick two models.
const rejudgeOpen = new Map();

function rejudgeForm(job, onClose, saved) {
  const form = document.createElement('div');
  form.className = 'rejudge-form';
  const head = document.createElement('p');
  head.className = 'muted small';
  head.textContent = 'Read every change in this review again and hold back the '
    + 'ones that fail. No detector calls — only the checks you pick here are '
    + 'paid for, and the result lands beside this review as its own.';
  form.append(head);
  const was = saved || {};
  form.append(rejudgeGateRow(
    'meaning_check', 'Meaning check',
    'Does the corrected sentence still mean what the original meant?',
    was.meaning_model || state.defaultMeaningModel,
    was.meaning_check !== false));
  form.append(rejudgeGateRow(
    'fix_check', 'Fix check',
    'Is the correction actually the right one?',
    was.fix_model || state.defaultFixModel,
    was.fix_check !== false));

  // Remember every choice as it is made, so a redraw can put it back exactly.
  const snapshot = () => {
    const g = (k) => form.querySelector(`input[data-gate="${k}"]`).checked;
    const m = (k) => (form.querySelector(`#rejudge-${k}-model`) || {}).value;
    rejudgeOpen.set(job.id, {
      meaning_check: g('meaning_check'), fix_check: g('fix_check'),
      meaning_model: m('meaning_check'), fix_model: m('fix_check'),
    });
  };
  form.addEventListener('change', snapshot);
  snapshot();

  const note = actionNote();
  const run = document.createElement('button');
  run.textContent = 'Run the checks';
  const cancel = document.createElement('button');
  cancel.textContent = 'Cancel';
  cancel.addEventListener('click', () => {
    rejudgeOpen.delete(job.id);
    form.remove();
    onClose();
  });

  run.addEventListener('click', async () => {
    const gate = (k) => form.querySelector(`input[data-gate="${k}"]`).checked;
    const model = (k) => (form.querySelector(`#rejudge-${k}-model`) || {}).value;
    if (!gate('meaning_check') && !gate('fix_check')) {
      note.textContent = 'Pick at least one check to run.';
      note.hidden = false;
      return;
    }
    run.disabled = true;
    note.hidden = true;
    try {
      await api(`/api/jobs/${job.id}/rejudge`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          meaning_check: gate('meaning_check'),
          fix_check: gate('fix_check'),
          meaning_model: gate('meaning_check') ? model('meaning_check') : null,
          fix_model: gate('fix_check') ? model('fix_check') : null,
        }),
      });
      rejudgeOpen.delete(job.id);
      form.remove();
      onClose();
      refreshJobs();
    } catch (err) {
      note.textContent = err.message;
      note.hidden = false;
      run.disabled = false;
    }
  });

  const actions = document.createElement('div');
  actions.className = 'job-actions';
  actions.append(run, cancel, note);
  form.append(actions);
  return form;
}

async function refreshJobs({ tick = false } = {}) {
  try {
    const { jobs } = tick
      ? await api('/api/tick', { method: 'POST' })
      : await api('/api/jobs');
    renderJobs(jobs);
  } catch (_) { /* transient; the next poll will catch up */ }
}

// Jobs the user has hit Abort on but which the worker hasn't yet flipped to
// cancelled — kept so the button reads "Aborting…" across the polls in between
// instead of springing back to "Abort".
const aborting = new Set();
const TERMINAL_STATES = ['done', 'failed', 'cancelled'];

// The steps a review moves through, in the order the pipeline runs them, with a
// one-line label and a plain-English quip for what each is actually doing. The
// ids match the stage ids the pipeline emits (see STAGE_STATE in app/jobs.py);
// `optional` stages only run when the job turns them on — see stageFlowFor.
const STAGE_FLOW = [
  { id: 'preparing', label: 'Reading your manuscript',
    quip: 'Skimming every page and sketching a story sheet — who’s who, what '
        + 'tense, whose voice — so the checks that follow actually know your book.' },
  { id: 'reviewing', label: 'Reviewing section by section',
    quip: 'The main read: working through the book in sections, hunting typos, '
        + 'grammar slips, and punctuation gremlins.' },
  { id: 'glossary', label: 'Building the glossary', optional: true,
    quip: 'Learning your invented names and spellings so they’re never quietly '
        + '“corrected” against you.' },
  { id: 'factcheck', label: 'Fact check', optional: true,
    quip: 'One read against the world outside the book — names, history, '
        + 'geography — asking, never editing: fiction bends the world on '
        + 'purpose.' },
  { id: 'adjudicate', label: 'Real-word typos', optional: true,
    quip: 'Weighing the sneaky ones — “form” for “from”, “lead” for “led” — that '
        + 'a spellchecker sails right past.' },
  { id: 'rewrite', label: 'Rewrite & compare', optional: true,
    quip: 'Quietly retyping each line and diffing it against yours to catch what '
        + 'a single read glides over.' },
  { id: 'languagetool', label: 'Mechanical check', optional: true,
    quip: 'A rules-based sweep for the commas, hyphens, and dropped words the '
        + 'model tends to shrug at.' },
  { id: 'continuity', label: 'Continuity read', optional: true,
    quip: 'Reading cover to cover for facts the book contradicts about itself — '
        + 'ages, dates, eye colours, the day of the week.' },
  { id: 'examination_judgment', label: 'Independent examination', optional: true,
    quip: 'A separate model is blindly judging a bounded sample of precise '
        + 'sites. Its answers are evaluation data only and cannot change the book.' },
  // Multi-round only: the judge between rounds. No switch backs it (it rides
  // the rounds choice), so it appears in the flow only while it is running.
  { id: 'round_judge', label: 'Judging the round', optional: true,
    quip: 'A strong judge reads every correction this round proposed — only '
        + 'what it vouches for is applied and read by the next round.' },
  // The wrap-up passes. These used to hide under "Writing your document",
  // which on a big book meant minutes of judge and whole-book work with no
  // sign of which was running. verify and low_confidence are config-file
  // choices with no switch, so like round_judge they only show while current.
  { id: 'verify', label: 'Cross-checking findings', optional: true,
    quip: 'A second model reads what the detectors agreed on and strikes '
        + 'anything it can’t vouch for.' },
  { id: 'sapling', label: 'Sapling grammar check', optional: true,
    quip: 'A second opinion from a dedicated grammar service — every '
        + 'suggestion vetted in context before it can touch your voice.' },
  { id: 'low_confidence', label: 'Second look at soft calls', optional: true,
    quip: 'Re-reading the model’s quieter hunches in context, promoting the '
        + 'real catches and letting the rest go.' },
  { id: 'smoothing', label: 'Line-editing suggestions', optional: true,
    quip: 'A line editor reads the whole book for small smoothings, a '
        + 'skeptical taste judge culls them, and every survivor reaches you '
        + 'as a question — never an edit.' },
  { id: 'chapter_continuity', label: 'Chapter continuity', optional: true,
    quip: 'Reading scene by scene for breaks that close inside a chapter — '
        + 'the cigarette lit twice, the dawn that turns to evening mid-scene.' },
  { id: 'meaning_check', label: 'Meaning check', optional: true,
    quip: 'One last read of every change, asking the question that matters '
        + 'most: does the sentence still mean what you meant?' },
  { id: 'fix_check', label: 'Fix check', optional: true,
    quip: 'The same last read, asking the other question: is each correction '
        + 'actually the right repair?' },
  { id: 'writing', label: 'Writing your document',
    quip: 'Folding every accepted change back in and packaging up your files.' },
];
// Corrections walks its own short pipeline — no detector, no rounds — and,
// with the two model passes switched on, the middle of it is minutes of
// frontier reads. Same shape as the review flow so the tracker renders it
// unchanged: ids match the stages apply_corrections emits (see CORR_STAGE in
// app/jobs.py), `optional` ones appear only when the job asked for them.
const CORRECTIONS_FLOW = [
  { id: 'reading', label: 'Reading the corrections list',
    quip: 'Parsing every correction and working out exactly what each one asks '
        + 'the book to do.' },
  { id: 'pagemap', label: 'Matching pages to the book', optional: true,
    quip: 'Lining the proof’s pages up against the book’s own text, so a mark '
        + 'on page 49 lands on page 49 and not on the first comma it finds.' },
  { id: 'second_look', label: 'Second look at open notes', optional: true,
    quip: 'Reading the reviewer’s open questions — the either/ors, the '
        + '“if this then that” — and settling what the page itself settles.' },
  { id: 'probing', label: 'Checking the anchors', optional: true,
    quip: 'A dry run to see which corrections would miss, before anything is '
        + 'written.' },
  { id: 'reanchor', label: 'Re-quoting what didn’t match', optional: true,
    quip: 'Quoting a lost anchor again from the book’s own words, so a '
        + 'correction the proof rendered differently still lands.' },
  { id: 'merge', label: 'Merging overlapping corrections', optional: true,
    quip: 'Two marks on one span become a single edit instead of fighting '
        + 'over it.' },
  { id: 'escalate', label: 'Settling the last queries', optional: true,
    quip: 'What’s still a question goes up to a stronger model with the whole '
        + 'book behind it — a consistency call settled by counting, not '
        + 'recalling.' },
  { id: 'sanity', label: 'Checking each correction', optional: true,
    quip: 'A last read of every proposed change; anything doubtful is held '
        + 'back for a person instead of applied.' },
  { id: 'applying', label: 'Applying your corrections',
    quip: 'Anchoring each correction to the exact text it names and replacing '
        + 'that span — the file itself, at last.' },
  { id: 'verifying', label: 'Checking the corrected file',
    quip: 'Reading our own output back against the list, so nothing went in '
        + 'that the list didn’t ask for.' },
  { id: 'writing', label: 'Writing your file',
    quip: 'Packaging the corrected InDesign file and the change report.' },
];

// A re-judge walks its own one-step flow, not the pipeline above: it runs the
// gates over a finished run's corrections and writes a new deliverable, and no
// detector pass fires. Shown on its own so the tracker can't check off passes
// this run never takes. See STAGE_STATE and JobRunner.rejudge in app/jobs.py.
const REJUDGE_FLOW = [
  { id: 'judging', label: 'Putting the corrections to the judges',
    quip: 'Reading every change the review proposed and asking whether it keeps '
        + 'your meaning and whether the fix is right.' },
];

// Which of the steps this particular job will run, in order. The always-on ones
// stay; an optional one is kept only when the job's toggles ask for it, so the
// tracker doesn't promise a pass that never fires. The current stage is always
// kept, even if a toggle says otherwise, so the tracker can never lose its
// place — and that clause alone is what admits the stages no switch backs
// (round_judge, verify, low_confidence): they surface while running and are
// never promised in advance.
function stageFlowFor(job) {
  if (job.stage === 'judging') return REJUDGE_FLOW;
  if (job.kind === 'corrections') {
    // The two model passes are the job's own checkboxes; the steps inside the
    // second look (page map, probe, re-anchor, merge) ride with it, and — like
    // the review's unswitched passes — the current one is always kept, so a
    // step that turns out not to run is simply never promised.
    return CORRECTIONS_FLOW.filter((s) => {
      if (s.id === job.stage) return true;
      if (!s.optional) return true;
      if (s.id === 'pagemap') return !!job.corrections_paged;
      if (s.id === 'sanity') return !!job.corrections_sanity;
      if (s.id === 'escalate') return !!job.corrections_escalate;   // its own tier now
      return !!job.corrections_second_look;   // the second look and its repairs
    });
  }
  const f = job.features || {};
  const on = (k) => !!f[k];
  return STAGE_FLOW.filter((s) => {
    if (s.id === job.stage) return true;
    if (!s.optional) return true;
    if (s.id === 'glossary') return job.glossary_model !== 'off';
    return on(s.id);            // every switch-backed pass, by its feature id
  });
}

// A review shows the step tracker while it is actively working through the
// pipeline: a sync run (state "running"), the "preparing" read that comes just
// before it (still "queued"), or a batch run collecting — collect re-walks the
// same steps (re-ingest, fold, the whole-book passes, writing), and is the
// stretch that used to read as one long "almost done". Prep and promo have
// their own single-step lives and set no stage, so they never show one.
function tracksStages(job) {
  return ['review', 'corrections'].includes(job.kind) && !!job.stage
    && stageFlowFor(job).some((s) => s.id === job.stage)
    && (job.state === 'running' || job.state === 'queued'
        || job.state === 'collecting');
}

function stageElapsed(job) {
  if (!job.stage_since) return null;             // older record, or not set yet
  const secs = Math.floor((Date.now() - Date.parse(job.stage_since)) / 1000);
  if (!Number.isFinite(secs) || secs < 0) return null;   // clock skew: show none
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins} min`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m`;
}

// The step tracker: every step this run will take, the finished ones checked
// off, the current one lit with its quip (and a count or elapsed time), the rest
// waiting. It is what turns a frozen "Reading your manuscript" into a visible,
// legible march through the pipeline.
function stageTracker(job) {
  const flow = stageFlowFor(job);
  // Position within the flow this job is actually walking — filtering keeps the
  // pipeline's order, and a re-judge walks a different flow entirely, so a
  // global index over STAGE_FLOW would place it past every review step and check
  // them all off.
  const here = flow.findIndex((s) => s.id === job.stage);
  const ol = document.createElement('ol');
  ol.className = 'stages';
  flow.forEach((s, i) => {
    const status = i < here ? 'done' : i === here ? 'current' : 'pending';
    const li = document.createElement('li');
    li.className = status;
    const marker = document.createElement('span');
    marker.className = 'stage-marker';
    marker.textContent = status === 'done' ? '✓' : status === 'current' ? '●' : '○';
    const body = document.createElement('span');
    body.className = 'stage-body';
    const name = document.createElement('span');
    name.className = 'stage-name';
    name.textContent = s.label;
    if (status === 'current') {
      const meta = stageMeta(job);
      if (meta) {
        const m = document.createElement('span');
        m.className = 'stage-meta';
        m.textContent = ` · ${meta}`;
        name.append(m);
      }
    }
    body.append(name);
    if (status === 'current') {
      const quip = document.createElement('span');
      quip.className = 'stage-quip';
      quip.textContent = s.quip;
      body.append(quip);
    }
    li.append(marker, body);
    ol.append(li);
  });
  return ol;
}

// What to show beside the current step's name: how far through the section
// count when there is one, otherwise how long the step has run.
// Which stages carry a count worth showing beside the step's name. The
// review's per-chunk loop, and the corrections passes that work through a
// list of queries one batch at a time.
const COUNTED_STAGES = ['reviewing', 'pagemap', 'second_look', 'escalate',
                        'sanity'];

function stageMeta(job) {
  const elapsed = stageElapsed(job);
  if (COUNTED_STAGES.includes(job.stage) && job.total) {
    return elapsed ? `${job.done} of ${job.total} · ${elapsed}`
                   : `${job.done} of ${job.total}`;
  }
  return elapsed;
}

function renderJobs(jobs) {
  const list = $('job-list');
  list.innerHTML = '';
  $('jobs-empty').hidden = jobs.length > 0;

  const active = jobs.filter(
    (j) => !TERMINAL_STATES.includes(j.state)).length;
  const badge = $('jobs-badge');
  badge.hidden = active === 0;
  badge.textContent = String(active);
  // A job that reached a terminal state is no longer aborting; forget it so the
  // set can't grow without bound.
  jobs.forEach((j) => { if (TERMINAL_STATES.includes(j.state)) aborting.delete(j.id); });
  $('jobs-clear').hidden = !jobs.some((j) => TERMINAL_STATES.includes(j.state));

  jobs.forEach((job) => {
    const li = document.createElement('li');

    const head = document.createElement('div');
    head.className = 'job-head';
    const name = document.createElement('span');
    name.className = 'file-name';
    name.textContent = job.filename;
    if (job.format) name.append(' ', formatBadge(job.format.name));
    // The effort tier a review ran at, if one was recorded.
    if (job.kind === 'review' && job.preset && TIER_LABELS[job.preset]) {
      name.append(' ', formatBadge(TIER_LABELS[job.preset]));
    }
    // While the step tracker is shown it already names the current step, so a
    // header that repeats that exact label prints the same words twice — most
    // visibly "Reading your manuscript", which the header (plain_state) and the
    // lit "preparing" step word identically. Drop the header echo in that case
    // and let the tracker carry it; every other step already reads differently
    // in the two places, so only the true duplicate is suppressed.
    const currentStep = tracksStages(job)
      && (stageFlowFor(job).find((s) => s.id === job.stage) || {});
    const echoesTracker = currentStep && currentStep.label === job.plain_state;
    if (!echoesTracker) {
      const status = document.createElement('span');
      status.className = 'job-state'
        + (job.state === 'done' ? ' is-done' : job.state === 'failed' ? ' is-failed' : '');
      status.textContent = job.plain_state;
      head.append(name, status);
    } else {
      head.append(name);
    }
    li.append(head);

    // "preparing" runs before the job flips to "running" (state is still
    // "queued"), so give it a bar too — otherwise the story-sheet wait shows
    // nothing moving at all. Everything else with a bar is a running job.
    if (job.state === 'running' || tracksStages(job)) {
      const bar = document.createElement('div');
      bar.className = 'bar';
      const fill = document.createElement('i');
      // The per-chunk loop ("reviewing") has a real count; the whole-book passes
      // do not, so the bar runs indeterminate while the step tracker below
      // carries the truth. A record with no stage — from before stages existed —
      // keeps the old numeric bar.
      const numeric = (job.stage === 'reviewing' || !job.stage) && job.total;
      if (numeric) {
        fill.style.width = `${Math.round((job.done / job.total) * 100)}%`;
      } else {
        bar.classList.add('indeterminate');
      }
      bar.append(fill);
      li.append(bar);
    }

    // The step tracker: the whole pipeline this run will walk, checked off as it
    // goes, the current step lit with a plain-English quip. This is what makes a
    // long whole-book pass read as progress instead of a stall.
    if (tracksStages(job)) li.append(stageTracker(job));

    // A running review is actively spending; let the user pull the plug. The
    // worker stops between calls and cancels everything not already in flight,
    // so the abort caps the spend rather than only hiding the result.
    if (job.state === 'running') {
      const actions = document.createElement('div');
      actions.className = 'job-actions';
      const note = actionNote();
      const abort = document.createElement('button');
      if (aborting.has(job.id)) {
        abort.textContent = 'Aborting…';
        abort.disabled = true;
      } else {
        abort.className = 'danger';
        abort.textContent = 'Abort';
        abort.addEventListener('click', async () => {
          abort.disabled = true;
          note.hidden = true;
          aborting.add(job.id);
          try {
            await api(`/api/jobs/${job.id}/cancel`, { method: 'POST' });
            refreshJobs();
          } catch (err) {
            aborting.delete(job.id);
            note.textContent = err.message;
            note.hidden = false;
            abort.disabled = false;
          }
        });
      }
      actions.append(abort, note);
      li.append(actions);
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
    } else if (job.ready && job.is_corrections) {
      li.append(correctionsActions(job));
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
      // The change log is a prose .docx the press can hand to an author. It is
      // written per config, so the server says whether this review has one —
      // on the web there is no Finder to find it in otherwise.
      if (job.has_change_log) {
        actions.append(openButton(job, 'changes',
          WEB ? 'Download the change log' : 'Open the change log', note,
          { quiet: true }));
      }
      if (job.has_examination_report) {
        actions.append(openButton(job, 'examination',
          'Download examination coverage', note, { quiet: true }));
      }
      if (job.has_examination_evaluation) {
        actions.append(openButton(job, 'examination-evaluation',
          'Download blind examination sample', note, { quiet: true }));
      }
      // "Show in Finder" only means something on the Mac the file lives on.
      if (!WEB) {
        actions.append(
          openButton(job, 'document', 'Show in Finder', note, { reveal: true }));
      }
      // A review that finished can have the judge gates run over it afterwards
      // — no detector call, so a book proofread before the gates existed can be
      // gated now for the price of the gates alone. The result lands beside this
      // one as its own review rather than overwriting it, which is what makes
      // the two comparable.
      if (job.rejudgeable) {
        const rj = document.createElement('button');
        rj.textContent = 'Re-judge';
        rj.title = 'Read every change in this review again — for meaning, for '
          + 'whether the fix is right, or both — and hold back the ones that '
          + 'fail. Makes no detector calls.';
        const open = (savedState) => {
          if (li.querySelector('.rejudge-form')) return;   // already open
          rj.disabled = true;
          li.append(rejudgeForm(job, () => { rj.disabled = false; }, savedState));
        };
        rj.addEventListener('click', () => open());
        actions.append(rj);
        // This list redraws on a timer, rebuilding every card. A form that was
        // open before the redraw comes back with its choices intact, rather
        // than disappearing under whoever was still reading it.
        if (rejudgeOpen.has(job.id)) queueMicrotask(() => open(rejudgeOpen.get(job.id)));
      }
      const bits = [];
      // "Suggested" was wrong on both counts: a tracked change is a correction
      // the review is willing to stand behind, and the things it merely
      // suggests are the margin queries — which this line never mentioned at
      // all. Prep never reaches here (it has its own actions above), so
      // `applied` on this branch is always tracked changes.
      if (typeof job.applied === 'number') {
        bits.push(`${job.applied} correction${job.applied === 1 ? '' : 's'} applied`);
      }
      if (job.queried) {
        bits.push(`${job.queried} question${job.queried === 1 ? '' : 's'} `
          + 'in the margins');
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
      driveActions(actions, note, job);
      li.append(actions, note);
      // Tracked changes are invisible until you know which panel shows them,
      // and that panel is in a different place in each application.
      if (job.format) {
        const where = document.createElement('p');
        where.className = 'where';
        where.textContent = job.format.where_to_look;
        li.append(where);
      }
      // A "done" run with a fail-open component must not read as clean: the
      // normal manuscript may be sound while an optional pass or experimental
      // artifact is missing. summary.md has the full accounting.
      if (Array.isArray(job.warnings) && job.warnings.length) {
        const warn = document.createElement('div');
        warn.className = 'job-warning';
        const lead = job.warnings.length === 1 ? '1 review warning'
          : `${job.warnings.length} review warnings`;
        warn.textContent = `⚠ ${lead} — ${job.warnings.join('; ')}. `
          + 'See summary.md.';
        li.append(warn);
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

      // An overnight review that failed AFTER its batch completed has its
      // results sitting at the vendor, already paid for. Offer to finish
      // collecting them instead of Retry, which would resubmit and bill twice —
      // so this is the primary action, ahead of Retry, when it applies.
      if (job.recoverable) {
        const recover = document.createElement('button');
        recover.textContent = 'Finish collecting';
        recover.addEventListener('click', async () => {
          recover.disabled = true;
          try {
            await api(`/api/jobs/${job.id}/recover`, { method: 'POST' });
            refreshJobs();
          } catch (err) {
            recover.disabled = false;
          }
        });
        actions.append(recover);
      }
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

    // A finished job — done, failed, or cancelled — can be cleared from the
    // list. It takes the produced documents with it, so it asks first.
    if (TERMINAL_STATES.includes(job.state)) {
      const remove = document.createElement('button');
      remove.className = 'link job-remove';
      remove.textContent = 'Remove';
      remove.title = 'Remove from this list and delete its downloaded files';
      confirmInline(remove, 'Remove this and delete its files?', async (row) => {
        try {
          await api(`/api/jobs/${job.id}`, { method: 'DELETE' });
          refreshJobs();                     // the whole list redraws; row goes
        } catch (err) {
          row.fail(err.message || 'Could not remove this one.');
        }
      });
      li.append(remove);
    }

    list.append(li);
  });
}

// Clear every finished job at once. One confirm, then a single call the server
// scopes to this user's own finished work.
$('jobs-clear').addEventListener('click', async () => {
  const btn = $('jobs-clear');
  if (!confirm('Remove every finished document from this list? This also '
    + "deletes their downloaded files, and can't be undone.")) return;
  btn.disabled = true;
  try {
    await api('/api/jobs/clear', { method: 'POST' });
    refreshJobs();
  } catch (err) {
    alert(err.message || 'Could not clear the finished documents.');
  } finally {
    btn.disabled = false;
  }
});

// Where a result button says why it couldn't do what it said.
function actionNote() {
  const p = document.createElement('p');
  p.className = 'action-note error';
  p.hidden = true;
  return p;
}

// A two-step confirm in place of a native dialog, for a destructive button that
// deletes files. The first click swaps the button for a short warning and
// Remove / Keep; Keep restores the button, Remove runs `onConfirm(row)`. `row`
// carries a `.fail(message)` so a failed deletion reports where it happened
// instead of a system alert. On success the caller usually redraws the list, so
// the transient row simply goes with it.
function confirmInline(button, prompt, onConfirm) {
  button.addEventListener('click', () => {
    const row = document.createElement('span');
    row.className = 'confirm-inline';
    const msg = document.createElement('span');
    msg.className = 'confirm-msg';
    msg.textContent = prompt;
    const yes = document.createElement('button');
    yes.className = 'danger';
    yes.textContent = 'Remove';
    const no = document.createElement('button');
    no.className = 'quiet';
    no.textContent = 'Keep';
    row.append(msg, yes, no);
    row.fail = (message) => {
      msg.textContent = message;
      yes.hidden = true;
      no.textContent = 'Dismiss';
      no.disabled = false;
    };
    no.addEventListener('click', () => row.replaceWith(button));
    yes.addEventListener('click', async () => {
      yes.disabled = true;
      no.disabled = true;
      await onConfirm(row);
    });
    button.replaceWith(row);
  });
  return button;
}

// The window this app runs in cannot display a Word file and will not download
// one, so "Open in Word" asks the app to hand the file to Word — it is sitting
// in the user's own Documents folder already. Run in an ordinary browser, the
// app says so and the file is downloaded instead.
function openButton(job, which, text, note, { reveal = false, quiet = false } = {}) {
  const button = document.createElement('button');
  button.textContent = text;
  if (reveal || quiet) button.className = 'quiet';
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

// The InDesign file opens with the whole book in one primary frame; InDesign
// reflows on edit, not on open, so the designer runs this one-time script once
// to thread it across pages. A plain download — it installs into the Scripts
// panel, and the file itself carries the install steps in its header comment.
function reflowButton() {
  const button = document.createElement('button');
  button.textContent = 'Get the one-time reflow script';
  button.title = 'Install once in InDesign (Scripts panel), then run it with '
    + 'a DocProof file open to flow the book across pages.';
  button.addEventListener('click', () => {
    window.location.href = '/api/prep/reflow-script';
  });
  return button;
}

// A finished prep job: whichever files it wrote, the notes, and the one number
// that matters — whether the author's words came through untouched.
// The designer notes, as a printable document. Same house style as the compare
// report (shared REPORT_CSS/esc), built from the prep JSON the in-app notes
// screen already uses. It prints itself on open, so the button reads as a PDF
// download: the browser's own Print → Save as PDF gives crisp, selectable text
// with no dependency. The heart of it is the "For the designer" list — the
// judgment calls prep raised but did not make.
function designerNotesHTML(d) {
  const c = d.counts || {};
  const flags = d.flags || [];
  const sheet = d.style_sheet || {};
  const cfg = d.config || {};
  const u = d.usage || {};
  const num = (n) => Number(n || 0).toLocaleString();
  const rightNum = ' style="text-align:right;font-variant-numeric:tabular-nums"';
  const money = typeof d.cost === 'number'
    ? (d.cost < 0.01 ? '<$0.01' : '$' + d.cost.toFixed(2)) : '—';

  const title = `Designer notes — ${d.source_name || 'manuscript'}`;
  const gen = d.generated_at ? new Date(d.generated_at).toLocaleString() : '';
  const sub = `<p class="sub">Style set ${esc(sheet.name || '—')}`
    + `${sheet.version ? ' v' + esc(sheet.version) : ''}`
    + `${sheet.trim ? ' · ' + esc(sheet.trim) : ''}`
    + `${cfg.model ? ' · ' + esc(cfg.model) : ''}</p>`
    + (gen ? `<p class="sub">Prepared ${esc(gen)}</p>` : '');

  const cards = '<div class="cards">'
    + `<div class="card"><span class="n">${num(c.tagged)}</span><span class="l">paragraphs tagged</span></div>`
    + `<div class="card"><span class="n">${num(c.words)}</span><span class="l">words</span></div>`
    + `<div class="card"><span class="n">${num(c.scene_breaks_inserted)}</span><span class="l">scene breaks written</span></div>`
    + `<div class="card"><span class="n">${num(flags.length)}</span><span class="l">flags for the designer</span></div>`
    + '</div>';

  const verify = d.verified
    ? '<p class="headline">✓ <b>Word for word, this says exactly what the '
      + `manuscript said</b> — checked on the finished `
      + `${(d.verification || []).length > 1 ? 'files' : 'file'}, not assumed.</p>`
    : '<p class="headline" style="background:#fbeae2">✗ <b>The word-for-word '
      + 'check did not pass.</b> Nothing was handed over — do not place this file.</p>';

  const styleRows = Object.entries(d.styles || {}).map(([name, n]) =>
    `<tr><td>${esc(name)}</td><td${rightNum}>${num(n)}</td></tr>`).join('');
  const styleTable = styleRows
    ? '<h2>Style mapping</h2><p class="blurb">Every paragraph carries one of '
      + 'these InDesign paragraph-style names — the template\'s own.</p>'
      + `<table><thead><tr><th>InDesign paragraph style</th>`
      + `<th${rightNum}>Paragraphs</th></tr></thead><tbody>${styleRows}</tbody></table>`
    : '';

  const cleanup = [
    [`${num(c.blank_lines_removed)} blank line(s) removed`,
      'InDesign takes vertical spacing from the styles, not from empty paragraphs.'],
    [`${num(c.paragraphs_trimmed)} paragraph(s) trimmed`,
      'Trailing spaces and tabs. First-line indents were left to the style.'],
    [`${num(c.scene_breaks_inserted)} scene break(s) written, ${num(c.scene_breaks_from_author)} the author had typed`,
      'Only where the author signalled one — none were invented.'],
    [`${num(c.italic_paragraphs)} paragraph(s) keep their italics`,
      'Inline emphasis is left exactly where it was.'],
  ];
  const cleanupHTML = '<h2>What it did</h2>' + cleanup.map(([t, b]) =>
    `<p><b>${esc(t)}</b> <span class="caveat">${esc(b)}</span></p>`).join('');

  const flagsHTML = flags.length
    ? `<h2>For the designer — ${num(flags.length)}</h2>`
      + '<p class="blurb">These are raised, not fixed. Each one is a judgment a '
      + 'person should make.</p>'
      + flags.map((f) => `<div class="flag"><code>${esc(f.para_id)}</code> `
        + `${esc(f.message)}`
        + (f.preview ? ` <em>“${esc(f.preview)}”</em>` : '') + '</div>').join('')
    : '<h2>For the designer</h2><p class="none">No flags — nothing needs a '
      + 'human decision.</p>';

  const un = d.unanswered_paragraphs || [];
  const unHTML = un.length
    ? `<h2>Not labelled — ${num(un.length)}</h2><p class="blurb">These paragraphs `
      + 'were left untagged; give them a style by hand in InDesign.</p>'
      + `<p class="mono">${un.map(esc).join(', ')}</p>`
    : '';

  const inTok = (u.input_tokens || 0) + (u.cache_creation_input_tokens || 0)
    + (u.cache_read_input_tokens || 0);
  const usageHTML = '<h2>Usage</h2><table><tbody>'
    + `<tr><td>API calls</td><td${rightNum}>${num(u.api_calls)}</td></tr>`
    + `<tr><td>Input tokens</td><td${rightNum}>${num(inTok)}</td></tr>`
    + `<tr><td>Output tokens</td><td${rightNum}>${num(u.output_tokens)}</td></tr>`
    + `<tr><td>Estimated cost</td><td${rightNum}>${esc(money)}</td></tr>`
    + '</tbody></table>';

  const flagStyle = '.flag{padding:9px 0;border-bottom:1px solid var(--line)}'
    + '.flag code{color:var(--accent);font-family:ui-monospace,SFMono-Regular,'
    + 'Menlo,monospace;font-size:.85rem}.flag em{color:var(--muted)}';
  // The document prints itself once loaded, so the button lands straight in the
  // browser's Save-as-PDF dialog.
  const autoPrint = '<script>window.addEventListener("load",function(){'
    + 'setTimeout(function(){try{window.print();}catch(e){}},300);});<\/script>';

  return '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    + '<meta name="viewport" content="width=device-width,initial-scale=1">'
    + `<title>${esc(title)}</title><style>${REPORT_CSS}${flagStyle}</style></head><body>`
    + '<div class="wrap"><header class="rep">'
    + '<div class="brand">DocProof · InDesign prep</div>'
    + `<h1>${esc(title)}</h1>${sub}</header>`
    + cards + verify + styleTable + cleanupHTML + flagsHTML + unHTML + usageHTML
    + `<div class="foot">Generated ${esc(new Date().toLocaleString())} · designer `
    + 'notes for the InDesign-ready file, produced by the prep run.</div>'
    + `</div>${autoPrint}</body></html>`;
}

function notesFilename(d) {
  const stem = (d.source_name || 'manuscript')
    .replace(/\.[^.]+$/, '').replace(/[\\/:*?"<>|]+/g, ' ').trim();
  return `${stem || 'manuscript'} — designer notes.html`;
}

// Fetch the prep JSON, build the printable notes, and open them in a new tab
// where they print themselves. If a pop-up blocker (or a webview that won't
// open tabs) stops the window, hand the .html over instead so the button never
// silently does nothing — the saved file still prints itself on open.
async function openDesignerNotes(job) {
  let d;
  try {
    d = await api(`/api/jobs/${job.id}/prep`);
  } catch (err) {
    alert(`Couldn't load the designer notes: ${err.message}`);
    return;
  }
  const url = URL.createObjectURL(
    new Blob([designerNotesHTML(d)], { type: 'text/html' }));
  if (!window.open(url, '_blank')) {
    const a = document.createElement('a');
    a.href = url;
    a.download = notesFilename(d);
    document.body.append(a);
    a.click();
    a.remove();
  }
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

// The Drive archive, shared by review and prep rows: a link to the folder
// once the copy lands, a quiet note while it is saving, and — if Drive kept
// refusing — the reason and a way to try again. Nothing shows when the
// archive is off: `archive` stays '' then, so this whole block is skipped.
function driveActions(actions, note, job) {
  if (job.archive === 'done' && job.drive_link) {
    const link = document.createElement('a');
    link.className = 'file-link';
    link.href = job.drive_link;
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = 'In Drive ↗';
    actions.append(link);
  } else if (job.archive === 'pending') {
    const saving = document.createElement('span');
    saving.className = 'file-meta muted';
    saving.textContent = 'saving to Drive…';
    actions.append(saving);
  } else if (job.archive === 'failed') {
    note.textContent = job.archive_error || 'The Drive copy did not finish.';
    note.hidden = false;
    const retryArchive = document.createElement('button');
    retryArchive.className = 'quiet';
    retryArchive.textContent = 'Retry Drive copy';
    retryArchive.addEventListener('click', async () => {
      retryArchive.disabled = true;
      try {
        await api(`/api/jobs/${job.id}/archive`, { method: 'POST' });
        refreshJobs();
      } catch (err) {
        note.textContent = err.message || 'Could not save to Drive.';
        retryArchive.disabled = false;
      }
    });
    actions.append(retryArchive);
  }
}

function prepActions(job) {
  const wrap = document.createElement('div');
  const actions = document.createElement('div');
  actions.className = 'job-actions';
  const note = actionNote();
  // Which files this run wrote, from the same vocabulary the server uses.
  const wrote = {
    book: ['book'], indesign: ['indesign'], tracked: ['tracked'],
    both: ['indesign', 'tracked'], all: ['book', 'indesign', 'tracked'],
  }[job.prep_output] || ['book'];
  const first = wrote[0];
  if (wrote.includes('book')) {
    actions.append(openButton(job, 'book',
      WEB ? 'Download the book-styled copy' : 'Open the book-styled copy',
      note));
  }
  if (wrote.includes('indesign')) {
    actions.append(openButton(job, 'indesign',
      WEB ? 'Download the InDesign file (IDML)' : 'Open the file in InDesign',
      note));
    actions.append(reflowButton());
  }
  if (wrote.includes('tracked')) {
    actions.append(openButton(job, 'tracked',
      WEB ? 'Download the tracked-changes file' : 'Open the tracked-changes file',
      note));
  }
  const read = document.createElement('button');
  read.textContent = 'Read the prep notes';
  read.addEventListener('click', () => openPrepReport(job));
  actions.append(read);
  const notesPdf = document.createElement('button');
  notesPdf.textContent = 'Download designer notes (PDF)';
  notesPdf.addEventListener('click', () => openDesignerNotes(job));
  actions.append(notesPdf);
  if (!WEB) {
    actions.append(
      openButton(job, first, 'Show in Finder', note, { reveal: true }));
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
  driveActions(actions, note, job);
  wrap.append(actions, note);

  const book = job.prep_book || {};
  if (book.subject || book.title || book.author) {
    const facts = document.createElement('p');
    facts.className = 'where';
    const how = book.detected === false ? 'as set' : 'as read from the book';
    facts.textContent = `Book sketch (${how}): `
      + [book.subject && `subject “${book.subject}”`,
         book.title && `title “${book.title}”`,
         book.author && `author “${book.author}”`]
        .filter(Boolean).join(', ') + '.';
    wrap.append(facts);
  }

  const where = document.createElement('p');
  where.className = 'where';
  where.textContent = job.verified
    ? 'Checked word for word against your manuscript: nothing the author '
      + 'wrote was changed.'
      + (wrote.includes('book')
        ? ' The book-styled copy is a reading sketch for the author and '
          + 'editors — the finished interior still comes from InDesign.'
        : ' Place the tagged file in InDesign — the paragraph style names in '
          + 'it are the template\'s own.')
    : 'Heads up: this file has not been confirmed word-for-word against the '
      + 'manuscript. Read the prep notes before passing it on.';
  wrap.append(where);
  return wrap;
}

function correctionsActions(job) {
  const wrap = document.createElement('div');
  const actions = document.createElement('div');
  actions.className = 'job-actions';
  const note = actionNote();

  actions.append(openButton(job, 'corrected',
    WEB ? 'Download the corrected file (IDML)'
        : 'Open the corrected file in InDesign', note));
  const read = document.createElement('button');
  read.textContent = 'Read the corrections report';
  read.addEventListener('click', () => openCorrectionsReport(job));
  actions.append(read);
  // The flags a person still owns, resolvable in place: comment-level when the
  // list came off a proof, edit-level for a typed list. Zero flags, no button.
  const toFix = (job.total_comments ? job.unresolved : job.flags) || 0;
  if (toFix > 0) {
    const fix = document.createElement('button');
    fix.className = 'primary';
    fix.textContent = `Resolve flagged corrections (${toFix})`;
    fix.addEventListener('click', () => openCorrectionsFix(job));
    actions.append(fix);
  }
  actions.append(openButton(job, 'corrections-notes',
    WEB ? 'Download the report (Markdown)' : 'Open the report notes', note,
    { quiet: true }));
  // The read-only InDesign check tour: a script that walks the designer to each
  // composition check and open flag in the live document — the one thing no file
  // comparison can settle. Offered only when the run left something to walk to.
  // A plain hand-over: WEB downloads the .jsx, desktop reveals it so it can be
  // installed into the Scripts panel and run with the corrected file open.
  const tourItems = (job.checks || 0)
    + ((job.total_comments ? job.unresolved : job.flags) || 0);
  if (tourItems > 0) {
    const tour = openButton(job, 'check-tour',
      WEB ? 'Download the InDesign check tour (JSX)'
          : 'Show the InDesign check tour in Finder', note,
      { quiet: true, reveal: !WEB });
    tour.title = 'A read-only InDesign script: run it with the corrected file '
      + 'open and it selects you onto each composition check and open flag in '
      + 'turn. It changes nothing.';
    actions.append(tour);
  }
  if (!WEB) {
    actions.append(
      openButton(job, 'corrected', 'Show in Finder', note, { reveal: true }));
  }

  // Count in one unit so the parts sum to the whole. From a proof that is the
  // comments — reviewer comments = applied + no change needed + need a human — so
  // the card no longer sets an edit count (535) beside a comment count (560) as if
  // they should add up. The edit tally lives in the report's sub-line instead. A
  // typed list has no comments, so there the unit is the edit.
  const bits = [];
  if (typeof job.total_comments === 'number' && job.total_comments) {
    const total = job.total_comments;
    const human = job.unresolved || 0;
    bits.push(`${total} comment${total === 1 ? '' : 's'}`);
    if (typeof job.applied_comments === 'number') {
      const noChange = Math.max(0, total - job.applied_comments - human);
      bits.push(`${job.applied_comments} applied`);
      if (noChange) bits.push(`${noChange} no change needed`);
    } else if (typeof job.applied === 'number') {
      // A job finished before comment-level counts were stored: show the edit
      // count, labelled as edits so it is not misread as a comment tally.
      bits.push(`${job.applied} edit${job.applied === 1 ? '' : 's'} applied`);
    }
    if (human) bits.push(`${human} need a human`);
  } else {
    if (typeof job.applied === 'number') bits.push(`${job.applied} applied`);
    if (job.flags) bits.push(`${job.flags} for a human`);
  }
  if (job.discrepancies) {
    bits.push(`${job.discrepancies} unaccounted change`
      + `${job.discrepancies === 1 ? '' : 's'}`);
  }
  if (job.results_name) bits.push(`saved in “${job.results_name}”`);
  if (bits.length) {
    const meta = document.createElement('span');
    meta.className = 'file-meta';
    meta.textContent = bits.join(' · ');
    actions.append(meta);
  }
  driveActions(actions, note, job);
  wrap.append(actions, note);

  const where = document.createElement('p');
  where.className = 'where';
  where.textContent = job.verified
    ? 'Every correction landed exactly, and nothing else in the file changed — '
      + 'checked word for word, not assumed. Read the corrections report to see '
      + 'each applied change in its sentence, then open the corrected .idml in '
      + 'InDesign; it reflows on open.'
    : 'Heads up: some corrections were refused, or the file changed in ways the '
      + 'list did not ask for. Read the report — it shows every applied change in '
      + 'context and every unaccounted one — before passing it on.';
  wrap.append(where);
  return wrap;
}

async function openCorrectionsReport(job) {
  let d;
  try {
    d = await api(`/api/jobs/${job.id}/corrections`);
  } catch (err) {
    alert(`Couldn't load the corrections report: ${err.message}`);
    return;
  }
  const url = URL.createObjectURL(
    new Blob([correctionsReportHTML(d)], { type: 'text/html' }));
  if (!window.open(url, '_blank')) {
    const a = document.createElement('a');
    a.href = url;
    a.download = `corrections - ${d.source_name || 'file'}.html`;
    document.body.append(a);
    a.click();
    a.remove();
  }
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

// ── resolving flagged corrections in place ─────────────────────────────────
// The fix screen: every flag the run left for a person, dealt with without
// leaving the app. Each card offers, in order of least effort: the model's own
// suggestion (stored advice applies in one click; any flag can ask for one and
// accept the highlighted preview), the concrete placements the change could
// land on (one click each), the line itself opened for hand-editing (bold,
// italics, section breaks, the proposed change pre-applied and highlighted),
// and a box to type a decision for the model to carry out. "Ignore" sets a
// flag aside, recorded and reversible. The applied corrections are reviewable
// and editable in their own section, recorded as touch-ups. Every action edits
// the corrected IDML on the server in place; the bar at the top hands the file
// back.

$('fix-back').addEventListener('click', () => show('jobs'));

const FIX_STATUS = {
  not_found: 'the text to change was not found',
  ambiguous: 'the text appears more than once',
  crosses_paragraph: 'the change would span a paragraph break',
  routed_to_design: 'a layout request, not a text edit',
  overlaps: 'two corrections land on the same words',
  withheld: 'held back for a human',
  unstyleable: 'the formatting could not be applied here',
  unplaceable: 'the paragraph could not be given a range of its own',
  off_page: 'the text is not on the page it was marked on',
  no_change: 'a query — no concrete change was proposed',
  not_extracted: 'no edit was made for this comment',
  flagged: 'flagged for a human',
};

// What a manual save did to the section breaks around its line, as one clause.
function breakNotes(b) {
  if (!b) return '';
  const bits = [];
  if (b.added_after) bits.push('added a section break after it');
  if (b.removed_above) bits.push('removed the break above it');
  if (b.removed_below) bits.push('removed the break below it');
  return bits.join(' and ');
}

// A window around what changed, so a paragraph-long preview reads as the line
// the designer has to eye rather than a wall of text.
function fixPreview(before, after) {
  const KEEP = 70;
  let p = 0;
  while (p < before.length && p < after.length && before[p] === after[p]) p += 1;
  let s = 0;
  while (s < before.length - p && s < after.length - p
         && before[before.length - 1 - s] === after[after.length - 1 - s]) s += 1;
  const lead = Math.max(0, p - KEEP);
  const cut = (t) => {
    const end = Math.min(t.length, t.length - s + KEEP);
    return (lead ? '…' : '') + t.slice(lead, end) + (end < t.length ? '…' : '');
  };
  return [cut(before), cut(after)];
}

// The span a change occupies in its after-text — common prefix and suffix off.
// [x, x) (empty) when the two read identically (a formatting-only change).
function changedSpan(before, after) {
  let p = 0;
  while (p < before.length && p < after.length && before[p] === after[p]) p += 1;
  let s = 0;
  while (s < before.length - p && s < after.length - p
         && before[before.length - 1 - s] === after[after.length - 1 - s]) s += 1;
  return [p, after.length - s];
}

// The page label a designer navigates by: folio first, physical page second.
function fixLoc(row) {
  return row.page_label ? `Page ${row.page_label}`
    : row.page ? `Page ${row.page}` : `¶ ${row.paragraph}`;
}

async function openCorrectionsFix(job) {
  show('fix');
  $('fix-title').textContent = `Resolve flagged corrections — ${job.filename}`;
  $('fix-sub').textContent = 'Reading the report…';
  $('fix-list').textContent = '';
  let data;
  try {
    data = await api(`/api/jobs/${job.id}/corrections`);
  } catch (err) {
    $('fix-sub').textContent = `Couldn't load the corrections report: `
      + err.message;
    return;
  }
  if (!(data.queue || []).length && !(data.changes || []).length) {
    $('fix-sub').textContent = (job.total_comments ? job.unresolved : job.flags)
      ? 'This run predates in-place resolutions — re-run the corrections and '
        + 'the flags will come back resolvable here.'
      : 'Nothing here to review.';
    return;
  }
  renderFixList(job, data);
}

function renderFixList(job, data) {
  const list = $('fix-list');
  list.textContent = '';
  const queue = data.queue || [];
  const open = queue.filter((q) => !q.resolved);
  const done = queue.filter((q) => q.resolved
    && q.resolved.kind !== 'dismissed');
  const aside = queue.filter((q) => q.resolved
    && q.resolved.kind === 'dismissed');

  // Which page numbers this screen speaks in, said once. A run that cited
  // pages but carried none at all gets the loud version — the flags below are
  // mostly the *consequence* of that, and the fix is a re-read, not a
  // click-through.
  const paged = queue.some((q) => q.page);
  const labeled = queue.some((q) => q.page_label);
  const pgs = data.pages || {};
  $('fix-sub').textContent = (!pgs.total && pgs.cited)
    ? 'No proof pages accompanied this run — marks lost their page narrowing, '
      + 'which is why repeated text flagged. Re-reading the marked proof '
      + 'attaches its pages.'
    : (paged && !labeled)
      ? 'Page numbers are the proof PDF’s own — the book’s folios could not be '
        + 'aligned for this run.'
      : '';

  // The working bar sticks below the header, so make sure the header height it
  // pins to is current — the header may have wrapped since load, and a resize
  // event is not guaranteed to have fired.
  updateHeaderHeight();
  // How far along, and the file itself once — or while — you're at it.
  // Downloading mid-way is fine; it simply carries what's resolved so far.
  const bar = document.createElement('div');
  bar.className = 'fix-bar';
  const progress = document.createElement('span');
  progress.className = 'fix-progress';
  const dealt = done.length + aside.length;
  progress.textContent = open.length
    ? `${dealt} of ${queue.length} dealt with · ${open.length} to go`
    : queue.length
      ? `All ${queue.length} dealt with — the corrected file is ready.`
      : 'No flags — every correction below applied.';
  bar.append(progress);
  const note = actionNote();
  const dl = openButton(job, 'corrected',
    WEB ? 'Download the corrected file' : 'Open the corrected file in InDesign',
    note, { quiet: open.length > 0 });
  if (!open.length) dl.className = 'primary';
  bar.append(dl);
  list.append(bar, note);

  list.append(fixAgentPanel(job, data));

  open.forEach((item) => list.append(fixItemCard(job, data, item)));

  // Every correction the run applied, reviewable and editable — the machine
  // proposed these; the last word is the designer's. Edits here are recorded
  // as touch-ups, resolving nothing and counted as nothing else.
  const applied = (data.changes || []).filter((c) => (c.after || '').trim());
  if (applied.length) {
    const det = document.createElement('details');
    det.className = 'fix-applied';
    const sum = document.createElement('summary');
    sum.textContent = `Applied corrections — review or edit (${applied.length})`;
    det.append(sum);
    applied.forEach((c) => det.append(fixAppliedRow(job, data, c)));
    list.append(det);
  }

  if (done.length) {
    const h = document.createElement('h3');
    h.textContent = `Resolved here — ${done.length}`;
    list.append(h);
    done.forEach((item) => list.append(fixItemCard(job, data, item)));
  }
  if (aside.length) {
    const h = document.createElement('h3');
    h.textContent = `Set aside — ${aside.length}`;
    list.append(h);
    aside.forEach((item) => list.append(fixItemCard(job, data, item)));
  }
}

// One resolve call, shared by the card's actions and the manual editor: on
// success the queue entry is swapped for the server's copy and the list
// re-renders; on failure the caller shows the sentence.
async function fixResolve(job, data, item, body, onError) {
  try {
    const out = await api(`/api/jobs/${job.id}/corrections/resolve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ item_id: item.id, ...body }),
    });
    const i = (data.queue || []).findIndex((q) => q.id === item.id);
    if (i >= 0 && out.item) data.queue[i] = out.item;
    renderFixList(job, data);
    return true;
  } catch (e) {
    onError(e.message);
    return false;
  }
}

// The run-level agent: a conversation about the whole book, not one flag. The
// designer brings a fresh request ("make every 'grey' British-spelled", "check
// the captain is always 'sir'") and the model works it out against the book —
// searching, proposing exact edits, raising notes. Nothing is written until the
// designer accepts a proposal, and every accept goes through the same
// deterministic placement a clicked option does, so the agent only ever
// proposes. The thread, its pending proposals, and its notes live in the report,
// so a reload picks the conversation up where it was left.
function fixAgentPanel(job, data) {
  const panel = document.createElement('details');
  panel.className = 'fix-agent';
  const agent = () => (data.agent || { chat: [], proposals: [], flags: [] });
  panel.open = !!(agent().chat && agent().chat.length);
  const sum = document.createElement('summary');
  sum.textContent = 'Ask the agent about the whole book';
  panel.append(sum);

  const intro = document.createElement('p');
  intro.className = 'muted fix-agent-intro';
  intro.textContent = 'Bring a request about the whole book — a consistency '
    + 'sweep, a spelling convention, a question. The agent searches the book '
    + 'and proposes exact edits you accept or reject; nothing changes until you '
    + 'do.';
  const thread = document.createElement('div');
  thread.className = 'fix-chat-thread';
  const proposals = document.createElement('div');
  proposals.className = 'fix-agent-proposals';
  const notes = document.createElement('div');
  notes.className = 'fix-agent-notes';
  const err = document.createElement('p');
  err.className = 'fix-error';
  err.hidden = true;

  const rowEl = document.createElement('div');
  rowEl.className = 'fix-chat-row';
  const input = document.createElement('input');
  input.type = 'text';
  input.placeholder = 'e.g. “make every ‘grey’ the British ‘grey’ (not ‘gray’)”';
  const send = document.createElement('button');
  send.textContent = 'Send';
  rowEl.append(input, send);

  const setBusy = (on, label) => {
    input.disabled = on;
    send.disabled = on;
    panel.querySelectorAll('.fix-agent-proposals button, .fix-agent-accept-all')
      .forEach((b) => { b.disabled = on; });
    send.textContent = on ? (label || 'Thinking…') : 'Send';
  };

  const rebuild = () => {
    const a = agent();
    thread.textContent = '';
    (a.chat || []).forEach((t) => {
      const line = document.createElement('p');
      line.className = t.role === 'designer'
        ? 'fix-chat-msg is-designer' : 'fix-chat-msg is-model';
      line.textContent = (t.role === 'designer' ? 'You: ' : 'Agent: ')
        + (t.text || '');
      thread.append(line);
    });

    proposals.textContent = '';
    const pend = a.proposals || [];
    if (pend.length) {
      const h = document.createElement('div');
      h.className = 'fix-agent-proposals-head';
      const label = document.createElement('b');
      label.textContent = `Proposed changes — ${pend.length}`;
      h.append(label);
      if (pend.length > 1) {
        const all = document.createElement('button');
        all.className = 'primary fix-agent-accept-all';
        all.textContent = `Accept all ${pend.length}`;
        all.addEventListener('click', () => doAccept({ accept_all: true },
          'Applying…'));
        h.append(all);
      }
      proposals.append(h);
      pend.forEach((p) => {
        const box = document.createElement('div');
        box.className = 'fix-agent-proposal';
        if (p.why) {
          const why = document.createElement('p');
          why.className = 'muted fix-chat-cap';
          why.textContent = p.why;
          box.append(why);
        }
        box.append(fixRedline(p.before, p.after));
        const accept = document.createElement('button');
        accept.className = 'primary';
        accept.textContent = 'Accept';
        accept.addEventListener('click', () => doAccept({ accept_id: p.id },
          'Applying…'));
        const drop = document.createElement('button');
        drop.className = 'quiet';
        drop.textContent = 'Dismiss';
        drop.addEventListener('click', () => doAccept({ dismiss_id: p.id }));
        box.append(accept, drop);
        proposals.append(box);
      });
    }

    notes.textContent = '';
    const flags = a.flags || [];
    // Grouped the way the report groups them: layout the agent can't do in the
    // text (a checklist), things blocked pending input, and plain notes.
    const groups = [
      ['task', 'To do in InDesign', 'Layout the agent can’t do in the text — '
        + 'do these by hand.'],
      ['hold', 'Waiting on', 'Blocked pending something you supply or decide.'],
      ['note', 'The agent flagged for you', ''],
    ];
    groups.forEach(([cat, title, blurb]) => {
      const items = flags.filter((f) => (f.category || 'note') === cat);
      if (!items.length) return;
      const h = document.createElement('p');
      h.className = 'fix-agent-notes-head';
      h.textContent = `${title} — ${items.length}`;
      notes.append(h);
      if (blurb) {
        const b = document.createElement('p');
        b.className = 'muted fix-agent-note';
        b.textContent = blurb;
        notes.append(b);
      }
      items.forEach((f) => {
        const line = document.createElement('p');
        line.className = 'fix-agent-note';
        line.textContent = `• ${f.note || f.why || ''}`
          + (f.quote ? ` — on “${f.quote}”` : '');
        notes.append(line);
      });
    });
  };

  async function post(body, busyLabel) {
    err.hidden = true;
    setBusy(true, busyLabel);
    try {
      return await api(`/api/jobs/${job.id}/corrections/agent`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } catch (e) {
      setBusy(false);
      err.textContent = e.message;
      err.hidden = false;
      return null;
    }
  }

  async function doSend() {
    const text = input.value.trim();
    if (!text) { input.focus(); return; }
    const out = await post({ text });
    if (!out) return;
    data.agent = out.agent;
    input.value = '';
    setBusy(false);
    rebuild();
    input.focus();
  }

  async function doAccept(body, label) {
    const out = await post(body, label);
    if (!out) return;
    data.agent = out.agent;
    setBusy(false);
    rebuild();
    if (out.failed && out.failed.length) {
      err.textContent = `${out.failed.length} couldn’t be applied — the line `
        + 'may have changed since. ' + out.failed.map((f) => f.why).join('; ');
      err.hidden = false;
    }
  }

  send.addEventListener('click', doSend);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSend(); });

  rebuild();
  panel.append(intro, thread, proposals, notes, rowEl, err);
  return panel;
}

function fixItemCard(job, data, item) {
  const card = document.createElement('div');
  card.className = 'fix-item';

  const err = document.createElement('p');
  err.className = 'fix-error';
  err.hidden = true;

  const busy = (on) => {
    card.querySelectorAll('button, input').forEach((el) => {
      el.disabled = on;
    });
    card.classList.toggle('is-busy', on);
  };

  const resolve = async (body) => {
    err.hidden = true;
    busy(true);
    const ok = await fixResolve(job, data, item, body, (msg) => {
      busy(false);
      err.textContent = msg;
      err.hidden = false;
    });
    return ok;
  };

  const head = document.createElement('div');
  head.className = 'fix-head';
  const where = document.createElement('b');
  // The page a designer navigates to: the InDesign folio when the run aligned
  // one, otherwise the proof's physical page — same rule as the report. The
  // physical page rides along when the two differ, so the mark can still be
  // found in the proof PDF (and runs archived before v0.99.9 wrote flag
  // details in it).
  where.textContent = item.page_label
    ? `Page ${item.page_label}`
      + (item.page && String(item.page) !== item.page_label
        ? ` (proof PDF p. ${item.page})` : '')
    : item.page ? `Page ${item.page}` : 'No page cited';
  head.append(where);
  const why = document.createElement('span');
  why.className = 'muted';
  why.textContent = ` — ${FIX_STATUS[item.status] || item.status || 'flagged'}`;
  head.append(why);
  if (!item.resolved) {
    const ignore = document.createElement('button');
    ignore.className = 'quiet fix-ignore';
    ignore.textContent = 'Ignore';
    ignore.title = 'Leave it as set (or handle it in InDesign) — recorded as '
      + 'set aside, and it can be put back';
    ignore.addEventListener('click', () => resolve({ dismiss: true }));
    head.append(ignore);
  }
  card.append(head);

  if (item.instruction) {
    const note = document.createElement('blockquote');
    note.className = 'fix-note';
    note.textContent = `“${item.instruction}”`;
    card.append(note);
  }
  if (item.anchor) {
    const anchor = document.createElement('p');
    anchor.className = 'muted fix-anchor';
    anchor.textContent = `Marked on: “${item.anchor}”`;
    card.append(anchor);
  }
  if (item.why && item.why !== FIX_STATUS[item.status]) {
    const detail = document.createElement('p');
    detail.className = 'muted fix-anchor';
    detail.textContent = item.why;
    card.append(detail);
  }

  if (item.resolved) {
    const r = item.resolved;
    if (r.kind === 'dismissed') {
      const doneLine = document.createElement('p');
      doneLine.className = 'muted fix-done-aside';
      doneLine.textContent = '⊘ Set aside — you chose to leave this as set. '
        + 'Nothing was changed.';
      const back = document.createElement('button');
      back.className = 'quiet';
      back.textContent = 'Put it back';
      back.addEventListener('click', () => resolve({ reopen: true }));
      card.append(doneLine, back, err);
      return card;
    }
    const doneLine = document.createElement('p');
    doneLine.className = 'fix-done';
    doneLine.textContent = '✓ Resolved — '
      + (r.kind === 'manual' ? 'you edited the line yourself'
        : r.kind === 'suggestion' ? 'you applied the model’s suggestion'
          : r.kind === 'no_change'
            ? 'confirmed correct as set — no change needed'
            : r.kind === 'chat'
              ? `you talked it through${r.text ? `: “${r.text}”` : ''}`
              : r.kind === 'typed' ? `you typed: “${r.text}”`
                : 'you picked a placement') + (r.note ? ` (${r.note})` : '');
    card.append(doneLine);
    // A no-change settlement rewrote nothing, so there is no redline to show.
    if (r.kind === 'no_change') {
      card.append(err);
      return card;
    }
    if (r.removed_line) {
      const rl = document.createElement('p');
      rl.className = 'rl fix-rl';
      rl.innerHTML = `<del>${esc(fixPreview(r.before || '', '')[0])}</del> `
        + '<span class="muted">(the emptied line was removed)</span>';
      card.append(rl);
    } else {
      card.append(fixRedline(r.before, r.after, r.format));
    }
    const breaks = breakNotes(r.breaks);
    if (breaks) {
      const b = document.createElement('p');
      b.className = 'muted fix-anchor';
      b.textContent = `And ${breaks}.`;
      card.append(b);
    }
    return card;
  }

  // A model pass already studied this flag and wrote what it would do — offer
  // carrying that out as one click, through the same typed path, so a good
  // suggestion is a button rather than a retype.
  if (item.advice) {
    const row = document.createElement('div');
    row.className = 'fix-advice';
    const text = document.createElement('p');
    text.className = 'fix-advice-text';
    text.textContent = `The model suggests: ${item.advice}`;
    const go = document.createElement('button');
    go.className = 'primary';
    go.textContent = 'Apply the suggestion';
    go.addEventListener('click', () => resolve({ suggestion: true }));
    row.append(text, go);
    card.append(row);
  }

  const editButton = (loc, preApply) => {
    const b = document.createElement('button');
    b.className = 'quiet';
    b.textContent = 'Edit the line';
    b.title = 'Open this line and edit it yourself — bold, italics and '
      + 'section breaks included' + (preApply
      ? '; the proposed change opens pre-applied, highlighted green' : '');
    b.addEventListener('click', () => openManualEditor(job, data, {
      item,
      loc,
      expect: loc.before || '',
      preApply,
      container: card,
    }));
    return b;
  };

  (item.options || []).forEach((o) => {
    const opt = document.createElement('div');
    opt.className = 'fix-opt';
    // A suggestion whose verdict was "leave": the model finds the text correct
    // as set. There is nothing to place — accepting it settles the flag as a
    // no-change, so it gets its own row, not a redline and an "Apply here".
    if (o.no_change) {
      const line = document.createElement('div');
      line.className = 'fix-opt-loc';
      line.textContent = '✨ Model’s pick · nothing to change — the text is '
        + 'correct as set' + (o.note ? ` (${o.note})` : '');
      const accept = document.createElement('button');
      accept.className = 'primary';
      accept.textContent = 'Accept — no change';
      accept.addEventListener('click', () => resolve({ option_id: o.id }));
      opt.append(line, accept);
      card.append(opt);
      return;
    }
    const loc = document.createElement('div');
    loc.className = 'fix-opt-loc';
    loc.textContent = (o.suggested ? '✨ Model’s pick · ' : '') + fixLoc(o);
    if (o.suggested && o.note) loc.title = o.note;
    opt.append(loc);
    opt.append(fixRedline(o.before, o.after));
    const apply = document.createElement('button');
    apply.textContent = 'Apply here';
    if (o.suggested) apply.className = 'primary';
    apply.addEventListener('click', () => resolve({ option_id: o.id }));
    opt.append(apply, editButton(o, {
      start: o.start, end: o.end, found: o.found,
      replacement: o.replacement,
    }));
    card.append(opt);
  });

  // Where the editor can open when there is nothing to click — the line the
  // flag was located in, or the line carrying the marked text.
  (item.targets || []).forEach((t) => {
    const row = document.createElement('div');
    row.className = 'fix-opt';
    const loc = document.createElement('div');
    loc.className = 'fix-opt-loc';
    loc.textContent = fixLoc(t);
    row.append(loc);
    row.append(fixRedline(t.before, t.before));
    row.append(editButton(t, null));
    card.append(row);
  });

  const nl = document.createElement('div');
  nl.className = 'fix-typed';
  // Any flag can ask the model for a fix; it comes back as a highlighted,
  // clickable placement to accept — never applied sight-unseen. Hidden once a
  // suggestion is already on the card, and when the advice banner (a stronger
  // one-click) is there instead.
  if (!item.advice && !(item.options || []).some((o) => o.suggested)) {
    const suggest = document.createElement('button');
    suggest.className = 'quiet';
    suggest.textContent = 'Suggest a fix';
    suggest.title = 'Ask the model to work this one out — its fix appears '
      + 'above as a placement for you to accept or edit';
    suggest.addEventListener('click', async () => {
      err.hidden = true;
      busy(true);
      suggest.textContent = 'Asking…';
      try {
        const out = await api(`/api/jobs/${job.id}/corrections/suggest`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ item_id: item.id }),
        });
        const i = (data.queue || []).findIndex((q) => q.id === item.id);
        if (i >= 0 && out.item) data.queue[i] = out.item;
        renderFixList(job, data);
      } catch (e) {
        busy(false);
        suggest.textContent = 'Suggest a fix';
        err.textContent = e.message;
        err.hidden = false;
      }
    });
    nl.append(suggest);
  }
  const input = document.createElement('input');
  input.type = 'text';
  input.placeholder = (item.options || []).length
    ? 'Or type what to do instead — e.g. “use the em dash”, “change the '
      + 'second one”'
    : 'Type what to do — e.g. “change ‘teh’ to ‘the’ in this line”, “use the '
      + 'em dash”';
  const go = document.createElement('button');
  go.textContent = 'Make this change';
  const submit = () => {
    const text = input.value.trim();
    if (!text) {
      err.textContent = 'Say what to do first.';
      err.hidden = false;
      return;
    }
    resolve({ text });
  };
  go.addEventListener('click', submit);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submit();
  });
  nl.append(input, go);
  card.append(nl, fixChatPanel(job, data, item), err);
  return card;
}

// A per-flag conversation with the model: the free-form escalation for a flag a
// single typed answer can't settle. Each message re-runs adjudication with the
// whole thread, so "no, the other one — and italicise it" resolves against what
// was proposed before; the model answers with a highlighted proposal the
// designer accepts or keeps refining. Nothing is written until Accept. The
// thread lives in the report, so it survives a reload. Self-contained — it owns
// its own busy and error state and updates itself in place, only re-rendering
// the whole list on the terminal Accept (which moves the flag to Resolved).
function fixChatPanel(job, data, item) {
  const panel = document.createElement('details');
  panel.className = 'fix-chat';
  // Open when a conversation is already under way, so a reload shows it.
  panel.open = !!(item.chat && item.chat.length);
  const sum = document.createElement('summary');
  sum.textContent = 'Talk it through with the model';
  panel.append(sum);

  const thread = document.createElement('div');
  thread.className = 'fix-chat-thread';
  const proposalBox = document.createElement('div');
  proposalBox.className = 'fix-chat-proposal';
  const err = document.createElement('p');
  err.className = 'fix-error';
  err.hidden = true;

  const row = document.createElement('div');
  row.className = 'fix-chat-row';
  const input = document.createElement('input');
  input.type = 'text';
  input.placeholder = 'Say what to do — e.g. “no, the other one — and '
    + 'italicise it”';
  const send = document.createElement('button');
  send.textContent = 'Send';
  row.append(input, send);

  const setBusy = (on, label) => {
    input.disabled = on;
    send.disabled = on;
    proposalBox.querySelectorAll('button').forEach((b) => { b.disabled = on; });
    send.textContent = on ? (label || 'Sending…') : 'Send';
  };

  // Redraw the transcript and the current proposal from the item, in place —
  // no full-list re-render, so the input keeps focus across turns.
  const rebuild = () => {
    thread.textContent = '';
    (item.chat || []).forEach((t) => {
      const line = document.createElement('p');
      line.className = t.role === 'designer'
        ? 'fix-chat-msg is-designer' : 'fix-chat-msg is-model';
      line.textContent = (t.role === 'designer' ? 'You: ' : 'Model: ')
        + (t.text || '');
      thread.append(line);
    });
    proposalBox.textContent = '';
    if (item.proposal) {
      const cap = document.createElement('p');
      cap.className = 'muted fix-chat-cap';
      cap.textContent = 'Proposed change'
        + (item.proposal.note ? ` — ${item.proposal.note}` : '');
      proposalBox.append(cap,
        fixRedline(item.proposal.before, item.proposal.after));
      const accept = document.createElement('button');
      accept.className = 'primary';
      accept.textContent = 'Accept this change';
      accept.addEventListener('click', doAccept);
      proposalBox.append(accept);
    }
  };

  async function turn(body, busyLabel) {
    err.hidden = true;
    setBusy(true, busyLabel);
    try {
      const out = await api(`/api/jobs/${job.id}/corrections/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ item_id: item.id, ...body }),
      });
      return out;
    } catch (e) {
      setBusy(false);
      err.textContent = e.message;
      err.hidden = false;
      return null;
    }
  }

  async function doSend() {
    const text = input.value.trim();
    if (!text) { input.focus(); return; }
    const out = await turn({ text });
    if (!out) return;
    if (out.item) Object.assign(item, out.item);
    input.value = '';
    setBusy(false);
    rebuild();
    input.focus();
  }

  async function doAccept() {
    const out = await turn({ accept: true }, 'Applying…');
    if (!out) return;
    // The flag is resolved now and moves to "Resolved here" — a full re-render
    // is what carries it there.
    const i = (data.queue || []).findIndex((q) => q.id === item.id);
    if (i >= 0 && out.item) data.queue[i] = out.item;
    renderFixList(job, data);
  }

  send.addEventListener('click', doSend);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSend(); });

  rebuild();
  panel.append(thread, proposalBox, row, err);
  return panel;
}

// One applied correction, reviewable in its line and editable — recorded as a
// touch-up, resolving nothing.
function fixAppliedRow(job, data, c) {
  const row = document.createElement('div');
  row.className = 'fix-opt';
  const loc = document.createElement('div');
  loc.className = 'fix-opt-loc';
  loc.textContent = `¶ ${c.paragraph}`;
  row.append(loc);
  row.append(fixRedline(c.before, c.after, c.formatting));
  const edit = document.createElement('button');
  edit.className = 'quiet';
  edit.textContent = 'Edit the line';
  edit.title = 'Open this line and adjust it yourself — the applied change is '
    + 'highlighted green';
  edit.addEventListener('click', () => openManualEditor(job, data, {
    item: null,
    loc: { story_id: c.story_id, paragraph: c.paragraph },
    expect: c.after || '',
    highlight: changedSpan(c.before || '', c.after || ''),
    instruction: c.instruction || '',
    container: row,
  }));
  row.append(edit);
  return row;
}

// A redline that opens as a window around the change and expands to the whole
// paragraph on click — judging a fix often needs the full line, and the full
// line does not fit a list. Only clickable when there is more to see. Called
// with before === after it renders the line plain, no marks.
function fixRedline(before, after, format) {
  const rl = document.createElement('p');
  rl.className = 'rl fix-rl';
  const [b, a] = fixPreview(before || '', after || '');
  const trimmed = b !== (before || '') || a !== (after || '');
  const draw = (full) => {
    rl.innerHTML = wordDiff(full ? (before || '') : b, full ? (after || '') : a);
    if (format) {
      rl.innerHTML += ` <span class="muted">— set ${esc(format)}</span>`;
    }
    rl.dataset.full = full ? '1' : '';
    rl.title = !trimmed ? ''
      : full ? 'Click to trim back to the change'
        : 'Click to see the whole paragraph';
  };
  draw(false);
  if (trimmed) {
    rl.classList.add('fix-expandable');
    rl.addEventListener('click', () => draw(!rl.dataset.full));
  }
  return rl;
}

// ── the manual editor ──────────────────────────────────────────────────────
// The line itself, opened in place of its card or row: edit the words
// directly, bold or italicize a selection, tick a section break on or off,
// and Save writes exactly that into the corrected IDML. Opened from a
// placement, the proposed change arrives pre-applied and highlighted green —
// accept it by saving, or adjust it first. Opened from an applied correction,
// the change already in the file is what glows.

async function openManualEditor(job, data, ctx) {
  const showErr = (msg) => {
    let err = ctx.container.querySelector('.fix-error');
    if (!err) {
      err = document.createElement('p');
      err.className = 'fix-error';
      ctx.container.append(err);
    }
    err.textContent = msg;
    err.hidden = false;
  };
  let state;
  try {
    state = await api(`/api/jobs/${job.id}/corrections/paragraph`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ story_id: ctx.loc.story_id,
                             paragraph: ctx.loc.paragraph,
                             expect: ctx.expect || '' }),
    });
  } catch (e) {
    showErr(e.message);
    return;
  }
  const card = ctx.container;
  card.textContent = '';
  card.className = 'fix-item is-editing';

  const model = editorModel(state, ctx.preApply || null, ctx.highlight || null);

  const head = document.createElement('div');
  head.className = 'fix-head';
  const title = document.createElement('b');
  title.textContent = `Edit the line — ${fixLoc({ ...ctx.loc,
    paragraph: state.paragraph })}`;
  head.append(title);
  card.append(head);
  const instruction = ctx.item ? ctx.item.instruction : ctx.instruction;
  if (instruction) {
    const note = document.createElement('blockquote');
    note.className = 'fix-note';
    note.textContent = `“${instruction}”`;
    card.append(note);
  }

  const bar = document.createElement('div');
  bar.className = 'fix-edit-bar';
  const tool = (label, cmd, hint) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'quiet';
    b.innerHTML = label;
    b.title = hint;
    // mousedown, and swallowed, so the selection in the box survives the click.
    b.addEventListener('mousedown', (e) => {
      e.preventDefault();
      document.execCommand(cmd);
    });
    return b;
  };
  bar.append(tool('<b>B</b>', 'bold', 'Bold the selection'),
             tool('<i>I</i>', 'italic', 'Italicize the selection'));
  const hint = document.createElement('span');
  hint.className = 'muted fix-edit-hint';
  hint.textContent = 'Select text, then B or I. One paragraph — breaks are '
    + 'the boxes below.'
    + (model.hl ? ' The proposed change is highlighted green.' : '');
  bar.append(hint);
  card.append(bar);

  const box = document.createElement('div');
  box.className = 'fix-editor';
  box.contentEditable = 'true';
  box.spellcheck = false;
  box.innerHTML = editorHTML(model.text, model.chars, model.hl);
  // One paragraph stays one paragraph: Enter is refused (section breaks are
  // the checkboxes), and a paste lands as plain text.
  box.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') e.preventDefault();
  });
  box.addEventListener('paste', (e) => {
    e.preventDefault();
    const text = (e.clipboardData || window.clipboardData)
      .getData('text/plain').replace(/\s*\n\s*/g, ' ');
    document.execCommand('insertText', false, text);
  });
  card.append(box);

  const checks = document.createElement('div');
  checks.className = 'fix-edit-checks';
  const check = (label) => {
    const wrap = document.createElement('label');
    wrap.className = 'fix-check-row';
    const c = document.createElement('input');
    c.type = 'checkbox';
    wrap.append(c, document.createTextNode(` ${label}`));
    checks.append(wrap);
    return c;
  };
  const addAfter = check('Add a section break (blank line) after this '
    + 'paragraph');
  const removeAbove = state.prev_break
    ? check('Remove the section break above') : null;
  const removeBelow = state.next_break
    ? check('Remove the section break below') : null;
  if (removeBelow) {
    // Adding a break below and removing the one below contradict.
    addAfter.addEventListener('change', () => {
      if (addAfter.checked) removeBelow.checked = false;
    });
    removeBelow.addEventListener('change', () => {
      if (removeBelow.checked) addAfter.checked = false;
    });
  }
  card.append(checks);

  const err = document.createElement('p');
  err.className = 'fix-error';
  err.hidden = true;

  const row = document.createElement('div');
  row.className = 'fix-typed';
  const save = document.createElement('button');
  save.className = 'primary';
  save.textContent = 'Save & apply';
  const cancel = document.createElement('button');
  cancel.className = 'quiet';
  cancel.textContent = 'Cancel';
  cancel.addEventListener('click', () => renderFixList(job, data));
  save.addEventListener('click', async () => {
    const parsed = parseEditor(box, state.text);
    err.hidden = true;
    save.disabled = cancel.disabled = true;
    save.textContent = 'Applying…';
    const fail = (msg) => {
      err.textContent = msg;
      err.hidden = false;
      save.disabled = cancel.disabled = false;
      save.textContent = 'Save & apply';
    };
    const manual = {
      story_id: ctx.loc.story_id,
      paragraph: state.paragraph,
      expected: state.text,
      text: parsed.text,
      runs: parsed.runs,
      insert_break_after: addAfter.checked,
      remove_break_above: !!(removeAbove && removeAbove.checked),
      remove_break_below: !!(removeBelow && removeBelow.checked),
    };
    if (ctx.item) {
      await fixResolve(job, data, ctx.item, { manual }, fail);
      return;
    }
    // A touch-up: no flag answered, so it goes through the edit route and the
    // fresh report is re-read for the re-render.
    try {
      await api(`/api/jobs/${job.id}/corrections/edit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ manual }),
      });
      renderFixList(job, await api(`/api/jobs/${job.id}/corrections`));
    } catch (e) {
      fail(e.message);
    }
  });
  row.append(save, cancel);
  card.append(row, err);
  box.focus();
}

// The editor's working model: per-character bold/italic states off the server
// runs, the proposed change spliced in (inheriting the state where it lands)
// when a placement opened the editor, and the green-highlight span. A
// placement whose span no longer matches is relocated by its text when that
// text now appears exactly once; otherwise the editor opens plain.
function editorModel(state, preApply, highlight) {
  let text = state.text;
  const chars = [];
  for (let i = 0; i < text.length; i += 1) {
    chars.push({ bold: false, italic: false });
  }
  (state.runs || []).forEach((r) => {
    for (let i = r.start; i < r.end && i < chars.length; i += 1) {
      chars[i] = { bold: !!r.bold, italic: !!r.italic };
    }
  });
  let hl = null;
  if (preApply) {
    let { start, end } = preApply;
    if (text.slice(start, end) !== preApply.found) {
      const first = text.indexOf(preApply.found);
      if (first >= 0 && text.indexOf(preApply.found, first + 1) < 0) {
        start = first;
        end = first + preApply.found.length;
      } else {
        return { text, chars, hl: null };
      }
    }
    const inherit = chars[start] || { bold: false, italic: false };
    const insert = Array.from({ length: preApply.replacement.length },
                              () => ({ ...inherit }));
    chars.splice(start, end - start, ...insert);
    text = text.slice(0, start) + preApply.replacement + text.slice(end);
    hl = [start, start + preApply.replacement.length];
  } else if (highlight && highlight[0] >= 0 && highlight[1] >= highlight[0]
             && highlight[1] <= text.length) {
    // A deletion leaves a zero-width span (the removed words are gone from the
    // after-text); it's kept and widened to the neighbouring word below, so the
    // place the change fell still glows.
    hl = highlight;
  }
  if (hl) hl = wordSpan(text, hl);
  return { text, chars, hl };
}

// A highlight snapped to whole words — “There” glows, not the “re” a minimal
// character diff leaves inside it, and one marked word glows as just that word,
// not with the space the diff left hanging off it. Visual only; the save reads
// none of this.
function wordSpan(text, span) {
  const wordish = (ch) => !!ch && /[\p{L}\p{N}'’]/u.test(ch);
  let [s, e] = span;
  while (s > 0 && wordish(text[s - 1]) && wordish(text[s])) s -= 1;
  while (e < text.length && wordish(text[e]) && wordish(text[e - 1])) e += 1;
  // Any word inside: glow exactly the word(s), trimming the spaces or
  // punctuation a minimal diff can leave clinging to either end.
  let hasWord = false;
  for (let i = s; i < e; i += 1) { if (wordish(text[i])) { hasWord = true; break; } }
  if (hasWord) {
    while (s < e && !wordish(text[s])) s += 1;
    while (e > s && !wordish(text[e - 1])) e -= 1;
    return [s, e];
  }
  // No word inside, but a real span — a punctuation- or space-only change, e.g.
  // a comma moved. Glow it as it is.
  if (e > s) return [s, e];
  // A collapsed span — a deletion removed whole words, so the after-text has
  // nothing at the point to mark. Glow the word just after the gap so the
  // change still has a visible home, or the word just before it if the gap
  // ends the line. Nothing wordish either side (an empty or all-punctuation
  // line) leaves it collapsed, and the wrapper simply draws nothing.
  let a = s;
  while (a < text.length && !wordish(text[a])) a += 1;
  let b = a;
  while (b < text.length && wordish(text[b])) b += 1;
  if (b > a) return [a, b];
  b = s;
  while (b > 0 && !wordish(text[b - 1])) b -= 1;
  a = b;
  while (a > 0 && wordish(text[a - 1])) a -= 1;
  return [a, b];
}

// The paragraph rendered for the editor: its text with the book's bold/italic
// runs as <b>/<i>, and the proposed-change span wrapped green. The highlight
// wrapper carries no styling of its own that the parser reads, so it is
// invisible to the save.
function editorHTML(text, chars, hl) {
  const parts = [];
  const key = (k) => `${chars[k].bold ? 1 : 0}${chars[k].italic ? 1 : 0}`
    + `${hl && k >= hl[0] && k < hl[1] ? 1 : 0}`;
  let i = 0;
  while (i < text.length) {
    let j = i;
    while (j < text.length && key(j) === key(i)) j += 1;
    let piece = esc(text.slice(i, j));
    if (chars[i].italic) piece = `<i>${piece}</i>`;
    if (chars[i].bold) piece = `<b>${piece}</b>`;
    if (hl && i >= hl[0] && i < hl[1]) {
      piece = `<span class="fix-hl">${piece}</span>`;
    }
    parts.push(piece);
    i = j;
  }
  return parts.join('');
}

// The editor read back: plain text plus the bold/italic runs the designer
// left, walked off the contenteditable DOM (execCommand writes <b>/<i> or
// styled spans depending on the browser — both are read; the green highlight
// wrapper is transparent).
function parseEditor(box, originalText) {
  const segs = [];
  const walk = (node, bold, italic) => {
    if (node.nodeType === Node.TEXT_NODE) {
      if (node.nodeValue) segs.push({ t: node.nodeValue, bold, italic });
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE || node.tagName === 'BR') return;
    const style = node.style || {};
    const b = bold || node.tagName === 'B' || node.tagName === 'STRONG'
      || /^(bold|[7-9]00)$/.test(style.fontWeight || '');
    const i = italic || node.tagName === 'I' || node.tagName === 'EM'
      || /italic|oblique/.test(style.fontStyle || '');
    node.childNodes.forEach((c) => walk(c, b, i));
  };
  walk(box, false, false);
  let text = '';
  const runs = [];
  segs.forEach((s) => {
    let piece = s.t.replace(/\n/g, ' ');
    // contenteditable pads with non-breaking spaces the designer never typed;
    // fold them back to spaces unless the line legitimately carries NBSPs
    // (house style does, before an ellipsis) — then leave every one alone.
    if (!originalText.includes('\u00a0')) {
      piece = piece.replace(/\u00a0/g, ' ');
    }
    if (!piece) return;
    const start = text.length;
    text += piece;
    const last = runs[runs.length - 1];
    if (last && last.bold === s.bold && last.italic === s.italic
        && last.end === start) {
      last.end = text.length;
    } else {
      runs.push({ start, end: text.length, bold: s.bold, italic: s.italic });
    }
  });
  return { text, runs };
}

// A word-level redline between two versions of one line: shared runs are plain,
// removed words struck, added words underlined. Whitespace is tokenised too, so
// the spacing survives. This is what turns a bare find→replace into a change a
// designer can read in context. LCS over tokens; lines are a sentence or two, so
// the n·m table is tiny — with a block fallback if a paragraph is unusually long.
function wordDiff(before, after) {
  const tok = (s) => (s || '').match(/\s+|\S+/g) || [];
  const a = tok(before);
  const b = tok(after);
  const n = a.length;
  const m = b.length;
  if (n > 600 || m > 600) {
    return `<del>${esc(before)}</del> <ins>${esc(after)}</ins>`;
  }
  const dp = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j]
        ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const ops = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { ops.push(['eq', a[i]]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push(['del', a[i]]); i++; }
    else { ops.push(['ins', b[j]]); j++; }
  }
  while (i < n) { ops.push(['del', a[i]]); i++; }
  while (j < m) { ops.push(['ins', b[j]]); j++; }
  let html = '';
  let k = 0;
  while (k < ops.length) {
    const t = ops[k][0];
    let s = '';
    while (k < ops.length && ops[k][0] === t) { s += ops[k][1]; k++; }
    html += t === 'eq' ? esc(s)
      : t === 'del' ? `<del>${esc(s)}</del>` : `<ins>${esc(s)}</ins>`;
  }
  return html;
}

// A self-contained printable report from corrections.json — the same stages the
// notes .md carries (parse, apply, changes to review, verify), in the shared
// report styling. Every value came from a document, so all of it is escaped.
function correctionsReportHTML(d) {
  const num = (n) => Number(n || 0).toLocaleString();
  // The page a designer navigates to: the InDesign folio when the run could align
  // one (`page_label`, e.g. roman front matter or a restarted section), otherwise
  // the physical proof page. So a mark shown on “page 7” is the page InDesign
  // calls 7, not the seventh leaf of the proof.
  const pageOf = (c) => (c.page_label
    ? esc(c.page_label)
    : (c.page ? num(c.page) : ''));
  const ap = d.apply || {};
  const v = d.verify || {};
  const issues = (d.parse || {}).issues || [];
  // A flag the designer already resolved on the review screen is done — it
  // counts with the applied and leaves the needs-a-human lists.
  const flagged = (ap.flagged || []).filter((o) => !o.resolved && !o.dismissed);
  const allRes = d.resolutions || [];
  const resolutions = allRes.filter((r) => r.kind !== 'dismissed');
  const setAside = allRes.filter((r) => r.kind === 'dismissed');
  const disc = v.discrepancies || [];
  const com = d.comments || { total: 0, unresolved: 0, items: [] };
  const comItems = com.items || [];
  const changes = d.changes || [];
  const noOp = ap.no_op || [];
  // The headline counts in one unit so its buckets sum to a total the reader can
  // check. A proof's unit is its comments — what the reviewer marked, and what
  // `com.total` names — so applied / no-change / need-a-human are counted per
  // comment, never per edit; `com.unresolved` is the two needs-a-human buckets
  // folded. The edit and line counts are the mechanism underneath and ride along
  // as a sub-line, never as a fourth card that would break the sum. A typed list
  // has no comments, so there the unit is the edit and the same three are read off
  // the apply outcomes instead.
  const appliedC = comItems.filter((c) => c.disposition === 'applied'
    || c.resolved).length;
  const noChangeC = comItems.filter((c) => c.disposition === 'no_op').length;
  const rightNum = ' style="text-align:right;font-variant-numeric:tabular-nums"';
  // The reviewer's own words are appended even when a find→replace is shown, so a
  // row an editor has to act on carries the note the mark was made with — not just
  // the edit the model inferred from it.
  const change = (o) => {
    const f = esc(o.find || ''); const r = esc(o.replace || '');
    const note = (o.instruction || '').trim();
    let core;
    if (f && !r) core = `delete “${f}”`;
    else if (!f) core = r ? `“${r}”` : '';
    else core = `“${f}” → “${r}”`;
    if (note && note !== (o.replace || '')) {
      return core ? `${core} <span class="caveat">— reviewer: “${esc(note)}”`
        + '</span>' : `<span class="caveat">reviewer: “${esc(note)}”</span>`;
    }
    return core;
  };
  const DISP = {
    applied: 'applied', flagged: 'flagged for a human',
    no_op: 'no change needed', not_extracted: 'not turned into an edit',
  };

  const title = `Corrections — ${d.source_name || 'file'}`;
  const gen = d.generated_at ? new Date(d.generated_at).toLocaleString() : '';
  const clean = v.clean && !flagged.length && issues.length === 0
    && !com.unresolved;

  // The comment ledger leads when the list came from a proof, and it reconciles:
  // reviewer comments = applied + no change needed + need a human. With no proof
  // (a typed list) there are no comments to count, so the same three buckets are
  // read off the edits instead. Either way the buckets sum to the first card, so
  // the headline can no longer be short of its own total.
  const cardOf = (n, label) => `<div class="card"><span class="n">${n}</span>`
    + `<span class="l">${label}</span></div>`;
  const ledgerCards = com.total
    ? cardOf(num(com.total), 'reviewer comments')
      + cardOf(num(appliedC), 'applied')
      + (noChangeC ? cardOf(num(noChangeC), 'no change needed') : '')
      + cardOf(num(com.unresolved), 'need a human')
    : cardOf(num(ap.applied), 'applied')
      + (noOp.length ? cardOf(num(noOp.length), 'no change needed') : '')
      + cardOf(num(flagged.length), 'for a human');
  const cards = '<div class="cards">'
    + ledgerCards
    + cardOf(num(disc.length), 'unaccounted changes')
    + cardOf(clean ? '✓' : '—', 'clean')
    + '</div>'
    // The edit/line mechanism behind the comment headline, said once so the cards
    // above read as a clean sum — and so the "changes to review" count below, which
    // is lines, is not misread as corrections gone missing.
    + (com.total && ap.applied
      ? `<p class="blurb" style="margin-top:.4em">${num(ap.applied)} edit`
        + `${ap.applied === 1 ? '' : 's'} applied across ${num(changes.length)} `
        + `line${changes.length === 1 ? '' : 's'} — the counts above are the `
        + 'reviewer’s comments, one to a mark.</p>'
      : '');

  const headline = clean
    ? '<p class="headline">✓ <b>Every correction landed exactly, and nothing '
      + 'else in the file changed.</b></p>'
    : '<p class="headline" style="background:#fbeae2"><b>Some corrections need a '
      + 'human, or the file changed in ways the list did not ask for.</b> '
      + 'See below.</p>';

  // Which page numbers this report speaks in: the file's own folios when the
  // run aligned them, otherwise the proof's physical pages — said out loud so a
  // designer is not hunting for a page InDesign numbers differently. A run
  // that cited pages but carried none at all gets the loud version: the marks
  // lost their page narrowing entirely, which is why repeated text flagged.
  const pg = d.pages || {};
  const pagesNote = (!pg.total && pg.cited)
    ? '<p class="headline" style="background:#fbeae2"><b>No proof pages '
      + 'accompanied this run.</b> '
      + `${num(pg.cited)} correction(s) cite a proof page, but the proof’s `
      + 'page texts were not attached — every mark had the whole book to '
      + 'match against, so repeated text could only be flagged. Re-reading '
      + 'the marked proof attaches its pages.</p>'
    : (pg.total && !pg.labeled)
      ? '<p class="blurb">Page numbers here are the proof PDF’s physical pages — '
        + 'the file’s own folios could not be aligned to the proof for this '
        + 'run.</p>'
      : '';

  const issuesHTML = issues.length
    ? `<h2>Corrections that could not be read — ${num(issues.length)}</h2>`
      + '<p class="blurb">Skipped; nothing was guessed.</p>'
      + issues.map((i) =>
        `<p><b>Entry ${num((i.index || 0) + 1)}</b> <span class="caveat">`
        + `${esc(i.reason)}</span></p>`).join('')
    : '';

  // The reviewer comments a person still owns — flagged during apply, or never
  // turned into an edit at all — each with the reviewer's original note and the
  // page it was marked on, so an editor can act on it straight from here. This is
  // the section that keeps a comment from being swallowed.
  const needHuman = comItems.filter((c) =>
    (c.disposition === 'flagged' || c.disposition === 'not_extracted')
    && !c.resolved && !c.dismissed);
  const commentsHumanHTML = needHuman.length
    ? `<h2>Reviewer comments needing a human — ${num(needHuman.length)}</h2>`
      + '<p class="blurb">Every one carries the reviewer’s original note and its '
      + 'page, so it can be handled by hand.</p>'
      + needHuman.map((c) =>
        '<div class="flag"><b>'
        + (pageOf(c) ? `page ${pageOf(c)}` : '—') + '</b> '
        + `<span class="caveat">(${esc(DISP[c.disposition] || c.disposition)})`
        + '</span><br>“' + esc(c.instruction || '(no note)') + '”'
        + (c.anchor ? ` <span class="caveat">— on “${esc(c.anchor)}”</span>` : '')
        + (c.detail ? ` <span class="caveat">— ${esc(c.detail)}</span>` : '')
        + '</div>').join('')
    : '';

  // Period/capitalization queries the last tier read and confirmed correct as set
  // — off the human's list by a decision, not omission, so the reasoning is shown
  // for a person to check the call rather than take it on trust.
  const confirmedSet = comItems.filter((c) =>
    c.disposition === 'no_op' && c.detail);
  const commentsConfirmedHTML = confirmedSet.length
    ? `<h2>Confirmed as set — ${num(confirmedSet.length)}</h2>`
      + '<p class="blurb">A model read each against the passage around it and found '
      + 'the line correct as set, so no change was made. The reasoning is given so '
      + 'the call can be checked.</p>'
      + confirmedSet.map((c) =>
        '<div class="flag"><b>'
        + (pageOf(c) ? `page ${pageOf(c)}` : '—') + '</b> '
        + '“' + esc(c.instruction || '(no note)') + '”'
        + (c.anchor ? ` <span class="caveat">— on “${esc(c.anchor)}”</span>` : '')
        + ` <span class="caveat">— ${esc(c.detail)}</span>`
        + '</div>').join('')
    : '';

  // And the full ledger: one row per comment, so none is missing from the log.
  const commentsAllHTML = com.total
    ? `<details><summary>All ${num(com.total)} reviewer comments</summary>`
      + '<table><thead><tr><th>Page</th><th>Comment</th><th>Outcome</th>'
      + '</tr></thead><tbody>'
      + comItems.map((c) => {
        const bad = (c.disposition === 'flagged'
          || c.disposition === 'not_extracted') && !c.resolved && !c.dismissed;
        const outcome = c.resolved ? 'resolved in review'
          : c.dismissed ? 'set aside in review'
            : (DISP[c.disposition] || c.disposition);
        return `<tr${bad ? ' style="background:#fbeae2"' : ''}>`
          + `<td${rightNum}>${pageOf(c) || '—'}</td>`
          + `<td>${esc(c.instruction || '(no note)')}</td>`
          + `<td>${esc(outcome)}</td></tr>`;
      }).join('') + '</tbody></table></details>'
    : '';

  // What the designer settled from the review screen — each is in the file now.
  const resolvedHTML = resolutions.length
    ? `<h2>Resolved in review — ${num(resolutions.length)}</h2>`
      + '<p class="blurb">These flags were resolved on the review screen; each '
      + 'change is in the corrected file.</p>'
      + resolutions.map((r) => {
        const how = r.kind === 'manual' ? 'edited the line by hand'
          : r.kind === 'suggestion' ? 'applied the model’s suggestion'
            : r.text ? `typed: “${esc(r.text)}”` : 'picked a placement';
        const body = r.removed_line
          ? `<p class="rl"><del>${esc(r.before || '')}</del> <span class="caveat">`
            + '(the emptied line was removed)</span></p>'
          : `<p class="rl">${wordDiff(r.before || '', r.after || '')}</p>`
            + (r.format
              ? `<div class="loc">also set ${esc(r.format)}</div>` : '');
        const breaks = breakNotes(r.breaks);
        return `<div class="chg"><div class="loc">${how}</div>${body}`
          + (breaks ? `<div class="loc">and ${esc(breaks)}</div>` : '')
          + '</div>';
      }).join('')
    : '';

  // The flags the designer deliberately left alone — a decision, not an
  // omission, so it is printed with the note it left as set.
  const byItem = {};
  (d.queue || []).forEach((q) => { byItem[q.id] = q; });
  const setAsideHTML = setAside.length
    ? `<h2>Set aside in review — ${num(setAside.length)}</h2>`
      + '<p class="blurb">The designer chose to leave each of these as set (or '
      + 'to handle it in InDesign). Nothing was changed.</p>'
      + setAside.map((r) => {
        const q = byItem[r.item_id] || {};
        const where = q.page_label || q.page;
        return '<div class="flag"><b>'
          + (where ? `page ${esc(String(where))}` : '—') + '</b> '
          + '“' + esc(q.instruction || '(no note)') + '”'
          + (q.anchor
            ? ` <span class="caveat">— on “${esc(q.anchor)}”</span>` : '')
          + '</div>';
      }).join('')
    : '';

  // A flagged edit read from a proof is the same problem as its reviewer comment,
  // already listed above with the note and page — so when comments are present,
  // this section shows only the flagged edits no comment covers (a hand-added or
  // typed edit), and is retitled, so the two never read as the same list twice.
  const covered = new Set();
  needHuman.forEach((c) => (c.edit_ids || []).forEach((id) => covered.add(id)));
  const flaggedShown = com.total
    ? flagged.filter((o) => !covered.has(o.id)) : flagged;
  const flaggedHTML = flaggedShown.length
    ? `<h2>${com.total ? 'Other edits needing a human' : 'For a human'} — `
      + `${num(flaggedShown.length)}</h2>`
      + '<p class="blurb">Each of these was refused rather than guessed at.</p>'
      + flaggedShown.map((o) => `<div class="flag"><code>${esc(o.id)}</code> `
        + `${change(o)}${o.detail ? ' — ' + esc(o.detail) : ''}</div>`).join('')
    : '';

  // The applied changes, each in the line it changed, as a redline — the
  // designer's quick check that the corrections read right, not just that they
  // anchored. Split by where the designer acts: a text or formatting edit is done
  // in the file ("changed here", just confirm); a paragraph layout op reflows the
  // page, which is InDesign's to settle ("needs to be done in InDesign"). The
  // unaccounted-changes table below is the complement: this is what did change.
  const changeCard = (c) => {
    const where = `story ${esc(c.story_id)}`
      + (c.paragraph >= 0 ? `, ¶ ${num(c.paragraph)}` : '');
    const only = c.formatting && c.before === c.after;
    const body = only
      ? `<p class="rl">${esc(c.after)}</p>`
        + `<div class="loc">set ${esc(c.formatting)}</div>`
      : `<p class="rl">${wordDiff(c.before, c.after)}</p>`
        + (c.formatting
          ? `<div class="loc">also set ${esc(c.formatting)}</div>` : '');
    return '<div class="chg"><div class="loc">' + where + '</div>' + body
      + (c.instruction
        ? `<div class="caveat">reviewer: “${esc(c.instruction)}”</div>` : '')
      + '</div>';
  };
  // A text or formatting edit is done in the file (just confirm it reads right);
  // a paragraph layout op reflows the page, which is InDesign's to settle. The
  // two are different work for the designer, so they get their own tabs — and
  // when only one kind is present there is nothing to tab between, so it renders
  // as a plain section. `panel-h` is the heading each tab hides on screen (the
  // tab label serves it there) and print restores, so a printed report — where
  // both panels show at once — still names each list.
  const here = changes.filter((c) => !c.layout);
  const inddChanges = changes.filter((c) => c.layout);
  const textPanel = (h) =>
    `<h3 class="panel-h">Text changes — ${num(here.length)}</h3>`
    + (h ? '' : '<p class="blurb">Confirm each reads right — the change is '
      + 'already in the file.</p>')
    + here.map(changeCard).join('');
  const layoutPanel = (h) =>
    `<h3 class="panel-h">Layout changes — ${num(inddChanges.length)}</h3>`
    + '<p class="blurb">The engine set the paragraph, but where the text now '
    + 'falls on the page is InDesign’s — open the file and confirm each reflowed '
    + 'as intended.</p>'
    + inddChanges.map(changeCard).join('');
  const changesBody = (here.length && inddChanges.length)
    ? '<div class="tabs">'
      + '<input type="radio" name="chgtab" id="chgtab-text" checked>'
      + '<input type="radio" name="chgtab" id="chgtab-layout">'
      + '<div class="tabbar">'
      + `<label for="chgtab-text">Text — ${num(here.length)}</label>`
      + `<label for="chgtab-layout">Layout — ${num(inddChanges.length)}</label>`
      + '</div>'
      + `<section class="tabpanel panel-text">${textPanel(true)}</section>`
      + `<section class="tabpanel panel-layout">${layoutPanel(true)}</section>`
      + '</div>'
    : (here.length
        ? `<h3>Changed here — confirm it reads right — ${num(here.length)}</h3>`
          + here.map(changeCard).join('') : '')
      + (inddChanges.length
        ? `<h3>Needs to be done in InDesign — ${num(inddChanges.length)}</h3>`
          + '<p class="blurb">The engine set the paragraph, but where the text '
          + 'now falls on the page is InDesign’s — open the file and confirm each '
          + 'reflowed as intended.</p>'
          + inddChanges.map(changeCard).join('') : '');
  const changesHTML = changes.length
    ? `<h2>Changes to review — ${num(changes.length)} `
      + `line${changes.length === 1 ? '' : 's'}</h2>`
      + '<p class="blurb">'
      + (ap.applied
        ? `All ${num(ap.applied)} applied corrections, shown in the `
          + `${num(changes.length)} line${changes.length === 1 ? '' : 's'} they `
          + 'changed — a line several corrections touched appears once. '
        : 'Every applied correction in the line it changed. ')
      + (here.length && inddChanges.length
        ? 'Text edits are done in the file; layout changes reflow the page in '
          + 'InDesign — one tab each. '
        : 'Split by where you act. ')
      + '<del>struck</del> was removed, <ins>added</ins> is underlined.</p>'
      + changesBody
    : '';

  const checks = d.checks || [];
  const checkHTML = checks.length
    ? `<h2>To check in InDesign — ${num(checks.length)}</h2>`
      + '<p class="blurb">Whether a line breaks well, a heading is stranded or a '
      + 'page runs long is decided when InDesign sets the text, so no comparison of '
      + 'files can settle it. Each of these is located; none was guessed at.</p>'
      + '<table><thead><tr><th>Where</th><th>What to look at</th><th>Why</th>'
      + '</tr></thead><tbody>'
      + checks.map((c) => {
        const where = (pageOf(c) ? `page ${pageOf(c)}` : '—')
          + (c.paragraph >= 0 ? `<br><span class="loc">${esc(c.story_id)}, `
            + `¶ ${num(c.paragraph)}</span>` : '');
        return `<tr><td>${where}</td><td>${esc(c.what)}</td>`
          + `<td>“${esc(c.why)}”</td></tr>`;
      }).join('')
      + '</tbody></table>'
    : '';

  const discHTML = disc.length
    ? `<h2>Unaccounted changes — ${num(disc.length)}</h2>`
      + '<p class="blurb">Present in the file, asked for by no correction.</p>'
      + '<table><thead><tr><th>Where</th><th>Was</th><th>Now</th></tr></thead>'
      + '<tbody>' + disc.map((x) => {
        const where = `story ${esc(x.story_id)}`
          + (x.paragraph >= 0 ? `, ¶ ${num(x.paragraph)}` : '');
        return `<tr><td>${where}</td><td>${esc(x.before)}</td>`
          + `<td>${esc(x.after)}</td></tr>`;
      }).join('') + '</tbody></table>'
    : '';

  const verifyHTML = '<h2>Verification</h2>'
    + '<p class="blurb">The corrected file is compared word for word against '
    + 'what a clean apply of the list should produce — so an unrequested change '
    + 'has nowhere to hide.</p>'
    + `<p>Paragraphs: ${num(v.paragraphs_before)} before, `
    + `${num(v.paragraphs_after)} after`
    + (v.structure_intended
      ? ` (${num(v.paragraphs_expected)} expected — the corrections asked for `
        + 'the difference)' : '')
    + (v.structure_changed
      ? ' — <b>a paragraph was added, removed or merged</b>' : '') + '.</p>'
    + (disc.length ? '' : '<p>No unaccounted changes.</p>');

  return '<!doctype html><html><head><meta charset="utf-8">'
    + `<title>${esc(title)}</title><style>${REPORT_CSS}`
    + '.flag{border-left:3px solid var(--accent-soft);padding:.2em .8em;'
    + 'margin:.4em 0}.flag code{color:var(--accent)}'
    + '.chg{border-left:3px solid var(--accent-soft);padding:.25em .8em;'
    + 'margin:.55em 0}.chg .loc{font-size:11px;letter-spacing:.04em;'
    + 'text-transform:uppercase;color:var(--muted,#8a7f77);margin-bottom:.1em}'
    + '.rl{margin:.1em 0;line-height:1.55}'
    + '.rl del{color:#c0392b;text-decoration:line-through;'
    + 'text-decoration-thickness:1px}'
    + '.rl ins{color:#1e7d34;text-decoration:none;background:#e6f4ea;'
    + 'border-radius:2px;padding:0 .1em}'
    // CSS-only tabs for "Changes to review": the radios hold the state and the
    // labels are the tab strip, so the report stays a single self-contained file
    // with no script. Print shows both panels (a hidden tab would not print) and
    // restores each panel's own heading.
    + '.tabs>input{position:absolute;width:0;height:0;opacity:0}'
    + '.tabbar{display:flex;gap:.4em;border-bottom:1px solid var(--line);'
    + 'margin:.2em 0 .9em}'
    + '.tabbar label{padding:.45em .95em;cursor:pointer;font-size:.82rem;'
    + 'font-weight:600;letter-spacing:.02em;color:var(--muted);'
    + 'border-bottom:2px solid transparent;margin-bottom:-1px}'
    + '.tabbar label:hover{color:var(--accent)}'
    + '#chgtab-text:checked~.tabbar label[for=chgtab-text],'
    + '#chgtab-layout:checked~.tabbar label[for=chgtab-layout]'
    + '{color:var(--accent);border-bottom-color:var(--accent)}'
    + '.tabpanel{display:none}'
    + '#chgtab-text:checked~.panel-text,'
    + '#chgtab-layout:checked~.panel-layout{display:block}'
    + '.tabpanel .panel-h{display:none}'
    + '@media print{.tabbar{display:none}.tabpanel{display:block!important}'
    + '.tabpanel .panel-h{display:block;font:600 1rem/1.3 "Iowan Old Style",'
    + 'Palatino,Georgia,serif;margin:26px 0 4px}}</style></head><body>'
    + '<div class="wrap"><header class="rep"><span class="brand">DocProof</span>'
    + `<h1>${esc(title)}</h1>`
    + '<p class="sub">'
    + (d.deterministic === false
      ? 'Deterministic apply, with an opt-in model pass over the edits'
      : 'Deterministic — no model, no cost')
    + (gen ? ' · ' + esc(gen) : '') + '</p></header>'
    + cards + headline + pagesNote + commentsHumanHTML + issuesHTML + flaggedHTML
    + resolvedHTML + setAsideHTML + changesHTML + checkHTML
    + commentsConfirmedHTML + discHTML
    + verifyHTML + commentsAllHTML
    + '</div></body></html>';
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
  } else if (!h.queries) {
    parts.push('Nothing needed changing');
  } else {
    // Queries but no corrections is a real outcome, not an empty review: the
    // run found nothing to fix and several things to ask about.
    parts.push('Nothing needed correcting');
  }
  // The other half of the deliverable. Said in the headline because it is the
  // part a reader would otherwise never go looking for.
  if (h.queries) {
    parts.push(`${h.queries} question${h.queries === 1 ? '' : 's'} `
      + 'in the margins');
  }
  if (typeof r.cost === 'number') {
    parts.push(`cost ${r.cost < 0.01 ? '<$0.01' : '$' + r.cost.toFixed(2)}`);
  }
  $('report-headline').textContent = parts.join(' · ');

  const groups = $('report-groups');
  groups.innerHTML = '';
  if (r.examination_graph && r.examination_graph.accounting) {
    const a = r.examination_graph.accounting;
    const section = document.createElement('section');
    section.className = 'card';
    const head = document.createElement('h3');
    head.textContent = 'Examination coverage — shadow mode';
    const copy = document.createElement('p');
    copy.textContent = `${Number(a.generated_sites || 0).toLocaleString()} sites · `
      + `${Number(a.terminal_sites || 0).toLocaleString()} terminal decisions `
      + `(${Number(a.terminal_percent || 0).toFixed(2)}%) · `
      + `${Number(a.explicitly_pending_sites || 0).toLocaleString()} explicitly pending. `
      + 'The examination layer observed this review and created no edits.';
    section.append(head, copy);
    groups.append(section);

    const p = r.examination_graph.production_verdicts;
    if (p && p.enabled) {
      const production = document.createElement('section');
      production.className = 'card examination-production';
      const productionHead = document.createElement('h3');
      productionHead.textContent = 'Phase 2 production verdicts — shadow only';
      const productionCopy = document.createElement('p');
      productionCopy.textContent = `${Number(p.explicit_sites || 0).toLocaleString()} `
        + `of ${Number(p.expected_sites || 0).toLocaleString()} paragraph/category `
        + `sites explicitly accounted for (${Number(p.coverage_percent || 0).toFixed(2)}%): `
        + `${Number(p.explicit_passes || 0).toLocaleString()} passes, `
        + `${Number(p.explicit_errors || 0).toLocaleString()} error signals, `
        + `${Number(p.pending_sites || 0).toLocaleString()} pending. `
        + 'These receipts created no findings or edits.';
      production.append(productionHead, productionCopy);
      groups.append(production);
    }

    const j = r.examination_graph.judgment;
    if (j && j.enabled) {
      const selection = j.selection || {};
      const comparison = j.comparison || {};
      const experiment = document.createElement('section');
      experiment.className = 'card examination-experiment';
      const experimentHead = document.createElement('h3');
      experimentHead.textContent = 'Independent examination — evaluation only';
      const guardrail = document.createElement('p');
      guardrail.textContent = 'A separate judge reviewed a deterministic, '
        + 'bounded sample. Its verdicts did not create findings or change the '
        + 'manuscript.';
      const metrics = document.createElement('dl');
      metrics.className = 'experiment-metrics';
      [
        ['Eligible sites', selection.eligible_sites],
        ['Selected', selection.selected_sites],
        ['Judged', comparison.judged_sites],
        ['Both found an error', comparison.both_found_error],
        ['Examination only', comparison.examination_only_error],
        ['Production only', comparison.production_only_error],
        ['Unresolved', comparison.unresolved],
        ['Cost', `$${Number(j.estimated_cost_usd || 0).toFixed(2)}`],
      ].forEach(([label, value]) => {
        const term = document.createElement('dt');
        term.textContent = label;
        const number = document.createElement('dd');
        number.textContent = typeof value === 'number'
          ? value.toLocaleString() : String(value == null ? 0 : value);
        metrics.append(term, number);
      });
      experiment.append(experimentHead, guardrail, metrics);
      groups.append(experiment);
    }
  }
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
  // Queries come first of the four, because they are the only ones the author
  // is being asked to do something about. The other three are the review
  // explaining itself.
  addAside(aside, 'Queries — questions, not corrections', r.queries,
    'These come from the kinds of mistake that ask rather than correct, '
    + 'because the answer is yours to make. Each one is a comment in the '
    + 'margin of the reviewed file; none of them changed your document.');
  addAside(aside, 'Left for you to judge', r.low_confidence,
    'These looked deliberate, or the model was unsure. Nothing in your '
    + 'document was changed for them.');
  addAside(aside, 'Couldn’t be placed', r.not_placed,
    'These were found, but the words they quote could no longer be located '
    + 'in the document, so there was nowhere safe to put them.');
  addAside(aside, 'Not applied', r.not_applied,
    'Nothing in your document was changed for these. Each one says why '
    + 'underneath it — most were covered by another change, or set aside by '
    + 'a check further down the line.');
}

function addAside(parent, title, findings, blurb) {
  // A report built before a bucket existed simply has no key for it, and a
  // missing panel is the right answer there — not a crash that blanks the
  // whole page.
  if (!findings || !findings.length) return;
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

// ── promo ───────────────────────────────────────────────────────────────────
//
// A new page, but the same two ideas as everywhere else: drop a manuscript and
// watch a job, and a settings form for the automatic pipeline. The copy a run
// produces is editable in place — a person tidies the teaser, saves, and the
// two .docx are re-made from what they approved.

let promoPollTimer = null;
const PROMO_TERMINAL = ['done', 'failed', 'cancelled'];

async function loadPromo() {
  if (!state.promoModels) {
    try { state.promoModels = (await api('/api/models')).models; }
    catch (_) { state.promoModels = []; }
  }
  fillPromoModels($('promo-model'));
  fillPromoModels($('plan-model'));
  renderPromoPanelCost();
  renderPlanCost();
  await refreshPromoJobs();
}

function fillPromoModels(select, blank) {
  const chosen = select.value;
  select.innerHTML = '';
  if (blank) {
    const o = document.createElement('option');
    o.value = ''; o.textContent = blank; select.append(o);
  }
  (state.promoModels || []).forEach((m) => {
    const o = document.createElement('option');
    o.value = m.id;
    o.textContent = m.available ? m.display : `${m.display} — no key yet`;
    select.append(o);
  });
  if (chosen) select.value = chosen;
}

// Stage the picked manuscript straight away — the same drop-and-preflight the
// main screen does — so the Promo tab can price it and flag an oversize book
// before the run, not just discover it when the run fails. The staged id is
// kept for the run to reuse.
async function stagePromoFile() {
  const file = $('promo-file').files[0];
  const status = $('promo-run-status');
  state.promoStaged = null;
  if (!file) { renderPromoPanelCost(); return; }
  status.hidden = false; status.textContent = 'Reading the manuscript…';
  $('promo-run').disabled = true;
  try {
    const form = new FormData();
    form.append('files', file);
    const staged = (await api('/api/files',
                              { method: 'POST', body: form })).files[0];
    if (!staged.ok || !staged.promo) {
      throw new Error(staged.promo_error || staged.error
                      || 'That file cannot be used for promo.');
    }
    state.promoStaged = staged;
    status.hidden = true;
  } catch (e) {
    status.hidden = false; status.textContent = e.message;
  }
  renderPromoPanelCost();
}

// One model's promo cost on the Promo tab: input plus the fixed teaser-and-posts
// output, the reasoning level scaling the output half only. Mirrors pricePromo
// on the main screen, against this tab's own staged file and effort control.
function promoModelCost(m, promo, level) {
  const factor = (m && m.supports_effort)
    ? (state.effortMultipliers[level] || 1) : 1;
  return (promo.input_tokens * m.input_per_mtok
    + promo.output_tokens * m.output_per_mtok * factor) / 1e6;
}

// The Promo tab's cost estimate, model comparison, and oversize override — the
// parity match for the main drop screen's renderPromoCost.
function renderPromoPanelCost() {
  const staged = state.promoStaged;
  const models = state.promoModels || [];
  const m = models.find((x) => x.id === $('promo-model').value);
  const level = $('pp-effort').value;
  const warn = $('pp-oversize');
  const ok = $('pp-oversize-ok');

  if (!staged || !staged.promo) {
    $('pp-cost').hidden = true;
    warn.hidden = true;
    $('promo-run').disabled = true;
    return;
  }

  const promo = staged.promo;
  $('pp-cost').hidden = false;
  $('pp-cost-line').textContent = m
    ? `About ${(promo.words || 0).toLocaleString()} words on ${m.display} costs `
      + `about ${money(promoModelCost(m, promo, level))}`
      + `${promo.verify_claims ? ' with the claim-check on' : ''}. One call `
      + `over the whole book.`
    : '';

  const rows = $('pp-cost-rows');
  rows.innerHTML = '';
  models
    .map((x) => ({ x, cost: promoModelCost(x, promo, level) }))
    .sort((a, b) => a.cost - b.cost)
    .forEach(({ x, cost }) => {
      const tr = document.createElement('tr');
      if (m && x.id === m.id) tr.className = 'chosen';
      const name = document.createElement('td');
      name.textContent = x.display + (x.available ? '' : ' — no key yet');
      const price = document.createElement('td');
      price.textContent = `about ${money(cost)}`;
      tr.append(name, price);
      rows.append(tr);
    });
  $('pp-cost-compare').hidden = false;

  if (promo.over_limit) {
    $('pp-oversize-note').textContent =
      `This book is about ${promo.pass_tokens.toLocaleString()} tokens, over `
      + `the ${promo.max_input_tokens.toLocaleString()}-token single-pass `
      + `limit for promo.`;
    warn.hidden = false;
  } else {
    warn.hidden = true;
    ok.checked = false;
  }

  $('promo-run').disabled =
    !(m && m.available && (!promo.over_limit || ok.checked));
}

async function runPromo() {
  const staged = state.promoStaged;
  if (!staged || !staged.ok) return;
  const status = $('promo-run-status');
  status.hidden = false; status.textContent = 'Writing your copy…';
  $('promo-run').disabled = true;
  try {
    await api('/api/promo/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        file_ids: [staged.id],
        model: $('promo-model').value,
        effort: $('pp-effort').value,
        allow_oversize: $('pp-oversize-ok').checked,
      }),
    });
    $('promo-file').value = '';
    state.promoStaged = null;
    status.hidden = true;
    renderPromoPanelCost();
    await refreshPromoJobs();
  } catch (e) {
    status.hidden = false; status.textContent = e.message;
    renderPromoPanelCost();
  }
}

async function refreshPromoJobs() {
  let jobs = [];
  try { jobs = (await api('/api/promo/jobs')).jobs; } catch (_) {}
  renderPromoJobs(jobs);
  // Keep polling only while something is still being written, and only while
  // this page is the one on screen.
  clearTimeout(promoPollTimer);
  const active = jobs.some((j) => !PROMO_TERMINAL.includes(j.state));
  if (active && !$('screen-promo').hidden) {
    promoPollTimer = setTimeout(refreshPromoJobs, 2500);
  }
}

function renderPromoJobs(jobs) {
  const box = $('promo-jobs');
  // While an editor is open, don't rebuild the list. A poll fires every couple
  // of seconds; replacing the cards would wipe the unsaved teaser and post
  // edits — and the caret with them — mid-keystroke. The open job is already
  // 'done' and its content can't change underneath the editor, and the next
  // poll after it closes catches everything else up.
  if (box.querySelector('.promo-edit[open]')) return;
  $('promo-empty').hidden = jobs.length > 0;
  box.innerHTML = '';
  jobs.forEach((job) => box.append(promoCard(job)));
}

function promoCard(job) {
  const card = document.createElement('div');
  card.className = 'promo-job';

  const head = document.createElement('div');
  head.className = 'promo-job-head';
  const name = document.createElement('strong');
  name.textContent = job.filename;
  // A plan and a copy run of the same book look alike in the list otherwise, so
  // the plan cards carry a small tag; copy is the tab's default and stays bare.
  if (job.is_plan) {
    const tag = document.createElement('span');
    tag.className = 'pill'; tag.textContent = 'marketing plan';
    name.append(' ', tag);
  }
  const state = document.createElement('span');
  state.className = 'muted';
  const flags = job.state === 'done' && job.unverified
    ? ` · ${job.unverified} to check` : '';
  state.textContent = ` ${job.plain_state}${flags}`;
  head.append(name, state);
  card.append(head);

  if (job.state === 'failed' && job.error) {
    const err = document.createElement('p');
    err.className = 'muted'; err.textContent = job.error;
    card.append(err);
  }

  if (job.state === 'done') {
    card.append(promoEditor(job), promoActions(job));
  } else if (PROMO_TERMINAL.includes(job.state)) {
    card.append(promoActions(job));
  }
  return card;
}

function promoEditor(job) {
  const details = document.createElement('details');
  details.className = 'promo-edit';
  const summary = document.createElement('summary');
  summary.textContent = job.is_plan ? 'Read & edit the plan'
    : 'Read & edit the copy';
  const body = document.createElement('div');
  const loading = document.createElement('p');
  loading.className = 'muted'; loading.textContent = 'Loading…';
  body.append(loading);
  details.append(summary, body);

  const build = job.is_plan ? buildPlanEditor : buildPromoEditor;
  let loaded = false;
  details.addEventListener('toggle', () => {
    if (details.open && !loaded) { loaded = true; build(job, body); }
  });
  return details;
}

async function buildPromoEditor(job, body) {
  let draft;
  try { draft = await api(`/api/promo/jobs/${job.id}/draft`); }
  catch (e) { body.textContent = e.message; return; }
  body.innerHTML = '';

  const grounding = promoGrounding(draft);
  if (grounding) body.append(grounding);

  const teaser = promoField('Teaser', draft.teaser, 4);
  body.append(teaser.label);
  const posts = draft.posts.map((post, i) => {
    const tag = post.platform ? ` · ${post.platform}` : '';
    const f = promoField(`Post ${i + 1}${tag}`, post.text, 2);
    body.append(f.label);
    return f.input;
  });

  const save = document.createElement('button');
  save.className = 'primary'; save.textContent = 'Save changes';
  const note = document.createElement('span');
  note.className = 'muted'; note.hidden = true;
  save.addEventListener('click', async () => {
    save.disabled = true; note.hidden = false; note.textContent = 'Saving…';
    const edited = draft.posts.map((p, i) => ({
      platform: p.platform || '', text: posts[i].value, hashtags: p.hashtags || [],
    }));
    try {
      await api(`/api/promo/jobs/${job.id}/draft`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ teaser: teaser.input.value, posts: edited }),
      });
      note.textContent = 'Saved — the documents were re-made.';
    } catch (e) { note.textContent = e.message; }
    finally { save.disabled = false; }
  });
  const row = document.createElement('div');
  row.className = 'promo-job-actions';
  row.append(save, note);
  body.append(row);
}

function promoGrounding(draft) {
  const terms = draft.flagged_terms || [];
  const claims = draft.unsupported_claims || [];
  if (!terms.length && !claims.length) return null;
  const box = document.createElement('div');
  box.className = 'promo-grounding';
  const title = document.createElement('strong');
  title.textContent = 'Worth a check before this ships';
  box.append(title);
  if (terms.length) {
    const p = document.createElement('p');
    p.textContent = `Not found in the book: ${terms.join(', ')}`;
    box.append(p);
  }
  claims.forEach((c) => {
    const p = document.createElement('p');
    p.textContent = c.note ? `“${c.claim}” — ${c.note}` : `“${c.claim}”`;
    box.append(p);
  });
  return box;
}

function promoField(labelText, value, rows) {
  const label = document.createElement('label');
  label.className = 'field';
  const span = document.createElement('span');
  span.textContent = labelText;
  const input = document.createElement('textarea');
  input.rows = rows; input.value = value;
  label.append(span, input);
  return { label, input };
}

function promoActions(job) {
  const row = document.createElement('div');
  row.className = 'promo-job-actions';
  if (job.state === 'done') {
    if (job.is_plan) {
      row.append(promoDownload(job, 'plan', 'Download plan'));
    } else {
      row.append(promoDownload(job, 'teaser', 'Download teaser'),
                 promoDownload(job, 'posts', 'Download posts'));
    }
  }
  const remove = document.createElement('button');
  remove.className = 'link'; remove.textContent = 'Remove';
  const prompt = job.is_plan ? 'Remove this marketing plan?'
    : 'Remove this promo copy?';
  confirmInline(remove, prompt, async (confirmRow) => {
    try {
      await api(`/api/jobs/${job.id}`, { method: 'DELETE' });
      await refreshPromoJobs();            // redraws; the transient row goes
    } catch (e) {
      confirmRow.fail(e.message || 'Could not remove this one.');
    }
  });
  row.append(remove);
  return row;
}

function promoDownload(job, which, label) {
  const a = document.createElement('a');
  a.className = 'link';
  a.href = `/api/promo/jobs/${job.id}/file/${which}`;
  a.setAttribute('download', '');
  a.textContent = label;
  return a;
}

async function loadPromoSettings() {
  const s = await api('/api/promo/settings');
  state.promoSettings = s;
  $('promo-enabled').checked = s.promo_enabled;
  $('promo-ready').value = s.hubspot_promo_ready_value || '';
  $('promo-done').value = s.hubspot_promo_done_value || '';
  $('promo-auto-upload').checked = s.promo_auto_upload;
  $('promo-auto-needs-hubspot').hidden = s.hubspot_enabled;
  $('promo-model-fallback').textContent = s.promo_model
    ? '' : `— using the DocWatch model, ${s.fallback_model}`;
  fillPromoModels($('promo-auto-model'), 'Use the DocWatch model');
  $('promo-auto-model').value = s.promo_model || '';
  if ($('wf-rows')) renderRegistry();
}

async function savePromoSettings() {
  const status = $('promo-settings-status');
  status.hidden = false; status.textContent = 'Saving…';
  try {
    await api('/api/promo/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        promo_enabled: $('promo-enabled').checked,
        hubspot_promo_ready_value: $('promo-ready').value.trim(),
        hubspot_promo_done_value: $('promo-done').value.trim(),
        promo_auto_upload: $('promo-auto-upload').checked,
        promo_model: $('promo-auto-model').value,
      }),
    });
    status.textContent = 'Saved.';
    await loadPromoSettings();
  } catch (e) { status.textContent = e.message; }
}

async function loadPlanSettings() {
  const s = await api('/api/promo/plan-settings');
  state.planSettings = s;
  $('plan-enabled').checked = s.plan_enabled;
  $('plan-property').value = s.hubspot_plan_property || '';
  $('plan-needed').value = s.hubspot_plan_needed_value || '';
  $('plan-done').value = s.hubspot_plan_done_value || '';
  $('plan-pen').value = s.hubspot_pen_property || '';
  $('plan-auto-upload').checked = s.plan_auto_upload;
  $('plan-blurb-pattern').value = s.plan_blurb_pattern || '';
  $('plan-form-pattern').value = s.plan_form_pattern || '';
  $('plan-auto-effort').value = s.plan_effort || 'low';
  $('plan-auto-needs-hubspot').hidden = s.hubspot_enabled;
  $('plan-model-fallback').textContent = s.plan_model
    ? '' : `— using the DocWatch model, ${s.fallback_model}`;
  fillPromoModels($('plan-auto-model'), 'Use the DocWatch model');
  $('plan-auto-model').value = s.plan_model || '';
  if ($('wf-rows')) renderRegistry();
}

async function savePlanSettings() {
  const status = $('plan-settings-status');
  status.hidden = false; status.textContent = 'Saving…';
  try {
    await api('/api/promo/plan-settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        plan_enabled: $('plan-enabled').checked,
        hubspot_plan_property: $('plan-property').value.trim(),
        hubspot_plan_needed_value: $('plan-needed').value.trim(),
        hubspot_plan_done_value: $('plan-done').value.trim(),
        hubspot_pen_property: $('plan-pen').value.trim(),
        plan_auto_upload: $('plan-auto-upload').checked,
        plan_model: $('plan-auto-model').value,
        plan_effort: $('plan-auto-effort').value,
        plan_blurb_pattern: $('plan-blurb-pattern').value.trim(),
        plan_form_pattern: $('plan-form-pattern').value.trim(),
      }),
    });
    status.textContent = 'Saved.';
    await loadPlanSettings();
  } catch (e) { status.textContent = e.message; }
}

// ── marketing plan ───────────────────────────────────────────────────────────
//
// The plan card on the Promo tab: the same stage-then-price-then-run shape as
// the copy card, plus the operator-typed metadata the plan takes. The jobs it
// makes land in the same list below, told apart by job.is_plan.

// Stage the plan's manuscript, and — as a convenience — offer the file's name
// as the author field when it is still empty, so the operator edits a guess
// rather than typing from nothing.
async function stagePlanFile() {
  const file = $('plan-file').files[0];
  const status = $('plan-run-status');
  state.planStaged = null;
  if (!file) { renderPlanCost(); return; }
  status.hidden = false; status.textContent = 'Reading the manuscript…';
  $('plan-run').disabled = true;
  try {
    const form = new FormData();
    form.append('files', file);
    const staged = (await api('/api/files',
                              { method: 'POST', body: form })).files[0];
    if (!staged.ok || !staged.promo) {
      throw new Error(staged.promo_error || staged.error
                      || 'That file cannot be used for a plan.');
    }
    state.planStaged = staged;
    status.hidden = true;
    if (!$('plan-author').value.trim()) {
      $('plan-author').value = file.name.replace(/\.docx$/i, '');
    }
  } catch (e) {
    status.hidden = false; status.textContent = e.message;
  }
  renderPlanCost();
}

// The plan's cost estimate and oversize override — the same single-call pricing
// as the copy card (input plus a small fixed output, effort scaling the output
// half), kept compact: one line and the override, no model-compare table.
function renderPlanCost() {
  const staged = state.planStaged;
  const models = state.promoModels || [];
  const m = models.find((x) => x.id === $('plan-model').value);
  const level = $('plan-effort').value;
  const warn = $('plan-oversize');
  const ok = $('plan-oversize-ok');

  if (!staged || !staged.promo) {
    $('plan-cost').hidden = true;
    warn.hidden = true;
    $('plan-run').disabled = true;
    return;
  }

  const promo = staged.promo;
  $('plan-cost').hidden = false;
  $('plan-cost-line').textContent = m
    ? `About ${(promo.words || 0).toLocaleString()} words on ${m.display} costs `
      + `about ${money(promoModelCost(m, promo, level))}. One call over the `
      + `whole book.`
    : '';

  if (promo.over_limit) {
    $('plan-oversize-note').textContent =
      `This book is about ${promo.pass_tokens.toLocaleString()} tokens, over `
      + `the ${promo.max_input_tokens.toLocaleString()}-token single-pass limit.`;
    warn.hidden = false;
  } else {
    warn.hidden = true;
    ok.checked = false;
  }

  $('plan-run').disabled =
    !(m && m.available && (!promo.over_limit || ok.checked));
}

async function runPlan() {
  const staged = state.planStaged;
  if (!staged || !staged.ok) return;
  const status = $('plan-run-status');
  status.hidden = false; status.textContent = 'Writing the marketing plan…';
  $('plan-run').disabled = true;
  try {
    await api('/api/promo/plan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        file_ids: [staged.id],
        model: $('plan-model').value,
        effort: $('plan-effort').value,
        allow_oversize: $('plan-oversize-ok').checked,
        author: $('plan-author').value.trim(),
        blurbs: $('plan-blurbs').value.trim(),
        city: $('plan-city').value.trim(),
        keywords: $('plan-keywords').value.trim(),
      }),
    });
    $('plan-file').value = '';
    state.planStaged = null;
    status.hidden = true;
    renderPlanCost();
    await refreshPromoJobs();
  } catch (e) {
    status.hidden = false; status.textContent = e.message;
    renderPlanCost();
  }
}

// The plan's editor: one Markdown textarea, re-rendered to the .docx on save —
// the plan analog of buildPromoEditor's teaser/posts fields.
async function buildPlanEditor(job, body) {
  let draft;
  try { draft = await api(`/api/promo/jobs/${job.id}/plan`); }
  catch (e) { body.textContent = e.message; return; }
  body.innerHTML = '';

  const field = promoField('The plan (Markdown)', draft.plan, 18);
  body.append(field.label);

  const save = document.createElement('button');
  save.className = 'primary'; save.textContent = 'Save changes';
  const note = document.createElement('span');
  note.className = 'muted'; note.hidden = true;
  save.addEventListener('click', async () => {
    save.disabled = true; note.hidden = false; note.textContent = 'Saving…';
    try {
      await api(`/api/promo/jobs/${job.id}/plan`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan: field.input.value }),
      });
      note.textContent = 'Saved — the document was re-made.';
    } catch (e) { note.textContent = e.message; }
    finally { save.disabled = false; }
  });
  const row = document.createElement('div');
  row.className = 'promo-job-actions';
  row.append(save, note);
  body.append(row);
}

$('promo-file').addEventListener('change', stagePromoFile);
$('promo-model').addEventListener('change', renderPromoPanelCost);
$('pp-effort').addEventListener('change', renderPromoPanelCost);
$('pp-oversize-ok').addEventListener('change', renderPromoPanelCost);
$('promo-run').addEventListener('click', runPromo);
$('promo-settings-save').addEventListener('click', savePromoSettings);
$('plan-settings-save').addEventListener('click', savePlanSettings);

$('plan-file').addEventListener('change', stagePlanFile);
$('plan-model').addEventListener('change', renderPlanCost);
$('plan-effort').addEventListener('change', renderPlanCost);
$('plan-oversize-ok').addEventListener('change', renderPlanCost);
$('plan-run').addEventListener('click', runPlan);

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
  const list = $('prompt-list');
  let data;
  try {
    data = await api('/api/prompts');
  } catch (err) {
    // Without this the tab just goes blank on a failed load; say what happened.
    list.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'error';
    p.textContent = err.message;
    list.append(p);
    return;
  }
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
  if (!quiet) {
    // The workflow settings live behind their own endpoints; load them when the
    // tab is opened so the registry rows and the drawers reflect what's saved.
    if (!state.promoModels) {
      try { state.promoModels = (await api('/api/models')).models; }
      catch (_) { state.promoModels = state.promoModels || []; }
    }
    await Promise.all([
      loadPromoSettings().catch(() => {}),
      loadPlanSettings().catch(() => {}),
    ]);
  }
  renderRegistry();
}

// The inputs are filled only on a deliberate load — opening the tab, or
// finishing a save. The five-second poll redraws everything else, and a poll
// that rewrote the folder field would eat a paste mid-keystroke.
function renderWatch(body, quiet) {
  const w = body.watch;
  state.watchStatus = w;
  renderWatchSignIn(body);
  renderWatchRun(body);
  renderWatchBanner(body);
  renderWatchFiles(w.files);
  applyWatchSchedule(body.can_schedule);
  // Cheap and keystroke-safe, so they run on the five-second poll too: showing
  // the editor follows the checkbox, and "next look" is a clock that should
  // keep ticking without a deliberate reload.
  $('watch-inapp-schedule').hidden = !w.auto_ticks;
  renderWatchNextRun(w);
  if (quiet) return;

  $('watch-folder').value = w.folder_id || '';
  $('watch-output').value = w.prep_output || 'indesign';
  $('watch-failure-note').checked = w.upload_failure_note;
  // Meaningful in both modes: in a flat folder it prepares only files named
  // "<surname> - Book Original"; in subfolder mode the surname comes from the
  // ready record. Always shown — a hidden switch that silently did nothing in
  // the flat folder is the bug this closes.
  $('watch-require-label-field').hidden = false;
  $('watch-require-label').checked = w.require_source_label;
  $('watch-archive-enabled').checked = w.archive_enabled;
  $('watch-archive-folder').value = w.archive_folder_id || '';
  $('watch-archive-source').checked = w.archive_include_source;
  $('watch-auto').checked = w.auto_ticks;
  fillWatchSchedule(w);
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

function applyWatchSchedule(canSchedule) {
  // The "look while closed" clock is a macOS launch agent — it has no meaning
  // on the always-on server, which never closes. Where it can't run, hide it
  // and let the remaining clock speak for itself: the in-app timer that looks
  // on a schedule while DocProof runs is the one doing the work there.
  $('watch-client-web').hidden = !WEB;
  $('watch-closed-schedule').hidden = !canSchedule;
  if (canSchedule) return;
  $('watch-schedule-intro').textContent =
    'DocProof looks on its own while it is running.';
  $('watch-auto-label').textContent = 'Look automatically';
  $('watch-auto-hint').textContent =
    'Checks the folder on a schedule and prepares anything new. Turning this '
    + 'off pauses the automatic passes — Look now still works.';
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
      : WEB
        ? 'Signed in. The sign-in is held on the server, not in a file.'
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
    const body = await api('/api/watch/auth', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    });
    // On the web build Google's consent page can't open on the server, so the
    // server hands back its address and the browser already on screen goes
    // there; it returns to this page through /api/watch/auth/callback. The
    // desktop build opens the page itself and reports back through the poll.
    if (body.consent_url) {
      window.location.href = body.consent_url;
      return;
    }
    renderWatch(body);
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
  if (last.waiting) {
    line.textContent += ` ${last.waiting} waiting on HubSpot.`;
  }
}

// The slim strip at the top of every screen. It reuses the same run state the
// DocWatch tab's bar reads, so the two never disagree, and shows only while a
// pass is in flight. The whole /api/watch surface is admin-only, so this is too
// — which is why it may name the manuscript it is on; a regular web user's poll
// never calls the endpoint and never sees the banner (see watchAvailable).
function renderWatchBanner(body) {
  const banner = $('watch-banner');
  if (!banner) return;
  const run = body && body.run;
  if (!run || !run.busy) { banner.hidden = true; return; }
  banner.hidden = false;

  const doing = run.progress[0];
  const bar = $('watch-banner-bar');
  const hasProgress = !!(doing && doing.total);
  bar.hidden = !hasProgress;
  if (hasProgress) {
    bar.firstElementChild.style.width =
      `${Math.round((doing.done / doing.total) * 100)}%`;
  }
  $('watch-banner-text').textContent = watchBannerSummary(body);
}

function watchBannerSummary(body) {
  const run = body.run;
  const doing = run.progress[0];
  const attention = (body.watch.files || []).filter((f) => f.error).length;
  let text = doing
    ? `DocWatch: ${doing.filename} — ${doing.plain_state.toLowerCase()}`
    : 'DocWatch: looking in the folder…';
  if (run.progress.length > 1) text += ` (+${run.progress.length - 1} more)`;
  if (attention) text += ` · ${attention} need attention`;
  return text;
}

// Off the DocWatch tab there is nothing to redraw but the banner, so the poll
// fetches the status alone rather than re-rendering the whole hidden screen.
async function refreshWatchBanner() {
  try {
    renderWatchBanner(await api('/api/watch'));
  } catch (_) { /* a poll that fails is a poll skipped */ }
}

// DocWatch is desktop-wide, and on the web it is an admin-only surface. The
// banner follows the same rule, so a regular web user's poll never calls the
// admin-only endpoint and the banner stays hidden for them.
function watchAvailable() {
  return !WEB || (ME && ME.is_admin);
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
  if (last.error_kind === 'hubspot_auth') {
    return 'HubSpot would not accept the token — it may be wrong or missing a '
      + 'scope. Set a new HUBSPOT_TOKEN and try again.';
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
  applyWatchFilesFilter();
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
        // Prep's model/output/notes moved to the Format-on-arrival drawer.
        folder: $('watch-folder').value.trim() || null,
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

$('watch-archive-save').addEventListener('click', async () => {
  const button = $('watch-archive-save');
  const note = $('watch-archive-note');
  note.hidden = true;
  button.disabled = true;
  try {
    const body = await api('/api/watch', {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        archive_enabled: $('watch-archive-enabled').checked,
        // null, not '': an empty box is "no change", the same as the folder.
        archive_folder: $('watch-archive-folder').value.trim() || null,
        archive_include_source: $('watch-archive-source').checked,
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

$('watch-test-email').addEventListener('click', async () => {
  const button = $('watch-test-email');
  const note = $('watch-run-note');
  note.hidden = true;
  button.disabled = true;
  button.textContent = 'Sending…';
  try {
    const body = await api('/api/watch/test-email', { method: 'POST' });
    watchNote(note, `Sent a test email to ${body.to}.`, 'ok');
  } catch (err) {
    watchNoteWithFix(note, err.message);
  } finally {
    button.textContent = 'Send a test email';
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
  // Reveal the editor with the switch, before the round trip, so turning the
  // clock on shows what it will do rather than an empty pause.
  $('watch-inapp-schedule').hidden = !$('watch-auto').checked;
  try {
    await api('/api/watch', {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ auto_ticks: $('watch-auto').checked }),
    });
  } catch (err) {
    $('watch-auto').checked = !$('watch-auto').checked;
    $('watch-inapp-schedule').hidden = !$('watch-auto').checked;
    watchNote(note, err.message, 'error');
  }
});

// The in-app clock's "run at set times" editor. This is the whole schedule on
// the always-on server, which cannot use launchd; on a Mac it sits beside the
// launch agent above, the clock that also runs while DocProof is closed.

function watchScheduleMode() {
  const on = document.querySelector('input[name="watch-mode"]:checked');
  return on ? on.value : 'times';
}

function setWatchScheduleMode(mode) {
  document.querySelectorAll('input[name="watch-mode"]').forEach((r) => {
    r.checked = r.value === mode;
  });
  $('watch-times-editor').hidden = mode !== 'times';
  $('watch-interval-editor').hidden = mode !== 'interval';
}

function addTimeRow(value) {
  const li = document.createElement('li');
  const input = document.createElement('input');
  input.type = 'time';
  input.value = value || '09:00';
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'remove';
  remove.textContent = '×';
  remove.setAttribute('aria-label', 'Remove this time');
  remove.addEventListener('click', () => li.remove());
  li.append(input, remove);
  $('watch-time-list').append(li);
  return input;
}

// Populated once, then reselected each render. `Intl.supportedValuesOf` gives
// the full IANA list where the browser has it; the blank option is "this
// machine's own time", which on the server is UTC — so a fresh setup defaults
// to the zone the browser is in, not to Greenwich.
function fillWatchTimezones(selected) {
  const sel = $('watch-tz');
  if (!sel.dataset.filled) {
    const detected = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
    let zones = [];
    try { zones = Intl.supportedValuesOf('timeZone'); } catch (_) { zones = []; }
    if (!zones.length && detected) zones = [detected];
    const blank = document.createElement('option');
    blank.value = '';
    blank.textContent = detected
      ? `This machine (${detected})` : "This machine's own time";
    sel.append(blank);
    zones.forEach((z) => {
      const o = document.createElement('option');
      o.value = z;
      o.textContent = z;
      sel.append(o);
    });
    sel.dataset.filled = '1';
    sel.dataset.detected = detected;
  }
  sel.value = selected;
}

function fillWatchSchedule(w) {
  const times = w.tick_at_times || [];
  // A saved zone wins, blank included; but a setup nobody has touched takes the
  // browser's, so the times a person types mean their own clock rather than the
  // server's UTC one before they have picked anything.
  const detected = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
  const fresh = !times.length && !w.tick_timezone;
  fillWatchTimezones(fresh ? detected : w.tick_timezone);
  const list = $('watch-time-list');
  list.innerHTML = '';
  times.forEach((t) => addTimeRow(t));
  if (!list.children.length) addTimeRow('09:00');  // one row, ready to edit
  $('watch-every').value = w.tick_every_minutes || 60;
  setWatchScheduleMode(times.length ? 'times' : 'interval');
}

function renderWatchNextRun(w) {
  const el = $('watch-next-run');
  if (!el) return;
  if (w.auto_ticks && w.next_tick_at) {
    const when = new Date(w.next_tick_at);
    el.textContent = 'Next look ' + when.toLocaleString([], {
      weekday: 'short', hour: 'numeric', minute: '2-digit',
    });
  } else {
    el.textContent = '';
  }
}

function watchScheduleSaved(w) {
  if (w.tick_at_times && w.tick_at_times.length) {
    const where = w.tick_timezone ? ` (${w.tick_timezone})` : '';
    return `DocProof will look at ${w.tick_at_times.join(', ')}${where}, `
      + 'every day while it is running.';
  }
  return `DocProof will look every ${w.tick_every_minutes} minutes while it `
    + 'is running.';
}

document.querySelectorAll('input[name="watch-mode"]').forEach((r) => {
  r.addEventListener('change', () => setWatchScheduleMode(watchScheduleMode()));
});

$('watch-time-add').addEventListener('click', () => {
  addTimeRow('09:00').focus();
});

$('watch-schedule-save').addEventListener('click', async () => {
  const button = $('watch-schedule-save');
  const note = $('watch-schedule-note');
  note.hidden = true;
  const mode = watchScheduleMode();
  const payload = { auto_ticks: true };
  if (mode === 'times') {
    const times = [...$('watch-time-list')
      .querySelectorAll('input[type="time"]')]
      .map((i) => i.value).filter(Boolean);
    if (!times.length) {
      watchNote(note, 'Add a time, or switch to “Every so often”.', 'error');
      return;
    }
    payload.tick_at_times = times;
    payload.tick_timezone = $('watch-tz').value;
  } else {
    payload.tick_at_times = [];        // hand the interval clock its job back
    const every = parseInt($('watch-every').value, 10);
    if (!(every >= 5 && every <= 1440)) {
      watchNote(note, 'Minutes must be between 5 and 1440.', 'error');
      return;
    }
    payload.tick_every_minutes = every;
  }
  button.disabled = true;
  try {
    const body = await api('/api/watch', {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    });
    renderWatch(body);
    watchNote(note, watchScheduleSaved(body.watch), 'ok');
  } catch (err) {
    watchNote(note, err.message, 'error');
  } finally {
    button.disabled = false;
  }
});

// ── settings ──────────────────────────────────────────────────────────────

async function loadSettings() {
  const { settings, keys } = await api('/api/settings');
  $('output-dir').value = settings.output_dir;
  $('indesign-template').value = settings.indesign_template || '';
  $('comments').checked = settings.comments;
  $('explanations').checked = settings.explanations;
  $('prep-output-default').value = settings.prep_output || 'book';
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
  renderSubjectChoices(d);
  renderStyleEditor(d);
}

// The subject dropdown on the drop screen: the design file's own list, so a
// subject added in YAML appears here with no code change.
function renderSubjectChoices(d) {
  const select = $('prep-subject');
  if (!select) return;
  const keep = select.value;
  while (select.options.length > 1) select.remove(1);
  (d.subjects || []).forEach((s) => {
    const opt = document.createElement('option');
    opt.value = s.key;
    opt.textContent = `${s.key.replace(/_/g, ' ')} — ${s.family}`
      + (s.key === d.default_subject ? ' (house default)' : '');
    select.append(opt);
  });
  if (keep) select.value = keep;
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
  // Corrections proofs ride in the same drop, so the picker offers PDFs too — a
  // chosen PDF is sorted to the corrections source by routeCorrectionsDrop, never
  // staged as a manuscript.
  input.accept = [...allowedSuffixes(), '.pdf'].join(',');

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
  const choice = settings.prep_output || 'book';
  const radio = document.querySelector(
    `input[name="prep-output"][value="${choice}"]`);
  if (radio) radio.checked = true;
  setEffort(settings.effort || 'low');
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
  // Local Settings have no place in a shared web build — keys live in the
  // server's environment and its file paths mean nothing to a browser — so
  // that tab is gone for everyone. DocWatch and the detection prompts are
  // shared server config an administrator manages, so they are hidden from
  // everyone else but stay for an admin.
  const hide = ['settings'];
  if (!ME.is_admin) hide.push('prompts', 'watch');
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

// The app header is sticky at top:0 and wraps to a taller row on narrow
// widths, so anything that sticks below it (the fix screen's tracker bar) needs
// its real height — not a guess. Measured into --header-h and kept current, so
// the bar pins flush under the nav instead of colliding with it. Read straight
// off the DOM each time rather than trusting one event to fire: it is recomputed
// on load, on every window resize, and — belt and suspenders — whenever the fix
// screen renders, so the bar always has the height that is true right now.
function updateHeaderHeight() {
  const header = document.querySelector('header');
  if (!header) return;
  document.documentElement.style.setProperty(
    '--header-h', `${Math.round(header.getBoundingClientRect().height)}px`);
}

function trackHeaderHeight() {
  updateHeaderHeight();
  window.addEventListener('resize', updateHeaderHeight);
  const header = document.querySelector('header');
  if (header && typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(updateHeaderHeight).observe(header);
  }
}

function startApp() {
  trackHeaderHeight();
  applyMode();
  loadFormats().catch(() => {});
  loadModels().catch(() => {});
  loadFeatures().catch(() => {});
  loadPresets().catch(() => {});
  loadStyleSheet().catch(() => {});
  applyDefaults().catch(() => {});
  if (!WEB) offerUpdateIfBehind();
  renderKind();
  refreshJobs();
  resumeWatchReturn();
  if (watchAvailable()) refreshWatchBanner();
  state.pollTimer = setInterval(() => {
    if (!$('screen-jobs').hidden) refreshJobs();
    if (!$('screen-watch').hidden) loadWatch({ quiet: true }).catch(() => {});
    else if (watchAvailable()) refreshWatchBanner();
  }, 5000);
}

function resumeWatchReturn() {
  // Coming back from Google's consent page on the web build: the callback
  // redirects here with #watch, and — if the sign-in did not take — an error
  // to show in the panel. Query and hash are dropped afterwards so a reload
  // does not replay any of it.
  const params = new URLSearchParams(location.search);
  const err = params.get('watch_auth') === 'error' ? params.get('msg') : null;
  if (location.hash !== '#watch' && !err) return;
  const tab = document.querySelector('.tab[data-screen="watch"]');
  if (tab && !tab.hidden) {
    show('watch');
    if (err) watchNote($('watch-signin-note'), err, 'error');
  }
  history.replaceState(null, '', location.pathname);
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

$('admin-restore').addEventListener('click', async () => {
  const btn = $('admin-restore');
  const note = $('admin-restore-note');
  note.hidden = true;
  btn.disabled = true;
  btn.textContent = 'Rebuilding…';
  try {
    await api('/api/admin/archive/restore', { method: 'POST' });
    // The rebuild runs in the background; the list fills in as it goes, so a
    // reload a moment later shows what came back.
    note.textContent = 'Rebuilding from Drive — your jobs will reappear shortly.';
    note.className = 'action-note ok';
    note.hidden = false;
    setTimeout(() => { refreshJobs().catch(() => {}); }, 2500);
  } catch (err) {
    note.textContent = err.message || 'Could not start the rebuild.';
    note.className = 'action-note error';
    note.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Rebuild from Drive';
  }
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

// -- house InDesign template (prep flows manuscripts into it) -----------------

async function loadHouseTemplate() {
  if (!WEB || !ME || !ME.is_admin) return;
  let data;
  try { data = await api('/api/prep/template'); } catch (_) { return; }
  const summary = $('admin-template-summary');
  const reset = $('admin-template-reset');
  reset.hidden = !data.using_override;
  if (!data.ok) {
    summary.className = 'error';
    summary.textContent = data.error
      || 'The current template could not be read.';
    return;
  }
  summary.className = 'muted';
  const shape = `${data.stories} stories, ${data.spreads} spreads`;
  summary.textContent = data.using_override
    ? `Using your uploaded template “${data.name}” — ${shape}.`
    : `Using the shipped placeholder — ${shape}. Upload your house IDML to `
      + 'replace it for everyone.';
}

$('admin-template-pick').addEventListener('click',
  () => $('admin-template-file').click());

$('admin-template-file').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const note = $('admin-template-note');
  note.className = 'action-note muted';
  note.textContent = 'Checking the template…';
  note.hidden = false;
  const fd = new FormData();
  fd.append('file', file);
  try {
    await api('/api/prep/template', { method: 'POST', body: fd });
    note.className = 'action-note ok';
    note.textContent = 'Installed. Prep flows into it from the next document on.';
    loadHouseTemplate();
  } catch (err) {
    note.className = 'action-note error';
    note.textContent = err.message;
  } finally {
    e.target.value = '';
  }
});

$('admin-template-reset').addEventListener('click', async () => {
  const note = $('admin-template-note');
  try {
    await api('/api/prep/template', { method: 'DELETE' });
    note.className = 'action-note ok';
    note.textContent = 'Back to the shipped template.';
    note.hidden = false;
    loadHouseTemplate();
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
  loadHouseTemplate();
}

// ═══ Automations: the workflow registry ═══════════════════════════════════
//
// The Automations tab is a scalable list of "trigger → effect" workflows, not a
// wall of cards: one row per workflow, a click opens its config drawer, and the
// list stays scannable as the count grows toward dozens. Today's rows are seeded
// from a small descriptor model built off the watch status and the promo/plan
// settings; a new workflow becomes a new descriptor, not new markup.

const wfUI = { search: '', filter: 'all', sort: 'status', selected: null };

function automationWorkflows() {
  const w = state.watchStatus || {};
  const ps = state.promoSettings || {};
  const pl = state.planSettings || {};
  const folderReady = !!(w.folder_id && w.signed_in);
  const promoReady = !!(ps.hubspot_enabled && ps.hubspot_promo_ready_value
                        && ps.hubspot_promo_done_value);
  const planReady = !!(pl.hubspot_enabled && pl.hubspot_plan_property
                       && pl.hubspot_plan_needed_value
                       && pl.hubspot_plan_done_value);
  // When a workflow reads "Needs setup", `setup` names what's missing and where
  // to fix it: a `target` tab jumps straight there, `self` opens the drawer
  // whose own fields are the fix, and a null target means the blocker is a
  // server credential no button here can set.
  const hubspotMissing = { hint: 'HubSpot isn’t connected — set HUBSPOT_TOKEN '
    + 'on the server to enable it', target: null };
  return [
    {
      id: 'prep', name: 'Format on arrival', sub: 'Prepare new manuscripts',
      trigger: { text: 'Folder arrival', hs: false }, effect: 'Prep / format',
      config: 'wf-config-prep', enabled: folderReady, toggleable: false,
      status: folderReady ? 'on' : 'setup',
      setup: folderReady ? null : { hint: 'Connect a Google Drive folder to '
        + 'switch this on.', target: 'connection' },
    },
    {
      id: 'promo', name: 'Promo copy', sub: 'Teaser + 12 social posts',
      trigger: {
        text: ps.hubspot_promo_ready_value
          ? 'HubSpot: ' + ps.hubspot_promo_ready_value : 'HubSpot status',
        hs: true,
      },
      effect: 'Teaser + posts', config: 'wf-config-promo',
      enabled: !!ps.promo_enabled, toggleable: true,
      status: !ps.promo_enabled ? 'off' : (promoReady ? 'on' : 'setup'),
      setup: (!ps.promo_enabled || promoReady) ? null
        : (ps.hubspot_enabled
            ? { hint: 'Fill in the HubSpot trigger values.', target: 'self' }
            : hubspotMissing),
    },
    {
      id: 'plan', name: 'Marketing plan', sub: 'Author-facing plan document',
      trigger: {
        text: pl.hubspot_plan_property
          ? 'HubSpot: ' + pl.hubspot_plan_property + ' = '
            + (pl.hubspot_plan_needed_value || 'Needed')
          : 'HubSpot property',
        hs: true,
      },
      effect: 'Plan .docx', config: 'wf-config-plan',
      enabled: !!pl.plan_enabled, toggleable: true,
      status: !pl.plan_enabled ? 'off' : (planReady ? 'on' : 'setup'),
      setup: (!pl.plan_enabled || planReady) ? null
        : (pl.hubspot_enabled
            ? { hint: 'Fill in the HubSpot property and values.', target: 'self' }
            : hubspotMissing),
    },
  ];
}

// A text button that switches Automations sub-tabs when clicked (delegated in
// initAutoTabs). Used inline in hints and the passes summary.
function wfJump(text, tab) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'link wf-jump';
  b.dataset.tab = tab;
  b.textContent = text;
  return b;
}

const WF_STATUS_LABEL = { on: 'On', setup: 'Needs setup', off: 'Off' };
const WF_STATUS_ORDER = { setup: 0, on: 1, off: 2 };

function wfLastLook() {
  const w = state.watchStatus || {};
  if (!w.last_tick_at) return 'Never';
  const t = new Date(w.last_tick_at);
  if (isNaN(t)) return '—';
  return t.toLocaleString([], { month: 'short', day: 'numeric',
                                hour: '2-digit', minute: '2-digit' });
}

function renderRegistry() {
  const rowsEl = $('wf-rows');
  if (!rowsEl) return;
  let items = automationWorkflows();
  const q = wfUI.search.trim().toLowerCase();
  if (q) {
    items = items.filter((x) =>
      (x.name + ' ' + x.sub + ' ' + x.trigger.text + ' ' + x.effect)
        .toLowerCase().includes(q));
  }
  if (wfUI.filter !== 'all') items = items.filter((x) => x.status === wfUI.filter);
  items.sort((a, b) => {
    if (wfUI.sort === 'name') return a.name.localeCompare(b.name);
    if (wfUI.sort === 'effect') return a.effect.localeCompare(b.effect);
    return WF_STATUS_ORDER[a.status] - WF_STATUS_ORDER[b.status]
      || a.name.localeCompare(b.name);
  });

  rowsEl.innerHTML = '';
  items.forEach((x) => rowsEl.append(registryRow(x)));
  $('wf-empty').hidden = items.length > 0;
  renderPassesSummary();
  applyDrawer();
}

// One shared line above the table. "Last look" and "Next" are properties of the
// single pass clock, not of any one workflow — showing them per row implied each
// ran on its own schedule. Here they belong to the clock, with a jump to set it.
function renderPassesSummary() {
  const el = $('wf-passes');
  if (!el) return;
  el.innerHTML = '';
  const w = state.watchStatus || {};
  const anyEnabled = automationWorkflows().some((x) => x.enabled);
  if (!anyEnabled) {
    el.append(document.createTextNode('Nothing runs yet. Turn a workflow on, '
      + 'then choose when passes run under '));
    el.append(wfJump('Schedule', 'schedule'), document.createTextNode('.'));
    return;
  }
  let nextClause;
  if (w.times && w.times.length) nextClause = `next at ${w.times.join(', ')}`;
  else if (w.auto_ticks) nextClause = 'next on the in-app timer';
  else nextClause = 'no automatic passes scheduled';
  el.append(document.createTextNode(
    `Passes run on one shared clock — last looked ${wfLastLook()}, `
    + `${nextClause}. `));
  el.append(wfJump('Change under Schedule', 'schedule'),
            document.createTextNode('.'));
}

function registryRow(x) {
  const tr = document.createElement('tr');
  tr.dataset.wf = x.id;
  if (wfUI.selected === x.id) tr.classList.add('selected');

  const tdToggle = document.createElement('td');
  const tog = document.createElement('button');
  tog.type = 'button';
  tog.className = 'wf-toggle' + (x.enabled ? ' on' : '');
  tog.setAttribute('role', 'switch');
  tog.setAttribute('aria-checked', x.enabled ? 'true' : 'false');
  tog.setAttribute('aria-label', (x.enabled ? 'Disable ' : 'Enable ') + x.name);
  if (x.toggleable) {
    tog.addEventListener('click', (e) => { e.stopPropagation(); toggleWorkflow(x); });
  } else {
    tog.disabled = true;
    tog.title = 'Runs whenever the folder is connected';
  }
  tdToggle.append(tog);
  tr.append(tdToggle);

  // The name is a real button — the row's keyboard-reachable "open this
  // workflow" control (the bare <tr> click is a mouse convenience on top).
  const tdName = document.createElement('td');
  tdName.className = 'wf-row-name';
  const open = document.createElement('button');
  open.type = 'button';
  open.className = 'wf-open';
  open.setAttribute('aria-expanded', wfUI.selected === x.id ? 'true' : 'false');
  const b = document.createElement('b'); b.textContent = x.name;
  const small = document.createElement('small'); small.textContent = x.sub;
  open.append(b, small);
  open.addEventListener('click', (e) => { e.stopPropagation(); openDrawer(x.id); });
  tdName.append(open);
  tr.append(tdName);

  tr.append(chipCell(x.trigger.text, x.trigger.hs));
  tr.append(chipCell(x.effect, false));

  const tdStatus = document.createElement('td');
  if (x.status === 'setup') {
    // The pill is the call to action: it jumps to whatever needs finishing —
    // another tab, or this workflow's own drawer.
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pill setup actionable';
    btn.textContent = 'Needs setup →';
    if (x.setup && x.setup.hint) btn.title = x.setup.hint;
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const t = x.setup && x.setup.target;
      if (t && t !== 'self') window.__activateAutoTab(t);
      else openDrawer(x.id);
    });
    tdStatus.append(btn);
  } else {
    const pill = document.createElement('span');
    pill.className = 'pill ' + x.status;
    pill.textContent = WF_STATUS_LABEL[x.status];
    tdStatus.append(pill);
  }
  tr.append(tdStatus);

  // Mouse convenience only; keyboard uses the name/toggle/setup buttons. Ignore
  // clicks that land on an inner control so they aren't double-handled.
  tr.addEventListener('click', (e) => {
    if (e.target.closest('button, a, input')) return;
    openDrawer(x.id);
  });
  return tr;
}

function chipCell(text, hs) {
  const td = document.createElement('td');
  const chip = document.createElement('span');
  chip.className = 'wf-chip' + (hs ? ' hs' : '');
  chip.textContent = text;
  td.append(chip);
  return td;
}

function setRowSelected(id) {
  const rows = $('wf-rows');
  if (!rows) return;
  rows.querySelectorAll('tr').forEach((tr) => {
    const on = tr.dataset.wf === id;
    tr.classList.toggle('selected', on);
    const ob = tr.querySelector('.wf-open');
    if (ob) ob.setAttribute('aria-expanded', on ? 'true' : 'false');
  });
}

function openDrawer(id) {
  wfUI.selected = id;
  setRowSelected(id);
  applyDrawer();
  const drawer = $('wf-drawer');
  if (drawer && !drawer.hidden) {
    const close = $('wf-drawer-close');
    if (close) close.focus({ preventScroll: true });
    // Stacked under the table on a narrow screen, the drawer opens off-screen —
    // bring it into view so a tap visibly does something.
    if (window.matchMedia('(max-width: 60rem)').matches) {
      drawer.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }
}

function closeDrawer() {
  const wasOpen = wfUI.selected;
  wfUI.selected = null;
  setRowSelected(null);
  applyDrawer();
  // Return focus to the row that was open, so keyboard doesn't jump to the top.
  const rows = $('wf-rows');
  if (wasOpen && rows) {
    const ob = rows.querySelector(`tr[data-wf="${wasOpen}"] .wf-open`);
    if (ob) ob.focus();
  }
}

function applyDrawer() {
  const drawer = $('wf-drawer');
  const layout = $('wf-layout');
  if (!drawer || !layout) return;
  const x = automationWorkflows().find((w) => w.id === wfUI.selected);
  ['wf-config-prep', 'wf-config-promo', 'wf-config-plan'].forEach((cid) => {
    const el = $(cid); if (el) el.hidden = !(x && cid === x.config);
  });
  drawer.hidden = !x;
  layout.classList.toggle('with-drawer', !!x);
  // Widen the whole Automations surface only while the Workflows tab is showing
  // a drawer — otherwise the wide class leaked onto Connection/Schedule/History
  // and stretched their prose to unreadable line lengths.
  const screen = $('screen-watch');
  const wfActive = ($('auto-panel-workflows') || {}).classList
    && $('auto-panel-workflows').classList.contains('is-active');
  if (screen) screen.classList.toggle('wf-wide', !!x && wfActive);
  // The prep drawer's setup banner: what's missing, and a jump to fix it.
  const banner = $('wf-prep-setup');
  if (banner) {
    const s = x && x.id === 'prep' ? x.setup : null;
    banner.hidden = !s;
    banner.innerHTML = '';
    if (s) {
      banner.append(document.createTextNode(s.hint + ' '));
      if (s.target) banner.append(wfJump('Go to Connection →', s.target));
    }
  }
  if (x) {
    $('wf-drawer-title').textContent = x.name;
    $('wf-drawer-sub').textContent = x.trigger.text + ' → ' + x.effect;
  }
}

async function toggleWorkflow(x) {
  try {
    if (x.id === 'promo') {
      await api('/api/promo/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ promo_enabled: !x.enabled }),
      });
      await loadPromoSettings();
    } else if (x.id === 'plan') {
      await api('/api/promo/plan-settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan_enabled: !x.enabled }),
      });
      await loadPlanSettings();
    }
  } catch (e) {
    const note = $(x.id === 'promo' ? 'promo-settings-status'
                                    : 'plan-settings-status');
    if (note) { note.hidden = false; note.textContent = e.message; }
  }
}

// The Automations sub-tabs (Workflows / Connection / History), the same
// roving-tabindex pattern the Settings screen uses.
(function initAutoTabs() {
  const tabs = $('auto-tabs');
  const panels = $('auto-panels');
  if (!tabs || !panels) return;
  const btns = () => [...tabs.querySelectorAll('.subtab')];
  function activate(btn, focus) {
    btns().forEach((b) => {
      const on = b === btn;
      b.setAttribute('aria-selected', on ? 'true' : 'false');
      b.tabIndex = on ? 0 : -1;
    });
    panels.querySelectorAll('.tabpanel').forEach((p) =>
      p.classList.toggle('is-active', p.dataset.tab === btn.dataset.tab));
    // Recompute the wide class against the now-active panel, so a drawer left
    // open on Workflows doesn't widen Connection/Schedule/History.
    if (typeof applyDrawer === 'function') applyDrawer();
    if (focus) btn.focus();
  }
  tabs.addEventListener('click', (e) => {
    const btn = e.target.closest('.subtab');
    if (btn) activate(btn, false);
  });
  // Inline "go to <tab>" buttons anywhere in the Automations screen.
  const screen = $('screen-watch');
  if (screen) screen.addEventListener('click', (e) => {
    const jump = e.target.closest('.wf-jump');
    if (jump && jump.dataset.tab) { e.preventDefault(); activate(
      tabs.querySelector(`[data-tab="${jump.dataset.tab}"]`), true); }
  });
  tabs.addEventListener('keydown', (e) => {
    const list = btns();
    const i = list.indexOf(document.activeElement);
    if (i < 0) return;
    let j = -1;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') j = (i + 1) % list.length;
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') j = (i - 1 + list.length) % list.length;
    else if (e.key === 'Home') j = 0;
    else if (e.key === 'End') j = list.length - 1;
    if (j < 0) return;
    e.preventDefault();
    activate(list[j], true);
  });
  window.__activateAutoTab = (name) => {
    const btn = tabs.querySelector(`[data-tab="${name}"]`);
    if (btn) activate(btn, false);
  };
})();

// The registry's search / filter / sort, and the drawer close.
$('wf-search').addEventListener('input', () => {
  wfUI.search = $('wf-search').value; renderRegistry();
});
$('wf-sort').addEventListener('change', () => {
  wfUI.sort = $('wf-sort').value; renderRegistry();
});
$('wf-filters').addEventListener('click', (e) => {
  const btn = e.target.closest('button');
  if (!btn) return;
  wfUI.filter = btn.dataset.filter;
  $('wf-filters').querySelectorAll('button').forEach((b) =>
    b.classList.toggle('active', b === btn));
  renderRegistry();
});
$('wf-drawer-close').addEventListener('click', closeDrawer);
$('wf-drawer').addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { e.stopPropagation(); closeDrawer(); }
});

// The Format-on-arrival drawer now owns all of prep's settings — the model, the
// output, the failure note, and the source-label filter — so its Save writes
// them together. (The watched folder itself lives under Connection.)
$('wf-prep-save').addEventListener('click', async () => {
  const button = $('wf-prep-save');
  const note = $('wf-prep-note');
  note.hidden = true; button.disabled = true;
  try {
    const body = await api('/api/watch', {
      method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        model: $('watch-model').value,
        prep_output: $('watch-output').value,
        upload_failure_note: $('watch-failure-note').checked,
        require_source_label: $('watch-require-label').checked,
      }),
    });
    renderWatch(body);
    watchNote(note, 'Saved.', 'ok');
  } catch (err) {
    watchNote(note, err.message, 'error');
  } finally { button.disabled = false; }
});

// History: filter the runs table by book name or status, so it stays usable as
// the list grows.
function applyWatchFilesFilter() {
  const input = $('watch-files-filter');
  const table = $('watch-files');
  if (!input || !table) return;
  const q = input.value.trim().toLowerCase();
  const rows = [...table.querySelectorAll('tr')];
  rows.forEach((tr, idx) => {
    if (idx === 0) return;               // the header row always stays
    tr.hidden = !!q && !tr.textContent.toLowerCase().includes(q);
  });
}
$('watch-files-filter').addEventListener('input', applyWatchFilesFilter);

boot();
