"""The last tier: the queries nothing below it could answer, given the book.

What reaches here is not a harder decision but a question that was asked of a
page. "Should 'the' be removed as in previous instances of Winn-Dixie?" is a
consistency lookup whose answer is spread over four hundred pages; "should 'I'
be added before 'locked'?" is settled by whether the narration is first person.
So these tests are mostly about the *input* — that the passage around a mark and
every occurrence of the terms it names are gathered deterministically, and that
what the model then says is guarded exactly as every tier below it is.
"""
from __future__ import annotations

from docproof.corrections.escalate import (escalate_queries, gather_terms,
                                           passage_around, term_evidence)
from docproof.corrections.idml import parse_story
from docproof.corrections.model import Edit, JUDGMENT, MECHANICAL
from docproof.corrections.pagemap import build_page_map
from docproof.models import Usage
from docproof.providers import NormalizedUsage, ProviderResult

from .fakes import DyingProvider, FakeProvider


def _story(*paragraphs: str, story_id: str = "s1"):
    body = "".join(
        '<ParagraphStyleRange AppliedParagraphStyle="ParagraphStyle/Body">'
        f"<CharacterStyleRange><Content>{p}</Content>"
        "<Br /></CharacterStyleRange></ParagraphStyleRange>"
        for p in paragraphs)
    return parse_story(('<?xml version="1.0"?><Story>' + body
                        + "</Story>").encode("utf-8"), story_id)


# A book that says "Winn-Dixie" without the article seven times and with it
# once — the shape of a real consistency question, where counting is the answer
# and no single page carries the evidence.
BOOK = [
    "She drove to Winn-Dixie before the sun came up.",
    "The Winn-Dixie parking lot was already full of gulls.",
    "Winn-Dixie had the good peaches that year.",
    "He worked at Winn-Dixie the summer he turned nineteen.",
    "Nobody at Winn-Dixie knew her name and she liked that.",
    "They met behind Winn-Dixie and did not speak.",
    "Winn-Dixie closed in the fall, and the lot went quiet.",
]


def _book_and_scope():
    story = _story(*BOOK)
    return [story], build_page_map([story], BOOK)


# --- gathering ----------------------------------------------------------------

def test_a_distinctive_name_is_picked_out_of_the_note():
    """The term a consistency question names — hyphenated and capitalized, the
    shape of a proper noun — is found without being told, and the quoted article
    the reviewer wrote comes along too."""
    terms = gather_terms('Should "the" be removed as in previous instances of '
                         'Winn-Dixie?', "The Winn-Dixie parking lot")
    assert "Winn-Dixie" in terms
    assert "the" in terms


def test_an_ordinary_sentence_yields_no_terms_to_research():
    """A note with no name in it asks nothing the book can be searched for, so
    nothing is researched — the tier does not go hunting for evidence that does
    not exist."""
    assert gather_terms("Confusing — should this be deleted?",
                        "and locked on to hers.") == []


def test_the_evidence_counts_every_instance_in_the_whole_book():
    """The count is the answer to a consistency question, and it is gathered from
    the whole book rather than the page — which is the thing no tier below this
    one could do."""
    stories, _ = _book_and_scope()
    found = term_evidence(stories, ["Winn-Dixie"])
    assert len(found) == 1
    term, count, hits = found[0]
    assert (term, count) == ("Winn-Dixie", 7)
    assert len(hits) == 7
    assert sum(1 for h in hits if "The Winn-Dixie" in h) == 1


def test_a_term_the_book_carries_once_is_not_evidence():
    """One instance settles no question about consistency, so it is dropped
    rather than sent up as if it meant something."""
    stories, _ = _book_and_scope()
    assert term_evidence(stories, ["peaches"]) == []


def test_the_passage_reaches_past_the_page_the_mark_was_on():
    """The point of the tier: a query is answered against the scene, not the
    page. The passage around page 2 carries the pages either side of it."""
    stories, scope = _book_and_scope()
    passage = passage_around(stories, scope, 2, radius=2)
    assert "The Winn-Dixie parking lot" in passage     # the page itself
    assert "before the sun came up" in passage         # the page before
    assert "the good peaches" in passage               # the page after
    assert "closed in the fall" not in passage         # beyond the radius


def test_an_unplaced_page_yields_no_passage():
    stories, scope = _book_and_scope()
    assert passage_around(stories, scope, 99) == ""
    assert passage_around(stories, None, 1) == ""


# --- the tier -----------------------------------------------------------------

def _query(instruction: str, context: str, page: int = 2) -> Edit:
    """A query in the shape the extractor emits one: anchored, but with no
    concrete change, so it flags as a person's."""
    return Edit(id="c1", find=context, replace=context, kind=JUDGMENT,
                instruction=instruction, context=context, page=page,
                source="p2-1")


