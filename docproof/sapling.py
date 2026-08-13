"""Sapling.ai grammar-and-spelling API — a thin client for the test panel.

Sapling is a hosted grammar/spell checker with a single edits endpoint: POST
some text, get back a list of suggested edits. It is wired into DocProof only as
a separate, opt-in surface for now — nothing in the review pipeline calls it —
so this module stays deliberately small: one request, one normalised list of
edits, and every failure surfaced as one exception the route can show verbatim.

The API returns each edit's `start`/`end` *relative to its sentence*, with the
sentence's own offset given separately as `sentence_start`. Nobody downstream
wants to redo that arithmetic, so `check` resolves both into absolute offsets
into the text that was submitted, slices out the original span while it has the
text in hand, and drops the sentence-relative numbers from the result.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

import httpx

log = logging.getLogger("docproof.sapling")

EDITS_URL = "https://api.sapling.ai/api/v1/edits"
TIMEOUT = 30.0

# Paragraphs are joined by a blank line before being sent as one request, so
# Sapling never runs a sentence — and thus an edit — across a paragraph break.
# One request per paragraph would be correct but ruinous on call volume for a
# manuscript; batching keeps the offset mapping exact while sending far fewer.
PARA_SEP = "\n\n"
# How much manuscript text to put in one request. Conservative on purpose: well
# under Sapling's per-call ceiling, and small enough that one slow call doesn't
# stall the whole document.
CHAR_BUDGET = 8_000


class SaplingError(Exception):
    """A check could not be run — no key, a rejected key, a network failure, or
    a response we could not read. Carries the message a person should see."""


@dataclass(frozen=True)
class Edit:
    """One Sapling suggestion, with offsets resolved into the submitted text."""
    start: int                 # absolute offset into the submitted text
    end: int                   # absolute offset, exclusive
    original: str              # the span [start:end) of the submitted text
    replacement: str           # Sapling's suggested correction ("" = deletion)
    error_type: str            # Sapling's specific code, e.g. "R:PUNCT"
    general_error_type: str    # its broad bucket, e.g. "Punctuation"

    def as_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "original": self.original,
                "replacement": self.replacement, "error_type": self.error_type,
                "general_error_type": self.general_error_type}


def _error_message(resp: httpx.Response) -> str:
    """Turn a non-200 into something a person can act on. Sapling puts a reason
    in a JSON `msg` on its 4xx bodies; fall back to trimmed text otherwise."""
    detail = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            detail = str(data.get("msg") or data.get("detail") or "")
    except ValueError:
        detail = (resp.text or "").strip()[:200]
    tail = f" ({detail})" if detail else ""
    if resp.status_code in (401, 403):
        return f"Sapling rejected the API key.{tail}"
    if resp.status_code == 429:
        return "Sapling is rate-limiting this key — try again in a moment."
    return f"Sapling returned an error ({resp.status_code}).{tail}"


def check(text: str, api_key: str, *, variety: str | None = None,
          session_id: str = "docproof", timeout: float = TIMEOUT) -> list[Edit]:
    """Send `text` to Sapling and return its edits, offsets resolved.

    `variety` is Sapling's regional spelling variant (e.g. "us-variety") or None
    for no preference. Raises SaplingError on a missing/rejected key or any
    transport or shape problem; empty or blank text is answered locally with an
    empty list rather than a wasted round-trip."""
    if not api_key:
        raise SaplingError("No Sapling API key is set.")
    if not text.strip():
        return []
    body: dict = {"key": api_key, "text": text, "session_id": session_id}
    if variety:
        body["variety"] = variety
    try:
        resp = httpx.post(EDITS_URL, json=body, timeout=timeout)
    except httpx.HTTPError as e:
        raise SaplingError(f"Could not reach Sapling: {e}") from e
    if resp.status_code != 200:
        raise SaplingError(_error_message(resp))
    try:
        raw = resp.json()["edits"]
    except (ValueError, KeyError, TypeError) as e:
        raise SaplingError("Sapling returned a response we couldn't read.") from e

    edits: list[Edit] = []
    for item in raw:
        try:
            base = int(item["sentence_start"])
            start = base + int(item["start"])
            end = base + int(item["end"])
        except (KeyError, TypeError, ValueError):
            log.warning("Skipping a malformed Sapling edit: %r", item)
            continue
        edits.append(Edit(
            start=start, end=end, original=text[start:end],
            replacement=str(item.get("replacement", "")),
            error_type=str(item.get("error_type", "")),
            general_error_type=str(item.get("general_error_type", ""))))
    edits.sort(key=lambda e: (e.start, e.end))
    return edits


# --- running a whole document, paragraph by paragraph -----------------------

@dataclass(frozen=True)
class ParaEdit:
    """A Sapling edit resolved back to one paragraph: offsets index into that
    paragraph's text, so a caller can turn it straight into a tracked change."""
    para_id: str
    start: int                 # offset into the paragraph's text
    end: int                   # exclusive
    original: str              # paragraph_text[start:end]
    replacement: str
    error_type: str
    general_error_type: str


def _batches(paragraphs: Sequence[tuple[str, str]], budget: int
             ) -> Iterator[list[tuple[str, str]]]:
    """Group (para_id, text) pairs into runs whose joined length stays under
    `budget`. A single paragraph longer than the budget goes alone rather than
    being split — Sapling handles it, and splitting mid-paragraph would be the
    one thing that breaks the offset mapping."""
    batch: list[tuple[str, str]] = []
    size = 0
    for pid, text in paragraphs:
        add = len(text) + (len(PARA_SEP) if batch else 0)
        if batch and size + add > budget:
            yield batch
            batch, size = [], 0
            add = len(text)
        batch.append((pid, text))
        size += add
    if batch:
        yield batch


def _place(edit: Edit, spans: list[tuple[str, str, int]]) -> ParaEdit | None:
    """Map a chunk-absolute edit to the paragraph that wholly contains it.
    Returns None for the rare edit that straddles a paragraph break — dropped
    rather than misapplied."""
    for pid, text, off in spans:
        if edit.start >= off and edit.end <= off + len(text):
            s, e = edit.start - off, edit.end - off
            return ParaEdit(pid, s, e, text[s:e], edit.replacement,
                            edit.error_type, edit.general_error_type)
    return None


def check_paragraphs(paragraphs: Iterable[tuple[str, str]], api_key: str, *,
                     variety: str | None = None, budget: int = CHAR_BUDGET,
                     timeout: float = TIMEOUT) -> list[ParaEdit]:
    """Run each paragraph's text through Sapling and return per-paragraph edits.

    `paragraphs` is a sequence of (para_id, text). Paragraphs are batched into
    single requests (see PARA_SEP / CHAR_BUDGET) and every edit is mapped back to
    the exact paragraph and offset it belongs to, so the result drops straight
    into a tracked-changes writer. Raises SaplingError like `check`."""
    pairs = [(pid, text) for pid, text in paragraphs if text.strip()]
    out: list[ParaEdit] = []
    for batch in _batches(pairs, budget):
        spans: list[tuple[str, str, int]] = []
        at = 0
        for pid, text in batch:
            spans.append((pid, text, at))
            at += len(text) + len(PARA_SEP)
        chunk_text = PARA_SEP.join(text for _, text in batch)
        for edit in check(chunk_text, api_key, variety=variety, timeout=timeout):
            placed = _place(edit, spans)
            if placed is not None:
                out.append(placed)
    return out
