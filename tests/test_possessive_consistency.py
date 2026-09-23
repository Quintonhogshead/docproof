"""Possessives of names ending in s are one book-wide decision, not a per-site
edit (Immanuel, 2026-09-22: the author wrote “Dolores’” 36 times and
“Dolores’s” once; Galley's readers added the s at ten scattered sites)."""
from __future__ import annotations

from docproof.consistency import (POSSESSIVE_KEY, find_possessive_drift, possessive_conversion,
                                  possessive_policy, _possessive_sites)
from docproof.models import ParagraphRef


def para(pid, text, reviewable=True):
    return ParagraphRef(pid, "word/document.xml", "body", text, "Normal", reviewable)


def book(*texts):
    return {f"p{i}": t for i, t in enumerate(texts)}


IMMANUEL = book(
    "She barely covers half of Dolores’ palm now.",
    "The pad was sticking out of Dolores’ bag on the floor.",
    "Resting a presumptuous hand on Dolores’ shoulder, the woman comes closer.",
    "He waits for Dolores’ approval, which she gives with her broadest grin.",
    "Imagine it digging into Dolores’s neck.",
    "Two snares, Dolores’ clarinet, Pris’ French horn, and a tuba.",
    "Marlon is ogling Pris’ shoulders. Pris laughs.",
    "Dolores walks home.",
)


def test_the_authors_dominant_form_wins_and_the_minority_conforms():
    policy = possessive_policy(IMMANUEL)
    dolores = policy.names["Dolores"]
    assert (dolores.form, dolores.basis, dolores.bare, dolores.s) == ("bare", "name", 5, 1)
    # Pris has too few sites of her own; the book's pooled count decides.
    assert (policy.names["Pris"].form, policy.names["Pris"].basis) == ("bare", "book")
    paragraphs = [para(pid, text) for pid, text in IMMANUEL.items()]
    [row] = find_possessive_drift(paragraphs, policy)
    assert (row.para_id, row.original_text, row.corrected_text) == (
        "p4", "Imagine it digging into Dolores’s neck.", "Imagine it digging into Dolores’ neck.")
    assert row.error_type == POSSESSIVE_KEY and row.status == "validated"
    assert "5 of 6" in row.explanation


def test_the_s_form_dominates_the_other_way():
    texts = book("It was Chris’s car.", "We saw Chris’s house.", "Then Chris’s dog.", "And Chris’ cat.")
    policy = possessive_policy(texts)
    assert policy.form("Chris") == "s"
    [row] = find_possessive_drift([para(k, v) for k, v in texts.items()], policy)
    assert row.corrected_text == "And Chris’s cat."


def test_no_clear_preference_takes_chicagos_default():
    texts = book("James’ car.", "James’s house.", "Then James left.")
    policy = possessive_policy(texts)
    assert (policy.names["James"].form, policy.names["James"].basis) == ("s", "chicago")
    assert [r.corrected_text for r in find_possessive_drift([para(k, v) for k, v in texts.items()], policy)] == [
        "James’s car."]


def test_a_straight_apostrophe_is_kept_as_written():
    texts = book("It is Dolores' hat.", "It is Dolores' coat.", "It is Dolores' scarf.", "It is Dolores's glove.")
    [row] = find_possessive_drift([para(k, v) for k, v in texts.items()], possessive_policy(texts))
    assert row.corrected_text == "It is Dolores' glove."


def test_a_closing_single_quote_is_not_a_possessive():
    names = {"Dolores"}
    # UK dialogue: the ’ after the name closes the speech.
    assert _possessive_sites("‘Hi, Dolores’ she said.", names) == []
    assert _possessive_sites("‘It’s you, Dolores’, he says. ‘Come in.’", names) == []
    # Inside open speech, a possessive is still a possessive when the quote closes later.
    [site] = _possessive_sites("‘Is that Dolores’ car?’ she asked.", names)
    assert site.form == "bare"
    # And with no quotation open at all.
    assert [s.form for s in _possessive_sites("a favourite of Dolores’, and one of the only books", names)] == ["bare"]


def test_plurals_contractions_and_idioms_are_never_sites():
    texts = book("Laflesh looks toward the Petters’ window.", "The McCoys’ Hang on Sloopy.",
                 "The Smiths’ garden. Smith waved.", "Dolores’s been here. Dolores’s singing.",
                 "His Achilles’ heel.", "Achilles fought.", "Then Petters and McCoys and Smiths arrived.")
    policy = possessive_policy(texts)
    assert policy.names == {}
    assert find_possessive_drift([para(k, v) for k, v in texts.items()], policy) == []


def test_a_sentence_initial_word_is_not_a_name():
    texts = book("‘Thanks’ was all she said.", "Thanks’ meaning was plain.")
    assert possessive_policy(texts).names == {}


def test_unreviewable_paragraphs_are_counted_but_never_edited():
    texts = book("It is Dolores’ hat.", "It is Dolores’ coat.", "It is Dolores’ scarf.", "It is Dolores’s glove.")
    paragraphs = [para(k, v, reviewable=(k != "p3")) for k, v in texts.items()]
    assert find_possessive_drift(paragraphs, possessive_policy(texts)) == []


def test_a_conversion_against_the_authors_form_is_named():
    policy = possessive_policy(IMMANUEL)
    reason = possessive_conversion("He waits for Dolores’ approval.", "He waits for Dolores’s approval.", policy)
    assert reason and "Dolores’" in reason and "5 of 6" in reason
    # The comma swallowed with it (Immanuel FN21) is the same conversion.
    assert possessive_conversion("a favourite of Dolores’, and", "a favourite of Dolores’s and", policy)
    # Toward the author's form, and an added closing quote, pass.
    assert possessive_conversion("into Dolores’s neck.", "into Dolores’ neck.", policy) is None
    assert possessive_conversion("‘Hi, Dolores", "‘Hi, Dolores’", policy) is None
    # A name the policy never decided is not this guard's business.
    assert possessive_conversion("Thomas’ hat", "Thomas’s hat", policy) is None
    chicago = possessive_policy(book("James’ car.", "James’s house.", "Then James left."))
    assert possessive_conversion("James’s house.", "James’ house.", chicago)
