"""The three genre-pack query-only scans (docproof/genrescans.py).

Every positive case here also checks `force_query is True` — the mechanism
that makes "query-only" a guarantee of the Finding object itself rather than
a config default a run could quietly override. See the module docstring in
genrescans.py for why: anachronism and citation_format reason about the world
outside the manuscript (an external-authority check, hallucination risk);
reading_level is internal but still has no "right" answer, only a question.
"""
from __future__ import annotations

from docproof.config import (AnachronismScanConfig, CitationFormatScanConfig,
                             GenreScansConfig, ReadingLevelScanConfig)
from docproof.genrescans import (anachronism_findings,
                                 citation_format_findings,
                                 reading_level_findings, run_genre_scans)
from docproof.models import ParagraphRef


def _para(text, i=0):
    return ParagraphRef(f"body-{i:04d}", "word/document.xml", "body", text,
                        "Normal")


# --- anachronism ---------------------------------------------------------

def test_anachronism_flags_a_word_that_postdates_the_stated_era():
    cfg = AnachronismScanConfig(enabled=True, era=1400)
    findings = anachronism_findings(
        [_para("The knight reached for his smartphone before the battle.")],
        cfg)
    assert len(findings) == 1
    f = findings[0]
    assert f.error_type == "anachronism"
    assert f.force_query is True
    assert f.corrected_text == f.original_text     # a query changes nothing
    assert "smartphone" in f.original_text.lower()


def test_anachronism_does_not_flag_a_word_that_predates_the_era():
    cfg = AnachronismScanConfig(enabled=True, era=2020)
    findings = anachronism_findings(
        [_para("The knight reached for his smartphone before the battle.")],
        cfg)
    assert findings == []


def test_anachronism_with_no_era_stated_is_a_deliberate_no_op():
    """era=None must never be guessed — enabled alone is not enough."""
    cfg = AnachronismScanConfig(enabled=True, era=None)
    findings = anachronism_findings(
        [_para("She checked her smartphone one more time before the meeting.")],
        cfg)
    assert findings == []


def test_anachronism_disabled_finds_nothing_even_with_an_era():
    cfg = AnachronismScanConfig(enabled=False, era=1000)
    findings = anachronism_findings(
        [_para("The wizard's smartphone glowed faintly in the dark.")], cfg)
    assert findings == []


def test_anachronism_respects_the_query_cap():
    cfg = AnachronismScanConfig(enabled=True, era=1000, max_queries=1)
    paras = [_para(f"Paragraph {i} mentions a smartphone and a laptop.", i)
            for i in range(5)]
    findings = anachronism_findings(paras, cfg)
    assert len(findings) <= 1


# --- citation format -------------------------------------------------------

def test_citation_format_flags_two_mixed_styles():
    cfg = CitationFormatScanConfig(enabled=True, min_occurrences=2)
    paras = [
        _para("The study found strong effects (Smith, 2019).", 0),
        _para("A later paper confirmed this (Jones, 2021).", 1),
        _para("Other researchers agree [12].", 2),
        _para("This was also noted elsewhere [13, 14].", 3),
    ]
    findings = citation_format_findings(paras, cfg)
    assert len(findings) == 2         # one per style found
    assert all(f.error_type == "citation_format" for f in findings)
    assert all(f.force_query is True for f in findings)
    assert all(f.corrected_text == f.original_text for f in findings)


def test_citation_format_one_style_only_raises_nothing():
    cfg = CitationFormatScanConfig(enabled=True, min_occurrences=2)
    paras = [
        _para("The study found strong effects (Smith, 2019).", 0),
        _para("A later paper confirmed this (Jones, 2021).", 1),
        _para("And a third source agrees too (Lee, 2022).", 2),
    ]
    assert citation_format_findings(paras, cfg) == []


def test_citation_format_below_min_occurrences_raises_nothing():
    """One (Author, Year) and one [12] is not "the book mixes styles" —
    min_occurrences requires each style to be established, not a one-off."""
    cfg = CitationFormatScanConfig(enabled=True, min_occurrences=3)
    paras = [
        _para("The study found strong effects (Smith, 2019).", 0),
        _para("Other researchers agree [12].", 1),
    ]
    assert citation_format_findings(paras, cfg) == []


