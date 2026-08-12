"""Composing several rounds of edits into one diff against the original.

Multi-round review corrects the manuscript between rounds: round k reviews the
text as round k-1 left it, so an edit round k proposes is expressed in that
already-corrected text's coordinates, not the original's. To deliver a single
Word file whose tracked changes all read against the ORIGINAL, every round's
edits must be folded back into original coordinates — and when a later round
edits text an earlier round introduced, the two must compose into one change
(the original span -> the final text), because Word cannot show a pending
revision stacked on another pending revision.

An `EditLayer` is that fold: for one paragraph, the ordered, non-overlapping set
of (original span -> replacement) entries that turns the ORIGINAL paragraph into
the current working text. `render(orig)` plays the layer forward to the working
text; `fold_round(orig, edits)` takes a round's approved edits — expressed in the
current working text's coordinates — and returns the layer that also accounts for
them, composing where they touch earlier entries.

Pure geometry: no I/O, no model calls, no logging of document content. Paragraph
ids are stable across rounds (span edits never split or merge paragraphs and each
round's working document is a transformed copy of the same file), so a layer is
always scoped to one paragraph and `orig` is that paragraph's fixed W0 text.

The load-bearing invariant, exercised by the property tests:

    render(orig, fold_round(orig, layer, edits)) ==
        <edits applied to render(orig, layer)>

i.e. folding a round then rendering equals rendering then applying that round's
edits — for any sequence of rounds.
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from itertools import chain
from typing import Iterator

from .models import Finding, ParagraphRef
from .validator import _oversteps

log = logging.getLogger("docproof.editlayer")


@dataclass(frozen=True)
class Contribution:
    """One round's authorship of (part of) a composed edit. Kept so the final
    report and change log can say which round and which finding produced each
    tracked change, even after several rounds fused into one."""
    round: int
    finding_id: str
    error_type: str
    explanation: str


@dataclass(frozen=True)
class Edit:
    """One entry of a layer: replace the ORIGINAL paragraph's [orig_start,
    orig_end) with `replacement`. A pure insertion has orig_start == orig_end; a
    pure deletion has replacement == "". `contributions` lists every round that
    fed into this entry, in original order — length 1 for an untouched edit, more
    when later rounds composed into it."""
    orig_start: int
    orig_end: int
    replacement: str
    contributions: tuple[Contribution, ...]


@dataclass(frozen=True)
class RoundEdit:
    """A single approved edit from a round, in the coordinates of the text that
    round reviewed — i.e. of `render(orig, layer)` for the layer as it stood
    before this round. wk_start == wk_end is an insertion; replacement == "" a
    deletion."""
    wk_start: int
    wk_end: int
    replacement: str
    contribution: Contribution


@dataclass(frozen=True)
class OverstepReport:
    """A composed edit the edit guard refused as too large to be a proofreading
    change (mirroring the validator's rejected_oversized). It is NOT folded into
    the layer — the working text is left as if this edit were declined — and is
    handed back so the orchestrator can route it to the query channel (a margin
    comment) instead of a silent tracked change. Coordinates are original."""
    orig_start: int
    orig_end: int
    replacement: str
    contributions: tuple[Contribution, ...]


@dataclass(frozen=True)
class FoldResult:
    """The outcome of folding a round: the new layer, plus what could not be
    folded as a clean tracked change. `oversteps` route to queries; `noops`
    counts round edits that exactly reverted an earlier edit (dropped, leaving
    the text as the original had it)."""
    layer: "EditLayer"
    oversteps: tuple[OverstepReport, ...] = ()
    noops: int = 0


@dataclass(frozen=True)
class _Seg:
    """A stretch of the working text and where it comes from. A 'keep' segment is
    a run of untouched original text (linear map to original coordinates); a
    'repl' segment is one entry's replacement text (maps as a whole to that
    entry's original span [orig_lo, orig_hi), not character by character)."""
    kind: str            # "keep" | "repl"
    wk_lo: int           # span in the working text
    wk_hi: int
    orig_lo: int         # keep: start of this run; repl: entry.orig_start
    orig_hi: int         # keep: end of this run; repl: entry.orig_end
    edit_index: int      # repl: index into the layer's edits; keep: -1


@dataclass(frozen=True)
class EditLayer:
    """The cumulative original->working diff for one paragraph, as ordered,
    non-overlapping entries. Immutable; every operation returns a new layer."""
    edits: tuple[Edit, ...] = ()

    # -- reading ------------------------------------------------------------

    def render(self, orig: str) -> str:
        """The current working text: `orig` with every entry applied."""
        out: list[str] = []
        o = 0
        for e in self.edits:
            out.append(orig[o:e.orig_start])
            out.append(e.replacement)
            o = e.orig_end
        out.append(orig[o:])
        return "".join(out)

    def to_orig_span(self, orig: str, wk_start: int, wk_end: int) -> tuple[int, int]:
        """Map a working-text span back to original coordinates, for translating a
        round's query anchors onto the delivered (original-coordinate) document. A
        span reaching into replaced text widens to the composed entry's whole
        original span — a query cannot point at half of a replacement that no
        longer exists in the original."""
        segs = _segments(orig, self.edits, self.render(orig))
        a2 = _clean_left(segs, wk_start)
        b2 = _clean_right(segs, wk_end)
        return _orig_at_clean(segs, a2), _orig_at_clean(segs, b2)

    @property
    def is_empty(self) -> bool:
        return not self.edits

    # -- folding a round ----------------------------------------------------

    def fold_round(self, orig: str, edits: list[RoundEdit],
                   guard=None) -> FoldResult:
        """Fold a round's approved edits — all in the coordinates of
        `render(orig, self)` — into a new layer.

        Every edit is resolved against ONE shared segment frame (the pre-fold
        working text), never re-rendering between edits, so two edits that land
        inside the same earlier replacement stay correctly addressed. An edit
        widens off any replacement interior it touches (a replacement is atomic in
        original coordinates); edits whose widened ranges overlap the same earlier
        entry are clustered and composed into a single change, so the delivered
        document shows one tracked change per site rather than a stack Word cannot
        render."""
        if not edits:
            return FoldResult(self)
        ordered = sorted(edits, key=lambda r: (r.wk_start, r.wk_end))
        for a, b in zip(ordered, ordered[1:]):
            overlap = a.wk_end > b.wk_start
            both_point = a.wk_start == a.wk_end == b.wk_start == b.wk_end
            if overlap or both_point:
                raise ValueError(
                    f"round edits overlap in working coordinates: "
                    f"[{a.wk_start},{a.wk_end}) and [{b.wk_start},{b.wk_end})")

        wk = self.render(orig)
        for r in ordered:
            if not (0 <= r.wk_start <= r.wk_end <= len(wk)):
                raise ValueError(
                    f"edit [{r.wk_start},{r.wk_end}) out of working-text range "
                    f"0..{len(wk)}")
        segs = _segments(orig, self.edits, wk)

        # Widen each edit off replacement interiors, then merge edits whose
        # widened working ranges overlap (they share an earlier entry) into one
        # cluster: [lo, hi, absorbed entry indices, its round edits].
        items = []
        for r in ordered:
            lo = _clean_left(segs, r.wk_start)
            hi = _clean_right(segs, r.wk_end)
            items.append([lo, hi, _absorbed(segs, lo, hi), [r]])
        items.sort(key=lambda it: (it[0], it[1]))
        clusters: list[list] = []
        for it in items:
            # Merge into the running cluster on a strict working overlap, or when
            # both absorb the same earlier entry (two edits abutting exactly at a
            # deletion's seam — the deletion belongs to one composed change).
            if clusters and (it[0] < clusters[-1][1] or (it[2] & clusters[-1][2])):
                clusters[-1][1] = max(clusters[-1][1], it[1])
                clusters[-1][2] |= it[2]
                clusters[-1][3].extend(it[3])
            else:
                clusters.append(it)

        absorbed_all: set[int] = set()
        for _, _, absorbed, _res in clusters:
            absorbed_all |= absorbed
        new_entries: list[tuple[int, Edit]] = []      # (working start, entry)
        oversteps: list[OverstepReport] = []
        noops = 0
        for lo, hi, absorbed, res in clusters:
            res.sort(key=lambda r: r.wk_start)
            o_lo = _orig_at_clean(segs, lo)
            o_hi = _orig_at_clean(segs, hi)
            # A composed entry must cover every original span it absorbs. This
            # only stretches the range for a zero-width deletion sitting on the
            # cluster's boundary (whose orig span is invisible in working
            # coordinates); normal replacements are already inside [o_lo, o_hi).
            for i in absorbed:
                o_lo = min(o_lo, self.edits[i].orig_start)
                o_hi = max(o_hi, self.edits[i].orig_end)
            local = [(r.wk_start - lo, r.wk_end - lo, r.replacement) for r in res]
            newtext = _apply_disjoint(wk[lo:hi], local)
            prior = tuple(chain.from_iterable(
                self.edits[i].contributions for i in sorted(absorbed)))
            contribs = prior + tuple(r.contribution for r in res)

            # Exact revert of everything it touched: drop it (and the entries it
            # absorbed) — the region is the original text again.
            if newtext == orig[o_lo:o_hi]:
                noops += 1
                continue
            # Too large to be a proofreading change once composed: keep the
            # earlier entries, drop this round's edits, and route it to a query.
            if guard is not None and _oversteps(orig[o_lo:o_hi], newtext, guard):
                oversteps.append(OverstepReport(o_lo, o_hi, newtext, contribs))
                absorbed_all -= absorbed
                continue
            new_entries.append((lo, Edit(o_lo, o_hi, newtext, contribs)))

        # Order the layer by working position, not original position: several
        # zero-width edits can share one original point (a prior insertion and a
        # new one), and only their working order says which renders first — a new
        # insertion at working offset p precedes existing content that starts at
        # p, exactly as text[:p] + new + text[p:] would. Working order keeps
        # original starts non-decreasing, so render still slices correctly.
        wk_lo_of = {s.edit_index: s.wk_lo for s in segs if s.kind == "repl"}
        ordered_entries: list[tuple[int, int, Edit]] = []
        for i, e in enumerate(self.edits):
            if i not in absorbed_all:
                ordered_entries.append((wk_lo_of[i], 1, e))   # kept: after new ties
        for lo, e in new_entries:
            ordered_entries.append((lo, 0, e))                # new: before kept ties
        ordered_entries.sort(key=lambda t: (t[0], t[1]))
        kept = tuple(e for _, _, e in ordered_entries)
        return FoldResult(EditLayer(kept), tuple(oversteps), noops)

    def fold_edit(self, orig: str, re: RoundEdit, guard=None) -> FoldResult:
        """Fold one edit, given in the coordinates of `render(orig, self)`."""
        return self.fold_round(orig, [re], guard)

    # -- bridging to the pipeline ------------------------------------------

    def to_findings(self, para: ParagraphRef, chunk_id: str,
                    ids: Iterator[str], *, confidence: str = "high") -> list[Finding]:
        """One Finding per entry, ready for the existing validator/apply path.

        Uses the whole-paragraph splice the rewrite pass already relies on
        (rewrite.py): original_text is the entire ORIGINAL paragraph and
        corrected_text is that paragraph with only this entry's span replaced, so
        the validator's canonical diff recovers exactly this edit and insertions
        anchor without an empty quote. error_type and explanation come from the
        entry's first contributor; the joined explanation names every round that
        composed in."""
        orig = para.text
        out: list[Finding] = []
        for e in self.edits:
            corrected = orig[:e.orig_start] + e.replacement + orig[e.orig_end:]
            out.append(Finding(
                finding_id=next(ids),
                chunk_id=chunk_id,
                para_id=para.para_id,
                error_type=e.contributions[0].error_type,
                original_text=orig,
                occurrence=1,
                corrected_text=corrected,
                explanation=_join_explanations(e.contributions),
                confidence=confidence))
        return out


def _apply_disjoint(text: str, edits: list[tuple[int, int, str]]) -> str:
    """Apply disjoint (start, end, replacement) edits, all in `text`'s
    coordinates, by splicing right-to-left."""
    for a, b, repl in sorted(edits, reverse=True):
        text = text[:a] + repl + text[b:]
    return text


# --- segment geometry --------------------------------------------------------

def _segments(orig: str, edits: tuple[Edit, ...], wk: str) -> list[_Seg]:
    """Slice the working text into keep/repl segments in order. `wk` is passed in
    (the caller already rendered it) to avoid re-rendering."""
    segs: list[_Seg] = []
    o = 0
    w = 0
    for i, e in enumerate(edits):
        if e.orig_start > o:                       # untouched original run
            width = e.orig_start - o
            segs.append(_Seg("keep", w, w + width, o, e.orig_start, -1))
            w += width
            o = e.orig_start
        rlen = len(e.replacement)
        segs.append(_Seg("repl", w, w + rlen, e.orig_start, e.orig_end, i))
        w += rlen
        o = e.orig_end
    if o < len(orig):                              # trailing original run
        segs.append(_Seg("keep", w, w + (len(orig) - o), o, len(orig), -1))
        w += len(orig) - o
    if not segs:                                   # empty paragraph, no edits
        segs.append(_Seg("keep", 0, 0, 0, 0, -1))
    assert w == len(wk), f"segment width {w} != rendered {len(wk)}"
    return segs


def _clean_left(segs: list[_Seg], p: int) -> int:
    """The nearest working position <= p that maps to a definite original
    position: p itself, unless p lies strictly inside a replacement, in which case
    the replacement's start."""
    for s in segs:
        if s.kind == "repl" and s.wk_lo < p < s.wk_hi:
            return s.wk_lo
    return p


def _clean_right(segs: list[_Seg], p: int) -> int:
    for s in segs:
        if s.kind == "repl" and s.wk_lo < p < s.wk_hi:
            return s.wk_hi
    return p


def _orig_at_clean(segs: list[_Seg], p: int) -> int:
    """Original coordinate of a clean working position (not strictly inside a
    replacement). At a segment boundary the earliest matching segment answers;
    both sides agree by construction (a repl's orig_hi equals the next segment's
    orig_lo)."""
    for s in segs:
        if s.wk_lo <= p <= s.wk_hi:
            if s.kind == "keep":
                return s.orig_lo + (p - s.wk_lo)
            if p == s.wk_lo:
                return s.orig_lo                       # entry.orig_start
            if p == s.wk_hi:
                return s.orig_hi                       # entry.orig_end
    raise AssertionError(f"position {p} is not a clean working coordinate")


def _absorbed(segs: list[_Seg], a2: int, b2: int) -> set[int]:
    """Indices of layer entries whose replacement lies within the composed working
    region [a2, b2]. A zero-width replacement (a pure deletion) is absorbed even
    when the region only meets it at a boundary: its removed original text is
    invisible in working coordinates, so an edit touching that seam must take it
    over rather than leave it to overlap the composed entry's original span."""
    out: set[int] = set()
    for s in segs:
        if s.kind == "repl" and a2 <= s.wk_lo and s.wk_hi <= b2:
            out.add(s.edit_index)
    return out


def _join_explanations(contribs: tuple[Contribution, ...]) -> str:
    seen: list[str] = []
    for c in contribs:
        e = c.explanation.strip()
        if e and e not in seen:
            seen.append(e)
    return " ".join(seen)
