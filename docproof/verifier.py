"""The overseer-verifier: a stronger model adjudicating the detectors' findings.

The ensemble's detectors are tuned for recall — union, so nothing a good reader
would catch is thrown away. That trades precision, and this is where it is
bought back. One stronger model reads each finding the detectors did not already
agree on, with the paragraph in full and that error type's own rules in front of
it, and returns one of three verdicts:

  keep      — a real error, correctly fixed (for a disputed fix it picks one)
  downgrade — maybe an error, not sure enough to change silently → a margin query
  reject    — not an error; the detectors were wrong

It is deliberately biased away from rejecting on a hunch: a missed comment costs
the author far less than a lost correction, so 'when unsure, downgrade' is in the
prompt. Runs between the merge and the validator, on the sync path, and its
tokens land in the same Usage as everything else. Cost scales with the number of
findings, not the length of the book, so even a premium model here is cheap.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from typing import Literal, Sequence

from pydantic import BaseModel, ValidationError

from .config import Config
from .error_registry import ErrorType
from .models import Finding, Usage, index_paragraphs
from .providers import Provider, strict_json_schema

log = logging.getLogger("docproof.verifier")

_CONFIDENCE_ORDER = ("low", "medium", "high")


class _Verdict(BaseModel):
    finding_id: str
    verdict: Literal["keep", "reject", "downgrade"]
    chosen_fix: str        # the fix to use for a disputed finding, else ""
    reason: str            # one short sentence, becomes the margin note on a downgrade


class _Verdicts(BaseModel):
    verdicts: list[_Verdict]


_SYSTEM = """You are a senior proofreader reviewing a junior team's findings \
before they reach the author. For each finding you get the paragraph it sits in, \
the change proposed, how many independent reviewers flagged it, and the rule it \
claims to apply. Return exactly one verdict per finding:

- keep: a real error and the fix is correct. When two or more reviewers agree, \
lean keep unless it is plainly wrong.
- downgrade: possibly an error, but not certain enough to change the manuscript \
silently. It becomes a margin question for the author instead of a tracked edit.
- reject: not an error. The reviewers were wrong — dialect, a coined name, a \
deliberate fragment or stylistic choice, or the "fix" itself is wrong.

For a finding marked disputed (reviewers proposed different fixes), keep it and \
put the single correct fix in chosen_fix; otherwise leave chosen_fix empty.

Your job is to catch clear false positives, not to relitigate every judgment \
call. When you are unsure, prefer downgrade over reject: a missed comment costs \
the author far less than a lost correction. Put a short reason on every reject \
and downgrade — on a downgrade it is what the author reads in the margin.

