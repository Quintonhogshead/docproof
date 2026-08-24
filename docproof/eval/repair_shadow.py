"""Shadow-mode scorer for the repair channel: run it, write nothing, score it.

The repair channel is the highest-risk pass in the pipeline, so the Avenue D
plan is explicit that it is *measured before it is trusted to write*. This module
is that measurement. It takes the sentences the trigger routed to repair (build
them with ``docproof.repair.triggered_sentences`` from a real run's findings, or
hand them in for a controlled test), runs the real repair+judge path — no
``finish()``, no tracked changes, no manuscript touched — and reports, per
sentence, what the pass decided: an edit it would have written, a margin query,
or a repair the judge rejected.

When a human reference is supplied — the sentence repairs a proofreader actually
made on the same manuscript — it scores each machine edit against it at the
granularity the field test could not reach: not "did the atomic comma match" but
"did the machine repair the same broken sentence, and repair it the same way."
That is the number Avenue G exists to produce for this channel.

Nothing here writes to a manuscript or calls ``finish()``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from itertools import count
from typing import Sequence

from ..models import ParagraphRef, Usage
from ..repair import BrokenSite, confirm, repair_sites

log = logging.getLogger("docproof.eval.repair_shadow")


def _norm(s: str) -> str:
    """Whitespace-collapsed, punctuation-folded form, for comparing two repairs
    of the same sentence without tripping on curly-vs-straight quotes or a
    doubled space. Reuses the validator's length-agnostic fold."""
    from ..validator import fold_punct
    return " ".join(fold_punct(s).split())


@dataclass(frozen=True)
class ClusterOutcome:
    cluster_id: str
    para_id: str
    sentence: str
    repaired: str
    reason: str
    # What the pass decided: "edit" (would write), "query" (margin only),
    # "rejected" (judge said no), or "unruled" (the judge lost the window).
    disposition: str
    # Against the human reference, for an "edit" only: "same" (the human made the
    # same repair), "different" (the human repaired this sentence another way),
    # "machine_only" (no human touched it), or None when no reference was given.
    match: str | None = None
    human_repaired: str | None = None


@dataclass
class ShadowReport:
    triggered: int = 0
    edits: int = 0
    queries: int = 0
    rejected: int = 0
    unruled: int = 0
    outcomes: list[ClusterOutcome] = field(default_factory=list)
    # vs the human reference (all 0 when none was supplied)
    human_total: int = 0
    same: int = 0
    different: int = 0
    machine_only: int = 0
    missed: int = 0            # human repairs no machine edit OR query covered

    def summary(self) -> str:
        base = (f"{self.triggered} triggered sentence(s): {self.edits} edit, "
                f"{self.queries} query, {self.rejected} rejected"
                + (f", {self.unruled} unruled" if self.unruled else ""))
        if not self.human_total:
            return base
        return (base + f" | vs human ({self.human_total}): {self.same} same, "
                f"{self.different} different, {self.machine_only} machine-only, "
                f"{self.missed} missed")


