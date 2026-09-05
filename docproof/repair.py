"""The repair channel: route error-dense sentences to a strong model, fix each
as one atomic cluster.

Every other correcting pass emits an atomic token edit — a comma, a word, a
letter — and the field test against a delivered human proofread showed exactly
where that falls short. When a human finds a broken sentence they repair it as a
*sentence*: the worked example's fix arrived as one decision that surfaced eight
edits — insert "watching", a period to a colon, a run of appositive commas, two
spelled-out numbers — because the pieces are right only together. Applied by
halves they leave the sentence worse than it was. DocProof scored 6% on grammar
and sentence repair and 10% on missing words for exactly this reason: a
token-level machine cannot propose a fix whose pieces are co-dependent, and a
per-edit confirm would reject each piece on its own.

The trigger is the other passes' own output. A sentence in which the ordinary
detectors — the model, the sweeps, LanguageTool, Sapling — flag several separate
errors is a sentence that is probably broken as a whole, not merely dotted with
isolated slips. So the repair channel counts the corrections landing in each
sentence and, above a threshold, routes THAT sentence to a strong model (Fable by
default) for a single minimal repair. The read is cheap because it is scoped to
the handful of sentences the rest of the review already found dense with errors,
not the whole manuscript.

The repair is diffed against the original into member edits that share one
``cluster_id``. A skeptical judge then rules on the WHOLE cluster in one verdict
— is the original genuinely broken (the flags might have been stylistic), does
the repair fix it minimally, does it preserve meaning — because the unit of
judgment is the sentence, not the comma.

Atomicity is the guarantee, and it is enforced in two places:

  * arbitration order — the repair members sit EARLY, so a coherent repair claims
    the sentence's spans first and the scattered token edits that triggered it are
    dropped as overlapping. The repair supersedes the pieces it replaces.
  * before the write — ``enforce_cluster_atomicity`` runs after the judge gates,
    so if any member did not survive (dropped for overlapping a surer sweep, or
    withdrawn by the meaning gate) every other member is withdrawn with it. The
    run never ships half a repair.

The members are delivered as SEPARATE tracked changes, not composed into one
whole-sentence strikethrough — matching the human file, whose worked example
shows eight distinct tracked changes for one repair, and keeping each piece
reviewable. The fabrication defence is the judge, not a character count, so
repair findings are guard-exempt in the validator; a deterministic cap still
drops a "repair" that rewrote more than it fixed.

Off by default and opt-in per run: it is the highest-risk pass in the pipeline —
judgment edits that write were the failure class in the corrections engine's QA
cycles — so it ships behind the trigger threshold, the judge, the meaning gate,
and the atomicity enforcement, and is meant to be measured in shadow mode
(docproof/eval/repair_shadow.py) before it is trusted to write. Whole-document
only. See docs/repair.md.
"""
from __future__ import annotations

import dataclasses
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Sequence

from pydantic import BaseModel, Field

from .agreement import canonical_anchors
from .models import Anchor, CONFIDENCE_RANK, Finding, ParagraphRef, Usage
from .providers.base import strict_json_schema
from .windowing import WindowReport, log_report, resolve_window

log = logging.getLogger("docproof.repair")

# Finding kinds that do NOT count toward a sentence's error density: a question
# is not a correction, and a repair member is not evidence for another repair.
# force_query findings (continuity, smoothing, withheld edits) are excluded by
# the counter directly; this set catches the correcting passes that should not
# feed the trigger.
_NON_TRIGGER_TYPES = frozenset({"repair"})


#
# The sentences reaching this prompt were each flagged with several errors, so
# the model is repairing pre-selected candidates rather than hunting. It still
# draws the hardest line — a flagged sentence may be stylistic, dialect, or a
# deliberate fragment, and the model must decline those — because the trigger
# counts errors, and a run of legitimate house-style commas can cross the
# threshold without the sentence being broken at all.
REPAIR_SYSTEM = """\
You are a proofreader repairing sentences in a novel. Each numbered sentence was
flagged with SEVERAL separate errors by other checks, so most are genuinely
broken — a dropped word, fused or mispunctuated clauses, a garble, a
missing-word cascade. Repair each with the SMALLEST change that makes it
grammatical and readable.

Absolute rules:
- change only what is necessary; preserve every word, name, and construction you
  can, and keep the author's voice, dialect, register, and meaning exactly
- add NO facts, names, or content the original did not already imply
- if a flagged sentence is NOT actually broken — the flags were stylistic, a
  matter of preference, dialect or an in-voice narrator, or a deliberate fragment
  used for effect — return it UNCHANGED, character for character

Return each sentence by its given number, in the `repaired` field. A sentence you
return unchanged is one you judged not broken; that is a valid and expected
answer."""


