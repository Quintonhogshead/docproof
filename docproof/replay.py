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
from typing import Any

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


def sanitize_corrected(text: str, variant: Variant | None) -> str:
    """Straight quotes to curly, the same context-aware curler ingest uses.
    Only ever called on corrected_text — never on original_text, which must
    stay byte-identical to what the manuscript actually contains for the
    anchor to find it at all."""
    return normalize_text(text, quotes=True, spaces=False, variant=variant)


def build_findings(rows: list[dict], *, variant: Variant | None,
                   error_dir: str | Path, remap_unchanneled: bool,
                   id_prefix: str = "import"
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

    Format-carrying rows are dropped here, unconditionally. A `title_italics`
    row records `original_text` as the whole sentence and `corrected_text` as
    just the span to italicise — a shape that only means "mark this span" when
    it rides the format channel. A row has no format slot the replay Finding
    preserves, so on a run that has not enabled `title_italics` the same pair
    routes to the ordinary change channel, where `shrink` reads it as "delete
    the sentence, keep the title" and eats real prose. The reject-all audit
    cannot catch it — rejecting a tracked deletion restores the text — so the
    only safe move is to never let a format row become a replay Finding.
    Italics are applied through the deliverable's own format channel
    (`galley.deliverable`), not by replaying a findings file."""
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
        if not isinstance(original_text, str) or not original_text:
            rejects.append({"index": i, "row": item,
                            "reason": "missing or non-string original_text"})
            continue
        if not isinstance(corrected_text, str):
            rejects.append({"index": i, "row": item,
                            "reason": "missing or non-string corrected_text"})
            continue

        raw_type = str(item.get("error_type") or "")
        # A format-carrying row cannot round-trip: its find/replace only mean
        # "mark this span" on the format channel, and replay has no format slot.
        # Drop it (by an explicit format field, or a format-channel type) rather
        # than let it become a prose-eating deletion. See the docstring.
        if item.get("format") or raw_type in format_keys:
            rejects.append({"index": i, "row": item,
                            "reason": f"format-carrying row ({raw_type or 'unknown'}) "
                            "dropped: formatting cannot round-trip through replay; "
                            "apply italics via the deliverable's format channel"})
            continue
        if remap_unchanneled:
            resolved_type, was_remapped = resolve_error_type(
                raw_type, registry, DEFAULT_IMPORT_TYPE)
        else:
            resolved_type = raw_type or DEFAULT_IMPORT_TYPE
            was_remapped = False
        if was_remapped:
            remapped += 1

        confidence = item.get("confidence", "medium")
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"
        try:
            occurrence = int(item.get("occurrence", 1) or 1)
        except (TypeError, ValueError):
            occurrence = 1

        findings.append(Finding(
            finding_id=f"f-{next(ids):04d}",
            chunk_id=str(item.get("chunk_id") or id_prefix),
            para_id=para_id,
            error_type=resolved_type,
            original_text=original_text,
            occurrence=occurrence,
            corrected_text=sanitize_corrected(corrected_text, variant),
            explanation=str(item.get("explanation") or ""),
            confidence=confidence,
            # status/anchor are deliberately NOT carried over from the row:
            # they belong to whatever run produced it (a different
            # content_hash may not even hold the same offsets). finish() and
            # validate_findings re-derive both fresh, the same as any other
            # source of findings.
        ))
    return findings, rejects, remapped


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