def test_citation_format_disabled_finds_nothing():
    cfg = CitationFormatScanConfig(enabled=False, min_occurrences=1)
    paras = [
        _para("Effects were strong (Smith, 2019).", 0),
        _para("Also noted elsewhere (Jones, 2021).", 1),
        _para("See also [1].", 2),
        _para("And [2].", 3),
    ]
    assert citation_format_findings(paras, cfg) == []


# --- reading level -----------------------------------------------------------

_PLAIN = ("The dog ran. The cat sat. The sun was hot. Birds flew by. "
         "It was a good day for all of them to be outside together "
         "in the warm afternoon light near the old wooden fence.")
_DENSE = ("Notwithstanding the aforementioned methodological considerations, "
         "the epistemological ramifications of the researchers' "
         "phenomenological approach necessitate a substantial "
         "reconceptualization of the interdisciplinary theoretical "
         "framework underpinning the entire investigative enterprise.")


def test_reading_level_flags_a_paragraph_far_from_the_books_own_band():
    cfg = ReadingLevelScanConfig(enabled=True, tolerance=6.0, min_words=10)
    # Several plain paragraphs establish the book's own median; one dense
    # outlier should stand out against it.
    paras = [_para(_PLAIN, i) for i in range(4)] + [_para(_DENSE, 4)]
    findings = reading_level_findings(paras, cfg)
    assert len(findings) == 1
    f = findings[0]
    assert f.error_type == "reading_level"
    assert f.force_query is True
    assert f.para_id == "body-0004"


def test_reading_level_uniform_manuscript_flags_nothing():
    cfg = ReadingLevelScanConfig(enabled=True, tolerance=6.0, min_words=10)
    paras = [_para(_PLAIN, i) for i in range(5)]
    assert reading_level_findings(paras, cfg) == []


def test_reading_level_short_paragraphs_are_excluded_from_scoring():
    cfg = ReadingLevelScanConfig(enabled=True, tolerance=1.0, min_words=200)
    paras = [_para(_PLAIN, i) for i in range(3)] + [_para(_DENSE, 3)]
    assert reading_level_findings(paras, cfg) == []


def test_reading_level_disabled_finds_nothing():
    cfg = ReadingLevelScanConfig(enabled=False, tolerance=1.0, min_words=5)
    paras = [_para(_PLAIN, i) for i in range(4)] + [_para(_DENSE, 4)]
    assert reading_level_findings(paras, cfg) == []


def test_reading_level_explicit_target_overrides_the_books_own_median():
    cfg = ReadingLevelScanConfig(enabled=True, target_ari=100.0,
                                 tolerance=1.0, min_words=10)
    # Every paragraph here is plain, but the explicit target is absurdly
    # high, so all of them should now read as "too simple."
    paras = [_para(_PLAIN, i) for i in range(3)]
    findings = reading_level_findings(paras, cfg)
    assert len(findings) == 3
    assert all("simpler" in f.explanation for f in findings)


# --- dispatcher ---------------------------------------------------------------

def test_run_genre_scans_combines_all_three_when_enabled():
    cfg = GenreScansConfig(
        anachronism=AnachronismScanConfig(enabled=True, era=1000),
        citation_format=CitationFormatScanConfig(enabled=True,
                                                  min_occurrences=2),
        reading_level=ReadingLevelScanConfig(enabled=True, tolerance=6.0,
                                             min_words=10))
    paras = [
        _para("The knight checked his smartphone before the duel.", 0),
        _para("Effects were strong (Smith, 2019).", 1),
        _para("This confirms earlier work (Jones, 2020).", 2),
        _para("Others agree [1].", 3),
        _para("As do these [2, 3].", 4),
    ] + [_para(_PLAIN, i) for i in range(5, 9)] + [_para(_DENSE, 9)]
    findings = run_genre_scans(paras, cfg)
    kinds = {f.error_type for f in findings}
    assert kinds == {"anachronism", "citation_format", "reading_level"}
    assert all(f.force_query for f in findings)


def test_run_genre_scans_all_disabled_finds_nothing():
    cfg = GenreScansConfig()
    paras = [_para("The knight checked his smartphone (Smith, 2019) [1].", 0)]
    assert run_genre_scans(paras, cfg) == []
