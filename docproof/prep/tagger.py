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

# When the model has stopped answering, the run stops too — it does not label
# the rest of the book "body" and ship it. Two tells, either one enough:
#
# - `ERROR_STREAK`: this many provider errors in a row. One failed window is
#   halved and retried, as it always was; a dozen in a row is an account that
#   is out of credit or a key that no longer works, and halving a window can
#   not fix either. A 39,000-word book once spent two hours and 2,717 calls
#   discovering that, one paragraph at a time.
# - `MAX_UNANSWERED_SHARE`: however the run went, a book with this share of
#   its paragraphs unlabelled is not a formatted book. A healthy run leaves a
#   handful unanswered at most.
ERROR_STREAK = 8
MAX_UNANSWERED_SHARE = 0.25

# Provider errors that mean the account, not the window: the run stops on the
# first one. Everything else (a 500, a timeout, an answer that did not parse)
# keeps the old halve-and-retry, because the next call may well succeed.
_ACCOUNT_STATUSES = ("401", "402", "403")
_ACCOUNT_TELLS = ("insufficient_quota", "quota", "billing", "credit",
                  "invalid_api_key", "api key", "authentication")


class TaggingPromptError(Exception):
    """A tagging prompt file that cannot be used."""


class ModelUnavailable(RuntimeError):
    """The model stopped answering, so the manuscript was not labelled.

    Raised rather than absorbed: every unanswered paragraph used to become
    "body, flagged", which is the right reading for one window nobody could
    label and the wrong one for a whole book — that run finished "done", at
    $0, with every paragraph flagged, and was uploaded to the author's folder.
    The windows already answered are in the checkpoint; a retry once the
    account is back replays them for nothing."""

    def __init__(self, reason: str, *, answered: int = 0, unanswered: int = 0):
        super().__init__(reason)
        self.reason = reason
        self.answered = answered
        self.unanswered = unanswered


def is_account_error(error: str | None) -> bool:
    """Whether a provider error says the account is the problem — no credit,
    no key, no permission — rather than this one request."""
    text = (error or "").strip().lower()
    # The providers format an HTTP failure as "<status>: <message>".
    if text.split(":", 1)[0].strip() in _ACCOUNT_STATUSES:
        return True
    return any(tell in text for tell in _ACCOUNT_TELLS)


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
        self._errors_in_a_row = 0
        self._last_error = ""


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
        self._errors_in_a_row = 0
        self._last_error = ""
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
                # Only an answered window is worth keeping: a retry should
                # ask again about paragraphs the model left out, not replay
                # the silence. This is what lets a run stopped by
                # `ModelUnavailable` resume without carrying its defaults.
                answered = all(t.source != "unanswered" for t in fresh)
                if checkpoint and answered:
                    checkpoint.put(
                        key,
                        items=[dataclasses.asdict(t) for t in fresh],
                        usage=usage_delta(before, usage), ok=True)
            if progress:
                progress(done, len(planned))
        ordered = self._order(tags, structure)
        unanswered = sum(1 for t in ordered if t.source == "unanswered")
        if ordered and unanswered / len(ordered) > MAX_UNANSWERED_SHARE:
            raise ModelUnavailable(
                f"The model left {unanswered} of {len(ordered)} paragraphs "
                f"unlabelled" + (f" (last error: {self._last_error})"
                                 if self._last_error else "")
                + ". The manuscript was not formatted.",
                answered=len(ordered) - unanswered, unanswered=unanswered)
        return ordered


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
            error = result.error or result.stop_reason
            log.error("Window %d: %s", window.index, error)
            self._note_error(error)
            return None
        try:
            parsed = self.output_model.model_validate(result.parsed)
        except ValidationError as e:
            log.error("Window %d: the answer did not match the label schema: %s",
                      window.index, e)
            self._note_error(f"the answer did not match the label schema: {e}")
            return None
        self._errors_in_a_row = 0
        return parsed

    def _note_error(self, error: str) -> None:
        """One more call the model did not answer. An account-level error
        stops the run at once; anything else stops it after `ERROR_STREAK`
        in a row, which is when halving windows has stopped being a retry
        and become a way of asking the same dead account 2,000 times."""
        self._last_error = error
        self._errors_in_a_row += 1
        if is_account_error(error):
            raise ModelUnavailable(
                f"The model refused the request ({error}). Check the "
                f"provider's account and credit; the manuscript was not "
                f"formatted and will be retried.")
        if self._errors_in_a_row >= ERROR_STREAK:
            raise ModelUnavailable(
                f"The model failed {self._errors_in_a_row} requests in a row "
                f"(last: {error}). The manuscript was not formatted and will "
                f"be retried.")

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