Return an object with a "verdicts" array holding one entry per finding_id given."""


def _bump(confidence: str) -> str:
    try:
        i = _CONFIDENCE_ORDER.index(confidence)
    except ValueError:
        return confidence
    return _CONFIDENCE_ORDER[min(i + 1, len(_CONFIDENCE_ORDER) - 1)]


class Verifier:
    """One paragraph's findings in, verdicts out. Fetch is pure (no shared
    state, no usage bookkeeping) so several paragraphs verify concurrently; the
    caller folds the results in serially."""

    def __init__(self, cfg: Config, types: dict[str, ErrorType],
                 provider: Provider, *, conventions: str = "",
                 vocabulary: str = ""):
        self.cfg = cfg
        self.types = types
        self.provider = provider
        parts = [_SYSTEM]
        if conventions:
            parts.append(conventions)
        if vocabulary:
            parts.append(vocabulary)
        self.system_prompt = "\n\n".join(parts)
        self.schema = strict_json_schema(_Verdicts)

    def _render(self, para_text: str, findings: Sequence[Finding]) -> str:
        lines = [f"PARAGRAPH:\n{para_text}\n", "FINDINGS:"]
        for f in findings:
            et = self.types.get(f.error_type)
            rule = et.detection_prompt if et else f.error_type
            lines.append(
                f"- finding_id={f.finding_id} type={f.error_type} "
                f"reviewers={f.agreement} disputed={'yes' if f.disputed_fix else 'no'}\n"
                f"  flagged: {f.original_text!r}\n"
                f"  proposed: {f.corrected_text!r}\n"
                f"  rule for {f.error_type}: {rule}")
        lines.append("\nReturn a verdict for every finding_id above.")
        return "\n".join(lines)

    def fetch(self, para_text: str, findings: Sequence[Finding]):
        return self.provider.complete_structured(
            model=self.cfg.ensemble.verifier_model,
            system=self.system_prompt,
            user=self._render(para_text, findings),
            schema=self.schema, schema_name="verdicts",
            max_tokens=self.cfg.api.max_output_tokens)

    def parse(self, result, findings: Sequence[Finding]) -> dict[str, dict]:
        """Verdicts keyed by finding_id. A verifier that refused, truncated, or
        returned junk yields nothing — the driver then keeps those findings
        unchanged, so a verifier hiccup can never silently drop a real error."""
        valid = {f.finding_id for f in findings}
        if result.stop_reason != "ok" or result.parsed is None:
            log.error("verifier: no usable answer for %d finding(s): %s",
                      len(findings), result.error or result.stop_reason)
            return {}
        try:
            parsed = _Verdicts.model_validate(result.parsed)
        except ValidationError as e:
            log.error("verifier: answer did not match the schema: %s", e)
            return {}
        out = {}
        for v in parsed.verdicts:
            if v.finding_id in valid:
                out[v.finding_id] = v.model_dump()
        return out


def verify_findings(cfg: Config, prepared, findings: list[Finding],
                    provider: Provider, usage: Usage
                    ) -> tuple[list[Finding], list[Finding]]:
    """Adjudicate `findings`, returning (survivors, rejected).

    survivors carry the verifier's edits — a kept finding as-is (its confidence
    bumped if consensus_confidence_bump is on and it has agreement), a disputed
    finding with its chosen fix, a downgraded finding routed to the query
    channel. rejected carry status 'rejected_by_verifier' with the reason, for
    the report. With verify_policy 'none' or no verifier_model, nothing is
    verified and every finding passes through (still bumped, if configured)."""
    ens = cfg.ensemble

    def bumped(f: Finding) -> Finding:
        if ens.consensus_confidence_bump and f.agreement > 1:
            return replace(f, confidence=_bump(f.confidence))
        return f

    if not ens.verifier_model or ens.verify_policy == "none":
        return [bumped(f) for f in findings], []

    # 'disputed' verifies only findings the detectors did not all agree on;
    # 'all' verifies everything. Consensus findings otherwise pass through.
    to_verify = [f for f in findings if ens.verify_policy == "all"
                 or f.agreement < prepared.n_detectors]
    verdicts = _run(cfg, prepared, to_verify, provider, usage) if to_verify else {}

    survivors: list[Finding] = []
    rejected: list[Finding] = []
    for f in findings:
        g = bumped(f)
        v = verdicts.get(f.finding_id)
        if v is None:                                  # not verified, or no answer
            survivors.append(g)
        elif v["verdict"] == "reject":
            rejected.append(replace(g, status="rejected_by_verifier",
                                    explanation=v.get("reason") or g.explanation))
        elif v["verdict"] == "downgrade":
            survivors.append(replace(g, force_query=True,
                                     explanation=v.get("reason") or g.explanation))
        else:                                          # keep
            if f.disputed_fix and v.get("chosen_fix"):
                g = replace(g, corrected_text=v["chosen_fix"])
            survivors.append(g)
    if rejected:
        log.info("verifier rejected %d of %d finding(s) it judged",
                 len(rejected), len(to_verify))
    return survivors, rejected


def _run(cfg: Config, prepared, to_verify: list[Finding], provider: Provider,
         usage: Usage) -> dict[str, dict]:
    from concurrent.futures import ThreadPoolExecutor

    paras = index_paragraphs(prepared.doc)
    types = {et.key: et for group in prepared.groups for et in group}
    verifier = Verifier(cfg, types, provider,
                        conventions=prepared.conventions,
                        vocabulary=prepared.vocabulary)

    by_para: dict[str, list[Finding]] = {}
    for f in to_verify:
        if f.para_id in paras:                         # a finding with no home paragraph
            by_para.setdefault(f.para_id, []).append(f)

    verdicts: dict[str, dict] = {}
    with ThreadPoolExecutor(
            max_workers=cfg.concurrency_for(cfg.ensemble.verifier_model)) as pool:
        pending = [(fs, pool.submit(verifier.fetch, paras[pid].text, fs))
                   for pid, fs in by_para.items()]
        for fs, fut in pending:                        # fold serially: usage.add is not thread-safe
            result = fut.result()
            usage.add(result.usage)
            verdicts.update(verifier.parse(result, fs))
    return verdicts


# --- the round judge (multi-round review) ------------------------------------
#
# Multi-round review corrects the manuscript between rounds. A correction the
# judge approves is applied AND becomes the text the next round reads, so a wrong
# approval corrupts the next round, not just the delivered file: the bar to
# approve is higher than the bar to propose, which is why this is a second,
# stronger gate even over candidates a cheap confirm already passed. Same
# fail-open discipline as the overseer above — a refusal or unparseable answer
# keeps the finding, never drops it — with a standalone, editable prompt.

_DEFAULT_JUDGE_PROMPT = """You are a senior proofreader deciding which of a \
junior team's proposed corrections reach the manuscript. This is multi-round \
review: a correction you approve is applied to the text AND becomes what the \
next review round reads, so approving a wrong change corrupts the next round, \
not just the delivered file. The bar to approve is therefore higher than the bar \
to propose. Return exactly one verdict per finding:

