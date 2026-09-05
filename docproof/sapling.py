"""Sapling.ai grammar-and-spelling API — a thin client with two callers.

Sapling is a hosted grammar/spell checker with a single edits endpoint: POST
some text, get back a list of suggested edits. Two surfaces in DocProof call
it: the standalone test panel (the /api/sapling/* routes, for eyeballing
Sapling against DocProof on the same text) and the opt-in review pass
(`sapling.enabled`), where the pipeline's `finish` folds Sapling's edits into
the tracked changes, vetted by the shared `rewrite.confirm` valve while
`sapling.confirm` is on. That wiring lives with the callers; this module stays
deliberately small: one request, one normalised list of edits, and every
failure surfaced as one exception a caller can show or log verbatim.

The API returns each edit's `start`/`end` *relative to its sentence*, with the
sentence's own offset given separately as `sentence_start`. Nobody downstream
wants to redo that arithmetic, so `check` resolves both into absolute offsets
into the text that was submitted, slices out the original span while it has the
text in hand, and drops the sentence-relative numbers from the result.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

import httpx

from .rewrite import RewriteCandidate

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


# Sapling names each edit with an ERRANT-style code — a one-letter operation
# (M missing, R replace, U unnecessary) then a colon-separated category, e.g.
# "R:SPELL", "M:PUNCT", "R:VERB:TENSE" — plus a friendlier `general_error_type`
# bucket like "Spelling" or "Punctuation". Neither is prose. `describe` turns
# them into one readable line so a Sapling change reads like the model's own
# findings in the margin, rather than a raw code. This maps the category tail to
# a human label; the general bucket is preferred when Sapling sends one, so an
# unlisted category still degrades to something sensible.
_CATEGORY_LABELS: dict[str, str] = {
    "SPELL": "Spelling",
    "PUNCT": "Punctuation",
    "ORTH": "Capitalisation or spacing",
    "TYPO": "Typo",
    "DET": "Article",
    "PREP": "Preposition",
    "PRON": "Pronoun",
    "NOUN": "Noun",
    "NOUN:NUM": "Noun number",
    "NOUN:POSS": "Possessive",
    "NOUN:INFL": "Noun form",
    "VERB": "Verb",
    "VERB:TENSE": "Verb tense",
    "VERB:FORM": "Verb form",
    "VERB:INFL": "Verb form",
    "VERB:SVA": "Subject–verb agreement",
    "ADJ": "Adjective",
    "ADJ:FORM": "Adjective form",
    "ADV": "Adverb",
    "MORPH": "Word form",
    "WO": "Word order",
    "CONTR": "Contraction",
    "CONJ": "Conjunction",
    "OTHER": "Grammar",
}


def _category_label(error_type: str) -> str:
    """The human label for an ERRANT code, matching the most specific tail first
    ("R:VERB:TENSE" → "Verb tense", falling back to "Verb"). Empty for a code we
    don't recognise, so the caller can fall back to the general bucket."""
    parts = [p for p in (error_type or "").split(":") if p]
    # Drop the leading operation letter (M/R/U/UNK) if present.
    if parts and parts[0] in ("M", "R", "U", "UNK"):
        parts = parts[1:]
    while parts:
        key = ":".join(parts)
        if key in _CATEGORY_LABELS:
            return _CATEGORY_LABELS[key]
        parts.pop()
    return ""


def describe(edit) -> str:
    """A short, readable explanation for a Sapling edit (an `Edit` or `ParaEdit`
    — anything with `.original`, `.replacement`, `.error_type`,
    `.general_error_type`). Reads out the label and, where it clarifies, the edit
    itself: `Spelling: “teh” → “the”.`, `Punctuation: add “,”.`, `Article.`"""
    label = _category_label(edit.error_type) \
        or (edit.general_error_type or "").strip() or "Grammar"
    o = (edit.original or "").strip()
    r = (edit.replacement or "").strip()
    if o and r and o != r:
        return f"{label}: “{o}” → “{r}”."
    if r and not o:
        return f"{label}: add “{r}”."
    if o and not r:
        return f"{label}: remove “{o}”."
    return f"{label}."


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
          session_id: str = "docproof", timeout: float = TIMEOUT,
          stats: dict | None = None) -> list[Edit]:
    """Send `text` to Sapling and return its edits, offsets resolved.

    `variety` is Sapling's regional spelling variant (e.g. "us-variety") or None
    for no preference. Raises SaplingError on a missing/rejected key or any
    transport or shape problem; empty or blank text is answered locally with an
    empty list rather than a wasted round-trip. `stats`, if given, is a counter
    dict the caller reads afterwards: `dropped_malformed` counts edits Sapling
    returned that we could not parse — a silent shed of real catches unless it
    is surfaced."""
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
            if stats is not None:
                stats["dropped_malformed"] = stats.get("dropped_malformed", 0) + 1
            continue
        edits.append(Edit(
            start=start, end=end, original=text[start:end],
            replacement=str(item.get("replacement", "")),
            error_type=str(item.get("error_type", "")),
            general_error_type=str(item.get("general_error_type", ""))))
    edits.sort(key=lambda e: (e.start, e.end))
    return edits



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
                     timeout: float = TIMEOUT,
                     stats: dict | None = None) -> list[ParaEdit]:
    """Run each paragraph's text through Sapling and return per-paragraph edits.

    `paragraphs` is a sequence of (para_id, text). Paragraphs are batched into
    single requests (see PARA_SEP / CHAR_BUDGET) and every edit is mapped back to
    the exact paragraph and offset it belongs to, so the result drops straight
    into a tracked-changes writer. Raises SaplingError like `check`. `stats`, if
    given, is a counter dict filled with `dropped_malformed` (from `check`) and
    `dropped_straddle` — edits that landed across a paragraph boundary and could
    not be placed. Both are catches shed silently unless the caller reports
    them."""
    pairs = [(pid, text) for pid, text in paragraphs if text.strip()]
    out: list[ParaEdit] = []
    for batch in _batches(pairs, budget):
        spans: list[tuple[str, str, int]] = []
        at = 0
        for pid, text in batch:
            spans.append((pid, text, at))
            at += len(text) + len(PARA_SEP)
        chunk_text = PARA_SEP.join(text for _, text in batch)
        for edit in check(chunk_text, api_key, variety=variety, timeout=timeout,
                          stats=stats):
            placed = _place(edit, spans)
            if placed is not None:
                out.append(placed)
            elif stats is not None:
                stats["dropped_straddle"] = stats.get("dropped_straddle", 0) + 1
    return out



