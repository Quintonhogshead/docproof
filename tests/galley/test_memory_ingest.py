"""G2 — precedent ingest from archived jobs: tolerant reader, idempotence, skips.

Fixtures are built inline as dicts (kept close to the assertions; no on-disk
fixture files needed except the one directory-walk test, which writes temp JSON).
"""

import json

import pytest

from galley.memory.ingest import (
    RULING_VOCAB,
    IngestSummary,
    ingest_archive,
    ingest_job,
)
from galley.memory.store import MemoryStore

PINNED = "2026-08-23T00:00:00Z"


@pytest.fixture
def store(tmp_path):
    with MemoryStore.open(tmp_path / "mem.db", now=PINNED) as s:
        yield s


# ---- fixture records ---------------------------------------------------


def corrections_record():
    """A finished corrections job: a human accepted / dismissed / swapped marks."""
    return {
        "kind": "corrections",
        "book": "Shams Book 7",
        "ruled_by": "Quinton",
        "resolutions": [
            {
                "error_type": "missing_comma",
                "find_text": "cold dark night",
                "resolution": "accept",
                "reason": "coordinate adjectives",
            },
            {
                "error_type": "hyphenation",
                "find_text": "well known author",
                "resolution": "dismiss",  # human dismissed the mark -> reject
            },
            {
                "error_type": "word_choice",
                "find_text": "affect",
                "resolution": "swap",  # author supplied a replacement -> accept
                "to": "effect",
                "ruled_by": "Author",
            },
        ],
    }


def review_record():
    """A finished review job: findings each carry a disposition."""
    return {
        "kind": "review",
        "title": "Redding",
        "reviewer": "gate",
        "findings": [
            {
                "error_type": "spelling",
                "find_text": "recieve",
                "disposition": "applied",  # -> accept
            },
            {
                "error_type": "comma_splice",
                "find_text": "it was late, we left",
                "disposition": "queried",  # -> query
                "reason": "author voice",
            },
            {
                "error_type": "capitalization",
                "find_text": "the earth",
                "disposition": "rejected",  # -> reject
            },
        ],
    }


# ---- basic ingest ------------------------------------------------------


def test_ingest_corrections_lands_fields(store):
    summary = ingest_job(store, corrections_record(), now=PINNED)
    assert summary.ingested == 3
    assert summary.skipped == 0

    by_type = {p.error_type: p for p in store.precedents()}

    accept = by_type["missing_comma"]
    assert accept.find_text == "cold dark night"
    assert accept.ruling == "accept"
    assert accept.book == "Shams Book 7"
    assert accept.ruled_by == "Quinton"  # job-level default
    assert accept.reason == "coordinate adjectives"

    reject = by_type["hyphenation"]
    assert reject.ruling == "reject"  # dismiss normalized

    swap = by_type["word_choice"]
    assert swap.ruling == "accept"  # swap normalized to accept
    assert swap.ruled_by == "Author"  # item-level override wins
    assert "swap -> effect" in swap.reason  # swap detail preserved


def test_ingest_review_lands_fields(store):
    summary = ingest_job(store, review_record(), now=PINNED)
    assert summary.ingested == 3

    by_type = {p.error_type: p for p in store.precedents()}
    assert by_type["spelling"].ruling == "accept"
    assert by_type["spelling"].book == "Redding"  # from "title"
    assert by_type["comma_splice"].ruling == "query"
    assert by_type["comma_splice"].ruled_by == "gate"  # job-level reviewer
    assert by_type["capitalization"].ruling == "reject"


def test_all_rulings_within_vocabulary(store):
    ingest_job(store, corrections_record())
    ingest_job(store, review_record())
    assert {p.ruling for p in store.precedents()} <= RULING_VOCAB


# ---- idempotence -------------------------------------------------------


def test_reingest_same_records_creates_no_duplicates(store):
    first = ingest_archive(store, [corrections_record(), review_record()])
    assert first.ingested == 6
    assert first.duplicates == 0
    count_after_first = len(store.precedents())

    second = ingest_archive(store, [corrections_record(), review_record()])
    assert second.ingested == 0
    assert second.duplicates == 6
    assert len(store.precedents()) == count_after_first  # row count identical


