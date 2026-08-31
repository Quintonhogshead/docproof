/* Cover Canvas — entry point.

   Boots one of two things: the job picker (no ?job= in the URL, or the key is
   missing) or the editor itself. Everything below the picker is wiring: the
   store owns the doc, the engine owns the pixels, the panels own the chrome,
   and this file is the only place that knows they exist. */

import { api, postJSON, postStream, fileObjectURL, toast, getKey, setKey, setConcept, ApiError } from './api.js';
import { createStore, clone, NUDGE, NUDGE_BIG } from './ops.js';
import { createEngine } from './engine.js';
import { el, buildShelf, buildLayerRail, buildPropsRail, newLayerId, currentQuality, guidesEnabled } from './panels.js';
import { buildAssistant } from './assistant.js';
import { panels as wrapPanels, checkPanelsAgree } from './wrap.js';

const root = document.getElementById('app');

function b64ToBlob(b64, type = 'image/png') {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type });
}

/* Families offered in the type dropdown: whatever the engine serves, plus the
   ones this cover already uses (a doc can outlive a fonts.css edit) and the
   two stacks every machine has. */
const FALLBACK_FAMILIES = ['Georgia', 'Helvetica'];
let FAMILIES = FALLBACK_FAMILIES.slice();

async function loadFamilies(doc) {
  const used = new Set((doc.layers || []).filter((l) => l.family).map((l) => l.family));
  let served = [];
  try {
    const css = await fetch('/api/canvas/fonts.css', { cache: 'no-store' }).then((r) => (r.ok ? r.text() : ''));
    served = [...css.matchAll(/font-family:\s*(['"]?)([^;'"]+)\1/g)].map((m) => m[2].trim());
  } catch { /* offline or not built yet — the doc's own families still work */ }
  FAMILIES = [...new Set([...served, ...used, ...FALLBACK_FAMILIES])].filter(Boolean).sort();
}

/* ------------------------------------------------------------------ boot */
function jobFromURL() {
  return new URLSearchParams(location.search).get('job') || '';
}

/* Which cover of that job — the number the studio's "Edit in Cover Canvas"
   door puts in the URL. Absent is a real answer ("whichever session this job
   has"), not zero: it is what the canvas picker's own links say, and the
   server answers it with the job's default concept. */
function conceptFromURL() {
  const raw = new URLSearchParams(location.search).get('concept');
  return raw === null || raw === '' ? null : raw;
}

async function openDoc(jobId) {
  try {
    const res = await api(`/api/canvas/${encodeURIComponent(jobId)}`);
    if (res && res.doc) return res.doc;
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 404) throw err;
  }
  // No session for this concept yet: ingest that concept of the finished
  // cover job into one. The studio's "Edit in Cover Canvas" door names the
  // concept the person was looking at; without one the server picks the
  // first ready concept. (An existing session for THAT concept wins over
  // both — the GET above returned it.)
  const concept = conceptFromURL();
  const body = { job_id: jobId };
  if (concept !== null && concept !== '') body.concept = Number(concept);
  return (await postJSON('/api/canvas/open', body)).doc;
}

/* Only a finished cover job has plates to edit. cover_list_jobs reports the
   pipeline's own JobState.status ("directing" | "working" | "ready" |
   "error"); "done"/"complete" are accepted as synonyms in case the wording
   ever moves. */
const EDITABLE = new Set(['ready', 'done', 'complete']);

async function showPicker(message) {
  root.textContent = '';
  const listBox = el('div');
  const err = el('div', { class: 'ferr', text: message || '' });
  const sheet = el('div', { class: 'sheetbox' }, [
    el('h1', { html: 'Cover Canvas' }),
    el('p', { class: 'lede', text: 'Open a finished cover as editable layers.' }),
    err, listBox,
    // The studio is the same server one page over; a picker with no covers
    // in it must be a door, not a dead end.
    el('p', { class: 'stack-s' }, [el('button', {
      class: 'btn', type: 'button', text: 'New cover — open Cover Studio',
      onclick: () => { location.href = '/sc-cover.html'; },
    })]),
  ]);
  root.appendChild(el('div', { class: 'picker' }, sheet));

  const unlock = () => {
    const field = el('input', { type: 'password', autocomplete: 'off', 'aria-label': 'Cover key' });
    // A key that came back 401 goes straight back to the box: a person
    // re-typing a password must never be a dead end. And when the URL still
    // names the cover they were headed for, a good key resumes THAT — the
    // picker was only ever the detour.
    const submit = () => {
      const typed = field.value.trim();
      if (!typed) { field.focus(); return; }
      setKey(typed);
      if (jobFromURL()) { location.reload(); return; }
      loadList();
    };
    listBox.textContent = '';
    listBox.append(
      el('div', { class: 'field' }, [el('label', { text: 'Cover key' }), field]),
      el('button', {
        class: 'btn primary', type: 'button', text: 'Unlock', onclick: submit,
      }),
    );
    field.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') submit();
    });
    field.focus();
  };

  async function loadList() {
    err.textContent = '';
    listBox.textContent = 'Loading…';
    if (!getKey()) { unlock(); return; }
    let jobs;
    try {
      jobs = (await api('/api/cover/jobs')).jobs || [];
    } catch (e) {
      err.textContent = e.message;
      if (e.status === 401) unlock(); else listBox.textContent = '';
      return;
    }
    const usable = jobs.filter((j) => EDITABLE.has(String(j.status || '').toLowerCase()));
    listBox.textContent = '';
    if (!usable.length) {
      listBox.appendChild(el('div', { class: 'rail-empty', text: 'No finished covers yet — run one in Cover Studio first.' }));
      return;
    }
    usable.forEach((j) => listBox.appendChild(el('button', {
      class: 'jobrow', type: 'button',
      onclick: () => { location.search = `?job=${encodeURIComponent(j.job_id)}`; },
    }, [
      el('span', { class: 'jt', text: j.title || j.job_id }),
      el('span', { class: 'js', text: j.status }),
      el('span', { class: 'jd', text: (j.created || '').slice(0, 10) }),
      el('span', { class: 'jc', text: `$${Number(j.total_usd || 0).toFixed(2)}` }),
    ])));
  }
  loadList();
}

/* ---------------------------------------------------------------- editor */
function buildEditor(jobId, doc) {
  root.textContent = '';

  /* --- plate images. Fetched as blobs so the key header rides along; kept in
     one cache so a re-render never re-downloads a plate. */
  const images = new Map();
  const urls = new Map();
  const pending = new Set();
  /* A plate being rendered right now, drawn in place of the plate it is
     replacing (keyed by the OLD source, which is what the document still
     points at while the call is in flight). Cleared when the call ends,
     whichever way it ends. */
  const previews = new Map();
  /* One chip per plate call in flight, and which layers those calls are on.
     Declared beside the image cache because the whole progressive-render
     path is one idea: what is on screen, and what is on its way. */
  const chips = el('div', { class: 'plate-chips' });
  const rolling = new Set();
  function imageFor(name) {
    if (!name) return null;
    if (previews.has(name)) return previews.get(name);
    if (images.has(name)) return images.get(name);
    if (!pending.has(name)) {
      pending.add(name);
      imageURL(name).then((url) => new Promise((res, rej) => {
        const img = new Image();
        img.onload = () => { images.set(name, img); res(); };
        img.onerror = rej;
        img.src = url;
      })).then(() => engine.render())
        .catch(() => toast(`Could not load the plate ${name}.`, 'err'))
        .finally(() => pending.delete(name));
    }
    return null;
  }
  function imageURL(name) {
    if (!urls.has(name)) urls.set(name, fileObjectURL(jobId, name));
    return urls.get(name);
  }

  const stagewrap = el('div', { class: 'stagewrap' });
  const stagehost = el('div', { class: 'stagehost' });
  const zval = el('span', { class: 'zval' });
  stagewrap.append(stagehost, el('div', { class: 'zoombar' }, [
    el('button', { class: 'btn small', type: 'button', text: '−', title: 'Zoom out', onclick: () => engine.zoomBy(1 / 1.2) }),
    zval,
    el('button', { class: 'btn small', type: 'button', text: '+', title: 'Zoom in', onclick: () => engine.zoomBy(1.2) }),
    el('button', { class: 'btn small', type: 'button', text: 'Fit', onclick: () => engine.zoomToFit() }),
  ]), el('div', { class: 'stagehelp', text: 'space-drag to pan · wheel to zoom' }));

  const store = createStore({
    doc,
    onChange: (d, meta) => { if (meta && meta.costOnly) { shelf.update(); return; } refresh(); },
    onStatus: () => shelf.update(),
    send: (ops) => postJSON(`/api/canvas/${encodeURIComponent(jobId)}/ops`, { ops }),
    onError: (e, info) => {
      if (!info || !info.permanent) {
        toast(`Edits aren’t saving: ${e.message}`, 'err', 9000);
        return;
      }
      /* The server refused the batch outright (a 409 naming one bad op). The
         local doc is now ahead of what is on disk in a way we can't reconcile
         by replaying, so take the server's copy back rather than drifting. */
      toast(`That edit didn’t stick: ${e.message} — reloading the saved cover.`, 'err', 9000);
      api(`/api/canvas/${encodeURIComponent(jobId)}`)
        .then((res) => { if (res && res.doc) store.adopt(res.doc); })
        .catch(() => toast('Could not reload the saved cover — your edits are still on screen.', 'err'));
    },
  });

  const ctx = {
    jobId,
    store,
    families: () => FAMILIES,
    imageURL,
    selectedId: () => engine.selectedId,
    selectedLayer: () => (engine.selectedId ? store.layer(engine.selectedId) : null),
    select: (id) => engine.select(id),
    repaint: () => engine.render(),
    refreshChrome: () => shelf.update(),
    undo: () => { store.undo(); },
    redo: () => { store.redo(); },
    interacting: (on) => engine.setInteracting(on),
    art: { reroll, finalize, rendering: (id) => rolling.has(id) },
    shelf: {},
  };

  const engine = createEngine({
    host: stagehost,
    getDoc: () => store.doc,
    imageFor,
    onSelect: () => { document.activeElement?.blur?.(); refresh(); },
    onCommit: (ops) => store.apply(ops),
    onView: (s) => { zval.textContent = `${Math.round(s * 100)}%`; },
  });
  ctx.engine = engine;
  engine.setGuidesVisible(guidesEnabled());

  const shelf = buildShelf(ctx);
  const layerRail = buildLayerRail(ctx);
  const props = buildPropsRail(ctx);
  const assistant = buildAssistant(ctx);
  const rightRail = el('div', { class: 'rail right' }, [props.root, assistant.root]);

  root.append(shelf.root, el('div', { class: 'main' }, [layerRail.root, stagewrap, rightRail]), chips);

  function refresh() {
    engine.render();
    layerRail.update();
    props.update();
    shelf.update();
  }

  /* ------------------------------------------------------- shelf actions */
  Object.assign(ctx.shelf, {
    addText() {
      const layer = {
        id: newLayerId(), kind: 'text', name: 'New text', visible: true, locked: false, opacity: 1,
        frame: { x: 0.5, y: 0.5, w: 0.76, h: 0.12, rotation: 0, flip_h: false, flip_v: false },
        effects: [],
        text: 'New text', family: FAMILIES[0] || 'Georgia', style: 'regular', size: 0.06,
        color: '#ffffff', tracking: 0.02, align: 'center', line_height: 1.15,
        warp: { kind: 'none', amount: 0 },
      };
      store.apply({ op: 'add_layer', layer, index: null });
      engine.select(layer.id);
    },
    addShape() {
      const layer = {
        id: newLayerId(), kind: 'shape', name: 'Shape', visible: true, locked: false, opacity: 1,
        frame: { x: 0.5, y: 0.5, w: 0.4, h: 0.2, rotation: 0, flip_h: false, flip_v: false },
        effects: [], shape: 'rect', fill: '#000000', stroke: null, stroke_w: 0, radius: 0,
      };
      store.apply({ op: 'add_layer', layer, index: null });
      engine.select(layer.id);
    },
    addFrame() {
      const layer = {
        id: newLayerId(), kind: 'frame', name: 'Ornament', visible: true, locked: false, opacity: 1,
        frame: { x: 0.5, y: 0.5, w: 0.9, h: 0.9, rotation: 0, flip_h: false, flip_v: false },
        effects: [], preset: 'double_rule', stroke: '#efe4cb', stroke_w: 0.0025, inset: 0.02, fill: null,
      };
      store.apply({ op: 'add_layer', layer, index: null });
      engine.select(layer.id);
    },
    deleteLayer() {
      const l = ctx.selectedLayer();
      if (!l) return;
      engine.select(null);
      store.apply({ op: 'remove_layer', layer_id: l.id });
    },

    /* §15 doctrine, as one click. */
    scrim() {
      const t = ctx.selectedLayer();
      if (!t || t.kind !== 'text') return;
      const f = t.frame;
      const layer = {
        id: newLayerId(), kind: 'scrim', name: `Scrim — ${t.name || 'type'}`,
        visible: true, locked: false, opacity: 1,
        frame: {
          x: f.x, y: f.y, w: Math.min(1.6, f.w * 1.2), h: Math.min(1.6, f.h * 1.2),
          rotation: f.rotation || 0, flip_h: false, flip_v: false,
        },
        effects: [], color: '#000000',
        // Dense where the type sits, fading away from it: down for a block in
        // the top half, up for one in the bottom half.
        gradient: { angle: f.y < 0.5 ? 90 : 270, stops: [{ at: 0, alpha: 0.72 }, { at: 1, alpha: 0 }] },
      };
      store.apply({ op: 'add_layer', layer, index: store.indexOf(t.id) });
      engine.select(layer.id);
    },
    /* A toggle, not a one-way door. §15.22: a cutout needs a planned PAIR —
       a wide ambient that seats it in the field, and a tight contact shadow
       that says where it touches — and the point of a planned pair is being
       able to see the layer with and without it. Pressing it again takes the
       shadows back off.

       The button owns this layer's drop shadows outright: turning it on has
       always replaced whatever drop shadows were there, so turning it off
       clears them. Anything hand-tuned that must survive lives in the
       effects list on the properties rail, where it can be edited rather
       than toggled. */
    shadowStack() {
      const l = ctx.selectedLayer();
      if (!l) return;
      const on = (l.effects || []).some((e) => e.type === 'drop_shadow');
      const effects = (l.effects || []).filter((e) => e.type !== 'drop_shadow');
      if (!on) {
        effects.push(
          { type: 'drop_shadow', params: { dx: 0, dy: 0.014, blur: 0.032, color: '#000000', alpha: 0.34 } },
          { type: 'drop_shadow', params: { dx: 0.0015, dy: 0.0026, blur: 0.004, color: '#000000', alpha: 0.62 } },
        );
      }
      store.apply({ op: 'set_effects', layer_id: l.id, effects });
      toast(on ? 'Shadow stack off.'
        : 'Shadow stack on — a wide ambient and a tight contact shadow.', 'ok');
    },
    repair() {
      const l = ctx.selectedLayer();
      if (!l || l.kind !== 'art') return;
      if (engine.marqueeActive) { engine.endMarquee(); shelf.update(); return; }
      toast('Drag a box over the part of the plate to repair.');
      engine.beginMarquee(l.id, askForRepair);
      shelf.update();
    },

    /* §15.23's cardinal rule as one click: the server draws the band mask
       itself, so unlike a repair there is nothing for the user to draw.

       It is easy to read this button as a shadow control sitting next to the
       shadow one. It is not: it REPAINTS the bottom of the plate as real
       ground (with a contact shadow in it), which is a paid image call and a
       new picture down there. Every string it shows says so — the button's
       own title, the progress chip, and the toast — because the difference
       between "free effect" and "money and new pixels" is the one thing a
       person has to know before pressing it. */
    async ground() {
      const l = ctx.selectedLayer();
      if (!l || l.kind !== 'art') return;
      try {
        const res = await plateCall('ground', { layer_id: l.id },
          'Repainting the bottom of the plate as ground…');
        toast(`New ground painted into the plate${money(res.cost_usd)} — `
          + `⌘Z puts the old plate back.`, 'ok', 9000);
      } catch (e) {
        toast(e.message, 'err');
      }
    },

    /* The one AI verb that spends nothing. Its measurement is the point, so
       it goes where reasoning about the cover lives — the transcript — as
       well as into the toast that says it happened. */
    async rebalance() {
      const l = ctx.selectedLayer();
      if (!l || l.kind !== 'art') return;
      try {
        const res = await plateCall('rebalance', { layer_id: l.id },
          'Measuring the plate…', { stream: false });
        const line = res.measured || 'Levels rebalanced.';
        assistant.note(line);
        toast(line, 'ok', 11000);
      } catch (e) {
        toast(e.message, 'err');
      }
    },
    wrapDialog() { openWrapDialog(); },

    /* The print deliverable: the same composite the PNG export makes, in a
       page of exactly the right physical size. Only offered on a wrapped
       document, because only a wrap knows how many inches across it is. */
    async exportPDF() {
      const d = store.doc;
      if (!d.wrap) { toast('Make this a print wrap first — a front cover has no physical size.', 'err'); return; }
      const sheet = wrapPanels(d.wrap).sheet;
      /* The raster must not be SMALLER than the sheet's own pixel size, or the
         PDF would print a page of the right inches at the wrong resolution. On
         a wrap the canvas already IS the sheet, so this is 1× in practice and
         the ratio is here for the document that is somehow behind — capped at
         4 because past that a 13-inch sheet is a canvas the browser refuses to
         allocate, and a refusal is worse than a slightly soft plate. */
      const ratio = Math.min(4, Math.max(1, sheet.w_px / (d.canvas.w || sheet.w_px)));
      const size = `${round2(sheet.w_in)} × ${round2(sheet.h_in)} in @ ${sheet.dpi}dpi`;
      const busy = el('div', { class: 'busy', text: `Rendering the sheet at ${ratio.toFixed(2)}×…` });
      document.body.appendChild(busy);
      try {
        const b64 = engine.exportDataURL(ratio).split(',')[1];
        let name;
        try {
          const res = await postJSON(`/api/canvas/${encodeURIComponent(jobId)}/export`,
            { png_b64: b64, format: 'pdf' });
          name = res && res.name;
        } catch (e) {
          toast(`The print PDF was not written — the server said: ${e.message}`, 'err', 9000);
          return;
        }
        if (!name) { toast('The server took the sheet but named no PDF.', 'err'); return; }
        /* Fetched back rather than built here: the PDF is the SERVER's file
           (it is the one that knows the page size), and this download is a
           copy of what now sits with the job. Through the api layer so the
           cover key rides along — a bare href could not carry it. */
        const url = await fileObjectURL(jobId, name);
        const a = el('a', { href: url, download: `${jobId}_wrap.pdf` });
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 8000);
        toast(`Print PDF saved as ${name} — ${size}.`, 'ok', 9000);
      } finally {
        busy.remove();
      }
    },
    async exportPNG() {
      const busy = el('div', { class: 'busy', text: 'Rendering at 2×…' });
      document.body.appendChild(busy);
      try {
        const url = engine.exportDataURL(2);
        const b64 = url.split(',')[1];
        let name = `${jobId}_cover.png`;
        try {
          const res = await postJSON(`/api/canvas/${encodeURIComponent(jobId)}/export`, { png_b64: b64 });
          if (res && res.name) name = res.name;
          toast(`Saved to the job as ${name}.`, 'ok');
        } catch (e) {
          toast(`Saved locally only — the server said: ${e.message}`, 'err');
        }
        // Through a blob, not the data URL: a 2048×3072 PNG is tens of MB of
        // base64 and some browsers refuse a href that long.
        const blobURL = URL.createObjectURL(b64ToBlob(b64));
        const a = el('a', { href: blobURL, download: `${jobId}_cover.png` });
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(blobURL), 8000);
      } finally {
        busy.remove();
      }
    },
  });

  /* ------------------------------------------------------------ the wrap */
  const round2 = (v) => (Math.round(v * 100) / 100).toFixed(2);
  // Four decimals is a ten-thousandth of an inch: past what any press holds,
  // and short of the float tail that would make "0.55" read as 0.5500000001.
  const round4 = (v) => Math.round(v * 1e4) / 1e4;
  const trimZeros = (v) => String(round4(v));

  /* Nobody knows their spine in inches; everybody knows their page count. The
     spine IS pages × the paper's per-page thickness, so that is what the
     dialog asks for and this is the table it multiplies by — KDP's published
     figures, with a custom rung for the printers who publish their own
     (IngramSpark's factors differ by stock). */
  const PAPERS = [
    ['white', 'White — 0.002252 in/page', 0.002252],
    ['cream', 'Cream — 0.0025 in/page', 0.0025],
    ['color', 'Premium colour — 0.002347 in/page', 0.002347],
    ['custom', 'Custom factor…', null],
  ];
  const PAPER_BY_ID = new Map(PAPERS.map(([id, , factor]) => [id, factor]));

  /* The page count is a fact about THIS book, so it is remembered per job
     rather than per browser: reopening the dialog when the count firms up
     should be a two-keystroke fix, not a re-derivation. It lives in the
     browser and not the document because it is not part of the cover — the
     document carries the inches it produced, which is what the printer needs. */
  const SPINE_STORAGE = `sc-canvas-spine:${jobId}`;
  function loadSpineChoice() {
    try {
      const raw = JSON.parse(localStorage.getItem(SPINE_STORAGE) || 'null');
      return (raw && Number(raw.pages) > 0) ? raw : null;
    } catch { return null; }
  }
  function saveSpineChoice(choice) {
    try { localStorage.setItem(SPINE_STORAGE, JSON.stringify(choice)); } catch { /* private mode */ }
  }

  /* One dialog, two jobs, because it is the same question at two moments.

     Before the conversion it asks the whole book: trim, page count, paper,
     bleed, resolution. Afterwards the trim is shown and not editable, because
     the server refuses to change it (ops.py set_wrap: "the trim size is the
     book's size, so changing it is a different book") and a field that could
     be typed into and then refused is a worse explanation than a sentence.

     The spine is DERIVED and never typed, because a typed spine is a number a
     person had to work out somewhere else and could get wrong silently. Pages
     × paper factor is the arithmetic their printer does, done here, shown as
     it happens — with an override for the one honest case the table cannot
     cover: a printer who hands you the exact inches. */
  function openWrapDialog() {
    const wrap = store.doc.wrap;
    const num = (value, step, min) => el('input', {
      type: 'number', value: String(value), step, min,
    });
    const field = (label, input, hint) => el('div', { class: 'field' }, [
      el('label', { text: label }), input,
      hint ? el('div', { class: 'dhint', text: hint }) : null,
    ]);

    const stored = loadSpineChoice();
    const trimW = num(wrap ? wrap.trim_w_in : 6, 0.125, 1);
    const trimH = num(wrap ? wrap.trim_h_in : 9, 0.125, 1);
    /* 220 cream pages is 0.55in — the spine this dialog used to default to,
       said the way the book says it. A wrapped document with no remembered
       count leaves this blank and pre-fills the override with the spine the
       sheet actually has, so the dialog opens telling the truth rather than
       back-solving a page count nobody stated. */
    const pages = num(stored ? stored.pages : (wrap ? '' : 220), 1, 1);
    const paper = el('select', {}, PAPERS.map(([id, label]) => el('option',
      Object.assign({ value: id, text: label },
        id === ((stored && stored.paper) || 'cream') ? { selected: true } : {}))));
    const factor = num(stored && stored.factor ? stored.factor : 0.0025, 0.00001, 0.0001);
    const factorField = field('Custom factor (in/page)', factor,
      'one page’s thickness — check your printer’s calculator (IngramSpark and '
      + 'others publish their own figures)');
    const override = el('input', {
      type: 'number', step: 0.001, min: 0.01, placeholder: 'from the page count',
      value: (wrap && !stored) ? trimZeros(wrap.spine_in) : '',
    });
    /* True while the override holds a value this dialog put there rather than
       a person: the moment they state a page count, an untouched pre-fill gets
       out of the way instead of quietly overruling them. */
    let overrideAuto = !!(wrap && !stored);
    const calc = el('div', { class: 'dcalc' });
    const bleed = num(wrap ? wrap.bleed_in : 0.125, 0.005, 0.01);
    const dpi = num(wrap ? wrap.dpi : 300, 1, 72);
    const err = el('div', { class: 'derr' });

    /* The spine this dialog would send, and where it came from. Null means it
       cannot say yet — no page count and no override. */
    function spineNow() {
      const typed = override.value.trim();
      if (typed !== '') {
        const inches = Number(typed);
        return inches > 0 ? { inches: round4(inches), override: true } : null;
      }
      const each = paper.value === 'custom' ? Number(factor.value) : PAPER_BY_ID.get(paper.value);
      const count = Math.round(Number(pages.value));
      if (!(count > 0) || !(each > 0)) return null;
      return { inches: round4(count * each), pages: count, factor: each };
    }

    function recalc() {
      factorField.hidden = paper.value !== 'custom';
      const now = spineNow();
      if (!now) {
        calc.textContent = 'Give a page count (or paste an override) and the spine works itself out.';
        calc.classList.add('is-idle');
        return;
      }
      calc.classList.remove('is-idle');
      calc.textContent = now.override
        ? `Override: ${trimZeros(now.inches)} in spine — the page count is ignored while this is filled.`
        : `${now.pages} pages × ${now.factor} = ${trimZeros(now.inches)} in spine`;
    }
    // Stating a page count is stating the spine, so an auto-filled override
    // steps aside; one the person typed is theirs and stays.
    [pages, paper, factor].forEach((control) => control.addEventListener('input', () => {
      if (overrideAuto && override.value !== '') { override.value = ''; overrideAuto = false; }
      recalc();
    }));
    paper.addEventListener('change', recalc);
    override.addEventListener('input', () => { overrideAuto = false; recalc(); });

    const close = () => { modal.remove(); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); close(); }
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); go(); }
    };

    const okLabel = wrap ? 'Re-measure' : 'Make the wrap';
    const modal = el('div', { class: 'modal' }, el('div', { class: 'sheetbox' }, [
      el('h2', { text: wrap ? 'Spine & bleed' : 'Print wrap' }),
      el('p', {
        class: 'lede',
        text: wrap
          ? 'The sheet, re-measured. Every layer keeps its place on its own '
            + 'panel — spine type stays centred on the spine, the front cover '
            + 'stays put on the front.'
          : 'Lay this front cover out as a full paperback wrap: back panel, '
            + 'spine, front panel and bleed on one sheet. The cover you have '
            + 'moves onto the front panel; the other two are seeded empty.',
      }),
      wrap
        ? el('div', { class: 'dstat', text:
            `Trim ${wrap.trim_w_in} × ${wrap.trim_h_in} in — the book's size, `
            + `and not something a re-measure can change.` })
        : el('div', { class: 'grid2' }, [
            field('Trim width (in)', trimW), field('Trim height (in)', trimH),
          ]),
      el('div', { class: 'grid2' }, [
        field('Pages', pages), field('Paper', paper),
      ]),
      factorField,
      calc,
      field('Override spine (in)', override,
        'advanced — only if your printer handed you the exact number'),
      el('div', { class: 'grid2' }, [
        field('Bleed (in)', bleed), field('Resolution (dpi)', dpi),
      ]),
      err,
      el('div', { class: 'row' }, [
        el('button', { class: 'btn', type: 'button', text: 'Cancel', onclick: close }),
        el('button', { class: 'btn primary', type: 'button', text: okLabel, onclick: () => go() }),
      ]),
    ]));
    document.body.appendChild(modal);
    document.addEventListener('keydown', onKey);
    recalc();
    (wrap ? pages : trimW).focus();
    (wrap ? pages : trimW).select();

    /* Checked here as well as at the door, only so the complaint can name the
       field while the field is still on screen. The server's own Wrap model is
       still the authority — this never accepts anything it would refuse. */
    function read() {
      const spine = spineNow();
      if (!spine) {
        err.textContent = 'The spine needs a page count and a paper — or an override in inches. '
          + 'A blank is a typo, not a thin book.';
        return null;
      }
      const values = {
        trim_w_in: Number(trimW.value), trim_h_in: Number(trimH.value),
        spine_in: spine.inches, bleed_in: Number(bleed.value),
        dpi: Math.round(Number(dpi.value)),
      };
      for (const [key, label] of [['trim_w_in', 'Trim width'], ['trim_h_in', 'Trim height'],
        ['bleed_in', 'Bleed']]) {
        if (!(values[key] > 0)) {
          err.textContent = `${label} has to be a real measurement — a zero or a blank is a typo, not a thin book.`;
          return null;
        }
      }
      if (!(values.dpi >= 72 && values.dpi <= 600)) {
        err.textContent = 'Resolution has to be between 72 and 600 dpi — 300 is what these printers ask for.';
        return null;
      }
      /* The EFFECTIVE factor is remembered, not whatever is sitting in the
         hidden custom box: reopening on "Custom factor…" should start from
         the thickness this spine was actually worked out with. */
      if (!spine.override) {
        saveSpineChoice({ pages: spine.pages, paper: paper.value, factor: spine.factor });
      }
      return values;
    }

    async function go() {
      const values = read();
      if (!values) return;
      close();
      if (!wrap) { await convertToWrap(values); return; }
      /* Only what actually moved: an op that widened the spine must not also
         re-state a dpi nobody touched, or its undo would claim to. */
      const op = { op: 'set_wrap' };
      ['spine_in', 'bleed_in', 'dpi'].forEach((k) => {
        if (values[k] !== wrap[k]) op[k] = values[k];
      });
      if (Object.keys(op).length === 1) { toast('Nothing changed — the sheet is as it was.'); return; }
      store.apply(op);
      const sheet = wrapPanels(store.doc.wrap).sheet;
      toast(`Sheet re-measured: ${round2(sheet.w_in)} × ${round2(sheet.h_in)} in `
        + `@ ${sheet.dpi}dpi (${store.doc.wrap.spine_in}in spine).`, 'ok');
    }
  }

  /* The conversion. One-way on the server by design, so this is the one place
     the editor throws its history away rather than pretending otherwise —
     see store.reset. */
  async function convertToWrap(values) {
    const busy = el('div', { class: 'busy', text: 'Laying the sheet out…' });
    document.body.appendChild(busy);
    try {
      /* Flush FIRST. The server converts the document it has on disk, so any
         edit still sitting in the queue would either be lost or — worse —
         land after the conversion, writing front-cover fractions onto a sheet. */
      await store.flushNow();
      const res = await postJSON(`/api/canvas/${encodeURIComponent(jobId)}/wrap`, values);
      if (!res || !res.doc) { toast('The server answered without a document.', 'err'); return; }
      // The one moment both derivations of the same geometry are in hand.
      checkPanelsAgree(res.doc.wrap, res.panels);
      store.reset(res.doc);
      engine.zoomToFit();
      const sheet = (res.panels && res.panels.sheet) || wrapPanels(res.doc.wrap).sheet;
      toast(`Wrapped: ${round2(sheet.w_in)} × ${round2(sheet.h_in)} in @ ${sheet.dpi}dpi. `
        + `Back, spine and front are one sheet now — earlier undo steps were `
        + `left behind with the front-cover canvas.`, 'ok', 11000);
    } catch (e) {
      toast(e.message, 'err', 9000);
    } finally {
      busy.remove();
    }
  }

  /* --------------------------------------------------------- AI plate ops */
  /* What one call cost, for the toast. doc.cost_usd carries the running total
     and is already in the shelf; this is the price of the click just made. */
  const money = (c) => (typeof c === 'number' ? ` — $${c.toFixed(2)}` : '');

  /* ------------------------------------------------------- plate progress */
  /* One chip per plate call in flight. Not a modal: a render is tens of
     seconds and there is no reason a person cannot keep moving type, panning
     or picking layers while the picture paints. The chip is the honest
     replacement — it says what is rendering, on which layer, and how far
     along the vendor's own partial frames have got. */
  function plateChip(text, layerName) {
    const label = el('span', { class: 'pc-text', text });
    const bar = el('span', { class: 'pc-bar' });
    const chip = el('div', { class: 'plate-chip' }, [
      el('span', { class: 'pc-name', text: layerName || 'plate' }), label, bar,
    ]);
    chips.appendChild(chip);
    return {
      tick(index) {
        label.textContent = `${text} (${index} of 3)`;
        bar.style.setProperty('--pc-fill', `${Math.min(100, index * 33)}%`);
      },
      remove() { chip.remove(); },
    };
  }

  /* The plate the server just made, folded into the document WE have.

     Not store.replaceDoc(res.doc): the server's copy was loaded before this
     call started, so anything edited while the plate rendered is missing
     from it — and now that the canvas stays live during a render, that is a
     real edit somebody just made, not a theoretical one. The same doctrine
     the ops flush already follows ("take the money back and keep our copy").
     What a plate verb actually changes is this layer's plate and the two
     ledgers, so that is exactly what is taken. It still goes through
     replaceDoc, so ⌘Z steps back across a roll as one entry. */
  function mergePlate(res, layerId) {
    if (!res || !res.doc) return;
    const theirs = (res.doc.layers || []).find((l) => l.id === layerId);
    const next = clone(store.doc);
    const mine = (next.layers || []).find((l) => l.id === layerId);
    if (theirs && mine) {
      mine.source = theirs.source;
      mine.plate_history = theirs.plate_history;
      mine.prompt = theirs.prompt;
      mine.effects = theirs.effects;          // rebalance writes one of these
    }
    next.cost_usd = res.doc.cost_usd;
    next.history = res.doc.history;
    store.replaceDoc(next);
  }

  /* Every money-spending plate verb, which is all of them. Streams: the
     vendor's partial frames land on the canvas in place of the plate they
     are replacing, so the picture resolves in front of you instead of
     appearing at the end of a blank wait. Throws on failure so each caller
     words its own error. */
  async function plateCall(path, body, busyText, { stream = true } = {}) {
    const layerId = body.layer_id;
    if (rolling.has(layerId)) {
      throw new ApiError(409, 'That layer is already rendering — one at a time.');
    }
    const layer = layerId ? store.layer(layerId) : null;
    const anchor = layer ? layer.source : null;
    const chip = plateChip(busyText, layer && layer.name);
    if (layerId) rolling.add(layerId);
    refresh();          // the plate's own buttons go out while it renders
    try {
      const url = `/api/canvas/${encodeURIComponent(jobId)}/${path}`;
      // `stream: false` is for the verb that has nothing to stream:
      // rebalance is Pillow arithmetic on a plate already on disk, back
      // before a progress bar could draw itself.
      const res = stream
        ? await postStream(url, body, (frame) => {
          chip.tick(frame.index);
          showPreview(anchor, layerId, frame);
        })
        : await postJSON(url, body);
      mergePlate(res, layerId);
      return res;
    } finally {
      if (anchor) { previews.delete(anchor); }
      if (layerId) rolling.delete(layerId);
      chip.remove();
      refresh();
    }
  }

  /* A partial frame, decoded and dropped into the preview channel. Best
     effort throughout: a frame that will not decode is simply not shown,
     and the render it belongs to is unaffected. */
  function showPreview(anchor, layerId, frame) {
    if (!anchor || !frame || !frame.image_b64) return;
    const img = new Image();
    img.onload = () => {
      if (!rolling.has(layerId)) return;    // that call already finished
      previews.set(anchor, img);
      engine.render();
    };
    img.src = `data:${frame.mime || 'image/png'};base64,${frame.image_b64}`;
  }

  async function reroll(layerId, prompt, quality) {
    const body = { layer_id: layerId };
    if (prompt) body.prompt = prompt;
    if (quality) body.quality = quality;
    try {
      const res = await plateCall('reroll', body,
        prompt ? 'Rolling the tweaked prompt…' : 'Re-rolling the plate…');
      toast(`New plate in${money(res.cost_usd)}.`, 'ok');
    } catch (e) {
      toast(e.message, 'err');
    }
  }

  /* The other end of the draft ladder. No confirmation dialogue on purpose:
     the price is on the button that set the rung, the toast says what it
     actually cost, and a modal between a person and the plate they have
     already decided on buys nothing. */
  async function finalize(layerId) {
    try {
      const res = await plateCall('finalize', { layer_id: layerId },
        'Re-rendering the plate at full quality…');
      toast(`Plate finalized${money(res.cost_usd)}.`, 'ok');
    } catch (e) {
      toast(e.message, 'err');
    }
  }

  function askForRepair(rect, layerId) {
    const ask = el('div', { class: 'marquee-ask' });
    ask.style.left = `${Math.min(rect.x, stagewrap.clientWidth - 272)}px`;
    ask.style.top = `${Math.min(rect.y + rect.height + 8, stagewrap.clientHeight - 108)}px`;
    const field = el('input', { type: 'text', placeholder: 'fix her hand / remove the lamp' });
    const close = () => { ask.remove(); engine.endMarquee(); shelf.update(); };
    ask.append(
      el('div', { class: 'lbl', text: 'Repair this region' }),
      field,
      el('div', { class: 'row' }, [
        el('button', { class: 'btn small primary', type: 'button', text: 'Repair', onclick: () => go() }),
        el('button', { class: 'btn small', type: 'button', text: 'Cancel', onclick: close }),
      ]),
    );
    stagewrap.appendChild(ask);
    field.focus();
    field.addEventListener('keydown', (e) => {
      e.stopPropagation();
      if (e.key === 'Enter') go();
      if (e.key === 'Escape') close();
    });

    async function go() {
      const instruction = field.value.trim();
      if (!instruction) { field.focus(); return; }
      const mask = engine.maskFor(layerId, rect);
      close();
      if (!mask) { toast('That plate has not finished loading yet.', 'err'); return; }
      try {
        const res = await plateCall('inpaint', {
          layer_id: layerId, instruction, mask_b64: mask, quality: currentQuality(),
        }, 'Repairing the region…');
        toast(`Region repaired${money(res.cost_usd)}.`, 'ok');
      } catch (e) {
        if (e.status === 501) toast('Inpaint lands with the next server build.');
        else toast(e.message, 'err');
      }
    }
  }

  /* -------------------------------------------------------------- keyboard */
  const typing = () => {
    const t = document.activeElement;
    return t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
  };
  window.addEventListener('keydown', (e) => {
    const meta = e.metaKey || e.ctrlKey;
    if (meta && e.key.toLowerCase() === 'z') {
      if (typing()) return;                 // let a field keep its own undo
      e.preventDefault();
      if (e.shiftKey) store.redo(); else store.undo();
      return;
    }
    if (typing()) return;
    if (e.code === 'Space') { e.preventDefault(); engine.setPanArmed(true); return; }
    if (e.key === 'Escape') {
      if (engine.marqueeActive) { engine.endMarquee(); shelf.update(); } else engine.select(null);
      return;
    }
    if (e.key === 'f' || e.key === 'F') { engine.zoomToFit(); return; }
    if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); ctx.shelf.deleteLayer(); return; }
    const step = e.shiftKey ? NUDGE_BIG : NUDGE;
    const move = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
    if (move && engine.selectedId) {
      e.preventDefault();
      const l = ctx.selectedLayer();
      const corners = l && l.frame.corners;
      /* A corner pin is stated in absolute canvas fractions, so a relative
         nudge would slide the box out from under pixels that stayed put.
         While pinned the same keystroke says the whole move absolutely
         instead — one op, still one undo step. */
      if (corners && corners.length === 4) {
        store.apply({
          op: 'set_frame', layer_id: l.id,
          x: l.frame.x + move[0], y: l.frame.y + move[1],
          corners: corners.map(([u, v]) => [u + move[0], v + move[1]]),
        });
        return;
      }
      store.apply({ op: 'nudge', layer_id: engine.selectedId, dx: move[0], dy: move[1] });
    }
  });
  window.addEventListener('keyup', (e) => { if (e.code === 'Space') engine.setPanArmed(false); });
  window.addEventListener('blur', () => engine.setPanArmed(false));
  window.addEventListener('beforeunload', (e) => {
    if (store.pending) { e.preventDefault(); e.returnValue = ''; }
  });

  refresh();
  return { store, engine, ctx, assistant };
}

/* ------------------------------------------------------------------ main */
(async function main() {
  // The Mac shell hands its own key over in the URL FRAGMENT — the one part
  // of a URL that never reaches the server or its logs — so a person at
  // their own machine is never asked to copy a password out of a terminal.
  // Consumed once and scrubbed from the address bar.
  const hash = new URLSearchParams(location.hash.replace(/^#/, ''));
  if (hash.get('key')) {
    setKey(hash.get('key'));
    history.replaceState(null, '', location.pathname + location.search);
  }
  const jobId = jobFromURL();
  if (!jobId) { showPicker(); return; }
  // Every request this page makes carries the concept, or the server cannot
  // tell which of the job's covers is being edited. The URL's answer opens
  // the document; the DOCUMENT's own answer is what the rest of the session
  // uses, because a URL with no concept in it still lands on a real one.
  setConcept(conceptFromURL());
  try {
    const doc = await openDoc(jobId);
    setConcept(doc.concept ?? 0);
    await loadFamilies(doc);
    window.coverCanvas = buildEditor(jobId, doc);
  } catch (err) {
    showPicker(err.message || 'That cover would not open.');
  }
}());
