/* The three panels around the canvas: the layer list (left), the contextual
   properties panel (right), and the button shelf (top) — plus the small `el`
   DOM helper the rest of the front-end builds markup with.

   Everything here is a thin skin over ops.js. A control's job is to produce an
   op, never to touch the doc: the one exception is the live *preview* while a
   slider is being dragged, which mutates the doc directly and is rolled back
   before the real op is applied, so one drag stays one undo step (same
   discipline the canvas transform uses). */

import { clone, NUDGE } from './ops.js';
import { panels as wrapPanels } from './wrap.js';

export function el(tag, attrs = {}, kids = []) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (k === 'value') n.value = v;
    else n.setAttribute(k, v === true ? '' : v);
  }
  (Array.isArray(kids) ? kids : [kids]).forEach((c) => {
    if (c === null || c === undefined || c === false) return;
    n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  });
  return n;
}

const ICON_EYE = '<svg viewBox="0 0 24 24"><path d="M2 12s3.8-6 10-6 10 6 10 6-3.8 6-10 6-10-6-10-6z"/><circle cx="12" cy="12" r="2.6"/></svg>';
const ICON_EYE_OFF = '<svg viewBox="0 0 24 24"><path d="M3 3l18 18"/><path d="M10.6 6.2A9.7 9.7 0 0112 6c6.2 0 10 6 10 6a17 17 0 01-3.3 3.7M6.4 6.7A17 17 0 002 12s3.8 6 10 6a9.9 9.9 0 003.6-.65"/></svg>';
const ICON_LOCK = '<svg viewBox="0 0 24 24"><rect x="4.5" y="10.5" width="15" height="9.5"/><path d="M8 10.5V7.5a4 4 0 018 0v3"/></svg>';
const ICON_UNLOCK = '<svg viewBox="0 0 24 24"><rect x="4.5" y="10.5" width="15" height="9.5"/><path d="M8 10.5V7.5a4 4 0 017.5-1.9"/></svg>';

const pct = (v) => Math.round(v * 1000) / 10;
const fromPct = (v) => Number(v) / 100;

export function newLayerId() {
  // The server owns ids in the end; this only has to be unique in one session.
  const rnd = (crypto && crypto.randomUUID) ? crypto.randomUUID().slice(0, 8)
    : Math.random().toString(16).slice(2, 10);
  return `l_${rnd}`;
}

/* Which rung of the quality ladder a re-roll buys. Remembered per browser
   rather than per document: it is a habit ("I am still composing"), not a
   property of the cover, and it should survive opening the next one.

   Draft is the default deliberately. The ladder's whole shape is roll cheap
   while the composition moves, then spend once on the plate you keep — and
   the button that spends is right there, labelled, one click away. Defaulting
   the other way would make every exploratory click cost full price silently,
   which is the one direction a money mistake is not recoverable in. */
const QUALITY_STORAGE = 'sc-canvas-quality';
let quality = 'draft';
try {
  const saved = localStorage.getItem(QUALITY_STORAGE);
  if (saved === 'draft' || saved === 'final') quality = saved;
} catch { /* private mode */ }

/* The rung a repair should be asked for too — a fix on a draft plate is worth
   drafting, and a fix on a finalized one must not come back a rung softer.
   The control lives in the art panel, so the reading of it does too. */
export const currentQuality = () => quality;

/* Whether the print wrap's guides are drawn. Per browser like the quality
   rung and for the same reason: "I am looking at the composition, not the
   folds" is a habit, not a property of one cover. Default ON — a person who
   has just made a wrap needs to see where the spine is, and the toggle is
   right there for when they don't. */
const GUIDES_STORAGE = 'sc-canvas-guides';
let guidesOn = true;
try { guidesOn = localStorage.getItem(GUIDES_STORAGE) !== 'off'; } catch { /* private mode */ }
export const guidesEnabled = () => guidesOn;

/* The four corners of a layer's own box, as canvas fractions — what a fresh
   pin starts from, so switching it on changes nothing until a corner moves.
   Rotation is applied in PIXELS and converted back: a degree of rotation
   moves x and y by different fractions on a canvas that is not square. */
function boxCorners(l, cw, ch) {
  const f = l.frame;
  const hw = (f.w / 2) * cw; const hh = (f.h / 2) * ch;
  const th = (f.rotation || 0) * Math.PI / 180;
  const cos = Math.cos(th); const sin = Math.sin(th);
  const clamp = (v) => Math.max(-2, Math.min(2, v));
  return [[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]].map(([dx, dy]) => [
    clamp(f.x + (dx * cos - dy * sin) / cw),
    clamp(f.y + (dx * sin + dy * cos) / ch),
  ]);
}

