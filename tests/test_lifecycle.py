"""The finding lifecycle ledger (galley/lifecycle.py): stable ids, append-only
history, duplicate detection, and reconstruction from a finished run."""
from __future__ import annotations

import json

import pytest

from galley.lifecycle import (LIFECYCLE_STATES, Ledger, reconstruct_from_findings,
                              stable_key)


def test_stable_key_is_whitespace_insensitive_and_content_derived():
    a = stable_key("body-1", "teh cat", "spelling")
    b = stable_key("body-1", "teh  cat", "spelling")   # extra space
    c = stable_key("body-1", "teh cat", "homophone")   # different type
    assert a == b
    assert a != c


def test_record_is_append_only_and_tracks_current_state():
    led = Ledger()
    led.record("f-1", "detected", wave=1, by="ensemble")
    led.record("f-1", "verified", wave=1, by="verifier")
    led.record("f-1", "merged", wave=2, by="merge-desk")
    lc = led.history("f-1")
    assert [e.state for e in lc.events] == ["detected", "verified", "merged"]
    assert led.state_of("f-1") == "merged"


def test_recording_the_same_state_in_the_same_wave_is_idempotent():
    led = Ledger()
    led.record("f-1", "detected", wave=1)
    led.record("f-1", "detected", wave=1)
    assert len(led.history("f-1").events) == 1
    # a later wave with the same state is a real transition, not a dup
    led.record("f-1", "detected", wave=2)
    assert len(led.history("f-1").events) == 2


def test_unknown_state_is_refused():
    led = Ledger()
    with pytest.raises(ValueError, match="unknown lifecycle state"):
        led.record("f-1", "teleported")


def test_duplicates_reports_shared_content_keys():
    led = Ledger()
    k = stable_key("body-1", "teh cat", "spelling")
    led.record("f-1", "detected", key=k)
    led.record("f-9", "detected", key=k)
    led.record("f-2", "detected", key=stable_key("body-2", "x", "comma_splice"))
    dups = led.duplicates()
    assert dups == {k: ["f-1", "f-9"]}


def test_reconstruct_maps_statuses_to_terminal_states():
    env = {"findings": [
        {"finding_id": "f-1", "para_id": "b1", "original_text": "teh cat",
         "error_type": "spelling", "status": "validated"},
        {"finding_id": "f-2", "para_id": "b2", "original_text": "their there",
         "error_type": "homophone_confusion", "status": "query"},
        {"finding_id": "f-3", "para_id": "b3", "original_text": "x y",
         "error_type": "comma_splice", "status": "rejected_by_verifier"},
        {"finding_id": "f-4", "para_id": "b4", "original_text": "z",
         "error_type": "spelling", "status": "rejected_duplicate"},
    ]}
    led = reconstruct_from_findings(env, by="ensemble")
    assert led.by_state() == {"merged": 1, "queried": 1, "rejected": 1,
                              "dropped": 1}
    assert [e.state for e in led.history("f-1").events] == ["detected", "merged"]


def test_reconstruct_flags_force_query_as_queried():
    env = {"findings": [
        {"finding_id": "f-1", "para_id": "b1", "original_text": "that that",
         "error_type": "sweep_doubled_word", "status": "validated",
         "force_query": True},
    ]}
    led = reconstruct_from_findings(env)
    assert led.state_of("f-1") == "queried"


def test_ledger_round_trips_through_json():
    led = Ledger()
    led.record("f-1", "detected", key="k1", wave=1, by="d", note="n")
    led.record("f-1", "merged", wave=2)
    back = Ledger.from_json(json.loads(json.dumps(led.to_json())))
    assert back.state_of("f-1") == "merged"
    assert len(back.history("f-1").events) == 2


def test_reconstruct_content_duplicate_across_findings():
    env = {"findings": [
        {"finding_id": "f-1", "para_id": "b1", "original_text": "teh cat",
         "error_type": "spelling", "status": "validated"},
        {"finding_id": "f-2", "para_id": "b1", "original_text": "teh  cat",
         "error_type": "spelling", "status": "rejected_duplicate"},
    ]}
    led = reconstruct_from_findings(env)
    assert len(led.duplicates()) == 1


def test_all_states_are_known():
    assert "detected" in LIFECYCLE_STATES and "delivered" in LIFECYCLE_STATES
