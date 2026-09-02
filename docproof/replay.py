"""Shared machinery for `docproof import-findings` and `docproof replay`.

Both verbs turn a findings list that already exists — produced outside
docproof entirely, or paid for by docproof itself on an earlier run — into a
$0 deliverable: shape-check every row, anchor it against the prepared
manuscript, sanitize what actually lands in the document, and run finish()
with every paid pass switched off.

The one place the two genuinely differ is provenance, and it changes how much
a row's own `error_type` can be trusted:

* `import-findings`' rows come from outside docproof — a subagent flight, a
  hand-built JSON file — and an `error_type` on them, if present at all,
  carries no reliable channel information. The Redding stand-in run hit this
  the hard way: a finding labelled "general_error" is channel:query in the
  shipped registry, so replaying it through the ordinary channel logic
  silently turns it into a margin comment instead of a tracked change, and
  the run needed a hand-rolled curated_fix.yaml to force those rows onto the
  edit channel. `import-findings` bakes that fix in: every row that does not
  already name a known channel:change type is relabelled to a dedicated
  edit-channel type (`imported_edit`, config/error_types/imported_edit.yaml)
  so a plain findings dump always lands as a tracked change, no YAML of your
  own required. See `resolve_error_type`.

* `replay`'s rows came FROM a docproof run — a per-run findings checkpoint or
  a finished run's findings.json — so their `error_type` is already one of
  ours, correctly channelled. Replaying must not relabel them: forcing a
  continuity question onto the edit channel on replay is not a faithful
  rebuild, it is a different (and wrong) deliverable. So replay leaves
  `error_type` alone and lets the full registry's channel assignments route
  it, exactly as any other run would.

Both sanitize `corrected_text` — straight quotes to curly, via the same
context-aware curler normalize.py uses on ingest — before it reaches the
document: a replayed row has round-tripped through at least one JSON file,
often written by a different model or a hand-editor, and straight quotes creep
in. `original_text` is never touched by this: sanitizing the anchor text but
not the manuscript's own characters at that span would make what should be a
quote-only touch-up read as a change to the whole surrounding sentence.
"""
from __future__ import annotations

import dataclasses
import itertools
import json
from pathlib import Path
from typing import Any, Mapping

from .config import Config
from .error_registry import ErrorType, load_error_types, shipped_keys
from .models import Finding
from .normalize import normalize_text
from .variants import Variant

DEFAULT_IMPORT_TYPE = "imported_edit"

_FINDING_FIELDS = {f.name for f in dataclasses.fields(Finding)}


def zero_paid_passes(cfg: Config) -> None:
    """Silence every model-backed stage `prepare()` or `finish()` could run,
    so an import/replay run makes no provider call regardless of what the
    loaded config says. Mirrors `docproof sweep`'s isolation (see cmd_sweep in
    __main__.py) but is a strict superset of it: sweep relies on several of
    these already defaulting to off in config/default.yaml, this zeroes them
    outright so the $0 guarantee does not depend on that.

    Deliberately leaves the deterministic, free stages — the scripted sweeps,
    the consistency scan, the spell scan — as the caller configured them: a
    replayed deliverable is meant to be a full deliverable, not a bare echo of
    the input rows."""
    # prepare() itself can call a provider, for these two:
    cfg.storysheet.enabled = False
    cfg.candidate_screening.mode = "off"
    # finish()'s paid stages. ensemble.enabled is a read-only property (true
    # whenever `detectors` is non-empty), so it is disabled by emptying the
    # list that actually drives it.
    cfg.ensemble.detectors = []
    cfg.sapling.enabled = False
    cfg.repair.enabled = False
    cfg.low_confidence.confirm = False
    cfg.smoothing.enabled = False
    cfg.continuity.enabled = False
    cfg.chapter_continuity.enabled = False
    cfg.meaning_check.enabled = False
    cfg.fix_check.enabled = False
    cfg.rounds.count = 1


def rows_from_payload(raw: Any) -> list[dict]:
    """Unwrap a contract envelope, a run_checkpoint.py file, a findings.json
    report, or a bare array — every one of those either IS a findings array or
    carries one under a `findings` key."""
    rows = raw.get("findings", raw) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ValueError(
            "expected a JSON array of findings, or an object with a "
            "'findings' array (the contract envelope, a findings.checkpoint.json, "
            "or a findings.json report)")
    return rows