/* --------------------------------------------------------------- shelf --- */
export function buildShelf(ctx) {
  const bar = el('div', { class: 'shelf' });
  const btn = (label, opts = {}) => el('button', Object.assign({
    class: 'btn' + (opts.cls ? ' ' + opts.cls : ''), type: 'button', text: label,
    title: opts.title || label, onclick: opts.on,
  }, opts.disabled ? { disabled: true } : {}));

  const undoBtn = btn('Undo', { on: () => ctx.undo(), title: 'Undo (⌘Z)' });
  const redoBtn = btn('Redo', { on: () => ctx.redo(), title: 'Redo (⇧⌘Z)' });
  const scrimBtn = btn('Scrim behind type', { on: () => ctx.shelf.scrim() });
  const shadowBtn = btn('Shadow stack', { on: () => ctx.shelf.shadowStack() });
  const groundBtn = btn('Ground the figure', { on: () => ctx.shelf.ground() });
  const balanceBtn = btn('Rebalance values', { on: () => ctx.shelf.rebalance() });
  const repairBtn = btn('Repair region', { on: () => ctx.shelf.repair() });
  const delBtn = btn('Delete', { cls: 'danger', on: () => ctx.shelf.deleteLayer() });
  const cost = el('div', { class: 'cost', title: 'Everything this cover has cost so far' });
  const save = el('div', { class: 'save' });

  /* One shelf slot, two jobs — because they are the same question asked at
     two moments. Before the conversion it is "how big is this book?"; after
     it, the only part of that answer still open is the spine (the trim is the
     book's size, and the server refuses to change it: ops.py set_wrap). */
  const wrapBtn = btn('Print wrap…', { on: () => ctx.shelf.wrapDialog() });

  const guidesBtn = el('button', {
    class: 'btn small gtog', type: 'button',
    onclick: () => {
      guidesOn = !guidesOn;
      try { localStorage.setItem(GUIDES_STORAGE, guidesOn ? 'on' : 'off'); } catch { /* private mode */ }
      ctx.engine.setGuidesVisible(guidesOn);
      update();
    },
  });

  /* Export is one button on a front cover and a two-item menu on a wrap: the
     PDF is only meaningful once the document knows its own physical size, so
     offering it before then would be offering a page of assumed inches. */
  const exportBtn = btn('Export PNG', { cls: 'primary', on: () => ctx.shelf.exportPNG() });
  const exportMenu = el('div', { class: 'menu', hidden: true }, [
    el('button', {
      class: 'btn', type: 'button', text: 'Export PNG',
      title: 'The sheet as pixels, saved with the job',
      onclick: () => { closeMenu(); ctx.shelf.exportPNG(); },
    }),
    el('button', {
      class: 'btn', type: 'button', text: 'Print PDF',
      title: 'The sheet as one page at its exact physical size — the printer’s file',
      onclick: () => { closeMenu(); ctx.shelf.exportPDF(); },
    }),
  ]);
  const exportPick = el('div', { class: 'menuwrap', hidden: true }, [
    el('button', {
      class: 'btn primary', type: 'button', text: 'Export ▾',
      onclick: (e) => { e.stopPropagation(); exportMenu.hidden = !exportMenu.hidden; },
    }),
    exportMenu,
  ]);
  function closeMenu() { exportMenu.hidden = true; }
  // Anywhere else on the page closes it, including the button that opened it
  // (that click stops propagating, so it toggles instead of double-firing).
  document.addEventListener('click', closeMenu);

  bar.append(
    // The brand is the way home: this window has no back button, so moving
    // between the picker, the studio and the editor is done by links or not
    // at all. The Studio link carries the job, landing on this very cover's
    // contact sheet (sc-cover.html reads ?job=).
    el('a', { class: 'brand', href: '/canvas', html: 'Cover Canvas',
              title: 'All covers' }),
    el('a', {
      class: 'navlink', title: 'This cover in Cover Studio — concepts, re-rolls, the brief',
      href: `/sc-cover.html?job=${encodeURIComponent(ctx.store.doc.job_id || '')}`,
      text: 'Studio',
    }),
    el('div', { class: 'sep' }),
    undoBtn, redoBtn,
    el('div', { class: 'sep' }),
    scrimBtn, shadowBtn, groundBtn, balanceBtn,
    el('div', { class: 'sep' }),
    btn('+ Text', { on: () => ctx.shelf.addText() }),
    btn('+ Shape', { on: () => ctx.shelf.addShape() }),
    btn('+ Frame', { on: () => ctx.shelf.addFrame() }),
    repairBtn, delBtn,
    el('div', { class: 'sep' }),
    wrapBtn, guidesBtn,
    el('div', { class: 'spacer' }),
    el('div', { class: 'jobline', text: '' }),
    save, cost,
    exportBtn, exportPick,
  );
  const jobline = bar.querySelector('.jobline');

  function update() {
    const l = ctx.selectedLayer();
    const art = !!(l && l.kind === 'art');
    // A pinned plate has no un-warped image node to map a drawn rectangle
    // back through, so the region it would send is unknowable (engine.maskFor).
    const pinned = art && ctx.engine.isPinned(l.id);
    undoBtn.disabled = !ctx.store.canUndo;
    redoBtn.disabled = !ctx.store.canRedo;
    scrimBtn.disabled = !(l && l.kind === 'text');
    shadowBtn.disabled = !l;
    delBtn.disabled = !l;
    groundBtn.disabled = !art;
    groundBtn.title = art
      ? 'Generate a floor under the figure and re-seat it (§15.23)'
      : 'Select an art layer first';
    balanceBtn.disabled = !art;
    balanceBtn.title = art
      ? 'Measure this plate and nudge its levels — costs nothing'
      : 'Select an art layer first';
    repairBtn.disabled = !art || pinned;
    repairBtn.title = pinned
      ? 'Turn the perspective pin off to draw a repair region'
      : 'Repair region';
    repairBtn.classList.toggle('on', ctx.engine.marqueeActive);

    const wrap = ctx.store.doc.wrap;
    wrapBtn.textContent = wrap ? 'Spine & bleed…' : 'Print wrap…';
    wrapBtn.title = wrap
      ? `Re-measure the sheet — ${wrap.trim_w_in}×${wrap.trim_h_in}in, `
        + `${wrap.spine_in}in spine, ${wrap.bleed_in}in bleed, ${wrap.dpi}dpi`
      : 'Lay this front cover out as a full paperback wrap: back, spine and front';
    guidesBtn.hidden = !wrap;
    guidesBtn.innerHTML = (guidesOn ? ICON_EYE : ICON_EYE_OFF) + '<span>Guides</span>';
    guidesBtn.classList.toggle('on', guidesOn);
    guidesBtn.title = guidesOn ? 'Hide the fold, trim and safe guides' : 'Show the print guides';
    exportBtn.hidden = !!wrap;
    exportPick.hidden = !wrap;
    if (!wrap) closeMenu();

    cost.textContent = `$${(ctx.store.doc.cost_usd || 0).toFixed(2)} this cover`;
    jobline.textContent = ctx.store.doc.job_id || '';
    const st = ctx.store.status();
    save.className = 'save' + (st === 'stuck' ? ' is-stuck' : (st === 'saved' ? '' : ' is-dirty'));
    save.textContent = { saved: 'saved', saving: 'saving…', dirty: 'unsaved…', stuck: 'unsaved edits' }[st];
  }
  return { root: bar, update };
}

