"""Tests for the seeded-error recall gauge (ticket E3)."""

from __future__ import annotations

import copy

from galley.contracts import GFinding, Provenance, Span
from galley.seeding import (
    DEFAULT_TAXONOMY,
    AnswerKey,
    RecallEstimate,
    assert_deliverable,
    is_seeded,
    score_catches,
    seed_copy,
)
from tests.galley.fakes import make_manuscript

# A paragraph rich enough that every default mutation has a candidate site:
# commas, doublable words, homophones (their / its), >=3-letter words to
# transpose, a straight double quote to drop, and a mid-sentence proper noun.
_RICH = 'She told Kathryn "wait here," and their dog wagged its tail slowly.'


def _rich_book(n_paras: int = 24, chapter_size: int = 3):
    return make_manuscript(*([_RICH] * n_paras), chapter_size=chapter_size)


# --- determinism --------------------------------------------------------------


def test_same_seed_is_reproducible():
    ms = _rich_book()
    a_ms, a_key = seed_copy(ms, 6, DEFAULT_TAXONOMY, 42)
    b_ms, b_key = seed_copy(ms, 6, DEFAULT_TAXONOMY, 42)

    assert a_ms.paragraphs == b_ms.paragraphs
    assert a_key == b_key
    assert a_key.seeded_chapters == b_key.seeded_chapters


def test_different_seed_differs():
    ms = _rich_book()
    _, a_key = seed_copy(ms, 6, DEFAULT_TAXONOMY, 1)
    _, b_key = seed_copy(ms, 6, DEFAULT_TAXONOMY, 2)

    # Different seed -> a different plant plan (chapters and/or plants differ).
    assert (a_key.planted, a_key.seeded_chapters) != (
        b_key.planted,
        b_key.seeded_chapters,
    )


# --- the original is provably untouched ---------------------------------------


def test_original_manuscript_unchanged():
    ms = _rich_book()
    snapshot = copy.deepcopy(ms)

    seeded, key = seed_copy(ms, 6, DEFAULT_TAXONOMY, 7)

    # The deliverable manuscript is identical to its pre-seed snapshot...
    assert ms == snapshot
    assert ms.to_json() == snapshot.to_json()
    # ...and it is genuinely a *different* object with different text.
    assert seeded is not ms
    assert seeded.paragraphs != ms.paragraphs
    assert len(key.planted) == 6


def test_seeded_paragraphs_actually_carry_the_mutation():
    ms = _rich_book()
    seeded, key = seed_copy(ms, 5, DEFAULT_TAXONOMY, 3)

    for pe in key.planted:
        seeded_text = seeded.text_of(pe.para_id)
        # Every plant changes its paragraph.
        assert ms.text_of(pe.para_id) != seeded_text
        # For a replacement (non-empty mutated), the recorded span picks the
        # mutation out of the SEEDED paragraph exactly. Deletions (comma/quote)
        # record an empty `mutated` with a 1-char seam window, so only their
        # length invariant is checked.
        if pe.mutated:
            assert seeded_text[pe.start : pe.end] == pe.mutated
        else:
            assert len(seeded_text) == len(ms.text_of(pe.para_id)) - 1


# --- the deliverable is never the seeded copy ---------------------------------


def test_is_seeded_and_delivery_guard():
    ms = _rich_book()
    seeded, _ = seed_copy(ms, 4, DEFAULT_TAXONOMY, 11)

    assert is_seeded(seeded) is True
    assert is_seeded(ms) is False

    # Guard passes the clean manuscript through, refuses the seeded one.
    assert assert_deliverable(ms) is ms
    try:
        assert_deliverable(seeded)
    except AssertionError:
        pass
    else:  # pragma: no cover - failure path
        raise AssertionError("assert_deliverable let a seeded copy through")


# --- scoring: a detector that catches k of n scores exactly k/n ---------------


def _finding_on(pe, fid: str) -> GFinding:
    """A GFinding whose span lands exactly on a planted error."""

    return GFinding(
        id=fid,
        error_type=pe.error_type,
        span=Span(pe.para_id, pe.start, max(pe.end, pe.start + 1)),
        find=pe.mutated or pe.original,
        replace=pe.original,
        note="",
        provenance=Provenance("scripted", 1),
    )


def test_scores_k_of_n_exactly():
    ms = _rich_book()
    _, key = seed_copy(ms, 5, DEFAULT_TAXONOMY, 99)
    assert len(key.planted) == 5

    k = 3
    hit = key.planted[:k]
    findings = [_finding_on(pe, f"f-{i}") for i, pe in enumerate(hit)]

    est = score_catches(findings, key)
    assert isinstance(est, RecallEstimate)
    assert est.planted == 5
    assert est.caught == 3
    assert est.rate == 3 / 5

    # by_type: the three caught types are credited, all five are counted.
    caught_total = sum(c for c, _ in est.by_type.values())
    planted_total = sum(t for _, t in est.by_type.values())
    assert caught_total == 3
    assert planted_total == 5
    for pe in hit:
        c, t = est.by_type[pe.error_type]
        assert c >= 1 and t >= 1


def test_score_empty_findings_is_zero_recall():
    ms = _rich_book()
    _, key = seed_copy(ms, 5, DEFAULT_TAXONOMY, 5)
    est = score_catches([], key)
    assert est.caught == 0
    assert est.rate == 0.0
    assert "blind" in est.caveat.lower()
    assert est.summary().startswith("seeded-recall")


def test_find_text_covers_mutation_even_off_span():
    # A finding in the right paragraph but at offset 0 still catches when its
    # find-text swallows the mutated string.
    ms = _rich_book()
    _, key = seed_copy(ms, 4, DEFAULT_TAXONOMY, 8)
    # Pick a plant whose mutation is a non-empty, locatable string.
    pe = next(p for p in key.planted if p.mutated)
    f = GFinding(
        id="cover",
        error_type=pe.error_type,
        span=Span(pe.para_id, 0, 0),  # zero-width, at the very start
        find=pe.mutated,
        replace=pe.original,
        provenance=Provenance("scripted", 1),
    )
    est = score_catches([f], key)
    assert est.caught >= 1


# --- answer-key size and short-book handling ----------------------------------


def test_answer_key_has_n_entries_when_room():
    ms = _rich_book()
    _, key = seed_copy(ms, 8, DEFAULT_TAXONOMY, 21)
    assert isinstance(key, AnswerKey)
    assert key.requested == 8
    assert len(key.planted) == 8
    # Ids are unique and sequential.
    assert len({p.id for p in key.planted}) == 8


def test_short_book_plants_fewer_than_requested():
    # One chapter, one paragraph: at most one plant is possible for n=5.
    ms = make_manuscript(_RICH, chapter_size=1)
    _, key = seed_copy(ms, 5, DEFAULT_TAXONOMY, 4)
    assert key.requested == 5
    assert len(key.planted) <= 1
    # Whatever was planted is fully recorded (no phantom entries).
    assert all(p.para_id in ms.paragraphs for p in key.planted)


def test_book_without_chapters_is_handled():
    # No declared chapters -> treated as a single chapter over all paragraphs.
    ms = make_manuscript(*([_RICH] * 6))  # chapter_size=0 -> no chapters
    seeded, key = seed_copy(ms, 4, DEFAULT_TAXONOMY, 6)
    assert len(key.planted) == 4
    assert key.seeded_chapters == (0,)
    assert is_seeded(seeded)
