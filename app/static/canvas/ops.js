/* The op layer: the single choke point every mutation goes through.

   One rule holds the whole editor together — a click, an arrow key, a shelf
   button and the assistant all edit the doc by *the same ops*, so anything any
   of them does is undoable the same way and persists the same way (spec §4).
   Nothing outside this module writes to `doc`.

   Wire shape: an op is {op:"<name>", layer_id:…, …fields}. Layer indexes are
   into the bottom→top `layers` array, matching the doc's own order.

   The inverse is captured BEFORE the op is applied (a set_frame is absolute,
   so its inverse is the layer's prior frame — which no longer exists once the
   op lands). Undone ops are queued to the server too: they are just more ops. */

import { panelEdgesIn, sheetInches, sheetSize } from './wrap.js';

export const NUDGE = 0.005;
export const NUDGE_BIG = 0.02;

const FLUSH_MS = 400;
const RETRY_MS = 900;
const MAX_UNDO = 200;

export const clone = (v) => (v === undefined ? v : JSON.parse(JSON.stringify(v)));

const findLayer = (doc, id) => (doc.layers || []).find((l) => l.id === id);
const findIndex = (doc, id) => (doc.layers || []).findIndex((l) => l.id === id);

/* Every op's own fields — used to apply, to invert and to diff, so the three
   can never drift apart, and mirroring docproof/canvas/ops.py's tables so the
   wire means the same thing on both ends. */
const FRAME_FIELDS = ['x', 'y', 'w', 'h', 'rotation', 'flip_h', 'flip_v',
  'corners'];
const TEXT_FIELDS = ['text', 'family', 'style', 'size', 'color', 'tracking',
  'align', 'line_height', 'warp'];
const LAYER_FIELDS = ['name', 'visible', 'locked', 'opacity'];
const ART_FIELDS = ['source', 'fit'];
const SCRIM_FIELDS = ['color', 'gradient'];
const FRAME_STYLE_FIELDS = ['preset', 'stroke', 'stroke_w', 'inset', 'fill'];
const SHAPE_FIELDS = ['shape', 'fill', 'stroke', 'stroke_w', 'radius'];

/* The print wrap's three adjustable numbers. `trim_w_in`/`trim_h_in` are NOT
   here for the reason ops.py gives: the trim is the book's size, so changing
   it is a different book rather than a re-measured wrap. */
const WRAP_FIELDS = ['spine_in', 'bleed_in', 'dpi'];

/* The four kinds whose own parameters have a typed op. One table, because a
   diff and a property panel must reach for the same verb: rebuilding a scrim
   with remove+add to recolour it throws away its id and its place in the
   stack (see the ops.py note on the same three verbs). */
const KIND_OPS = {
  art: { op: 'set_art', fields: ART_FIELDS },
  scrim: { op: 'set_scrim', fields: SCRIM_FIELDS },
  frame: { op: 'set_frame_style', fields: FRAME_STYLE_FIELDS },
  shape: { op: 'set_shape', fields: SHAPE_FIELDS },
};

/* Copy across whichever of `fields` the op actually names. `undefined` means
   "not named"; null is a value (a frame ornament with no fill, an unpinned
   corner set), so the test is against undefined and never falsy. */
function assign(target, op, fields) {
  for (const k of fields) if (op[k] !== undefined) target[k] = clone(op[k]);
}

/* How close to the sheet's own edges a layer has to reach before it counts as
   BEING the sheet rather than sitting on a panel — ops.py's _FULL_SHEET_EPS,
   and the same hair of tolerance for a 1.0 that has been through JSON and a
   scale gesture. */
const FULL_SHEET_EPS = 1e-6;

/* MIRROR OF docproof/canvas/ops.py::_op_set_wrap + _rewrapped_layers.

   It has to be a mirror and not an approximation: a flush takes only the money
   back from the server's answer and keeps the local document (see flush), so
   the one op that moves EVERY layer at once is the one op a sloppy local apply
   would silently drift on. The rule, in a sentence: nothing moves on the panel
   it sits on. A layer's panel is decided by its centre on the OLD sheet, it
   keeps its distance from that panel's own edge (the spine's from the spine's
   CENTRE, which is what keeps spine type centred as the spine widens), sizes
   are preserved in INCHES, and a layer covering the whole sheet keeps its
   fractions instead so the wrap's ground still covers the wrap. */
