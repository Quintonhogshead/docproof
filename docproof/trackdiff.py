"""Compare the tracked changes on two .docx files.

Each document's revisions are reduced to a set of edits — DocProof's own
`Anchor` type, `(start, end, delete_text, insert_text)` — expressed as offsets
into the document's *reject-all* text (the manuscript as it reads with every
change rejected). Two documents that began from the same manuscript share that
base, so their edits land in the same coordinate system and can be set-compared
directly.

Paragraphs are lined up by that reject-all *content*, not by ordinal position:
a single inserted, deleted, or split paragraph — a blank line, a scene break —
shifts every positional id after it, so aligning on position throws the whole
tail of the document out of sync. Matching on the base text lets identical prose
pair up regardless of surrounding structural drift.

The comparison buckets every edit as:

  * agree           — both docs make the same net change to the same place
  * different fix   — both change the same place, to different text
  * only in A / B   — one doc changed it and the other did not

Point it at (human-proofread doc) vs (DocProof's output on the same raw file)
and "only in A" is DocProof's recall gap, "agree + different fix" is what it
caught, and "only in B" is the set to eyeball for false positives. That is the
manuscript-mode eval; it is also just a useful way to diff two editors' passes.

The core here has no CLI or app dependency on purpose: the `compare` command,
the scorecard, and any future app tab are thin wrappers over `compare_files`.
"""
from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from .agreement import canonical_anchors, fold
from .models import Anchor
from .utils.xml_helpers import (DELTEXT_TAG, DEL_TAG, DocxPackage, INS_TAG,
                                T_TAG, walk_package)

_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0"


class TrackDiffError(ValueError):
    pass


# --- opening a tracked-changes doc (without the review preflight) -------------

def open_docx(path: str | Path) -> DocxPackage:
    """Open a .docx as a package. Unlike ingest.preflight, this does NOT reject
    a document that carries tracked changes — comparing them is the whole
    point."""
    path = Path(path)
    if not path.exists():
        raise TrackDiffError(f"File not found: {path}")
    with path.open("rb") as fh:
        head = fh.read(4)
    if head == _OLE_MAGIC:
        raise TrackDiffError(
            f"{path.name} is a legacy .doc or password-protected file. Open it "
            f"in Word and Save As .docx, then rerun.")
    if head != _ZIP_MAGIC:
        raise TrackDiffError(f"{path.name} is not a .docx file (bad signature).")
    try:
        pkg = DocxPackage(path)
    except zipfile.BadZipFile as e:
        raise TrackDiffError(f"{path.name} is corrupt: {e}") from e
    if not pkg.has("word/document.xml"):
        raise TrackDiffError(f"{path.name} is missing word/document.xml.")
    return pkg


# --- extracting edits as anchors in reject-coordinates -----------------------

def extract_paragraph_edits(p) -> tuple[str, list[Anchor]]:
    """Return (reject_text, edits) for one paragraph element.

    reject_text is the paragraph with every change rejected — the base. Each
    edit's offsets index into that base: a deletion covers the span it removes,
    an insertion is zero-width at the point it adds text. Adjacent delete+insert
    (a replacement, however Word ordered them) is coalesced into one anchor, so
    the result matches the shape DocProof's own findings take."""
    raw: list[Anchor] = []
    base: list[str] = []
    pos = 0

    def walk(el, in_ins: bool, in_del: bool):
        nonlocal pos
        for c in el:
            if c.tag == INS_TAG:
                walk(c, True, in_del)
            elif c.tag == DEL_TAG:
                walk(c, in_ins, True)
            elif c.tag in (T_TAG, DELTEXT_TAG):
                text = c.text or ""
                if in_ins:
                    raw.append(Anchor(pos, pos, "", text))     # insertion point
                elif in_del:
                    raw.append(Anchor(pos, pos + len(text), text, ""))
                    base.append(text)
                    pos += len(text)
                else:
                    base.append(text)
                    pos += len(text)
            else:
                walk(c, in_ins, in_del)

    walk(p, False, False)
    return "".join(base), _coalesce(raw)


