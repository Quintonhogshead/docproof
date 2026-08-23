"""Seeded-error recall gauge (ticket E3).

A *reference-independent* recall estimator. Real recall needs a human-marked
key; here we manufacture one. We plant a handful of known, typed, reversible
errors into a **copy** of a few sampled chapters, later run the fleet on the
seeded copy, and score how many planted errors any finding lands on. The ratio
caught / planted is a blind estimate of the fleet's recall — blind because it
can only speak to the error classes we know how to plant.

This module owns two of the three moves: it builds the seeded copy + answer key
(:func:`seed_copy`) and scores catches against that key (:func:`score_catches`).
Running the fleet on the seeded copy is a separate ticket.

Two invariants matter and are enforced by tests:

* **The original is never mutated.** :func:`seed_copy` deep-reads ``ms`` and
  returns a brand-new :class:`~galley.contracts.Manuscript`; ``ms`` compares
  equal to a pre-seed snapshot afterward.
* **The deliverable is never the seeded copy.** Every manuscript returned by
  :func:`seed_copy` is tagged; :func:`is_seeded` reports it and
  :func:`assert_deliverable` refuses to let a seeded copy ship.

Determinism: the same ``(ms, n, taxonomy, rng_seed)`` yields byte-identical
seeded text and an identical answer key on every run — a single
``random.Random(rng_seed)`` drives every choice, in a fixed order.

Stdlib only (``random``, ``dataclasses``, ``re``, ``copy``, ``weakref``); the
only project import is the frozen contracts module.
"""

from __future__ import annotations

import random
import re
import weakref
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from galley.contracts import Chapter, GFinding, Manuscript

# --- planted-error records ----------------------------------------------------


@dataclass(frozen=True)
class PlantedError:
    """One deliberately planted, reversible error and where it now sits.

    ``start``/``end`` index into the **seeded** paragraph's text; ``original``
    is what stood there before, ``mutated`` what replaced it (empty for a pure
    deletion, e.g. a removed comma). ``original`` makes every plant reversible.
    """

    id: str
    error_type: str
    para_id: str
    start: int
    end: int
    original: str
    mutated: str


@dataclass(frozen=True)
class AnswerKey:
    """The key to a seeded run: what was planted, and which chapters were hit.

    ``requested`` is the ``n`` asked for; ``len(planted)`` is what actually fit
    (a book with few mutable paragraphs in the sampled chapters may hold fewer —
    see :func:`seed_copy`). ``rng_seed`` is recorded so a run is replayable.
    """

    planted: tuple[PlantedError, ...] = ()
    seeded_chapters: tuple[int, ...] = ()
    rng_seed: int = 0
    requested: int = 0


# --- the default taxonomy of deterministic, reversible mutations --------------


@dataclass(frozen=True)
class MutationResult:
    """The outcome of applying one mutation to a paragraph.

    ``new_text`` is the whole paragraph after the edit; ``start``/``end`` index
    the mutated region within ``new_text``.
    """

    start: int
    end: int
    original: str
    mutated: str
    new_text: str


@dataclass(frozen=True)
class Mutation:
    """A named, reversible text mutation.

    ``apply(text, rng)`` returns a :class:`MutationResult`, or ``None`` when the
    paragraph offers no candidate site. It consumes ``rng`` **only** when it
    successfully mutates (choosing among candidate sites), which keeps the random
    stream — and therefore the whole run — deterministic.
    """

    error_type: str
    apply: Callable[[str, random.Random], Optional[MutationResult]]


_QUOTES = '"”’'  # straight double, right double curly, right single curly
_HOMOPHONES = {
    "its": "it's",
    "it's": "its",
    "their": "there",
    "there": "their",
    "your": "you're",
    "you're": "your",
}


def _delete_comma(text: str, rng: random.Random) -> Optional[MutationResult]:
    positions = [m.start() for m in re.finditer(",", text)]
    if not positions:
        return None
    pos = rng.choice(positions)
    new = text[:pos] + text[pos + 1 :]
    end = pos + 1 if pos < len(new) else pos  # 1-char window at the seam
    return MutationResult(pos, end, ",", "", new)


def _double_word(text: str, rng: random.Random) -> Optional[MutationResult]:
    words = list(re.finditer(r"[A-Za-z]+", text))
    if not words:
        return None
    m = rng.choice(words)
    ws, we, word = m.start(), m.end(), m.group()
    new = text[:we] + " " + word + text[we:]
    return MutationResult(ws, we + 1 + len(word), word, word + " " + word, new)


