"""The edit map: the engine's own record of how a paragraph's SOURCE text became
its ACCEPTED text, one applied tracked change at a time — and the translation
between the two coordinate systems that record makes exact.

Why this exists. The finished-text walk (galley/verify.py) reads the ACCEPTED
manuscript — the text the author will read — and quotes what it finds in that
text. But every finding the engine can apply is anchored in the SOURCE text
(validator offsets into the canonical paragraph), and a residual the walk finds
very often sits INSIDE a span some applied edit already owns: fixing it means
revising THAT edit's replacement, not adding a second row that would lose the
overlap. On the Redding run (2026-09-01) 79 residuals could not be settled for
exactly this reason, and the position alignment the practitioner improvised
(text matching against paragraph ids that drift in the tracked docx) produced
218 false alarms. The map here is built from the rows finish() actually wrote
— their anchors are the offsets the reassembler edited at — so an accepted
offset translates to a source offset by arithmetic, with no matching.

Two views of one paragraph are modelled as a list of `Segment`s in document
order. An UNTOUCHED segment copies source text through; an OWNED segment is one
applied row's anchor (source span [src_start, src_end) replaced by `text`,
owner = the row's working key). The accepted paragraph is the concatenation of
every segment's `text`. `locate` finds which segments an accepted range
overlaps; `compose` widens that range until it covers every segment of every
owner it touches (a row split into minimal regions is ONE owner and must be
absorbed whole), maps the widened range back to source, and produces the
composite replacement — the one safe way to change text inside an owned span.

Nothing here reads a document or calls a model. Inputs are plain findings.json
rows and paragraph text; outputs are dataclasses the settle loop (galley/
settle.py) and `import-findings --anchor accepted` act on.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

EDITMAP_NAME = "editmap.json"
EDITMAP_SCHEMA_VERSION = 1

# A row split into minimal regions by the validator carries a letter suffix on
# every region after the first ("f-0031", "f-0031b", "f-0031c"). All of them
# are one decision and one working row.
_REGION_SUFFIX = re.compile(r"^(?P<base>.+?-\d+)(?P<suffix>[b-z])$")


def base_id(finding_id: str) -> str:
    """The id of the row a minimal-region sub-row was split from ("f-0031b" ->
    "f-0031"); any other id unchanged."""
    m = _REGION_SUFFIX.match(finding_id or "")
    return m.group("base") if m else (finding_id or "")


def row_key(row: Mapping[str, Any]) -> tuple:
    """What makes two rows the SAME decision: paragraph, quoted sentence,
    occurrence, correction, and error type. Minimal-region siblings share it;
    so do a checkpoint row and its findings.json copy."""
    return (str(row.get("para_id", "")), str(row.get("original_text", "")),
            int(row.get("occurrence", 1) or 1),
            str(row.get("corrected_text", "")), str(row.get("error_type", "")))


def collapse_region_siblings(rows: Iterable[Any]) -> tuple[list[Any], list[str]]:
    """Fold a findings.json REPORT back into the decisions it reports.

    finish() writes one row per tracked change, so a decision the validator
    split into minimal regions is serialized as its row plus a lettered
    sibling per extra region ("f-0077", "f-0077b"): same paragraph, quote,
    correction, and error type, differing only in id and anchor. Read back
    as INPUT (the merge desk, replay/import-findings, a synthesized case
    file), those siblings are not two decisions. The validator re-splits the
    first on its own, and the sibling, quoting the same whole span, either
    contests its own base row (a phantom same-lane overlap in the merge
    ledger) or lands as `rejected_duplicate` carrying the WHOLE shrunk diff
    under the very id the re-split just minted for a minimal region. The
    Redding final build (2026-09-01) shipped 16 such pairs, and the edit map
    could not tell which "f-0077b" was the real one.

    One row survives per (base id, decision): the one with the shortest id
    (the base row when present, whatever order the file lists them in), at
    the position of the group's first row. Dropped are its lettered siblings
    and any verbatim repeat of the same id (a checkpoint row beside its
    findings.json copy). A sibling whose text DIFFERS from its base is a
    different decision (someone edited one of them) and stays for the
    validator to arbitrate; so do two rows with different base ids that
    agree (the validator's own `rejected_duplicate` covers those, under
    distinct ids). Items that are not rows, or carry no id, pass through.
    Returns (kept, dropped_ids)."""
    slots: list[tuple[str, Any]] = []
    chosen: dict[tuple, Mapping[str, Any]] = {}
    ids: dict[tuple, list[str]] = {}
    for r in rows:
        fid = str(r.get("finding_id") or "") if isinstance(r, Mapping) else ""
        if not fid:
            slots.append(("item", r))
            continue
        key = (base_id(fid), row_key(r))
        if key not in chosen:
            chosen[key] = r
            ids[key] = [fid]
            slots.append(("key", key))
            continue
        ids[key].append(fid)
        if len(fid) < len(str(chosen[key].get("finding_id") or "")):
            chosen[key] = r
    kept = [it if tag == "item" else chosen[it] for tag, it in slots]
    dropped: list[str] = []
    for key, fids in ids.items():
        rest = list(fids)
        rest.remove(str(chosen[key].get("finding_id") or ""))
        dropped.extend(rest)
    return kept, dropped


def _anchor(row: Mapping[str, Any]) -> dict[str, Any] | None:
    a = row.get("anchor")
    if (isinstance(a, dict) and isinstance(a.get("start"), int)
            and isinstance(a.get("end"), int) and 0 <= a["start"] <= a["end"]):
        return a
    return None


def row_applied(row: Mapping[str, Any]) -> bool:
    """A row that landed as a tracked CHANGE (not a query, not a format mark,
    not a rejection). The reporter's `applied` flag is believed when present;
    without it, a validated status is taken as applied."""
    if row.get("force_query") or row.get("queried"):
        return False
    if row.get("format"):
        return False
    applied = row.get("applied")
    if isinstance(applied, bool):
        return applied
    return str(row.get("status") or "") == "validated"


def edit_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The applied, anchored, text-changing rows of an envelope — the rows an
    edit map is built from. Rows without a usable anchor cannot be mapped and
    are left out (the caller sees the paragraph's map disagree with the
    accepted text and falls back to matching)."""
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict) or not row_applied(r):
            continue
        a = _anchor(r)
        if a is None:
            continue
        if a["start"] == a["end"] and not str(a.get("insert_text") or ""):
            continue                                        # a no-op
        out.append(dict(r))
    return out