def _coalesce(raw: list[Anchor]) -> list[Anchor]:
    """Merge an adjacent pure-delete and pure-insert into one replacement."""
    out: list[Anchor] = []
    for a in raw:
        if out:
            prev = out[-1]
            # delete then insert at the deletion's end
            if (prev.insert_text == "" and a.delete_text == ""
                    and a.start == a.end == prev.end):
                out[-1] = Anchor(prev.start, prev.end, prev.delete_text,
                                 a.insert_text)
                continue
            # insert then delete at the insertion point
            if (prev.delete_text == "" and prev.start == prev.end
                    and a.insert_text == "" and a.start == prev.start):
                out[-1] = Anchor(a.start, a.end, a.delete_text, prev.insert_text)
                continue
        out.append(a)
    return out


@dataclass(frozen=True)
class DocEdits:
    base: dict[str, str]                 # para_id -> reject-all text
    edits: dict[str, list[Anchor]]      # para_id -> edits, in base coordinates

    @property
    def edit_count(self) -> int:
        return sum(len(v) for v in self.edits.values())


def extract_edits(pkg: DocxPackage) -> DocEdits:
    base: dict[str, str] = {}
    edits: dict[str, list[Anchor]] = {}
    for wp in walk_package(pkg):
        text, anchors = extract_paragraph_edits(wp.element)
        base[wp.para_id] = text
        if anchors:
            edits[wp.para_id] = anchors
    return DocEdits(base=base, edits=edits)


# --- comparison --------------------------------------------------------------

def _overlaps(a: Anchor, b: Anchor) -> bool:
    a0, a1 = a.start, a.end
    b0, b1 = b.start, b.end
    if a0 == a1:          # widen a zero-width insertion so it can touch a span
        a1 = a0 + 1
    if b0 == b1:
        b1 = b0 + 1
    return a0 < b1 and b0 < a1


def _apply(base: str, a: Anchor) -> str:
    return fold(base[:a.start] + a.insert_text + base[a.end:])


def _same_effect(base: str, a: Anchor, b: Anchor) -> bool:
    """True when a and b produce the same paragraph, even if one used a whole
    word and the other a minimal one-character diff."""
    return _apply(base, a) == _apply(base, b)


def _apply_all(base: str, edits: list[Anchor]) -> str:
    """The paragraph as it reads with every one of `edits` accepted — one
    document's whole tracked-changes result for this paragraph. The edits are
    non-overlapping (they come from a single document's revisions), so stitching
    the base's untouched spans around each insertion reproduces it."""
    out: list[str] = []
    last = 0
    for a in sorted(edits, key=lambda e: (e.start, e.end)):
        out.append(base[last:a.start])
        out.append(a.insert_text)
        last = a.end
    out.append(base[last:])
    return "".join(out)




@dataclass
class ParaCompare:
    para_id: str
    agree: list[tuple[Anchor, Anchor]] = field(default_factory=list)
    diff_fix: list[tuple[Anchor, Anchor]] = field(default_factory=list)
    only_a: list[Anchor] = field(default_factory=list)
    only_b: list[Anchor] = field(default_factory=list)


@dataclass
class CompareReport:
    label_a: str
    label_b: str
    paras: list[ParaCompare] = field(default_factory=list)
    aligned_paras: int = 0
    unaligned: list[str] = field(default_factory=list)   # base mismatch / structure

    # -- tallies ----------------------------------------------------------
    @property
    def agree(self) -> int:
        return sum(len(p.agree) for p in self.paras)

    @property
    def diff_fix(self) -> int:
        return sum(len(p.diff_fix) for p in self.paras)

    @property
    def only_a(self) -> int:
        return sum(len(p.only_a) for p in self.paras)

    @property
    def only_b(self) -> int:
        return sum(len(p.only_b) for p in self.paras)

    # -- manuscript-mode score (A is ground truth) ------------------------
    @property
    def located_recall(self) -> float | None:
        found, missed = self.agree + self.diff_fix, self.only_a
        d = found + missed
        return found / d if d else None

    @property
    def exact_recall(self) -> float | None:
        d = self.agree + self.diff_fix + self.only_a
        return self.agree / d if d else None

    @property
    def precision(self) -> float | None:
        found = self.agree + self.diff_fix
        d = found + self.only_b
        return found / d if d else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.located_recall
        if not p or not r:
            return None
        return 2 * p * r / (p + r)