def _swap_homophone(text: str, rng: random.Random) -> Optional[MutationResult]:
    cands = [m for m in re.finditer(r"[A-Za-z']+", text) if m.group() in _HOMOPHONES]
    if not cands:
        return None
    m = rng.choice(cands)
    key = m.group()
    val = _HOMOPHONES[key]
    ws = m.start()
    new = text[:ws] + val + text[m.end() :]
    return MutationResult(ws, ws + len(val), key, val, new)


def _transpose_letters(text: str, rng: random.Random) -> Optional[MutationResult]:
    # Words of >=3 letters whose 2nd and 3rd differ (so the swap is visible).
    cands = [
        m
        for m in re.finditer(r"[A-Za-z]{3,}", text)
        if m.group()[1] != m.group()[2]
    ]
    if not cands:
        return None
    m = rng.choice(cands)
    w = m.group()
    new_w = w[0] + w[2] + w[1] + w[3:]
    ws, we = m.start(), m.end()
    return MutationResult(ws, we, w, new_w, text[:ws] + new_w + text[we:])


def _drop_closing_quote(text: str, rng: random.Random) -> Optional[MutationResult]:
    positions = [i for i, c in enumerate(text) if c in _QUOTES]
    if not positions:
        return None
    pos = rng.choice(positions)
    new = text[:pos] + text[pos + 1 :]
    end = pos + 1 if pos < len(new) else pos
    return MutationResult(pos, end, text[pos], "", new)


def _lowercase_proper(text: str, rng: random.Random) -> Optional[MutationResult]:
    # A capitalized word that is NOT the paragraph's first word and does NOT open
    # a sentence — i.e. a mid-sentence proper noun.
    cands = []
    for m in re.finditer(r"[A-Z][a-z]+", text):
        ws = m.start()
        if ws == 0:
            continue
        head = text[:ws].rstrip()
        if not head or head[-1] in ".!?…":
            continue
        cands.append(m)
    if not cands:
        return None
    m = rng.choice(cands)
    w = m.group()
    low = w[0].lower() + w[1:]
    ws, we = m.start(), m.end()
    return MutationResult(ws, we, w, low, text[:ws] + low + text[we:])


DEFAULT_TAXONOMY: tuple[Mutation, ...] = (
    Mutation("missing_comma", _delete_comma),
    Mutation("doubled_word", _double_word),
    Mutation("homophone", _swap_homophone),
    Mutation("transposition", _transpose_letters),
    Mutation("dropped_quote", _drop_closing_quote),
    Mutation("lowercase_proper_noun", _lowercase_proper),
)


# --- the seeded-copy tag ------------------------------------------------------
#
# Manuscript carries a ``dict`` field, so it is not hashable and cannot live in a
# WeakSet. We tag by object identity instead: id -> weakref-held object. The
# WeakValueDictionary drops an entry when its manuscript is collected, so a later
# id() collision maps to a *different* live object and ``get(...) is ms`` fails
# closed. The original ms is never registered, so is_seeded(original) is False.
_SEEDED: "weakref.WeakValueDictionary[int, Manuscript]" = weakref.WeakValueDictionary()


def is_seeded(ms: Manuscript) -> bool:
    """True iff ``ms`` is a seeded copy produced by :func:`seed_copy`."""

    return _SEEDED.get(id(ms)) is ms


def assert_deliverable(ms: Manuscript) -> Manuscript:
    """Guard: raise if ``ms`` is a seeded copy, else return it.

    Wrap the manuscript headed for the reader in this so a seeded copy — which
    contains deliberately planted errors — can never be shipped by mistake.
    """

    if is_seeded(ms):
        raise AssertionError(
            "refusing to deliver a seeded manuscript: it contains planted errors"
        )
    return ms


# --- planting -----------------------------------------------------------------


