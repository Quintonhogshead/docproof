"""Promo, end to end.

The same three steps as review and prep — prepare, run, finish — so the app and
a future CLI drive it the way they already drive the others. What differs is the
middle: not many passes (review) nor one ordered pass over windows (prep), but a
single call with the whole book in front of the model, because a teaser has to
know how the story ends and a per-chunk read never could.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic import ValidationError

from ..config import Config
from ..models import Usage
from ..providers import Provider, strict_json_schema
from ..utils.tokens import estimate_tokens
from .ingest import Manuscript, read_manuscript
from .model import MarketingPlan, PromoResult, SocialPost
from .prompts import (PlanMeta, PlanPrompt, PromoPrompt, load_plan_prompt,
                      load_promo_prompt)
from .verify import ClaimCheck, verify_grounding
from .writers import write_plan, write_posts, write_teaser

log = logging.getLogger("docproof.promo.pipeline")

OUTPUT_KINDS = ("teaser", "posts")

# Rough size of what a promo run writes back, for the pre-run cost hint only.
# Output is a fixed deliverable — one teaser and post_count posts — so, unlike
# the manuscript, it does not grow with book length. These are estimate
# constants, not billed figures: the real spend is measured from usage after a
# run, like every other cost hint in the app.
TEASER_OUTPUT_TOKENS = 200
POST_OUTPUT_TOKENS = 120


def estimate_output_tokens(post_count: int) -> int:
    """A guess at the teaser-plus-posts the model writes back, for the drop-time
    cost estimate. Small next to a whole novel of input, and it does not scale
    with the book — the deliverable is fixed regardless of manuscript length."""
    return TEASER_OUTPUT_TOKENS + max(0, post_count) * POST_OUTPUT_TOKENS


class PromoError(RuntimeError):
    """A promo run that cannot go on — too large for one pass, or an answer that
    did not come back usable. Carries a sentence meant for a person to read."""


class PromoTooLarge(PromoError):
    """A book over the single-pass token limit, with the numbers behind the
    refusal so a caller can act on it — the watched folder emails them, the app
    offers the human override. Subclasses PromoError, so every existing
    `except PromoError` still catches it."""

    def __init__(self, message: str, *, tokens: int, limit: int, words: int):
        super().__init__(message)
        self.tokens = tokens
        self.limit = limit
        self.words = words


@dataclass
class PreparedPromo:
    manuscript: Manuscript
    prompt: PromoPrompt
    system: str
    user: str
    post_count: int
    est_input_tokens: int


@dataclass(frozen=True)
class PromoOutputs:
    documents: dict[str, Path]        # kind → written file
    data_json: Path                   # promo.json, the editable source of truth
    result: PromoResult
    unverified: tuple[str, ...]       # capitalised terms not found in the book
    words: int
    # Claims the entailment pass found the manuscript did not support, empty when
    # that pass is off or found nothing.
    unsupported_claims: tuple[str, ...] = ()

    @property
    def flag_count(self) -> int:
        """Everything a person might want to look at, across both checks."""
        return len(self.unverified) + len(self.unsupported_claims)


def resolve(config_dir: str | Path, value: str) -> Path:
    """A path from the config, read relative to the config file that named it."""
    path = Path(value)
    return path if path.is_absolute() else Path(config_dir) / path


def prepare(cfg: Config, input_path: str | Path, *, config_dir: str | Path,
            override_dir: str | Path | None = None,
            allow_oversize: bool = False) -> PreparedPromo:
    """Read the whole manuscript and render the prompt, or refuse a book too big
    for a single pass.

    Raises IngestError on a document promo cannot open, PromoError on one whose
    estimated size overflows the single-pass limit. Splitting a novel across
    calls (chapter by chapter) is a planned extension, not silent behaviour — a
    book that would need it stops here with a sentence saying so.

    `allow_oversize` is the human override: a person who has seen the size at
    drop time and chosen to run anyway sends the whole book past the limit. The
    limit is DocProof's own caution, not a model ceiling — the current models all
    take far more than 180k tokens — so overriding sends one large call and lets
    the provider be the judge. It only lifts the pre-check; a book that genuinely
    exceeds the chosen model's context window still fails at the call, with the
    provider's own error."""
    manuscript = read_manuscript(input_path)
    prompt = load_promo_prompt(resolve(config_dir, cfg.promo.generation_prompt),
                               override_dir=override_dir)
    system = prompt.render_system(post_count=cfg.promo.post_count)
    user = prompt.render_user(title=manuscript.title, manuscript=manuscript.text)
    tokens = estimate_tokens(system) + estimate_tokens(user)
    if tokens > cfg.promo.max_input_tokens:
        if not allow_oversize:
            raise PromoTooLarge(
                f"{Path(input_path).name} is about {tokens:,} tokens, over the "
                f"{cfg.promo.max_input_tokens:,}-token single-pass limit for "
                f"promo. Promo reads the whole book in one call; splitting a "
                f"long novel across calls is a planned extension not yet built.",
                tokens=tokens, limit=cfg.promo.max_input_tokens,
                words=manuscript.word_count)
        log.warning(
            "Promo running oversize by human override: %s is about %d tokens, "
            "over the %d-token single-pass limit — sending it anyway.",
            Path(input_path).name, tokens, cfg.promo.max_input_tokens)
    log.info("Promo ready: %d words, ~%d input tokens, %d post(s) requested.",
             manuscript.word_count, tokens, cfg.promo.post_count)
    return PreparedPromo(manuscript=manuscript, prompt=prompt, system=system,
                         user=user, post_count=cfg.promo.post_count,
                         est_input_tokens=tokens)


