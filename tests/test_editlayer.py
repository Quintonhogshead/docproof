"""EditLayer — folding several rounds of corrections into one diff against the
original (design doc stage B).

The load-bearing invariant is that folding a round then rendering equals
rendering then applying that round's edits, for any sequence of rounds. The
property tests pin that against a direct oracle over many random edit sequences;
the unit tests pin the specific behaviours the orchestrator relies on
(composition, insertions, deletions, exact-revert drop, guard->query, query
anchor translation).
"""
from __future__ import annotations

import random

import pytest

from docproof.config import EditGuardConfig
from docproof.editlayer import (Contribution, Edit, EditLayer, OverstepReport,
                                RoundEdit)
from docproof.models import ParagraphRef


def _c(rnd: int = 1, fid: str = "f-0001", etype: str = "typo",
       expl: str = "") -> Contribution:
    return Contribution(rnd, fid, etype, expl)


def _re(a: int, b: int, repl: str, rnd: int = 1, fid: str = "f-0001",
        etype: str = "typo", expl: str = "") -> RoundEdit:
    return RoundEdit(a, b, repl, _c(rnd, fid, etype, expl))


# --- oracle for the property tests -------------------------------------------

def _apply_disjoint(text: str, edits: list[tuple[int, int, str]]) -> str:
    """Apply a round's disjoint edits (all in `text`'s coordinates) directly, by
    splicing right-to-left. This is what the layer must reproduce."""
    for a, b, repl in sorted(edits, reverse=True):
        text = text[:a] + repl + text[b:]
    return text


def _random_round(rng: random.Random, text: str) -> list[tuple[int, int, str]]:
    """A few disjoint edits in `text`'s coordinates: replacements, insertions,
    and deletions, none overlapping and no two insertions at one point."""
    n = len(text)
    cuts = sorted(rng.sample(range(n + 1), min(n + 1, rng.randint(2, 6))))
    edits: list[tuple[int, int, str]] = []
    i = 0
    while i + 1 < len(cuts):
        a, b = cuts[i], cuts[i + 1]
        kind = rng.random()
        if kind < 0.3 and a < b:                       # deletion
            edits.append((a, b, ""))
        elif kind < 0.6:                               # insertion at a
            edits.append((a, a, rng.choice(["X", "YZ", ", ", "'"])))
        elif a < b:                                    # replacement
            edits.append((a, b, rng.choice(["Q", "qq", "word"])))
        i += 2                                         # skip a gap, keep disjoint
    # keep at most one edit per exact point, drop identity edits
    seen_points = set()
    out = []
    for a, b, r in edits:
        if a == b:
            if a in seen_points:
                continue
            seen_points.add(a)
        if (a, b, r) == (a, b, "") and a == b:
            continue
        out.append((a, b, r))
    return out


@pytest.mark.parametrize("seed", range(150))
def test_fold_matches_direct_apply_over_random_rounds(seed):
    rng = random.Random(seed)
    orig = "".join(rng.choice("abcdefg .,'") for _ in range(rng.randint(5, 40)))
    layer = EditLayer()
    truth = orig
    for rnd in range(rng.randint(1, 6)):
        working = layer.render(orig)
        assert working == truth                        # render tracks the oracle
        edits = _random_round(rng, working)
        if not edits:
            continue
        round_edits = [_re(a, b, r, rnd=rnd + 1, fid=f"f-{rnd}-{i}")
                       for i, (a, b, r) in enumerate(edits)]
        res = layer.fold_round(orig, round_edits)       # guard=None: pure geometry
        layer = res.layer
        truth = _apply_disjoint(working, edits)
        # entries stay sorted and non-overlapping in original coordinates
        for e1, e2 in zip(layer.edits, layer.edits[1:]):
            assert (e1.orig_start, e1.orig_end) <= (e2.orig_start, e2.orig_end)
            assert e1.orig_end <= e2.orig_start
    assert layer.render(orig) == truth


# --- render ------------------------------------------------------------------

def test_render_empty_layer_is_identity():
    assert EditLayer().render("hello world") == "hello world"


def test_render_applies_entries_in_order():
    layer = EditLayer((
        Edit(0, 5, "HELLO", (_c(),)),
        Edit(6, 11, "WORLD", (_c(),)),
    ))
    assert layer.render("hello world") == "HELLO WORLD"


# --- single-round folding ----------------------------------------------------

def test_replacement_in_clean_text_maps_to_original():
    layer = EditLayer().fold_round("hello world", [_re(6, 11, "there")]).layer
    assert layer.render("hello world") == "hello there"
    assert layer.edits == (Edit(6, 11, "there", (_c(),)),)


