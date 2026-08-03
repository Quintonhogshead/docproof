"""Which models docproof offers, what they cost, and what they accept.

Prices are $/million tokens, verified against each vendor's published pricing
on 2026-08-03. They drive both the cost estimate in summary.md and the "about
$X for these files" hint in the app, so a stale number here is a number the
user sees. Re-check them when a model is added.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    id: str
    provider: str                 # "anthropic" | "openai"
    display: str                  # what a non-technical user reads
    blurb: str                    # one line of plain-language guidance
    input_per_mtok: float
    output_per_mtok: float
    supports_effort: bool = True  # cheap-mode hint (Anthropic output_config
                                  # .effort / OpenAI reasoning.effort)
    batch_discount: float = 0.5   # both vendors: flat 50%, any hour of day


MODELS: tuple[ModelInfo, ...] = (
    # --- Anthropic -----------------------------------------------------------
    ModelInfo("claude-opus-5", "anthropic", "Claude Opus 5",
              "Most thorough. Best on subtle grammar and style calls.",
              5.00, 25.00),
    ModelInfo("claude-sonnet-5", "anthropic", "Claude Sonnet 5",
              "Balanced. A good default for full manuscripts.",
              # Standard rates; an introductory $2/$10 runs through 2026-08-31,
              # so real bills may come in under the estimate until then.
              3.00, 15.00),
    ModelInfo("claude-haiku-4-5", "anthropic", "Claude Haiku 4.5",
              "Fastest and cheapest. Good for spelling and typos.",
              1.00, 5.00, supports_effort=False),
    # --- OpenAI --------------------------------------------------------------
    ModelInfo("gpt-5.6-sol", "openai", "ChatGPT 5.6 Sol",
              "Most thorough of the ChatGPT models.",
              5.00, 30.00),
    ModelInfo("gpt-5.6-terra", "openai", "ChatGPT 5.6 Terra",
              "Balanced ChatGPT option.",
              2.00, 12.00),
    ModelInfo("gpt-5.6-luna", "openai", "ChatGPT 5.6 Luna",
              "Fastest and cheapest ChatGPT option.",
              0.20, 1.20),
)

BY_ID = {m.id: m for m in MODELS}


def lookup(model_id: str) -> ModelInfo | None:
    return BY_ID.get(model_id)


def provider_for(model_id: str, fallback: str) -> str:
    """The vendor that serves this model, or `fallback` for a model the
    catalog has never heard of (a new release, or a fine-tune)."""
    info = lookup(model_id)
    return info.provider if info else fallback


def estimate_cost(model_id: str, *, input_tokens: int, output_tokens: int,
                  batch: bool = False) -> float | None:
    """Dollars, or None when the model isn't in the catalog."""
    info = lookup(model_id)
    if info is None:
        return None
    rate = info.batch_discount if batch else 1.0
    return rate * (input_tokens * info.input_per_mtok
                   + output_tokens * info.output_per_mtok) / 1_000_000
