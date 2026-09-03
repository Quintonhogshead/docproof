"""The merge desk: cross-lane merging of two edit sets into one deliverable.

Galley runs two lanes over the same manuscript: a mechanical/proofread lane
(the ordinary review — sweeps, spelling, grammar, punctuation) and a copy-edit
lane (a rewrite pass that proposes whole-sentence fixes for weaker prose). Each
lane is a finished, independent findings set. This module reconciles the two
into ONE set of tracked changes, deciding — span by span — which lane's edit
survives where the two disagree.

The claim rules, in priority order — STRICT since the 2026-09-01 corruption
(overlapping rewrites composed with mechanical fixes into "response was was",
"talk about about", "deeplyly"; both rows landed because their *shrunk* diffs
happened not to touch, and the validator's minimal-region splitting then
interleaved them inside one sentence):

  (a) a composite/cluster (Finding.cluster_id, the repair channel's atomic
      multi-edit unit — see docproof/repair.py) claims its spans FIRST,
      ahead of every ordinary finding in either lane, and is ATOMIC: if any
      member is later rejected or withdrawn, the whole cluster goes with it.
      That enforcement is `repair.enforce_cluster_atomicity`, already generic
      (it keys purely on cluster_id, not on which pass produced the member) —
      this module only has to place cluster members first and hand the merged
      set to a caller that still runs the atomicity pass. (`pipeline.finish`
      does, unconditionally now — see the comment at its call site.)
  (b) MECHANICS WIN OVERLAPS (STRICT): a copy-edit finding whose claimed span
      overlaps ANY mechanical-lane finding's span loses outright, and is
      REMOVED from the merged set — not merely ordered behind the mechanical
      row. The one exception is a true subsumption: the rewrite's own
      corrected text already CONTAINS every contested mechanical row's
      corrected text verbatim (so the rewrite makes that fix itself, rather
      than racing it) AND the rewrite passes the $0 checks clean — the
      deterministic sweeps and (when installed) LanguageTool find nothing
      wrong with it. Then, and only then, the rewrite subsumes them and the
      mechanical rows step aside. Any sweep/LanguageTool hit, or a rewrite
      that does not repeat the mechanical fix verbatim, and the rewrite loses;
      LanguageTool being unavailable is not "clean" either — `rewrite_is_clean`.
  (c) same-lane overlaps between two ordinary (non-cluster) findings: the
      EARLIER one in the input order keeps the span, the later one is removed
      and ledgered "same-lane overlap". Two alternative rewrites of one
      sentence are versions, never halves to be composed.
  (d) two edits that do not overlap both land — this needs no code of its
      own, since the arbitration only ever restricts CONTESTED spans.

A copy-edit finding's CLAIM is its whole quoted span, not the shrunk diff:
a rewrite re-types its whole quote, so anything a mechanical row does inside
that quote is contested even when the two minimal diffs miss each other.
That widening is the actual fix for the corruption above. A mechanical row
claims the shrunk span the validator itself claims. The invariant this module
now owns: no two findings whose claims overlap can both reach `validated`.

Losers are removed from `MergeResult.findings` (so nothing downstream can
re-derive them) but are still reported: they come back in
`MergeResult.rejected`, and in `MergeResult.validated`, stamped
`rejected_overlap`, with the ledger naming the winner and the rule.

For everything the claim rules leave standing, this module still does not
decide who "wins" span by span — matching finish()'s own philosophy
(pipeline.py: "arbitration is order, not deletion") it produces one ORDERED
list of pending findings, winner-first, and hands it to
`validator.validate_findings` (directly, for the ledger this module reports,
and again inside `pipeline.finish` for the real write) which does the actual
first-come-wins overlap accounting. Two traps that ordering alone resolves for
free, because `validate_findings`'/`_overlaps`' semantics already cover them:

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
It drops the losing edit's whole PARENT finding: the validator emits one
tracked change per minimal region, so a wide row reaches the scan as several
edits with derived ids ("fl-0646" -> "fl-0646b", "fl-0646c"). Dropping the
derived id alone left the parent in the working set, which re-derived the
same member on the next pass — the loop that never converged and reported
"UNRESOLVED" one row per run until the parent was deleted from the input by
hand. Dropping the parent removes every sibling with it and guarantees each
iteration strictly shrinks the working set.
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .candidate_screening import text_invariant_violation
from .models import (Anchor, DocumentModel, Finding, ParagraphRef,
                     index_paragraphs)
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
    "pending", None on the way in.

    A prior run's findings.json is a REPORT, one row per tracked change: a
    decision the validator split into minimal regions arrives as its row
    plus lettered siblings ("f-0077", "f-0077b") quoting the same span. Those
    fold back into one row here (`editmap.collapse_region_siblings`) before
    anything is placed. Left in, the sibling contested its own base row as a
    same-lane overlap, and before that rule existed it reached finish() as a
    `rejected_duplicate` carrying the whole shrunk diff under the id the
    re-split had just minted for a minimal region: the 16 same-start pairs
    on the Redding final build."""
    from .editmap import collapse_region_siblings

    items = [dataclasses.asdict(i) if isinstance(i, Finding) else i
             for i in raw]
    items, folded = collapse_region_siblings(items)
    if folded:
        log.info("merge desk: folded %d split-region sibling row(s) back into "
                 "their decisions on the %s lane (%s)", len(folded),
                 default_lane, ", ".join(folded[:8])
                 + (", ..." if len(folded) > 8 else ""))
    out: list[Finding] = []
    for item in items:
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


