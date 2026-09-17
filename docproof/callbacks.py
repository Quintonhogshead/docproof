"""A line the book quotes back to itself, and gets wrong.

Three shapes, all from the Cooper QA, all invisible to a per-paragraph read for
the same structural reason: each paragraph is perfectly well-formed on its own,
and the error is the *difference* between two paragraphs that may be eleven
apart or four chapters apart.

  1. A misquoted callback. Spoken aloud: "Decide if you're willing to be one."
     Remembered eleven paragraphs later, on a loop in the character's head:
     "Decide if you are willing to be one." A remembered line is a quotation —
     the contraction is the character's voice, and dropping it is a slip, not a
     paraphrase. Corrected, because the earlier line is the authority and the
     frame ("replayed", "the words", "still hear") is the book saying so.
  2. A near-repeat with no memory frame. Two sentences that are 90% the same and
     nothing in the text says one is quoting the other — which is either an echo
     the author wants or a paragraph that got written twice. A question.
  3. The same sentence twice, verbatim, inside one scene. Cooper had one sentence
     repeated word for word in two paragraphs of a single phone call. A question:
     which of the two to cut is a writer's decision.

WHAT IT MUST NOT FLAG. Repetition is a device. A refrain, a catchphrase, a
chapter epigraph reprinted at the top of every part — anything the book says
three or more times is deliberate by construction, and is excluded before
anything is measured. Short sentences are excluded too: "He nodded." recurring
is not a callback, and at eight tokens the shingle index stops being able to tell
a quotation from a coincidence of English.

Everything here is deterministic and offline — a word-shingle index plus
``difflib``. No model reads the book to find these, which is the point: a model
reading chunk 4 has already forgotten chunk 40.
"""
from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Sequence

from .models import Finding, ParagraphRef
from .sweeps import sentence_window

log = logging.getLogger("docproof.callbacks")

# The key a callback finding carries. Like the consistency keys it lives outside
# config/error_types: there is no prompt to write, and the whole judgment is made
# before any model sees the document.
CALLBACK_KEY = "callback_drift"

# The sentence boundary ``sweeps.sentence_window`` quotes by, so a span this scan
# measures and the window a finding quotes cannot disagree.
_SENTENCE_END = re.compile(r"[.!?…][\"”’')\]]*\s+")

# A word for tokenizing: letters and digits, with an apostrophe as a SEPARATOR.
# That is the whole trick that makes a misquoted contraction measurable: "you're"
# tokenizes as (you, re) and "you are" as (you, are), so the difference between
# the spoken line and the remembered one is one token out of eight rather than
# two out of seven — inside the similarity band instead of below it, and long
# enough to index in the first place.
_TOKEN = re.compile(r"[^\W_]+")

# Abbreviations whose full stop does not end a sentence. Needed because the
# shape this scan exists for includes a re-quoted letter whose salutation
# changed ("Ms. Chávez—" against "Dr. Chávez—"): split after the title and the
# only difference between the two quotations lands in a one-token fragment
# nothing compares.
_ABBREVIATIONS = frozenset("""
mr mrs ms dr prof st sr jr lt sgt capt col gen rev hon messrs mt fr
vs etc no cf ca approx dept est
""".split())

# The book saying, in its own words, that what follows is a quotation of
# something already said. This is what separates a correction from a question:
# with a frame the earlier line is the authority, without one the repetition may
# be the author's own echo.
_MEMORY_FRAME = re.compile(
    r"\b(?:replayed|replaying|on a loop|echoed|kept burning|memoriz(?:ed|ing)|"
    r"memoris(?:ed|ing)|the words|still hear|rang in|repeated|remembered|"
    r"read it again|rereads?|reread|his words|her words|their words|"
    r"(?:she|he|they)[’']d read it)\b", re.IGNORECASE)

