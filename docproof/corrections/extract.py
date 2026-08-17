"""Extracting an `Edit` list from a free-form corrections source, with a model.

The fuzzy front-end to the deterministic core. An author's list — prose ("change
'harbor' to 'harbour' throughout"), a pasted table, or the text pulled out of a
corrections PDF — is read by a model into the same structured edit list a typed
list would give, then handed straight to `parse.parse_edits` for validation.

The safety argument is the whole reason extraction is a separate stage from
application: the model only *proposes*. Every anchor it emits is checked by
`apply` against the real book — a `find` that is not there is flagged, never
guessed — and `verify` proves the applied result matches the list. So extraction
is allowed to be imperfect without being unsafe. It is also why this returns a
`ParseResult` (edits + issues) for a human to review before anything is applied,
rather than applying anything itself.

Model-agnostic: it takes a `Provider` and reuses the same structured-output path
prep's subject detection does, so it works on whichever model the caller wired.
"""
from __future__ import annotations

from typing import Callable, Literal, Sequence

from pydantic import BaseModel

from ..models import Usage
from ..providers import Provider, strict_json_schema
from .parse import ParseResult, parse_edits

MAX_OUTPUT_TOKENS = 8000


class ExtractionError(RuntimeError):
    """The model would not, or could not, return a usable edit list. Distinct
    from a parse issue, which is a *single* entry the model got wrong — this is
    the whole call failing (a refusal, a truncation, no answer)."""


# The schema the model fills. Every field is required on the wire (see
# strict_json_schema), but the defaults document what an unmarked field means, and
# parse_edits — not the schema — is what finally normalizes and validates them, so
# a model that puts an odd kind or a stray occurrence is corrected or flagged
# there rather than crashing the call.
class _ExtractedEdit(BaseModel):
    find: str
    replace: str
    context: str = ""
    instruction: str = ""
    kind: Literal["mechanical", "judgment", "design"] = "mechanical"
    occurrence: int = 0


class _Extracted(BaseModel):
    edits: list[_ExtractedEdit]


_SYSTEM = """\
You convert a list of corrections for a book into a precise, machine-applicable \
edit list. Each correction becomes one edit:

- find: the exact text in the book to change, copied VERBATIM — same words, \
same punctuation, same capitalization. This is how the edit is located, so it \
must be text that actually appears in the book, not a paraphrase. Quote the \
smallest phrase that uniquely identifies the spot. If the source text carries \
obvious extraction artifacts — a stray space inside a word ("Y ou", "hil t", "b \
efore"), a broken hyphenation — repair them so find reads as the clean text the \
book actually contains; the edit is located against the real book, not the \
artifact.
- replace: the corrected text. Use an empty string for a pure deletion. For an \
insertion, set find to the existing text around the insertion point and put that \
same text plus the new words in replace.
- context: when find is a short or common phrase that may appear more than once \
in the book, set context to a longer VERBATIM run of the surrounding text (the \
sentence or line the correction is attached to) that contains find exactly once. \
The edit is then located inside this context, so it lands on the intended spot — \
the one the mark was on — not another copy elsewhere. Leave context empty when \
find is already unique. Copy it verbatim from the same line as find, repairing \
only obvious extraction artifacts.
- instruction: the human note the correction came from, verbatim, for the report.
- kind: "mechanical" for a clear text change with one right answer; "judgment" \
when a person should decide (a vague or stylistic note, or one you cannot turn \
into an exact find/replace); "design" for a layout request that is not a text \
edit at all (move a chapter, change spacing).
- occurrence: 0 when the text should be unique, or the 1-based Nth when the \
correction names a specific instance among several.

Rules:
- Extract only corrections that are actually stated. Never invent one.
- If a correction is described but you cannot pin the exact text to change, \
still emit it as kind "judgment" with the note in instruction, so a human sees \
it — do not drop it and do not guess a location.
- One edit per correction. Do not merge or split them."""


def extract_edits(source_text: str, provider: Provider, *, model: str,
                  usage: Usage, max_tokens: int = MAX_OUTPUT_TOKENS
                  ) -> ParseResult:
    """Read a free-form corrections source into a `ParseResult`.

    `usage` accrues the model call's tokens. Raises `ExtractionError` if the
    call itself fails (refusal, truncation, no answer); per-entry problems come
    back as `ParseResult.issues`, not exceptions."""
    if not source_text.strip():
        return ParseResult(edits=(), issues=())
    result = provider.complete_structured(
        model=model, system=_SYSTEM, user=source_text,
        schema=strict_json_schema(_Extracted), schema_name="corrections",
        max_tokens=max_tokens)
    usage.add(result.usage, model=model)
    if result.stop_reason != "ok" or result.parsed is None:
        raise ExtractionError(
            result.error or result.stop_reason or "no answer from the model")
    # parse_edits owns the "flag, never guess" contract and accepts the
    # {"edits": [...]} wrapper the schema produces, so validation lands in one
    # place whether a list was typed or extracted.
    return parse_edits(result.parsed)


def extract_edits_batched(sources: Sequence[str], provider: Provider, *,
                          model: str, usage: Usage,
                          max_tokens: int = MAX_OUTPUT_TOKENS,
                          progress: Callable[[int, int, ParseResult], None]
                          | None = None) -> ParseResult:
    """Extract several bounded corrections sources in turn, concatenated into one
    `ParseResult`. Each source is read by its own model call, so no single call
    can overrun the output ceiling and truncate — the failure mode that silently
    dropped every edit when a large proof was read in one shot.

    Edits keep their source order (batch 1's edits, then batch 2's, …), which is
    the order the reviewer expects. `usage` accrues across all calls. A batch
    that fails raises `ExtractionError` as the single-call form does — the caller
    decides whether to surface a partial result; the batches read so far are not
    lost, because the caller drives the loop when it wants per-batch recovery.
    `progress(done, total, cumulative)` is called after each batch."""
    all_edits: list = []
    all_issues: list = []
    total = len(sources)
    for i, src in enumerate(sources, 1):
        result = extract_edits(src, provider, model=model, usage=usage,
                               max_tokens=max_tokens)
        all_edits.extend(result.edits)
        all_issues.extend(result.issues)
        if progress is not None:
            progress(i, total,
                     ParseResult(edits=tuple(all_edits), issues=tuple(all_issues)))
    return ParseResult(edits=tuple(all_edits), issues=tuple(all_issues))