def _provisional_span(f: Finding, doc: DocumentModel,
                      paras: dict[str, ParagraphRef] | None = None
                      ) -> _Span | None:
    """Steps 1 and 2 of `validator.validate_findings` — locate the quote, shrink
    it to a minimal diff — replicated read-only, with no confidence gate, edit
    guard, or span bookkeeping. This is ONLY used to detect cross-lane overlap
    before the real arbitration; `validate_findings` (called on this module's
    ordered output, both here for the ledger and again in `pipeline.finish`
    for the write) is the sole source of truth for what actually lands.

    `paras` is `index_paragraphs(doc)`, passed in when the caller already has
    it so a whole-book merge does not rebuild the index per finding."""
    para = (paras if paras is not None else index_paragraphs(doc)).get(f.para_id)
    if para is None:
        return None
    s = anchor_offset(para.text, f.original_text, f.occurrence)
    if s == -1:
        return None
    pre, deleted, inserted = shrink(f.original_text, f.corrected_text)
    if not deleted and not inserted:
        return None
    return _Span(s + pre, s + pre + len(deleted))


@dataclass(frozen=True)
class _Placed:
    """A finding with both spans the claim rules need: `span`, the shrunk diff
    the validator would claim, and `claim`, the span this module arbitrates
    on — wider for a copy-edit row (see `_claim_span`)."""
    f: Finding
    span: _Span | None
    claim: _Span | None


def _claim_span(f: Finding, span: _Span | None,
                paras: dict[str, ParagraphRef]) -> _Span | None:
    """The span a finding CLAIMS for arbitration.

    A mechanical row claims exactly what the validator claims: its shrunk
    diff. A copy-edit row claims its WHOLE QUOTED SPAN, because a rewrite
    re-types the sentence it quotes — every mechanical fix inside that quote
    is contested by it, even when the two minimal diffs happen to fall on
    different characters. Composing those two "non-overlapping" edits is
    exactly what produced "response was was" and "and, and and" on
    2026-09-01; the wide claim is what makes rule (b) able to see them."""
    if span is None:
        return None
    if (f.lane or MECHANICAL) != COPYEDIT or f.cluster_id or not f.original_text:
        return span
    para = paras.get(f.para_id)
    if para is None:
        return span
    s = anchor_offset(para.text, f.original_text, f.occurrence)
    if s == -1:
        return span
    return _Span(s, s + len(f.original_text))