@dataclass(frozen=True)
class Segment:
    """One stretch of a paragraph in both coordinate systems. `owner` is None
    for text no edit touched; otherwise the working key id of the applied row
    whose replacement `text` is."""

    src_start: int
    src_end: int
    acc_start: int
    acc_end: int
    text: str
    owner: str | None = None
    row_ids: tuple[str, ...] = ()

    @property
    def untouched(self) -> bool:
        return self.owner is None

    def to_json(self) -> dict[str, Any]:
        return {"src": [self.src_start, self.src_end],
                "acc": [self.acc_start, self.acc_end], "text": self.text,
                "owner": self.owner, "rows": list(self.row_ids)}

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "Segment":
        return cls(src_start=int(d["src"][0]), src_end=int(d["src"][1]),
                   acc_start=int(d["acc"][0]), acc_end=int(d["acc"][1]),
                   text=str(d.get("text", "")), owner=d.get("owner"),
                   row_ids=tuple(d.get("rows") or ()))


class EditMapError(ValueError):
    """The rows do not describe the source text they claim to (an anchor whose
    delete_text is not what sits at its offsets) — the map for that paragraph
    cannot be trusted."""


def paragraph_map(source: str, rows: Sequence[Mapping[str, Any]],
                  *, owner_of: Mapping[str, str] | None = None,
                  skipped: list[str] | None = None) -> list[Segment]:
    """Build one paragraph's segment list from its applied rows.

    `owner_of` maps a row's finding_id to its working key id (minimal-region
    siblings and duplicate rows resolve to one owner); without it each row's
    base id is its owner. Rows are ordered by anchor start; an anchor whose
    delete_text is not what the source holds at its offsets raises
    EditMapError rather than producing a map that lies.

    An anchor that OVERLAPS the previous edit is a validator artifact (a
    sub-row carrying the whole diff beside its own region — 16 pairs on the
    Redding build); the reassembler applied one of them, and which one is
    decided by the accepted text, not by the anchors. When `skipped` is given
    such a row is left out and its id appended there, and the caller checks
    the reconstruction against the docx before trusting the map; without
    `skipped` it raises, as before."""
    owner_of = owner_of or {}
    ordered = sorted(rows, key=lambda r: (r["anchor"]["start"],
                                          r["anchor"]["end"]))
    segs: list[Segment] = []
    src_pos = 0
    acc_pos = 0
    last_start_of: dict[str, int] = {}
    for r in ordered:
        a = r["anchor"]
        start, end = a["start"], a["end"]
        fid = str(r.get("finding_id") or "")
        owner = owner_of.get(fid) or base_id(fid) or "?"
        # Two anchors of ONE owner at the same start are the validator
        # artifact (a region beside the whole diff), even when the first is
        # a zero-width insertion that a plain range test would let through
        # — applied as two, they compose ",,".
        same_owner_same_start = last_start_of.get(owner) == start
        if start < src_pos or same_owner_same_start:
            if skipped is not None:
                skipped.append(fid)
                continue
            raise EditMapError(
                f"{fid}: anchor [{start},{end}) overlaps the previous edit "
                f"(source position {src_pos})")
        last_start_of[owner] = start
        if end > len(source):
            raise EditMapError(
                f"{r.get('finding_id')}: anchor end {end} past the paragraph "
                f"({len(source)} chars)")
        expected = str(a.get("delete_text") or "")
        if source[start:end] != expected:
            raise EditMapError(
                f"{r.get('finding_id')}: source[{start}:{end}] is "
                f"{source[start:end]!r}, anchor says {expected!r}")
        if start > src_pos:
            gap = source[src_pos:start]
            segs.append(Segment(src_pos, start, acc_pos, acc_pos + len(gap),
                                gap))
            acc_pos += len(gap)
        ins = str(a.get("insert_text") or "")
        segs.append(Segment(start, end, acc_pos, acc_pos + len(ins), ins,
                            owner=owner, row_ids=(fid,)))
        acc_pos += len(ins)
        src_pos = end
    if src_pos < len(source):
        tail = source[src_pos:]
        segs.append(Segment(src_pos, len(source), acc_pos, acc_pos + len(tail),
                            tail))
    return segs


