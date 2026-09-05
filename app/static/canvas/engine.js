/* The canvas itself: a Konva stage that draws the CanvasDoc, and the handles
   that let a person reach into it.

   Three coordinate spaces, and every bug in here is a confusion between them:
     doc      — fractions of the reference canvas. frame.x/y are the CENTRE of
                the layer box; frame.w/h its size. This is what ops speak.
     world    — reference pixels (doc.canvas.w × doc.canvas.h). The `world`
                group holds the whole cover at 1:1; zoom and pan are its scale
                and position, so nothing else in the render path knows about
                either.
     screen   — stage pixels. Only the overlay layer lives here.

   The Transformer is deliberately NOT inside `world`: Konva's Transformer
   overrides getAbsoluteTransform() to return its own local transform, so it
   only draws correctly under an untransformed parent. It gets its own
   identity-transform layer, which also keeps the handles a fixed size at any
   zoom and keeps them out of every export for free. The corner-pin handles
   share that layer for the same three reasons.

   The corner pin is a RENDER-side distortion and nothing else: x/y/w/h/
   rotation still say where the box is, and `frame.corners` — four
   canvas-fraction points, TL/TR/BR/BL — only says how the pixels sit inside
   it. A client that could not draw the pin would draw an undistorted plate
   in exactly the right place, which is why the two are separable at all. */

import { PANEL_NAMES, panels, snapLines, wrapKey } from './wrap.js';

const DEG = Math.PI / 180;
const MIN_ZOOM = 0.04;
const ANCHOR = 9;          // transformer handle size, in screen px
const MAX_ZOOM = 8;

/* How close a dragged edge has to come before a guide line catches it, in
   SCREEN pixels — so the pull feels the same at any zoom, and zooming in is
   how you place something between two lines that snap on top of each other. */
const SNAP_PX = 6;

/* The guide palette, in the chrome's own ink (style.css tokens, restated here
   because Konva takes colours and not variables). Light on purpose: these are
   the printer's marks, not the design. */
const GUIDE_STYLE = {
  bleed: { stroke: 'rgba(239,228,203,.16)', width: 1, dash: null },
  trim: { stroke: 'rgba(239,228,203,.34)', width: 1, dash: null },
  fold: { stroke: 'rgba(207,106,52,.55)', width: 1, dash: [9, 5] },
  safe: { stroke: 'rgba(92,152,142,.5)', width: 1, dash: [4, 4] },
};

/* Corner-pin mesh density. 12×12 is where the eye stops seeing facets on a
   book-cover-sized plate at a plausible pin angle; going finer costs
   drawImage calls per frame and buys nothing anybody can see. */
const MESH = 12;

/* One source pixel of overlap on each cell's right and bottom edge.
   Neighbouring affine patches meet at slightly different angles, and without
   the overlap the seams show as hairlines of whatever is behind the plate.
   The overlapping pixels are the true neighbouring source pixels, so on an
   opaque plate the overdraw is invisible; on a cutout it doubles alpha along
   a one-pixel seam, which is the cheaper of the two artefacts. */
const MESH_BLEED = 1;

/* The doc's own latitude for a frame point (model.py Frame): a pinned corner
   may hang off the trim exactly as far as the box's centre may. */
const clampFrac = (v) => Math.max(-2, Math.min(2, v));

/* Konva builds its context font as "<style> <variant> <size>px <family>" with
   no quoting; measure warped glyphs exactly the same way or the per-character
   layout drifts from the un-warped rendering of the same string. */
const measureCanvas = document.createElement('canvas');
const measureCtx = measureCanvas.getContext('2d');
function fontString(l, fontSize) {
  const style = l.style === 'bold' ? 'bold' : (l.style === 'italic' ? 'italic' : 'normal');
  return `${style} normal ${fontSize}px ${l.family || 'serif'}`;
}
const konvaStyle = (s) => (s === 'bold' || s === 'italic' ? s : 'normal');

function hexToRGBA(hex, alpha) {
  const h = String(hex || '#000000').replace('#', '');
  const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h.slice(0, 6);
  const n = parseInt(full || '000000', 16);
  const a = alpha === undefined ? 1 : Math.max(0, Math.min(1, alpha));
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

/* Konva only carries a shadow on a Shape, never on a Group, so an effect has
   to be painted onto every leaf of the layer's content. */
function eachShape(node, fn) {
  if (!node) return;
  if (typeof node.getChildren === 'function') node.getChildren().forEach((c) => eachShape(c, fn));
  else fn(node);
}

/* A frame's corner pin, validated the way a renderer needs it: four finite
   [x,y] pairs or nothing at all. The doc can carry `corners: null` (unpinned)
   and predates the field entirely on older sessions, so every read goes
   through here rather than trusting the shape. */
function cornersOf(l) {
  const c = l && l.frame && l.frame.corners;
  if (!Array.isArray(c) || c.length !== 4) return null;
  for (const p of c) {
    if (!Array.isArray(p) || p.length !== 2) return null;
    if (!Number.isFinite(p[0]) || !Number.isFinite(p[1])) return null;
  }
  return c;
}

/* Square-to-quad, the classic closed form: the projective map taking
   (0,0),(1,0),(1,1),(0,1) onto TL, TR, BR, BL.

   Mesh VERTICES go through this exactly; each cell is then filled with a
   plain affine drawImage built from three of its four corners. That is the
   deliberate approximation — a grid of affine patches whose corners sit on
   the true homography reads as one smooth perspective, where a genuine
   per-pixel projective warp would mean writing a rasterizer in JS and
   giving up the browser's own filtering. Visually smooth beats
   mathematically exact here. */
function squareToQuad(q) {
  const [p0, p1, p2, p3] = q;
  const sx = p0.x - p1.x + p2.x - p3.x;
  const sy = p0.y - p1.y + p2.y - p3.y;
  let g = 0; let h = 0;
  // sx and sy are both zero exactly when the quad is a parallelogram, which
  // has no projective term — the affine branch, and the one a freshly
  // pinned (still rectangular) layer takes.
  if (Math.abs(sx) > 1e-9 || Math.abs(sy) > 1e-9) {
    const dx1 = p1.x - p2.x; const dx2 = p3.x - p2.x;
    const dy1 = p1.y - p2.y; const dy2 = p3.y - p2.y;
    const den = dx1 * dy2 - dx2 * dy1;
    if (Math.abs(den) > 1e-9) {
      g = (sx * dy2 - dx2 * sy) / den;
      h = (dx1 * sy - sx * dy1) / den;
    }
  }
  return {
    a: p1.x - p0.x + g * p1.x, b: p3.x - p0.x + h * p3.x, c: p0.x,
    d: p1.y - p0.y + g * p1.y, e: p3.y - p0.y + h * p3.y, f: p0.y,
    g, h,
  };
}

function projected(m, u, v) {
  const w = m.g * u + m.h * v + 1 || 1e-9;
  return { x: (m.a * u + m.b * v + m.c) / w, y: (m.d * u + m.e * v + m.f) / w };
}

/* A `levels` effect as the two dials Konva's own filters take.

   The doc's contract (docproof/canvas/regen.py plan_correction) is
   brightness added to normalized luminance, then contrast widening about
   mid-grey: out = (v + brightness − 0.5)·(1 + contrast) + 0.5. Konva's
   Brighten adds brightness·255, which is the same number. Its Contrast dial
   is a percentage it SQUARES — adjust = ((dial + 100)/100)² — so the dial
   that yields our multiplier is that multiplier's square root. */
function levelsOf(l) {
  const e = (l.effects || []).find((x) => x && x.type === 'levels');
  if (!e) return null;
  const p = e.params || {};
  const brightness = Math.max(-1, Math.min(1, Number(p.brightness) || 0));
  // −1 would collapse the image to flat grey and anything below it inverts;
  // the server clamps to ±0.15, but this is the wire and the wire is untrusted.
  const c = Math.max(-0.95, Math.min(4, Number(p.contrast) || 0));
  if (!brightness && !c) return null;
  return { brightness, contrast: 100 * (Math.sqrt(1 + c) - 1) };
}


/* ------------------------------------------- masks & adjust layers (§15.2/3)

   Everything from here to createEngine mirrors docproof/cover/effects.py,
   which is the module the SERVER runs to compose a cover and to render this
   document headlessly. That is a deliberate change of allegiance for this
   file: the rest of it mirrors docproof/canvas/render.py's geometry, but a
   mask's threshold and a grade's curve are not geometry — they are pixel
   math a cover is judged on, and a canvas that graded differently from the
   composer would show a designer one cover and deliver another.

   Named constants, each with the effects.py line it mirrors:

     MASK_SCALE          gradient_mask: quarter-scale synthesis
     MASK_STENCIL_CUT    resolve_mask: from_layer thresholds alpha at 50%
     TEMPERATURE_MAX_SHIFT  _op_grade: 8-bit R+/B- shift at |temperature| = 1
     WCAG_*              luminance_band: relative-luminance weights

   The one structural difference: where effects.py loops per pixel to paint a
   ramp, this asks the browser for the same ramp through createLinearGradient
   / createRadialGradient. The RAMP is the contract; the loop was never it. */
const MASK_SCALE = 0.25;
const MASK_STENCIL_CUT = 127;
const TEMPERATURE_MAX_SHIFT = 24;
const WCAG_R = 0.2126; const WCAG_G = 0.7152; const WCAG_B = 0.0722;

/* Konva blend names for the §15.1 table. Only color_wash reads a blend. */
const BLEND_CSS = {
  normal: 'source-over', multiply: 'multiply', overlay: 'overlay',
  soft_light: 'soft-light', screen: 'screen', add: 'lighter',
  lighten: 'lighten', darken: 'darken', color_dodge: 'color-dodge',
};

function scratch(w, h) {
  const c = document.createElement('canvas');
  c.width = Math.max(1, Math.round(w));
  c.height = Math.max(1, Math.round(h));
  return c;
}

/* effects.luminance_band's LUT: sRGB -> linear, once. */
const SRGB_LUT = (() => {
  const t = new Float32Array(256);
  for (let i = 0; i < 256; i += 1) {
    const c = i / 255;
    t[i] = c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  }
  return t;
})();
function wcagLuma(d, i) {
  return 255 * (WCAG_R * SRGB_LUT[d[i]] + WCAG_G * SRGB_LUT[d[i + 1]]
                + WCAG_B * SRGB_LUT[d[i + 2]]);
}

/* One gradient source as a white-on-transparent alpha field, canvas-sized.
   `start`/`end` remap where along the ramp alpha actually moves — the
   browser expresses that as the gradient's own endpoints rather than
   gradient_mask's per-pixel clamp, which is the same ramp. */
function gradientField(g, w, h) {
  const small = scratch(w * MASK_SCALE, h * MASK_SCALE);
  const ctx = small.getContext('2d');
  const s = Math.max(0, Math.min(1, g.start === undefined ? 0 : g.start));
  const e = Math.max(s + 1e-9, Math.min(1, g.end === undefined ? 1 : g.end));
  let grad;
  if ((g.kind || 'linear') === 'radial') {
    const cx = (g.center ? g.center[0] : 0.5) * (small.width - 1);
    const cy = (g.center ? g.center[1] : 0.5) * (small.height - 1);
    const far = Math.max(
      Math.hypot(cx, cy), Math.hypot(small.width - 1 - cx, cy),
      Math.hypot(cx, small.height - 1 - cy),
      Math.hypot(small.width - 1 - cx, small.height - 1 - cy)) || 1;
    grad = ctx.createRadialGradient(cx, cy, far * s, cx, cy, far * e);
  } else {
    /* The direction vector in y-down degrees (90 = top-transparent to
       bottom-opaque), spanning the canvas's own projected extent — the
       p_min/p_range normalization gradient_mask does, as two endpoints. */
    const th = (g.angle === undefined ? 90 : g.angle) * DEG;
    const ux = Math.cos(th); const uy = Math.sin(th);
    const proj = [[0, 0], [small.width - 1, 0], [0, small.height - 1],
                  [small.width - 1, small.height - 1]]
      .map(([x, y]) => x * ux + y * uy);
    const lo = Math.min(...proj);
    const span = (Math.max(...proj) - lo) || 1e-9;
    const at = (t) => ({ x: ux * (lo + t * span), y: uy * (lo + t * span) });
    const a = at(s); const b = at(e);
    grad = ctx.createLinearGradient(a.x, a.y, b.x, b.y);
  }
  grad.addColorStop(0, 'rgba(255,255,255,0)');
  grad.addColorStop(1, 'rgba(255,255,255,1)');
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, small.width, small.height);
  const full = scratch(w, h);
  const fctx = full.getContext('2d');
  fctx.imageSmoothingQuality = 'high';
  fctx.drawImage(small, 0, 0, w, h);
  return full;
}