def run(cfg: Config, prepared: PreparedPromo,
        provider: Provider) -> tuple[PromoResult, Usage]:
    """One call: the whole book in, a teaser and the posts out."""
    usage = Usage()
    schema = strict_json_schema(PromoResult)
    result = provider.complete_structured(
        model=cfg.api.model,
        system=prepared.system,
        user=prepared.user,
        schema=schema,
        schema_name="promo_copy",
        max_tokens=cfg.api.max_output_tokens,
    )
    usage.add(result.usage, model=cfg.api.model)
    if result.stop_reason != "ok" or result.parsed is None:
        raise PromoError(
            f"The model did not return promo copy: "
            f"{result.error or result.stop_reason}.")
    try:
        parsed = PromoResult.model_validate(result.parsed)
    except ValidationError as e:
        raise PromoError(
            f"The model's answer did not match the promo schema: {e}") from e
    if len(parsed.posts) != prepared.post_count:
        # Not fatal: the count lives in the prompt, not the schema (a strict
        # JSON schema cannot carry a list length), so a stray count is the
        # model's, not a bug. The panel shows what came back for a human to fix.
        log.warning("Asked for %d post(s), got %d; keeping what came back.",
                    prepared.post_count, len(parsed.posts))
    return parsed, usage


def run_mock(prepared: PreparedPromo, *,
             canned: PromoResult | None = None) -> tuple[PromoResult, Usage]:
    """Everything downstream of the model, without the model. Exercises finish
    and the grounding check on a real manuscript for free."""
    result = canned or PromoResult(
        teaser="A gripping story you will not put down.",
        posts=[SocialPost(text=f"Sample post {i + 1}.")
               for i in range(prepared.post_count)])
    return result, Usage()


# --- the marketing plan -------------------------------------------------------
#
# Promo's third deliverable, and its own generation: a separate call because it
# takes author/book metadata (pen name, blurbs, city, keywords) the teaser call
# does not, and produces one Markdown document rather than the structured
# teaser+posts. It reuses prepare's manuscript read and the same oversize guard,
# so a book too big for the copy is too big for the plan and refused the same way.


@dataclass
class PreparedPlan:
    manuscript: Manuscript
    prompt: PlanPrompt
    meta: PlanMeta
    system: str
    user: str
    est_input_tokens: int


@dataclass(frozen=True)
class PlanOutputs:
    document: Path                    # {stem} - marketing plan.docx
    data_json: Path                   # marketing_plan.json, the editable source
    plan: MarketingPlan
    words: int


def prepare_plan(cfg: Config, input_path: str | Path, meta: PlanMeta, *,
                 config_dir: str | Path, override_dir: str | Path | None = None,
                 allow_oversize: bool = False) -> PreparedPlan:
    """Read the manuscript and render the plan prompt, or refuse an oversize book.

    Same contract and same single-pass guard as `prepare` — a marketing plan
    reads the whole novel in one call too — so `PromoTooLarge` carries the same
    numbers and `allow_oversize` is the same human override. `meta` is the
    author/book metadata the plan prompt needs beyond the book itself."""
    manuscript = read_manuscript(input_path)
    prompt = load_plan_prompt(resolve(config_dir, cfg.promo.plan_prompt),
                              override_dir=override_dir)
    # The manuscript filename is the title of record; a blank title in `meta`
    # falls back to it so the plan is never headed by an empty string.
    meta = meta if meta.title else replace(meta, title=manuscript.title)
    system = prompt.render_system()
    user = prompt.render_user(meta, manuscript=manuscript.text)
    tokens = estimate_tokens(system) + estimate_tokens(user)
    if tokens > cfg.promo.max_input_tokens:
        if not allow_oversize:
            raise PromoTooLarge(
                f"{Path(input_path).name} is about {tokens:,} tokens, over the "
                f"{cfg.promo.max_input_tokens:,}-token single-pass limit for "
                f"promo. The marketing plan reads the whole book in one call; "
                f"splitting a long novel across calls is a planned extension "
                f"not yet built.",
                tokens=tokens, limit=cfg.promo.max_input_tokens,
                words=manuscript.word_count)
        log.warning(
            "Plan running oversize by human override: %s is about %d tokens, "
            "over the %d-token single-pass limit — sending it anyway.",
            Path(input_path).name, tokens, cfg.promo.max_input_tokens)
    log.info("Plan ready: %d words, ~%d input tokens.",
             manuscript.word_count, tokens)
    return PreparedPlan(manuscript=manuscript, prompt=prompt, meta=meta,
                        system=system, user=user, est_input_tokens=tokens)


