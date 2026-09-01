"""G3 — precedent query ranks similar rulings and returns their reasons verbatim."""

from galley.memory.store import MemoryStore
from galley.tools.precedent import TOOLS, precedents_for

from tests.galley.fakes import gfinding


def _store(tmp_path):
    store = MemoryStore.open(tmp_path / "mem.db", now="2026-08-21T00:00:00Z")
    store.add_precedent("comma_splice", "sat down, he", "reject",
                        reason="two independent clauses", book="A", ruled_by="ed")
    store.add_precedent("comma_splice", "ran fast, she", "accept",
                        reason="comma is fine here", book="B", ruled_by="ed")
    store.add_precedent("spelling", "teh", "accept",
                        reason="obvious typo", book="C", ruled_by="ed")
    return store


def test_type_match_ranks_first(tmp_path):
    store = _store(tmp_path)
    finding = gfinding("g-1", "body-0001", "sat down, he", "sat down. He",
                       error_type="comma_splice")
    ranked = precedents_for(finding, store)
    assert ranked[0].error_type == "comma_splice"
    # exact find-text match is the closest same-type precedent
    assert ranked[0].find_text == "sat down, he"
    assert ranked[0].reason == "two independent clauses"  # verbatim


def test_similar_ranks_above_dissimilar(tmp_path):
    store = _store(tmp_path)
    finding = gfinding("g-2", "body-0001", "sat down, he", "sat down. He",
                       error_type="comma_splice")
    ranked = precedents_for(finding, store)
    texts = [p.find_text for p in ranked if p.error_type == "comma_splice"]
    assert texts.index("sat down, he") < texts.index("ran fast, she")


def test_empty_store_returns_empty(tmp_path):
    store = MemoryStore.open(tmp_path / "mem.db", now="2026-08-21T00:00:00Z")
    finding = gfinding("g-3", "body-0001", "anything", "x")
    assert precedents_for(finding, store) == []


def test_limit_is_honored(tmp_path):
    store = _store(tmp_path)
    finding = gfinding("g-4", "body-0001", "teh", "the", error_type="spelling")
    assert len(precedents_for(finding, store, limit=1)) == 1


def test_registered_as_tool():
    assert TOOLS["precedents_for"] is precedents_for


def test_uncontested_findings_from_one_book_answer_the_next(tmp_path):
    """The point of ingest: a wave-one edit kept without dispute on book N is an
    ``accept`` precedent the same mark on book N+1 can lean on."""
    from galley.casefile import CaseFile
    from galley.memory.ingest import ingest_casefile

    with MemoryStore.open(tmp_path / "mem.db", now="2026-08-21T00:00:00Z") as store:
        cf = CaseFile(book="Book N")
        cf.findings = [gfinding("g-1", "body-0001", "recieve", "receive",
                                error_type="spelling", note="misspelling")]
        assert ingest_casefile(store, cf).ingested == 1
        later = gfinding("h-1", "body-0042", "recieve", "receive", error_type="spelling")
        ranked = precedents_for(later, store)
    assert ranked and ranked[0].ruling == "accept"
    assert ranked[0].book == "Book N" and ranked[0].reason == "misspelling"