/* ---------------------------------------------------------- layer list --- */
export function buildLayerRail(ctx) {
  const head = el('div', { class: 'rail-head' }, [el('span', { text: 'Layers' }), el('span', { class: 'count' })]);
  const body = el('div', { class: 'rail-body' });
  const root = el('div', { class: 'rail left' }, [head, body]);
  let dragId = null;

  function update() {
    const doc = ctx.store.doc;
    const layers = (doc.layers || []).slice().reverse();   // top of stack first
    head.querySelector('.count').textContent = String(doc.layers?.length || 0);
    body.textContent = '';
    if (!layers.length) { body.appendChild(el('div', { class: 'rail-empty', text: 'No layers yet.' })); return; }

    layers.forEach((l) => {
      const row = el('div', {
        class: 'lrow' + (l.id === ctx.selectedId() ? ' sel' : '') + (l.visible === false ? ' hid' : ''),
        'data-kind': l.kind, 'data-id': l.id, draggable: true, title: l.name || l.kind,
        onclick: () => ctx.select(l.id),
      }, [
        el('span', { class: 'kind', text: (l.kind || '?').slice(0, 2) }),
        el('span', { class: 'lname', text: l.name || l.kind }),
        el('button', {
          class: 'tog' + (l.visible === false ? '' : ' on'), type: 'button',
          title: l.visible === false ? 'Show layer' : 'Hide layer',
          html: l.visible === false ? ICON_EYE_OFF : ICON_EYE,
          onclick: (e) => {
            e.stopPropagation();
            ctx.store.apply({ op: 'set_layer', layer_id: l.id, visible: !(l.visible !== false) });
          },
        }),
        el('button', {
          class: 'tog' + (l.locked ? ' on' : ''), type: 'button',
          title: l.locked ? 'Unlock layer' : 'Lock layer',
          html: l.locked ? ICON_LOCK : ICON_UNLOCK,
          onclick: (e) => {
            e.stopPropagation();
            ctx.store.apply({ op: 'set_layer', layer_id: l.id, locked: !l.locked });
          },
        }),
      ]);

      row.addEventListener('dragstart', (e) => {
        dragId = l.id; row.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', l.id);
      });
      row.addEventListener('dragend', () => { dragId = null; update(); });
      row.addEventListener('dragover', (e) => {
        if (!dragId || dragId === l.id) return;
        e.preventDefault();
        const above = (e.offsetY < row.offsetHeight / 2);
        row.classList.toggle('drop-above', above);
        row.classList.toggle('drop-below', !above);
      });
      row.addEventListener('dragleave', () => row.classList.remove('drop-above', 'drop-below'));
      row.addEventListener('drop', (e) => {
        e.preventDefault();
        row.classList.remove('drop-above', 'drop-below');
        if (!dragId || dragId === l.id) return;
        // The rail reads top-first; the doc stores bottom-first. Dropping
        // ABOVE a row means a HIGHER index in the doc's array.
        const above = (e.offsetY < row.offsetHeight / 2);
        const target = ctx.store.indexOf(l.id);
        const from = ctx.store.indexOf(dragId);
        let index = above ? target + 1 : target;
        if (from < index) index -= 1;
        ctx.store.apply({ op: 'reorder_layer', layer_id: dragId, index });
        dragId = null;
      });
      body.appendChild(row);
    });
  }
  return { root, update };
}