def _ordered(doc: DocEdits) -> list[tuple[str, str]]:
    """(para_id, reject-all base) in document order. `DocEdits.base` is built by
    walking the package top to bottom, and dicts preserve insertion order, so
    this is the paragraph sequence as it reads."""
    return list(doc.base.items())


def compare_edits(a: DocEdits, b: DocEdits, *, label_a: str = "A",
                  label_b: str = "B") -> CompareReport:
    report = CompareReport(label_a=label_a, label_b=label_b)
    a_items = _ordered(a)
    b_items = _ordered(b)
    # Align paragraphs by content. We only ever compare a pair whose reject-all
    # bases are identical (quote-fold aside) — otherwise the offsets are not in
    # the same coordinate system and a "match" would be luck — so diff the two
    # sequences of base texts and take difflib's runs of equal paragraphs.
    a_keys = [fold(base) for _, base in a_items]
    b_keys = [fold(base) for _, base in b_items]
    sm = SequenceMatcher(a=a_keys, b=b_keys, autojunk=False)
    matched_a: set[int] = set()
    matched_b: set[int] = set()
    for i, j, n in sm.get_matching_blocks():
        for k in range(n):
            ia, ib = i + k, j + k
            matched_a.add(ia)
            matched_b.add(ib)
            pa_id, base = a_items[ia]
            pb_id, _ = b_items[ib]
            report.aligned_paras += 1
            report.paras.append(_compare_para(
                pa_id, base, a.edits.get(pa_id, []), b.edits.get(pb_id, [])))
    # A paragraph that carries edits but found no content match is unaligned:
    # its base differs between the two files, so it has no comparable partner.
    unaligned = {pid for idx, (pid, _) in enumerate(a_items)
                 if idx not in matched_a and a.edits.get(pid)}
    unaligned |= {pid for idx, (pid, _) in enumerate(b_items)
                  if idx not in matched_b and b.edits.get(pid)}
    report.unaligned = sorted(unaligned)
    # Drop paragraphs where nothing happened, to keep the report about edits.
    report.paras = [p for p in report.paras
                    if p.agree or p.diff_fix or p.only_a or p.only_b]
    return report


def _compare_para(para_id: str, base: str, a_edits: list[Anchor],
                  b_edits: list[Anchor]) -> ParaCompare:
    pc = ParaCompare(para_id=para_id)
    # Compare by resulting text, not by how each document recorded its edits:
    # re-derive both sides from their accept-all result to one canonical
    # granularity first, so a word-level retype and a minimal fix that reach the
    # same text agree instead of splitting into a disagreement and a phantom.
    a_canon = canonical_anchors(base, _apply_all(base, a_edits))
    b_canon = canonical_anchors(base, _apply_all(base, b_edits))
    unmatched_b = list(b_canon)
    for a in a_canon:
        overlapping = [b for b in unmatched_b if _overlaps(a, b)]
        # Prefer a same-effect partner (an "agree") over a mere overlap.
        exact = next((b for b in overlapping if _same_effect(base, a, b)), None)
        chosen = exact or (overlapping[0] if overlapping else None)
        if chosen is None:
            pc.only_a.append(a)
        elif exact is not None:
            pc.agree.append((a, chosen))
            unmatched_b.remove(chosen)
        else:
            pc.diff_fix.append((a, chosen))
            unmatched_b.remove(chosen)
    pc.only_b = unmatched_b
    return pc


def compare_files(path_a: str | Path, path_b: str | Path, *,
                  label_a: str = "A", label_b: str = "B") -> CompareReport:
    return compare_edits(extract_edits(open_docx(path_a)),
                         extract_edits(open_docx(path_b)),
                         label_a=label_a, label_b=label_b)


# --- rendering ---------------------------------------------------------------

def _edit_str(base: str, a: Anchor) -> str:
    """A compact, readable rendering of one edit: what it deletes → inserts,
    with a little surrounding context from the base text."""
    lo = max(0, a.start - 12)
    hi = min(len(base), a.end + 12)
    pre = ("…" if lo > 0 else "") + base[lo:a.start]
    post = base[a.end:hi] + ("…" if hi < len(base) else "")
    dele = f"«{a.delete_text}»" if a.delete_text else ""
    ins = f"⟨{a.insert_text}⟩" if a.insert_text else ""
    arrow = "→" if a.delete_text and a.insert_text else ""
    return f"{pre}{dele}{arrow}{ins}{post}".replace("\n", " ")


