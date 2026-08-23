"""Tests for the concordance / KWIC tool (ticket F2)."""

from __future__ import annotations

from galley.tools.concordance import Hit, kwic, levenshtein
from tests.galley.fakes import make_manuscript


# --- levenshtein --------------------------------------------------------------


def test_levenshtein_basics():
    assert levenshtein("Kathryn", "Kathryn") == 0
    assert levenshtein("Kathryn", "Katheryn") == 1  # one insertion
    assert levenshtein("", "abc") == 3
    assert levenshtein("abc", "") == 3
    # Kathryn -> Katherine: within 2? distance is 3, so NOT within 2.
    assert levenshtein("kathryn", "katherine") == 3


def test_levenshtein_max_distance_early_exit():
    # Cap short-circuits: returns cap+1 as a "greater than" sentinel.
    assert levenshtein("kitten", "sitting", max_distance=1) == 2
    assert levenshtein("abcdef", "uvwxyz", max_distance=2) == 3


# --- exact mode ---------------------------------------------------------------


def test_exact_match_offsets_and_slice():
    ms = make_manuscript("The cat sat on the mat.")
    hits = kwic(ms, "cat")
    assert len(hits) == 1
    h = hits[0]
    assert h.para_id == "body-0001"
    assert ms.text_of("body-0001")[h.start : h.end] == "cat"
    assert h.match == "cat"
    assert h.fuzzy is False


def test_case_insensitive_default_preserves_original_case():
    ms = make_manuscript("Cat and cat and CAT.")
    hits = kwic(ms, "cat")
    assert [h.match for h in hits] == ["Cat", "cat", "CAT"]
    assert [h.start for h in hits] == [0, 8, 16]


def test_fold_case_false_is_exact_case():
    ms = make_manuscript("Cat and cat and CAT.")
    hits = kwic(ms, "cat", fold_case=False)
    assert [h.match for h in hits] == ["cat"]
    assert [h.start for h in hits] == [8]


def test_multiple_occurrences_in_one_paragraph_all_returned():
    ms = make_manuscript("na na na na batman")
    hits = kwic(ms, "na")
    assert len(hits) == 4
    assert [h.start for h in hits] == [0, 3, 6, 9]


def test_reading_order_across_paragraphs():
    ms = make_manuscript(
        "first mention of River here",
        "no hit in this one",
        "second River appears later",
    )
    hits = kwic(ms, "River")
    assert [h.para_id for h in hits] == ["body-0001", "body-0003"]


def test_exact_mode_does_not_fuzzy_match():
    ms = make_manuscript("Kathryn walked in. Katherine did not.")
    hits = kwic(ms, "Katherine")  # fuzzy defaults False
    assert [h.match for h in hits] == ["Katherine"]
    assert all(h.fuzzy is False for h in hits)


# --- window clipping ----------------------------------------------------------


def test_window_clips_at_paragraph_boundaries():
    ms = make_manuscript("abcXYZdef")
    hits = kwic(ms, "XYZ", window=2)
    h = hits[0]
    assert h.left == "bc"
    assert h.right == "de"
    assert h.left_clipped is True
    assert h.right_clipped is True


def test_no_ellipsis_when_match_at_edges():
    ms = make_manuscript("XYZ")
    h = kwic(ms, "XYZ", window=60)[0]
    assert h.left == ""
    assert h.right == ""
    assert h.left_clipped is False
    assert h.right_clipped is False
    assert h.line() == "«XYZ»"


# --- fuzzy / name drift -------------------------------------------------------


def test_fuzzy_surfaces_name_drift():
    # Searching "Katheryn" surfaces both "Kathryn" (dist 1) and the exact.
    ms = make_manuscript(
        "Kathryn opened the door.",
        "Later, Katheryn closed it.",
    )
    hits = kwic(ms, "Katheryn", fuzzy=True)
    matched = {h.match for h in hits}
    assert "Kathryn" in matched  # fuzzy, distance 1
    assert "Katheryn" in matched  # exact
    # The drift spelling is flagged fuzzy, the exact one is not.
    by_match = {h.match: h.fuzzy for h in hits}
    assert by_match["Kathryn"] is True
    assert by_match["Katheryn"] is False


def test_fuzzy_only_matches_capitalized_tokens():
    # "cat" is within edit distance 2 of "car" but lowercase -> not fuzzy-matched.
    ms = make_manuscript("the cat and the Cab drove off")
    hits = kwic(ms, "Car", fuzzy=True)
    matched = {h.match for h in hits}
    assert "cat" not in matched  # lowercase, excluded from fuzzy
    assert "Cab" in matched  # capitalized, distance 1


def test_fuzzy_exact_still_applies_for_base_term():
    ms = make_manuscript("river Riven Rivera")
    hits = kwic(ms, "river", fuzzy=True)
    # exact substring 'river' (case-folded) matches "river"; capitalized
    # Riven/Rivera within distance 2 also surface.
    matched = [h.match for h in hits]
    assert "river" in matched
    assert "Riven" in matched


def test_empty_term_returns_nothing():
    ms = make_manuscript("anything at all")
    assert kwic(ms, "") == []


# --- render snapshot ----------------------------------------------------------


def test_hit_line_render_snapshot():
    ms = make_manuscript("The quick brown fox jumps over the lazy dog.")
    h = kwic(ms, "fox", window=10)[0]
    # left window: "ick brown " (10 chars before), right: " jumps ove" (10 after)
    assert h.line() == "…ick brown «fox» jumps ove…"
    assert str(h) == h.line()


def test_full_kwic_render_snapshot():
    ms = make_manuscript("Kathryn and Katherine met.")
    lines = [h.line() for h in kwic(ms, "Katherine")]
    assert lines == ["Kathryn and «Katherine» met."]