def test_insertion_maps_to_original_point():
    layer = EditLayer().fold_round("ab", [_re(1, 1, "X")]).layer
    assert layer.render("ab") == "aXb"
    assert layer.edits[0].orig_start == layer.edits[0].orig_end == 1


def test_pure_deletion():
    layer = EditLayer().fold_round("a b c", [_re(1, 3, "")]).layer
    assert layer.render("a b c") == "a c"


# --- composition across rounds ----------------------------------------------

def test_second_round_edits_first_rounds_insertion_composes():
    # round 1 inserts " too" after "go"; round 2 edits inside that new text.
    l1 = EditLayer().fold_round("go", [_re(2, 2, " too", rnd=1, fid="f-1")]).layer
    assert l1.render("go") == "go too"
    # round 2 (coords of "go too"): fix "too" -> "to" at [3,6). orig stays "go".
    res = l1.fold_round("go", [_re(3, 6, "to", rnd=2, fid="f-2")])
    l2 = res.layer
    assert l2.render("go") == "go to"
    # one composed entry against the ORIGINAL, carrying both rounds' provenance
    assert len(l2.edits) == 1
    prov = [(c.round, c.finding_id) for c in l2.edits[0].contributions]
    assert prov == [(1, "f-1"), (2, "f-2")]


def test_second_round_edits_first_rounds_replacement_composes():
    l1 = EditLayer().fold_round("colour", [_re(0, 6, "color", fid="f-1")]).layer
    assert l1.render("colour") == "color"
    # round 2 appends "," right after the replacement text (clean, separate entry)
    r_after = l1.fold_round("colour", [_re(5, 5, ",", rnd=2, fid="f-2")]).layer
    assert r_after.render("colour") == "color,"
    assert len(r_after.edits) == 2                     # abutting end -> not composed
    # round 2 instead retypes inside the replacement -> composes into one entry
    r_in = l1.fold_round("colour", [_re(0, 5, "col033", rnd=2, fid="f-3")]).layer
    assert r_in.render("colour") == "col033"
    assert len(r_in.edits) == 1
    assert [c.finding_id for c in r_in.edits[0].contributions] == ["f-1", "f-3"]


def test_two_edits_in_one_round_inside_the_same_earlier_replacement():
    # Regression: both round-2 edits land inside round-1's replacement. Resolving
    # them against one shared frame (not re-rendering between them) is what keeps
    # the left edit's coordinates valid.
    l1 = EditLayer().fold_round("xxxx", [_re(0, 4, "abcd", fid="f-1")]).layer
    assert l1.render("xxxx") == "abcd"
    # round 2 (coords of "abcd"): "a"->"A" at [0,1) and "d"->"D" at [3,4)
    l2 = l1.fold_round("xxxx", [_re(0, 1, "A", rnd=2, fid="f-2a"),
                                _re(3, 4, "D", rnd=2, fid="f-2b")]).layer
    assert l2.render("xxxx") == "AbcD"
    assert len(l2.edits) == 1                          # one composed change
    assert [c.finding_id for c in l2.edits[0].contributions] == \
        ["f-1", "f-2a", "f-2b"]


def test_insertion_at_an_earlier_insertions_seam_orders_by_working_position():
    # Regression: a new insertion at the working offset where an earlier
    # insertion sits must render before it, as text[:p] + new + text[p:] would.
    l1 = EditLayer().fold_round("ab", [_re(1, 1, "X", fid="f-1")]).layer
    assert l1.render("ab") == "aXb"
    # round 2 inserts "'" at working offset 1 (just before X)
    l2 = l1.fold_round("ab", [_re(1, 1, "'", rnd=2, fid="f-2")]).layer
    assert l2.render("ab") == "a'Xb"


def test_deletion_then_insertion_at_the_seam_composes():
    # Round 1 deletes "bcd"; round 2 inserts at the seam the deletion left. The
    # insertion absorbs the deletion into one change rather than overlapping it.
    l1 = EditLayer().fold_round("abcde", [_re(1, 4, "", fid="f-1")]).layer
    assert l1.render("abcde") == "ae"
    l2 = l1.fold_round("abcde", [_re(1, 1, "Z", rnd=2, fid="f-2")]).layer
    assert l2.render("abcde") == "aZe"
    for e1, e2 in zip(l2.edits, l2.edits[1:]):         # no overlapping entries
        assert e1.orig_end <= e2.orig_start


