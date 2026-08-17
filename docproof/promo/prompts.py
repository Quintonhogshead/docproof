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


@dataclass(frozen=True)
class PlanMeta:
    """The author/book facts the marketing-plan prompt takes beyond the book.

    On a manual panel run the operator types these; on the watched-folder run
    they come from HubSpot (the pen name) and the Drive folder (blurbs, city,
    keywords, and the author's publicity-questionnaire answers). Every field but
    the title may be blank — the prompt degrades to a thinner but valid plan — so
    a bare manuscript still produces one.

    `questionnaire` is the author's own answers to the press's publicity
    questionnaire (the PNQ): free-text Q&A the author filled out — where they
    live, their platform and communities, who they think their readers are. It is
    author-provided fact, grounded the same way blurbs are, and it may itself name
    the city the Local & Regional Opportunities section keys off."""

    title: str
    author_name: str = ""
    keywords: str = ""
    back_cover: str = ""
    author_city: str = ""
    questionnaire: str = ""


@dataclass(frozen=True)
class PlanPrompt:
    """The marketing-plan prompt. Separate from PromoPrompt because it fills a
    different set of placeholders — the author/book metadata, not a post count —
    though it loads from the same YAML shape."""

    version: int
    name: str
    system_prompt: str
    user_template: str
    path: str

    def render_system(self) -> str:
        return self.system_prompt.strip()

    def render_user(self, meta: PlanMeta, *, manuscript: str) -> str:
        # {manuscript} is substituted last, so a book that itself contains the
        # literal "{author_city}" (or any other placeholder) is never touched —
        # only the metadata, which a person wrote, can carry a placeholder.
        # A blank optional field becomes an em dash so the block reads as "none
        # given", except the pen name: it feeds the title heading, where a dash
        # would print literally, so it is left empty and the prompt heads the
        # plan with the title alone when it is absent.
        out = self.user_template
        out = out.replace("{title}", meta.title)
        out = out.replace("{author_name}", meta.author_name)
        for key, value in (("keywords", meta.keywords),
                           ("back_cover", meta.back_cover),
                           ("author_city", meta.author_city),
                           ("questionnaire", meta.questionnaire)):
            out = out.replace("{" + key + "}", value or "—")
        out = out.replace("{manuscript}", manuscript)
        return out.strip()


def _load_prompt_yaml(path: str | Path,
                      override_dir: str | Path | None) -> tuple[dict, Path]:
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
    return raw, source


def load_promo_prompt(path: str | Path, *,
                      override_dir: str | Path | None = None) -> PromoPrompt:
    raw, source = _load_prompt_yaml(path, override_dir)
    return PromoPrompt(
        version=int(raw.get("version", 1)),
        name=str(raw.get("name") or source.stem),
        system_prompt=str(raw["system_prompt"]),
        user_template=str(raw["user_template"]),
        path=str(source))


def load_plan_prompt(path: str | Path, *,
                     override_dir: str | Path | None = None) -> PlanPrompt:
    raw, source = _load_prompt_yaml(path, override_dir)
    return PlanPrompt(
        version=int(raw.get("version", 1)),
        name=str(raw.get("name") or source.stem),
        system_prompt=str(raw["system_prompt"]),
        user_template=str(raw["user_template"]),
        path=str(source))