def test_a_consistency_query_is_resolved_on_the_book_s_own_evidence():
    """The reviewer stated the rule — match the other instances — and the book
    holds the answer. The query becomes a concrete edit, and the note says what
    the evidence was."""
    stories, scope = _book_and_scope()
    edit = _query('Should "the" be removed as in previous instances of '
                  'Winn-Dixie?', "The Winn-Dixie parking lot was already full")
    provider = FakeProvider([ProviderResult(
        parsed={"verdict": "resolve",
                "find": "The Winn-Dixie parking lot",
                "replace": "Winn-Dixie parking lot",
                "context": "",
                "note": "six of seven instances take no article"},
        usage=NormalizedUsage(input_tokens=900, output_tokens=60))])
    out, resolved, advised = escalate_queries(
        [edit], provider, model="m", usage=Usage(), stories=stories, scope=scope)
    assert (resolved, advised) == (1, 0)
    assert out[0].kind == MECHANICAL
    assert out[0].find == "The Winn-Dixie parking lot"
    assert "six of seven instances" in out[0].instruction
    # It was given both the passage and the whole book's evidence.
    sent = provider.calls[0]["user"]
    assert "7 occurrences in the book" in sent
    assert "THE BOOK AROUND THAT MARK" in sent


def test_a_question_of_intent_is_advised_on_and_stays_a_person_s():
    """Dialect written as dialect is not an error, and "correcting" it is a real
    loss — so the tier does not decide it. The query stays exactly what it was,
    and arrives carrying the reading."""
    stories, scope = _book_and_scope()
    edit = _query("Unsure if there's a typo here", "Winn-Dixie had the good")
    provider = FakeProvider([ProviderResult(
        parsed={"verdict": "recommend", "note": "the phrasing matches how this "
                                                "character speaks elsewhere; I "
                                                "would leave it"},
        usage=NormalizedUsage(input_tokens=800, output_tokens=40))])
    out, resolved, advised = escalate_queries(
        [edit], provider, model="m", usage=Usage(), stories=stories, scope=scope)
    assert (resolved, advised) == (0, 1)
    assert out[0].kind == JUDGMENT and out[0].find == out[0].replace
    assert "how this character speaks" in out[0].advice


def test_a_resolution_quoting_text_not_in_the_passage_is_refused():
    """The anchor has to be copied out of the book it was shown, never recalled.
    An answer that fails that check falls back to advice, so the work is not lost
    but nothing unanchorable is proposed."""
    stories, scope = _book_and_scope()
    edit = _query("Should this match the others?", "The Winn-Dixie parking lot")
    provider = FakeProvider([ProviderResult(
        parsed={"verdict": "resolve", "find": "a line from another book",
                "replace": "something else", "note": "recalled, not quoted"})])
    out, resolved, advised = escalate_queries(
        [edit], provider, model="m", usage=Usage(), stories=stories, scope=scope)
    assert resolved == 0 and advised == 1
    assert out[0].kind == JUDGMENT and out[0].find == edit.find


def test_a_failed_call_leaves_the_query_untouched():
    """A tier that dies changes nothing — the flag it was reading stands."""
    stories, scope = _book_and_scope()
    edit = _query("Should this be deleted?", "The Winn-Dixie parking lot")
    out, resolved, advised = escalate_queries(
        [edit], DyingProvider(survive=0), model="m", usage=Usage(),
        stories=stories, scope=scope)
    assert (resolved, advised) == (0, 0)
    assert out == [edit]


def test_only_queries_are_escalated():
    """A concrete edit is not a query and is never re-opened, however it was
    made — the tier reads what would be flagged, not what would land."""
    stories, scope = _book_and_scope()
    concrete = Edit(id="c1", find="the good peaches", replace="the best peaches",
                    page=3)
    provider = FakeProvider([])
    out, resolved, advised = escalate_queries(
        [concrete], provider, model="m", usage=Usage(), stories=stories,
        scope=scope)
    assert (resolved, advised) == (0, 0)
    assert out == [concrete] and provider.calls == []


def test_the_spend_is_accrued_for_the_run_to_price():
    stories, scope = _book_and_scope()
    usage = Usage()
    provider = FakeProvider([ProviderResult(
        parsed={"verdict": "recommend", "note": "a view"},
        usage=NormalizedUsage(input_tokens=1200, output_tokens=80))])
    escalate_queries([_query("Typo?", "Winn-Dixie had the good")], provider,
                     model="m", usage=usage, stories=stories, scope=scope)
    assert usage.input_tokens == 1200 and usage.output_tokens == 80
