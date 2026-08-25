"""The merge desk: cross-lane merging of two edit sets into one deliverable.

Galley runs two lanes over the same manuscript: a mechanical/proofread lane
(the ordinary review — sweeps, spelling, grammar, punctuation) and a copy-edit
lane (a rewrite pass that proposes whole-sentence fixes for weaker prose). Each
lane is a finished, independent findings set. This module reconciles the two
into ONE set of tracked changes, deciding — span by span — which lane's edit
survives where the two disagree.

The four claim rules, in priority order:

  (a) a composite/cluster (Finding.cluster_id, the repair channel's atomic
      multi-edit unit — see docproof/repair.py) claims its spans FIRST,
      ahead of every ordinary finding in either lane, and is ATOMIC: if any
      member is later rejected or withdrawn, the whole cluster goes with it.
      That enforcement is `repair.enforce_cluster_atomicity`, already generic
      (it keys purely on cluster_id, not on which pass produced the member) —
      this module only has to place cluster members first and hand the merged
      set to a caller that still runs the atomicity pass. (`pipeline.finish`
      does, unconditionally now — see the comment at its call site.)
  (b) a copy-edit rewrite may SUBSUME an overlapping mechanical fix, but only
      when the rewrite's own resulting sentence passes the $0 checks clean —
      the deterministic sweeps and (when installed) LanguageTool find nothing
      wrong with it. Any hit and the rewrite loses outright; LanguageTool
      being unavailable is not "clean" either — see `rewrite_is_clean`.
  (c) otherwise, on any span the two lanes both touch, the mechanical fix
      wins. This is also what happens to a copy-edit rewrite that failed (b).
  (d) two edits that do not overlap both land — this needs no code of its
      own, since the arbitration only ever restricts CONTESTED spans.

This module does not itself decide who "wins" and delete a finding. Instead —
matching finish()'s own philosophy (pipeline.py: "arbitration is order, not
deletion") — it produces one ORDERED list of pending findings, winner-first,
and hands it to `validator.validate_findings` (directly, for the ledger this
module reports, and again inside `pipeline.finish` for the real write) which
does the actual first-come-wins overlap accounting. Two traps that ordering
alone resolves for free, because `validate_findings`'/`_overlaps`' semantics
already cover them:

  * same-point insertions from both lanes composing into ",," — two zero-
    width insertions conflict exactly when they share a point (see
    `validator._overlaps`), so the later one in the ordering is rejected as
    an overlap, never composed.
  * a span containing an ellipsis losing to `sweep_ellipsis` — the built-in
    sweep findings are placed ahead of this module's ordering in
    `pipeline.finish`'s own `proposed` list (unchanged by this module), so a
    lane's edit on the same characters is rejected as an overlap there,
    exactly as any other pass loses to a sweep today.

A third trap — straight-vs-curly quote mismatches between lanes — is handled
by `validator.anchor_offset`'s existing punctuation fold: a finding whose
quoted `original_text` mismatches the manuscript's curly punctuation still
anchors, so this module (which re-derives every finding's span with that same
`anchor_offset`) sees the same span a straight- or curly-quoted finding would
report, and treats the two consistently.

Deliverable 2, the merged-result artifact scan, is `iterate_until_clean`: it
splices every surviving edit into its paragraph, runs
`candidate_screening.text_invariant_violation` over the result (the ",,"/
doubled-space/space-before-punctuation guard, never wired into the main
pipeline today), and — bounded — drops the losing edit and rechecks until the
merged paragraph is clean or the loop gives up and reports the hole loudly.
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .candidate_screening import text_invariant_violation
from .models import DocumentModel, Finding, ParagraphRef, index_paragraphs
from .validator import anchor_offset, shrink

log = logging.getLogger("docproof.mergedesk")

MECHANICAL = "mechanical"
COPYEDIT = "copyedit"

_FINDING_FIELDS = frozenset(f.name for f in dataclasses.fields(Finding))
_FINDING_REQUIRED = frozenset(
    f.name for f in dataclasses.fields(Finding)
    if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING)  # type: ignore[misc]


class MergeError(ValueError):
    """A raw finding dict is malformed — missing a required field, or the
    wrong shape entirely. Raised at load time, before anything is anchored."""


# --- loading and lane-tagging --------------------------------------------------

def tag_lane(raw: Sequence[dict | Finding], default_lane: str) -> list[Finding]:
    """Normalize a list of raw finding dicts (as findings.json / a run
    checkpoint writes them) or already-built `Finding` objects into `Finding`
    objects carrying `.lane`.

    A dict's own `"lane"` key wins when present (the flights/copy-edit lane is
    expected to tag its own output `lane: "copyedit"`); otherwise it takes
    `default_lane` — so an untagged findings file handed to `--mechanical`
    reads as the mechanical lane, matching the task's "treat untagged as
    mechanical" contract. `status` and `anchor`, if present in the dict, are
    dropped: every finding re-anchors from scratch at merge time (the source
    run's own anchoring is not trusted across two independent runs, e.g. of
    a manuscript in a different revision), so those fields are always
    "pending", None on the way in."""
    out: list[Finding] = []
    for item in raw:
        if isinstance(item, Finding):
            out.append(dataclasses.replace(
                item, lane=item.lane or default_lane,
                status="pending", anchor=None))
            continue
        if not isinstance(item, dict):
            raise MergeError(f"a finding must be an object, got {type(item).__name__}")
        d = dict(item)
        d.pop("status", None)
        d.pop("anchor", None)
        d.setdefault("lane", default_lane)
        if not d.get("lane"):
            d["lane"] = default_lane
        unknown = set(d) - _FINDING_FIELDS
        known = {k: v for k, v in d.items() if k in _FINDING_FIELDS}
        missing = _FINDING_REQUIRED - known.keys()
        if missing:
            raise MergeError(
                f"finding {d.get('finding_id', '?')!r} is missing required "
                f"field(s): {', '.join(sorted(missing))}")
        if unknown:
            log.debug("finding %s: ignoring unknown field(s) %s",
                     d.get("finding_id"), ", ".join(sorted(unknown)))
        out.append(Finding(**known))
    return out


# --- provisional anchoring (read-only; never claims a span) -------------------

@dataclass(frozen=True)
class _Span:
    start: int
    end: int


def _provisional_span(f: Finding, doc: DocumentModel) -> _Span | None:
    """Steps 1 and 2 of `validator.validate_findings` — locate the quote, shrink
    it to a minimal diff — replicated read-only, with no confidence gate, edit
    guard, or span bookkeeping. This is ONLY used to detect cross-lane overlap
    before the real arbitration; `validate_findings` (called on this module's
    ordered output, both here for the ledger and again in `pipeline.finish`
    for the write) is the sole source of truth for what actually lands."""
    para = index_paragraphs(doc).get(f.para_id)
    if para is None:
        return None
    s = anchor_offset(para.text, f.original_text, f.occurrence)
    if s == -1:
        return None
    pre, deleted, inserted = shrink(f.original_text, f.corrected_text)
    if not deleted and not inserted:
        return None
    return _Span(s + pre, s + pre + len(deleted))


def _overlaps(s1: int, e1: int, s2: int, e2: int) -> bool:
    """Mirrors `validator._overlaps` exactly — kept as a local copy rather than
    imported, since that name is private to its module. See its docstring
    there for the composition reasoning (same-point insertions conflict only
    with each other, an insertion into a span conflicts, abutting a span's end
    does not)."""
    if s1 == e1 and s2 == e2:
        return s1 == s2
    if s1 == e1:
        return s2 <= s1 < e2
    if s2 == e2:
        return s1 <= s2 < e1
    return s1 < e2 and s2 < e1


# --- rule (b): is a rewrite clean? ---------------------------------------------

def rewrite_is_clean(sentence: str, *, sweep_keys: Sequence[str] | None = None,
                     variant=None, ellipsis_style: str = "nbsp",
                     languagetool_dictionary: str = "en-US",
                     languagetool_lexicon: Sequence[str] = (),
                     check_languagetool: bool = True) -> tuple[bool, str]:
    """Whether a copy-edit rewrite's own sentence passes the $0 checks clean:
    every deterministic sweep is silent on it, and — when LanguageTool is
    installed — it proposes nothing either. Returns `(clean, reason)`; `reason`
    names the first hit, for the claim ledger.

    LanguageTool unavailable is deliberately NOT treated as clean: failing
    closed means an environment without the jar never silently favors the
    rewrite lane over the mechanical one just because it could not check."""
    from .sweeps import SWEEPS_BY_KEY, run_sweep_objects

    keys = sweep_keys if sweep_keys is not None else list(SWEEPS_BY_KEY)
    sweeps = [SWEEPS_BY_KEY[k] for k in keys if k in SWEEPS_BY_KEY]
    throwaway = ParagraphRef(para_id="__mergedesk__", part="", location="body",
                             text=sentence, style="Normal")
    findings, _reports = run_sweep_objects(
        [throwaway], sweeps, variant, ellipsis_style=ellipsis_style)
    if findings:
        return False, f"sweep {findings[0].error_type!r} fires on the rewrite"

    if check_languagetool:
        from . import languagetool
        if not languagetool.AVAILABLE:
            return False, "LanguageTool is not installed — failing closed"
        candidates = languagetool.propose(
            [throwaway], lexicon=languagetool_lexicon,
            dictionary=languagetool_dictionary)
        if candidates:
            return False, "LanguageTool flags the rewrite"
    return True, ""


# --- the claim ledger -----------------------------------------------------------

@dataclass(frozen=True)
class ClaimRecord:
    """One contested span and how it was settled."""
    para_id: str
    start: int
    end: int
    winner_id: str
    winner_lane: str
    loser_id: str
    loser_lane: str
    rule: str            # "cluster_atomic" | "rewrite_clean" | "mechanical_default"
    reason: str = ""      # the rewrite checker's reason, when rule == "mechanical_default"

    def to_json(self) -> dict:
        return dataclasses.asdict(self)


@dataclass
class MergeResult:
    """`findings` is winner-ordered and PENDING (unanchored, unvalidated) — the
    shape `pipeline.finish` takes as its `findings` argument. `validated` is
    the same set after this module's own `validate_findings` pass, offered so
    a caller (or a test) can inspect exactly what would land without invoking
    the full pipeline. `ledger` explains every contested span this module
    itself detected (an overlap between the two lanes); a span a cluster
    claims from an ordinary finding of either lane is settled by ordering
    alone and is not separately entered here — see `merge_lanes`."""
    findings: list[Finding]
    validated: list[Finding] = field(default_factory=list)
    ledger: list[ClaimRecord] = field(default_factory=list)


def merge_lanes(mechanical: Sequence[dict | Finding],
                copyedit: Sequence[dict | Finding],
                doc: DocumentModel, *,
                min_confidence: str = "medium",
                query_types: frozenset[str] = frozenset(),
                format_types: dict[str, str] | None = None,
                edit_guard=None,
                rewrite_checker: Callable[[str], tuple[bool, str]] | None = None,
                sweep_keys: Sequence[str] | None = None,
                variant=None, ellipsis_style: str = "nbsp") -> MergeResult:
    """Reconcile the mechanical and copy-edit findings sets into one ordered,
    winner-first list, per the four claim rules (see module docstring).

    `rewrite_checker`, if given, replaces `rewrite_is_clean` entirely — the
    seam a test uses to fake LanguageTool/sweep results without a JVM or a
    provider. It is called with one argument, the rewrite's `corrected_text`,
    and must return `(clean: bool, reason: str)`.

    Also runs `validator.validate_findings` once on the ordered list, purely
    to populate `MergeResult.validated`/build the ledger's winners with real
    anchors — this is diagnostic, not authoritative; the write path
    (`pipeline.finish`) re-validates the same ordered list alongside the
    manuscript's own sweep/consistency findings, which is what actually
    decides the deliverable."""
    from .validator import validate_findings

    mech = tag_lane(mechanical, MECHANICAL)
    ce = tag_lane(copyedit, COPYEDIT)
    checker = rewrite_checker or (lambda s: rewrite_is_clean(
        s, sweep_keys=sweep_keys, variant=variant, ellipsis_style=ellipsis_style))

    mech_spans = [(f, _provisional_span(f, doc)) for f in mech]
    ce_spans = [(f, _provisional_span(f, doc)) for f in ce]

    # (a) — clusters claim their spans first, atomic, regardless of lane.
    # `enforce_cluster_atomicity` (docproof/repair.py) is what actually
    # withdraws a partial cluster; this only has to make sure a cluster
    # member's span is never conceded to an ordinary finding by ARRIVING
    # later in the list validate_findings arbitrates.
    mech_cluster = [f for f, _ in mech_spans if f.cluster_id]
    ce_cluster = [f for f, _ in ce_spans if f.cluster_id]
    mech_rest = [(f, s) for f, s in mech_spans if not f.cluster_id]
    ce_rest = [(f, s) for f, s in ce_spans if not f.cluster_id]

    ledger: list[ClaimRecord] = []
    promoted: list[Finding] = []
    promoted_ids: set[str] = set()

    # (b)/(c) — for every copy-edit finding overlapping one or more mechanical
    # findings, one clean-rewrite check decides all of them: a rewrite is a
    # property of its own resulting sentence, not of which fix it contests.
    for ce_f, ce_span in ce_rest:
        if ce_span is None:
            continue
        contested = [(mf, ms) for mf, ms in mech_rest
                    if ms is not None and mf.para_id == ce_f.para_id
                    and _overlaps(ce_span.start, ce_span.end, ms.start, ms.end)]
        if not contested:
            continue
        clean, reason = checker(ce_f.corrected_text)
        if clean:
            promoted.append(ce_f)
            promoted_ids.add(ce_f.finding_id)
            for mf, _ms in contested:
                ledger.append(ClaimRecord(
                    ce_f.para_id, ce_span.start, ce_span.end,
                    ce_f.finding_id, COPYEDIT, mf.finding_id, MECHANICAL,
                    "rewrite_clean"))
        else:
            for mf, ms in contested:
                ledger.append(ClaimRecord(
                    mf.para_id, ms.start, ms.end,
                    mf.finding_id, MECHANICAL, ce_f.finding_id, COPYEDIT,
                    "mechanical_default", reason=reason))

    spans_by_id = {f.finding_id: s for f, s in mech_spans + ce_spans}
    for f in mech_cluster + ce_cluster:
        span = spans_by_id.get(f.finding_id)
        ledger.append(ClaimRecord(
            f.para_id, span.start if span else -1, span.end if span else -1,
            f.finding_id, f.lane or MECHANICAL, "", "", "cluster_atomic"))

    ce_tail = [f for f, _ in ce_rest if f.finding_id not in promoted_ids]
    ordered = (mech_cluster + ce_cluster + promoted
              + [f for f, _ in mech_rest] + ce_tail)

    validated = validate_findings(
        ordered, doc, min_confidence, query_types=query_types,
        format_types=format_types or {}, edit_guard=edit_guard,
        guard_exempt=frozenset({"repair"}))

    return MergeResult(findings=ordered, validated=validated, ledger=ledger)


# --- deliverable 2: the merged-result artifact scan ----------------------------

@dataclass(frozen=True)
class ArtifactHit:
    """One artifact the merged result would introduce, and how it was settled."""
    para_id: str
    pattern: str
    dropped_id: str | None
    resolved: bool

    def to_json(self) -> dict:
        return dataclasses.asdict(self)


def _splice(para_text: str, edits: Sequence[Finding]) -> str:
    """`para_text` with every edit's anchor applied, descending, so an earlier
    edit's offsets are never shifted by a later one — the same order
    `reassembler.apply_tracked_changes` writes in."""
    text = para_text
    for f in sorted(edits, key=lambda x: (x.anchor.start, x.anchor.end),
                    reverse=True):
        a = f.anchor
        text = text[:a.start] + a.insert_text + text[a.end:]
    return text


def scan_artifacts(validated: Sequence[Finding],
                   doc: DocumentModel) -> list[ArtifactHit]:
    """The merged result's would-be output text, per paragraph, checked against
    `candidate_screening.text_invariant_violation` (new ",,"/";;"/doubled-space/
    space-before-punctuation). Composing two lanes' edits is exactly the case
    that check was written for but the main pipeline never runs it on today —
    two edits that individually pass `validator._overlaps` (an insertion
    abutting a span's end, say) can still compose into an artifact neither
    lane's edit was itself responsible for."""
    paras = index_paragraphs(doc)
    by_para: dict[str, list[Finding]] = {}
    for f in validated:
        if f.status == "validated" and f.anchor is not None and not f.format:
            by_para.setdefault(f.para_id, []).append(f)
    hits: list[ArtifactHit] = []
    for para_id, edits in by_para.items():
        para = paras.get(para_id)
        if para is None:
            continue
        corrected = _splice(para.text, edits)
        reason = text_invariant_violation(para.text, corrected)
        if reason:
            hits.append(ArtifactHit(para_id, reason, None, False))
    return hits


def iterate_until_clean(merge: MergeResult, doc: DocumentModel, *,
                        min_confidence: str = "medium",
                        query_types: frozenset[str] = frozenset(),
                        format_types: dict[str, str] | None = None,
                        edit_guard=None,
                        max_iterations: int = 25
                        ) -> tuple[MergeResult, list[ArtifactHit]]:
    """Re-run `merge`'s ordered findings through `validate_findings`, scan the
    result for artifacts, and — bounded — drop the losing edit and recheck
    until the merged paragraphs are clean.

    "Losing" prefers a copy-edit finding over a mechanical one in the same
    paragraph (mechanical wins ties elsewhere, so it keeps that preference
    here); among several candidates in a lane, each is tried in turn and the
    first whose removal actually clears the violation is dropped — a single
    blind "drop the last edit" can leave the real culprit in place and loop
    the full budget for nothing. A cluster member is never a candidate: rule
    (a) says a cluster's span is claimed first and whole, so a member that
    reaches here already survived arbitration and dropping only PART of its
    cluster would produce exactly the half-applied repair atomicity forbids.
    A paragraph where every remaining edit is a cluster member and still dirty
    is reported unresolved rather than silently broken further.

    Returns the (possibly narrower) `MergeResult` and the list of hits —
    empty when the merge started clean, non-empty and every `resolved=True`
    when the loop fixed it, and containing an unresolved hit only when the
    bound was exhausted or no droppable edit could clear it."""
    from .validator import validate_findings

    findings = list(merge.findings)
    reports: list[ArtifactHit] = []
    for _ in range(max_iterations):
        validated = validate_findings(
            findings, doc, min_confidence, query_types=query_types,
            format_types=format_types or {}, edit_guard=edit_guard,
            guard_exempt=frozenset({"repair"}))
        hits = scan_artifacts(validated, doc)
        if not hits:
            return MergeResult(findings, validated, merge.ledger), reports
        hit = hits[0]
        by_id = {f.finding_id: f for f in validated
                if f.status == "validated" and f.anchor is not None
                and f.para_id == hit.para_id and not f.format}
        para = index_paragraphs(doc)[hit.para_id]
        candidates = [f for f in by_id.values() if not f.cluster_id]
        candidates.sort(key=lambda f: 0 if f.lane == COPYEDIT else 1)
        dropped_id = None
        for cand in candidates:
            remaining = [f for f in by_id.values()
                        if f.finding_id != cand.finding_id]
            if text_invariant_violation(para.text,
                                        _splice(para.text, remaining)) is None:
                dropped_id = cand.finding_id
                break
        if dropped_id is None:
            reports.append(hit)
            log.warning("merge desk: artifact %r in %s could not be resolved "
                       "by dropping any losing edit — reported, not fixed.",
                       hit.pattern, hit.para_id)
            return MergeResult(findings, validated, merge.ledger), reports
        findings = [f for f in findings if f.finding_id != dropped_id]
        reports.append(ArtifactHit(hit.para_id, hit.pattern, dropped_id, True))
        log.info("merge desk: dropped %s to clear artifact %r in %s.",
                dropped_id, hit.pattern, hit.para_id)
    # Bound exhausted — should not happen (each iteration strictly shrinks
    # `findings`), but report rather than loop forever on something unforeseen.
    reports.append(ArtifactHit("", "artifact loop did not converge", None, False))
    return MergeResult(findings, merge.validated, merge.ledger), reports


__all__ = ["MECHANICAL", "COPYEDIT", "MergeError", "ClaimRecord", "MergeResult",
          "ArtifactHit", "tag_lane", "rewrite_is_clean", "merge_lanes",
          "scan_artifacts", "iterate_until_clean"]
