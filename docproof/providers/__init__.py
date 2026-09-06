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


def build_provider(cfg, *, api_key: str | None = None,
                   model: str | None = None) -> Provider:
    """The one place a vendor SDK is chosen. Imports are deferred so a missing
    optional SDK only breaks the provider that needs it.

    ``model`` overrides which model the provider is chosen FOR: a caller that
    resolved its own model (the audit's whole-book reader, say) must get that
    model's vendor, not ``cfg.api.model``'s — sending gpt-5.6-luna to the
    Anthropic SDK is a guaranteed request-time error. The ``cfg.api.provider``
    pin applies only to ``cfg.api.model`` itself."""
    if model is not None and model != cfg.api.model:
        name = provider_for(model, None)
    else:
        name = provider_for(cfg.api.model, cfg.api.provider)
    kwargs = {"api_key": api_key, "max_retries": cfg.api.max_retries,
              "prompt_caching": cfg.api.prompt_caching,
              "effort": cfg.api.effort}
    if name == "anthropic":
        # The subscription lane, when the deployment asks for it. Every Claude
        # call becomes a session turn billed to the Max plan instead of the
        # vendor API. Unavailable is FATAL on purpose: falling back to the API
        # here would spend real money under a config that promised it wouldn't.
        if getattr(cfg.api, "claude_lane", "api") == "subagent":
            from .subagent import SubagentProvider, availability
            ok, why = availability()
            if not ok:
                raise ProviderError(
                    f"api.claude_lane is 'subagent', so Claude models must run "
                    f"on the subscription, but the lane is unavailable: {why}. "
                    f"Refusing to fall back to the Anthropic API — that would "
                    f"bill API dollars this config forbids. Fix the lane, or "
                    f"set api.claude_lane: api to allow vendor billing.")
            return SubagentProvider(model=model or cfg.api.model)
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(**kwargs)
    if name == "openai":
        from .openai_provider import OpenAIProvider
        return OpenAIProvider(**kwargs)
    if name == "gemini":
        from .gemini_provider import GeminiProvider
        return GeminiProvider(**kwargs)
    if name == "deepinfra":
        from .deepinfra_provider import DeepInfraProvider
        return DeepInfraProvider(**kwargs)
    raise ProviderError(
        f"Unknown provider {name!r} for model {cfg.api.model!r}. "
        f"Set api.provider to 'anthropic', 'openai', 'gemini', or "
        f"'deepinfra'.")
