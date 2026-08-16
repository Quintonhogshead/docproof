"""English variants.

A handful of conventions flip on the variant, and getting one wrong is worse
than not supporting variants at all: a U.K. manuscript run as U.S. comes back
Americanized, with every change looking deliberate. So the tests here care
mostly about the flips actually reaching the stages that act on them.
"""
from __future__ import annotations

import pytest

from docproof.analyzer import build_system_prompt
from docproof.config import Config, load_config
from docproof.error_registry import load_error_types
from docproof.normalize import normalize_text
from docproof.sweeps import SWEEPS_BY_KEY, apply_hits
from docproof.variants import VARIANT_KEYS, load_variant

from .test_error_types import ERROR_DIR


def swept(key, text, variant):
    return apply_hits(text, SWEEPS_BY_KEY[key].scan(text, variant))


# --- the files ----------------------------------------------------------------

@pytest.mark.parametrize("key", VARIANT_KEYS)
def test_every_variant_loads(key):
    v = load_variant(key)
    assert v.key == key and v.name and v.conventions
    assert v.authorities, f"{key} names no authority"
    assert v.primary_quote in ("single", "double")


def test_the_variants_split_the_way_the_brief_says():
    """U.S. and Canadian take double-quote dialogue; U.K. and Australian take
    single. Canadian is the trap — U.K.-looking spellings, U.S. punctuation."""
    assert load_variant("us").primary_quote == "double"
    assert load_variant("ca").primary_quote == "double"
    assert load_variant("uk").primary_quote == "single"
    assert load_variant("au").primary_quote == "single"


def test_the_hybrids_ask_the_press_to_confirm():
    assert load_variant("ca").confirm and load_variant("au").confirm
    assert not load_variant("us").confirm and not load_variant("uk").confirm


def test_an_unknown_variant_names_the_real_ones():
    with pytest.raises(FileNotFoundError, match="No such English variant"):
        load_variant("nz")


def test_each_variant_picks_its_own_dictionary():
    assert [load_variant(k).dictionary for k in VARIANT_KEYS] == \
        ["en_US", "en_GB", "en_CA", "en_AU"]


# --- what the model is told ---------------------------------------------------

def test_the_conventions_reach_the_system_prompt():
    types = list(load_error_types(ERROR_DIR, ["that_which"]).values())
    uk = build_system_prompt(types, conventions=load_variant("uk").prompt_section())
    assert "U.K. English" in uk
    assert "single (‘…’) for primary dialogue" in uk
    assert "per cent" in uk
    # ...and it says it wins over a type's own wording, since that_which
    # states the Chicago rule in its section.
    assert "override any convention stated in an error type" in uk


def test_a_run_without_a_variant_prompt_is_unchanged():
    types = list(load_error_types(ERROR_DIR, ["that_which"]).values())
    assert "ENGLISH VARIANT" not in build_system_prompt(types)


def test_a_hybrid_variant_tells_the_model_to_prefer_a_query():
    section = load_variant("ca").prompt_section()
    assert "prefer a query over a correction" in section


# --- the dialogue-tag sweep ---------------------------------------------------

US_TAG = "“Wait here.” he said."
UK_TAG = "‘Wait here.’ he said."


def test_the_sweep_follows_the_variants_quotation_marks():
    """A pattern hard-coded to double quotes would run over a U.K. manuscript
    and report zero matches, which reads exactly like a clean book."""
    us, uk = load_variant("us"), load_variant("uk")
    assert swept("sweep_dialogue_tag", US_TAG, us) == "“Wait here,” he said."
    assert swept("sweep_dialogue_tag", UK_TAG, uk) == "‘Wait here,’ he said."


def test_the_sweep_ignores_the_other_variants_marks():
    us, uk = load_variant("us"), load_variant("uk")
    assert swept("sweep_dialogue_tag", UK_TAG, us) == UK_TAG
    assert swept("sweep_dialogue_tag", US_TAG, uk) == US_TAG


def test_australian_follows_the_uk_pattern():
    au = load_variant("au")
    assert swept("sweep_dialogue_tag", UK_TAG, au) == "‘Wait here,’ he said."


# --- the normalizer -----------------------------------------------------------

def test_a_uk_line_of_dialogue_curls_as_a_pair():
    """In a single-quote variant a word-initial mark is usually dialogue
    opening, not a dialect elision."""
    uk = load_variant("uk")
    assert normalize_text("'Hello,' she said.", variant=uk) == "‘Hello,’ she said."


def test_elisions_still_curl_right_in_every_variant():
    """'Tis is an apostrophe wherever it appears, and a variant that got this
    wrong would put an opening quote in front of every dialect contraction."""
    for key in VARIANT_KEYS:
        v = load_variant(key)
        assert normalize_text("'Tis the season.", variant=v) == "’Tis the season."
        assert normalize_text("He was runnin' home.", variant=v) == \
            "He was runnin’ home."


def test_contractions_and_possessives_are_variant_independent():
    for key in VARIANT_KEYS:
        v = load_variant(key)
        assert normalize_text("It's the dogs' bowl.", variant=v) == \
            "It’s the dogs’ bowl."


def test_an_unmatched_mark_is_still_left_alone_in_a_double_quote_variant():
    us = load_variant("us")
    text = "unmatched 'quote with nothing to close it"
    assert normalize_text(text, variant=us) == text


# --- config -------------------------------------------------------------------

def test_the_shipped_config_is_us_by_default():
    assert load_config("config/default.yaml").variant == "us"
    assert Config().variant == "us"


def test_the_variant_is_stated_never_inferred():
    """There is deliberately no auto-detection. Guessing the variant and then
    applying its rules silently is how a U.K. book comes back Americanized."""
    assert not hasattr(Config(), "detect_variant")


def test_an_invalid_variant_is_rejected_by_config():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Config(variant="nz")


def test_the_variant_picks_the_dictionary_unless_told_otherwise():
    cfg = load_config("config/default.yaml")
    assert cfg.spellcheck.dictionary is None, \
        "a pinned dictionary would silently override every variant"


# --- variant respellings (DP-003) ---------------------------------------------

def test_us_respell_covers_the_missed_evidence():
    """'grey' and 'travellers' survived a U.S. manuscript because en_US
    accepts both; the respell list is what finally raises them."""
    us = load_variant("us")
    assert us.respell_map["grey"] == "gray"
    assert us.respell_map["travellers"] == "travelers"


def test_respell_lists_are_mirrored_not_shared():
    uk = load_variant("uk")
    assert uk.respell_map["gray"] == "grey"
    assert "grey" not in uk.respell_map


def test_respell_sites_every_occurrence_as_its_own_ruling():
    from docproof.adjudicate import generate
    from docproof.models import ParagraphRef
    ps = [ParagraphRef("body-0000", "word/document.xml", "body",
                       "The grey sky met the travellers at dawn, and "
                       "Mr. Grey watched them pass.", "Normal")]
    cands = generate(ps, respell=load_variant("us").respell_map)
    respell = [c for c in cands if c.kind == "respell"]
    # Three sites — the proper-noun "Grey" included, because keeping it is
    # the adjudicator's call to make in context, not the generator's.
    assert len(respell) == 3
    assert {c.suggestion for c in respell} == {"gray", "travelers"}