/* One already-drawn layer as an alpha field: a hard stencil (from_layer) or
   its luminance gated by its own alpha (luminance_of), matching
   resolve_mask's two readings exactly. */
function layerField(raster, mode) {
  const ctx = raster.getContext('2d');
  const img = ctx.getImageData(0, 0, raster.width, raster.height);
  const d = img.data;
  for (let i = 0; i < d.length; i += 4) {
    const a = mode === 'luminance'
      ? (wcagLuma(d, i) * d[i + 3]) / 255
      : (d[i + 3] > MASK_STENCIL_CUT ? 255 : 0);
    d[i] = 255; d[i + 1] = 255; d[i + 2] = 255; d[i + 3] = a;
  }
  ctx.putImageData(img, 0, 0);
  return raster;
}

/* Multiply `into` by `field`, both canvas-sized alpha fields. resolve_mask's
   fold, done with a composite op instead of ImageChops.multiply. */
function foldField(into, field) {
  if (!into) return field;
  const ctx = into.getContext('2d');
  ctx.globalCompositeOperation = 'destination-in';
  ctx.drawImage(field, 0, 0);
  ctx.globalCompositeOperation = 'source-over';
  return into;
}

function invertField(field) {
  const ctx = field.getContext('2d');
  const img = ctx.getImageData(0, 0, field.width, field.height);
  const d = img.data;
  for (let i = 0; i < d.length; i += 4) {
    d[i] = 255; d[i + 1] = 255; d[i + 2] = 255; d[i + 3] = 255 - d[i + 3];
  }
  ctx.putImageData(img, 0, 0);
  return field;
}

/* An axis-aligned box as an alpha field, or null for a full-canvas box —
   render.py's _box_alpha, including its "None rather than 255 everywhere"
   shortcut, so a full-canvas adjust layer costs no field at all. */
function boxField(cx, cy, bw, bh, w, h) {
  const left = cx - bw / 2; const top = cy - bh / 2;
  if (left <= 0 && top <= 0 && left + bw >= w && top + bh >= h) return null;
  const c = scratch(w, h);
  const ctx = c.getContext('2d');
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(Math.round(left), Math.round(top), Math.round(bw), Math.round(bh));
  return c;
}

/* The five ops that read the pixels beneath them (effects._ADJUST_OPS), each
   rewriting `d` in place. Alpha is never touched: an adjust layer grades what
   the composite LOOKS like, never how much of it exists (_with_alpha_of). */
function gradePixels(d, l) {
  const b = l.brightness || 0; const c = l.contrast || 0;
  const sat = l.saturation || 0; const temp = l.temperature || 0;
  const shift = Math.round(temp * TEMPERATURE_MAX_SHIFT);
  const cf = 1 + c;
  for (let i = 0; i < d.length; i += 4) {
    let r = d[i]; let g = d[i + 1]; let bl = d[i + 2];
    // ImageEnhance's order, which does not commute: brightness (a scale from
    // black), contrast (about the image's own mean, approximated here by
    // mid-grey — Pillow uses the mean of the greyscale image, and 128 is
    // that mean for the graded composites this runs on), then saturation
    // (a blend from the greyscale degenerate), then the temperature LUT.
    if (b) { r *= 1 + b; g *= 1 + b; bl *= 1 + b; }
    if (c) { r = (r - 128) * cf + 128; g = (g - 128) * cf + 128; bl = (bl - 128) * cf + 128; }
    if (sat) {
      const grey = 0.299 * r + 0.587 * g + 0.114 * bl;   // Pillow's "L" weights
      r = grey + (r - grey) * (1 + sat);
      g = grey + (g - grey) * (1 + sat);
      bl = grey + (bl - grey) * (1 + sat);
    }
    if (shift) { r += shift; bl -= shift; }
    d[i] = Math.max(0, Math.min(255, r));
    d[i + 1] = Math.max(0, Math.min(255, g));
    d[i + 2] = Math.max(0, Math.min(255, bl));
  }
}

function gradientMapPixels(d, l) {
  const stops = (l.stops || []).map((hex) => {
    const h = String(hex).replace('#', '');
    const n = parseInt(h.length === 3 ? h.split('').map((x) => x + x).join('') : h, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  });
  if (stops.length < 2) return;
  // ImageOps.colorize: black->white for two stops, black->mid->white for
  // three, over Pillow's own "L" luminance (a look, not a measurement).
  const ramp = new Uint8ClampedArray(256 * 3);
  for (let v = 0; v < 256; v += 1) {
    let a; let b; let t;
    if (stops.length === 2) { a = stops[0]; b = stops[1]; t = v / 255; }
    else if (v < 128) { a = stops[0]; b = stops[1]; t = v / 127; }
    else { a = stops[1]; b = stops[2]; t = (v - 128) / 127; }
    ramp[v * 3] = a[0] + (b[0] - a[0]) * t;
    ramp[v * 3 + 1] = a[1] + (b[1] - a[1]) * t;
    ramp[v * 3 + 2] = a[2] + (b[2] - a[2]) * t;
  }
  for (let i = 0; i < d.length; i += 4) {
    const grey = Math.round(0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]);
    d[i] = ramp[grey * 3]; d[i + 1] = ramp[grey * 3 + 1]; d[i + 2] = ramp[grey * 3 + 2];
  }
}