def _place(f: Finding, doc: DocumentModel,
           paras: dict[str, ParagraphRef]) -> _Placed:
    span = _provisional_span(f, doc, paras)
    return _Placed(f, span, _claim_span(f, span, paras))


def _as_rejected(p: _Placed, paras: dict[str, ParagraphRef]) -> Finding:
    """A claim-rule loser, stamped the way the validator stamps an edit that
    lost a contested span. Removed from the merged set, but still reported —
    a row that vanished with no status at all would be a silent deletion."""
    para = paras.get(p.f.para_id)
    anchor = None
    if p.span is not None and para is not None:
        _pre, _deleted, inserted = shrink(p.f.original_text, p.f.corrected_text)
        anchor = Anchor(start=p.span.start, end=p.span.end,
                        delete_text=para.text[p.span.start:p.span.end],
                        insert_text=inserted)
    return dataclasses.replace(p.f, status="rejected_overlap", anchor=anchor)


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

# The doctrine each ledger rule enforces, in words, carried on the record
# itself so a ledger read on its own says WHY without a lookup table. `rule`
# stays the stable machine key (`__main__._LEDGER_VERBS` renders it).
STRICT_MECHANICS = "mechanics win overlaps (strict)"
SAME_LANE = "same-lane overlap"
CLUSTER_FIRST = "a cluster claims its spans first and whole"
NO_SUBSUMPTION = ("the rewrite does not contain the mechanical fix verbatim — "
                  "not a subsumption")


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
    # "cluster_atomic" | "rewrite_clean" | "mechanical_default"
    #  | "same_lane_overlap"
    rule: str
    reason: str = ""      # the rewrite checker's reason, when rule == "mechanical_default"
    # The claim rule in words — STRICT_MECHANICS / SAME_LANE / CLUSTER_FIRST.
    policy: str = ""

    def to_json(self) -> dict:
        return dataclasses.asdict(self)