def accepted_of(segments: Sequence[Segment]) -> str:
    return "".join(s.text for s in segments)


@dataclass
class EditMap:
    """Every paragraph's segments, keyed by para_id, plus the id->owner index
    the settle loop uses to remove absorbed rows from its working set."""

    paragraphs: dict[str, list[Segment]] = field(default_factory=dict)
    owner_of: dict[str, str] = field(default_factory=dict)
    # Paragraphs whose rows could not be mapped, with the reason. The settle
    # loop treats a residual in such a paragraph as unresolvable by arithmetic.
    unmapped: dict[str, str] = field(default_factory=dict)
    # Paragraphs mapped only by leaving out overlapping anchors (row ids).
    skipped: dict[str, list[str]] = field(default_factory=dict)

    def accepted(self, para_id: str) -> str | None:
        segs = self.paragraphs.get(para_id)
        return accepted_of(segs) if segs is not None else None

    def to_json(self) -> dict[str, Any]:
        return {"schema_version": EDITMAP_SCHEMA_VERSION,
                "paragraphs": {pid: [s.to_json() for s in segs]
                               for pid, segs in self.paragraphs.items()},
                "owner_of": dict(self.owner_of),
                "unmapped": dict(self.unmapped),
                "skipped": {k: list(v) for k, v in self.skipped.items()}}

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "EditMap":
        return cls(
            paragraphs={pid: [Segment.from_json(s) for s in segs]
                        for pid, segs in (d.get("paragraphs") or {}).items()},
            owner_of=dict(d.get("owner_of") or {}),
            unmapped=dict(d.get("unmapped") or {}),
            skipped={k: list(v) for k, v in (d.get("skipped") or {}).items()})

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.write_text(json.dumps(self.to_json(), indent=1, ensure_ascii=False),
                     encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> "EditMap":
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))