# The judge re-draws the same line from the other side: it defaults to "not
# broken" and must be convinced on three independent axes before a repair writes.
CONFIRM_SYSTEM = """\
You are a senior proofreader ruling on a proposed SENTENCE REPAIR in a novel.
Each item gives the ORIGINAL sentence, a proposed REPAIRED sentence, and a short
note on what the other checks flagged. Rule on the repair as a whole — the
sentence is the unit, not the individual commas.

Answer three questions independently:
- broken: is the ORIGINAL genuinely ungrammatical or unreadable as written — a
  real error, not merely awkward, and NOT a deliberate fragment, dialect, or
  in-voice construction? Default to false when the original could be intended.
- fixes: does the REPAIRED sentence correct the problem with MINIMAL change and
  nothing gratuitous — no rephrasing, reordering, or tightening beyond what the
  repair requires?
- meaning_preserved: does the repair say exactly what the author meant, adding
  and removing no content, and keeping the voice and register?

This is a literary manuscript. When in any doubt on any axis, answer false.
Reserve HIGH confidence for a repair that is correct beyond argument; use medium
or low when the original might be intended or the repair reaches past the error."""


class _Repair(BaseModel):
    index: int = Field(description="the sentence number being repaired")
    repaired: str = Field(description="the minimally repaired sentence, or the "
                          "original unchanged if it is not actually broken")


class _Repairs(BaseModel):
    repairs: list[_Repair]


class _Verdict(BaseModel):
    index: int = Field(description="the item number being ruled on")
    broken: bool = Field(description="true only if the original is genuinely "
                         "ungrammatical, not merely awkward or a deliberate "
                         "fragment")
    fixes: bool = Field(description="true if the repair corrects it with minimal "
                        "change and nothing gratuitous")
    meaning_preserved: bool = Field(description="true if the repair adds and "
                                    "removes no content and keeps the voice")
    confidence: str = Field(description="high only when all three are beyond "
                            "doubt; medium or low otherwise")


class _Verdicts(BaseModel):
    verdicts: list[_Verdict]


@dataclass(frozen=True)
class BrokenSite:
    """One sentence the trigger routed to repair: its manuscript text, where it
    sits, and the error types that flagged it (for the judge's note and the
    report)."""
    para_id: str
    sentence: str          # the manuscript's own characters
    occurrence: int        # 1-based, disambiguates a sentence repeated in a paragraph
    error_count: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RepairCluster:
    """One broken sentence and its proposed repair, sited exactly.

    ``members`` are the atomic edits of the repair, in SENTENCE coordinates (a
    member's span indexes ``sentence``, not the paragraph). They are derived once,
    by diffing the repair against the original, so the same cluster scores the
    same way in the shadow harness and applies the same way in the pipeline.
    """
    cluster_id: str
    para_id: str
    sentence: str
    occurrence: int
    repaired: str
    reason: str
    members: tuple[Anchor, ...]

    @property
    def net_added(self) -> int:
        return _net_added(self.members)



