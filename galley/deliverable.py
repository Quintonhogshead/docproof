"""The reviewed-manuscript deliverable — adjudicated GFindings, for real.

A finished Galley run had, until now, a case file and nothing a human could
open: every candidate the fleet raised lived only as JSON. This module is the
bridge from an :class:`~galley.adjudicate.AdjudicationResult` (or a plain list
of adjudicated :class:`~galley.contracts.GFinding`) to a real tracked-changes
``.docx``, driven through DocProof's own :func:`~docproof.pipeline.finish` at
$0 — no provider, no API call.

The shape mirrors how a mock/scripted review already reaches ``finish()``
(``docproof review --mock-findings``, ``galley.adapters.docproof_ladder``'s own
throwaway runs): ``prepare()`` an ordinary :class:`~docproof.pipeline.Prepared`
from the job's source document, hand ``finish()`` a list of already-decided
``docproof.models.Finding`` objects instead of running any detector, and let
DocProof's own validator, sweeps, and reassembler do the rest. Every *paid*
analyzer pass is forced off first (see :func:`zero_cost_config`) — DocProof's
own free, deterministic passes (the house-style sweeps, the consistency scan,
the spell scan) still run, exactly as they would behind any $0 review.

Two invariants:

* **Never a seeded manuscript.** :func:`build_manuscript_deliverable` calls
  :func:`galley.seeding.assert_deliverable` before touching anything else.
* **A stale span is a warning, not a crash.** A GFinding whose recorded span no
  longer anchors in the manuscript's current text — the paragraph moved, the
  wave that raised it read a different revision — is dropped and named in
  :attr:`DeliverableResult.dropped`, never allowed to sink the whole run.

Sibling-work note: a companion CLI verb (``docproof import-findings``, a
different worktree) is building similar GFinding -> ``docproof.Finding``
conversion machinery for its own entry point. This module's conversion
(:func:`gfinding_to_finding`) is kept small and separately named so the two
can be reconciled later without either blocking the other.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from docproof.config import Config
from docproof.error_registry import load_error_types, shipped_keys
from docproof.models import Finding, Usage
from docproof.pipeline import Outputs, finish, prepare
from docproof.replay import WordCountDelta, word_count_delta_guard

from galley.adapters.docproof_ladder import DEFAULT_ERROR_DIR
from galley.contracts import GFinding, Manuscript
from galley.seeding import assert_deliverable

if TYPE_CHECKING:
    from galley.adjudicate import AdjudicationResult

log = logging.getLogger("galley.deliverable")

# What a rebuilt findings.json envelope carries so `galley certify` can tell a
# legitimate $0 rebuild of adjudicated findings from a run whose detectors
# silently never billed. ``judge_model`` is the same slot ``import-judgments``
# marks itself in; ``rebuild`` says where the paid spend actually lives —
# the case file the orchestrator saves beside this deliverable — so certify
# reconciles the budget against that ledger instead of this envelope's $0.
REBUILD_MARKER: dict = {
    "judge_model": "galley:rebuild",
    "rebuild": {"from": "casefile", "paid_spend_recorded_in": "casefile.json"},
}
# finish() writes the manuscript into a scratch directory under out_dir and
# it is promoted only after the word-count guard passes, so a refused build
# never leaves a manuscript in out_dir for a caller to ship.
_STAGING = ".deliverable-staging"


def zero_cost_config(cfg: Config) -> Config:
    """A deep copy of ``cfg`` with every paid analyzer pass forced off.

    Galley's own adjudication (arbitration, and the panel screen when a
    provider is available) already decided what belongs in the manuscript;
    re-running any of DocProof's model-backed passes here would both cost
    money and second-guess a decision that was already made. DocProof's free,
    deterministic passes — the house-style sweeps, the consistency scan, the
    spell scan, the residual-coverage and recurrence-propagation post-passes —
    are untouched: they make no API call and no provider is required for them
    to run, exactly as they do on any ordinary ``--mock-findings`` review.

    ``ensemble`` has no plain ``enabled`` switch (it is computed from whether
    any detector is configured), so it is disabled by clearing ``detectors``
    rather than by attribute assignment.
    """

    z = cfg.model_copy(deep=True)
    z.ensemble.detectors = []
    z.languagetool.enabled = False
    z.sapling.enabled = False
    z.repair.enabled = False
    z.smoothing.enabled = False
    z.chapter_continuity.enabled = False
    z.low_confidence.confirm = False
    z.meaning_check.enabled = False
    z.fix_check.enabled = False
    z.candidate_screening.mode = "off"
    z.examination_graph.judgment.enabled = False
    return z


def ensure_format_types_enabled(
    cfg: Config, findings: list[Finding], error_dir: str | Path
) -> tuple[Config, list[str]]:
    """Guarantee every format-channel type the findings use is routable.

    A ``title_italics`` finding changes no characters — ``original_text`` and
    ``corrected_text`` are the same span. The validator routes it down the
    reversible tracked-format path (a ``w:rPrChange``, which "reject" undoes)
    ONLY when its ``error_type`` is in ``prepared.format_types``, which is built
    from the run's enabled ``error_types``. If the config that reaches the
    deliverable trimmed ``title_italics`` — a genre/posture pack narrowing the
    house-style group, a lean per-phase config across multiple passes — the same
    finding falls through to the ordinary change path, where ``shrink`` sees no
    diff and drops it as ``rejected_noop``: the italic is lost, silently.

    The findings are already adjudicated; whether ``title_italics`` ran as a
    *detector* is beside the point (paid passes are off here anyway). What has
    to hold is that it can be *applied*. So any format-channel key present among
    the findings but absent from ``cfg.error_types`` is appended as its own
    group. Returns the (possibly unchanged) config and the keys it enabled, for
    the caller to note. A no-op — byte-identical config — when every needed
    format type was already enabled.
    """

    used = {f.error_type for f in findings if f.error_type}
    if not used:
        return cfg, []
    registry = load_error_types(error_dir, sorted(shipped_keys(error_dir)))
    format_keys = {k for k, et in registry.items() if et.is_format}
    already = set(cfg.error_type_keys)
    missing = sorted((used & format_keys) - already)
    if not missing:
        return cfg, []
    z = cfg.model_copy(deep=True)
    # A fresh group of bare keys — a valid ``error_types`` entry shape, and one
    # that adds no duplicate key (every element is absent from ``already``).
    z.error_types = list(z.error_types) + [list(missing)]
    log.info("deliverable: auto-enabled format type(s) %s so their findings "
             "apply as tracked formatting rather than being dropped as no-ops",
             ", ".join(missing))
    return z, missing


def _occurrence_at(haystack: str, needle: str, start: int) -> int | None:
    """The 1-based occurrence number of ``needle`` at exactly ``start``.

    Walks the same stepping :func:`docproof.validator.find_nth` uses (each
    successive search starts one past the previous match), so the ``n`` this
    returns reproduces exactly the same offset through
    :func:`docproof.validator.anchor_offset`. Returns ``None`` when no
    occurrence sits at ``start`` — the paragraph's text has moved since the
    finding's span was recorded, so the caller should treat it as unanchorable
    rather than risk editing the wrong characters.

    Works uniformly for the empty needle (a pure insertion point, ``start ==
    end`` on the originating :class:`~galley.contracts.Span`): each successive
    empty-needle match advances by exactly one position, so the n-th occurrence
    sits at position ``n - 1`` — the same rule a non-empty needle follows.
    """

    pos = -1
    n = 0
    while True:
        pos = haystack.find(needle, pos + 1)
        if pos == -1 or pos > start:
            return None
        n += 1
        if pos == start:
            return n


def gfinding_to_finding(
    g: GFinding, ms: Manuscript, *, query: bool = False
) -> Finding | None:
    """Convert one adjudicated GFinding into a docproof Finding, or ``None``.

    ``None`` means the span no longer anchors: the paragraph is gone from
    ``ms``, or ``g.find`` no longer sits at ``g.span.start`` in it. The caller
    drops it with a coverage warning rather than failing the run.

    ``query=True`` sets ``force_query``, which routes the validator's query
    branch instead of the ordinary shrink-to-a-diff edit path (see
    ``docproof.validator.validate_findings``) — the finding becomes a margin
    comment, never a tracked change. Used for adjudication queries (arbitration
    overlap losers, panel rejects) and for a GFinding that names itself a query
    (``error_type`` or ``confidence`` literally ``"query"``) even though it held
    its span cleanly — the same self-declared-query convention
    ``galley.letter``'s open-queries section already honours.

    The finding's ``original_text``/``corrected_text`` are ``g.find``/
    ``g.replace`` themselves (already the minimal diff, per
    ``galley.adapters.docproof_ladder.gfindings_from_json``), not a wider
    sentence quote — ``validate_findings`` shrinks them again, a no-op on an
    already-minimal pair, and anchors on the correct occurrence via
    :func:`_occurrence_at` rather than trusting occurrence 1.
    """

    text = ms.paragraphs.get(g.span.para_id)
    if text is None:
        return None
    occurrence = _occurrence_at(text, g.find, g.span.start)
    if occurrence is None:
        return None
    return Finding(
        finding_id=g.id,
        chunk_id="",
        para_id=g.span.para_id,
        error_type=g.error_type or "galley",
        original_text=g.find,
        occurrence=occurrence,
        corrected_text=g.replace,
        explanation=g.note,
        confidence=g.confidence,
        force_query=query,
    )


def partition_for_deliverable(
    result: "AdjudicationResult",
) -> tuple[list[GFinding], list[GFinding]]:
    """Split an adjudication into ``(edits, queries)`` for the deliverable.

    ``result.queries`` (arbitration overlap losers, panel rejects) are always
    margin comments. ``result.kept`` may still hold a self-declared query — a
    GFinding whose own ``confidence`` or ``error_type`` is literally
    ``"query"`` — that arbitration and the panel never routed to the query
    channel because it held its span without contest; those become margin
    comments too, matching the convention ``galley.letter``'s open-queries
    section already applies when it surfaces "orphan" queries.
    """

    edits: list[GFinding] = []
    queries: list[GFinding] = list(result.queries)
    for f in result.kept:
        if f.confidence == "query" or f.error_type == "query":
            queries.append(f)
        else:
            edits.append(f)
    return edits, queries


def stamp_rebuild_marker(findings_json: str | Path) -> None:
    """Post-edit the envelope ``finish()`` wrote with :data:`REBUILD_MARKER`.

    ``finish()`` builds the envelope itself (``reporting.write_findings_json``)
    and takes no extra top-level fields, so the least invasive way to mark it
    is to re-read and re-write the JSON after the fact: the two keys are added
    at the top level and everything else is preserved byte-for-byte in
    content. A missing or unreadable file is left alone (the deliverable's
    own reporting is the thing that failed, and it says so itself)."""
    path = Path(findings_json)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    payload.update(copy.deepcopy(REBUILD_MARKER))
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                    encoding="utf-8")


def _promote(staging: Path, out: Path, outputs: Outputs) -> Outputs:
    """Move everything ``finish()`` left in ``staging`` up into ``out`` —
    replacing any same-named file from an earlier build — remove the scratch
    directory, and return ``outputs`` with every path rewritten to its final
    home. Called on success with the manuscript present, and on a refused
    build with the manuscript already deleted, so the diagnostics
    (findings.json, summary.md, the change log) survive either way."""
    out.mkdir(parents=True, exist_ok=True)
    for child in sorted(staging.iterdir()):
        dest = out / child.name
        if dest.is_dir() and not dest.is_symlink():
            shutil.rmtree(dest)
        elif dest.exists() or dest.is_symlink():
            dest.unlink()
        shutil.move(str(child), str(dest))
    shutil.rmtree(staging, ignore_errors=True)

    def relocate(value):
        if isinstance(value, Path) and staging in value.parents:
            return out / value.relative_to(staging)
        return value

    return dataclasses.replace(outputs, **{
        f.name: relocate(getattr(outputs, f.name))
        for f in dataclasses.fields(outputs)})


@dataclass
class DeliverableResult:
    """What building the manuscript deliverable produced.

    ``outputs`` is DocProof's own :class:`~docproof.pipeline.Outputs` — the
    tracked-changes ``.docx``, the findings/summary/change-log paths, and the
    applied/queried counts. ``dropped`` names every GFinding whose span no
    longer anchored, one line each, meant to ride the job's ``warnings`` list
    alongside the wave coverage notes.
    """

    outputs: Outputs
    dropped: list[str] = field(default_factory=list)


def build_manuscript_deliverable(
    adjudication: "AdjudicationResult",
    ms: Manuscript,
    *,
    source_path: str | Path,
    cfg: Config,
    out_dir: str | Path,
    error_dir: str | Path = DEFAULT_ERROR_DIR,
) -> DeliverableResult:
    """Drive ``finish()`` at $0 over one adjudicated Galley run.

    Guarded first by :func:`galley.seeding.assert_deliverable` — a seeded
    manuscript (deliberately planted errors, from the recall gauge) can never
    reach this far. ``adjudication.kept`` (minus any self-declared query, see
    :func:`partition_for_deliverable`) become tracked changes;
    ``adjudication.queries`` become margin comments. A finding whose span no
    longer anchors is dropped and named in the result rather than failing the
    run.

    Every paid analyzer pass is disabled (:func:`zero_cost_config`); no
    provider is constructed or required. DocProof's own free deterministic
    passes still run over the source document, same as any ordinary $0 review.

    The findings.json it leaves carries :data:`REBUILD_MARKER`, and the
    manuscript reaches ``out_dir`` only after the word-count guard passes: a
    refused build raises :class:`~docproof.replay.WordCountDelta` with the
    reports promoted and NO manuscript in ``out_dir``.
    """

    assert_deliverable(ms)
    edits, queries = partition_for_deliverable(adjudication)

    findings: list[Finding] = []
    dropped: list[str] = []
    for g in edits:
        f = gfinding_to_finding(g, ms, query=False)
        if f is None:
            dropped.append(
                f"{g.id} ({g.error_type}): span no longer anchors in the "
                f"manuscript; dropped from the deliverable")
            continue
        findings.append(f)
    for g in queries:
        f = gfinding_to_finding(g, ms, query=True)
        if f is None:
            dropped.append(
                f"{g.id} ({g.error_type}): query span no longer anchors in "
                f"the manuscript; dropped from the deliverable")
            continue
        findings.append(f)

    zcfg = zero_cost_config(cfg)
    # Format findings (title_italics) change no characters, so they must ride
    # the format channel or they vanish as no-ops. Guarantee their type is
    # enabled even if the run's config trimmed it — across multiple passes a
    # lean per-phase config easily drops it. Uses the un-normalized findings so
    # a query'd format finding still counts (it will simply anchor as a
    # comment). Errors are surfaced, not swallowed: an unroutable format finding
    # is a lost edit, not a cosmetic detail.
    zcfg, _ = ensure_format_types_enabled(zcfg, findings, error_dir)
    prepared = prepare(zcfg, str(source_path), error_dir)
    usage = Usage()
    out = Path(out_dir)
    staging = out / _STAGING
    if staging.exists():
        shutil.rmtree(staging)
    outputs = finish(prepared, findings, usage, zcfg,
                     out_dir=staging, source_path=source_path)
    stamp_rebuild_marker(outputs.findings_json)
    # Backstop: a formatting finding that reached the change channel by mistake
    # deletes the sentence it should only have marked. The reject-all audit is
    # blind to it (rejecting a deletion restores the text); a word-count delta
    # on the accepted view is not. Raises WordCountDelta rather than shipping a
    # manuscript short a sentence — and the manuscript is still in staging when
    # it does, so it is deleted there and never reaches out_dir.
    try:
        word_count_delta_guard(outputs.reviewed_path)
    except WordCountDelta:
        outputs.reviewed_path.unlink(missing_ok=True)
        _promote(staging, out, outputs)
        raise
    outputs = _promote(staging, out, outputs)
    return DeliverableResult(outputs=outputs, dropped=dropped)


__all__ = [
    "REBUILD_MARKER",
    "DeliverableResult",
    "build_manuscript_deliverable",
    "ensure_format_types_enabled",
    "gfinding_to_finding",
    "partition_for_deliverable",
    "stamp_rebuild_marker",
    "zero_cost_config",
]
