"""Judging when two findings are the same edit, and merging an ensemble's.

Two jobs share one definition of "same edit":

  * Compare (docproof/trackdiff.py) lines a human proofread up against
    docproof's, and must call a word-level retype and a one-character fix equal
    when they change the same characters.
  * The ensemble merge folds several detectors' findings for one chunk into a
    single list, counting how many detectors agree on each edit.

`canonical_anchors` is the shared engine: it re-derives base→target to a fixed
granularity so the two callers can never drift on what counts as the same
change. `merge` is the ensemble half — union for recall, with an agreement count
and a disputed-fix flag the verifier reads.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from difflib import SequenceMatcher

from .models import CONFIDENCE_RANK, Anchor, DocumentModel, Finding, index_paragraphs
from .validator import anchor_offset, find_nth

log = logging.getLogger("docproof.agreement")

# Two change regions this few unchanged characters apart or fewer count as one
# edit — a single logical fix that touches more than one spot must not split
# into an agreement and a phantom extra. Wider than this and two genuinely
# separate edits would fuse. (The value Compare has always used.)
_CANON_GAP = 3


def fold(s: str) -> str:
    """Quote-fold, length-preserving, so a straight/curly difference is not
    mistaken for an edit or allowed to hide agreement."""
    return (s.replace("“", '"').replace("”", '"')
             .replace("‘", "'").replace("’", "'"))


def canonical_anchors(base: str, target: str) -> list[Anchor]:
    """`base` → `target` as a minimal set of replacement anchors in base
    coordinates, independent of how the change was expressed.

    Both sides are re-derived to the *same* granularity, so a change one source
    wrote as a single wide edit and another split into several narrow ones line
    up instead of drifting apart with a phantom extra left over."""
    fb, ft = fold(base), fold(target)
    ops = [op for op in
           SequenceMatcher(None, fb, ft, autojunk=False).get_opcodes()
           if op[0] != "equal"]
    anchors: list[Anchor] = []
    i = 0
    while i < len(ops):
        _, i1, i2, j1, j2 = ops[i]
        while i + 1 < len(ops) and ops[i + 1][1] - i2 <= _CANON_GAP:
            i2, j2 = ops[i + 1][2], ops[i + 1][4]
            i += 1
        anchors.append(Anchor(i1, i2, base[i1:i2], target[j1:j2]))
        i += 1
    return anchors


# --- ensemble merge ----------------------------------------------------------

def _edit_region(f: Finding, para_text: str) -> tuple[int, int, str] | None:
    """(lo, hi, folded insert) of a finding's edit in paragraph coordinates, or
    None when the quote does not anchor or the fix is a no-op. lo..hi is the
    span the edit deletes; two findings are the same edit when these overlap."""
    s = anchor_offset(para_text, f.original_text, f.occurrence)
    if s == -1:
        return None
    anchors = canonical_anchors(f.original_text, f.corrected_text)
    if not anchors:
        return None
    lo = s + min(a.start for a in anchors)
    hi = s + max(a.end for a in anchors)
    return lo, hi, fold("".join(a.insert_text for a in anchors))


def _touch(r1: tuple[int, int], r2: tuple[int, int]) -> bool:
    """Whether two delete regions are the same edit site. Overlapping spans
    touch; a zero-width insertion touches only at its own point (so two fixes to
    different words of one sentence stay distinct, but two fixes to the same word
    cluster)."""
    lo, hi = max(r1[0], r2[0]), min(r1[1], r2[1])
    if lo < hi:
        return True
    if lo == hi:                                  # meet at a single point
        return r1[0] == r1[1] == lo or r2[0] == r2[1] == lo
    return False


def _best(cluster: list[tuple[Finding, bool]]) -> Finding:
    """The representative for a cluster: prefer a finding whose quote anchored
    exactly (cleanest form), then higher confidence, then the lowest id for a
    stable choice."""
    def key(item):
        f, exact = item
        return (0 if exact else 1, -CONFIDENCE_RANK.get(f.confidence, 0),
                f.finding_id)
    return min(cluster, key=key)[0]


def merge(findings: list[Finding], doc: DocumentModel) -> list[Finding]:
    """Fold several detectors' findings for the document into one list.

    Findings whose edits land on the same span are one finding with an
    `agreement` count (how many distinct detectors found it), `provenance` (which
    ones), and `disputed_fix` set when they proposed different corrections for
    that span — the verifier resolves those. Findings that can't anchor or are
    no-ops pass straight through as singletons, so the validator still reports
    them exactly as it would have. Runs BEFORE validate_findings, so the
    validator's own dedupe/overlap rules see one already-merged list."""
    paras = index_paragraphs(doc)
    order = {p.para_id: i for i, p in enumerate(doc.paragraphs)}

    by_para: dict[str, list] = {}
    passthrough: list[tuple[Finding, int]] = []      # (finding, sort position)
    for f in findings:
        para = paras.get(f.para_id)
        region = _edit_region(f, para.text) if para else None
        if region is None:
            passthrough.append((replace(f, agreement=1,
                                        provenance=(f.detector,)), 0))
            continue
        exact = para and find_nth(para.text, f.original_text, f.occurrence) != -1
        by_para.setdefault(f.para_id, []).append((f, region, bool(exact)))

    out: list[tuple[int, int, Finding]] = []
    for para_id, items in by_para.items():
        items.sort(key=lambda t: (t[1][0], t[1][1]))
        clusters: list[dict] = []
        for f, region, exact in items:
            for cl in clusters:
                if _touch(region, cl["span"]):
                    cl["members"].append((f, exact))
                    cl["inserts"].add(region[2])
                    cl["span"] = (min(cl["span"][0], region[0]),
                                  max(cl["span"][1], region[1]))
                    break
            else:
                clusters.append({"members": [(f, exact)], "inserts": {region[2]},
                                 "span": (region[0], region[1])})
        for cl in clusters:
            rep = _best(cl["members"])
            provenance = tuple(sorted({f.detector for f, _ in cl["members"]}))
            out.append((order.get(para_id, 1 << 30), cl["span"][0],
                        replace(rep, agreement=len(provenance),
                                provenance=provenance,
                                disputed_fix=len(cl["inserts"]) > 1)))

    merged = [f for _, _, f in sorted(out, key=lambda t: (t[0], t[1]))]
    merged += [f for f, _ in passthrough]
    return merged