def triggered_sentences(findings: Sequence[Finding],
                        paragraphs: Sequence[ParagraphRef], *,
                        threshold: int,
                        syntax_types: Sequence[str] = (),
                        syntax_threshold: int | None = None) -> list[BrokenSite]:
    """Sentences carrying at least ``threshold`` separate corrections, each as a
    BrokenSite to route to the repair model.

    ``syntax_types`` widens the net for structurally broken sentences: a
    sentence flagged by one of these detectors (run_on_sentence, missing_word —
    the flags that say "this sentence does not parse", not "a comma is
    missing") triggers at ``syntax_threshold`` instead. The Grenada memoir's
    "I came upon, in a face-to-face manner, by the husband" accumulates one or
    two flags, never three; a broken-syntax flag plus any corroborating second
    is evidence enough to let the judge look. Empty ``syntax_types`` keeps the
    single-threshold behavior exactly.

    Only corrections count — a ``force_query`` finding is a question, not
    evidence a sentence is broken, and a repair member does not count toward
    another repair. Each finding is attributed to the sentence its edit falls in
    (found by shrinking to the changed span, then taking the enclosing sentence),
    so a finding that quotes the whole paragraph still lands in the right
    sentence. The manuscript's own characters are carried on the site, so the
    downstream diff and anchor land on real text."""
    from .sweeps import sentence_window
    from .validator import anchor_offset, shrink

    text_of = {p.para_id: p.text for p in paragraphs}
    counts: dict[tuple[str, int, int], int] = defaultdict(int)
    reasons: dict[tuple[str, int, int], list[str]] = defaultdict(list)
    for f in findings:
        if f.force_query or f.error_type in _NON_TRIGGER_TYPES or f.format:
            continue
        text = text_of.get(f.para_id)
        if not text:
            continue
        s = anchor_offset(text, f.original_text, f.occurrence)
        if s == -1:
            continue
        pre, deleted, _inserted = shrink(f.original_text, f.corrected_text)
        # An edit at the very end of the paragraph (a terminal period, a closing
        # quote) yields pos at — or an end just past — len(text); clamp both into
        # the text so the site lands in the trailing sentence instead of asking
        # sentence_window for a span the text does not contain.
        pos = min(s + pre, len(text) - 1)
        end = min(max(pos + 1, pos + len(deleted)), len(text))
        _quote, lo, _occ = sentence_window(text, pos, end)
        # Re-derive the sentence span in a stable way (sentence_window returns the
        # trimmed sentence and its start; hi is start + its length).
        hi = lo + len(_quote)
        key = (f.para_id, lo, hi)
        counts[key] += 1
        reasons[key].append(f.error_type)

    syntax = frozenset(syntax_types)
    floor = syntax_threshold if syntax_threshold is not None else threshold
    sites: list[BrokenSite] = []
    for (para_id, lo, hi), n in counts.items():
        bar = (floor if syntax and any(r in syntax
                                       for r in reasons[(para_id, lo, hi)])
               else threshold)
        if n < bar:
            continue
        text = text_of[para_id]
        sentence = text[lo:hi]
        if not sentence.strip():
            continue
        sites.append(BrokenSite(
            para_id=para_id, sentence=sentence,
            occurrence=_occurrence(text, lo, sentence),
            error_count=n, reasons=tuple(sorted(set(reasons[(para_id, lo, hi)])))))
    # Deterministic order: by paragraph then position, so ids and the report are
    # stable across runs (dict insertion order already is, but a sort makes it
    # independent of finding order too).
    sites.sort(key=lambda s: (s.para_id, s.occurrence, -s.error_count))
    log.info("Repair trigger: %d sentence(s) at or over the %d-error threshold.",
             len(sites), threshold)
    return sites


def _members(sentence: str, repaired: str) -> tuple[Anchor, ...]:
    """The repair's atomic edits, in sentence coordinates, whitespace-only
    shuffles dropped. Empty when the repair changed nothing that matters."""
    out = []
    for a in canonical_anchors(sentence, repaired):
        if not (a.delete_text.strip() or a.insert_text.strip()):
            continue
        out.append(a)
    return tuple(out)


def _net_added(members: Sequence[Anchor]) -> int:
    return sum(len(m.insert_text) - len(m.delete_text) for m in members)


def _occurrence(text: str, start: int, sentence: str) -> int:
    """Which 1-based occurrence of ``sentence`` begins at ``start``. The
    validator re-anchors by (quote, occurrence), so a sentence repeated verbatim
    in one paragraph has to say which copy it is."""
    return text.count(sentence, 0, start) + 1



def _payload(rows: Sequence[tuple[int, BrokenSite]]) -> str:
    return "\n\n".join(f"[{n}] {site.sentence}" for n, site in rows)