function applySetWrap(doc, op) {
  // The server refuses this outright on a front cover; nothing to do here but
  // agree, so a stray op cannot invent a wrap the document does not have.
  if (!doc.wrap) return;
  const old = doc.wrap;
  const next = Object.assign({}, old);
  for (const k of WRAP_FIELDS) if (op[k] !== undefined) next[k] = Number(op[k]);

  const oldSheet = sheetInches(old);
  const newSheet = sheetInches(next);
  const oldEdges = panelEdgesIn(old);
  const newEdges = panelEdgesIn(next);
  const [spineX0, spineX1] = oldEdges.spine;
  const dy = next.bleed_in - old.bleed_in;
  const shiftX = (centerIn) => {
    if (centerIn < spineX0) return newEdges.back[0] - oldEdges.back[0];
    if (centerIn > spineX1) return newEdges.front[0] - oldEdges.front[0];
    return ((newEdges.spine[0] + newEdges.spine[1])
      - (oldEdges.spine[0] + oldEdges.spine[1])) / 2;
  };

  (doc.layers || []).forEach((l) => {
    const f = l.frame;
    const spansW = f.x - f.w / 2 <= FULL_SHEET_EPS && f.x + f.w / 2 >= 1 - FULL_SHEET_EPS;
    const spansH = f.y - f.h / 2 <= FULL_SHEET_EPS && f.y + f.h / 2 >= 1 - FULL_SHEET_EPS;
    // Read off the ORIGINAL centre, before anything below moves it.
    const dx = shiftX(f.x * oldSheet.w);
    const mapX = (v) => (spansW ? v : (v * oldSheet.w + dx) / newSheet.w);
    const mapY = (v) => (spansH ? v : (v * oldSheet.h + dy) / newSheet.h);
    if (Array.isArray(f.corners) && f.corners.length === 4) {
      f.corners = f.corners.map(([px, py]) => [mapX(px), mapY(py)]);
    }
    f.x = mapX(f.x);
    f.y = mapY(f.y);
    if (!spansW) f.w = f.w * oldSheet.w / newSheet.w;
    if (!spansH) f.h = f.h * oldSheet.h / newSheet.h;
  });

  doc.wrap = next;
  // The canvas IS the sheet on a wrapped document, so it is re-measured with
  // it — a canvas that disagreed with its wrap is a doc the server won't load.
  doc.canvas = sheetSize(next);
}

export function applyOp(doc, op) {
  const layer = op.layer_id ? findLayer(doc, op.layer_id) : null;
  switch (op.op) {
    case 'set_frame': {
      if (!layer) return;
      assign(layer.frame, op, FRAME_FIELDS);
      return;
    }
    case 'nudge': {
      if (!layer) return;
      layer.frame.x += op.dx || 0;
      layer.frame.y += op.dy || 0;
      return;
    }
    case 'set_text': {
      if (!layer) return;
      assign(layer, op, TEXT_FIELDS);
      return;
    }
    case 'set_layer': {
      if (!layer) return;
      assign(layer, op, LAYER_FIELDS);
      return;
    }
    /* The four typed parameter ops. `set_art`'s `source` is a plate swap: the
       server only accepts a plate this layer already has, so hopping along
       the strip neither reorders nor consumes it.

       MIRROR OF docproof/canvas/ops.py::_op_set_art, including the one write
       the swap makes to the shelf. regen only shelves the plate it REPLACES,
       so the newest plate exists nowhere but `source` — swapping away from it
       without shelving it first strands the plate the person paid for most
       recently. The server has always done this; not mirroring it here is
       what made clicking back along the strip look like it ATE the newest
       plate: the flush keeps our local copy of the document (see flush), so
       the server's preserved shelf was never read back. */
    case 'set_art':
    case 'set_scrim':
    case 'set_frame_style':
    case 'set_shape': {
      if (!layer) return;
      const spec = KIND_OPS[layer.kind];
      if (!spec || spec.op !== op.op) return;
      if (op.op === 'set_art' && op.source !== undefined
          && op.source !== layer.source) {
        const shelf = layer.plate_history || [];
        if (!shelf.some((h) => h.source === layer.source)) {
          layer.plate_history = shelf.concat(
            [{ source: layer.source, prompt: layer.prompt }]);
        }
      }
      assign(layer, op, spec.fields);
      return;
    }
    case 'set_effects': {
      if (!layer) return;
      layer.effects = clone(op.effects) || [];
      return;
    }
    case 'add_layer': {
      const at = (op.index === null || op.index === undefined)
        ? doc.layers.length : Math.max(0, Math.min(doc.layers.length, op.index));
      doc.layers.splice(at, 0, clone(op.layer));
      return;
    }
    case 'remove_layer': {
      const i = findIndex(doc, op.layer_id);
      if (i >= 0) doc.layers.splice(i, 1);
      return;
    }
    case 'reorder_layer': {
      const i = findIndex(doc, op.layer_id);
      if (i < 0) return;
      const [moved] = doc.layers.splice(i, 1);
      const at = Math.max(0, Math.min(doc.layers.length, op.index));
      doc.layers.splice(at, 0, moved);
      return;
    }
    /* The one op with no layer_id: it addresses the document. */
    case 'set_wrap': {
      applySetWrap(doc, op);
      return;
    }
    default:
      // An op we don't know is the server's business, not ours: leave the doc
      // alone rather than guessing (the /ops response stays authoritative).
      return;
  }
}