@dataclass
class MergeResult:
    """`findings` is winner-ordered and PENDING (unanchored, unvalidated) — the
    shape `pipeline.finish` takes as its `findings` argument, and it holds
    WINNERS ONLY: a finding that lost a contested span is removed outright
    (strict claim rules), never merely ordered behind its winner. `validated`
    is that set after this module's own `validate_findings` pass PLUS the
    losers, stamped `rejected_overlap`, offered so a caller (or a test) can
    inspect exactly what would land — and what did not — without invoking the
    full pipeline. `rejected` is just the losers. `ledger` explains every
    contested span this module settled."""
    findings: list[Finding]
    validated: list[Finding] = field(default_factory=list)
    ledger: list[ClaimRecord] = field(default_factory=list)
    rejected: list[Finding] = field(default_factory=list)


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
    winner-first list of SURVIVORS, per the claim rules (see module docstring
    — mechanics win overlaps strictly; a loser is removed and reported in
    `MergeResult.rejected`, not merely ordered behind its winner).

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

    paras = index_paragraphs(doc)
    mech = tag_lane(mechanical, MECHANICAL)
    ce = tag_lane(copyedit, COPYEDIT)
    checker = rewrite_checker or (lambda s: rewrite_is_clean(
        s, sweep_keys=sweep_keys, variant=variant, ellipsis_style=ellipsis_style))

    placed_m = [_place(f, doc, paras) for f in mech]
    placed_c = [_place(f, doc, paras) for f in ce]

    # (a) — clusters claim their spans first, atomic, regardless of lane.
    # `enforce_cluster_atomicity` (docproof/repair.py) is what actually
    # withdraws a partial cluster; this only has to make sure a cluster
    # member's span is never conceded to an ordinary finding — here by
    # claiming ahead of every ordinary row in the sweep below, and by
    # ARRIVING first in the list validate_findings arbitrates.
    mech_cluster = [p for p in placed_m if p.f.cluster_id]
    ce_cluster = [p for p in placed_c if p.f.cluster_id]
    mech_rest = [p for p in placed_m if not p.f.cluster_id]
    ce_rest = [p for p in placed_c if not p.f.cluster_id]

    ledger: list[ClaimRecord] = []
    rejected: list[Finding] = []
    out_ids: set[str] = set()        # every finding the claim rules removed
    promoted_ids: set[str] = set()   # copy-edit rows that subsumed a mechanical row

    # (b) — the strict cross-lane rule. For every copy-edit finding whose claim
    # overlaps one or more mechanical findings, ONE clean-rewrite check decides
    # all of them (a rewrite is a property of its own resulting sentence, not of
    # which fix it contests) — but cleanliness alone is no longer enough: the
    # rewrite must also already SAY what each contested mechanical row says,
    # verbatim. Otherwise the two are rival fixes for one span, and mechanics
    # win; the copy-edit row is removed, not just ordered behind.
    for cp in ce_rest:
        if cp.claim is None:
            continue
        contested = [mp for mp in mech_rest
                    if mp.claim is not None and mp.f.para_id == cp.f.para_id
                    and mp.f.finding_id not in out_ids
                    and _overlaps(cp.claim.start, cp.claim.end,
                                  mp.claim.start, mp.claim.end)]
        if not contested:
            continue
        clean, reason = checker(cp.f.corrected_text)
        subsumes = all(mp.f.corrected_text
                      and mp.f.corrected_text in cp.f.corrected_text
                      for mp in contested)
        if clean and subsumes:
            promoted_ids.add(cp.f.finding_id)
            for mp in contested:
                ledger.append(ClaimRecord(
                    cp.f.para_id, cp.claim.start, cp.claim.end,
                    cp.f.finding_id, COPYEDIT, mp.f.finding_id, MECHANICAL,
                    "rewrite_clean", policy=STRICT_MECHANICS))
                out_ids.add(mp.f.finding_id)
                rejected.append(_as_rejected(mp, paras))
        else:
            why = reason if not clean else NO_SUBSUMPTION
            for mp in contested:
                ledger.append(ClaimRecord(
                    mp.f.para_id, mp.claim.start, mp.claim.end,
                    mp.f.finding_id, MECHANICAL, cp.f.finding_id, COPYEDIT,
                    "mechanical_default", reason=why, policy=STRICT_MECHANICS))
            out_ids.add(cp.f.finding_id)
            rejected.append(_as_rejected(cp, paras))

    for p in mech_cluster + ce_cluster:
        ledger.append(ClaimRecord(
            p.f.para_id, p.span.start if p.span else -1,
            p.span.end if p.span else -1,
            p.f.finding_id, p.f.lane or MECHANICAL, "", "", "cluster_atomic",
            policy=CLUSTER_FIRST))

    # (a)/(c) and the invariant — one greedy claim sweep, winner-first, over
    # everything still standing. Cluster members claim unconditionally (their
    # co-dependent spans are settled among themselves by validate_findings, and
    # rule (a) puts them ahead of every ordinary row); each ordinary row must
    # then find its claim free. A row that does not is removed and ledgered.
    # This is what makes "no two findings that overlap on a span both reach
    # validated" true by construction rather than by hoping the shrunk diffs
    # collide the way the validator's own overlap check needs them to.
    order = (mech_cluster + ce_cluster
            + [p for p in ce_rest if p.f.finding_id in promoted_ids]
            + [p for p in mech_rest if p.f.finding_id not in out_ids]
            + [p for p in ce_rest if p.f.finding_id not in out_ids
               and p.f.finding_id not in promoted_ids])
    claims: dict[str, list[tuple[int, int, Finding]]] = {}
    ordered: list[Finding] = []
    for p in order:
        if p.claim is None or p.f.cluster_id:
            # No anchor (validate_findings will reject it honestly), or a
            # cluster member, which never yields.
            ordered.append(p.f)
            if p.claim is not None:
                claims.setdefault(p.f.para_id, []).append(
                    (p.claim.start, p.claim.end, p.f))
            continue
        clash = next((w for a, b, w in claims.get(p.f.para_id, [])
                     if _overlaps(p.claim.start, p.claim.end, a, b)), None)
        if clash is not None:
            same_lane = (clash.lane or MECHANICAL) == (p.f.lane or MECHANICAL)
            if clash.cluster_id:
                rule, policy = "cluster_atomic", CLUSTER_FIRST
            elif same_lane:
                rule, policy = "same_lane_overlap", SAME_LANE
            else:
                rule, policy = "mechanical_default", STRICT_MECHANICS
            ledger.append(ClaimRecord(
                p.f.para_id, p.claim.start, p.claim.end,
                clash.finding_id, clash.lane or MECHANICAL,
                p.f.finding_id, p.f.lane or MECHANICAL, rule,
                reason=policy, policy=policy))
            out_ids.add(p.f.finding_id)
            rejected.append(_as_rejected(p, paras))
            log.info("merge desk: %s loses the span [%d:%d] in %s to %s (%s).",
                    p.f.finding_id, p.claim.start, p.claim.end, p.f.para_id,
                    clash.finding_id, policy)
            continue
        ordered.append(p.f)
        claims.setdefault(p.f.para_id, []).append(
            (p.claim.start, p.claim.end, p.f))

    validated = validate_findings(
        ordered, doc, min_confidence, query_types=query_types,
        format_types=format_types or {}, edit_guard=edit_guard,
        guard_exempt=frozenset({"repair"}))

    return MergeResult(findings=ordered, validated=validated + rejected,
                      ledger=ledger, rejected=rejected)