def _rows_of_repairs(parsed: dict, items) -> dict[int, str]:
    try:
        return {r.index: r.repaired for r in _Repairs.model_validate(parsed).repairs}
    except Exception as e:                               # malformed structured output
        log.error("repair: response did not match the schema: %s", e)
        return {}


def repair_sites(sites: Sequence[BrokenSite], provider, *, model: str,
                 max_output_tokens: int, usage: Usage, context: str = "",
                 max_added: int = 120, max_members: int = 12,
                 batch_size: int = 20, system: str | None = None,
                 concurrency: int = 1, loss_sink: list | None = None,
                 stats: dict | None = None) -> list[RepairCluster]:
    """Send each triggered sentence to the strong model and return sited,
    size-guarded repair clusters.

    ``context`` carries the same whole-book prompt sections the detectors get —
    the vocabulary (coinages are not errors), the variant conventions, the story
    sheet — so the model repairs against the book's own rules. Batched, with the
    shared truncation-recovery (a window that comes back short is halved and
    re-asked); anything still unanswered is counted, never taken for "left
    unchanged". Fail-closed on the diff: a repair that changed nothing, or
    rewrote more than ``max_added``/``max_members`` (a paraphrase, not a repair),
    is dropped for cause and counted."""
    if not sites:
        return []
    system = system or REPAIR_SYSTEM
    if context.strip():
        system = f"{system}\n\n{context.strip()}"
    sites = list(sites)
    windows = [sites[i:i + batch_size]
               for i in range(0, len(sites), batch_size)]
    schema = strict_json_schema(_Repairs)
    drops = {"noop": 0, "too_large": 0}
    seq = [0]

    def fetch(window, ceiling: int = max_output_tokens):
        rows = list(enumerate(window, 1))
        return provider.complete_structured(
            model=model, system=system, user=_payload(rows),
            schema=schema, schema_name="repairs", max_tokens=ceiling)

    report = WindowReport(label="repair read")
    clusters: list[RepairCluster] = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        pending = [(w, pool.submit(fetch, w)) for w in windows]
        try:
            for window, future in pending:
                res = future.result()
                usage.add(res.usage, model=model)
                rows = resolve_window(
                    window, res, fetch=fetch, rows_of=_rows_of_repairs,
                    max_tokens=max_output_tokens, report=report,
                    usage_sink=lambda ru: usage.add(ru, model=model))
                for offset in sorted(rows):
                    site = window[offset]
                    repaired = (rows[offset] or "").strip()
                    cluster = _cluster_from_repair(
                        site, repaired, seq, max_added=max_added,
                        max_members=max_members, drops=drops)
                    if cluster is not None:
                        clusters.append(cluster)
        except BaseException:
            for _w, unstarted in pending:
                unstarted.cancel()
            raise
    log_report(report)
    if loss_sink is not None:
        loss_sink.append(report)
    if stats is not None:
        stats.update(drops, sites=len(sites), clusters=len(clusters))
    if any(drops.values()):
        log.info("Repair dropped for cause: %s", drops)
    log.info("Repair: %d cluster(s) from %d triggered sentence(s)%s.",
             len(clusters), len(sites),
             f" — {report.lost} sentence(s) UNRULED" if report.lost else "")
    return clusters


def _cluster_from_repair(site: BrokenSite, repaired: str, seq: list[int], *,
                         max_added: int, max_members: int,
                         drops: dict) -> RepairCluster | None:
    """One triggered sentence's repaired form as a sited, size-guarded cluster,
    or None (counted in ``drops``) when the model left it unchanged or rewrote
    more than a repair plausibly should."""
    if not repaired or repaired == site.sentence:
        drops["noop"] += 1
        return None
    members = _members(site.sentence, repaired)
    if not members:
        drops["noop"] += 1
        return None
    # The deterministic half of the fabrication defence: a "repair" that added a
    # clause it invented or touched a dozen scattered spans is a rewrite, not a
    # repair, and must not ride in under the validator's guard exemption. The
    # judge's meaning check is the other half.
    if len(members) > max_members or max(0, _net_added(members)) > max_added:
        drops["too_large"] += 1
        return None
    seq[0] += 1
    return RepairCluster(
        cluster_id=f"rp-c-{seq[0]:04d}", para_id=site.para_id,
        sentence=site.sentence, occurrence=site.occurrence, repaired=repaired,
        reason=", ".join(site.reasons) or "multiple errors", members=members)