# A paragraph that opens a chapter or a part. Used only to scope the
# verbatim-repeat question to one scene: two identical sentences four chapters
# apart are far likelier to be a deliberate echo than a duplicated paragraph.
_CHAPTER_OPENER = re.compile(
    r"^\s*(?:chapter|part|book|prologue|epilogue|interlude)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Callback:
    """One sentence that repeats an earlier one.

    `kind` is the channel and the reason:
      "misquote" — a near match inside a memory frame; corrected to `earlier`.
      "near"     — a near match with no frame; a question.
      "verbatim" — word-for-word, in the same chapter; a question.
    `ratio` is the ``difflib`` token ratio, 1.0 for a verbatim repeat."""
    kind: str
    para_id: str
    start: int
    end: int
    text: str                             # the later sentence, verbatim
    earlier_para_id: str
    earlier_text: str
    ratio: float


@dataclass(frozen=True)
class _Sentence:
    order: int                            # position in the document
    para_index: int
    para_id: str
    chapter: int
    start: int
    end: int
    text: str
    tokens: tuple[str, ...]


def _skip(para: ParagraphRef) -> bool:
    """A paragraph whose text is not prose a callback could live in: a heading,
    or a line set mostly in capitals."""
    style = (para.style or "").lower()
    if "head" in style or "title" in style:
        return True
    letters = [c for c in para.text if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.6:
        return True
    return False


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Sentences as (start, end), split where ``sentence_window`` splits except
    that a full stop closing an abbreviation does not end a sentence.

    Both ends of every span are still boundaries of the underlying split, so
    ``sentence_window`` called on one of these spans returns exactly it — which
    is what lets a correction be written back into the window it quotes."""
    bounds = [0] + [m.end() for m in _SENTENCE_END.finditer(text)] + [len(text)]
    spans: list[tuple[int, int]] = []
    lo = bounds[0]
    for hi in bounds[1:]:
        end = lo + len(text[lo:hi].rstrip())
        if end <= lo:
            continue
        last = _TOKEN.findall(text[lo:end])
        if text[end - 1] == "." and last and last[-1].lower() in _ABBREVIATIONS:
            continue                          # "Ms." — the sentence goes on
        spans.append((lo, end))
        lo = hi
    if lo < len(text) and text[lo:].strip():
        spans.append((lo, lo + len(text[lo:].rstrip())))
    return spans


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(m.group(0).lower().replace("’", "'")
                 for m in _TOKEN.finditer(text))


def _shingles(tokens: Sequence[str]) -> set[tuple[str, ...]]:
    return {tuple(tokens[i:i + 3]) for i in range(len(tokens) - 2)}


def _read(paragraphs: Sequence[ParagraphRef], min_tokens: int) -> list[_Sentence]:
    """Every sentence long enough to be worth indexing, in document order, with
    the chapter it belongs to."""
    out: list[_Sentence] = []
    chapter = 0
    order = 0
    for index, para in enumerate(paragraphs):
        style = (para.style or "").lower()
        if _CHAPTER_OPENER.match(para.text or "") or "heading1" in style.replace(" ", ""):
            chapter += 1
        if _skip(para):
            continue
        for lo, hi in _sentence_spans(para.text):
            text = para.text[lo:hi]
            tokens = _tokens(text)
            if len(tokens) < min_tokens:
                continue
            out.append(_Sentence(order, index, para.para_id, chapter,
                                 lo, hi, text, tokens))
            order += 1
    return out


def find_callbacks(paragraphs: Sequence[ParagraphRef], *,
                   min_tokens: int = 8,
                   near: tuple[float, float] = (0.80, 0.999),
                   frame_chars: int = 240,
                   same_chapter_paragraphs: int = 60,
                   verbatim_min_tokens: int = 12,
                   max_queries: int = 30) -> tuple[Callback, ...]:
    """Sentences that quote an earlier sentence, exactly or almost.

    `min_tokens` is the floor for indexing a sentence at all; `near` is the
    similarity band a *misquote* lives in — below it the two sentences are
    different sentences, and at 1.0 the quotation is already right.
    `frame_chars` is how far back of the later sentence is read looking for a
    memory frame, which is what routes a near match to the correction channel
    instead of the question channel.

    A sentence the book uses three or more times is a refrain and is dropped
    before anything is compared: deliberate repetition is the one thing this scan
    would otherwise be guaranteed to misread."""
    low, high = near
    sentences = _read(paragraphs, min_tokens)
    if len(sentences) < 2:
        return ()
    text_by_id = {p.para_id: p.text for p in paragraphs}
    prev_text = {}
    for i, para in enumerate(paragraphs):
        prev_text[para.para_id] = paragraphs[i - 1].text if i else ""

    # Refrains: anything the book says three times or more is a device.
    repeats = Counter(s.tokens for s in sentences)
    usable = [s for s in sentences if repeats[s.tokens] < 3]

    index: dict[tuple[str, ...], list[int]] = defaultdict(list)
    found: list[Callback] = []
    for pos, later in enumerate(usable):
        shingles = _shingles(later.tokens)
        shared: Counter = Counter()
        for sh in shingles:
            bucket = index[sh]
            # A shingle shared by dozens of sentences is a turn of phrase, not a
            # quotation, and would make this quadratic for nothing.
            if len(bucket) > 40:
                continue
            shared.update(bucket)
        candidates = [j for j, n in shared.most_common(20) if n >= 3]
        best: Callback | None = None
        for j in candidates:
            earlier = usable[j]
            if earlier.para_id == later.para_id and earlier.start == later.start:
                continue
            ratio = SequenceMatcher(None, earlier.tokens, later.tokens).ratio()
            kind = _classify(earlier, later, ratio, low, high,
                             text_by_id, prev_text, frame_chars,
                             same_chapter_paragraphs, verbatim_min_tokens)
            if kind is None:
                continue
            if best is None or ratio > best.ratio:
                best = Callback(kind, later.para_id, later.start, later.end,
                                later.text, earlier.para_id, earlier.text,
                                ratio)
        if best is not None:
            found.append(best)
        for sh in shingles:
            index[sh].append(pos)

    found = _drop_formulas(found)
    if max_queries and len(found) > max_queries:
        log.info("Callback findings capped at %d (%d found).",
                 max_queries, len(found))
        found = found[:max_queries]
    return tuple(found)


def _drop_formulas(found: Sequence[Callback]) -> list[Callback]:
    """Keep the unframed near-matches that are ISOLATED pairs, and drop the rest.

    The refrain rule upstream catches exact repetition. This catches the other
    kind — a sentence the author writes again and again with one thing changed
    ("The corridor lights flickered as she walked, step four") — and it catches it
    by the only signal available without reading the book: a sentence that takes
    part in more than one near-match is a pattern, not an accident. What survives
    is the shape the query channel is actually for, one paragraph that looks like
    it was written twice.

    Framed misquotes and verbatim repeats are exempt. The frame is the book's own
    statement that the line is a quotation, and a verbatim repeat is already held
    to three-or-more-is-a-refrain."""
    near = [cb for cb in found if cb.kind == "near"]
    seen: Counter = Counter()
    for cb in near:
        seen[(cb.para_id, cb.text)] += 1
        seen[(cb.earlier_para_id, cb.earlier_text)] += 1
    return [cb for cb in found
            if cb.kind != "near"
            or (seen[(cb.para_id, cb.text)] == 1
                and seen[(cb.earlier_para_id, cb.earlier_text)] == 1)]


def _classify(earlier: _Sentence, later: _Sentence, ratio: float,
              low: float, high: float, text_by_id, prev_text,
              frame_chars: int, same_chapter_paragraphs: int,
              verbatim_min_tokens: int) -> str | None:
    if ratio >= 1.0:
        if (earlier.tokens == later.tokens
                and len(later.tokens) >= verbatim_min_tokens
                and earlier.chapter == later.chapter
                and later.para_index - earlier.para_index
                <= same_chapter_paragraphs):
            return "verbatim"
        return None
    if not low <= ratio <= high:
        return None
    return "misquote" if _framed(later, text_by_id, prev_text,
                                 frame_chars) else "near"


def _framed(later: _Sentence, text_by_id, prev_text, frame_chars: int) -> bool:
    """Whether the book frames this sentence as a quotation of something said
    before — in its own paragraph, or in the `frame_chars` of running text
    leading up to it."""
    text = text_by_id.get(later.para_id, "")
    if _MEMORY_FRAME.search(text):
        return True
    lead = (prev_text.get(later.para_id, "") + " " + text[:later.start])
    return bool(_MEMORY_FRAME.search(lead[-frame_chars:]))


_QUOTES = "\"'“”‘’«»"


def _requote(earlier: str, later: str) -> str:
    """The earlier line's words wearing the later sentence's own quotation marks.

    A sentence lifted out of dialogue carries the closing quote of the paragraph
    it ended, and writing that quote into the middle of a narrated paragraph
    would be a worse error than the one being fixed. Only the outermost marks are
    touched; an apostrophe inside a word is never at an edge."""
    core = earlier.strip(_QUOTES).strip()
    lead = later[:len(later) - len(later.lstrip(_QUOTES))]
    trail = later[len(later.rstrip(_QUOTES)):]
    return lead + core + trail


def callback_findings(callbacks: Sequence[Callback],
                      paragraphs: Sequence[ParagraphRef],
                      start_id: int = 1) -> list[Finding]:
    """One finding per callback: a tracked change for a misquote, a margin
    question for anything the scan cannot settle on its own."""
    by_id = {p.para_id: p for p in paragraphs}
    out: list[Finding] = []
    n = start_id
    for cb in callbacks:
        para = by_id.get(cb.para_id)
        if para is None:
            continue
        window, lo, occurrence = sentence_window(para.text, cb.start, cb.end)
        a, b = cb.start - lo, cb.end - lo
        replaceable = (0 <= a <= b <= len(window)
                       and window[a:b] == para.text[cb.start:cb.end])
        percent = round(cb.ratio * 100)
        if cb.kind == "misquote" and replaceable:
            quoted = _requote(cb.earlier_text, window[a:b])
            out.append(Finding(
                finding_id=f"b-{n:04d}",
                chunk_id="consistency",
                para_id=cb.para_id,
                error_type=CALLBACK_KEY,
                original_text=window,
                occurrence=occurrence,
                corrected_text=window[:a] + quoted + window[b:],
                explanation=(
                    f"This remembers a line the book already gave, in "
                    f"{cb.earlier_para_id}: “{quoted}” — {percent}% "
                    f"the same wording. A remembered line is a quotation, so it "
                    f"is set back to the words as spoken. Reject if the "
                    f"character is meant to misremember."),
                confidence="medium",
            ))
        elif cb.kind == "verbatim":
            out.append(Finding(
                finding_id=f"b-{n:04d}",
                chunk_id="consistency",
                para_id=cb.para_id,
                error_type=CALLBACK_KEY,
                original_text=window,
                occurrence=occurrence,
                corrected_text=window,
                explanation=(
                    f"This sentence is repeated verbatim from "
                    f"{cb.earlier_para_id} in the same scene. Nothing has been "
                    f"changed — which of the two to cut is yours to decide."),
                confidence="high",
            ))
        else:
            out.append(Finding(
                finding_id=f"b-{n:04d}",
                chunk_id="consistency",
                para_id=cb.para_id,
                error_type=CALLBACK_KEY,
                original_text=window,
                occurrence=occurrence,
                corrected_text=window,
                explanation=(
                    f"This is {percent}% the same as a sentence in "
                    f"{cb.earlier_para_id}: “{cb.earlier_text}” Is the echo "
                    f"deliberate? If one is quoting the other, the two should "
                    f"match word for word."),
                confidence="medium",
            ))
        n += 1
    return out