def _errant_keys(error_type: str) -> list[str]:
    """The category tails of an ERRANT code, operation letter dropped, most
    specific first: "R:VERB:TENSE" -> ["VERB:TENSE", "VERB"]. A denylist entry
    matching any tail drops the edit, so "VERB" catches every verb subtype while
    "VERB:TENSE" catches only that one."""
    parts = [p for p in (error_type or "").split(":") if p]
    if parts and parts[0] in ("M", "R", "U", "UNK"):
        parts = parts[1:]
    keys: list[str] = []
    while parts:
        keys.append(":".join(parts))
        parts.pop()
    return keys


def to_candidates(edits: Sequence[ParaEdit], para_text: Mapping[str, str], *,
                  lexicon: Sequence[str] = (),
                  disabled_error_types: Sequence[str] = (),
                  ) -> list[RewriteCandidate]:
    """Turn Sapling's placed edits into RewriteCandidates for `rewrite.confirm`,
    so an LLM rules on each in literary context instead of Sapling editing blind.

    Mirrors `languagetool.propose`'s two cheap pre-filters, run before the model
    ever sees a candidate because they are certain and free:
      * the spell scan's lexicon (the author's own names/coinages) suppresses a
        spelling/typo flag on one of them — Sapling's largest false-positive
        source on a novel, exactly as it is LanguageTool's;
      * `disabled_error_types` drops whole ERRANT classes by category tail or raw
        code or general bucket (e.g. "ORTH" to stop spacing edits that fight
        house style, "PUNCT" for every punctuation subtype).

    Each surviving edit's `describe()` line rides along as the candidate's `note`,
    so a confirmed Sapling change reads in the margin like Sapling's own finding,
    not the valve's generic default. Offsets are re-checked against the paragraph
    text; an edit that no longer slices its own `original` (drift) or is a no-op
    is dropped rather than confirmed."""
    lex = {w.strip("'’\".,").lower() for w in lexicon}
    deny = frozenset(disabled_error_types)
    cands: list[RewriteCandidate] = []
    seen: set[tuple[str, int, int, str]] = set()
    dropped = {"denied": 0, "name": 0, "drift": 0, "noop": 0}
    for e in edits:
        text = para_text.get(e.para_id)
        if text is None:
            continue
        keys = _errant_keys(e.error_type)
        if (e.error_type in deny or e.general_error_type in deny
                or any(k in deny for k in keys)):
            dropped["denied"] += 1; continue
        is_spelling = any(k in ("SPELL", "TYPO") for k in keys)
        if is_spelling and (e.original or "").strip("'’\".,").lower() in lex:
            dropped["name"] += 1; continue
        if text[e.start:e.end] != e.original:          # offset drift
            dropped["drift"] += 1; continue
        if e.replacement == e.original:                # nothing to change
            dropped["noop"] += 1; continue
        key = (e.para_id, e.start, e.end, e.replacement)
        if key in seen:
            continue
        seen.add(key)
        cands.append(RewriteCandidate(
            para_id=e.para_id, start=e.start, end=e.end,
            original=e.original, replacement=e.replacement, note=describe(e)))
    log.info("Sapling: %d candidate(s) after filter "
             "(dropped %d name, %d denied, %d drift, %d no-op)",
             len(cands), dropped["name"], dropped["denied"],
             dropped["drift"], dropped["noop"])
    return cands