def owners_index(rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """finding_id -> working key id, where the key id is the FIRST row's
    finding_id for each `row_key`. Minimal-region siblings, and a duplicate
    the validator rejected beside its applied twin, all resolve to one owner."""
    first: dict[tuple, str] = {}
    index: dict[str, str] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        fid = str(r.get("finding_id") or "")
        if not fid:
            continue
        key = row_key(r)
        head = first.setdefault(key, base_id(fid))
        index[fid] = head
    return index


def build_editmap(source_paras: Mapping[str, str],
                  rows: Iterable[Mapping[str, Any]]) -> EditMap:
    """The whole document's edit map from its canonical paragraphs and its
    findings.json rows. A paragraph whose applied rows contradict its source
    text (an anchor that no longer matches — a build over a different source)
    is recorded in `unmapped` with the reason, never silently mapped wrong."""
    rows = [r for r in rows if isinstance(r, dict)]
    owner_of = owners_index(rows)
    by_para: dict[str, list[dict[str, Any]]] = {}
    for r in edit_rows(rows):
        by_para.setdefault(str(r.get("para_id", "")), []).append(r)
    em = EditMap(owner_of=owner_of)
    for pid, text in source_paras.items():
        rows_here = by_para.get(pid, ())
        try:
            em.paragraphs[pid] = paragraph_map(text, rows_here,
                                               owner_of=owner_of)
            continue
        except EditMapError as e:
            first = str(e)
        # Second pass, tolerating overlapping anchors: the reassembler applied
        # ONE of an overlapping pair, and the caller's drift check (the docx
        # accepted view against this reconstruction) is what decides whether
        # the tolerant map describes the real text.
        skipped: list[str] = []
        try:
            segs = paragraph_map(text, rows_here, owner_of=owner_of,
                                 skipped=skipped)
        except EditMapError:
            em.unmapped[pid] = first
            continue
        em.paragraphs[pid] = segs
        em.skipped[pid] = skipped
    return em


# ---- translating an accepted range back to the source --------------------------

@dataclass(frozen=True)
class Resolution:
    """Where an accepted-text range lives in the source, and who owns it.

    `case` is one of `untouched` (entirely in text no edit changed — the
    residual becomes a NEW row on `source_span`), `inside` (entirely within one
    owner's replacement), or `straddle` (crosses at least one edit boundary).
    For the two owned cases `owners` names every owner the range touches and
    `widened`/`source_widened` are the accepted and source extents a composite
    must cover to absorb them whole."""

    case: str
    source_span: tuple[int, int] | None
    owners: tuple[str, ...]
    widened: tuple[int, int]
    source_widened: tuple[int, int]
    absorbed_rows: tuple[str, ...]


def _acc_to_src(segments: Sequence[Segment], acc: int) -> int | None:
    """The source offset for an accepted offset that lies in untouched text or
    on an owned segment's boundary; None for one strictly inside a replacement."""
    for s in segments:
        if s.acc_start <= acc <= s.acc_end:
            if s.untouched:
                return s.src_start + (acc - s.acc_start)
            if acc == s.acc_start:
                return s.src_start
            if acc == s.acc_end:
                return s.src_end
            # inside a replacement — fall through to a later segment only if
            # this is a zero-width boundary case handled above
            return None
    if segments and acc == segments[-1].acc_end:
        return segments[-1].src_end
    return None


def locate(segments: Sequence[Segment], acc_lo: int, acc_hi: int) -> Resolution:
    """Resolve the accepted range [acc_lo, acc_hi) — see `Resolution`.

    Widening: every owned segment the range overlaps pulls in ALL segments of
    that owner (a split row is one decision), and the widened range is re-
    checked until it stops growing. A zero-width range (a pure insertion the
    walker suggests) overlapping nothing owned is untouched at its point."""
    if not segments:
        raise ValueError("no segments for this paragraph")
    total = segments[-1].acc_end
    acc_lo = max(0, min(acc_lo, total))
    acc_hi = max(acc_lo, min(acc_hi, total))

    def overlapping(lo: int, hi: int) -> list[Segment]:
        if lo == hi:
            # An insertion point touches the segment it sits inside; on a
            # boundary between two, it touches an OWNED neighbour (editing
            # at the edge of a replacement composes with that replacement).
            hits = [s for s in segments
                    if (s.acc_start < lo < s.acc_end)
                    or (s.acc_start == lo == s.acc_end)]
            if not hits:
                hits = [s for s in segments
                        if (s.acc_start == lo or s.acc_end == lo)
                        and not s.untouched]
            if not hits:
                hits = [s for s in segments
                        if s.acc_start <= lo <= s.acc_end][:1]
            return hits
        return [s for s in segments if s.acc_start < hi and lo < s.acc_end]

    wa0, wa1 = acc_lo, acc_hi
    owners: list[str] = []
    while True:
        hit = overlapping(wa0, wa1)
        owned = [s for s in hit if not s.untouched]
        new_owners: list[str] = []
        for s in owned:
            if s.owner not in owners and s.owner not in new_owners:
                new_owners.append(s.owner)  # type: ignore[arg-type]
        owners.extend(new_owners)
        if not owned:
            break
        lo = min([wa0] + [s.acc_start for s in segments if s.owner in owners])
        hi = max([wa1] + [s.acc_end for s in segments if s.owner in owners])
        if (lo, hi) == (wa0, wa1) and not new_owners:
            break
        wa0, wa1 = lo, hi

    if not owners:
        s0 = _acc_to_src(segments, acc_lo)
        s1 = _acc_to_src(segments, acc_hi)
        if s0 is None or s1 is None:                       # pragma: no cover
            raise ValueError("untouched range failed to map")
        return Resolution("untouched", (s0, s1), (), (acc_lo, acc_hi),
                          (s0, s1), ())

    ws0 = _acc_to_src(segments, wa0)
    ws1 = _acc_to_src(segments, wa1)
    if ws0 is None or ws1 is None:                         # pragma: no cover
        raise ValueError("widened range did not land on a boundary")
    # An owner's pure DELETION is a zero-width accepted segment: it sits at
    # an accepted offset shared with the untouched text around it, so the
    # boundary arithmetic above can land on the untouched neighbour and stop
    # short of the deleted source. The source extent of the composite is the
    # union of every absorbed owner's source spans, whatever the accepted
    # boundaries say (Redding body-0290: a flight row that replaced "is a
    # venue where" with "lets" AND deleted "can begin to" — the composite
    # covered the first and resurrected the second).
    owned_src = [s for s in segments if s.owner in owners]
    ws0 = min([ws0] + [s.src_start for s in owned_src])
    ws1 = max([ws1] + [s.src_end for s in owned_src])
    rows = tuple(rid for s in segments if s.owner in owners
                 for rid in s.row_ids)
    inside = [s for s in overlapping(acc_lo, acc_hi)]
    case = "inside" if (len(inside) == 1 and not inside[0].untouched) \
        else "straddle"
    return Resolution(case, None, tuple(owners), (wa0, wa1), (ws0, ws1), rows)


@dataclass(frozen=True)
class Composite:
    """A settlement expressed as ONE edit on the source: replace
    source[ws0:ws1] with `text`, absorbing every row in `absorbed`. `before` is
    the accepted text the widened range held (what the reader saw), so a fact
    or length guard compares what the SETTLEMENT changed, not what the original
    edit did."""

    src_start: int
    src_end: int
    text: str
    before: str
    owners: tuple[str, ...]
    absorbed: tuple[str, ...]
    case: str
    # The accepted-text extent `before` came from, so a guard can look at the
    # characters on either side of the settlement (an artifact like `”.` is
    # a BOUNDARY between the replacement and the text after it).
    acc_start: int = 0
    acc_end: int = 0

    @property
    def source_span(self) -> tuple[int, int]:
        return self.src_start, self.src_end


def compose(segments: Sequence[Segment], acc_lo: int, acc_hi: int,
            replacement: str) -> Composite:
    """The composite that applies `replacement` to accepted[acc_lo:acc_hi].

    Untouched range: a plain new edit on the mapped source span. Owned range:
    the widened accepted region with the replacement spliced in becomes the
    revised replacement for the union of the owners' source spans, and every
    owner row is absorbed. Text outside the widened region is never touched."""
    res = locate(segments, acc_lo, acc_hi)
    acc = accepted_of(segments)
    wa0, wa1 = res.widened
    before = acc[wa0:wa1]
    if res.case == "untouched":
        s0, s1 = res.source_span  # type: ignore[misc]
        return Composite(s0, s1, replacement, before, (), (), "untouched",
                         acc_start=wa0, acc_end=wa1)
    text = acc[wa0:acc_lo] + replacement + acc[acc_hi:wa1]
    ws0, ws1 = res.source_widened
    return Composite(ws0, ws1, text, before, res.owners, res.absorbed_rows,
                     res.case, acc_start=wa0, acc_end=wa1)


def sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """[lo, hi) of the sentence of `text` spanning start..end — bounded by
    ., !, ? or the paragraph edges. The same rule galley.verify applies."""
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    lo = 0
    for m in range(start - 1, -1, -1):
        if text[m] in ".!?":
            lo = m + 1
            break
    hi = len(text)
    for m in range(end, len(text)):
        if text[m] in ".!?":
            hi = m + 1
            break
    return lo, hi


def as_row(source: str, comp: Composite, *, para_id: str, error_type: str,
           explanation: str = "", confidence: str = "high",
           extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A findings row (import-findings / replay shape) that applies `comp` to
    the source paragraph. original_text is the MINIMAL span: the composite's
    source span, widened by one character on each side only when it is empty
    (a pure insertion needs something to anchor on), with its occurrence in
    the paragraph; corrected_text is that span replaced. Quoting the whole
    sentence instead dragged unchanged text into the row — a period another
    row deletes — and certify's artifact scan, which reads row text, failed
    a clean deliverable on it."""
    lo, hi = comp.src_start, comp.src_end
    if lo == hi:
        lo = max(0, lo - 1)
        hi = min(len(source), hi + 1)
    if lo == hi:                                            # empty paragraph
        lo, hi = 0, len(source)
    quote = source[lo:hi]
    corrected = (source[lo:comp.src_start] + comp.text
                 + source[comp.src_end:hi])
    occurrence = source[:lo].count(quote) + 1 if quote else 1
    row: dict[str, Any] = {
        "para_id": para_id, "original_text": quote, "occurrence": occurrence,
        "corrected_text": corrected, "error_type": error_type,
        "explanation": explanation, "confidence": confidence,
    }
    if extra:
        row.update({k: v for k, v in extra.items() if v not in (None, "")})
    return row


def rows_from_findings(findings: Iterable[Any], applied_ids: Iterable[str],
                       queried_ids: Iterable[str] = ()) -> list[dict[str, Any]]:
    """findings.json-shaped rows from Finding objects and the reassembler's
    applied/queried id sets — the same shape reporting.write_findings_json
    writes, so a map built in finish() equals one built from the file."""
    import dataclasses
    applied = set(applied_ids)
    queried = set(queried_ids)
    rows: list[dict[str, Any]] = []
    for f in findings:
        d = dataclasses.asdict(f)
        d["anchor"] = dataclasses.asdict(f.anchor) if f.anchor else None
        d["applied"] = f.finding_id in applied
        d["queried"] = f.finding_id in queried
        rows.append(d)
    return rows


def write_editmap(out_dir: str | Path, source_paras: Mapping[str, str],
                  rows: Iterable[Mapping[str, Any]]) -> Path:
    """Build and write `editmap.json` beside a build's findings.json."""
    em = build_editmap(source_paras, rows)
    return em.save(Path(out_dir) / EDITMAP_NAME)


def load_or_build(run_dir: str | Path, source_paras: Mapping[str, str],
                  rows: Iterable[Mapping[str, Any]]) -> EditMap:
    """The run's editmap.json when it is present AND describes these rows;
    otherwise a map built from the rows themselves (an older build)."""
    path = Path(run_dir) / EDITMAP_NAME
    rows = list(rows)
    if path.is_file():
        try:
            em = EditMap.load(path)
        except (OSError, ValueError, KeyError, TypeError):
            em = None
        if em is not None:
            ids = {str(r.get("finding_id") or "") for r in rows
                   if isinstance(r, dict)}
            known = {rid for segs in em.paragraphs.values() for s in segs
                     for rid in s.row_ids}
            if known <= ids and set(em.paragraphs) >= set(source_paras):
                return em
    return build_editmap(source_paras, rows)


__all__ = [
    "EDITMAP_NAME", "Composite", "EditMap", "EditMapError", "Resolution",
    "Segment", "accepted_of", "as_row", "base_id", "build_editmap",
    "collapse_region_siblings", "compose", "edit_rows", "load_or_build", "locate", "owners_index", "paragraph_map",
    "row_applied", "row_key", "rows_from_findings", "sentence_bounds",
    "write_editmap",
]
