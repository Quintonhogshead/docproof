"""DeepSeek V4 Pro, reasoning on, writes the five teasers from the whole manuscript.

One conversation per package: the manuscript turn, then (only if needed) short
follow-up turns asking for a length fix or for the adjudicator's rewrites. Every
word of the package is the writer's own."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

from docproof.providers import strict_json_schema
from docproof.providers.base import inlined_json_schema
from docproof.providers.deepinfra_provider import BASE_URL, result_from_completion
from . import WRITER_EFFORT, WRITER_MODEL
from .models import Draft, digest, draft_issues
from . import prompts

# Reasoning shares the output allowance with the answer.
MAX_OUTPUT_TOKENS = 64_000
# A reasoning pass over a whole novel runs for minutes.
TIMEOUT_SECONDS = 1500
# Follow-up turns for a length or paragraph miss, in the same conversation.
FIX_ROUNDS = 3


class WriterError(RuntimeError):
    pass


class WriterUnavailable(WriterError):
    """DeepInfra refused or dropped the call; the book is not at fault."""


class Writer:
    def __init__(self, *, client=None, api_key=None):
        if client is None:
            import openai
            key = api_key or os.environ.get("DEEPINFRA_API_KEY")
            if not key:
                raise WriterUnavailable("DEEPINFRA_API_KEY is not set on the teaser worker.")
            client = openai.OpenAI(api_key=key, base_url=BASE_URL, max_retries=0,
                                   timeout=TIMEOUT_SECONDS)
        self.client = client

    def complete(self, messages: list[dict]) -> tuple[Draft | None, dict]:
        schema = inlined_json_schema(strict_json_schema(Draft))
        started = time.time()
        try:
            response = self.client.chat.completions.create(
                model=WRITER_MODEL, messages=messages, max_tokens=MAX_OUTPUT_TOKENS,
                reasoning_effort=WRITER_EFFORT,
                response_format={"type": "json_schema", "json_schema": {
                    "name": "author_teasers", "strict": True, "schema": schema}})
        except Exception as exc:
            # Transport, rate limit and server errors: the call can simply be made again.
            raise WriterUnavailable(f"DeepInfra call failed: {type(exc).__name__}: {exc}") from exc
        body = response.model_dump()
        result = result_from_completion(body)
        details = (body.get("usage") or {}).get("completion_tokens_details") or {}
        receipt = {"at": started, "seconds": round(time.time() - started, 1), "model": WRITER_MODEL,
                   "effort": WRITER_EFFORT, "stop_reason": result.stop_reason,
                   "input_tokens": result.usage.input_tokens + result.usage.cache_read_input_tokens,
                   "output_tokens": result.usage.output_tokens,
                   "reasoning_tokens": details.get("reasoning_tokens") or 0,
                   "cost_usd": (body.get("usage") or {}).get("estimated_cost")}
        if result.stop_reason != "ok" or result.parsed is None:
            raise WriterError(f"DeepSeek did not return a complete package ({result.stop_reason}: {result.error}).")
        return Draft.model_validate(result.parsed), receipt


def converse(writer: Writer, messages: list[dict], *, keep: Draft | None = None,
             only: set[int] | None = None, progress=lambda text: None) -> tuple[Draft, list[dict]]:
    """Run the conversation until the package passes the mechanical checks.

    With `keep` and `only`, options outside `only` are taken from `keep`, so a
    rewrite can never silently change an option the adjudicator passed."""
    messages = list(messages)
    receipts = []
    for round_number in range(FIX_ROUNDS + 1):
        draft, receipt = writer.complete(messages)
        receipts.append(receipt)
        if keep is not None:
            kept = {t.number: t for t in keep.teasers}
            draft = draft.model_copy(update={"title": keep.title, "author": keep.author, "teasers": [
                t if t.number in (only or set()) else kept[t.number]
                for t in sorted(draft.teasers, key=lambda t: t.number) if t.number in kept]})
        issues = draft_issues(draft)
        if not issues:
            return draft, receipts
        if round_number == FIX_ROUNDS:
            break
        progress("DeepSeek is fixing length: " + "; ".join(issues)[:200])
        messages += [{"role": "assistant", "content": json.dumps(draft.model_dump(), ensure_ascii=False)},
                     {"role": "user", "content": prompts.fix_request(issues)}]
    raise WriterError("DeepSeek's package still fails the mechanical checks: " + "; ".join(issues))


def _saved(work: Path, key: str):
    path = Path(work) / "writer" / (key + ".json")
    if path.exists():
        raw = json.loads(path.read_text())
        return Draft.model_validate(raw["draft"]), raw["receipts"]
    return None


def _save(work: Path, key: str, draft: Draft, receipts: list[dict]):
    path = Path(work) / "writer" / (key + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"draft": draft.model_dump(), "receipts": receipts}))
    temporary.replace(path)


def write(manuscript: str, work: Path, *, writer: Writer | None = None, attempt: int = 0,
          progress=lambda text: None) -> tuple[Draft, list[dict]]:
    """Five teasers from the whole manuscript. A finished package is saved before
    it is handed on, so a lost reply is never paid for twice."""
    key = digest({"write": digest(manuscript), "model": WRITER_MODEL, "attempt": attempt})
    saved = _saved(work, key)
    if saved:
        return saved
    progress("DeepSeek V4 Pro is reading the whole manuscript and writing five teasers")
    draft, receipts = converse(writer or Writer(), prompts.writer_messages(manuscript), progress=progress)
    _save(work, key, draft, receipts)
    return draft, receipts


def rewrite(manuscript: str, current: Draft, notes: dict[int, str], work: Path, *,
            writer: Writer | None = None, progress=lambda text: None) -> tuple[Draft, list[dict]]:
    """The writer redoes only the options the adjudicator could not correct."""
    key = digest({"rewrite": digest(manuscript), "draft": digest(current), "notes": notes,
                  "model": WRITER_MODEL})
    saved = _saved(work, key)
    if saved:
        return saved
    progress(f"DeepSeek is rewriting option(s) {', '.join(map(str, sorted(notes)))}")
    messages = prompts.writer_messages(manuscript) + [
        {"role": "assistant", "content": json.dumps(current.model_dump(), ensure_ascii=False)},
        {"role": "user", "content": prompts.rewrite_request(notes)}]
    draft, receipts = converse(writer or Writer(), messages, keep=current, only=set(notes),
                               progress=progress)
    _save(work, key, draft, receipts)
    return draft, receipts