def _explanation(cluster: RepairCluster, *, applied: bool) -> str:
    reason = cluster.reason or "broken sentence"
    if applied:
        return f"Sentence repair ({reason})."
    return f"Possible broken sentence ({reason}) — suggested repair in the margin."


def _member_findings(cluster: RepairCluster, confidence: str, ids,
                     id_prefix: str) -> list[Finding]:
    """One Finding per member of an affirmed cluster, all sharing the cluster's
    id. Each quotes the whole sentence and carries the sentence with only its own
    member applied, so the validator shrinks it to exactly that atomic edit while
    every member anchors to the same sentence occurrence."""
    out: list[Finding] = []
    for m in cluster.members:
        corrected = cluster.sentence[:m.start] + m.insert_text + \
            cluster.sentence[m.end:]
        out.append(Finding(
            finding_id=f"{id_prefix}-{next(ids):04d}",
            chunk_id="repair",
            para_id=cluster.para_id,
            error_type="repair",
            original_text=cluster.sentence,
            occurrence=cluster.occurrence,
            corrected_text=corrected,
            explanation=_explanation(cluster, applied=True),
            confidence=confidence,
            cluster_id=cluster.cluster_id))
    return out


def _query_finding(cluster: RepairCluster, confidence: str, ids,
                   id_prefix: str) -> Finding:
    """The whole repair as one margin question, for a cluster the judge affirmed
    as broken but not confidently enough to write. It carries no cluster_id: a
    single query is atomic by itself, and there is no partial version of it to
    guard against."""
    return Finding(
        finding_id=f"{id_prefix}-{next(ids):04d}",
        chunk_id="repair",
        para_id=cluster.para_id,
        error_type="repair",
        original_text=cluster.sentence,
        occurrence=cluster.occurrence,
        corrected_text=cluster.repaired,
        explanation=_explanation(cluster, applied=False),
        confidence=confidence,
        force_query=True)


def confirm(clusters: Sequence[RepairCluster], provider, *, model: str,
            max_tokens: int, usage: Usage, ids, batch_size: int = 20,
            edit_confidence: str = "high", system: str | None = None,
            reject_sink: list | None = None, loss_sink: list | None = None,
            id_prefix: str = "rp", concurrency: int = 1) -> list[Finding]:
    """Rule on each cluster as a whole and turn the affirmed ones into findings.

    A cluster the judge holds broken, correctly fixed, and meaning-preserving at
    ``edit_confidence`` becomes its member findings — separate tracked changes
    that share a cluster_id and stand or fall together. A cluster it holds broken
    but at a softer confidence becomes ONE margin question carrying the whole
    repair. Anything the judge does not hold broken-and-meaning-preserving is
    dropped (and recorded in ``reject_sink`` when given).

    Truncation is counted, never hidden: a batch the judge could not answer in
    full has its unruled clusters logged and added to ``loss_sink``."""
    if not clusters:
        return []
    edit_floor = CONFIDENCE_RANK.get(edit_confidence, 2)
    clusters = list(clusters)
    windows = [clusters[i:i + batch_size]
               for i in range(0, len(clusters), batch_size)]
    schema = strict_json_schema(_Verdicts)

    def fetch(window, ceiling: int = max_tokens):
        lines = []
        for n, c in enumerate(window, 1):
            lines.append(f"{n}. ORIGINAL: {c.sentence}\n"
                         f"   REPAIRED: {c.repaired}\n"
                         f"   flagged: {c.reason or '(none)'}")
        return provider.complete_structured(
            model=model, system=system or CONFIRM_SYSTEM,
            user="\n\n".join(lines), schema=schema, schema_name="verdicts",
            max_tokens=ceiling)

    report = WindowReport(label="repair confirm")
    findings: list[Finding] = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        pending = [(w, pool.submit(fetch, w)) for w in windows]
        try:
            for window, future in pending:
                res = future.result()
                usage.add(res.usage, model=model)
                rows = resolve_window(
                    window, res, fetch=fetch, rows_of=_rows_of,
                    max_tokens=max_tokens, report=report,
                    usage_sink=lambda ru: usage.add(ru, model=model))
                _fold(rows, window, findings, reject_sink, ids, edit_floor,
                      id_prefix=id_prefix)
        except BaseException:
            for _w, unstarted in pending:
                unstarted.cancel()
            raise
    log_report(report)
    if loss_sink is not None:
        loss_sink.append(report)
    edits = sum(1 for f in findings if not f.force_query)
    queries = len(findings) - edits
    kept = len(reject_sink) if reject_sink is not None else 0
    log.info("Repair: %d edit-finding(s) in clusters + %d margin query(ies) "
             "from %d cluster(s)%s%s.", edits, queries, len(clusters),
             f" ({kept} rejected)" if reject_sink is not None else "",
             f" — {report.lost} UNRULED" if report.lost else "")
    return findings