/* --------------------------------------------------------- properties --- */
export function buildPropsRail(ctx) {
  const head = el('div', { class: 'rail-head' }, [el('span', { text: 'Properties' })]);
  const body = el('div', { class: 'rail-body pad' });
  const root = el('div', { class: 'props' }, [head, body]);

  /* One drag = one undo step. `input` previews on the live doc; `change`
     rewinds that preview and lets the real op carry the whole move. */
  function scrub(input, layerId, { preview, commit }) {
    let before = null;
    const arm = () => {
      if (before) return;
      before = clone(ctx.store.layer(layerId));
      // A preview repaints on every input event; tell the engine to stop
      // re-caching pixel filters until the hand comes off the control.
      ctx.interacting(true);
    };
    input.addEventListener('pointerdown', arm);
    input.addEventListener('keydown', arm);
    input.addEventListener('focus', arm);
    input.addEventListener('input', () => {
      arm();
      preview(input.value);
      ctx.repaint();
    });
    input.addEventListener('change', () => {
      const snap = before; before = null;
      const live = ctx.store.layer(layerId);
      if (snap && live) Object.assign(live, snap);
      ctx.interacting(false);
      commit(input.value);
    });
    // A control abandoned without ever firing `change` would otherwise leave
    // the engine believing a hand is still on it, and its filters switched off.
    input.addEventListener('blur', () => ctx.interacting(false));
    return input;
  }

  const group = (title, rows) => el('div', { class: 'prop-group' },
    [el('div', { class: 'prop-title', text: title }), ...rows.filter(Boolean)]);
  const row = (label, field, cls = '') => el('div', { class: 'prow' + (cls ? ' ' + cls : '') },
    [el('label', { text: label }), el('div', { class: 'pfield' }, field)]);

  function numberInput(value, step, onCommit) {
    return el('input', { type: 'number', class: 'num', step, value, onchange: (e) => onCommit(e.target.value) });
  }

  /* A slider and a number box that mean the same thing; both live-preview. */
  function slider(layerId, { value, min, max, step, preview, commit, suffix }) {
    const range = el('input', { type: 'range', min, max, step, value });
    const num = el('input', { type: 'number', class: 'num', min, max, step, value });
    const sync = (v) => { range.value = v; num.value = v; };
    scrub(range, layerId, { preview: (v) => { sync(v); preview(v); }, commit });
    scrub(num, layerId, { preview: (v) => { range.value = v; preview(v); }, commit });
    return [range, num, suffix ? el('span', { class: 'unit', text: suffix }) : null].filter(Boolean);
  }

  /* Buttons inside this rail blur themselves before acting: update() refuses
     to rebuild the panel while it holds focus (so a drag is never yanked out
     from under the cursor), and a chip that kept focus would never restyle. */
  function act(fn) {
    return (e) => { e.currentTarget.blur(); fn(e); };
  }

  function chips(options, current, onPick) {
    return el('div', { class: 'pchips' }, options.map(([v, label]) => el('button', {
      class: 'btn small' + (v === current ? ' on' : ''), type: 'button', text: label,
      onclick: act(() => onPick(v)),
    })));
  }

  function select(value, options, onPick) {
    return el('select', { onchange: (e) => onPick(e.target.value) },
      options.map(([v, label]) => el('option', Object.assign({ value: v, text: label }, v === value ? { selected: true } : {}))));
  }

  /* --------------------------------------------------------------- frame */
  function frameGroup(l) {
    const setFrame = (patch) => ctx.store.apply(Object.assign({ op: 'set_frame', layer_id: l.id }, patch));
    return group('Frame', [
      el('div', { class: 'prow' }, [
        el('span', { class: 'plabel', text: 'Position' }),
        el('div', { class: 'pfield' }, [
          numberInput(pct(l.frame.x), 0.1, (v) => setFrame({ x: fromPct(v) })),
          el('span', { class: 'unit', text: '% x' }),
          numberInput(pct(l.frame.y), 0.1, (v) => setFrame({ y: fromPct(v) })),
          el('span', { class: 'unit', text: '% y' }),
        ]),
      ]),
      el('div', { class: 'prow' }, [
        el('span', { class: 'plabel', text: 'Size' }),
        el('div', { class: 'pfield' }, [
          numberInput(pct(l.frame.w), 0.1, (v) => setFrame({ w: Math.max(0.002, fromPct(v)) })),
          el('span', { class: 'unit', text: '% w' }),
          numberInput(pct(l.frame.h), 0.1, (v) => setFrame({ h: Math.max(0.002, fromPct(v)) })),
          el('span', { class: 'unit', text: '% h' }),
        ]),
      ]),
      row('Rotation', slider(l.id, {
        value: l.frame.rotation || 0, min: -180, max: 180, step: 0.5, suffix: '°',
        preview: (v) => { ctx.store.layer(l.id).frame.rotation = Number(v); },
        commit: (v) => setFrame({ rotation: Number(v) }),
      })),
      row('Flip', chips([['h', 'Horizontal'], ['v', 'Vertical']], null, (v) => setFrame(
        v === 'h' ? { flip_h: !l.frame.flip_h } : { flip_v: !l.frame.flip_v }))),
      el('div', { class: 'phint', text: 'Arrow keys nudge ' + (NUDGE * 100) + '%, ⇧ + arrows 2%.' }),
    ]);
  }

  /* The one thing a spine layer needs said. Which panel a layer is ON is
     decided by its CENTRE — the same rule the set_wrap remap uses to keep it
     there when the spine widens — and the constraint that actually bites is
     the width: type wider than the spine's safe box folds onto the covers.
     Nothing is enforced here, because a deliberate wrap-around IS a design;
     this is the number a person needs while they judge it. */
  function spineNote(l) {
    const wrap = ctx.store.doc.wrap;
    if (!wrap) return null;
    const p = wrapPanels(wrap);
    if (l.frame.x < p.spine.x0 || l.frame.x > p.spine.x1) return null;
    const safeIn = Math.max(0, wrap.spine_in - 2 * p.safe.inches);
    return el('div', { class: 'phint', text:
      `Spine layer — keep inside the spine safe box (${safeIn.toFixed(2)}in of `
      + `a ${wrap.spine_in}in spine).` });
  }

  function commonGroup(l) {
    const setLayer = (patch) => ctx.store.apply(Object.assign({ op: 'set_layer', layer_id: l.id }, patch));
    return group('Layer', [
      row('Name', el('input', {
        type: 'text', value: l.name || '', onchange: (e) => setLayer({ name: e.target.value }),
      })),
      row('Opacity', slider(l.id, {
        value: l.opacity === undefined ? 1 : l.opacity, min: 0, max: 1, step: 0.01,
        preview: (v) => { ctx.store.layer(l.id).opacity = Number(v); },
        commit: (v) => setLayer({ opacity: Number(v) }),
      })),
      el('div', { class: 'prow' }, [
        el('span', { class: 'plabel', text: 'State' }),
        el('div', { class: 'pfield' }, [
          el('label', { class: 'pnote', style: 'display:flex;gap:6px;align-items:center;margin:0' }, [
            el('input', {
              type: 'checkbox', checked: l.visible !== false,
              onchange: (e) => setLayer({ visible: e.target.checked }),
            }), 'visible']),
          el('label', { class: 'pnote', style: 'display:flex;gap:6px;align-items:center;margin:0' }, [
            el('input', {
              type: 'checkbox', checked: !!l.locked,
              onchange: (e) => setLayer({ locked: e.target.checked }),
            }), 'locked']),
        ]),
      ]),
    ]);
  }

  /* ---------------------------------------------------------------- text */
  function textGroup(l) {
    const setText = (patch) => ctx.store.apply(Object.assign({ op: 'set_text', layer_id: l.id }, patch));
    const area = el('textarea', { rows: 3, value: l.text || '' });
    scrub(area, l.id, {
      preview: (v) => { ctx.store.layer(l.id).text = v; },
      commit: (v) => setText({ text: v }),
    });
    const warp = l.warp || { kind: 'none', amount: 0 };
    return group('Type', [
      el('div', { class: 'prow stack' }, [el('label', { text: 'Text' }), area]),
      row('Family', select(l.family, ctx.families().map((f) => [f, f]), (v) => setText({ family: v }))),
      row('Style', chips([['regular', 'Regular'], ['bold', 'Bold'], ['italic', 'Italic']],
        l.style || 'regular', (v) => setText({ style: v }))),
      row('Size', slider(l.id, {
        value: pct(l.size || 0.05), min: 0.4, max: 40, step: 0.1, suffix: '% h',
        preview: (v) => { ctx.store.layer(l.id).size = fromPct(v); },
        commit: (v) => setText({ size: fromPct(v) }),
      })),
      row('Colour', scrub(el('input', { type: 'color', value: l.color || '#ffffff' }), l.id, {
        preview: (v) => { ctx.store.layer(l.id).color = v; },
        commit: (v) => setText({ color: v }),
      })),
      row('Tracking', slider(l.id, {
        value: l.tracking || 0, min: -0.15, max: 0.6, step: 0.005, suffix: 'em',
        preview: (v) => { ctx.store.layer(l.id).tracking = Number(v); },
        commit: (v) => setText({ tracking: Number(v) }),
      })),
      row('Align', chips([['left', 'Left'], ['center', 'Centre'], ['right', 'Right']],
        l.align || 'center', (v) => setText({ align: v }))),
      row('Leading', slider(l.id, {
        value: l.line_height || 1.15, min: 0.6, max: 2.4, step: 0.01,
        preview: (v) => { ctx.store.layer(l.id).line_height = Number(v); },
        commit: (v) => setText({ line_height: Number(v) }),
      })),
      row('Warp', select(warp.kind || 'none',
        [['none', 'None'], ['arc', 'Arc'], ['arch', 'Arch'], ['flag', 'Flag'], ['bulge', 'Bulge']],
        (v) => setText({ warp: { kind: v, amount: warp.amount || (v === 'none' ? 0 : 0.35) } }))),
      row('Amount', slider(l.id, {
        value: warp.amount || 0, min: -1, max: 1, step: 0.01,
        preview: (v) => { ctx.store.layer(l.id).warp = { kind: warp.kind, amount: Number(v) }; },
        commit: (v) => setText({ warp: { kind: warp.kind, amount: Number(v) } }),
      })),
    ]);
  }

  /* ----------------------------------------------------------------- art */
  function artGroup(l) {
    const prompt = el('textarea', { rows: 4, value: l.prompt || '', readonly: true });
    const tweak = el('button', {
      class: 'btn small', type: 'button', text: 'Tweak & roll',
      onclick: () => {
        if (prompt.hasAttribute('readonly')) {
          prompt.removeAttribute('readonly');
          prompt.focus();
          tweak.textContent = 'Roll this prompt';
          tweak.classList.add('on');
        } else {
          ctx.art.reroll(l.id, prompt.value, quality);
        }
      },
    });

    /* The strip is the layer's whole shelf of plates, oldest first: the
       superseded ones the server keeps, plus the one on screen — which is NOT
       in plate_history (the server only files a plate there once something
       replaces it), so it is appended here or it would have no cell to click
       back to. Deduped by source, because a swap leaves history untouched and
       the current plate is usually also on it. */
    const shelfPlates = [];
    const seen = new Set();
    [...(l.plate_history || []), { source: l.source, prompt: l.prompt }]
      .forEach((h) => {
        if (!h.source || seen.has(h.source)) return;
        seen.add(h.source);
        shelfPlates.push(h);
      });
    const strip = el('div', { class: 'plate-strip' });
    shelfPlates.forEach((h) => {
      const current = h.source === l.source;
      const cell = el('button', {
        class: 'plate' + (current ? ' is-current' : ''), type: 'button',
        title: (current ? 'current plate — ' : 'swap to this plate — ') + (h.prompt || h.source),
        // A swap is a set_art, so it undoes, persists and reads in the log
        // like any other edit — and it never consumes the strip it came from.
        onclick: act(() => {
          if (!current) ctx.store.apply({ op: 'set_art', layer_id: l.id, source: h.source });
        }),
      });
      ctx.imageURL(h.source).then((url) => cell.appendChild(el('img', { src: url, alt: '' }))).catch(() => {});
      strip.appendChild(cell);
    });

    const pinned = !!(l.frame.corners && l.frame.corners.length === 4);
    const canvas = ctx.store.doc.canvas || { w: 1, h: 1 };

    return group('Plate', [
      el('div', { class: 'prow stack' }, [el('label', { text: 'Prompt' }), prompt]),
      row('Quality', chips([['draft', 'Draft ~$0.03'], ['final', 'Final full price']],
        quality, (v) => {
          quality = v;
          try { localStorage.setItem(QUALITY_STORAGE, v); } catch { /* private mode */ }
          update();
        })),
      el('div', { class: 'pchips' }, [
        el('button', {
          class: 'btn small', type: 'button', text: 'Re-roll',
          onclick: act(() => ctx.art.reroll(l.id, null, quality)),
        }),
        tweak,
      ]),
      el('div', { class: 'pchips' }, [
        el('button', {
          class: 'btn small', type: 'button', text: 'Finalize plate',
          title: 'Re-render this exact plate at full quality, composition anchored',
          onclick: act(() => ctx.art.finalize(l.id)),
        }),
      ]),
      el('div', { class: 'phint', text: 'Roll cheap while the composition moves; finalize the plate you keep.' }),
      row('Fit', select(l.fit || 'cover',
        [['cover', 'Cover'], ['contain', 'Contain'], ['stretch', 'Stretch']],
        (v) => ctx.store.apply({ op: 'set_art', layer_id: l.id, fit: v }))),
      row('Perspective', el('div', { class: 'pchips' }, [
        el('button', {
          class: 'btn small' + (pinned ? ' on' : ''), type: 'button',
          text: pinned ? 'Pinned — click to release' : 'Perspective pin',
          title: pinned
            ? 'Drop the pin and draw the plate square in its box again'
            : 'Drag the four corners to sit the plate in perspective',
          // Starting from the box's own corners means switching the pin on
          // changes nothing on screen — the distortion is the drag, not the pin.
          onclick: act(() => ctx.store.apply({
            op: 'set_frame', layer_id: l.id,
            corners: pinned ? null : boxCorners(l, canvas.w, canvas.h),
          })),
        }),
      ])),
      pinned ? el('div', { class: 'phint', text: 'Drag the four corner handles. Shadows are not drawn while pinned.' }) : null,
      shelfPlates.length > 1 ? el('div', { class: 'prop-title', text: 'Plate history' }) : null,
      shelfPlates.length > 1 ? strip : null,
      l.transparent ? el('div', { class: 'phint', text: 'Cutout plate (alpha).' }) : null,
    ]);
  }

  /* --------------------------------------------------------------- scrim */
  const DEFAULT_GRADIENT = { angle: 90, stops: [{ at: 0, alpha: 1 }, { at: 1, alpha: 0 }] };

  function scrimGroup(l) {
    const setScrim = (patch) => ctx.store.apply(
      Object.assign({ op: 'set_scrim', layer_id: l.id }, patch));

    /* The gradient travels whole (`gradient` is one field of set_scrim), and
       it is read off the LIVE layer at commit time — which by then holds the
       values from before the drag, because scrub rewound its preview. So the
       op carries the original ramp with exactly one number changed. */
    const liveGradient = () => {
      const live = ctx.store.layer(l.id);
      if (!live.gradient || !Array.isArray(live.gradient.stops)
          || live.gradient.stops.length < 2) live.gradient = clone(DEFAULT_GRADIENT);
      return live.gradient;
    };
    const withGradient = (mutate) => {
      const next = clone(liveGradient());
      mutate(next);
      return next;
    };
    const grad = (l.gradient && l.gradient.stops && l.gradient.stops.length >= 2)
      ? l.gradient : DEFAULT_GRADIENT;
    const stops = grad.stops;
    const lastStop = (g) => g.stops[g.stops.length - 1];

    return group('Scrim', [
      row('Colour', scrub(el('input', { type: 'color', value: l.color || '#000000' }), l.id, {
        preview: (v) => { ctx.store.layer(l.id).color = v; },
        commit: (v) => setScrim({ color: v }),
      })),
      row('Angle', slider(l.id, {
        value: grad.angle || 0, min: 0, max: 360, step: 1, suffix: '°',
        preview: (v) => { liveGradient().angle = Number(v); },
        commit: (v) => setScrim({ gradient: withGradient((g) => { g.angle = Number(v); }) }),
      })),
      row('Start α', slider(l.id, {
        value: stops[0].alpha, min: 0, max: 1, step: 0.01,
        preview: (v) => { liveGradient().stops[0].alpha = Number(v); },
        commit: (v) => setScrim({ gradient: withGradient((g) => { g.stops[0].alpha = Number(v); }) }),
      })),
      row('End α', slider(l.id, {
        value: stops[stops.length - 1].alpha, min: 0, max: 1, step: 0.01,
        preview: (v) => { lastStop(liveGradient()).alpha = Number(v); },
        commit: (v) => setScrim({ gradient: withGradient((g) => { lastStop(g).alpha = Number(v); }) }),
      })),
    ]);
  }

  /* --------------------------------------------------- ornament + shape */
  function frameStyleGroup(l) {
    const setStyle = (patch) => ctx.store.apply(
      Object.assign({ op: 'set_frame_style', layer_id: l.id }, patch));
    return group('Ornament', [
      row('Preset', select(l.preset || 'single_rule', [
        ['single_rule', 'Single rule'], ['double_rule', 'Double rule'],
        ['corner_serifs', 'Corner serifs'], ['inset_panel', 'Inset panel'],
      ], (v) => setStyle({ preset: v }))),
      row('Stroke', scrub(el('input', { type: 'color', value: l.stroke || '#ffffff' }), l.id, {
        preview: (v) => { ctx.store.layer(l.id).stroke = v; },
        commit: (v) => setStyle({ stroke: v }),
      })),
      row('Weight', slider(l.id, {
        value: pct(l.stroke_w || 0.002), min: 0.02, max: 3, step: 0.01, suffix: '% w',
        preview: (v) => { ctx.store.layer(l.id).stroke_w = fromPct(v); },
        commit: (v) => setStyle({ stroke_w: fromPct(v) }),
      })),
      row('Inset', slider(l.id, {
        value: pct(l.inset || 0), min: 0, max: 30, step: 0.1, suffix: '%',
        preview: (v) => { ctx.store.layer(l.id).inset = fromPct(v); },
        commit: (v) => setStyle({ inset: fromPct(v) }),
      })),
      row('Fill', el('div', { class: 'pfield' }, [
        scrub(el('input', { type: 'color', value: l.fill || '#000000' }), l.id, {
          preview: (v) => { ctx.store.layer(l.id).fill = v; },
          commit: (v) => setStyle({ fill: v }),
        }),
        // null, not undefined: an ornament with no fill is a bezel you can see
        // through, and it is a value the op has to be able to say.
        el('button', { class: 'btn small', type: 'button', text: 'None', onclick: act(() => setStyle({ fill: null })) }),
      ])),
    ]);
  }

  function shapeGroup(l) {
    const setShape = (patch) => ctx.store.apply(
      Object.assign({ op: 'set_shape', layer_id: l.id }, patch));
    return group('Shape', [
      row('Shape', select(l.shape || 'rect', [['rect', 'Rectangle'], ['ellipse', 'Ellipse']],
        (v) => setShape({ shape: v }))),
      row('Fill', el('div', { class: 'pfield' }, [
        scrub(el('input', { type: 'color', value: l.fill || '#000000' }), l.id, {
          preview: (v) => { ctx.store.layer(l.id).fill = v; },
          commit: (v) => setShape({ fill: v }),
        }),
        el('button', { class: 'btn small', type: 'button', text: 'None', onclick: act(() => setShape({ fill: null })) }),
      ])),
      row('Stroke', el('div', { class: 'pfield' }, [
        scrub(el('input', { type: 'color', value: l.stroke || '#ffffff' }), l.id, {
          preview: (v) => { ctx.store.layer(l.id).stroke = v; },
          commit: (v) => setShape({ stroke: v }),
        }),
        el('button', { class: 'btn small', type: 'button', text: 'None', onclick: act(() => setShape({ stroke: null })) }),
      ])),
      row('Weight', slider(l.id, {
        value: pct(l.stroke_w || 0), min: 0, max: 3, step: 0.01, suffix: '% w',
        preview: (v) => { ctx.store.layer(l.id).stroke_w = fromPct(v); },
        commit: (v) => setShape({ stroke_w: fromPct(v) }),
      })),
      row('Corner', slider(l.id, {
        value: l.radius || 0, min: 0, max: 0.5, step: 0.005,
        preview: (v) => { ctx.store.layer(l.id).radius = Number(v); },
        commit: (v) => setShape({ radius: Number(v) }),
      })),
    ]);
  }

  const EFFECT_LABEL = { bevel: 'Bevel', levels: 'Levels' };
  function effectSummary(e) {
    if (e.type === 'bevel') return `depth ${e.params?.depth ?? 0}`;
    if (e.type === 'levels') {
      const p = e.params || {};
      return `b ${Number(p.brightness || 0).toFixed(2)} · c ${Number(p.contrast || 0).toFixed(2)}`;
    }
    return `blur ${((e.params?.blur || 0) * 100).toFixed(1)}%`;
  }

  function effectsGroup(l) {
    const effects = l.effects || [];
    if (!effects.length) return null;
    return group('Effects', [
      ...effects.map((e, i) => el('div', { class: 'prow' }, [
        el('span', { class: 'plabel', text: EFFECT_LABEL[e.type] || 'Shadow ' + (i + 1) }),
        el('div', { class: 'pfield' }, [
          el('span', { class: 'unit', text: effectSummary(e) }),
          el('button', {
            class: 'btn small danger', type: 'button', text: 'Remove',
            onclick: () => ctx.store.apply({
              op: 'set_effects', layer_id: l.id, effects: effects.filter((_, j) => j !== i),
            }),
          }),
        ]),
      ])),
    ]);
  }

  function update() {
    // Never rebuild the panel out from under a control the user is holding.
    if (root.contains(document.activeElement) && document.activeElement !== document.body) return;
    body.textContent = '';
    const l = ctx.selectedLayer();
    if (!l) {
      body.appendChild(el('div', { class: 'rail-empty', text: 'Select a layer to edit it.' }));
      return;
    }
    const kindGroup = {
      text: textGroup, art: artGroup, scrim: scrimGroup, frame: frameStyleGroup, shape: shapeGroup,
    }[l.kind];
    [spineNote(l), kindGroup && kindGroup(l), frameGroup(l), effectsGroup(l), commonGroup(l)]
      .filter(Boolean).forEach((g) => body.appendChild(g));
  }

  return { root, body, update };
}
