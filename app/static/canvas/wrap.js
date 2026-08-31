/* The print wrap's geometry, on this side of the wire.

   MIRROR OF docproof/canvas/wrap.py::panels and docproof/canvas/model.py::Wrap
   — if the fold lines ever move over there, they move here. The server hands
   the panels back exactly once (POST /wrap answers {doc, panels}) and the
   document does NOT carry them, so everything afterwards — the guides, the
   snap lines, the PDF's physical size, the "is this layer on the spine?"
   question — has to re-derive them from `doc.wrap`. This module is the ONE
   place that does: two derivations of one fold line is one fold line too many,
   which is the same sentence the Python states about itself.

   Every fraction is SHEET-relative, like every other fraction in the document.
   `bleed` and `safe` are INSETS, not positions:

       the sheet's trim box = (bleed.x, bleed.y) … (1-bleed.x, 1-bleed.y)
       a panel's safe box   = (panel.x0 + safe.x, panel.y0 + safe.y)
                              … (panel.x1 - safe.x, panel.y1 - safe.y)   */

/* KDP's own cover guide asks for a quarter inch and Ingram's is no looser —
   wrap.py's SAFE_MARGIN_IN, and the same caveat rides along: check the
   printer's current template before a real run. */
export const SAFE_MARGIN_IN = 0.25;

/* Left to right on the flat sheet, outside face up. */
export const PANEL_NAMES = ['back', 'spine', 'front'];

/* The model's own defaults, for the two fields a hand-built doc might omit. */
const BLEED = (w) => (Number.isFinite(Number(w.bleed_in)) ? Number(w.bleed_in) : 0.125);
const DPI = (w) => (Number.isFinite(Number(w.dpi)) ? Number(w.dpi) : 300);

export function sheetInches(wrap) {
  return {
    w: 2 * wrap.trim_w_in + wrap.spine_in + 2 * BLEED(wrap),
    h: wrap.trim_h_in + 2 * BLEED(wrap),
  };
}

/* The pixel canvas the sheet IS on a wrapped document (Wrap.sheet_size),
   rounded the same way — a canvas that disagreed with its wrap is a document
   the server refuses to load. */
export function sheetSize(wrap) {
  const inches = sheetInches(wrap);
  return { w: Math.round(inches.w * DPI(wrap)), h: Math.round(inches.h * DPI(wrap)) };
}

/* Each panel's [x0, x1] in inches from the sheet's left edge. */
export function panelEdgesIn(wrap) {
  const bleed = BLEED(wrap);
  const backX1 = bleed + wrap.trim_w_in;
  const spineX1 = backX1 + wrap.spine_in;
  return {
    back: [bleed, backX1],
    spine: [backX1, spineX1],
    front: [spineX1, spineX1 + wrap.trim_w_in],
  };
}

export function trimYIn(wrap) {
  const bleed = BLEED(wrap);
  return [bleed, bleed + wrap.trim_h_in];
}

/* The whole geometry as canvas fractions — the same object shape POST /wrap
   answers with, so a caller can take either without knowing which it got. */
export function panels(wrap) {
  const inches = sheetInches(wrap);
  const size = sheetSize(wrap);
  const [top, bottom] = trimYIn(wrap);
  const edges = panelEdgesIn(wrap);
  const out = {
    sheet: { w_in: inches.w, h_in: inches.h, w_px: size.w, h_px: size.h, dpi: DPI(wrap) },
    bleed: { x: BLEED(wrap) / inches.w, y: BLEED(wrap) / inches.h, inches: BLEED(wrap) },
    safe: { x: SAFE_MARGIN_IN / inches.w, y: SAFE_MARGIN_IN / inches.h, inches: SAFE_MARGIN_IN },
  };
  PANEL_NAMES.forEach((name) => {
    const [x0, x1] = edges[name];
    out[name] = { x0: x0 / inches.w, x1: x1 / inches.w, y0: top / inches.h, y1: bottom / inches.h };
  });
  return out;
}

/* A stable signature of the sheet — what a cache of guide nodes or snap lines
   is keyed on, since nothing about them changes until one of these five does. */
export function wrapKey(wrap) {
  if (!wrap) return '';
  return [wrap.trim_w_in, wrap.trim_h_in, wrap.spine_in, BLEED(wrap), DPI(wrap)].join(':');
}

/* Which panel a point sits on, by the same rule the set_wrap remap uses: the
   centre decides, and a straddler is judged by its middle. */
export function panelAt(p, x) {
  if (x < p.spine.x0) return 'back';
  if (x > p.spine.x1) return 'front';
  return 'spine';
}

/* The lines a drag can land on, as fractions, sorted so the search is cheap.
   `edges` are what a layer's EDGE snaps to (folds, trim, safe); `centres` are
   what its CENTRE snaps to (each panel's middle). Both carry the kind, so the
   overlay can flash the line that caught. */
export function snapLines(p) {
  const xs = [
    { at: p.spine.x0, kind: 'fold' }, { at: p.spine.x1, kind: 'fold' },
    { at: p.bleed.x, kind: 'trim' }, { at: 1 - p.bleed.x, kind: 'trim' },
  ];
  const ys = [
    { at: p.bleed.y, kind: 'trim' }, { at: 1 - p.bleed.y, kind: 'trim' },
    { at: p.back.y0 + p.safe.y, kind: 'safe' }, { at: p.back.y1 - p.safe.y, kind: 'safe' },
  ];
  PANEL_NAMES.forEach((name) => {
    const q = p[name];
    // A spine narrow enough that its two safe insets cross has no safe box —
    // don't offer a snap line that is on the wrong side of itself.
    if (q.x1 - q.x0 > 2 * p.safe.x) {
      xs.push({ at: q.x0 + p.safe.x, kind: 'safe' }, { at: q.x1 - p.safe.x, kind: 'safe' });
    }
  });
  const by = (a, b) => a.at - b.at;
  return {
    edges: { xs: xs.sort(by), ys: ys.sort(by) },
    centres: {
      xs: PANEL_NAMES.map((n) => ({ at: (p[n].x0 + p[n].x1) / 2, kind: 'centre' })).sort(by),
      ys: [{ at: (p.back.y0 + p.back.y1) / 2, kind: 'centre' }],
    },
  };
}

/* The drift detector this file's whole premise needs: the ONE moment the
   client holds both derivations of the same wrap (POST /wrap answers with the
   server's panels) is the one moment it can check them against each other.
   Loud in the console rather than a toast — a person editing a cover cannot
   act on it, and whoever moved one of the two formulas can. */
export function checkPanelsAgree(wrap, served) {
  if (!served || !wrap) return true;
  const mine = panels(wrap);
  const bad = [];
  const near = (a, b) => Math.abs(a - b) <= 1e-6;
  PANEL_NAMES.forEach((name) => {
    ['x0', 'x1', 'y0', 'y1'].forEach((k) => {
      if (served[name] && !near(mine[name][k], served[name][k])) {
        bad.push(`${name}.${k}: ours ${mine[name][k]}, server ${served[name][k]}`);
      }
    });
  });
  if (served.sheet && (mine.sheet.w_px !== served.sheet.w_px
      || mine.sheet.h_px !== served.sheet.h_px)) {
    bad.push(`sheet px: ours ${mine.sheet.w_px}x${mine.sheet.h_px}, `
      + `server ${served.sheet.w_px}x${served.sheet.h_px}`);
  }
  if (bad.length) {
    console.warn('canvas/wrap.js has drifted from docproof/canvas/wrap.py::panels — '
      + bad.join(' · '));
  }
  return !bad.length;
}
