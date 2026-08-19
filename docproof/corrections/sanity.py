"""An optional model gate over the proposed edits, before any is applied.

The deterministic core is faithful by design: it applies exactly what the edit
list says, and refuses what it cannot anchor. That faithfulness is also its blind
spot — a reviewer's mark that reads "iPhone" against a fantasy novel, or a swap
the extractor over-grabbed so it drops a verb ("slowly eased" → "unhurriedly"),
is applied as loyally as a real typo fix. Neither is a *mechanical* error the
apply stage can see; each is an edit that should never have been made.

This gate reads each proposed edit beside the line it changes and judges whether
carrying it out leaves sound, faithful prose. What it doubts it does not apply —
it hands back the edit ids to withhold, with a reason, and those become
`WITHHELD` outcomes in the "for a human" bucket. It never *makes* an edit and
never rewrites one; it only holds a doubtful one back for a person, so a false
alarm costs a second look, never a wrong change shipped.

Opt-in: the default corrections run stays deterministic, model-free and free. A
gate that itself fails (a refusal, a truncation) withholds nothing and lets the
run proceed — a broken check must not sink the corrections it was checking.
"""
from __future__ import annotations

import logging
from typing import Callable, Literal, Sequence

from pydantic import BaseModel

from ..models import Usage
from ..providers import Provider, strict_json_schema
from .model import DESIGN, Edit

log = logging.getLogger("docproof.corrections.sanity")

MAX_OUTPUT_TOKENS = 8000
# Judge a bounded batch at a time: a big proof has hundreds of edits, and a
# structured reply that overruns the output ceiling parses as nothing — which
# would withhold nothing and silently wave the whole batch through.
BATCH_SIZE = 40

# The verdicts that hold an edit back. "safe" is the only one that lets it apply.
WITHHOLD = {"over_grab", "absurd", "nonsense"}


class _Verdict(BaseModel):
    id: str
    verdict: Literal["safe", "over_grab", "absurd", "nonsense"]
    reason: str = ""


class _Verdicts(BaseModel):
    verdicts: list[_Verdict]


_SYSTEM = """\
You are a careful copy editor's second pair of eyes. You are given a list of \
proposed corrections to a book — each an exact find → replace against a line of \
the manuscript, with the reviewer's note. For each, decide whether carrying it \
out leaves sound, faithful prose, and return one verdict:

- "safe": a sensible correction — a typo, punctuation, word choice, or a \
deletion that reads cleanly. When in doubt between safe and a concern, choose \
safe; the goal is to catch the clear problems, not to second-guess the editor.
- "over_grab": the replace drops or garbles words it should have kept — the find \
reached past the word that actually changes and took a verb, object, or phrase \
with it (e.g. find "slowly eased", replace "unhurriedly", losing "eased"). The \
result is missing a word or ungrammatical.
- "absurd": the change is out of place in the work — an anachronism, a joke, or \
content that does not belong in this book's world or register (e.g. changing a \
fantasy dagger to "an iPhone").
- "nonsense": the replacement is not real language, is broken, or makes the \
sentence incoherent.

Judge only what is in front of you; do not invent problems. Return a verdict for \
every edit id, using the exact id given."""


def _format(edits: Sequence[Edit]) -> str:
    lines = ["Proposed corrections:", ""]
    for e in edits:
        line = e.context or e.find
        lines.append(f"- id {e.id}: on the line \"{line}\"")
        lines.append(f"    change \"{e.find}\" to \"{e.replace}\""
                     + (f"  (note: {e.instruction})" if e.instruction else ""))
    return "\n".join(lines)


def review_edits(edits: Sequence[Edit], provider: Provider, *, model: str,
                 usage: Usage, max_tokens: int = MAX_OUTPUT_TOKENS,
                 progress: Callable[[int, int], None] | None = None
                 ) -> dict[str, str]:
    """The edit ids the gate holds back, each mapped to a short reason.

    Only real text edits are judged — a design route or a no-op has nothing to
    apply. `usage` accrues the model spend. A batch whose model call fails is
    logged and skipped (nothing withheld from it), so the gate degrades to
    letting edits through rather than blocking the run.

    `progress(done, total)`, when given, is called as each batch is reached and
    once at the end, so a caller (the app's job card) can show the gate moving
    instead of one long silence."""
    candidates = [e for e in edits if e.kind != DESIGN and e.find != e.replace]
    total = len(candidates)
    withheld: dict[str, str] = {}
    for i in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[i:i + BATCH_SIZE]
        if progress:
            progress(i, total)
        try:
            result = provider.complete_structured(
                model=model, system=_SYSTEM, user=_format(batch),
                schema=strict_json_schema(_Verdicts), schema_name="edit_sanity",
                max_tokens=max_tokens)
        except Exception:                         # noqa: BLE001 - a gate must not sink the run
            log.warning("Sanity gate call failed; letting this batch through",
                        exc_info=True)
            continue
        usage.add(result.usage, model=model)
        if result.stop_reason != "ok" or result.parsed is None:
            log.warning("Sanity gate returned no usable answer (%s); letting "
                        "this batch through", result.stop_reason)
            continue
        for v in result.parsed.get("verdicts", []):
            eid, verdict = v.get("id"), v.get("verdict")
            if eid and verdict in WITHHOLD:
                reason = (v.get("reason") or "").strip()
                withheld[eid] = f"{verdict}: {reason}" if reason else verdict
    if progress:
        progress(total, total)
    if withheld:
        log.info("Sanity gate held back %d of %d edit(s) for a human",
                 len(withheld), len(candidates))
    return withheld
