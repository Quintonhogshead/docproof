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
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from ..config import Config
from ..models import Usage
from ..providers import Provider, strict_json_schema
from ..utils.tokens import estimate_tokens
from .ingest import Manuscript, read_manuscript
from .model import PromoResult, SocialPost
from .prompts import PromoPrompt, load_promo_prompt
from .verify import ClaimCheck, verify_grounding
from .writers import write_posts, write_teaser

log = logging.getLogger("docproof.promo.pipeline")

OUTPUT_KINDS = ("teaser", "posts")


class PromoError(RuntimeError):
    """A promo run that cannot go on — too large for one pass, or an answer that
    did not come back usable. Carries a sentence meant for a person to read."""


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
            override_dir: str | Path | None = None) -> PreparedPromo:
    """Read the whole manuscript and render the prompt, or refuse a book too big
    for a single pass.

    Raises IngestError on a document promo cannot open, PromoError on one whose
    estimated size overflows the single-pass limit. Splitting a novel across
    calls (chapter by chapter) is a planned extension, not silent behaviour — a
    book that would need it stops here with a sentence saying so."""
    manuscript = read_manuscript(input_path)
    prompt = load_promo_prompt(resolve(config_dir, cfg.promo.generation_prompt),
                               override_dir=override_dir)
    system = prompt.render_system(post_count=cfg.promo.post_count)
    user = prompt.render_user(title=manuscript.title, manuscript=manuscript.text)
    tokens = estimate_tokens(system) + estimate_tokens(user)
    if tokens > cfg.promo.max_input_tokens:
        raise PromoError(
            f"{Path(input_path).name} is about {tokens:,} tokens, over the "
            f"{cfg.promo.max_input_tokens:,}-token single-pass limit for promo. "
            f"Promo reads the whole book in one call; splitting a long novel "
            f"across calls is a planned extension not yet built.")
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
    usage.add(result.usage)
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