function invertOp(doc, op) {
  const layer = op.layer_id ? findLayer(doc, op.layer_id) : null;
  switch (op.op) {
    case 'set_frame':
      if (!layer) return null;
      return Object.assign({ op: 'set_frame', layer_id: op.layer_id }, clone(layer.frame));
    case 'nudge':
      return { op: 'nudge', layer_id: op.layer_id, dx: -(op.dx || 0), dy: -(op.dy || 0) };
    case 'set_text': {
      if (!layer) return null;
      const back = { op: 'set_text', layer_id: op.layer_id };
      for (const k of TEXT_FIELDS) if (op[k] !== undefined) back[k] = clone(layer[k]);
      return back;
    }
    case 'set_layer': {
      if (!layer) return null;
      const back = { op: 'set_layer', layer_id: op.layer_id };
      for (const k of LAYER_FIELDS) if (op[k] !== undefined) back[k] = layer[k];
      return back;
    }
    case 'set_art':
    case 'set_scrim':
    case 'set_frame_style':
    case 'set_shape': {
      if (!layer) return null;
      const spec = KIND_OPS[layer.kind];
      if (!spec || spec.op !== op.op) return null;
      const back = { op: op.op, layer_id: op.layer_id };
      for (const k of spec.fields) if (op[k] !== undefined) back[k] = clone(layer[k]);
      return back;
    }
    case 'set_effects':
      if (!layer) return null;
      return { op: 'set_effects', layer_id: op.layer_id, effects: clone(layer.effects) || [] };
    case 'add_layer':
      return { op: 'remove_layer', layer_id: op.layer.id };
    case 'remove_layer': {
      const i = findIndex(doc, op.layer_id);
      if (i < 0) return null;
      return { op: 'add_layer', layer: clone(doc.layers[i]), index: i };
    }
    case 'reorder_layer': {
      const i = findIndex(doc, op.layer_id);
      if (i < 0) return null;
      return { op: 'reorder_layer', layer_id: op.layer_id, index: i };
    }
    /* A re-measured wrap undoes by being re-measured back: the remap is one
       affine per axis per panel, so running it in reverse puts every layer on
       the fraction it came from. Only the fields this op actually named are
       restored — an op that widened the spine must not also silently re-state
       a dpi nobody touched. */
    case 'set_wrap': {
      if (!doc.wrap) return null;
      const back = { op: 'set_wrap' };
      for (const k of WRAP_FIELDS) if (op[k] !== undefined) back[k] = doc.wrap[k];
      return Object.keys(back).length > 1 ? back : null;
    }
    default:
      return null;
  }
}

/* Ops that turn `from` into `to`. The assistant hands back a whole doc rather
   than the ops it applied to it, and an undo of that turn has to travel the
   wire as ops like everything else — so we recover them by comparison. */