# --- deliverable 2: the merged-result artifact scan ----------------------------

@dataclass(frozen=True)
class ArtifactHit:
    """One artifact the merged result would introduce, and how it was settled.

    `dropped_id` is the PARENT finding removed to clear it (never a derived
    split id — see `iterate_until_clean`). `finding_ids` names the findings
    that contribute to an UNRESOLVED artifact, so a run that gives up says
    which rows to look at instead of only which paragraph."""
    para_id: str
    pattern: str
    dropped_id: str | None
    resolved: bool
    finding_ids: tuple[str, ...] = ()

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
    result for artifacts, and — bounded — drop the losing FINDING and recheck
    until the merged paragraphs are clean.

    What gets dropped is the whole parent finding, never one derived edit.
    `validate_findings` emits one tracked change per minimal region, so a row
    with id "fl-0646" reaches the scan as "fl-0646", "fl-0646b", "fl-0646c";
    dropping "fl-0646c" alone left the parent in the working set, which
    re-derived the identical member on the very next iteration — the loop that
    burned its whole budget and reported "artifact loop did not converge —
    UNRESOLVED" until a human deleted the parent row from the input file, one
    row per run. `_parent_finding_id` walks the trailing letter suffix back to
    the id actually in the working set, and a dropped parent takes every
    sibling region (and, defensively, every co-member of its cluster) with it,
    so each iteration strictly shrinks the working set and the ordinary case
    converges on the first or second pass.

    "Losing" prefers a copy-edit finding over a mechanical one in the same
    paragraph (mechanics win ties elsewhere, so it keeps that preference
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
    bound was exhausted or no droppable finding could clear it. An unresolved
    hit names the contributing findings in `finding_ids`."""
    from .validator import validate_findings

    findings = list(merge.findings)
    paras = index_paragraphs(doc)
    reports: list[ArtifactHit] = []
    dropped_ids: set[str] = set()
    for _ in range(max_iterations):
        validated = validate_findings(
            findings, doc, min_confidence, query_types=query_types,
            format_types=format_types or {}, edit_guard=edit_guard,
            guard_exempt=frozenset({"repair"}))
        hits = scan_artifacts(validated, doc)
        if not hits:
            return MergeResult(findings, validated + merge.rejected,
                              merge.ledger, merge.rejected), reports
        hit = hits[0]
        working = {f.finding_id: f for f in findings}
        edits = [f for f in validated
                if f.status == "validated" and f.anchor is not None
                and f.para_id == hit.para_id and not f.format]
        para = paras[hit.para_id]

        # Group this paragraph's edits under the parent finding each came
        # from, so a candidate is a ROW the caller could delete, not a region
        # this module invented.
        parent_of = {f.finding_id: _parent_finding_id(f.finding_id, working)
                    for f in edits}
        candidates: list[str] = []
        for f in edits:
            pid = parent_of[f.finding_id]
            if pid is None or pid in candidates or pid in dropped_ids:
                continue
            if working[pid].cluster_id:
                continue     # rule (a): a cluster is claimed whole or not at all
            candidates.append(pid)
        candidates.sort(
            key=lambda pid: 0 if (working[pid].lane or "") == COPYEDIT else 1)

        dropped_id = None
        for pid in candidates:
            doomed = _kin_ids(working[pid], working)
            remaining = [f for f in edits
                        if parent_of[f.finding_id] not in doomed]
            if text_invariant_violation(para.text,
                                        _splice(para.text, remaining)) is None:
                dropped_id = pid
                break
        if dropped_id is None:
            offenders = tuple(sorted(
                {parent_of[f.finding_id] or f.finding_id for f in edits}))
            reports.append(ArtifactHit(hit.para_id, hit.pattern, None, False,
                                      offenders))
            log.warning("merge desk: artifact %r in %s could not be resolved "
                       "by dropping any losing finding (%s) — reported, not "
                       "fixed.", hit.pattern, hit.para_id, ", ".join(offenders))
            return MergeResult(findings, validated + merge.rejected,
                              merge.ledger, merge.rejected), reports
        doomed = _kin_ids(working[dropped_id], working)
        findings = [f for f in findings if f.finding_id not in doomed]
        dropped_ids |= doomed
        reports.append(ArtifactHit(hit.para_id, hit.pattern, dropped_id, True,
                                  tuple(sorted(doomed))))
        log.info("merge desk: dropped %s to clear artifact %r in %s.",
                ", ".join(sorted(doomed)), hit.pattern, hit.para_id)
    # Bound exhausted — cannot happen while every iteration removes at least
    # one finding from `findings`, but report loudly (with the ids still in
    # play) rather than loop forever on something unforeseen.
    stuck = tuple(r.para_id for r in reports if not r.resolved) or ("",)
    remaining_ids = tuple(sorted(
        f.finding_id for f in findings if f.para_id in stuck))
    reports.append(ArtifactHit("", "artifact loop did not converge", None,
                              False, remaining_ids))
    return MergeResult(findings, merge.validated, merge.ledger,
                      merge.rejected), reports


def _parent_finding_id(finding_id: str, working: dict[str, Finding]) -> str | None:
    """The id of the finding in `working` that `finding_id` came from.

    `validate_findings` splits one row into minimal regions and names them by
    appending a letter to the parent id ("fl-0646" -> "fl-0646b", "fl-0646c"),
    so a scan result's id is not necessarily a row anyone can delete. Walk the
    trailing letters back until an id in the working set appears. None when
    nothing matches, which means the edit came from somewhere this call cannot
    withdraw — the caller reports it rather than dropping blindly."""
    cand = finding_id
    while cand:
        if cand in working:
            return cand
        if not cand[-1].isalpha():
            return None
        cand = cand[:-1]
    return None


def _kin_ids(parent: Finding, working: dict[str, Finding]) -> set[str]:
    """Every working id that must go when `parent` goes: the parent itself,
    and — defensively, since a cluster member is never a drop candidate today
    — every co-member of its cluster, because half a repair is worse than
    none (see `repair.enforce_cluster_atomicity`)."""
    doomed = {parent.finding_id}
    if parent.cluster_id:
        doomed |= {f.finding_id for f in working.values()
                  if f.cluster_id == parent.cluster_id}
    return doomed


__all__ = ["MECHANICAL", "COPYEDIT", "MergeError", "ClaimRecord", "MergeResult",
          "ArtifactHit", "tag_lane", "rewrite_is_clean", "merge_lanes",
          "scan_artifacts", "iterate_until_clean",
          "STRICT_MECHANICS", "SAME_LANE", "CLUSTER_FIRST", "NO_SUBSUMPTION"]