def _rows_of(parsed: dict, items) -> dict[int, "_Verdict"]:
    try:
        return {v.index: v for v in _Verdicts.model_validate(parsed).verdicts}
    except Exception as e:                               # malformed structured output
        log.error("repair confirm: response did not match the schema: %s", e)
        return {}


def _fold(rows: dict, window, findings: list, reject_sink, ids,
          edit_floor: int, *, id_prefix: str) -> None:
    """One batch's verdicts, appended in the order asked. A verdict absent from
    ``rows`` was never returned — left out of both the findings and the reject
    log rather than counted as a rejection."""
    for offset in sorted(rows):
        v = rows[offset]
        c = window[offset]
        affirmed = v.broken and v.fixes and v.meaning_preserved
        if not affirmed:
            if reject_sink is not None:
                reject_sink.append({
                    "cluster_id": c.cluster_id, "para_id": c.para_id,
                    "sentence": c.sentence, "repaired": c.repaired,
                    "broken": v.broken, "fixes": v.fixes,
                    "meaning_preserved": v.meaning_preserved,
                    "confidence": v.confidence})
            continue
        conf = v.confidence if v.confidence in CONFIDENCE_RANK else "low"
        if CONFIDENCE_RANK[conf] >= edit_floor:
            findings.extend(_member_findings(c, conf, ids, id_prefix))
        else:
            # Broken and correctly fixed, but not sure enough to write: the whole
            # repair goes to the margin as one suggestion, never a partial edit.
            findings.append(_query_finding(c, conf, ids, id_prefix))



def enforce_cluster_atomicity(validated: list[Finding], doc) -> int:
    """Withdraw any repair cluster that did not survive whole, in place.

    Runs AFTER validation and the judge gates, when every finding carries its
    final status, so it sees both kinds of partial failure: a member dropped at
    validation for overlapping a surer edit, and a member the meaning gate
    withdrew to the margin. A cluster is intact only when every one of its
    members is still a clean tracked change (status "validated", not a query); if
    any member is anything else, every still-standing member is withdrawn to the
    margin via ``validator.to_query`` — the span-preserving withdrawal the judge
    gates already use, so nothing else in the run moves. The withdrawn members
    are ``withheld``, so by default they leave no comment (``not_applied_comments``
    off) and the run simply does not ship the partial repair; the change log still
    records that a broken sentence was seen.

    Returns how many member edits were withdrawn, for the report. A run with
    repair off, or with no cluster that came apart, does no work and returns 0."""
    from .validator import to_query

    by_cluster: dict[str, list[int]] = defaultdict(list)
    for i, f in enumerate(validated):
        if f.cluster_id:
            by_cluster[f.cluster_id].append(i)
    withdrawn = 0
    for cid, idxs in by_cluster.items():
        intact = all(validated[i].status == "validated"
                     and not validated[i].force_query for i in idxs)
        if intact:
            continue
        for i in idxs:
            f = validated[i]
            if f.status == "validated" and not f.force_query:
                validated[i] = to_query(f, doc)
                withdrawn += 1
        log.info("Repair: cluster %s did not survive whole; withdrew its "
                 "surviving member(s) to keep the repair atomic.", cid)
    if withdrawn:
        log.info("Repair: %d member edit(s) withdrawn to preserve cluster "
                 "atomicity.", withdrawn)
    return withdrawn