def seed_copy(
    ms: Manuscript,
    n: int,
    taxonomy: Iterable[Mutation] = DEFAULT_TAXONOMY,
    rng_seed: int = 0,
) -> tuple[Manuscript, AnswerKey]:
    """Plant ``n`` typed errors into a copy of a few sampled chapters.

    Deterministically samples 3-4 chapters (fewer if the book has fewer), then
    plants at most one error per paragraph, drawing mutation types from
    ``taxonomy``, until ``n`` are placed or the sampled paragraphs are exhausted.
    Capping at one plant per paragraph keeps every recorded offset valid in the
    final seeded text (no later plant shifts an earlier one).

    Returns ``(seeded_ms, answer_key)``. ``seeded_ms`` is a fresh manuscript with
    only the mutated paragraphs changed; ``ms`` is left untouched. If the sampled
    chapters cannot host ``n`` plants, fewer are planted and
    ``len(answer_key.planted) < answer_key.requested`` records the shortfall.
    """

    rng = random.Random(rng_seed)
    taxonomy = tuple(taxonomy)

    # A book without declared chapters is treated as one chapter over all paras.
    chapters = ms.chapters or (Chapter(0, "", tuple(ms.order)),)

    desired = rng.choice((3, 4))
    k = min(desired, len(chapters))
    sampled = sorted(rng.sample(list(chapters), k), key=lambda c: c.index)
    seeded_chapters = tuple(c.index for c in sampled)

    # Candidate paragraphs, reading order across sampled chapters, then shuffled.
    candidates = [
        pid for c in sampled for pid in c.para_ids if pid in ms.paragraphs
    ]
    rng.shuffle(candidates)

    new_paras = dict(ms.paragraphs)  # strings are immutable; ms.paragraphs untouched
    planted: list[PlantedError] = []

    for pid in candidates:
        if len(planted) >= n:
            break
        text = new_paras[pid]
        order = list(taxonomy)
        rng.shuffle(order)
        for mut in order:
            res = mut.apply(text, rng)
            if res is None:
                continue
            new_paras[pid] = res.new_text
            planted.append(
                PlantedError(
                    id=f"seed-{len(planted) + 1:04d}",
                    error_type=mut.error_type,
                    para_id=pid,
                    start=res.start,
                    end=res.end,
                    original=res.original,
                    mutated=res.mutated,
                )
            )
            break

    seeded_ms = Manuscript(
        paragraphs=new_paras, order=ms.order, chapters=ms.chapters
    )
    _SEEDED[id(seeded_ms)] = seeded_ms

    key = AnswerKey(
        planted=tuple(planted),
        seeded_chapters=seeded_chapters,
        rng_seed=rng_seed,
        requested=n,
    )
    return seeded_ms, key


# --- scoring ------------------------------------------------------------------


@dataclass(frozen=True)
class RecallEstimate:
    """A blind recall estimate: catches over plants, overall and by type.

    ``by_type`` maps each planted ``error_type`` to ``(caught, planted)``.
    ``caveat`` states the estimate's blind spot: it can only speak to the classes
    the taxonomy plants, not the book's native errors.
    """

    planted: int = 0
    caught: int = 0
    rate: float = 0.0
    by_type: dict[str, tuple[int, int]] = field(default_factory=dict)
    caveat: str = ""

    def summary(self) -> str:
        """A one-line human summary of the estimate."""

        return (
            f"seeded-recall ~= {self.rate:.0%} "
            f"({self.caught}/{self.planted} planted errors caught)"
        )


def _covers(finding: GFinding, pe: PlantedError) -> bool:
    """Does ``finding`` land on planted error ``pe``? (same para assumed)."""

    s = finding.span
    # Inclusive overlap: touching counts, so a zero-width / seam finding at the
    # boundary of a deletion still catches it.
    if s.start <= pe.end and pe.start <= s.end:
        return True
    # Or the finding's verbatim find-text swallows the mutated string.
    return bool(pe.mutated) and pe.mutated in finding.find


def score_catches(
    findings: list[GFinding], answer_key: AnswerKey
) -> RecallEstimate:
    """Score how many planted errors the ``findings`` caught.

    A planted error is caught when some finding shares its ``para_id`` and either
    its span overlaps the planted span or its ``find`` text covers the mutation.
    """

    by_para: dict[str, list[GFinding]] = {}
    for f in findings:
        by_para.setdefault(f.span.para_id, []).append(f)

    caught = 0
    caught_by_type: dict[str, int] = {}
    total_by_type: dict[str, int] = {}
    for pe in answer_key.planted:
        total_by_type[pe.error_type] = total_by_type.get(pe.error_type, 0) + 1
        if any(_covers(f, pe) for f in by_para.get(pe.para_id, ())):
            caught += 1
            caught_by_type[pe.error_type] = caught_by_type.get(pe.error_type, 0) + 1

    by_type = {
        et: (caught_by_type.get(et, 0), total)
        for et, total in total_by_type.items()
    }
    total = len(answer_key.planted)
    rate = caught / total if total else 0.0
    classes = ", ".join(sorted(total_by_type)) or "none"
    caveat = (
        "Seeded-recall is blind to error classes outside the planted taxonomy "
        f"({classes}); it estimates recall only for planted, reversible "
        "mutations, not the manuscript's native errors."
    )
    return RecallEstimate(
        planted=total,
        caught=caught,
        rate=rate,
        by_type=by_type,
        caveat=caveat,
    )