def load_findings_file(path: str | Path) -> list[dict]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return rows_from_payload(raw)


def resolve_error_type(raw_type: str, registry: dict[str, ErrorType],
                       default_key: str) -> tuple[str, bool]:
    """(key, remapped). `raw_type` is kept as-is when it names a known
    channel:change type; otherwise it is replaced by `default_key` — this is
    the whole of the "don't hand-roll a curated_fix.yaml" fix. A type this run
    never loaded (an unrecognised key, or one that is query/format channel)
    is exactly what must not reach validate_findings unchanged: an unknown key
    is not in `query_types`/`format_types` either, so it would silently fall
    through to the edit channel anyway on THIS run, but a reader of the
    findings file would see the original (wrong) label."""
    et = registry.get(raw_type) if raw_type else None
    if et is not None and et.channel == "change":
        return raw_type, False
    return default_key, True


def _swept_and_map(pre: str, edits: list[tuple[int, int, str]]):
    """Apply `edits` (sorted, non-overlapping (start, end, insert) spans in
    pre-sweep coordinates) to `pre`, returning the swept text and, for each
    swept-text index, the pre-sweep index it maps to and whether it is a char an
    edit INSERTED (so a caller can tell a real overlap from an adjacency).

    The map has one extra trailing entry (== len(pre)) so a half-open end index
    maps too."""
    S: list[str] = []
    sw2pre: list[int] = []
    inserted: list[bool] = []
    i = 0
    for s, e, ins in sorted(edits):
        for j in range(i, s):
            S.append(pre[j]); sw2pre.append(j); inserted.append(False)
        for ch in ins:
            S.append(ch); sw2pre.append(s); inserted.append(True)
        i = e
    for j in range(i, len(pre)):
        S.append(pre[j]); sw2pre.append(j); inserted.append(False)
    sw2pre.append(len(pre))
    return "".join(S), sw2pre, inserted


def _minimal_diff(original: str, corrected: str) -> tuple[int, str, str]:
    """The single (offset, deleted, inserted) that turns `original` into
    `corrected` by trimming the shared prefix and suffix."""
    a, b = original, corrected
    p = 0
    while p < len(a) and p < len(b) and a[p] == b[p]:
        p += 1
    sa, sb = len(a), len(b)
    while sa > p and sb > p and a[sa - 1] == b[sb - 1]:
        sa -= 1; sb -= 1
    return p, a[p:sa], b[p:sb]


def reanchor_after_sweeps(rows: list[dict], paragraphs, sweep_findings
                          ) -> tuple[list[dict], int]:
    """Re-express imported rows written against POST-sweep text so they anchor
    against the pre-sweep manuscript (P2-11).

    A curated row whose `original_text` was captured after the deterministic
    sweeps ran (an ellipsis collapsed, a hyphen turned to an en-dash, a `:00`
    added before a lowered `am`) will not be found in the pre-sweep canonical
    text and lands as `rejected_no_anchor` — so the row has to be hand-built as a
    micro-span against pre-sweep text. This resolves each such row against the
    swept text instead and rewrites it to the equivalent pre-sweep quote, so a
    findings file exported after a swept run replays without hand-editing.

    Safe by construction: a row is rewritten ONLY when its own edit lands on
    characters no sweep touched (the disjoint case — every reported site: a fix
    ADJACENT to a sweep, never on it). A row whose edit overlaps a sweep is left
    exactly as it was for the ordinary arbitration to rule on, so this never
    composes a garbled third version with a sweep. Returns (rows, adjusted)."""
    by_id = {p.para_id: p.text for p in paragraphs}
    # Validated sweep edits per paragraph, as (start, end, insert) in pre coords.
    edits_by_para: dict[str, list[tuple[int, int, str]]] = {}
    for f in sweep_findings:
        a = getattr(f, "anchor", None)
        if a is None or f.status != "validated":
            continue
        edits_by_para.setdefault(f.para_id, []).append(
            (a.start, a.end, a.insert_text))
    out: list[dict] = []
    adjusted = 0
    for row in rows:
        if not isinstance(row, dict):
            out.append(row); continue
        pid = row.get("para_id")
        original = row.get("original_text")
        pre = by_id.get(pid)
        edits = edits_by_para.get(pid)
        if (not edits or not isinstance(original, str) or not original
                or pre is None or original in pre):
            out.append(row); continue          # anchors pre-sweep already, or n/a
        swept, sw2pre, inserted = _swept_and_map(pre, edits)
        idx = swept.find(original)
        if idx == -1:
            out.append(row); continue          # not a post-sweep quote either
        d_off, deleted, ins = _minimal_diff(original, row.get("corrected_text", ""))
        e_lo, e_hi = idx + d_off, idx + d_off + len(deleted)
        if any(inserted[k] for k in range(e_lo, e_hi)):
            out.append(row); continue          # edit lands ON a sweep — leave it
        pre_lo, pre_hi = sw2pre[idx], sw2pre[idx + len(original)]
        pre_quote = pre[pre_lo:pre_hi]
        off_in_pre = sw2pre[e_lo] - pre_lo
        if pre_quote[off_in_pre:off_in_pre + len(deleted)] != deleted:
            out.append(row); continue          # not the clean disjoint case
        new_corrected = (pre_quote[:off_in_pre] + ins
                         + pre_quote[off_in_pre + len(deleted):])
        occurrence = pre[:pre_lo].count(pre_quote) + 1
        new = dict(row)
        new["original_text"] = pre_quote
        new["corrected_text"] = new_corrected
        new["occurrence"] = occurrence
        out.append(new)
        adjusted += 1
    return out, adjusted


