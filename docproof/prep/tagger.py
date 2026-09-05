"""The one place prep talks to a model.

It asks a single question — what IS this paragraph — and the answer is
constrained by the schema to labels that exist in the house style sheet. The
model cannot return a style the template doesn't have, cannot return text, and
cannot change anything: everything that touches the document is deterministic
Python downstream of here.
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ValidationError, create_model

from ..models import Usage
from ..providers import Provider, strict_json_schema
from .chunker import Window, preview, split, windows
from .model import Structure, Tag
from .styles import BODY, SPACING, StyleSheet

log = logging.getLogger("docproof.prep.tagger")

TAGGING_FILE = "tagging.yaml"


class TaggingPromptError(Exception):
    """A tagging prompt file that cannot be used."""


@dataclass(frozen=True)
class TaggingPrompt:
    version: int
    name: str
    system_prompt: str
    context_header: str
    path: str

    def render(self, sheet: StyleSheet) -> str:
        """Fill the placeholders in, by name.

        Deliberately a substitution rather than str.format: the prompt shows
        the model a JSON object, and a file people edit should not blow up
        because it contains a brace."""
        out = self.system_prompt
        for key, value in (("sheet", sheet.name),
                           ("trim", sheet.trim or "the house trim"),
                           ("glyph", sheet.scene_break_glyph),
                           ("choices", sheet.describe_choices())):
            out = out.replace("{" + key + "}", value)
        return out.strip()


def load_tagging_prompt(path: str | Path, *,
                        override_dir: str | Path | None = None) -> TaggingPrompt:
    source = Path(path)
    if override_dir:
        replacement = Path(override_dir) / source.name
        if replacement.is_file():
            log.info("Using the tagging prompt at %s", replacement)
            source = replacement
    if not source.is_file():
        raise TaggingPromptError(f"No tagging prompt at {source.resolve()}")
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not raw.get("system_prompt"):
        raise TaggingPromptError(f"{source}: system_prompt is required.")
    return TaggingPrompt(
        version=int(raw.get("version", 1)),
        name=str(raw.get("name") or source.stem),
        system_prompt=str(raw["system_prompt"]),
        context_header=str(raw.get("context_header")
                           or "Already labelled, for continuity:"),
        path=str(source))


class RawTag(BaseModel):
    para_id: str
    role: str
    flag: str = ""


def build_output_model(choices: tuple[str, ...]) -> type[BaseModel]:
    """A schema whose `role` is an enum over exactly this style sheet's labels.
    An off-template style name is then impossible on the wire, not something
    the validator has to catch after the fact."""
    row = create_model("RawTag", __base__=RawTag,
                       role=(Literal[choices], ...))
    return create_model("TagList", paragraphs=(list[row], ...))


def _attr(value: str) -> str:
    """A style name that will sit inside the pseudo-XML the model reads without
    closing a tag early. The paragraph text is not escaped either — this is a
    prompt, not a document."""
    return value.replace('"', "'").replace("<", "(").replace(">", ")")


def render_window(window: Window, assigned: dict[str, str], *,
                  header: str, preview_chars: int) -> str:
    """The user turn: a little already-decided context, then the paragraphs to
    label. Everything else lives in the system prompt, which bills once per run
    and is read from cache after that."""
    parts: list[str] = []
    if window.context:
        lines = [f"[{p.para_id}] {assigned.get(p.para_id, '?')} — "
                 f"{preview(p, 90) or '(blank line)'}" for p in window.context]
        parts.append(header.strip() + "\n" + "\n".join(lines))

    blocks = []
    for p in window.paragraphs:
        if p.is_blank:
            blocks.append(f'<blank id="{p.para_id}"/>')
        else:
            # What the manuscript already called this paragraph, when it called
            # it anything. Most are "Normal" and worth no tokens, but a
            # "Heading 1" or a "TOC 2" is the author telling us what they meant.
            named = ""
            if p.style and p.style.lower() not in ("normal", "body text"):
                named = f' style="{_attr(p.style)}"'
            blocks.append(f'<p id="{p.para_id}" words="{p.words}"{named}>'
                          f'{preview(p, preview_chars)}</p>')
    parts.append("\n".join(blocks))
    return "\n\n".join(parts)


class Tagger:
    """One pass over the manuscript, window by window, in order."""

    def __init__(self, sheet: StyleSheet, prompt: TaggingPrompt,
                 provider: Provider, *, model: str, max_paragraphs: int = 120,
                 token_budget: int = 6000, context: int = 8,
                 preview_chars: int = 400, max_output_tokens: int = 16000):
        self.sheet = sheet
        self.prompt = prompt
        self.provider = provider
        self.model = model
        self.max_paragraphs = max_paragraphs
        self.token_budget = token_budget
        self.context = context
        self.preview_chars = preview_chars
        self.max_output_tokens = max_output_tokens
        self.system_prompt = prompt.render(sheet)
        self.output_model = build_output_model(sheet.model_choices)
        self.schema = strict_json_schema(self.output_model)


    def plan_windows(self, structure: Structure) -> list[Window]:
        return windows(structure.taggable, max_paragraphs=self.max_paragraphs,
                       token_budget=self.token_budget, context=self.context,
                       preview_chars=self.preview_chars)

    def tag(self, structure: Structure, usage: Usage, *,
            progress=None, checkpoint=None) -> list[Tag]:
        """Label every window, in order.

        `checkpoint` (docproof.checkpoint.Checkpoint, loaded) makes the pass
        resumable: each finished window's tags are saved as they land, and a
        restart replays them instead of paying for them again. The replay
        also rebuilds `assigned`, so the first live window still gets its
        context — the same labels it would have seen in an unbroken run."""
        from ..checkpoint import add_usage, snapshot, usage_delta

        planned = self.plan_windows(structure)
        assigned: dict[str, str] = {}
        tags: list[Tag] = []
        for done, window in enumerate(planned, start=1):
            key = f"w{window.index}"
            cached = checkpoint.get(key) if checkpoint else None
            if cached is not None:
                for item in cached.items:
                    tag = Tag(**item)
                    tags.append(tag)
                    assigned[tag.para_id] = tag.role
                add_usage(usage, cached.usage)
            else:
                before = snapshot(usage)
                fresh = self._tag_window(window, assigned, usage)
                tags.extend(fresh)
                if checkpoint:
                    checkpoint.put(
                        key,
                        items=[dataclasses.asdict(t) for t in fresh],
                        usage=usage_delta(before, usage), ok=True)
            if progress:
                progress(done, len(planned))
        return self._order(tags, structure)


    def _tag_window(self, window: Window, assigned: dict[str, str],
                    usage: Usage) -> list[Tag]:
        parsed = self._ask(window, assigned, usage)
        if parsed is None:
            halves = split(window)
            if not halves:
                return self._unanswered(window, assigned)
            log.warning("Window of %d paragraph(s) failed; retrying in two "
                        "halves.", len(window.paragraphs))
            out: list[Tag] = []
            for half in halves:
                out.extend(self._tag_window(half, assigned, usage))
            return out

        wanted = window.ids
        seen: set[str] = set()
        out = []
        for row in parsed.paragraphs:
            if row.para_id not in wanted or row.para_id in seen:
                log.debug("Ignoring a label for %r, which was not asked about "
                          "in this window.", row.para_id)
                continue
            seen.add(row.para_id)
            assigned[row.para_id] = row.role
            out.append(Tag(para_id=row.para_id, role=row.role,
                           flag=(row.flag or "").strip()))
        missing = [p for p in window.paragraphs if p.para_id not in seen]
        if missing:
            log.warning("%d paragraph(s) came back unlabelled; they will be "
                        "treated as running text and flagged.", len(missing))
            out.extend(self._unanswered(Window(window.index, tuple(missing)),
                                        assigned))
        return out

    def _ask(self, window: Window, assigned: dict[str, str],
             usage: Usage) -> BaseModel | None:
        result = self.provider.complete_structured(
            model=self.model,
            system=self.system_prompt,
            user=render_window(window, assigned,
                               header=self.prompt.context_header,
                               preview_chars=self.preview_chars),
            schema=self.schema,
            schema_name="paragraph_styles",
            max_tokens=self.max_output_tokens,
        )
        usage.add(result.usage, model=self.model)
        if result.stop_reason != "ok" or result.parsed is None:
            log.error("Window %d: %s", window.index,
                      result.error or result.stop_reason)
            return None
        try:
            return self.output_model.model_validate(result.parsed)
        except ValidationError as e:
            log.error("Window %d: the answer did not match the label schema: %s",
                      window.index, e)
            return None

    def _unanswered(self, window: Window,
                    assigned: dict[str, str]) -> list[Tag]:
        """A window nobody could label. Treating the paragraphs as body is the
        conservative reading — it changes no structure — but it is never done
        quietly: every one of them is flagged for the designer."""
        out = []
        for p in window.paragraphs:
            role = SPACING if p.is_blank else BODY
            assigned[p.para_id] = role
            out.append(Tag(para_id=p.para_id, role=role, source="unanswered",
                           flag="Not labelled — check this paragraph by hand."))
        return out

    def _order(self, tags: list[Tag], structure: Structure) -> list[Tag]:
        """Back into document order, with anything still missing filled in.
        The writers walk the document, so a gap here would be a silent
        untagged paragraph."""
        by_id = {t.para_id: t for t in tags}
        out = []
        for p in structure.taggable:
            tag = by_id.get(p.para_id)
            if tag is None:
                tag = Tag(para_id=p.para_id,
                          role=SPACING if p.is_blank else BODY,
                          source="unanswered",
                          flag="Not labelled — check this paragraph by hand.")
            out.append(tag)
        return out


class MockTagger:
    """Same interface, no API. Labels everything running text, which is enough
    to exercise the rules, both writers and the verifier on a real manuscript
    for free."""

    def __init__(self, sheet: StyleSheet, canned: dict[str, str] | None = None):
        self.sheet = sheet
        self.canned = canned or {}

    def plan_windows(self, structure: Structure) -> list[Window]:
        return [Window(0, structure.taggable)]

    def tag(self, structure: Structure, usage: Usage, *, progress=None,
            checkpoint=None):
        if progress:
            progress(1, 1)
        return [Tag(para_id=p.para_id,
                    role=self.canned.get(p.para_id,
                                         SPACING if p.is_blank else BODY))
                for p in structure.taggable]