def report_json(report: CompareReport, base_a: dict[str, str]) -> dict:
    def edit(base, a):
        # Raw offsets for machines, a rendered snippet for humans.
        return {"span": [a.start, a.end], "delete": a.delete_text,
                "insert": a.insert_text, "text": _edit_str(base, a)}

    def pair(base, a, b):
        return {"a": edit(base, a), "b": edit(base, b)}

    return {
        "schema_version": 1,
        "label_a": report.label_a,
        "label_b": report.label_b,
        "aligned_paragraphs": report.aligned_paras,
        "unaligned_paragraphs": report.unaligned,
        "totals": {"agree": report.agree, "different_fix": report.diff_fix,
                   "only_a": report.only_a, "only_b": report.only_b},
        "score_a_as_truth": {
            "located_recall": _round(report.located_recall),
            "exact_recall": _round(report.exact_recall),
            "precision": _round(report.precision),
            "f1": _round(report.f1)},
        "paragraphs": [
            {"para_id": p.para_id,
             "agree": [pair(base_a.get(p.para_id, ""), a, b)
                       for a, b in p.agree],
             "different_fix": [pair(base_a.get(p.para_id, ""), a, b)
                               for a, b in p.diff_fix],
             "only_a": [edit(base_a.get(p.para_id, ""), a) for a in p.only_a],
             "only_b": [edit(base_a.get(p.para_id, ""), a) for a in p.only_b]}
            for p in report.paras],
    }


def _round(x: float | None) -> float | None:
    return round(x, 4) if x is not None else None


def render_markdown(report: CompareReport, base_a: dict[str, str]) -> str:
    def pct(x): return f"{x * 100:.0f}%" if x is not None else "—"
    a, b = report.label_a, report.label_b
    L: list[str] = []
    L.append(f"# Tracked-changes comparison — {a} vs {b}\n")
    L.append(f"{report.aligned_paras} paragraph(s) compared"
             + (f" · {len(report.unaligned)} skipped because their base text "
                f"differs between the two files — a different underlying "
                f"version, so the edits cannot be lined up"
                if report.unaligned else "") + "\n")

    L.append("## Totals\n")
    L.append(f"- **Agree** (same place, same fix): {report.agree}")
    L.append(f"- **Different fix** (same place, different text): "
             f"{report.diff_fix}")
    L.append(f"- **Only in {a}**: {report.only_a}")
    L.append(f"- **Only in {b}**: {report.only_b}\n")

    # Treat A as ground truth — the manuscript-mode scorecard.
    L.append(f"## Scored against {a} as ground truth\n")
    L.append(f"| metric | value |")
    L.append(f"|---|---|")
    L.append(f"| located recall (found the spot) | {pct(report.located_recall)} |")
    L.append(f"| exact recall (same fix too) | {pct(report.exact_recall)} |")
    L.append(f"| precision | {pct(report.precision)} |")
    L.append(f"| F1 | {pct(report.f1)} |")
    L.append(f"\n> Precision counts an *only in {b}* edit against {b}, but such "
             f"an edit may be a real error {a} missed rather than a false "
             f"alarm. Treat precision here as a floor and eyeball the "
             f"*only in {b}* list.\n")

    if report.only_a or report.diff_fix:
        L.append(f"## Misses & disagreements ({a} caught, {b} did not match)\n")
        for p in report.paras:
            if not (p.only_a or p.diff_fix):
                continue
            base = base_a.get(p.para_id, "")
            L.append(f"**{p.para_id}**")
            for edit in p.only_a:
                L.append(f"- miss: {_edit_str(base, edit)}")
            for ea, eb in p.diff_fix:
                L.append(f"- differ: {a} {_edit_str(base, ea)}  ·  "
                         f"{b} {_edit_str(base, eb)}")
            L.append("")

    if report.only_b:
        L.append(f"## Only in {b} (review for false positives)\n")
        for p in report.paras:
            if not p.only_b:
                continue
            base = base_a.get(p.para_id, "")
            L.append(f"**{p.para_id}**")
            for edit in p.only_b:
                L.append(f"- {_edit_str(base, edit)}")
            L.append("")

    return "\n".join(L)
