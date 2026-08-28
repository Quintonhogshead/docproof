"""Sample-sweep lite: six cheap Luna calls, one per party member's lane, over
the first ~800 words — a taste of what each member actually catches, shown as
before→after scraps while the waiting room plays.

This is a *taste*, not the pipeline: one small model, no LanguageTool rules, no
judge gates, no whole-book memory. So every lane is told to quote the author's
own words and to return nothing rather than invent a problem — the copy on the
page promises "the kind of thing each of us catches," and this has to earn that
by only ever showing real snags from the real pages.

The six calls run in parallel (`iter_sweep` yields each lane the moment it
lands, so the page can pin scraps up as they arrive). A lane that fails, refuses,
or returns junk yields an empty catch list with the reason logged — the sweep is
a flourish over the quote, never a gate in front of it.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Iterator

from pydantic import BaseModel, ValidationError

from ..models import Usage
from ..providers import Provider, cost_of_usage, strict_json_schema
from .skin import LUNA_MODEL

log = logging.getLogger("docproof.quest.sweep")

# How much of the book each lane reads. The first pages only — a taste, and the
# cheapest possible one: at ~800 words a detector read is nearly free, so the
# whole six-lane sweep costs a cent or two, findings and all.
SWEEP_WORDS = 800

# At most this many scraps per member — three is a taste, not a report.
MAX_CATCHES = 3

# Reasoning models share max_tokens with thinking, and a truncated structured
# reply parses as nothing; three short catches need little, so leave room.
MAX_OUTPUT_TOKENS = 4000

# One lane per member: (key, member name, what the model hunts for in the
# sample, and how to fill before/after so the scrap reads honestly). Kept beside
# skin.ROLES on purpose — same six, same order, same identities.
LANES = (
    ("pip", "Pip",
     "misspelled words, doubled words, and literal typos",
     'Put the author\'s exact wrong word or phrase in "before" and the '
     'correction in "after" (e.g. before "teh", after "the"; before "the the '
     'door", after "the door").'),
    ("bram", "Bram",
     "grammar and punctuation slips — comma splices, run-ons, dialogue "
     "punctuation, apostrophes, agreement",
     'Put the author\'s own clause in "before" and the corrected version in '
     '"after" (e.g. before "\\"Run\\" she said", after "\\"Run,\\" she said").'),
    ("maple", "Maple",
     "inconsistencies visible within these pages — a name, spelling, "
     "hyphenation, or capitalization used two ways",
     'Put both forms in "before" (e.g. "grey / gray") and "pick one" in '
     '"after". These pages are a small window, so it is normal to find little '
     'here — return an empty list rather than stretch.'),
    ("cinder", "Cinder",
     "badly tangled, garbled, or broken sentences that need real repair",
     'Put the tangled sentence (trimmed if long) in "before" and a clean '
     'rewrite in "after".'),
    ("sage", "Sage",
     "continuity or logic snags inside the passage — a fact that contradicts "
     "itself, a timeline or detail that does not add up",
     'Put the detail in "before" and the question you would raise in "after" '
     '(e.g. after "flagged as a question — never changed"). Sage watches the '
     'whole book; over a few pages there may be nothing, and that is fine — '
     'return an empty list.'),
    ("lark", "Lark",
     "optional line-level style notes — a flat line that could sing, a word "
     "repeated close together, a filler phrase",
     'Put the author\'s line in "before" and a gentler direction in "after". '
     'Lark only ever suggests: make "why" say it is a suggestion, never a '
     'change.'),
)


class LaneCatch(BaseModel):
    """One scrap: the author's own words, what this member would do, and a few
    words naming what it is."""
    before: str
    after: str
    why: str


class LaneCatches(BaseModel):
    catches: list[LaneCatch]


@dataclass(frozen=True)
class LaneResult:
    """One member's take on the first pages — up to three scraps, plus what it
    cost and (when the call did not land) why the list is empty."""
    key: str
    catches: list[dict] = field(default_factory=list)
    cost: float | None = None
    error: str | None = None


def sweep_sample(text: str) -> str:
    """The first SWEEP_WORDS words — the taste every lane reads."""
    return " ".join(text.split()[:SWEEP_WORDS])


def _system_prompt(name: str, hunts: str, howto: str) -> str:
    return f"""You are {name}, one member of Spell & Check, a proofreading party. \
You are shown the FIRST FEW PAGES of a manuscript. Your single job on this pass \
is to find {hunts}.

This is a taste, not a full edit — show at most {MAX_CATCHES} of the clearest \
real examples so the author sees the kind of thing you catch.

Rules, strictly:
- Only report issues that ACTUALLY appear in the text below. Quote the author's \
own words verbatim in "before" — never paraphrase, never invent an example.
- {howto}
- "why" is a short label (2–6 words) naming what it is, e.g. "comma splice", \
"doubled word", "homophone slip".
- Keep every field short: a phrase or a single sentence, trimmed if needed.
- If you find nothing in your lane in these pages, return an empty list. Finding \
nothing is a fine and honest answer — do NOT manufacture a problem to fill space.
- Stay tasteful: quote only what a stranger could read over the author's shoulder."""


def run_lane(sample: str, lane: tuple[str, str, str, str], provider: Provider,
             *, model: str = LUNA_MODEL) -> LaneResult:
    """One member's cheap read of the sample. Never raises: a failed, refused,
    or malformed call becomes an empty LaneResult with the reason in `error`,
    because a scrap missing is never worth failing the whole sweep."""
    key, name, hunts, howto = lane
    usage = Usage()
    try:
        result = provider.complete_structured(
            model=model,
            system=_system_prompt(name, hunts, howto),
            user=f"FIRST PAGES:\n{sample}",
            schema=strict_json_schema(LaneCatches),
            schema_name="quest_sweep_lane",
            max_tokens=MAX_OUTPUT_TOKENS,
        )
        usage.add(result.usage, model=model)
        if result.stop_reason != "ok" or result.parsed is None:
            return LaneResult(key=key, cost=cost_of_usage(
                usage, fallback_model=model), error=(
                    result.error or result.stop_reason))
        parsed = LaneCatches.model_validate(result.parsed)
    except ValidationError as e:
        return LaneResult(key=key, error=f"schema mismatch: {e}")
    except Exception as e:  # noqa: BLE001 - SDK/network variants; sweep is optional
        return LaneResult(key=key, error=f"lane call failed: {e}")
    catches = [c.model_dump() for c in parsed.catches[:MAX_CATCHES]]
    return LaneResult(key=key, catches=catches,
                      cost=cost_of_usage(usage, fallback_model=model))


def iter_sweep(text: str, provider: Provider, *,
               model: str = LUNA_MODEL) -> Iterator[LaneResult]:
    """Run all six lanes at once, yielding each the moment its call lands, so a
    caller can pin scraps up as they arrive rather than after the slowest one.

    Lane order out is completion order, not LANES order — the page keys each
    scrap to its member by `key`, so arrival order does not matter."""
    sample = sweep_sample(text)
    if not sample.strip():
        return
    with ThreadPoolExecutor(max_workers=len(LANES)) as pool:
        futures = {pool.submit(run_lane, sample, lane, provider, model=model):
                   lane[0] for lane in LANES}
        for fut in as_completed(futures):
            yield fut.result()


def sweep(text: str, provider: Provider, *,
          model: str = LUNA_MODEL) -> list[LaneResult]:
    """All six lanes, collected in LANES order — the batch view, for tests and
    any caller that does not care about streaming."""
    by_key = {r.key: r for r in iter_sweep(text, provider, model=model)}
    return [by_key[key] for key, *_ in LANES if key in by_key]