def run_plan(cfg: Config, prepared: PreparedPlan,
             provider: Provider) -> tuple[MarketingPlan, Usage]:
    """One call: the whole book plus the metadata in, the plan out."""
    usage = Usage()
    schema = strict_json_schema(MarketingPlan)
    result = provider.complete_structured(
        model=cfg.api.model,
        system=prepared.system,
        user=prepared.user,
        schema=schema,
        schema_name="marketing_plan",
        max_tokens=cfg.api.max_output_tokens,
    )
    usage.add(result.usage, model=cfg.api.model)
    if result.stop_reason != "ok" or result.parsed is None:
        raise PromoError(
            f"The model did not return a marketing plan: "
            f"{result.error or result.stop_reason}.")
    try:
        parsed = MarketingPlan.model_validate(result.parsed)
    except ValidationError as e:
        raise PromoError(
            f"The model's answer did not match the plan schema: {e}") from e
    if not parsed.plan.strip():
        raise PromoError("The model returned an empty marketing plan.")
    return parsed, usage


def run_plan_mock(prepared: PreparedPlan, *,
                  canned: MarketingPlan | None = None
                  ) -> tuple[MarketingPlan, Usage]:
    """finish_plan and the writer, without the model — a real manuscript through
    the whole plan round trip for free."""
    plan = canned or MarketingPlan(plan=(
        f"# {prepared.meta.title} — {prepared.meta.author_name or 'the author'}\n\n"
        "This is a resource for you, the author, to consider some publicity "
        "efforts and endeavors to pursue!\n\n"
        "## Target Audience\n\n- **Sample readers:** a placeholder audience.\n\n"
        "## Marketing Strategy\n\n- **Sample tactic:** a placeholder strategy.\n\n"
        "## Comparable Titles\n\n- **A Book:** a placeholder comparison.\n\n"
        "## Key Message\n\nA placeholder key message."))
    return plan, Usage()


def finish_plan(prepared: PreparedPlan, plan: MarketingPlan, cfg: Config, *,
                out_dir: str | Path, source_path: str | Path) -> PlanOutputs:
    """Write the plan document and the JSON it came from.

    No grounding check runs here, unlike `finish`: the plan's Comparable Titles
    are real books external to the manuscript by design, so the proper-noun
    guard that protects the teaser would flag every one of them. The plan is
    spoiler- and invention-guarded in the prompt instead."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = Path(source_path).stem or "manuscript"
    document = write_plan(out / f"{stem} - marketing plan.docx",
                          plan.plan, title=stem)
    data_path = out / "marketing_plan.json"
    data_path.write_text(
        json.dumps(plan.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8")
    return PlanOutputs(document=document, data_json=data_path, plan=plan,
                       words=prepared.manuscript.word_count)


def finish(prepared: PreparedPromo, result: PromoResult, usage: Usage,
           cfg: Config, *, out_dir: str | Path, source_path: str | Path,
           claims: tuple[ClaimCheck, ...] | list[ClaimCheck] = ()) -> PromoOutputs:
    """Write the two documents and the JSON they came from, and record the
    grounding checks. Nothing here blocks: a flagged term or claim is surfaced,
    not fatal. `claims` is the entailment pass's result when it ran (it needs a
    provider, so the caller runs it and hands it in); the deterministic term
    check runs here. `usage` is threaded through for the caller's cost ledger."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    kinds = [k for k in OUTPUT_KINDS if k in tuple(cfg.promo.outputs)]
    if not kinds:
        raise ValueError(
            f"Nothing to write: promo.outputs must include at least one of "
            f"{', '.join(OUTPUT_KINDS)}.")

    stem = Path(source_path).stem or "manuscript"
    written: dict[str, Path] = {}
    if "teaser" in kinds:
        written["teaser"] = write_teaser(
            out / f"{stem} - teaser.docx", result.teaser, title=stem)
    if "posts" in kinds:
        written["posts"] = write_posts(
            out / f"{stem} - social posts.docx", result.posts, title=stem)

    # The editable source of truth: the panel loads this, a human edits it, and
    # the documents above are rewritten from it.
    data_path = out / "promo.json"
    data_path.write_text(
        json.dumps(result.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8")

    unverified = tuple(verify_grounding(prepared.manuscript, result)
                       if cfg.promo.verify else ())
    if unverified:
        log.warning("Grounding: %d term(s) not found in the manuscript: %s",
                    len(unverified), ", ".join(unverified))
    unsupported = tuple(c.claim for c in claims if not c.supported)

    # A sidecar the panel reads to show a person exactly what to look at — kept
    # out of promo.json so editing the copy never has to round-trip it.
    (out / "promo_verify.json").write_text(
        json.dumps({"terms": list(unverified),
                    "claims": [c.model_dump() for c in claims]},
                   indent=2, ensure_ascii=False), encoding="utf-8")

    return PromoOutputs(documents=written, data_json=data_path, result=result,
                        unverified=unverified,
                        words=prepared.manuscript.word_count,
                        unsupported_claims=unsupported)
