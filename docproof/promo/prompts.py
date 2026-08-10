"""What promo says to the model, loaded from a file.

The same idea as `config/prep/tagging.yaml` and `config/error_types/*.yaml`:
the prompt is a YAML people edit, not code they change. When the house copy
specs land — how a teaser reads, which platform each post is for — they are
edited into `config/promo/generation.yaml`, and promo generates differently
with no build. Two placeholders are filled in before the prompt is sent:
`{post_count}` in the system turn, `{title}` and `{manuscript}` in the user turn.
Substitution is literal, not str.format, so a manuscript full of braces is text,
not a template.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger("docproof.promo.prompts")


class PromoPromptError(Exception):
    """A generation prompt file that cannot be used."""


@dataclass(frozen=True)
class PromoPrompt:
    version: int
    name: str
    system_prompt: str
    user_template: str
    path: str

    def render_system(self, *, post_count: int) -> str:
        return self.system_prompt.replace("{post_count}", str(post_count)).strip()

    def render_user(self, *, title: str, manuscript: str) -> str:
        # {title} first, so a manuscript that happens to contain the literal
        # "{title}" is never touched — it goes in whole at the {manuscript} step.
        out = self.user_template.replace("{title}", title)
        out = out.replace("{manuscript}", manuscript)
        return out.strip()


def load_promo_prompt(path: str | Path, *,
                      override_dir: str | Path | None = None) -> PromoPrompt:
    source = Path(path)
    if override_dir:
        replacement = Path(override_dir) / source.name
        if replacement.is_file():
            log.info("Using the promo prompt at %s", replacement)
            source = replacement
    if not source.is_file():
        raise PromoPromptError(f"No promo prompt at {source.resolve()}")
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not raw.get("system_prompt"):
        raise PromoPromptError(f"{source}: system_prompt is required.")
    if not raw.get("user_template"):
        raise PromoPromptError(f"{source}: user_template is required.")
    return PromoPrompt(
        version=int(raw.get("version", 1)),
        name=str(raw.get("name") or source.stem),
        system_prompt=str(raw["system_prompt"]),
        user_template=str(raw["user_template"]),
        path=str(source))
