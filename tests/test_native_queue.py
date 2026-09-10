import importlib
import json

import pytest

from app.watch import native_queue as queue


def _book(project_id="p1", folder_id=None):
    return {
        "project_id": project_id,
        "title": f"Book {project_id}",
        "author": "Quinton Johnson",
        "surname": "Johnson",
        "folder_id": folder_id or f"folder-{project_id}",
        "source_id": f"source-{project_id}",
        "source_version": 10,
    }


def _event(event_id, project_id="p1", marker=None, text="Fix this"):
    return {
        "event_id": event_id,
        "marker": marker or f"marker-{event_id}",
        "submitted_at": 0,
        "urls": [],
        "text": text,
        "raw": {"event_id": event_id, "text": text},
        "project_id": project_id,
        "reason": "",
    }


def _registered(home, *books):
    for book in books or (_book(),):
        queue.register_book(home, book)


def _fresh_capture(home, events, *, first_at=0, fresh_at=None):
    queue.capture(home, "form-1", events, now=first_at)
    queue.capture(home, "form-1", events,
                  now=queue.QUIET_SECONDS - 100 if fresh_at is None else fresh_at)


def test_seven_events_are_preserved_in_one_claimed_batch(tmp_path):
    _registered(tmp_path)
    events = [_event(f"event-{index}") for index in range(7)]
    _fresh_capture(tmp_path, events)

    assert len(queue.status(tmp_path, now=queue.QUIET_SECONDS)["events"]) == 7
    batch = queue.claim(tmp_path, now=queue.QUIET_SECONDS)
    assert batch is not None
    assert {event["raw"]["event_id"] for event in batch["events"]} == {f"event-{index}" for index in range(7)}


def test_repeated_poll_does_not_reset_ready_time_and_boundary_is_exact(tmp_path):
    _registered(tmp_path)
    event = _event("event-1")
    queue.capture(tmp_path, "form-1", [event], now=0)
    queue.capture(tmp_path, "form-1", [event], now=queue.QUIET_SECONDS - 100)

    assert queue.claim(tmp_path, now=queue.QUIET_SECONDS - 1) is None
    batch = queue.claim(tmp_path, now=queue.QUIET_SECONDS)
    assert batch is not None and [event["raw"]["event_id"] for event in batch["events"]] == ["event-1"]


def test_new_event_resets_only_its_books_quiet_period(tmp_path):
    _registered(tmp_path)
    first = _event("event-1")
    second = _event("event-2")
    queue.capture(tmp_path, "form-1", [first], now=0)
    queue.capture(tmp_path, "form-1", [first], now=queue.QUIET_SECONDS - 100)
    queue.capture(tmp_path, "form-1", [second], now=queue.QUIET_SECONDS - 100)

    assert queue.claim(tmp_path, now=queue.QUIET_SECONDS) is None
    assert queue.status(tmp_path, now=queue.QUIET_SECONDS)["events"][0]["ready_at"] == queue.QUIET_SECONDS * 2 - 100


def test_restart_reopens_the_same_sqlite_queue(tmp_path):
    _registered(tmp_path)
    _fresh_capture(tmp_path, [_event("event-1")])

    reloaded = importlib.reload(queue)
    batch = reloaded.claim(tmp_path, now=reloaded.QUIET_SECONDS)
    assert batch is not None and [event["raw"]["event_id"] for event in batch["events"]] == ["event-1"]


def test_two_books_claim_independently(tmp_path):
    _registered(tmp_path, _book("p1"), _book("p2"))
    _fresh_capture(tmp_path, [_event("event-1", "p1"), _event("event-2", "p2")])

    first = queue.claim(tmp_path, now=queue.QUIET_SECONDS)
    second = queue.claim(tmp_path, now=queue.QUIET_SECONDS)
    assert first["project_id"] == "p1"
    assert second["project_id"] == "p2"


def test_arrival_during_claimed_or_running_batch_waits(tmp_path):
    _registered(tmp_path)
    _fresh_capture(tmp_path, [_event("event-1")])
    batch = queue.claim(tmp_path, now=queue.QUIET_SECONDS)
    queue.capture(tmp_path, "form-1", [_event("event-2")], now=queue.QUIET_SECONDS)

    assert queue.claim(tmp_path, now=queue.QUIET_SECONDS) is None
    queue.set_batch(tmp_path, batch["batch_id"], "running")
    assert queue.claim(tmp_path, now=queue.QUIET_SECONDS + queue.QUIET_SECONDS) is None


@pytest.mark.parametrize("state", ["held", "local_complete"])
def test_held_or_local_complete_batch_blocks_later_same_book_events(tmp_path, state):
    _registered(tmp_path)
    _fresh_capture(tmp_path, [_event("event-1")])
    batch = queue.claim(tmp_path, now=queue.QUIET_SECONDS)
    queue.set_batch(tmp_path, batch["batch_id"], state)
    queue.capture(tmp_path, "form-1", [_event("event-2")], now=queue.QUIET_SECONDS)

    assert queue.claim(tmp_path, now=queue.QUIET_SECONDS * 2) is None


def test_duplicate_event_id_is_captured_once(tmp_path):
    _registered(tmp_path)
    event = _event("event-1")
    queue.capture(tmp_path, "form-1", [event], now=0)
    queue.capture(tmp_path, "form-1", [event], now=1)

    assert len(queue.status(tmp_path, now=1)["events"]) == 1


def test_conflicting_payload_is_preserved_and_blocks_the_book(tmp_path):
    _registered(tmp_path)
    queue.capture(tmp_path, "form-1", [_event("event-1", text="first")], now=0)
    queue.capture(tmp_path, "form-1", [_event("event-1", text="second")], now=1)

    with queue.transaction(tmp_path) as db:
        event = db.execute("SELECT payload,reason FROM events").fetchone()
        conflicts = db.execute("SELECT COUNT(*) AS count FROM conflicts").fetchone()["count"]
        blocked = db.execute("SELECT blocked FROM books WHERE project_id='p1'").fetchone()["blocked"]
    assert json.loads(event["payload"])["text"] == "first"
    assert "changed" in event["reason"]
    assert conflicts == 1 and "changed" in blocked
    assert queue.claim(tmp_path, now=queue.QUIET_SECONDS) is None


def test_folder_registry_requires_unique_folder_ids(tmp_path):
    _registered(tmp_path, _book("p1", "shared"))
    with pytest.raises(queue.QueueError, match="already belongs"):
        queue.register_book(tmp_path, _book("p2", "shared"))


@pytest.mark.parametrize(
    "job,marker",
    [
        ({"status": "verified", "uploaded": {"pdf": "ok"},
          "artifact_hashes": {"pdf": "hash"}}, "delivered-marker"),
        ({"status": "technical_block"}, "held-marker"),
    ],
)
def test_imported_delivered_history_is_skipped_and_unfinished_history_holds(tmp_path, job, marker):
    _registered(tmp_path)
    queue.import_history(tmp_path, [{"record_id": "p1", "submission_marker": marker, **job}])
    event = _event("event-1", marker=marker)
    _fresh_capture(tmp_path, [event])

    assert queue.claim(tmp_path, now=queue.QUIET_SECONDS) is None
