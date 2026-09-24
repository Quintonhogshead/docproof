"""Opus 5.5, on the Claude subscription, fact-checks the five teasers against the
whole manuscript and corrects each error with the smallest exact edit."""
from __future__ import annotations

import json
from pathlib import Path
import time

from docproof.providers import strict_json_schema
from . import ADJUDICATOR_EFFORT, ADJUDICATOR_MODEL
from .models import Adjudication, Draft, Manuscript, apply_adjudication, digest
from . import prompts

# A whole-book read at high effort, as Galley's continuity stage allows.
TIMEOUT_SECONDS = 1800
MAX_OUTPUT_TOKENS = 32_000
# A ruling the checks refuse is sent back to Opus with the reason, in the same attempt.
ROUNDS = 3


class AdjudicatorError(RuntimeError):
    pass


def provider():
    """One fenced Claude Code turn on the subscription, never an API key."""
    import asyncio
    from dataclasses import replace
    from docproof.agent_lane import require_cli_for
    from docproof.providers.subagent import SubagentProvider

    require_cli_for(ADJUDICATOR_MODEL)

    class Timed(SubagentProvider):
        async def _turn(self, *args, evidence, **kwargs):
            result = await asyncio.wait_for(super()._turn(*args, evidence=evidence, **kwargs),
                                            timeout=TIMEOUT_SECONDS)
            message = evidence.get("result")
            if message is None or getattr(message, "is_error", False) or \
                    getattr(message, "subtype", None) != "success":
                return replace(result, stop_reason="error", error="Claude turn did not complete")
            return result

    return Timed(model=ADJUDICATOR_MODEL, effort=ADJUDICATOR_EFFORT)


def adjudicate(manuscript: str, draft: Draft, work: Path, *, lane=None,
               progress=lambda text: None) -> tuple[Adjudication, Draft, list[dict]]:
    """The validated ruling, the corrected package, and a receipt per call.

    A ruling is accepted only if every correction applies exactly, cites a
    verbatim passage of this manuscript, and leaves the package passing its
    checks; otherwise Opus is told why and rules again."""
    book = Manuscript(manuscript)
    draft_sha256 = digest(draft)
    saved = Path(work) / "adjudications" / (draft_sha256 + ".json")
    if saved.exists():
        raw = json.loads(saved.read_text())
        ruling = Adjudication.model_validate(raw["ruling"])
        return ruling, apply_adjudication(draft, ruling, book), raw["receipts"]
    lane = lane or provider()
    schema = strict_json_schema(Adjudication)
    feedback, receipts = [], []
    for round_number in range(1, ROUNDS + 1):
        progress("Opus 5.5 is checking the five teasers against the whole manuscript" +
                 (f" (round {round_number})" if round_number > 1 else ""))
        started = time.time()
        result = lane.complete_structured(
            model=ADJUDICATOR_MODEL, system=prompts.ADJUDICATOR_SYSTEM,
            user=prompts.adjudicator_prompt(manuscript, draft, draft_sha256, feedback),
            schema=schema, schema_name="teaser_adjudication", max_tokens=MAX_OUTPUT_TOKENS)
        receipts.append({"at": started, "seconds": round(time.time() - started, 1),
                         "model": ADJUDICATOR_MODEL, "effort": ADJUDICATOR_EFFORT,
                         "stop_reason": result.stop_reason,
                         "input_tokens": (result.usage.input_tokens + result.usage.cache_read_input_tokens +
                                          result.usage.cache_creation_input_tokens),
                         "output_tokens": result.usage.output_tokens})
        if result.stop_reason != "ok" or result.parsed is None:
            raise AdjudicatorError(f"Opus did not return a ruling ({result.stop_reason}: {result.error}).")
        try:
            ruling = Adjudication.model_validate(result.parsed)
            corrected = apply_adjudication(draft, ruling, book)
        except ValueError as exc:
            feedback.append(str(exc)[:1500])
            continue
        saved.parent.mkdir(parents=True, exist_ok=True)
        temporary = saved.with_suffix(".tmp")
        temporary.write_text(json.dumps({"ruling": ruling.model_dump(), "receipts": receipts}))
        temporary.replace(saved)
        return ruling, corrected, receipts
    raise AdjudicatorError("Opus's ruling failed the checks " + str(ROUNDS) + " times: " + feedback[-1])