def run_shadow(sites: Sequence[BrokenSite], repair_provider, *, model: str,
               confirm_provider=None, confirm_model: str | None = None,
               max_output_tokens: int = 16_000, confirm_max_tokens: int = 16_000,
               batch_size: int = 20, edit_confidence: str = "high",
               max_added: int = 120, max_members: int = 12,
               human_repairs: dict[tuple[str, str], str] | None = None,
               usage: Usage | None = None) -> ShadowReport:
    """Repair and judge the triggered sentences without writing, and score the
    result.

    ``sites`` are the BrokenSites the trigger routed to repair — from
    ``repair.triggered_sentences(findings, paragraphs, threshold=…)`` on a real
    run, or built directly for a controlled test. ``human_repairs`` maps
    (para_id, normalized-sentence) to the sentence a human proofreader repaired it
    to; build it with ``human_repairs_from``. With it, every machine edit is
    scored same / different / machine-only, and every human repair a machine
    neither wrote nor queried is counted as missed."""
    usage = usage or Usage()
    confirm_provider = confirm_provider or repair_provider
    confirm_model = confirm_model or model

    clusters = repair_sites(sites, repair_provider, model=model,
                            max_output_tokens=max_output_tokens, usage=usage,
                            max_added=max_added, max_members=max_members,
                            batch_size=batch_size)
    rejected: list = []
    findings = confirm(clusters, confirm_provider, model=confirm_model,
                       max_tokens=confirm_max_tokens, usage=usage, ids=count(1),
                       batch_size=batch_size, edit_confidence=edit_confidence,
                       reject_sink=rejected)

    edited_ids = {f.cluster_id for f in findings
                  if f.cluster_id and not f.force_query}
    queried = {(f.para_id, _norm(f.original_text))
               for f in findings if f.force_query}
    rejected_ids = {r["cluster_id"] for r in rejected}

    report = ShadowReport(triggered=len(sites))
    human = dict(human_repairs or {})
    consumed: set[tuple[str, str]] = set()

    for c in clusters:
        key = (c.para_id, _norm(c.sentence))
        if c.cluster_id in edited_ids:
            disp = "edit"
        elif key in queried:
            disp = "query"
        elif c.cluster_id in rejected_ids:
            disp = "rejected"
        else:
            disp = "unruled"                 # the judge lost this window
        match = None
        human_rep = None
        if disp == "edit" and human_repairs is not None:
            if key in human:
                human_rep = human[key]
                match = ("same" if _norm(human_rep) == _norm(c.repaired)
                         else "different")
                consumed.add(key)
            else:
                match = "machine_only"
        if disp == "query" and key in human:
            consumed.add(key)                # a query still "covers" it for recall
        report.outcomes.append(ClusterOutcome(
            cluster_id=c.cluster_id, para_id=c.para_id, sentence=c.sentence,
            repaired=c.repaired, reason=c.reason, disposition=disp, match=match,
            human_repaired=human_rep))
        report.edits += disp == "edit"
        report.queries += disp == "query"
        report.rejected += disp == "rejected"
        report.unruled += disp == "unruled"
        report.same += match == "same"
        report.different += match == "different"
        report.machine_only += match == "machine_only"

    if human_repairs is not None:
        report.human_total = len(human)
        report.missed = sum(1 for k in human if k not in consumed)

    log.info("Repair shadow: %s", report.summary())
    return report


def human_repairs_from(base_paragraphs: Sequence[ParagraphRef],
                       repaired_paragraphs: Sequence[ParagraphRef],
                       ) -> dict[tuple[str, str], str]:
    """A (para_id, normalized-sentence) -> repaired-sentence map from a before/
    after pair of the same manuscript, so a delivered human proofread becomes a
    reference the scorer can read.

    A sentence is counted as a human *repair* only when the change inside it is
    larger than a lone token edit — a single word or comma is the typed passes'
    territory, not this channel's — so the reference measures the same clustered
    work the pass is built to do. Paragraphs are matched by para_id; a paragraph
    absent from either side is skipped."""
    from ..agreement import canonical_anchors
    from ..sweeps import sentence_window

    after = {p.para_id: p.text for p in repaired_paragraphs}
    out: dict[tuple[str, str], str] = {}
    for base in base_paragraphs:
        target = after.get(base.para_id)
        if target is None or target == base.text:
            continue
        anchors = canonical_anchors(base.text, target)
        by_sentence: dict[tuple[int, int], list] = {}
        for a in anchors:
            quote, lo, _occ = sentence_window(base.text, a.start,
                                              max(a.start + 1, a.end))
            by_sentence.setdefault((lo, lo + len(quote)), []).append(a)
        for (lo, hi), edits in by_sentence.items():
            sentence = base.text[lo:hi]
            rebuilt = sentence
            touched = 0
            for a in sorted(edits, key=lambda a: a.start, reverse=True):
                s, e = a.start - lo, a.end - lo
                if s < 0 or e > len(sentence):
                    continue
                rebuilt = rebuilt[:s] + a.insert_text + rebuilt[e:]
                touched += len(a.delete_text) + len(a.insert_text)
            if rebuilt != sentence and (len(edits) > 1 or touched > 6):
                out[(base.para_id, _norm(sentence))] = rebuilt
    return out