def test_edit_spanning_two_earlier_edits_composes_all():
    # two separate round-1 edits, then a round-2 edit that spans across both.
    l1 = EditLayer().fold_round(
        "the cat sat",
        [_re(4, 7, "dog", fid="f-a"), _re(8, 11, "ran", fid="f-b")]).layer
    assert l1.render("the cat sat") == "the dog ran"
    # round 2 (coords of "the dog ran"): replace "dog ran" [4,11) with "fox fled"
    l2 = l1.fold_round("the cat sat",
                       [_re(4, 11, "fox fled", rnd=2, fid="f-c")]).layer
    assert l2.render("the cat sat") == "the fox fled"
    assert len(l2.edits) == 1
    assert [c.finding_id for c in l2.edits[0].contributions] == \
        ["f-a", "f-b", "f-c"]


# --- exact revert drops the change ------------------------------------------

def test_round_that_reverts_earlier_edit_drops_it():
    l1 = EditLayer().fold_round("cat", [_re(0, 3, "dog", fid="f-1")]).layer
    assert l1.render("cat") == "dog"
    res = l1.fold_round("cat", [_re(0, 3, "cat", rnd=2, fid="f-2")])
    assert res.layer.is_empty                          # net change is nothing
    assert res.noops == 1
    assert res.layer.render("cat") == "cat"


# --- edit guard routes an oversized composition to a query -------------------

def test_oversized_composition_is_reported_not_folded():
    guard = EditGuardConfig(max_edit_chars=8, max_added_chars=4)
    orig = "the small dog"
    l1 = EditLayer().fold_round(orig, [_re(4, 9, "biggish", fid="f-1")],
                                guard=guard).layer
    assert l1.render(orig) == "the biggish dog"
    # round 2 retypes inside -> composed span [4,9) with a long insert overflows
    res = l1.fold_round(orig,
                        [_re(4, 11, "enormouslyso", rnd=2, fid="f-2")],
                        guard=guard)
    assert res.layer.edits == l1.edits                 # layer unchanged
    assert len(res.oversteps) == 1
    rep = res.oversteps[0]
    assert isinstance(rep, OverstepReport)
    assert (rep.orig_start, rep.orig_end) == (4, 9)
    assert [c.finding_id for c in rep.contributions] == ["f-1", "f-2"]


def test_within_guard_composition_still_folds():
    guard = EditGuardConfig(max_edit_chars=64, max_added_chars=16)
    l1 = EditLayer().fold_round("colour", [_re(0, 6, "color", fid="f-1")],
                                guard=guard).layer
    l2 = l1.fold_round("colour", [_re(0, 5, "colur", rnd=2, fid="f-2")],
                       guard=guard).layer
    assert l2.render("colour") == "colur"
    assert len(l2.edits) == 1


# --- query anchor translation ------------------------------------------------

def test_to_orig_span_in_clean_region_is_exact():
    layer = EditLayer().fold_round("hello world", [_re(0, 5, "HI")]).layer
    # a query over "world" in the working text "HI world"
    lo, hi = layer.to_orig_span("hello world", 3, 8)
    assert "hello world"[lo:hi] == "world"


def test_to_orig_span_into_replacement_widens_to_whole_span():
    layer = EditLayer().fold_round("colour", [_re(0, 6, "color")]).layer
    # a query pointing at "olo" inside the replacement "color" widens to [0,6)
    lo, hi = layer.to_orig_span("colour", 1, 4)
    assert (lo, hi) == (0, 6)


# --- to_findings bridges to the validator path -------------------------------

def test_to_findings_uses_whole_paragraph_splice():
    para = ParagraphRef("body-0000", "word/document.xml", "body",
                        "the cat sat", "Normal")
    layer = EditLayer().fold_round(
        "the cat sat",
        [_re(4, 7, "dog", fid="f-a", expl="animal"),
         _re(8, 11, "ran", fid="f-b", expl="verb")]).layer
    ids = iter(["r-0001", "r-0002"])
    findings = layer.to_findings(para, "chunk-000", ids)
    assert len(findings) == 2
    for f in findings:
        assert f.original_text == "the cat sat"        # whole-paragraph splice
        assert f.para_id == "body-0000"
    # corrected_text splices only that entry's span
    assert findings[0].corrected_text == "the dog sat"
    assert findings[1].corrected_text == "the cat ran"


def test_joined_explanation_names_each_round():
    l1 = EditLayer().fold_round("go", [_re(2, 2, " too", fid="f-1",
                                           expl="add adverb")]).layer
    l2 = l1.fold_round("go too", [_re(4, 6, "to", rnd=2, fid="f-2",
                                      expl="fix spelling")]).layer
    para = ParagraphRef("body-0000", "w", "body", "go", "Normal")
    f = l2.to_findings(para, "chunk-000", iter(["r-0001"]))[0]
    assert f.explanation == "add adverb fix spelling"
