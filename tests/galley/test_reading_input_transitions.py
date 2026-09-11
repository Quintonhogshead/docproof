"""Package-only changes retain original readers without weakening their inputs."""
import json
from zipfile import ZipFile

import docx
import pytest

from galley import verify
from tests.galley.test_verification_checkpoints import (
    MODEL, POLICY, Provider, complete, isolated_coverage, run,
)


def change_package(run, name="first"):
    # A harmless extra ZIP member changes bytes without changing either view.
    with ZipFile(verify.deliverable_docx(run), "a") as package:
        package.writestr(f"custom/{name}.txt", name)


def valid(run, provider=None, **overrides):
    options = {**POLICY, **overrides}
    model = options.pop("model", MODEL)
    return verify.validate_complete_pass(run, run, provider or Provider(), model, **options)


def preserve_evidence(run):
    files = list((run / verify._CHECKPOINT_DIR).rglob("*.json"))
    files += [run / "change_verify.json", run / "finished_walk.json"]
    return {p: p.read_bytes() for p in files}


def test_explicit_transition_preserves_original_proofs_and_response_windows(run):
    complete(run)
    evidence = preserve_evidence(run)
    before = verify.reading_input_snapshot(run)
    change_package(run)
    assert not valid(run)
    row = verify.record_reading_input_transition(run, before, reason="Reviewed package reconstruction")
    assert row["before"] == before
    assert row["after"]["document_sha256"] != before["document_sha256"]
    provider = Provider()
    assert valid(run, provider)
    assert provider.calls == []
    assert all(p.read_bytes() == data for p, data in evidence.items())


def test_transition_chain_requires_every_explicit_link(run):
    complete(run)
    before = verify.reading_input_snapshot(run)
    change_package(run)
    verify.record_reading_input_transition(run, before, reason="Reconstruction")
    before = verify.reading_input_snapshot(run)
    change_package(run, "comments")
    assert not valid(run)
    verify.record_reading_input_transition(run, before, reason="Comment-only change")
    assert valid(run)
    path = run / verify._READING_TRANSITIONS
    data = json.loads(path.read_text())
    data["transitions"].pop(0)
    path.write_text(json.dumps(data))
    assert not valid(run)


@pytest.mark.parametrize("change", ["word", "source", "edits"])
def test_transition_creation_refuses_changed_reading_inputs(run, monkeypatch, change):
    complete(run)
    before = verify.reading_input_snapshot(run)
    if change == "word":
        document = docx.Document(verify.deliverable_docx(run))
        document.paragraphs[0].text = "The dog wakes."
        document.save(verify.deliverable_docx(run))
    elif change == "source":
        original, accepted = verify.paragraph_views(run)
        original[next(iter(original))] = "Another original sentence."
        monkeypatch.setattr(verify, "paragraph_views", lambda _: (original, accepted))
    else:
        path = run / "findings.json"
        payload = json.loads(path.read_text())
        payload["findings"][0]["corrected_text"] = "a"
        path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="changed source"):
        verify.record_reading_input_transition(run, before, reason="Must fail")
    assert not (run / verify._READING_TRANSITIONS).exists()


@pytest.mark.parametrize("change", ["model", "context", "policy", "effort", "config", "edits"])
def test_transition_never_exempts_other_identity_fields(run, change):
    complete(run)
    before = verify.reading_input_snapshot(run)
    change_package(run)
    verify.record_reading_input_transition(run, before, reason="Package-only")
    options, provider = {}, Provider()
    if change == "effort":
        provider.effort = "low"
    elif change == "edits":
        path = run / "findings.json"
        payload = json.loads(path.read_text())
        payload["findings"][0]["original_text"] = "tge"
        path.write_text(json.dumps(payload))
    else:
        options[{"model": "model", "context": "context", "policy": "policy_id",
                 "config": "config_sha256"}[change]] = "changed"
    assert not valid(run, provider, **options)
    assert provider.calls == []


@pytest.mark.parametrize("damage", ["response", "proof", "transition", "before_digest"])
def test_transition_does_not_rescue_tampered_evidence(run, damage):
    complete(run)
    before = verify.reading_input_snapshot(run)
    change_package(run)
    verify.record_reading_input_transition(run, before, reason="Package-only")
    path = run / "finished_walk.json"
    artifact = json.loads(path.read_text())
    if damage == "response":
        proof = artifact["verification_provenance"]
        key = next(iter(proof["windows"]))
        path = run / verify._CHECKPOINT_DIR / proof["scope"] / proof["invocation_id"] / (key + ".json")
        data = json.loads(path.read_text())
        data["identity_sha256"] = "0" * 64
    elif damage == "proof":
        data = artifact
        data["verification_provenance"]["identity"]["document_sha256"] = "0" * 64
    else:
        path = run / verify._READING_TRANSITIONS
        data = json.loads(path.read_text())
        row = data["transitions"][0]
        row["before"]["edits_sha256"] = "0" * 64
        if damage == "before_digest":
            row["transition_sha256"] = verify._digest({k: v for k, v in row.items() if k != "transition_sha256"})
    path.write_text(json.dumps(data))
    assert not valid(run)