export function diffDocs(from, to) {
  const ops = [];
  const fromIds = (from.layers || []).map((l) => l.id);
  const toIds = (to.layers || []).map((l) => l.id);
  const toById = new Map((to.layers || []).map((l) => [l.id, l]));
  const fromById = new Map((from.layers || []).map((l) => [l.id, l]));

  /* The document-level op goes FIRST, because it remaps every layer and every
     frame op below it is absolute — they have to land on the sheet set_wrap
     made. Only a RE-MEASURE is expressible: the conversion from front cover to
     wrap has no inverse op at all (docproof/canvas/wrap.py — there is no
     `to_front`), so a document that gained or lost its wrap never travels this
     way, and the editor doesn't ask it to (app.js convertToWrap). */
  if (from.wrap && to.wrap) {
    const changed = WRAP_FIELDS.filter((k) => from.wrap[k] !== to.wrap[k]);
    if (changed.length) {
      const op = { op: 'set_wrap' };
      for (const k of changed) op[k] = to.wrap[k];
      ops.push(op);
    }
  }

  for (const id of fromIds) if (!toById.has(id)) ops.push({ op: 'remove_layer', layer_id: id });
  toIds.forEach((id, i) => {
    if (!fromById.has(id)) ops.push({ op: 'add_layer', layer: clone(toById.get(id)), index: i });
  });

  for (const id of toIds) {
    const a = fromById.get(id); const b = toById.get(id);
    if (!a) continue;
    const same = (k) => JSON.stringify(a[k]) === JSON.stringify(b[k]);
    if (!same('frame')) {
      ops.push(Object.assign({ op: 'set_frame', layer_id: id }, clone(b.frame)));
    }
    const textChanged = TEXT_FIELDS.filter((k) => b[k] !== undefined && !same(k));
    if (textChanged.length) {
      const op = { op: 'set_text', layer_id: id };
      for (const k of textChanged) op[k] = clone(b[k]);
      ops.push(op);
    }
    const layerChanged = LAYER_FIELDS.filter((k) => b[k] !== undefined && !same(k));
    if (layerChanged.length) {
      const op = { op: 'set_layer', layer_id: id };
      for (const k of layerChanged) op[k] = b[k];
      ops.push(op);
    }
    /* The kind's own parameters, as its typed op. This is what makes a plate
       verb undoable: a re-roll changes `source` and nothing else an op can
       say, and before `source` was diffable the whole turn produced an empty
       diff and therefore no undo entry at all. */
    const spec = KIND_OPS[b.kind];
    if (spec) {
      const changed = spec.fields.filter((k) => b[k] !== undefined && !same(k));
      if (changed.length) {
        const op = { op: spec.op, layer_id: id };
        for (const k of changed) op[k] = clone(b[k]);
        ops.push(op);
      }
    }
    if (!same('effects')) ops.push({ op: 'set_effects', layer_id: id, effects: clone(b.effects) || [] });
  }

  // Order last, once both sides hold the same ids: walk the target order and
  // pull anything out of place into position.
  const order = fromIds.filter((id) => toById.has(id));
  for (const id of toIds) if (!order.includes(id)) order.push(id);
  toIds.forEach((id, want) => {
    const at = order.indexOf(id);
    if (at !== want) {
      order.splice(at, 1);
      order.splice(want, 0, id);
      ops.push({ op: 'reorder_layer', layer_id: id, index: want });
    }
  });
  return ops;
}

/* onChange(doc)      — re-render everything
   onStatus(state)    — 'saved' | 'saving' | 'dirty' | 'stuck', for the shelf
   send(ops)          — POST the queue; resolves to the server's {doc,...} */