def sanitize_corrected(text: str, variant: Variant | None) -> str:
    """Straight quotes to curly, the same context-aware curler ingest uses.
    Only ever called on corrected_text — never on original_text, which must
    stay byte-identical to what the manuscript actually contains for the
    anchor to find it at all."""
    return normalize_text(text, quotes=True, spaces=False, variant=variant)


def build_findings(rows: list[dict], *, variant: Variant | None,
                   error_dir: str | Path, remap_unchanneled: bool,
                   id_prefix: str = "import",
                   format_round_trip: bool = False,
                   paragraphs: Mapping[str, str] | None = None
                   ) -> tuple[list[Finding], list[dict], int]:
    """Rows to `Finding`s. Anchoring is NOT done here — validate_findings (via
    a dry-run report) or finish() (for a real run) does that — this stage is
    shape-checking, error-type resolution, and sanitizing only.

    Returns `(findings, rejects, remapped_count)`. `rejects` are rows that
    failed basic shape validation (missing para_id/original_text/
    corrected_text, or not a JSON object at all), each with an index and a
    plain-text reason. An anchor failure is a different thing — the row was
    well-formed but its quote is not in the manuscript — and shows up later,
    as status "rejected_no_anchor" on the finding validate_findings returns.

    Format-carrying rows round-trip ONLY on a shipped format-channel type: a
    `title_italics` row records `original_text` as the whole sentence and
    `corrected_text` as just the span to italicise — a shape that only means
    "mark this span" on the format channel. `_import_or_replay` loads any such
    type named by the rows into the run so validate/finish route them as marks.
    A format payload on a type the shipped registry does NOT know as format
    channel is still rejected here: on the change channel that pair reads as
    "delete the sentence, keep the title" and eats real prose, which the
    reject-all audit cannot see (the word-count delta guard is the backstop)."""
    registry: dict[str, ErrorType] = {}
    if remap_unchanneled:
        registry = load_error_types(error_dir, sorted(shipped_keys(error_dir)))
    # Format-channel keys are needed on BOTH paths (replay too) to spot a
    # format row by its error type, so load them even when not remapping.
    format_registry = registry or load_error_types(
        error_dir, sorted(shipped_keys(error_dir)))
    format_keys = {k for k, et in format_registry.items() if et.is_format}

    ids = itertools.count(1)
    findings: list[Finding] = []
    rejects: list[dict] = []
    remapped = 0
    for i, item in enumerate(rows):
        if not isinstance(item, dict):
            rejects.append({"index": i, "reason": "not a JSON object"})
            continue
        # Tolerate a row shaped like finding_to_dict's superset (findings.json
        # adds applied/queried/unplaced; run_checkpoint entries are bare) —
        # only the fields build_findings itself understands are read here.
        para_id = item.get("para_id")
        original_text = item.get("original_text")
        corrected_text = item.get("corrected_text")
        if not isinstance(para_id, str) or not para_id:
            rejects.append({"index": i, "row": item,
                            "reason": "missing or non-string para_id"})
            continue
        if isinstance(original_text, str) and not original_text:
            # A pure insertion (a sweep's appended period, say) is a legal
            # finding, but an empty original_text cannot anchor. When the row
            # carries its serialized anchor and the caller handed us the
            # canonical paragraphs, re-express it as a whole-paragraph O→C —
            # the validator reduces that to the minimal insert. Purpura beta:
            # a run's own findings.json failed to round-trip on exactly this.
            anchor = item.get("anchor")
            ptext = (paragraphs or {}).get(para_id)
            if (isinstance(anchor, dict) and ptext
                    and isinstance(anchor.get("insert_text"), str)
                    and anchor.get("insert_text")
                    and isinstance(anchor.get("start"), int)
                    and anchor.get("end") == anchor.get("start")
                    and 0 <= anchor["start"] <= len(ptext)):
                start = anchor["start"]
                original_text = ptext
                corrected_text = (ptext[:start] + anchor["insert_text"]
                                  + ptext[start:])
                # The old row's occurrence indexed the empty span, not the
                # whole paragraph — carrying it over sends the validator
                # hunting a 38th copy of the paragraph.
                item = {k: v for k, v in item.items() if k != "occurrence"}
            else:
                rejects.append({"index": i, "row": item,
                                "reason": "empty original_text (pure "
                                "insertion) with no usable anchor/paragraph "
                                "to re-express it against"})
                continue
        elif not isinstance(original_text, str):
            rejects.append({"index": i, "row": item,
                            "reason": "missing or non-string original_text"})
            continue
        if not isinstance(corrected_text, str):
            rejects.append({"index": i, "row": item,
                            "reason": "missing or non-string corrected_text"})
            continue

        raw_type = str(item.get("error_type") or "")
        is_format_row = bool(item.get("format")) or raw_type in format_keys
        if is_format_row and not (format_round_trip
                                  and raw_type in format_keys):
            # Without `format_round_trip` — a caller that has NOT armed the
            # format channel — a format row's find/replace pair would route to
            # the change channel and read as "delete the sentence, keep the
            # span", eating real prose. The CLI path (`_import_or_replay`)
            # loads the row's format type into the run and opts in, so
            # italics DO round-trip there; a format payload on a non-format
            # type never has a safe route and is always dropped.
            rejects.append({"index": i, "row": item,
                            "reason": f"format-carrying row ({raw_type or 'unknown'}) "
                            "dropped: formatting cannot round-trip through "
                            "this caller; shipped format types (e.g. "
                            "title_italics) round-trip via import-findings/"
                            "replay, which arm the format channel"})
            continue
        if remap_unchanneled and not is_format_row:
            resolved_type, was_remapped = resolve_error_type(
                raw_type, registry, DEFAULT_IMPORT_TYPE)
        else:
            resolved_type = raw_type or DEFAULT_IMPORT_TYPE
            was_remapped = False
        if was_remapped:
            remapped += 1

        # A row that asks rather than corrects rides the query channel as a
        # margin comment, never as an edit: the author flagged it (queried /
        # force_query on the row), or its type is query-channel in the SHIPPED
        # registry — which this run's config may not have loaded (a final-replay
        # stage zeroes error_types), so without this an unknown query type
        # falls through to the edit channel and a held-back proposal silently
        # APPLIES. That inversion is the one outcome a downgrade must never
        # produce.
        et_shipped = format_registry.get(resolved_type)
        force_query = bool(item.get("force_query") or item.get("queried")
                           or (et_shipped is not None and et_shipped.is_query))

        confidence = item.get("confidence", "medium")
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"
        try:
            occurrence = int(item.get("occurrence", 1) or 1)
        except (TypeError, ValueError):
            occurrence = 1

        # I3 — replacements are text, never notes. An externally produced
        # row whose correction carries an aside to the editor ("(spell out
        # and hyphenate)") would ship that aside into the manuscript. The
        # note is stripped at intake, the same way the flight deck repairs
        # its proposals; a row that is NOTHING but a note is malformed.
        if remap_unchanneled and not force_query and not is_format_row:
            from .flights import strip_editorial_note
            cleaned, had_note = strip_editorial_note(corrected_text)
            if had_note:
                if not cleaned.strip() or cleaned == original_text:
                    rejects.append({"index": i, "row": item,
                                    "reason": "editorial note in place of a "
                                    "correction (nothing usable remains once "
                                    "the note is removed)"})
                    continue
                corrected_text = cleaned

        # Provenance a rebuilt deliverable must keep: which tracked-changes
        # author a lane's edit carries, a repair cluster's atomicity tag, and
        # the two margin-note flags. A replay that dropped `lane` re-attributed
        # a merged run's copy-edits to the proofreader.
        keep: dict[str, Any] = {}
        for field in ("lane", "cluster_id"):
            v = item.get(field)
            if isinstance(v, str) and v:
                keep[field] = v
        for field in ("silent", "withheld"):
            if item.get(field) is True:
                keep[field] = True

        findings.append(Finding(
            finding_id=f"f-{next(ids):04d}",
            chunk_id=str(item.get("chunk_id") or id_prefix),
            para_id=para_id,
            error_type=resolved_type,
            original_text=original_text,
            occurrence=occurrence,
            corrected_text=sanitize_corrected(corrected_text, variant),
            explanation=str(item.get("explanation") or item.get("comment")
                            or ""),
            confidence=confidence,
            force_query=force_query,
            # status/anchor are deliberately NOT carried over from the row:
            # they belong to whatever run produced it (a different
            # content_hash may not even hold the same offsets). finish() and
            # validate_findings re-derive both fresh, the same as any other
            # source of findings.
            **keep,
        ))
    return findings, rejects, remapped


