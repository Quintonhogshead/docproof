"""Recurrence propagation: a validated word/phrase swap is re-emitted at every
other occurrence of the original surface, so a fix caught in one place is no
longer missed in another (the catch-it-here-miss-it-there failure).

See docproof.adjudicate.propagate_recurrences and its call site in
pipeline.finish.
"""
from __future__ import annotations

import pytest

from docproof.adjudicate import propagate_recurrences
from docproof.models import Anchor, DocumentModel, Finding, ParagraphRef

spylls = pytest.importorskip("spylls", reason="the dictionary needs spylls")


def paras(*texts: str) -> list[ParagraphRef]:
    return [ParagraphRef(f"body-{i:04d}", "word/document.xml", "body", t,
                         "Normal") for i, t in enumerate(texts)]


def _edit(para: ParagraphRef, surface: str, fix: str, *, occ: int = 1,
          confidence: str = "high") -> Finding:
    """A validated edit swapping `surface` for `fix` at its `occ`-th occurrence."""
    start = -1
    for _ in range(occ):
        start = para.text.find(surface, start + 1)
    end = start + len(surface)
    return Finding(
        finding_id="seed", chunk_id="chunk-000", para_id=para.para_id,
        error_type="spelling", original_text=para.text, occurrence=1,
        corrected_text=para.text[:start] + fix + para.text[end:],
        explanation="", confidence=confidence, status="validated",
        anchor=Anchor(start=start, end=end, delete_text=surface,
                      insert_text=fix))


def test_nonword_typo_propagates_as_edit_everywhere():
    ps = paras("She staired at the wall.",             # fixed here (the seed)
               "Later he staired at the floor.",        # missed — must propagate
               "STAIRED, again, into the dark.")        # different casing
    seed = _edit(ps[0], "staired", "stared")
    out = propagate_recurrences([seed], ps)
    assert {f.para_id for f in out} == {"body-0001", "body-0002"}
    assert all(not f.force_query for f in out)          # a non-word: a silent edit
    # Casing is matched to each site.
    fixes = {f.para_id: f.corrected_text for f in out}
    assert "stared" in fixes["body-0001"]
    assert "STARED" in fixes["body-0002"]


def test_a_curated_import_row_never_seeds_propagation():
    # P1-4: a curated/imported one-off (a TOC line matched to an epigraph) must
    # not flood the margin with queries about every ordinary use of a common
    # word. Purpura: one curated "massive"->"drastic" queried all 9 other uses.
    ps = paras("A massive overhaul was needed.",         # the curated seed site
               "It was a massive undertaking.",           # ordinary use — leave it
               "The massive doors swung open.")           # ordinary use — leave it
    seed = _edit(ps[0], "massive", "drastic")
    seed = seed.__class__(**{**seed.__dict__, "finding_id": "import-0001"})
    assert propagate_recurrences([seed], ps) == []


def test_a_machine_row_still_seeds_alongside_an_imported_one():
    # The gate is provenance-specific: an imported row does not seed, but a
    # machine detector row in the same set still does.
    ps = paras("She staired at the wall.",
               "Later he staired at the floor.",
               "A massive door.", "Another massive door.")
    machine = _edit(ps[0], "staired", "stared")          # finding_id "seed"
    imported = _edit(ps[2], "massive", "drastic")
    imported = imported.__class__(**{**imported.__dict__,
                                     "finding_id": "replay-0002"})
    out = propagate_recurrences([machine, imported], ps)
    assert {f.para_id for f in out} == {"body-0001"}     # only the typo propagated


def test_seed_site_is_not_re_emitted():
    ps = paras("She staired at the wall.")
    seed = _edit(ps[0], "staired", "stared")
    assert propagate_recurrences([seed], ps) == []      # only the seed site exists


def test_real_word_swap_is_a_query_not_a_silent_edit():
    ps = paras("It had a big effect on her.",           # seed: effect -> affect
               "It did not effect him at all.")          # may be correct here
    seed = _edit(ps[0], "effect", "affect")
    out = propagate_recurrences([seed], ps)
    assert [f.para_id for f in out] == ["body-0001"]
    (f,) = out
    assert f.force_query                                 # real word: ask, never edit
    assert "affect" in f.corrected_text


def test_conflicting_fixes_drop_the_surface_entirely():
    # The same surface changed two different ways is context-dependent; nothing
    # is swept from it.
    ps = paras("The effect was large.",                 # effect -> affect
               "The effect was small.",                 # effect -> effects
               "It had no effect here.")                 # the site that would propagate
    a = _edit(ps[0], "effect", "affect")
    b = _edit(ps[1], "effect", "effects")
    assert propagate_recurrences([a, b], ps) == []


def test_protected_lexicon_is_never_swept():
    ps = paras("The Vashteen guard stood.",             # a coined name
               "Another Vashteen passed by.")
    # Even if one site were (wrongly) validated as an edit, protection wins.
    seed = _edit(ps[0], "Vashteen", "Vashteem")
    out = propagate_recurrences([seed], ps, protected=["Vashteen"])
    assert out == []


def test_punctuation_and_grammar_edits_do_not_propagate():
    ps = paras("He ran, and left.", "She ran and left.")
    # A comma-insertion diff (delete "", insert ", ") is not a word swap.
    seed = Finding(
        finding_id="seed", chunk_id="chunk-000", para_id="body-0000",
        error_type="comma", original_text=ps[0].text, occurrence=1,
        corrected_text="He ran and left.", explanation="", confidence="high",
        status="validated",
        anchor=Anchor(start=6, end=8, delete_text=", ", insert_text=" "))
    assert propagate_recurrences([seed], ps) == []


def test_cap_is_logged_not_silently_truncated(caplog):
    ps = paras("staired " * 5)
    seed = _edit(ps[0], "staired", "stared")
    with caplog.at_level("WARNING"):
        out = propagate_recurrences([seed], ps, max_sites_per_surface=2)
    # Five occurrences; the seed's own site (a validated anchor) is skipped, so
    # four remain, of which the 2-site cap lets two through and drops two.
    assert len(out) == 2
    assert any("cap" in r.message for r in caplog.records)


def test_tiny_surface_does_not_seed_propagation():
    # A minimal diff can trim to a single-letter surface (a "B" -> "B." initial
    # fix). Propagating it would touch every stray "B" in the book; it must not.
    ps = paras("Marshall B stood.", "Then B spoke.", "And B left.")
    seed = _edit(ps[0], "B", "B.")
    assert propagate_recurrences([seed], ps) == []


def test_common_word_surface_is_dropped_not_flooded(caplog):
    # "try and" -> "try to" trims to the surface "and"; a real, common word.
    # It must not raise a query at every "and" in the book (the Breniman flood).
    ps = paras("You should try and rest.",             # seed: and -> to
               *[f"A cat and a dog number {i}." for i in range(20)])
    seed = _edit(ps[0], "and", "to")
    with caplog.at_level("INFO"):
        out = propagate_recurrences([seed], ps)
    assert out == []
    assert any("common word" in r.message for r in caplog.records)


def test_a_real_word_recurring_a_few_times_is_still_queried():
    # The cap only fires on a flood: a handful of sites still propagate as
    # queries, so the genuine catch-it-here-miss-it-there case is preserved.
    ps = paras("It had a big effect on her.",           # seed: effect -> affect
               "It did not effect him.",                # queried
               "That will effect nothing.")             # queried
    seed = _edit(ps[0], "effect", "affect")
    out = propagate_recurrences([seed], ps)
    assert {f.para_id for f in out} == {"body-0001", "body-0002"}
    assert all(f.force_query for f in out)