function vignettePixels(d, l, w, h) {
  const hex = String(l.color || '#000000').replace('#', '');
  const n = parseInt(hex.length === 3 ? hex.split('').map((x) => x + x).join('') : hex, 16);
  const ink = [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  const strength = l.strength === undefined ? 0.5 : l.strength;
  const cx = w / 2; const cy = h / 2;
  const far = Math.hypot(cx, cy) || 1;
  for (let y = 0; y < h; y += 1) {
    for (let x = 0; x < w; x += 1) {
      const i = (y * w + x) * 4;
      const t = strength * Math.min(1, Math.hypot(x - cx, y - cy) / far);
      d[i] += (ink[0] - d[i]) * t;
      d[i + 1] += (ink[1] - d[i + 1]) * t;
      d[i + 2] += (ink[2] - d[i + 2]) * t;
    }
  }
}

export function createEngine({ host, getDoc, imageFor, onSelect, onCommit, onView }) {
  const stage = new Konva.Stage({ container: host, width: host.clientWidth || 10, height: host.clientHeight || 10 });
  const sceneLayer = new Konva.Layer({ listening: true });
  const overlay = new Konva.Layer({ listening: true });
  stage.add(sceneLayer, overlay);

  const world = new Konva.Group({ x: 0, y: 0, scaleX: 1, scaleY: 1 });
  const paper = new Konva.Rect({ x: 0, y: 0, fill: '#ffffff', listening: false });
  const content = new Konva.Group();
  world.add(paper, content);
  sceneLayer.add(world);

  const tr = new Konva.Transformer({
    rotateEnabled: true,
    keepRatio: false,             // shift makes it proportional (shiftBehavior 'default')
    shiftBehavior: 'default',
    ignoreStroke: true,
    padding: 2,
    anchorSize: ANCHOR,
    anchorStroke: '#cf6a34',
    anchorFill: '#17130f',
    anchorCornerRadius: 0,
    borderStroke: '#cf6a34',
    borderStrokeWidth: 1,
    borderDash: [4, 3],
    rotateAnchorOffset: 26,
    boundBoxFunc: (oldBox, box) => ((box.width < 6 || box.height < 6) ? oldBox : box),
  });
  const marquee = new Konva.Rect({
    stroke: '#cf6a34', strokeWidth: 1, dash: [5, 4],
    fill: 'rgba(207,106,52,.14)', visible: false, listening: false,
  });

  /* The print wrap's guides, and the line a drag is currently snapped to.
     They live in the overlay for the third reason the handles do: the overlay
     is a different Konva layer, so composite() never sees it — the fold lines
     are on the person's screen and never in the exported sheet or in the
     snapshot the assistant is shown. Added BEFORE the transformer so the
     handles draw over them. */
  const guides = new Konva.Group({ listening: false });
  const snapHint = new Konva.Group({ listening: false, visible: false });
  const snapHintX = new Konva.Line({ stroke: '#cf6a34', strokeWidth: 1.5, visible: false });
  const snapHintY = new Konva.Line({ stroke: '#cf6a34', strokeWidth: 1.5, visible: false });
  snapHint.add(snapHintX, snapHintY);
  overlay.add(guides, snapHint, tr, marquee);

  /* The corner pin's own handles, in the same identity-transform overlay the
     Transformer lives in and for the same reason: they stay a fixed size at
     any zoom and never reach an export. The dashed quad is drawn first so
     the handles sit on top of it. */
  const PIN_SIZE = ANCHOR + 3;
  const pinQuad = new Konva.Line({
    stroke: '#cf6a34', strokeWidth: 1, dash: [4, 3], closed: true,
    listening: false, visible: false,
  });
  overlay.add(pinQuad);
  const pinHandles = [0, 1, 2, 3].map(() => new Konva.Rect({
    width: PIN_SIZE, height: PIN_SIZE, offsetX: PIN_SIZE / 2, offsetY: PIN_SIZE / 2,
    fill: '#17130f', stroke: '#cf6a34', strokeWidth: 1.5,
    draggable: true, visible: false,
  }));
  pinHandles.forEach((h) => overlay.add(h));

  let selectedId = null;
  let marqueeMode = null;      // {layerId, onDone(rectScreen)} while drawing
  let nodeIndex = new Map();   // layer id -> {group, art}
  let dragPrior = null;
  let pinPrior = null;         // corners as they were when a pin drag began
  let filtered = [];           // nodes cached this render because of `levels`
  let interacting = false;     // a hand is on a control — don't re-cache
  let guidesVisible = true;    // the shelf's eye toggle
  let guideKey = null;         // the wrap the guide nodes were built for
  let guideNodes = [];         // {node, pts} — points in DOC fractions
  let snapCache = null;        // {key, lines} — rebuilt only when the wrap does

  const doc = () => getDoc();
  const W = () => doc().canvas.w;
  const H = () => doc().canvas.h;

  function boxOf(l) {
    const w = Math.max(1e-4, l.frame.w) * W();
    const h = Math.max(1e-4, l.frame.h) * H();
    return { bw: w, bh: h, cx: l.frame.x * W(), cy: l.frame.y * H() };
  }

  /* The pin's four corners in the coordinates the layer's own content is
     drawn in.

     The pin is stated ABSOLUTELY — "four canvas-fraction [x,y] points"
     (model.py Frame) — but it is drawn inside a group that is already
     translated to the box centre, rotated, and flipped. So each point is
     walked backwards through that transform here; the group then re-applies
     it, and the pixels land exactly on the canvas points the doc named, at
     any rotation. Doing it the other way round (drawing the quad in world
     space) would mean taking the art out of its own group and losing drag,
     z-order and opacity with it. */
  function pinLocal(l, bw, bh) {
    const c = cornersOf(l);
    if (!c) return null;
    const { cx, cy } = boxOf(l);
    const th = -(l.frame.rotation || 0) * DEG;
    const cos = Math.cos(th); const sin = Math.sin(th);
    const fx = l.frame.flip_h ? -1 : 1;
    const fy = l.frame.flip_v ? -1 : 1;
    return c.map(([u, v]) => {
      const dx = u * W() - cx;
      const dy = v * H() - cy;
      return {
        x: (dx * cos - dy * sin) / fx + bw / 2,
        y: (dx * sin + dy * cos) / fy + bh / 2,
      };
    });
  }

  /* The pinned plate: one Konva.Shape whose sceneFunc walks a MESH×MESH grid
     and drawImages each cell through the affine map its three corners imply.

     One shape rather than 144 Konva.Images because the mesh is redrawn on
     every corner-drag frame, and 144 nodes' worth of transform bookkeeping
     per frame is the expensive part; a sceneFunc is just canvas calls.

     `fit` has no meaning while pinned and is ignored: the quad IS the
     destination, so the whole plate maps onto it whatever the box says. */
  function buildPinnedArt(l, bw, bh, img) {
    const g = new Konva.Group();
    const shape = new Konva.Shape({
      listening: false,
      sceneFunc: (ctx) => {
        // Read live, not from a captured quad: a corner drag previews by
        // mutating the doc and calling batchDraw, exactly the way a slider
        // preview does, so one drag stays one undo step.
        const quad = pinLocal(l, bw, bh);
        if (!quad) return;
        const m = squareToQuad(quad);
        const iw = img.naturalWidth || img.width || 1;
        const ih = img.naturalHeight || img.height || 1;
        const cw = iw / MESH; const ch = ih / MESH;
        for (let j = 0; j < MESH; j += 1) {
          for (let i = 0; i < MESH; i += 1) {
            const u0 = i / MESH; const v0 = j / MESH;
            const a = projected(m, u0, v0);
            const b = projected(m, (i + 1) / MESH, v0);
            const d = projected(m, u0, (j + 1) / MESH);
            const sx = i * cw; const sy = j * ch;
            const sw = Math.min(cw + MESH_BLEED, iw - sx);
            const sh = Math.min(ch + MESH_BLEED, ih - sy);
            ctx.save();
            ctx.transform((b.x - a.x) / cw, (b.y - a.y) / cw,
              (d.x - a.x) / ch, (d.y - a.y) / ch, a.x, a.y);
            ctx.drawImage(img, sx, sy, sw, sh, 0, 0, sw, sh);
            ctx.restore();
          }
        }
      },
    });
    /* A custom Shape reports a zero self-rect, and the cache() that a levels
       effect needs would then clip the mesh to nothing. The quad's own
       bounding box is the honest answer, and it is the only thing that reads
       this — the transformer is hidden while a layer is pinned. */
    const quad = pinLocal(l, bw, bh) || [];
    const xs = quad.map((p) => p.x); const ys = quad.map((p) => p.y);
    const x0 = xs.length ? Math.min(...xs) : 0;
    const y0 = ys.length ? Math.min(...ys) : 0;
    shape.getSelfRect = () => ({
      x: x0, y: y0,
      width: (xs.length ? Math.max(...xs) : bw) - x0,
      height: (ys.length ? Math.max(...ys) : bh) - y0,
    });
    g.add(shape);
    return { node: g, art: null };
  }

  function buildArt(l, bw, bh) {
    const g = new Konva.Group();
    const img = imageFor(l.source);
    if (img && cornersOf(l)) return buildPinnedArt(l, bw, bh, img);
    if (!img) {
      // Plate still loading (or gone): hold the box so the layout doesn't jump.
      g.add(new Konva.Rect({
        width: bw, height: bh, fill: '#2a231b',
        stroke: 'rgba(239,228,203,.18)', strokeWidth: 1, dash: [6, 5],
      }));
      return { node: g, art: null };
    }
    const iw = img.naturalWidth || img.width || 1;
    const ih = img.naturalHeight || img.height || 1;
    const fit = l.fit || 'cover';
    let dw = bw; let dh = bh;
    if (fit !== 'stretch') {
      const s = fit === 'contain' ? Math.min(bw / iw, bh / ih) : Math.max(bw / iw, bh / ih);
      dw = iw * s; dh = ih * s;
    }
    const node = new Konva.Image({
      image: img, x: (bw - dw) / 2, y: (bh - dh) / 2, width: dw, height: dh, listening: false,
    });
    if (fit === 'cover') {
      const clip = new Konva.Group({ clipX: 0, clipY: 0, clipWidth: bw, clipHeight: bh });
      clip.add(node);
      g.add(clip);
    } else {
      g.add(node);
    }
    return { node: g, art: node };
  }

  /* Un-warped text: one Konva.Text per line, no auto-wrap (line breaks in the
     string are the only breaks the doc has). */
  function buildPlainText(l, bw, bh, fontSize, lineH, tracking) {
    const g = new Konva.Group();
    const lines = String(l.text ?? '').split('\n');
    const y0 = (bh - lines.length * lineH) / 2;
    lines.forEach((line, i) => {
      g.add(new Konva.Text({
        text: line, x: 0, y: y0 + i * lineH, width: bw, height: lineH,
        fontSize, fontFamily: l.family || 'serif', fontStyle: konvaStyle(l.style),
        fill: l.color || '#ffffff', align: l.align || 'center', verticalAlign: 'middle',
        letterSpacing: tracking, lineHeight: 1, wrap: 'none', listening: false,
      }));
    });
    return g;
  }

  /* Warped text is placed a character at a time. `amount` is the whole knob:
       arc / arch — a circular baseline, sweep = |amount| · 135°. arc rotates
                    each glyph to the tangent; arch keeps them upright (that is
                    the distinction the two names carry in every type tool).
       flag       — one sine period across the line, glyphs tilted to the slope.
       bulge      — per-glyph scale peaking mid-line, advances scaled with it. */
  function buildWarpedText(l, bw, bh, fontSize, lineH, tracking) {
    const g = new Konva.Group();
    const kind = l.warp.kind;
    const amount = l.warp.amount || 0;
    const lines = String(l.text ?? '').split('\n');
    const y0 = (bh - lines.length * lineH) / 2;
    measureCtx.font = fontString(l, fontSize);

    lines.forEach((line, li) => {
      const chars = Array.from(line);
      if (!chars.length) return;
      const midY = y0 + li * lineH + lineH / 2;
      const adv = chars.map((c) => measureCtx.measureText(c).width + tracking);
      const widths = chars.map((c) => measureCtx.measureText(c).width);
      const rawW = adv.reduce((a, b) => a + b, 0) - tracking;

      // Bulge changes advances, so its metrics are computed up front.
      let scales = chars.map(() => 1);
      let advance = adv.slice();
      if (kind === 'bulge') {
        let run = 0;
        chars.forEach((c, i) => {
          const t = rawW > 0 ? (run + widths[i] / 2) / rawW : 0.5;
          scales[i] = 1 + amount * 0.55 * Math.cos(Math.PI * (t - 0.5));
          run += adv[i];
        });
        advance = adv.map((a, i) => a * scales[i]);
      }
      const lineW = advance.reduce((a, b) => a + b, 0) - tracking;
      const align = l.align || 'center';
      const xStart = align === 'left' ? 0 : (align === 'right' ? bw - lineW : (bw - lineW) / 2);

      const theta = Math.abs(amount) * Math.PI * 0.75;
      const sgn = amount >= 0 ? 1 : -1;
      const R = theta > 1e-3 ? lineW / theta : Infinity;

      let run = 0;
      chars.forEach((c, i) => {
        const s = run + advance[i] / 2;
        run += advance[i];
        if (c === ' ') return;
        const t = lineW > 0 ? s / lineW : 0.5;
        let px = s; let py = 0; let rot = 0;
        if ((kind === 'arc' || kind === 'arch') && R !== Infinity) {
          const phi = (s - lineW / 2) / R;
          px = lineW / 2 + R * Math.sin(phi);
          py = sgn * (R - R * Math.cos(phi));
          rot = kind === 'arc' ? (sgn * phi) / DEG : 0;
        } else if (kind === 'flag') {
          const amp = amount * fontSize * 0.45;
          py = amp * Math.sin(2 * Math.PI * t);
          const slope = lineW > 0 ? amp * 2 * Math.PI * Math.cos(2 * Math.PI * t) / lineW : 0;
          rot = Math.atan(slope) / DEG;
        }
        const node = new Konva.Text({
          text: c, fontSize, fontFamily: l.family || 'serif', fontStyle: konvaStyle(l.style),
          fill: l.color || '#ffffff', lineHeight: 1, listening: false,
          x: xStart + px, y: midY + py, rotation: rot,
          scaleX: scales[i], scaleY: scales[i],
        });
        node.offsetX(node.width() / 2);
        node.offsetY(node.height() / 2);
        g.add(node);
      });
    });
    return g;
  }

  function buildText(l, bw, bh) {
    const fontSize = Math.max(1, (l.size || 0.05) * H());
    const lineH = fontSize * (l.line_height || 1.15);
    const tracking = (l.tracking || 0) * fontSize;
    const warped = l.warp && l.warp.kind && l.warp.kind !== 'none' && (l.warp.amount || 0) !== 0;
    return warped
      ? buildWarpedText(l, bw, bh, fontSize, lineH, tracking)
      : buildPlainText(l, bw, bh, fontSize, lineH, tracking);
  }

  function buildScrim(l, bw, bh) {
    const g = new Konva.Group();
    const grad = l.gradient || { angle: 90, stops: [{ at: 0, alpha: 1 }, { at: 1, alpha: 0 }] };
    const th = (grad.angle || 0) * DEG;
    const dx = Math.cos(th); const dy = Math.sin(th);
    // Span the box along the gradient direction so stop 0 and 1 sit on its edges.
    const len = Math.abs(bw * dx) + Math.abs(bh * dy);
    const stops = [];
    (grad.stops || []).slice().sort((a, b) => a.at - b.at).forEach((s) => {
      stops.push(s.at, hexToRGBA(l.color || '#000000', s.alpha));
    });
    if (stops.length < 4) { stops.length = 0; stops.push(0, hexToRGBA(l.color, 1), 1, hexToRGBA(l.color, 0)); }
    g.add(new Konva.Rect({
      width: bw, height: bh,
      fillLinearGradientStartPoint: { x: bw / 2 - dx * len / 2, y: bh / 2 - dy * len / 2 },
      fillLinearGradientEndPoint: { x: bw / 2 + dx * len / 2, y: bh / 2 + dy * len / 2 },
      fillLinearGradientColorStops: stops,
    }));
    return g;
  }

  /* Ornament frames. `inset` is read as a fraction of the box's SHORT side so a
     margin stays visually even on a tall box; stroke_w is a canvas-width
     fraction, as the doc says.

     No hit pad here, unlike art and text: a bezel is mostly a hole, and a pad
     across its box would make a full-cover ornament swallow every click on the
     art beneath it. The rules carry a fat hitStrokeWidth instead, so even a
     hairline stays easy to grab. */
  function buildFrame(l, bw, bh) {
    const g = new Konva.Group();
    const sw = Math.max(0.4, (l.stroke_w || 0.002) * W());
    const grab = Math.max(sw * 3, W() * 0.012);
    const ins = (l.inset || 0) * Math.min(bw, bh);
    const stroke = l.stroke || '#ffffff';
    const x = ins; const y = ins; const w = Math.max(1, bw - 2 * ins); const h = Math.max(1, bh - 2 * ins);
    const preset = l.preset || 'single_rule';

    if (preset === 'corner_serifs') {
      const arm = Math.min(w, h) * 0.16;
      const corners = [
        [[x, y + arm], [x, y], [x + arm, y]],
        [[x + w - arm, y], [x + w, y], [x + w, y + arm]],
        [[x + w, y + h - arm], [x + w, y + h], [x + w - arm, y + h]],
        [[x + arm, y + h], [x, y + h], [x, y + h - arm]],
      ];
      corners.forEach((pts) => g.add(new Konva.Line({
        points: pts.flat(), stroke, strokeWidth: sw, lineCap: 'square', lineJoin: 'miter',
        hitStrokeWidth: grab, fillEnabled: false,
      })));
    } else {
      g.add(new Konva.Rect({
        x, y, width: w, height: h, stroke, strokeWidth: sw,
        // fillEnabled matters even with fill:null — Konva's HIT canvas fills
        // regardless of the scene fill, which would turn an empty bezel into a
        // solid click-blocker over the art.
        fill: l.fill || null, fillEnabled: !!l.fill, hitStrokeWidth: grab,
      }));
      if (preset === 'double_rule' || preset === 'inset_panel') {
        const gap = preset === 'double_rule' ? Math.max(sw * 3, Math.min(w, h) * 0.022) : sw * 2.6;
        g.add(new Konva.Rect({
          x: x + gap, y: y + gap, width: Math.max(1, w - 2 * gap), height: Math.max(1, h - 2 * gap),
          stroke, strokeWidth: preset === 'double_rule' ? sw * 0.6 : sw * 0.5,
          opacity: preset === 'inset_panel' ? 0.6 : 1, listening: false, fillEnabled: false,
        }));
      }
    }
    return g;
  }

  /* Same rule as the ornament: an unfilled shape is a hole, so the shape
     itself is the hit area rather than its whole box. */
  function buildShape(l, bw, bh) {
    const g = new Konva.Group();
    const sw = (l.stroke_w || 0) * W();
    const common = {
      fill: l.fill || null, stroke: l.stroke || null, strokeWidth: sw,
      fillEnabled: !!l.fill, hitStrokeWidth: Math.max(sw * 3, W() * 0.012),
      strokeScaleEnabled: false,
    };
    if (l.shape === 'ellipse') {
      g.add(new Konva.Ellipse(Object.assign({ x: bw / 2, y: bh / 2, radiusX: bw / 2, radiusY: bh / 2 }, common)));
    } else {
      g.add(new Konva.Rect(Object.assign({
        width: bw, height: bh, cornerRadius: (l.radius || 0) * Math.min(bw, bh),
      }, common)));
    }
    return g;
  }

  /* Effects. drop_shadow maps straight onto Konva's shadow props; a stack of
     them needs one ghost copy of the content per extra shadow, since a Shape
     carries exactly one. bevel is approximated as an emboss — a light ghost
     up-left and a dark one down-right — which is all §4 asks of it. */
  function applyEffects(l, build) {
    const body = build();
    const shadows = (l.effects || []).filter((e) => e && e.type === 'drop_shadow');
    const bevel = (l.effects || []).find((e) => e && e.type === 'bevel');
    if (!shadows.length && !bevel) return [body];

    const out = [];
    const ghost = (shadow) => {
      const g = build();
      g.listening(false);
      setShadow(g, shadow);
      return g;
    };
    if (bevel) {
      const d = Math.max(0.5, Math.abs(bevel.params?.depth || 0.3) * 0.006 * W());
      out.push(ghost({ dxPx: -d, dyPx: -d, blurPx: d, color: '#ffffff', alpha: 0.65 }));
      out.push(ghost({ dxPx: d, dyPx: d, blurPx: d, color: '#000000', alpha: 0.65 }));
    }
    shadows.slice(0, -1).forEach((s) => out.push(ghost(s.params || {})));
    if (shadows.length) setShadow(body, shadows[shadows.length - 1].params || {});
    out.push(body);
    return out;
  }

  function setShadow(node, p) {
    const w = W();
    const dx = p.dxPx !== undefined ? p.dxPx : (p.dx || 0) * w;
    const dy = p.dyPx !== undefined ? p.dyPx : (p.dy || 0) * w;
    const blur = p.blurPx !== undefined ? p.blurPx : (p.blur || 0) * w;
    eachShape(node, (s) => {
      s.shadowColor(p.color || '#000000');
      s.shadowBlur(blur);
      s.shadowOffset({ x: dx, y: dy });
      s.shadowOpacity(p.alpha === undefined ? 0.5 : p.alpha);
      s.shadowForStrokeEnabled(false);
    });
  }


  /* ------------------------------------------ masks & adjust layers (in-engine)

     These need the view: a node's own pixels have to be read back in COVER
     coordinates, and everything on stage is inside `world`, which the pan and
     zoom transform. `raster` is the one place that is untangled — crop at the
     content's absolute origin and undo the zoom with pixelRatio — so every
     field below is canvas-sized whatever the view is doing. */

  function raster(node) {
    const t = content.getAbsoluteTransform();
    const origin = t.point({ x: 0, y: 0 });
    const z = t.decompose().scaleX || 1;
    const out = scratch(W(), H());
    out.getContext('2d').drawImage(node.toCanvas({
      x: origin.x, y: origin.y, width: W() * z, height: H() * z,
      pixelRatio: 1 / z,
    }), 0, 0, W(), H());
    return out;
  }

  /* One layer's mask as a canvas-sized alpha field, or null. Sources multiply
     and `invert` is last (resolve_mask). A source naming a layer that is not
     on stage resolves to nothing rather than throwing: the document model
     refuses that arrangement, so it can only mean a client got ahead of a
     save, and an unmasked plate for one frame beats a dead canvas. */
  function maskField(l) {
    const m = l.mask;
    if (!m) return null;
    let field = null;
    [[m.from_layer, 'stencil'], [m.luminance_of, 'luminance']].forEach(
      ([ref, mode]) => {
        if (!ref) return;
        const entry = nodeIndex.get(ref);
        if (!entry) return;
        field = foldField(field, layerField(raster(entry.group), mode));
      });
    if (m.gradient) field = foldField(field, gradientField(m.gradient, W(), H()));
    if (!field) return null;
    return m.invert ? invertField(field) : field;
  }

  /* A canvas-space field seen from inside a cached node.

     The mask is painted in cover coordinates and the node it clips has its
     own translation, rotation and flips — so rather than transforming every
     pixel, the field is DRAWN into the node's cache rect under the inverse of
     the node's own matrix. The browser does the resampling; this only has to
     get the matrix right.

     `pw`/`ph` are the cache BITMAP's pixel size, which is the rect times
     Konva's cache pixelRatio — 2 on a retina display. They are passed in
     rather than derived from the rect because the two are not the same
     number, and assuming they were is a mask that lines up on one machine
     and shreds the layer on another. */
  function fieldInLocal(field, node, rect, pw, ph) {
    const local = scratch(pw, ph);
    const ctx = local.getContext('2d');
    const inv = node.getAbsoluteTransform(content).copy().invert().getMatrix();
    ctx.scale(pw / rect.width, ph / rect.height);
    ctx.translate(-rect.x, -rect.y);
    ctx.transform(inv[0], inv[1], inv[2], inv[3], inv[4], inv[5]);
    ctx.drawImage(field, 0, 0);
    return ctx.getImageData(0, 0, local.width, local.height).data;
  }

  /* The Konva filter a masked layer wears: multiply alpha by the field.
     apply_mask's semantics exactly — the layer's own soft edges survive
     wherever the mask lets them through.

     The field is built on the FIRST call, not here, because only the filter
     is told how big the cache bitmap actually is (imageData's own size). It
     is then kept: Konva re-runs filters on redraw, and re-projecting the
     mask every frame would be the expensive half of a cheap operation. */
  function maskFilter(field, node, rect) {
    let local = null;
    return function maskAlpha(imageData) {
      if (!local || local.length !== imageData.data.length) {
        local = fieldInLocal(field, node, rect,
                             imageData.width, imageData.height);
      }
      const d = imageData.data;
      for (let i = 0; i < d.length; i += 4) {
        d[i + 3] = (d[i + 3] * local[i + 3]) / 255;
      }
    };
  }

  /* An adjust layer as a node drawn over everything below it (§15.3).

     `color_wash` needs no readback: a solid rect through a CSS blend mode is
     the same "composite a fill AS a layer through the blend table" that
     apply_adjust branches to. Every other op reads the composite, so it takes
     one raster of what is already on the canvas, rewrites the pixels, and is
     drawn back over the top through the scope — which is the mix
     Image.composite(op_result, base, m * opacity) performs. */
  function buildAdjust(l) {
    const { bw, bh, cx, cy } = boxOf(l);
    /* Mid-drag, the grade is a translucent stand-in over its own box. The
       real one is a raster of everything below it, and a raster dragged
       around the canvas would slide its CONTENT with it — showing the cover
       smeared rather than the region moving. Same rule as levels and masks:
       the honest preview during a drag is the one that does not lie about
       what is underneath. Released, setInteracting re-renders it for real. */
    if (interacting) {
      return new Konva.Rect({
        x: 0, y: 0, width: bw, height: bh, fill: 'rgba(120,140,190,0.28)',
        listening: false,
      });
    }
    /* Opacity is NOT set on these nodes: buildLayer already put it on the
       group they go into, and setting it here too would square it. The §15.3
       equation's `opacity` is that same group opacity — the node carries the
       op's full-strength result and the group mixes it back. */
    let scope = maskField(l);
    const box = boxField(cx, cy, bw, bh, W(), H());
    if (box) scope = scope ? foldField(scope, box) : box;

    if (l.op === 'color_wash') {
      const wash = new Konva.Rect({
        x: 0, y: 0, width: W(), height: H(),
        fill: l.color || '#000000', listening: false,
        globalCompositeOperation: BLEND_CSS[l.blend] || 'source-over',
      });
      if (!scope) return wash;
      const c = scratch(W(), H());
      const ctx = c.getContext('2d');
      ctx.fillStyle = l.color || '#000000';
      ctx.fillRect(0, 0, W(), H());
      foldField(c, scope);
      return new Konva.Image({
        x: 0, y: 0, image: c, width: W(), height: H(), opacity,
        listening: false,
        globalCompositeOperation: BLEND_CSS[l.blend] || 'source-over',
      });
    }

    const under = raster(content);
    const ctx = under.getContext('2d');
    if (l.op === 'bloom' || l.op === 'blur') {
      // Canvas's filter blur radius IS a Gaussian standard deviation, the
      // same number PIL's GaussianBlur takes, and `radius` is a fraction of
      // canvas HEIGHT in both.
      const px = (l.radius === undefined ? 0.02 : l.radius) * H();
      if (l.op === 'blur') {
        const soft = scratch(W(), H());
        const sctx = soft.getContext('2d');
        sctx.filter = `blur(${px}px)`;
        sctx.drawImage(under, 0, 0);
        ctx.clearRect(0, 0, W(), H());
        ctx.drawImage(soft, 0, 0);
      } else {
        // Keep only what clears the threshold, blur that, scale by strength,
        // and SCREEN it back — a composite with nothing bright enough blooms
        // into nothing, because screening black is the identity.
        const cut = Math.round((l.threshold === undefined ? 0.75 : l.threshold) * 255);
        const strength = l.strength === undefined ? 0.5 : l.strength;
        const glow = scratch(W(), H());
        const gctx = glow.getContext('2d');
        gctx.drawImage(under, 0, 0);
        const gi = gctx.getImageData(0, 0, W(), H());
        const gd = gi.data;
        for (let i = 0; i < gd.length; i += 4) {
          const v = wcagLuma(gd, i);
          const keep = (v >= cut ? v : 0) * strength;
          gd[i] = keep; gd[i + 1] = keep; gd[i + 2] = keep; gd[i + 3] = 255;
        }
        gctx.putImageData(gi, 0, 0);
        const soft = scratch(W(), H());
        const sctx = soft.getContext('2d');
        sctx.filter = `blur(${px}px)`;
        sctx.drawImage(glow, 0, 0);
        ctx.globalCompositeOperation = 'screen';
        ctx.drawImage(soft, 0, 0);
        ctx.globalCompositeOperation = 'source-over';
      }
    } else {
      const img = ctx.getImageData(0, 0, W(), H());
      if (l.op === 'grade') gradePixels(img.data, l);
      else if (l.op === 'gradient_map') gradientMapPixels(img.data, l);
      else if (l.op === 'vignette') vignettePixels(img.data, l, W(), H());
      ctx.putImageData(img, 0, 0);
    }
    if (scope) foldField(under, scope);
    return new Konva.Image({
      x: 0, y: 0, image: under, width: W(), height: H(), listening: false,
    });
  }

  function buildLayer(l) {
    const { bw, bh, cx, cy } = boxOf(l);
    const group = new Konva.Group({
      x: cx, y: cy, rotation: l.frame.rotation || 0,
      opacity: l.opacity === undefined ? 1 : l.opacity,
      visible: l.visible !== false,
      listening: l.visible !== false && !l.locked,
      draggable: !l.locked,
    });
    group.setAttr('layerId', l.id);
    // Flip lives on an inner group so the Transformer only ever writes scale
    // onto the outer one — negative scale and a rotate handle do not mix.
    const flip = new Konva.Group({ scaleX: l.frame.flip_h ? -1 : 1, scaleY: l.frame.flip_v ? -1 : 1 });
    group.add(flip);

    let art = null;
    const build = () => {
      /* An adjust layer's node is painted in COVER coordinates — it grades
         the whole canvas and is scoped by its own box, rather than drawing
         inside one — so it is the one kind whose body is not offset to its
         box corner, and the group it sits in is put back at the origin. */
      const body = new Konva.Group((l.kind === 'adjust' && !interacting)
        ? { x: -cx, y: -cy }
        : { x: -bw / 2, y: -bh / 2 });
      let made;
      // Ghost copies are built first and the real body last, so the art node
      // we keep for mask mapping is the one actually on screen.
      if (l.kind === 'art') { made = buildArt(l, bw, bh); if (made.art) art = made.art; made = made.node; }
      else if (l.kind === 'text') made = buildText(l, bw, bh);
      else if (l.kind === 'scrim') made = buildScrim(l, bw, bh);
      else if (l.kind === 'frame') made = buildFrame(l, bw, bh);
      else if (l.kind === 'adjust') made = buildAdjust(l);
      else made = buildShape(l, bw, bh);
      body.add(made);
      return body;
    };

    /* Shadows are skipped on a pinned plate, and not only to save work:
       Konva sets the shadow on the context BEFORE calling a sceneFunc, so
       every one of the mesh's 144 drawImage calls would cast its own — a
       grid of shadows rather than one silhouette. A pin is a geometry tool;
       plan the shadow before pinning, or unpin to place it. */
    const pinned = l.kind === 'art' && !!cornersOf(l);
    (pinned ? [build()] : applyEffects(l, build)).forEach((n) => flip.add(n));

    /* Pixel filters need a cached bitmap, and caching is the expensive part —
       so it happens only for a layer that actually carries `levels`, and
       never while a hand is on a control (see setInteracting: a slider
       preview re-renders on every input event, and re-caching a full-bleed
       plate at each one is the difference between 60fps and a slideshow).
       The cache is taken in world coordinates at 1:1, which is exact at 100%
       zoom; composite() re-takes it at the export's own ratio. */
    /* Masks ride the same cache as levels, and under the same rule: a cached
       node cannot follow a drag, so both are dropped while a hand is on a
       control and taken back on release (setInteracting). A masked layer
       therefore shows UNMASKED mid-drag, which is the honest preview — the
       mask is canvas-space and does not travel with the layer, so what moves
       under it is exactly what the person is aiming.

       The filters are composed in the document's own order: levels grade the
       layer's pixels, then the mask decides how much of them exists. */
    const levels = levelsOf(l);
    const field = (l.mask && l.kind !== 'adjust' && !interacting)
      ? maskField(l) : null;
    if ((levels || field) && !interacting) {
      /* A levels-only layer keeps Konva's automatic cache rect, which pads
         for shadows and strokes. A masked one has to pass an explicit rect,
         because the field is painted into that exact rect and a cache Konva
         sized for itself would be off by the padding. */
      let rect = null;
      if (field) {
        const r = flip.getClientRect({ relativeTo: flip });
        rect = { x: Math.floor(r.x), y: Math.floor(r.y),
                 width: Math.max(1, Math.ceil(r.width)),
                 height: Math.max(1, Math.ceil(r.height)) };
        flip.cache(rect);
      } else {
        flip.cache();
      }
      const filters = [];
      if (levels) {
        filters.push(Konva.Filters.Brighten, Konva.Filters.Contrast);
        flip.brightness(levels.brightness);
        flip.contrast(levels.contrast);
      }
      if (field) filters.push(maskFilter(field, flip, rect));
      flip.filters(filters);
      filtered.push(flip);
    }

    /* The transparent grab pad, so the whole box is clickable and not just
       the opaque pixels of a cutout plate. It sits OUTSIDE `flip` on purpose:
       a cached node answers hit tests from its cached hit canvas, where a
       0.001-alpha rectangle is not a hit — so a levels effect would quietly
       make its own layer unselectable. */
    if (l.kind === 'art' || l.kind === 'text' || l.kind === 'adjust') {
      group.add(new Konva.Rect({
        x: -bw / 2, y: -bh / 2, width: bw, height: bh, fill: 'rgba(0,0,0,0.001)',
      }));
    }
    return { group, art };
  }

  function render() {
    const d = doc();
    paper.width(W()); paper.height(H());
    content.destroyChildren();
    nodeIndex = new Map();
    filtered = [];
    (d.layers || []).forEach((l) => {
      const built = buildLayer(l);
      wireDrag(built.group, l.id);
      content.add(built.group);
      nodeIndex.set(l.id, built);
    });
    syncTransformer();
    syncPin();
    syncGuides();
    sceneLayer.batchDraw();
    overlay.batchDraw();
  }

  function wireDrag(group, id) {
    group.on('dragstart', () => {
      const l = doc().layers.find((x) => x.id === id);
      dragPrior = l
        ? { x: l.frame.x, y: l.frame.y, corners: JSON.parse(JSON.stringify(cornersOf(l))) }
        : null;
      if (selectedId !== id) select(id);
    });
    /* The snap rides the existing drag: it only ever moves the Konva node, so
       the commit below still reads the node's own position and one drag is
       still one op. A pinned plate is left alone — its four corners are the
       geometry, and pulling the box to a fold would slide them off the pixels
       they were placed on. */
    group.on('dragmove', (e) => {
      const l = doc().layers.find((x) => x.id === id);
      if (l) snapDrag(group, l, e.evt);
    });
    group.on('dragend', () => {
      clearSnapHint();
      if (!dragPrior) return;
      const prior = dragPrior;
      dragPrior = null;
      const op = { op: 'set_frame', layer_id: id, x: group.x() / W(), y: group.y() / H() };
      /* A pin is stated in absolute canvas fractions, so moving the box has
         to move it too — otherwise dragging a pinned plate would slide an
         empty rectangle out from under pixels that stayed put. Same op, so
         it is still one undo step. */
      if (prior.corners) {
        const dx = op.x - prior.x; const dy = op.y - prior.y;
        op.corners = prior.corners.map(([u, v]) => [clampFrac(u + dx), clampFrac(v + dy)]);
      }
      // One drag is one undo step: the ops layer only hears about it here.
      onCommit([op]);
    });
  }

  const toScreen = (x, y) => ({
    x: world.x() + x * world.scaleX(), y: world.y() + y * world.scaleY(),
  });
  const toWorld = (x, y) => ({
    x: (x - world.x()) / world.scaleX(), y: (y - world.y()) / world.scaleY(),
  });

  /* The selected layer, if it is pinned and reachable — the one condition
     every pin handler shares. */
  function pinnedLayer() {
    const l = selectedId ? doc().layers.find((x) => x.id === selectedId) : null;
    if (!l || l.locked || l.visible === false || !cornersOf(l)) return null;
    return l;
  }

  /* Handles and the dashed quad, from the doc. `skip` is the handle a drag is
     currently holding: repositioning it mid-drag would fight the pointer. */
  function syncPin(skip = -1) {
    const l = pinnedLayer();
    const c = l && cornersOf(l);
    pinQuad.visible(!!c);
    if (!c) { pinHandles.forEach((h) => h.visible(false)); return; }
    const pts = c.map(([u, v]) => toScreen(u * W(), v * H()));
    pinQuad.points(pts.flatMap((p) => [p.x, p.y]));
    pinHandles.forEach((h, i) => {
      h.visible(true);
      if (i !== skip) h.position(pts[i]);
    });
  }

  pinHandles.forEach((h, i) => {
    h.on('dragstart', () => {
      const l = pinnedLayer();
      if (!l) return;
      pinPrior = JSON.parse(JSON.stringify(l.frame.corners));
      // Safe to re-render here, unlike a layer drag: the node under the
      // pointer lives in the overlay, so destroying the scene can't break it.
      setInteracting(true);
    });
    h.on('dragmove', () => {
      const l = pinnedLayer();
      if (!l || !pinPrior) return;
      const p = toWorld(h.x(), h.y());
      // Preview by mutating the live doc, exactly as a slider does; the
      // sceneFunc reads it fresh, so the mesh warps under the pointer.
      l.frame.corners[i] = [clampFrac(p.x / W()), clampFrac(p.y / H())];
      syncPin(i);
      sceneLayer.batchDraw();
      overlay.batchDraw();
    });
    h.on('dragend', () => {
      const l = pinnedLayer();
      if (!l || !pinPrior) return;
      const next = JSON.parse(JSON.stringify(l.frame.corners));
      // Rewind the preview so the op carries the whole move and its inverse
      // is the corners the drag STARTED from — one drag, one undo step.
      l.frame.corners = pinPrior;
      pinPrior = null;
      onCommit([{ op: 'set_frame', layer_id: l.id, corners: next }]);
      setInteracting(false);
    });
  });

  /* The fold lines, the trim boxes, the bleed edge and the dashed safe boxes,
     drawn from `doc.wrap` through the one panels() mirror (wrap.js).

     Two caches, because the overlay lives in SCREEN space and therefore has to
     be re-pointed on every pan frame: the NODES are rebuilt only when the wrap
     itself changes (guideKey), and each sync just walks their stored fractions
     through the current view. A wrap has about twenty guide lines; rebuilding
     twenty Konva nodes per mousemove would be the expensive way to draw the
     cheapest thing on screen. */
  function rect(x0, y0, x1, y1) {
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
  }

  function buildGuides(p) {
    guides.destroyChildren();
    guideNodes = [];
    if (!p) return;
    const line = (pts, kind, closed) => {
      const style = GUIDE_STYLE[kind];
      const node = new Konva.Line({
        stroke: style.stroke, strokeWidth: style.width, dash: style.dash || undefined,
        closed: !!closed, listening: false,
      });
      guides.add(node);
      guideNodes.push({ node, pts });
    };

    // The sheet's own edge: where the bleed ends and the guillotine has been.
    line(rect(0, 0, 1, 1), 'bleed', true);
    // One trim box per panel — the page as it is cut.
    PANEL_NAMES.forEach((name) => line(rect(p[name].x0, p[name].y0, p[name].x1, p[name].y1), 'trim', true));
    // The two folds, run the whole height of the sheet: they are the edges of
    // the spine, and the one pair of lines that has to be visible in the bleed.
    line([[p.spine.x0, 0], [p.spine.x0, 1]], 'fold');
    line([[p.spine.x1, 0], [p.spine.x1, 1]], 'fold');
    // Safe boxes, dashed. A spine narrow enough that its two safe insets cross
    // gets none rather than an inside-out rectangle.
    PANEL_NAMES.forEach((name) => {
      const q = p[name];
      const x0 = q.x0 + p.safe.x; const x1 = q.x1 - p.safe.x;
      const y0 = q.y0 + p.safe.y; const y1 = q.y1 - p.safe.y;
      if (x1 > x0 && y1 > y0) line(rect(x0, y0, x1, y1), 'safe', true);
    });
    // Which panel is which, said out loud at the top of each one.
    PANEL_NAMES.forEach((name) => {
      const q = p[name];
      const node = new Konva.Text({
        text: name.toUpperCase(), fontSize: 10, fontFamily: 'monospace',
        letterSpacing: 1.5, fill: 'rgba(239,228,203,.45)', listening: false,
      });
      guides.add(node);
      // Anchored to the panel's top trim corner; the offset below nudges the
      // label just inside it, in screen pixels so it never scales away.
      guideNodes.push({ node, pts: [[(q.x0 + q.x1) / 2, q.y0]], label: true });
    });
  }

  function syncGuides() {
    const wrap = doc().wrap;
    const key = wrapKey(wrap);
    if (key !== guideKey) {
      guideKey = key;
      const p = wrap ? panels(wrap) : null;
      buildGuides(p);
      snapCache = p ? { key, lines: snapLines(p) } : null;
    }
    const on = guidesVisible && !!wrap;
    guides.visible(on);
    if (!on) return;
    guideNodes.forEach((g) => {
      if (g.label) {
        const at = toScreen(g.pts[0][0] * W(), g.pts[0][1] * H());
        g.node.position({ x: at.x - g.node.width() / 2, y: at.y + 5 });
        return;
      }
      g.node.points(g.pts.flatMap(([x, y]) => {
        const at = toScreen(x * W(), y * H());
        return [at.x, at.y];
      }));
    });
  }

  function setGuidesVisible(on) {
    guidesVisible = !!on;
    syncGuides();
    overlay.batchDraw();
  }

  /* One axis of a snap: the dragged box's CENTRE and its two EDGES are offered
     to the lines that accept each, and the nearest catch inside the tolerance
     wins. Returns the centre the layer should take, in world pixels, plus the
     line it caught so the overlay can flash it. */
  function snapAxis(center, half, edges, centres, span, tol) {
    let best = null;
    const consider = (candidate, at) => {
      const d = Math.abs(candidate - center);
      if (d <= tol && (!best || d < best.d)) best = { d, value: candidate, at };
    };
    centres.forEach((c) => consider(c.at * span, c.at));
    edges.forEach((c) => {
      consider(c.at * span + half, c.at);   // the box's LEFT/TOP edge on the line
      consider(c.at * span - half, c.at);   // its RIGHT/BOTTOM edge on the line
    });
    return best;
  }

  /* Called on every drag frame of an unpinned layer on a wrapped document.
     Alt suppresses it — the standard "let me put it exactly where I said". */
  function snapDrag(group, l, evt) {
    // No wrap, Alt held, or a pinned plate: nothing to catch on, and the drag
    // is left exactly as the pointer states it.
    if (!snapCache || (evt && evt.altKey) || cornersOf(l)) { clearSnapHint(); return; }
    const { edges, centres } = snapCache.lines;
    const tol = SNAP_PX / world.scaleX();
    const hitX = snapAxis(group.x(), Math.abs(l.frame.w) * W() / 2,
      edges.xs, centres.xs, W(), tol);
    const hitY = snapAxis(group.y(), Math.abs(l.frame.h) * H() / 2,
      edges.ys, centres.ys, H(), tol);
    if (hitX) group.x(hitX.value);
    if (hitY) group.y(hitY.value);
    const top = toScreen(0, 0);
    const bottom = toScreen(W(), H());
    snapHintX.visible(!!hitX);
    if (hitX) {
      const at = toScreen(hitX.at * W(), 0).x;
      snapHintX.points([at, top.y, at, bottom.y]);
    }
    snapHintY.visible(!!hitY);
    if (hitY) {
      const at = toScreen(0, hitY.at * H()).y;
      snapHintY.points([top.x, at, bottom.x, at]);
    }
    snapHint.visible(!!(hitX || hitY));
    overlay.batchDraw();
  }

  function clearSnapHint() {
    if (!snapHint.visible()) return;
    snapHint.visible(false);
    overlay.batchDraw();
  }

  /* Filters are dropped while a hand is on a control and taken back when it
     lets go. Never call this from a scene-node drag handler — it re-renders,
     and re-rendering destroys the node Konva is dragging. */
  function setInteracting(on) {
    if (interacting === !!on) return;
    interacting = !!on;
    if (filtered.length || !interacting) render();
  }

  function syncTransformer() {
    const entry = selectedId ? nodeIndex.get(selectedId) : null;
    const l = selectedId ? doc().layers.find((x) => x.id === selectedId) : null;
    if (!entry || !l || l.locked || l.visible === false) { tr.nodes([]); tr.visible(false); return; }
    // While a layer is pinned its four corners ARE its transform handles;
    // a scale box over the top of them would offer two ways to say the same
    // thing and disagree about the answer.
    if (cornersOf(l)) { tr.nodes([]); tr.visible(false); return; }
    tr.nodes([entry.group]);
    tr.visible(true);
    /* Anchors are a fixed 9px on screen, so on a small layer — or any layer at
       a far-out zoom — they cover the thing they are framing, and a drag meant
       to MOVE it silently resizes it instead. Below that threshold the
       transformer becomes a plain border: move and nudge still work, and the
       properties panel still has exact numbers. */
    const box = entry.group.getClientRect({ skipShadow: true, skipStroke: true });
    const roomy = box.width > ANCHOR * 3.5 && box.height > ANCHOR * 3.5;
    tr.resizeEnabled(roomy);
    tr.rotateEnabled(roomy);
  }

  tr.on('transformend', () => {
    const id = selectedId;
    const entry = id && nodeIndex.get(id);
    const l = id && doc().layers.find((x) => x.id === id);
    if (!entry || !l) return;
    const g = entry.group;
    const sx = g.scaleX(); const sy = g.scaleY();
    if (!Number.isFinite(sx) || !Number.isFinite(sy) || sx === 0 || sy === 0) {
      g.scale({ x: 1, y: 1 });
      return;
    }
    const ops = [{
      op: 'set_frame', layer_id: id,
      x: g.x() / W(), y: g.y() / H(),
      w: Math.max(0.002, Math.abs(l.frame.w * sx)),
      h: Math.max(0.002, Math.abs(l.frame.h * sy)),
      rotation: g.rotation(),
      flip_h: sx < 0 ? !l.frame.flip_h : !!l.frame.flip_h,
      flip_v: sy < 0 ? !l.frame.flip_v : !!l.frame.flip_v,
    }];
    // Type scales with its box — dragging a corner of a title should make the
    // title bigger, not stretch an invisible frame around unchanged letters.
    if (l.kind === 'text' && Math.abs(sy - 1) > 1e-4) {
      ops.push({ op: 'set_text', layer_id: id, size: Math.max(0.004, l.size * Math.abs(sy)) });
    }
    g.scale({ x: 1, y: 1 });
    onCommit(ops);
  });

  function hitLayerId(target) {
    let n = target;
    while (n && n !== stage) {
      if (n.getAttr && n.getAttr('layerId')) return n.getAttr('layerId');
      n = n.getParent();
    }
    return null;
  }

  function select(id) {
    selectedId = id;
    syncTransformer();
    syncPin();
    overlay.batchDraw();
    onSelect && onSelect(id);
  }

  stage.on('mousedown touchstart', (e) => {
    // panning.armed, not .active: the pan handler is registered after this one,
    // so on a space-drag press .active is still false when we get here.
    if (marqueeMode || panning.armed || panning.active) return;
    if (e.target === stage || e.target === paper) { select(null); return; }
    const id = hitLayerId(e.target);
    if (id && id !== selectedId) select(id);
  });

  function setView(scale, x, y) {
    const s = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, scale));
    world.scale({ x: s, y: s });
    world.position({ x, y });
    sceneLayer.batchDraw();
    syncTransformer();
    syncPin();
    syncGuides();
    tr.forceUpdate();
    overlay.batchDraw();
    onView && onView(s);
  }

  // Once the view is the user's own, the window stops re-framing it for them.
  let userView = false;

  function zoomToFit() {
    const cw = stage.width(); const ch = stage.height();
    const s = Math.min(cw / W(), ch / H()) * 0.92;
    userView = false;
    setView(s, (cw - W() * s) / 2, (ch - H() * s) / 2);
  }

  function zoomBy(factor, pivot) {
    userView = true;
    const old = world.scaleX();
    const next = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, old * factor));
    const p = pivot || { x: stage.width() / 2, y: stage.height() / 2 };
    const wx = (p.x - world.x()) / old;
    const wy = (p.y - world.y()) / old;
    setView(next, p.x - wx * next, p.y - wy * next);
  }

  stage.on('wheel', (e) => {
    e.evt.preventDefault();
    userView = true;
    const pointer = stage.getPointerPosition();
    // Trackpad pinch arrives as ctrlKey+wheel; both gestures mean "zoom here".
    const factor = Math.exp(-e.evt.deltaY * (e.evt.ctrlKey ? 0.01 : 0.0022));
    zoomBy(factor, pointer);
  });

  const panning = { active: false, armed: false, last: null };
  function setPanArmed(on) {
    panning.armed = on;
    host.parentElement?.classList.toggle('is-panning', on);
  }
  stage.on('mousedown', (e) => {
    if (!panning.armed && e.evt.button !== 1) return;
    e.evt.preventDefault();
    userView = true;
    panning.active = true;
    panning.last = { x: e.evt.clientX, y: e.evt.clientY };
    host.parentElement?.classList.add('is-grabbing');
  });
  window.addEventListener('mousemove', (ev) => {
    if (!panning.active) return;
    const dx = ev.clientX - panning.last.x; const dy = ev.clientY - panning.last.y;
    panning.last = { x: ev.clientX, y: ev.clientY };
    setView(world.scaleX(), world.x() + dx, world.y() + dy);
  });
  window.addEventListener('mouseup', () => {
    panning.active = false;
    host.parentElement?.classList.remove('is-grabbing');
  });

  function beginMarquee(layerId, onDone) {
    marqueeMode = { layerId, onDone, start: null };
    host.parentElement?.classList.add('is-marquee');
    content.listening(false);
  }
  function endMarquee() {
    marqueeMode = null;
    marquee.visible(false);
    host.parentElement?.classList.remove('is-marquee');
    content.listening(true);
    overlay.batchDraw();
  }

  stage.on('mousedown touchstart', (e) => {
    if (!marqueeMode) return;
    const p = stage.getPointerPosition();
    marqueeMode.start = p;
    marquee.setAttrs({ x: p.x, y: p.y, width: 0, height: 0, visible: true });
    overlay.batchDraw();
    e.evt.preventDefault();
  });
  stage.on('mousemove touchmove', () => {
    if (!marqueeMode || !marqueeMode.start) return;
    const p = stage.getPointerPosition();
    const s = marqueeMode.start;
    marquee.setAttrs({
      x: Math.min(s.x, p.x), y: Math.min(s.y, p.y),
      width: Math.abs(p.x - s.x), height: Math.abs(p.y - s.y),
    });
    overlay.batchDraw();
  });
  stage.on('mouseup touchend', () => {
    if (!marqueeMode || !marqueeMode.start) return;
    const rect = { x: marquee.x(), y: marquee.y(), width: marquee.width(), height: marquee.height() };
    const { onDone, layerId } = marqueeMode;
    marqueeMode.start = null;
    if (rect.width < 6 || rect.height < 6) { endMarquee(); return; }
    onDone(rect, layerId);
  });

  /* The inpaint mask, in the plate's OWN pixels.
     The screen rectangle is walked through the art node's inverse absolute
     transform, which folds zoom, pan, the layer's rotation, its flip, and the
     cover/contain fit into one step: the result is the marquee as a quad in
     the drawn image's local space (0..drawnW, 0..drawnH). Scaling that by
     natural/drawn puts it in plate pixels. A rotated layer therefore yields a
     rotated quad, not a bounding box — so we fill the polygon, not a rect.
     Alpha 0 marks the region to regenerate; everything else stays opaque.

     A pinned layer has no Konva.Image to invert — its pixels are drawn by a
     mesh sceneFunc — so this returns null and the shelf keeps Repair
     disabled until the pin comes off. */
  function maskFor(layerId, rect) {
    const entry = nodeIndex.get(layerId);
    if (!entry || !entry.art) return null;
    const art = entry.art;
    const el = art.image();
    const iw = el.naturalWidth || el.width;
    const ih = el.naturalHeight || el.height;
    const inv = art.getAbsoluteTransform().copy().invert();
    const sx = iw / art.width();
    const sy = ih / art.height();
    const pts = [
      { x: rect.x, y: rect.y }, { x: rect.x + rect.width, y: rect.y },
      { x: rect.x + rect.width, y: rect.y + rect.height }, { x: rect.x, y: rect.y + rect.height },
    ].map((p) => { const q = inv.point(p); return { x: q.x * sx, y: q.y * sy }; });

    const c = document.createElement('canvas');
    c.width = iw; c.height = ih;
    const ctx = c.getContext('2d');
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, iw, ih);
    ctx.globalCompositeOperation = 'destination-out';
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
    ctx.closePath();
    ctx.fill();
    return c.toDataURL('image/png').split(',')[1];
  }

  /* Both the export and the assistant's `look` want the whole cover at
     reference size, not the letterboxed viewport — so the world is momentarily
     put back to 1:1 at the origin and drawn into its own canvas. The overlay
     layer (handles, marquee) is a different layer and never appears. */
  function composite(pixelRatio, mimeType = 'image/png', quality) {
    const sx = world.scaleX(); const px = world.x(); const py = world.y();
    world.scale({ x: 1, y: 1 });
    world.position({ x: 0, y: 0 });
    /* A filtered layer draws from a bitmap cached at 1:1, so a 2× export
       would print that one layer at half the resolution of everything around
       it. Re-take the cache at the ratio being asked for, and put it back
       afterwards so the screen keeps its cheap one. */
    filtered.forEach((n) => n.cache({ pixelRatio: Math.max(0.05, pixelRatio) }));
    let url;
    try {
      url = world.toDataURL({ x: 0, y: 0, width: W(), height: H(), pixelRatio, mimeType, quality });
    } finally {
      filtered.forEach((n) => n.cache());
      world.scale({ x: sx, y: sx });
      world.position({ x: px, y: py });
      sceneLayer.batchDraw();
    }
    return url;
  }

  /* The stage is built before its host is in the document — and a pane that
     is hidden at load reports no size at all — so the first honest size
     arrives from the observer, not from boot. We keep re-framing on every
     resize until the user takes the view over with a zoom or a pan; after
     that the window can change shape all it likes and their view stays put. */
  function resize() {
    const w = host.clientWidth || 10;
    const h = host.clientHeight || 10;
    stage.width(w); stage.height(h);
    if (!userView && w > 40 && h > 40) { zoomToFit(); return; }
    syncTransformer();
    syncPin();
    syncGuides();
    sceneLayer.batchDraw(); overlay.batchDraw();
  }
  const ro = new ResizeObserver(resize);
  ro.observe(host);

  return {
    stage,
    render,
    select,
    get selectedId() { return selectedId; },
    zoomToFit,
    zoomBy,
    get zoom() { return world.scaleX(); },
    setPanArmed,
    beginMarquee,
    endMarquee,
    get marqueeActive() { return !!marqueeMode; },
    setInteracting,
    setGuidesVisible,
    get guidesVisible() { return guidesVisible; },
    isPinned: (id) => !!cornersOf(doc().layers.find((x) => x.id === id)),
    maskFor,
    exportDataURL: (pixelRatio = 2) => composite(pixelRatio),
    snapshotBase64(maxWidth = 700) {
      /* JPEG, not PNG. The server reads the format off the magic bytes
         (docproof/canvas/assistant.py _image_mime), and a cover photograph is
         roughly a tenth the size as JPEG — every turn of the conversation
         carries one, so the saving is the whole conversation's. Still bare
         base64 with no data: prefix, which is what /chat has always taken. */
      const url = composite(Math.min(1, maxWidth / W()), 'image/jpeg', 0.8);
      return url.split(',')[1];
    },
    destroy() { ro.disconnect(); stage.destroy(); },
  };
}