export function createStore({ doc, onChange, onStatus, send, onError }) {
  let current = doc;
  const undoStack = [];
  const redoStack = [];
  let queue = [];
  let timer = null;
  let inFlight = false;
  let stuck = false;

  const status = () => {
    if (stuck) return 'stuck';
    if (inFlight) return 'saving';
    return queue.length ? 'dirty' : 'saved';
  };
  const tellStatus = () => onStatus && onStatus(status());

  function schedule() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(flush, FLUSH_MS);
    tellStatus();
  }

  async function flush(isRetry = false) {
    if (timer) { clearTimeout(timer); timer = null; }
    if (inFlight || !queue.length || !send) return;
    const batch = queue;
    queue = [];
    inFlight = true;
    tellStatus();
    try {
      const res = await send(batch);
      stuck = false;
      inFlight = false;
      /* The server's doc is authoritative on disk, but the local doc is the one
         under the user's cursor — adopting it mid-drag or mid-keystroke would
         fight them. We take only the money back and keep our copy. */
      const cost = res && (res.cost_usd ?? (res.doc && res.doc.cost_usd));
      if (typeof cost === 'number') current.cost_usd = cost;
      tellStatus();
      if (queue.length) schedule();
      onChange && onChange(current, { costOnly: true });
    } catch (err) {
      inFlight = false;
      /* A 4xx is the server REFUSING this batch (a 409 naming one bad op, a
         422 on a malformed layer) — replaying it just jams the queue forever.
         Drop it, say so, and let the caller re-sync from the server; only a
         network blip or a 5xx is worth a retry. */
      const permanent = err && err.status >= 400 && err.status < 500
        && err.status !== 408 && err.status !== 429;
      if (permanent) {
        stuck = false;
        tellStatus();
        onError && onError(err, { permanent: true, ops: batch });
        return;
      }
      // Put the batch back at the front — the local doc never loses an edit.
      queue = batch.concat(queue);
      if (!isRetry) {
        setTimeout(() => flush(true), RETRY_MS);
        tellStatus();
        return;
      }
      stuck = true;
      tellStatus();
      onError && onError(err, { permanent: false, ops: batch });
    }
  }

  function run(ops, { undoable = true } = {}) {
    const list = (Array.isArray(ops) ? ops : [ops]).filter(Boolean);
    if (!list.length) return;
    const inverse = [];
    for (const op of list) {
      const back = invertOp(current, op);
      if (back) inverse.unshift(back);   // undo runs the batch backwards
      applyOp(current, op);
    }
    if (undoable) {
      undoStack.push({ ops: clone(list), inverse });
      if (undoStack.length > MAX_UNDO) undoStack.shift();
      redoStack.length = 0;
    }
    queue.push(...list);
    schedule();
    onChange && onChange(current);
  }

  return {
    get doc() { return current; },
    get canUndo() { return undoStack.length > 0; },
    get canRedo() { return redoStack.length > 0; },
    get pending() { return queue.length; },
    status,

    apply(ops) { run(ops); },

    /* A whole-doc swap (the assistant's return value, a re-roll, a finalize,
       a ground pass, an inpaint, a rebalance) recorded as one undo entry,
       expressed in ops so undoing it persists like anything else.

       An empty diff still pushes nothing: a change the op vocabulary cannot
       say would put an Undo on the shelf that does nothing when pressed.

       A roll is both undoable and redoable. Undoing one is a `set_art` back
       to the plate the roll replaced, which the server accepts because that
       plate is on the layer's history shelf; redoing it asks for the newly
       minted plate, which is still on the shelf because a swap PRESERVES the
       plate it swaps away from (docproof/canvas/ops.py::_op_set_art, now
       mirrored in applyOp). This used to push `noRedo` on a set_art entry —
       correct while the local apply dropped that preservation, and wrong
       once it stopped. */
    replaceDoc(next) {
      const before = clone(current);
      const forward = diffDocs(before, next);
      const back = diffDocs(next, before);
      current = next;
      if (forward.length) {
        undoStack.push({ ops: forward, inverse: back, wholeDoc: true });
        if (undoStack.length > MAX_UNDO) undoStack.shift();
        redoStack.length = 0;
      }
      // The server already holds this state; don't echo it back at /ops.
      onChange && onChange(current);
      tellStatus();
    },

    undo() {
      const entry = undoStack.pop();
      if (!entry) return false;
      for (const op of entry.inverse) applyOp(current, op);
      queue.push(...entry.inverse);
      redoStack.push(entry);
      schedule();
      onChange && onChange(current);
      return true;
    },

    redo() {
      const entry = redoStack.pop();
      if (!entry) return false;
      for (const op of entry.ops) applyOp(current, op);
      queue.push(...entry.ops);
      undoStack.push(entry);
      schedule();
      onChange && onChange(current);
      return true;
    },

    /* Take the server's document as truth — used after a refused batch, when
       the local copy has drifted from what is on disk. Not an edit: no undo
       entry, nothing queued back. */
    adopt(next) {
      current = next;
      onChange && onChange(current);
      tellStatus();
    },

    /* A document that REPLACES this one rather than editing it: the print-wrap
       conversion, and only that.

       Why it clears the stacks instead of pushing an entry like replaceDoc.
       The conversion is one-way on the server by design (docproof/canvas/
       wrap.py: "a wrap that could be un-wrapped would have to decide what
       happens to the back copy and the spine type"), so there is no op that
       says "un-wrap". An undo entry here could only carry the LAYER half of
       the change — the frames would spring back to front-cover fractions while
       the canvas stayed a sheet, and those ops would travel the wire and write
       that nonsense to disk. The same goes for everything already on the
       stack: an undo of a nudge made before the conversion is stated in
       fractions of a canvas that no longer exists. So the history ends here,
       which is the honest reading of "this is a different document now". */
    reset(next) {
      current = next;
      undoStack.length = 0;
      redoStack.length = 0;
      onChange && onChange(current);
      tellStatus();
    },

    flushNow() { return flush(); },
    layer(id) { return findLayer(current, id); },
    indexOf(id) { return findIndex(current, id); },
  };
}