def test_engine_preserves_both_independent_readers_across_transition(tmp_path, monkeypatch):
    from galley.engine_phases import EnginePhases
    from tests.test_galley_engine_phases import _driver, _complete_verification
    phases = EnginePhases(_driver(tmp_path), lambda spec: None)
    run = _complete_verification(phases, monkeypatch)
    before = verify.reading_input_snapshot(run)
    change_package(run)
    assert not phases._coverage(run, require_full_passes=True)
    verify.record_reading_input_transition(run, before, reason="Equivalent writer reconstruction")
    assert phases._coverage(run, require_full_passes=True)


@pytest.mark.parametrize("damage", [None, "missing_transition", "findings", "output",
                                     "validation", "audit", "settle", "other_command"])
def test_completed_command_transition_is_only_for_unchanged_verified_outputs(
        tmp_path, monkeypatch, damage):
    from types import SimpleNamespace
    from galley.engine_phases import EnginePhases, EnginePhaseError
    from tests.test_galley_engine_phases import _driver, _complete_verification
    executed = []
    def execute(spec):
        executed.append(spec)
        return SimpleNamespace(returncode=0, limit=None)
    phases = EnginePhases(_driver(tmp_path), execute)
    run = _complete_verification(phases, monkeypatch)
    phase = damage if damage in {"audit", "settle"} else "verify"
    name = "verify-recorded" if damage != "other_command" else "another-command"
    args = [phase, str(run)]
    outputs = [run / "change_verify.json", run / "finished_walk.json"]
    check = lambda: phases._coverage(run, require_full_passes=True)
    phases._command(phase, name, args, outputs, validate=check)
    receipt = phases.directory / f"{name}.json"
    original_receipt = receipt.read_bytes()
    original_outputs = {p: p.read_bytes() for p in outputs}
    before = verify.reading_input_snapshot(run)
    change_package(run)
    if damage != "missing_transition":
        verify.record_reading_input_transition(run, before, reason="Equivalent reconstruction")
    if damage == "findings":
        path = run / "findings.json"
        payload = json.loads(path.read_text())
        payload["findings"].append({"para_id": "body-0001", "original_text": "Book",
                                    "corrected_text": "book", "status": "validated"})
        path.write_text(json.dumps(payload))
    elif damage == "output":
        outputs[0].write_bytes(outputs[0].read_bytes() + b"\n")
    elif damage == "validation":
        check = lambda: False
    if damage is None:
        phases._command(phase, name, args, outputs, validate=check)
        assert all(p.read_bytes() == data for p, data in original_outputs.items())
    else:
        with pytest.raises(EnginePhaseError, match="completed output has changed"):
            phases._command(phase, name, args, outputs, validate=check)
    assert len(executed) == 1
    assert receipt.read_bytes() == original_receipt


@pytest.mark.parametrize("full_proof", [True, False])
def test_native_comment_cleanup_and_finalization_keep_truthful_engine_coverage(
        tmp_path, monkeypatch, full_proof):
    from types import SimpleNamespace
    from docproof.config import load_config
    from galley.engine_phases import EnginePhases
    from galley.settle import Settler, SettleOptions
    from tests.galley.test_native_comment_reconcile import _prepared
    from tests.galley.test_settle import _replay_config
    run, source, _key = _prepared(tmp_path)
    complete(run)
    if not full_proof:
        # An already bound partial/delta artifact need not claim a full pass.
        for name in ("change_verify.json", "finished_walk.json"):
            path = run / name
            payload = json.loads(path.read_text())
            payload.pop("verification_provenance")
            path.write_text(json.dumps(payload))
    original_artifacts = {name: (run / name).read_bytes()
                          for name in ("change_verify.json", "finished_walk.json")}
    original_views = verify.paragraph_views(run)
    original_inputs = verify.reading_input_snapshot(run)
    phases = EnginePhases(SimpleNamespace(workspace=tmp_path), lambda spec: pytest.fail("Unexpected command"))
    assert phases._coverage(run)
    provider = Provider()
    settler = Settler(run, cfg=load_config(_replay_config(tmp_path)), manuscript=source,
                     error_dir="config/error_types", provider=provider,
                     options=SettleOptions(engine="provider", model=MODEL, verify_delta=False))
    result = settler.run()
    assert result.usage.api_calls == 0 and provider.calls == []
    assert verify.paragraph_views(run) == original_views
    assert phases._coverage(run)
    transition = next((run / "settle" / "comment-transitions").iterdir())
    for name, original in original_artifacts.items():
        assert (transition / "before" / name).read_bytes() == original
    # Finalization projects disposition metadata; it does not constitute the
    # original reader's exact request payload or a fresh independently read pass.
    assert verify.reading_input_snapshot(run)["edits_sha256"] != original_inputs["edits_sha256"]
    assert not valid(run)
