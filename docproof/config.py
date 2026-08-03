from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class APIConfig(BaseModel):
    model: str = "claude-opus-5"
    # Which vendor serves `model`. Only consulted for models the catalog
    # doesn't recognise — a known model brings its own provider.
    provider: Literal["anthropic", "openai"] = "anthropic"
    max_retries: int = Field(default=2, ge=0)
    max_output_tokens: int = Field(default=16000, ge=1)
    prompt_caching: bool = True
    # Reasoning depth. Grammar detection is a precise, well-specified task, so
    # a low setting is both cheaper and no less accurate here. Ignored on
    # models that don't accept it. null omits the parameter entirely.
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "low"


class ChunkingConfig(BaseModel):
    token_budget: int = Field(default=2500, ge=1)         # soft target per chunk
    hard_cap_tokens: int = Field(default=8000, ge=1)      # beyond this, split paragraph
    min_paragraph_chars: int = Field(default=20, ge=0)


class SkipConfig(BaseModel):
    styles: list[str] = Field(default_factory=lambda: [
        "Heading*", "Title", "Subtitle", "TOC*", "Caption"])


class PricingConfig(BaseModel):
    """Optional $/MTok rates for the cost estimate in summary.md.
    Leave unset to omit the estimate."""
    input_per_mtok: float | None = None
    output_per_mtok: float | None = None


class Config(BaseModel):
    # CLI flags overwrite fields after load; validate those too.
    model_config = ConfigDict(validate_assignment=True)

    api: APIConfig = Field(default_factory=APIConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    skip: SkipConfig = Field(default_factory=SkipConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    min_confidence: Literal["low", "medium", "high"] = "medium"
    # Each entry is one API pass over the whole document: a bare key runs alone,
    # a list of keys runs as a single combined pass. Grouping trades a little
    # detection focus for a large cut in input tokens — see docs/error-types.md.
    error_types: list[str | list[str]] = Field(default_factory=list)
    tracked_changes_policy: Literal["abort", "accept_all_first", "ignore"] = "abort"
    output_dir: str = "output"
    comments: bool = True
    revision_author: str = "docproof"

    @field_validator("error_types")
    @classmethod
    def _validate_error_types(cls, value):
        seen: set[str] = set()
        for entry in value:
            keys = [entry] if isinstance(entry, str) else entry
            if not keys:
                raise ValueError("error_types: a group must list at least one key")
            for key in keys:
                if not key or not key.strip():
                    raise ValueError("error_types: keys must be non-empty")
                if key in seen:
                    raise ValueError(
                        f"error_types: '{key}' appears more than once; a key in "
                        f"two passes would review the document twice for it")
                seen.add(key)
        return value

    @property
    def error_type_groups(self) -> tuple[tuple[str, ...], ...]:
        """error_types normalized to one tuple per pass."""
        return tuple((entry,) if isinstance(entry, str) else tuple(entry)
                     for entry in self.error_types)

    @property
    def error_type_keys(self) -> tuple[str, ...]:
        """Every enabled key, flat, in pass order."""
        return tuple(k for group in self.error_type_groups for k in group)


def load_config(path: str | Path) -> Config:
    config_path = Path(path)
    if not config_path.is_file():
        raise FileNotFoundError(
            f"Configuration file not found: {config_path.resolve()}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"Configuration root must be a YAML mapping, not {type(raw).__name__}")
    return Config.model_validate(raw)