@dataclasses.dataclass
class RebuildResult:
    """What `rebuild_from_rows` did. `outputs` is None on a dry run."""

    prepared: Any
    findings: list[Finding]
    rejects: list[dict]
    remapped: int
    checked: list[Finding]
    tally: dict[str, int]
    reanchored: int = 0
    outputs: Any = None


def rebuild_from_rows(cfg: Config, *, manuscript: str | Path, rows: list[dict],
                      error_dir: str | Path, remap_unchanneled: bool,
                      id_prefix: str, after_sweeps: bool = False,
                      dry_run: bool = False) -> RebuildResult:
    """The $0 rebuild both `import-findings`/`replay` and the settle loop
    share: silence every paid pass, prepare the manuscript, shape the rows into
    findings, validate them, and (unless `dry_run`) run finish() to write the
    deliverable, guarding the word count afterwards. `cfg.output_dir` is where
    it writes. Raises IngestError/FileNotFoundError/ValueError from prepare and
    WordCountDelta from the guard; the caller reports."""
    from .pipeline import finish, prepare
    from .validator import validate_findings

    # Zeroed BEFORE prepare(): storysheet and candidate_screening are the two
    # stages prepare() itself can spend on, and both must be off no matter what
    # the loaded config says — see zero_paid_passes' docstring.
    zero_paid_passes(cfg)
    # Imported/replayed rows are curated input — hand-written, or a prior run's
    # output that already faced the guard once. The overreach guard's threat
    # model (a live model fabricating a rewrite mid-run) does not apply, and a
    # composed multi-fix row legitimately spans more than 64 characters. The
    # word-count delta guard below stays as the prose-eating backstop.
    cfg.edit_guard.enabled = False

    # Rows on a SHIPPED format-channel type (title_italics) round-trip by
    # loading that type into the run, so validate/finish route them down the
    # format channel — a mark, never a deletion.
    registry = load_error_types(error_dir, sorted(shipped_keys(error_dir)))
    fmt_in_rows = sorted({str(r.get("error_type") or "") for r in rows
                          if isinstance(r, dict)
                          and registry.get(str(r.get("error_type") or ""))
                          is not None
                          and registry[str(r.get("error_type"))].is_format})
    missing_fmt = [k for k in fmt_in_rows if k not in set(cfg.error_type_keys)]
    if missing_fmt:
        cfg.error_types = list(cfg.error_types) + [missing_fmt]

    prepared = prepare(cfg, str(manuscript), error_dir, dry_run=dry_run)

    reanchored = 0
    if after_sweeps:
        swept = validate_findings(
            list(prepared.sweep_findings), prepared.doc, cfg.min_confidence,
            query_types=prepared.query_types, format_types=prepared.format_types)
        rows, reanchored = reanchor_after_sweeps(rows, prepared.doc.paragraphs,
                                                 swept)

    findings, rejects, remapped = build_findings(
        rows, variant=prepared.variant, error_dir=error_dir,
        remap_unchanneled=remap_unchanneled, id_prefix=id_prefix,
        format_round_trip=True,
        paragraphs={p.para_id: p.text for p in prepared.doc.paragraphs})
    checked = validate_findings(findings, prepared.doc, cfg.min_confidence,
                                query_types=prepared.query_types,
                                format_types=prepared.format_types)
    tally: dict[str, int] = {}
    for f in checked:
        tally[f.status] = tally.get(f.status, 0) + 1
    result = RebuildResult(prepared=prepared, findings=findings,
                           rejects=rejects, remapped=remapped, checked=checked,
                           tally=tally, reanchored=reanchored)
    if dry_run:
        return result
    from .models import Usage
    usage = Usage()
    outputs = finish(prepared, findings, usage, cfg,
                     out_dir=Path(cfg.output_dir), source_path=str(manuscript))
    # A formatting row that reached the change channel deletes the sentence it
    # should only have marked, and the reject-all audit cannot see it. Catch it
    # by word count before calling the deliverable done.
    word_count_delta_guard(outputs.reviewed_path)
    result.outputs = outputs
    return result


