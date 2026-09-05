"""Which models docproof offers, what they cost, and what they accept.

Prices are $/million tokens, verified against each vendor's published pricing
on 2026-08-04. They drive both the cost estimate in summary.md and the "about
$X for these files" hint in the app, so a stale number here is a number the
user sees. Re-check them when a model is added.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    id: str
    provider: str                 # "anthropic" | "openai" | "gemini" | "deepinfra"
    display: str                  # what a non-technical user reads
    blurb: str                    # one line of plain-language guidance
    input_per_mtok: float
    output_per_mtok: float
    supports_effort: bool = True  # cheap-mode hint (Anthropic output_config
                                  # .effort / OpenAI reasoning.effort)
    batch_discount: float = 0.5   # the three big vendors: flat 50%, any hour
    supports_batch: bool = True   # False = no batch endpoint at all; the app
                                  # greys out batch mode for the model


MODELS: tuple[ModelInfo, ...] = (
    # Fable is the most capable model available and priced accordingly — twice
    # Opus, ten times Haiku. Grammar detection is a precise, well-specified
    # task, so it is listed for the judgment-call error types rather than
    # recommended: the blurb says so plainly because this row is what a user
    # picks from. It also requires 30-day data retention (no zero-retention
    # orgs) and always thinks — docproof sends no `thinking` parameter, which
    # is what that model requires.
    ModelInfo("claude-fable-5", "anthropic", "Claude Fable 5",
              "The most capable reviewer there is, at twice the price of "
              "Opus. Overkill for most manuscripts.",
              10.00, 50.00),
    ModelInfo("claude-opus-5", "anthropic", "Claude Opus 5",
              "Most thorough. Best on subtle grammar and style calls.",
              5.00, 25.00),
    ModelInfo("claude-sonnet-5", "anthropic", "Claude Sonnet 5",
              "Balanced. A good default for full manuscripts.",
              3.00, 15.00),
    ModelInfo("claude-haiku-4-5", "anthropic", "Claude Haiku 4.5",
              "Fastest and cheapest. Good for spelling and typos.",
              1.00, 5.00, supports_effort=False),
    ModelInfo("gpt-5.6-sol", "openai", "ChatGPT 5.6 Sol",
              "Most thorough of the ChatGPT models.",
              5.00, 30.00),
    ModelInfo("gpt-5.6-terra", "openai", "ChatGPT 5.6 Terra",
              "Balanced ChatGPT option.",
              2.00, 12.00),
    ModelInfo("gpt-5.6-luna", "openai", "ChatGPT 5.6 Luna",
              "Fastest and cheapest ChatGPT option.",
              0.20, 1.20),
    # Gemini prices two of these by context length. A docproof request is one
    # chunk against a token budget of a few thousand, so the sub-200k tier is
    # the one that ever applies — those are the rates listed here.
    ModelInfo("gemini-3.1-pro-preview", "gemini", "Gemini 3.1 Pro",
              "Most thorough of the Gemini models.",
              2.00, 12.00),
    ModelInfo("gemini-3.6-flash", "gemini", "Gemini 3.6 Flash",
              "Balanced Gemini option.",
              1.50, 7.50),
    ModelInfo("gemini-3.1-flash-lite", "gemini", "Gemini 3.1 Flash Lite",
              "Fastest and cheapest Gemini option.",
              0.25, 1.50),
    # Open-weight models served per token. Prices from deepinfra.com model
    # pages on 2026-09-04. Nothing here is watermarked and inputs are not
    # retained after the call, but the text does transit a third party's GPU —
    # the choice is the user's, and the blurbs say so. No batch endpoint.
    # Context is what DeepInfra SERVES, which can be below the model's native
    # window: all four here take a whole novel (131k / 262k / 1M tokens).
    # Effort: gpt-oss takes reasoning_effort; the others are sent none, since
    # an unsupported parameter is a 400 on this endpoint rather than ignored.
    ModelInfo("openai/gpt-oss-120b", "deepinfra", "gpt-oss 120B (DeepInfra)",
              "OpenAI's open model, hosted. Very cheap, reads a whole novel; "
              "no watermark. Text leaves your machine for the call.",
              0.037, 0.17, supports_batch=False),
    ModelInfo("Qwen/Qwen3.6-27B", "deepinfra", "Qwen 3.6 27B (DeepInfra)",
              "Strong open-weight writer, 256k context, hosted. "
              "No watermark. Text leaves your machine for the call.",
              0.32, 3.20, supports_effort=False, supports_batch=False),
    ModelInfo("deepseek-ai/DeepSeek-V4-Flash", "deepinfra",
              "DeepSeek V4 Flash (DeepInfra)",
              "Cheapest frontier-class open model, 1M context, hosted. "
              "No watermark. Text leaves your machine for the call.",
              0.09, 0.18, supports_effort=False, supports_batch=False),
    ModelInfo("deepseek-ai/DeepSeek-V4-Pro", "deepinfra",
              "DeepSeek V4 Pro (DeepInfra)",
              "Most thorough open model here, 1M context, hosted. "
              "No watermark. Text leaves your machine for the call.",
              1.30, 2.60, supports_effort=False, supports_batch=False),
)

BY_ID = {m.id: m for m in MODELS}


def lookup(model_id: str) -> ModelInfo | None:
    return BY_ID.get(model_id)


def provider_for(model_id: str, fallback: str) -> str:
    """The vendor that serves this model, or `fallback` for a model the
    catalog has never heard of (a new release, or a fine-tune)."""
    info = lookup(model_id)
    return info.provider if info else fallback


# How much a deeper reasoning effort inflates what the model writes back. The
# effort dial changes only the model's own thinking, not the document, so it
# lands entirely on output tokens — the input (the manuscript) is unchanged.
# These are estimate multipliers for the cost hint, not billed figures: the
# real spend is measured from actual usage after a run. Low is the shipped
# default and the baseline, so it is 1.0. A model that ignores effort
# (supports_effort=False, e.g. Haiku) is never scaled.
EFFORT_MULTIPLIER: dict[str, float] = {
    "low": 1.0,
    "medium": 1.6,
    "high": 2.6,
    "xhigh": 4.0,
    "max": 6.0,
}


def effort_multiplier(model_id: str, effort: str | None) -> float:
    """The output-token cost factor for this model at this effort — 1.0 when
    the model ignores effort, or the effort is unset or unrecognised."""
    info = lookup(model_id)
    if info is None or not info.supports_effort or not effort:
        return 1.0
    return EFFORT_MULTIPLIER.get(effort, 1.0)


# Cache pricing as a multiple of a model's base input rate. Anthropic publishes
# these directly: a 5-minute cache write costs 1.25x input, a read 0.1x. OpenAI
# and Gemini bill a cached-input read at ~half and charge nothing extra to write
# (the write is just ordinary input), so 0.5x/1.0x. Reads dominate a repeated-
# context run — the whole-book passes re-send the manuscript every call — so
# Omitting reads is the larger of the two estimation errors.
# DeepInfra lists a cached-input rate per model, 1/5 to 1/10 of input; 0.2 is
# the conservative end.
_CACHE_READ_MULT = {"anthropic": 0.10, "openai": 0.50, "gemini": 0.25,
                    "deepinfra": 0.20}
_CACHE_WRITE_MULT = {"anthropic": 1.25, "openai": 1.0, "gemini": 1.0,
                     "deepinfra": 1.0}


def estimate_cost(model_id: str, *, input_tokens: int, output_tokens: int,
                  cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                  batch: bool = False, effort: str | None = None) -> float | None:
    """Dollars, or None when the model isn't in the catalog.

    Cache reads and writes are priced at the vendor's cache multiple of the input
    rate (see _CACHE_READ_MULT / _CACHE_WRITE_MULT), not the full input rate — on
    a whole-book run the cache-read count dwarfs the fresh input, so this is what
    turns the old "upper bound" into a real figure. `effort` scales the output
    half for a pre-run estimate (see EFFORT_MULTIPLIER); omit it to price actual
    usage, where the output tokens are already the real count."""
    info = lookup(model_id)
    if info is None:
        return None
    # A vendor with no batch endpoint has no batch price: the estimate for a
    # batch run is the same money as running it now, which is also what the
    # app shows beside a greyed-out batch option.
    rate = info.batch_discount if batch and info.supports_batch else 1.0
    scaled_output = output_tokens * effort_multiplier(model_id, effort)
    read_rate = info.input_per_mtok * _CACHE_READ_MULT.get(info.provider, 0.5)
    write_rate = info.input_per_mtok * _CACHE_WRITE_MULT.get(info.provider, 1.0)
    return rate * (input_tokens * info.input_per_mtok
                   + cache_read_tokens * read_rate
                   + cache_write_tokens * write_rate
                   + scaled_output * info.output_per_mtok) / 1_000_000


def cost_of_usage(usage, *, fallback_model: str,
                  batch: bool = False) -> float | None:
    """Total model cost for a run, summed at each model's own rate.

    Uses the per-model breakdown a run records (`usage.by_model`) so a mixed
    OpenAI/Anthropic review is priced correctly, not at one model's rate. Falls
    back to pricing the flat token total at `fallback_model` for older records
    that carry no breakdown. Sapling is excluded (it bills per character); the
    caller adds `usage.sapling_cost`. Accepts a Usage object or a plain dict, so
    the report path and the stored-record paths share one costing."""
    def _f(obj, name, default=0):
        return obj.get(name, default) if isinstance(obj, dict) \
            else getattr(obj, name, default)

    by_model = _f(usage, "by_model", None)
    if by_model:
        total, priced_any = 0.0, False
        for model_id, tk in by_model.items():
            c = estimate_cost(
                model_id or fallback_model,
                input_tokens=tk.get("input_tokens", 0),
                output_tokens=tk.get("output_tokens", 0),
                cache_read_tokens=tk.get("cache_read_input_tokens", 0),
                cache_write_tokens=tk.get("cache_creation_input_tokens", 0),
                batch=batch)
            if c is not None:
                total += c
                priced_any = True
        return total if priced_any else None
    return estimate_cost(
        fallback_model,
        input_tokens=_f(usage, "input_tokens", 0),
        output_tokens=_f(usage, "output_tokens", 0),
        cache_read_tokens=_f(usage, "cache_read_input_tokens", 0),
        cache_write_tokens=_f(usage, "cache_creation_input_tokens", 0),
        batch=batch)