def test_idempotent_within_a_single_batch(store):
    # The very same record twice in one call must still dedup.
    summary = ingest_archive(store, [corrections_record(), corrections_record()])
    assert summary.ingested == 3
    assert summary.duplicates == 3
    assert len(store.precedents()) == 3


# ---- resilience: bad records are skipped, not fatal --------------------


def test_malformed_record_in_batch_is_skipped_not_fatal(store):
    batch = [
        corrections_record(),
        {"kind": "totally_unknown", "stuff": [1, 2, 3]},  # unrecognized shape
        "not even a dict",  # wrong type entirely
        review_record(),
    ]
    summary = ingest_archive(store, batch)
    # Both good records fully ingested despite the two bad ones in between.
    assert summary.ingested == 6
    assert summary.skipped == 2
    assert len(store.precedents()) == 6


def test_finding_with_unknown_ruling_is_skipped(store):
    rec = {
        "kind": "review",
        "title": "Edge",
        "findings": [
            {"error_type": "spelling", "find_text": "teh", "disposition": "applied"},
            {"error_type": "mystery", "find_text": "x", "disposition": "levitate"},
        ],
    }
    summary = ingest_job(store, rec)
    assert summary.ingested == 1
    assert summary.skipped == 1
    assert [p.error_type for p in store.precedents()] == ["spelling"]


def test_unrecognized_record_returns_skip(store):
    summary = ingest_job(store, {"nothing": "useful"})
    assert summary.ingested == 0
    assert summary.skipped == 1
    assert store.precedents() == []


# ---- shape detection without an explicit kind --------------------------


def test_shape_inferred_from_keys_when_kind_absent(store):
    corrections = {
        "book": "NoKind",
        "resolutions": [
            {"error_type": "missing_comma", "find_text": "a b", "resolution": "accept"}
        ],
    }
    review = {
        "book": "NoKind2",
        "findings": [
            {"error_type": "spelling", "find_text": "c d", "disposition": "rejected"}
        ],
    }
    assert ingest_job(store, corrections).ingested == 1
    assert ingest_job(store, review).ingested == 1
    rulings = {p.error_type: p.ruling for p in store.precedents()}
    assert rulings == {"missing_comma": "accept", "spelling": "reject"}


# ---- directory walk + filename book fallback ---------------------------


def test_ingest_archive_walks_directory_and_uses_stem_as_book(tmp_path):
    arc = tmp_path / "archive"
    arc.mkdir()
    # A record with no book field -> file stem is the fallback book label.
    (arc / "bookless_corrections.json").write_text(
        json.dumps(
            {
                "kind": "corrections",
                "resolutions": [
                    {
                        "error_type": "spacing",
                        "find_text": "a  b",
                        "resolution": "accept",
                    }
                ],
            }
        )
    )
    (arc / "with_review.json").write_text(json.dumps(review_record()))
    # A corrupt file must not sink the batch.
    (arc / "broken.json").write_text("{ this is not json ")

    with MemoryStore.open(tmp_path / "mem.db", now=PINNED) as store:
        summary = ingest_archive(store, arc)
        assert summary.ingested == 4  # 1 + 3
        assert summary.skipped == 1  # the broken file
        precs = {p.error_type: p for p in store.precedents()}
        assert precs["spacing"].book == "bookless_corrections"  # file stem fallback
        assert precs["spelling"].book == "Redding"  # explicit title still wins


def test_ingest_archive_accepts_single_file_path(tmp_path):
    f = tmp_path / "one.json"
    f.write_text(json.dumps(corrections_record()))
    with MemoryStore.open(tmp_path / "mem.db", now=PINNED) as store:
        summary = ingest_archive(store, f)
        assert summary.ingested == 3


# ---- summary accumulation ----------------------------------------------


def test_summary_is_additive():
    s = IngestSummary()
    s._add(IngestSummary(ingested=2, duplicates=1, skipped=3))
    s._add(IngestSummary(ingested=1, duplicates=0, skipped=1))
    assert (s.ingested, s.duplicates, s.skipped) == (3, 1, 4)
