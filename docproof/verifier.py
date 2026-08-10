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
from dataclasses import replace
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
    with ThreadPoolExecutor(max_workers=cfg.api.concurrency) as pool:
        pending = [(fs, pool.submit(verifier.fetch, paras[pid].text, fs))
                   for pid, fs in by_para.items()]
        for fs, fut in pending:                        # fold serially: usage.add is not thread-safe
            result = fut.result()
            usage.add(result.usage)
            verdicts.update(verifier.parse(result, fs))
    return verdicts
