"""The resumable run state machine (galley/state_machine.py) and the fuller
certify checks it and the lifecycle ledger feed."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from galley.manifest import certify_run
from galley.state_machine import (RUN_STATES, ArtifactHash, RunStateMachine,
                                  StateError)


# --- the state machine -------------------------------------------------------

def test_forward_only_and_current():
    m = RunStateMachine()
    m.advance("intake")
    m.advance("profiled")
    m.advance("mechanical_complete")
    assert m.current == "mechanical_complete"
    assert m.reached("profiled") is True
    assert m.reached("certified") is False


def test_backward_move_is_refused():
    m = RunStateMachine()
    m.advance("mechanical_complete")
    with pytest.raises(StateError, match="backward"):
        m.advance("profiled")


def test_reentering_the_same_state_is_allowed():
    m = RunStateMachine()
    m.advance("profiled")
    m.advance("profiled")            # idempotent stage re-run
    assert m.current == "profiled"


def test_unknown_state_is_refused():
    m = RunStateMachine()
    with pytest.raises(StateError, match="unknown run state"):
        m.advance("teleport")


def test_verify_resume_detects_a_changed_source():
    m = RunStateMachine()
    m.advance("mechanical_complete", source_sha256="abc", config_sha256="cfg")
    assert m.verify_resume(source_sha256="abc", config_sha256="cfg") == []
    bad = m.verify_resume(source_sha256="zzz", config_sha256="cfg")
    assert bad and "source changed" in bad[0]


def test_verify_resume_detects_a_changed_artifact():
    m = RunStateMachine()
    m.advance("profiled",
              artifacts=[ArtifactHash(path="p.json", sha256="h1")])
    changed = m.verify_resume(artifact_hasher=lambda p: "DIFFERENT")
    assert changed == ["artifact changed: p.json"]
    missing = m.verify_resume(
        artifact_hasher=lambda p: (_ for _ in ()).throw(OSError()))
    assert missing == ["artifact missing: p.json"]


def test_verify_resume_fails_when_no_hash_was_stamped():
    """A resume that supplies a hash no stage ever stamped has nothing to
    prove the inputs against — a failure to re-advance, not a vacuous pass."""
    m = RunStateMachine()
    m.advance("mechanical_complete")            # advanced without --source
    out = m.verify_resume(source_sha256="abc")
    assert out == ["no source hash was stamped at 'mechanical_complete'; "
                   "re-advance with --source/--config"]
    both = m.verify_resume(source_sha256="abc", config_sha256="cfg")
    assert len(both) == 2 and any("no config hash" in x for x in both)


@pytest.mark.parametrize("missing", [False, True])
def test_verify_resume_checks_artifacts_from_earlier_stages(tmp_path, missing):
    from galley.state_machine import hash_artifact

    artifact = tmp_path / "profile.json"
    artifact.write_text('{"genre": "fiction"}')
    m = RunStateMachine()
    m.advance("profiled", artifacts=[
        ArtifactHash(path=str(artifact), sha256=hash_artifact(artifact))])
    m.advance("plan_approved")
    if missing:
        artifact.unlink()
    else:
        artifact.write_text('{"genre": "nonfiction"}')
    assert m.verify_resume(artifact_hasher=hash_artifact) == [
        f"artifact {'missing' if missing else 'changed'}: {artifact}"]


def test_verify_resume_uses_the_latest_hash_for_replaced_artifacts():
    m = RunStateMachine()
    m.advance("profiled", artifacts=[ArtifactHash(path="p.json", sha256="old")])
    m.advance("plan_approved", artifacts=[ArtifactHash(path="p.json", sha256="new")])
    m.advance("audited")
    checked = []

    def hasher(path):
        checked.append(path)
        return "new"

    assert m.verify_resume(artifact_hasher=hasher) == []
    assert checked == ["p.json"]


def test_verify_resume_fails_when_resume_supplies_no_hash_against_a_stamp():
    m = RunStateMachine()
    m.advance("profiled", source_sha256="abc", config_sha256="cfg")
    out = m.verify_resume(source_sha256="abc")            # no config hash
    assert len(out) == 1
    assert "config hash was stamped at 'profiled'" in out[0]
    assert "resume supplied none" in out[0]


def test_verify_resume_compares_every_stamped_record_not_just_the_last():
    m = RunStateMachine()
    m.advance("intake", source_sha256="abc", config_sha256="cfg")
    m.advance("audited")                        # later stage stamped nothing
    assert m.verify_resume(source_sha256="abc", config_sha256="cfg") == []
    bad = m.verify_resume(source_sha256="zzz", config_sha256="cfg")
    assert bad == ["source changed since 'intake': now zzz…, recorded abc…"]


def test_verify_resume_reports_each_distinct_stamped_hash_once():
    m = RunStateMachine()
    m.advance("intake", source_sha256="abc")
    m.advance("profiled", source_sha256="abc")
    m.advance("plan_approved", source_sha256="def")
    bad = m.verify_resume(source_sha256="zzz")
    assert [b.split(":")[0] for b in bad] == [
        "source changed since 'intake'", "source changed since 'plan_approved'"]


def test_state_machine_round_trips(tmp_path):
    m = RunStateMachine()
    m.advance("intake", source_sha256="s")
    m.advance("audited", source_sha256="s")
    path = tmp_path / "state.json"
    m.save(path)
    back = RunStateMachine.load(path)
    assert back.current == "audited"


def test_run_states_are_ordered():
    assert RUN_STATES.index("mechanical_complete") < RUN_STATES.index("audited")


# --- fuller certify ----------------------------------------------------------

def _run(tmp_path, envelope, state=None) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    (run / "findings.json").write_text(json.dumps(envelope))
    if state is not None:
        state.save(run / "state.json")
    return run


def test_certify_fails_on_a_duplicate_merged_edit(tmp_path):
    env = {"findings": [
        {"finding_id": "f-1", "para_id": "b1", "original_text": "teh cat",
         "error_type": "spelling", "status": "validated"},
        {"finding_id": "f-2", "para_id": "b1", "original_text": "teh cat",
         "error_type": "spelling", "status": "validated"},
    ], "cost": {"total_usd": 0.0}}
    cert = certify_run(_run(tmp_path, env))
    dup = [c for c in cert.checks if c.name == "no duplicate merged edits"]
    assert dup and dup[0].status == "fail"


def test_certify_passes_when_a_duplicate_was_deduped(tmp_path):
    env = {"findings": [
        {"finding_id": "f-1", "para_id": "b1", "original_text": "teh cat",
         "error_type": "spelling", "status": "validated"},
        {"finding_id": "f-2", "para_id": "b1", "original_text": "teh cat",
         "error_type": "spelling", "status": "rejected_duplicate"},
    ], "cost": {"total_usd": 0.0}}
    cert = certify_run(_run(tmp_path, env))
    dup = [c for c in cert.checks if c.name == "no duplicate merged edits"]
    assert dup and dup[0].status == "pass"


def test_certify_fails_on_conflicting_edits_at_one_anchor(tmp_path):
    env = {"findings": [
        {"finding_id": "f-1", "para_id": "b1", "original_text": "the cat",
         "occurrence": 1, "corrected_text": "the cats", "error_type": "x"},
        {"finding_id": "f-2", "para_id": "b1", "original_text": "the cat",
         "occurrence": 1, "corrected_text": "the CAT", "error_type": "y"},
    ], "cost": {"total_usd": 0.0}}
    cert = certify_run(_run(tmp_path, env))
    coll = [c for c in cert.checks if c.name == "no insertion collisions"]
    assert coll and coll[0].status == "fail"


def test_certify_two_author_needs_the_deliverable_to_confirm(tmp_path):
    """Both lanes present but no .docx to read the revision authors from is a
    loud skip, never a pass — the pass/fail cases live in tests/test_manifest.py
    where a deliverable with tracked changes is built."""
    env = {"findings": [
        {"finding_id": "f-1", "para_id": "b1", "error_type": "spelling",
         "lane": "mechanical", "corrected_text": "x"},
        {"finding_id": "f-2", "para_id": "b2", "error_type": "copyedit",
         "lane": "copyedit", "corrected_text": "y"},
    ], "cost": {"total_usd": 0.0}}
    cert = certify_run(_run(tmp_path, env))
    ta = [c for c in cert.checks if c.name == "two-author attribution"]
    assert ta and ta[0].status == "skip"
    assert "no manuscript .docx" in ta[0].detail


def test_certify_run_state_requires_audited(tmp_path):
    m = RunStateMachine()
    m.advance("mechanical_complete")
    env = {"findings": [], "cost": {"total_usd": 0.0}}
    cert = certify_run(_run(tmp_path, env, state=m))
    rs = [c for c in cert.checks if c.name == "run state"]
    assert rs and rs[0].status == "fail"

    m.advance("audited")
    m.save(tmp_path / "run" / "state.json")
    cert2 = certify_run(tmp_path / "run")
    rs2 = [c for c in cert2.checks if c.name == "run state"]
    assert rs2 and rs2[0].status == "pass"
