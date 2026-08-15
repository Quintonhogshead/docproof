from __future__ import annotations

from .base import (BatchRequest, BatchStatus, NormalizedUsage, Provider,
                   ProviderError, ProviderResult, inlined_json_schema,
                   strict_json_schema)
from .catalog import (EFFORT_MULTIPLIER, MODELS, ModelInfo, cost_of_usage,
                      effort_multiplier, estimate_cost, lookup, provider_for)

__all__ = ["BatchRequest", "BatchStatus", "EFFORT_MULTIPLIER", "MODELS",
           "ModelInfo", "NormalizedUsage", "Provider", "ProviderError",
           "ProviderResult", "build_provider", "cost_of_usage",
           "effort_multiplier", "estimate_cost", "inlined_json_schema",
           "lookup", "provider_for", "strict_json_schema"]


def build_provider(cfg, *, api_key: str | None = None) -> Provider:
    """The one place a vendor SDK is chosen. Imports are deferred so a missing
    optional SDK only breaks the provider that needs it."""
    name = provider_for(cfg.api.model, cfg.api.provider)
    kwargs = {"api_key": api_key, "max_retries": cfg.api.max_retries,
              "prompt_caching": cfg.api.prompt_caching,
              "effort": cfg.api.effort}
    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(**kwargs)
    if name == "openai":
        from .openai_provider import OpenAIProvider
        return OpenAIProvider(**kwargs)
    if name == "gemini":
        from .gemini_provider import GeminiProvider
        return GeminiProvider(**kwargs)
    raise ProviderError(
        f"Unknown provider {name!r} for model {cfg.api.model!r}. "
        f"Set api.provider to 'anthropic', 'openai', or 'gemini'.")