class WordCountDelta(Exception):
    """A replayed/mock deliverable removed more prose than a proofread should.

    Message is user-facing. Raised by :func:`word_count_delta_guard`."""

    def __init__(self, reject_words: int, accept_words: int, reason: str):
        self.reject_words = reject_words
        self.accept_words = accept_words
        super().__init__(reason)


def word_count_delta_guard(reviewed_path: str | Path, *,
                           max_drop_ratio: float = 0.02, min_drop: int = 25
                           ) -> tuple[int, int]:
    """Refuse a finished replay/mock document that ate prose it should not have.

    The backstop for a format-carrying row that slips past
    :func:`build_findings`' drop-filter (or any other misroute that lands as a
    deletion): such a row records the whole sentence as its ``original_text``
    and only a span as its ``corrected_text``, so on the change channel it reads
    as "delete the sentence, keep the span" and removes real words. The
    reject-all audit cannot see it — rejecting a tracked deletion restores the
    text, so that view still matches the ingest — but the ACCEPTED view is now
    short a sentence.

    Counts words in both views of the finished document and raises
    :class:`WordCountDelta` when accepting the changes dropped more than
    ``min_drop`` words AND more than ``max_drop_ratio`` of the ingested count.
    Both conditions together: the ratio keeps a real book from tripping on a
    handful of legitimate deletions, the floor keeps a tiny document from
    tripping on one. A proofread only ever nets a few words down; a
    sentence-eating misroute clears both bars at once. Returns
    ``(reject_words, accept_words)`` on success.
    """
    from .reassembler import paragraph_view_text
    from .utils.xml_helpers import DocxPackage, walk_package

    pkg = DocxPackage(reviewed_path)
    reject_words = accept_words = 0
    for wp in walk_package(pkg):
        reject_words += len(paragraph_view_text(wp.element, "reject").split())
        accept_words += len(paragraph_view_text(wp.element, "accept").split())
    drop = reject_words - accept_words
    if drop > min_drop and drop > max_drop_ratio * max(reject_words, 1):
        raise WordCountDelta(
            reject_words, accept_words,
            f"Refusing to ship: accepting the changes removed {drop} words "
            f"({reject_words} → {accept_words}), far more than a proofread "
            f"should. This is the signature of a formatting row misapplied as a "
            f"deletion — investigate before delivering.")
    return reject_words, accept_words


__all__ = ["DEFAULT_IMPORT_TYPE", "zero_paid_passes", "rows_from_payload",
          "load_findings_file", "resolve_error_type", "sanitize_corrected",
          "build_findings", "WordCountDelta", "word_count_delta_guard"]