- approve: the flagged text is a real error and the fix is correct. Approve even \
if you would have phrased the fix differently — a supplied missing word, a fixed \
agreement, a corrected comma is right however it is worded. Do not reject a sound \
fix because it is not your preferred wording.
- query: possibly an error, but you are not certain enough to change the \
manuscript silently. It becomes a margin question for the author and changes no \
text. Use this whenever you are unsure.
- reject: not an error. You are confident the flagged text is correct as written \
— dialect, a coined name or term, a deliberate fragment or stylistic choice — or \
the proposed fix itself introduces an error.

When in doubt, query; never reject on a hunch. A missed correction and an extra \
margin question both cost the author far less than a wrong change applied \
silently and carried into the next round. Reject only clear false positives.

Do not relitigate house style. Em-dash and ellipsis spacing, serial commas, \
capitalization conventions and similar house rules are decided elsewhere; where a \
fix applies one, treat it as correct — do not reject it as wrong. Judge whether \
the flagged text is an error, not whether the fix matches your taste. In \
particular, a differently-worded but correct rewrite of a comma splice, run-on, \
missing word, or agreement error is still an approval, not a reject.

Put one short sentence of reason on every query and reject — on a query it is \
what the author reads in the margin.

Return an object with a "verdicts" array holding one entry per finding_id given, \
using verdict values exactly "approve", "query", or "reject"."""


def default_judge_prompt() -> str:
    """The built-in judge instructions — for the review panel to pre-fill its
    editable field and to fall back to when that field is cleared."""
    return _DEFAULT_JUDGE_PROMPT


class _JudgeVerdict(BaseModel):
    finding_id: str
    verdict: Literal["approve", "query", "reject"]
    reason: str            # one short sentence; the margin note on a query


class _JudgeVerdicts(BaseModel):
    verdicts: list[_JudgeVerdict]


def rejection_key(f: Finding) -> tuple:
    """A coordinate-free identity for 'this correction, in this paragraph', so a
    fix the judge rejected in one round is recognised when a later round proposes
    it again — even re-quoted or re-worded. Built from the essential change
    (canonical delete/insert spans, quote-folded), not the raw offsets, which
    shift between rounds; paragraph ids are stable across rounds by design."""
    from .agreement import canonical_anchors, fold
    anchors = canonical_anchors(f.original_text, f.corrected_text)
    return (f.para_id,
            tuple((fold(a.delete_text), fold(a.insert_text)) for a in anchors))


@dataclass(frozen=True)
class RoundJudgment:
    """One round's adjudication. `edits` are approved corrections — they become
    tracked changes and rewrite the text the next round reads; `queries` are
    downgraded to margin questions and change no text; `rejected` are dropped,
    kept only for the report. `new_rejections` are the decision-memory keys to
    carry into the next round so a rejected fix is not judged again."""
    edits: list[Finding]
    queries: list[Finding]
    rejected: list[Finding]
    new_rejections: frozenset


class RoundJudge:
    """A strong model ruling on one paragraph's proposed corrections. Fetch is
    pure (no shared state, no usage bookkeeping) so paragraphs judge concurrently;
    the caller folds results in serially."""

    def __init__(self, types: dict[str, ErrorType], provider: Provider,
                 model: str, *, instructions: str = "", context: str = "",
                 # rounds.judge_model is a reasoning model at judge_effort high
                 # (gpt-5.6-sol by default) and rounds.py does not override this,
                 # so this default IS the multi-round judge's ceiling — and it
                 # has to cover the model's thinking, not just its verdicts.
                 max_tokens: int = 12000):
        self.types = types
        self.provider = provider
        self.model = model
        self.max_tokens = max_tokens
        parts = [instructions.strip() or _DEFAULT_JUDGE_PROMPT]
        if context:
            parts.append(context)
        self.system_prompt = "\n\n".join(parts)
        self.schema = strict_json_schema(_JudgeVerdicts)

    def _render(self, para_text: str, findings: Sequence[Finding]) -> str:
        lines = [f"PARAGRAPH:\n{para_text}\n", "PROPOSED CORRECTIONS:"]
        for f in findings:
            et = self.types.get(f.error_type)
            rule = et.detection_prompt if et else f.error_type
            lines.append(
                f"- finding_id={f.finding_id} type={f.error_type}\n"
                f"  flagged: {f.original_text!r}\n"
                f"  proposed: {f.corrected_text!r}\n"
                f"  rule for {f.error_type}: {rule}")
        lines.append("\nReturn a verdict for every finding_id above.")
        return "\n".join(lines)

    def fetch(self, para_text: str, findings: Sequence[Finding]):
        return self.provider.complete_structured(
            model=self.model, system=self.system_prompt,
            user=self._render(para_text, findings),
            schema=self.schema, schema_name="verdicts",
            max_tokens=self.max_tokens)

    def parse(self, result, findings: Sequence[Finding]) -> dict[str, dict]:
        """Verdicts keyed by finding_id. A judge that refused, truncated, or
        returned junk yields nothing — the caller then keeps those findings as
        approved edits, so a judge hiccup can never silently drop a correction."""
        valid = {f.finding_id for f in findings}
        if result.stop_reason != "ok" or result.parsed is None:
            log.error("round judge: no usable answer for %d finding(s): %s",
                      len(findings), result.error or result.stop_reason)
            return {}
        try:
            parsed = _JudgeVerdicts.model_validate(result.parsed)
        except ValidationError as e:
            log.error("round judge: answer did not match the schema: %s", e)
            return {}
        return {v.finding_id: v.model_dump()
                for v in parsed.verdicts if v.finding_id in valid}


def adjudicate_round(findings: list[Finding], para_text: dict[str, str],
                     types: dict[str, ErrorType], provider: Provider, *,
                     model: str, instructions: str = "", context: str = "",
                     max_tokens: int = 12000, concurrency: int = 8,
                     prior_rejections: frozenset = frozenset(),
                     usage: Usage) -> RoundJudgment:
    """Rule on a round's model-generated corrections. The caller passes only the
    tracked-change candidates — deterministic sweeps and findings already headed
    for the query channel are not judged.

    A correction whose rejection_key a prior round already rejected is refused
    without a model call (decision memory). The rest are judged per paragraph,
    concurrently; a finding the judge does not answer for is kept as an approved
    edit (fail open). Returns the corrections split into approved edits,
    downgraded queries, and rejects, plus the rejection keys to remember."""
    auto: list[Finding] = []
    to_judge: list[Finding] = []
    for f in findings:
        if rejection_key(f) in prior_rejections:
            auto.append(replace(f, status="rejected_by_verifier",
                                explanation="Rejected in an earlier round."))
        else:
            to_judge.append(f)

    verdicts = (_run_judge(to_judge, para_text, types, provider, model,
                           instructions, context, max_tokens, concurrency, usage)
                if to_judge else {})

    edits: list[Finding] = []
    queries: list[Finding] = []
    rejected: list[Finding] = list(auto)
    new_rej: set = {rejection_key(f) for f in auto}
    for f in to_judge:
        v = verdicts.get(f.finding_id)
        if v is None or v["verdict"] == "approve":     # fail open on no answer
            edits.append(f)
        elif v["verdict"] == "reject":
            rejected.append(replace(f, status="rejected_by_verifier",
                                    explanation=v.get("reason") or f.explanation))
            new_rej.add(rejection_key(f))
        else:                                          # query
            queries.append(replace(f, force_query=True,
                                   explanation=v.get("reason") or f.explanation))
    if rejected or queries:
        log.info("round judge: %d approved, %d queried, %d rejected (%d from "
                 "memory)", len(edits), len(queries), len(rejected), len(auto))
    return RoundJudgment(edits, queries, rejected, frozenset(new_rej))


def _run_judge(to_judge: list[Finding], para_text: dict[str, str],
               types: dict[str, ErrorType], provider: Provider, model: str,
               instructions: str, context: str, max_tokens: int,
               concurrency: int, usage: Usage) -> dict[str, dict]:
    from concurrent.futures import ThreadPoolExecutor
    judge = RoundJudge(types, provider, model, instructions=instructions,
                       context=context, max_tokens=max_tokens)
    by_para: dict[str, list[Finding]] = {}
    for f in to_judge:
        if f.para_id in para_text:                     # no paragraph -> cannot judge
            by_para.setdefault(f.para_id, []).append(f)
    verdicts: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        pending = [(fs, pool.submit(judge.fetch, para_text[pid], fs))
                   for pid, fs in by_para.items()]
        for fs, fut in pending:                        # fold serially: usage.add is not thread-safe
            result = fut.result()
            usage.add(result.usage)
            verdicts.update(judge.parse(result, fs))
    return verdicts
