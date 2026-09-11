"""Code-owned phases preserve coverage, residuals, and restart identity."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from galley.engine_phases import EnginePhaseError, EnginePhases, review_action


def _driver(tmp_path):
    import docx
    import shutil

    (tmp_path / "runs").mkdir(exist_ok=True)
    (tmp_path / "runs" / "mech.yaml").write_text("api: {model: claude-opus-5}\n")
    book = tmp_path / "source.docx"
    document = docx.Document()
    document.add_paragraph("The book was quiet.")
    document.save(book)
    run = tmp_path / "runs" / "final"
    run.mkdir(exist_ok=True)
    shutil.copyfile(book, run / "book - Atmosphere Press Proofreader.docx")
    (run / "findings.json").write_text(json.dumps({"source": str(book), "findings": []}))
    return SimpleNamespace(
        workspace=tmp_path, book=book, budget_usd=10, review_rounds=2,
        review_calls=40, _engine_env={}, _final_run=lambda: run,
        _resource_env=lambda phase, env: dict(env), timeout_for=lambda phase: 60)


def _complete_verification(phases, monkeypatch):
    from docproof.__main__ import main
    from galley import verify
    from galley.engine_phases import PASS_IDS, POLICY_ID, READER_MODELS
    from tests.galley.test_verification_checkpoints import Provider

    monkeypatch.setattr(verify, "UNREAD", [])
    monkeypatch.setattr(verify, "UNREAD_BATCHES", [])
    monkeypatch.setattr(verify, "_LOSSES", [])
    # Validate the same provider identity that issued the fake structured reads.
    monkeypatch.setattr("docproof.providers.subagent.SubagentProvider",
                        lambda *args, **kwargs: Provider())
    run = phases.driver._final_run()
    for pass_id, model in READER_MODELS.items():
        output = run if pass_id == "primary" else run / "verification" / pass_id
        assert main([
            "galley", "verify", str(run), "--out", str(output),
            "--config", str(phases._config()), "--engine", "subagent", "--model", model,
            "--verification-pass", pass_id, "--verification-policy", POLICY_ID,
            "--required-verification-passes", ",".join(PASS_IDS),
            "--context", str(phases._context(pass_id)),
        ]) == 0
    return run


@pytest.mark.parametrize("damage", ["missing_pass", "incomplete", "policy", "model", "config"])
def test_full_coverage_requires_both_complete_reads_under_the_current_policy(
        tmp_path, monkeypatch, damage):
    phases = EnginePhases(_driver(tmp_path), lambda spec: None)
    run = _complete_verification(phases, monkeypatch)
    assert phases._coverage(run, require_full_passes=True)
    path = run / "verification" / "type-compare" / "finished_walk.json"
    if damage == "missing_pass":
        path.unlink()
    else:
        payload = json.loads(path.read_text())
        if damage == "incomplete":
            payload["verification_provenance"]["complete"] = False
        elif damage == "policy":
            payload["verification_provenance"]["identity"]["policy"]["pass_id"] = "primary"
        elif damage == "model":
            payload["model"] = "some-other-reviewer"
            payload["verification_provenance"]["identity"]["model"] = "some-other-reviewer"
        elif damage == "config":
            phases._config().write_text("api: {model: changed-model}\n")
        path.write_text(json.dumps(payload))
    assert not phases._coverage(run, require_full_passes=True)


def test_delta_coverage_counts_skipped_display_paragraphs_in_the_accepted_book(tmp_path, monkeypatch):
    import docx
    import shutil
    from docproof.config import load_config
    from galley.settle import Settler, _prepare_source
    from galley.verify import VerifyRunResult

    driver = _driver(tmp_path)
    document = docx.Document()
    document.add_paragraph("The Book Title", style="Title")
    document.add_paragraph("The book was quiet.")
    document.save(driver.book)
    shutil.copyfile(driver.book, driver._final_run() / "book - Atmosphere Press Proofreader.docx")
    phases = EnginePhases(driver, lambda spec: None)
    run = _complete_verification(phases, monkeypatch)
    cfg = load_config(str(phases._config()))
    settler = Settler(run, cfg=cfg, manuscript=driver.book, error_dir="config/error_types")
    settler._source = _prepare_source(cfg, driver.book, "config/error_types")[0]
    assert list(settler._source.values()) == ["The book was quiet."]
    # A real delta merge over the prose must preserve the whole accepted
    # manuscript's coverage count, including the unedited Title paragraph.
    settler._merge_coverage(VerifyRunResult([], [], True, True), list(settler._source))
    assert json.loads((run / "finished_walk.json").read_text())["paragraphs"] == 2
    assert phases._coverage(run)


@pytest.mark.parametrize("coverage,ids,prior,rounds,affected,expected", [
    (False, set(), set(), 0, 0, "blocked"),
    (False, {"r-1"}, set(), 1, 1, "blocked"),
    (True, set(), set(), 0, 0, "final_review"),
    (True, {"r-1"}, set(), 0, 1, "local_repair"),
    (True, {"r-1"}, set(), 0, 20, "broad_repair"),
    (True, {"r-1"}, {"r-1"}, 1, 1, "final_review_with_residuals"),
    (True, {"r-2"}, {"r-1"}, 2, 1, "final_review_with_residuals"),
])
def test_review_action_never_calls_small_or_exhausted_residuals_clean(
        coverage, ids, prior, rounds, affected, expected):
    assert review_action(
        coverage_complete=coverage, open_ids=ids, prior_ids=prior,
        rounds=rounds, max_rounds=2, affected_paragraphs=affected,
        reviewed_paragraphs=100) == expected


def test_verification_preserves_both_complete_named_reads(tmp_path, monkeypatch):
    phases = EnginePhases(_driver(tmp_path), lambda spec: None)
    calls = []

    def command(*args, **kwargs):
        calls.append((args, kwargs))
        for output in args[3]:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text('{"fixture": true}')

    monkeypatch.setattr(phases, "_command", command)
    monkeypatch.setattr(phases, "_coverage", lambda *args, **kwargs: True)
    phases.verify()
    assert len(calls) == 2
    for (phase, name, args, outputs), kwargs in calls:
        assert phase == "verify"
        assert args[args.index("--required-verification-passes") + 1] == "primary,type-compare"
        assert args[args.index("--verification-policy") + 1] == "mechanical-verification-v1"
        assert "--paragraphs" not in args
        assert len(outputs) == 2
        assert kwargs["acceptable"] == (0, 1)
    assert calls[0][0][2][calls[0][0][2].index("--verification-pass") + 1] == "primary"
    assert calls[1][0][2][calls[1][0][2].index("--verification-pass") + 1] == "type-compare"
    assert "Independent slow comparison" in (
        phases.directory / "type-compare-context.txt").read_text(encoding="utf-8")
    run = phases.driver._final_run()
    assert phases._initial_coverage_saved(run)
    # Current primary artifacts may change after repairs; the archived full
    # reads must remain intact and bound to the original receipt.
    (run / "finished_walk.json").write_text('{"delta": true}')
    assert phases._initial_coverage_saved(run)
    receipt = json.loads((phases.directory / "initial-coverage.json").read_text())
    archived = phases.directory / next(iter(receipt["artifacts"]))
    archived.write_text('{"changed": true}')
    assert not phases._initial_coverage_saved(run)


def test_incomplete_read_receipt_can_resume_and_complete_the_same_operation(tmp_path):
    driver = _driver(tmp_path)
    output = driver._final_run() / "finished_walk.json"
    calls = []

    def execute(spec):
        calls.append(spec)
        output.write_text(json.dumps({"complete": len(calls) == 2}))
        return SimpleNamespace(returncode=1 if len(calls) == 1 else 0, limit="", tail="")

    phases = EnginePhases(driver, execute)
    kwargs = {"acceptable": (0, 1),
              "validate": lambda: json.loads(output.read_text())["complete"]}
    with pytest.raises(EnginePhaseError):
        phases._command("verify", "recover-read", ["verify"], [output], **kwargs)
    receipt_path = phases.directory / "recover-read.json"
    assert json.loads(receipt_path.read_text())["status"] == "incomplete"
    resumed = EnginePhases(driver, execute)
    resumed._command("verify", "recover-read", ["verify"], [output], **kwargs)
    assert json.loads(receipt_path.read_text())["status"] == "completed"
    assert len(calls) == 2
    resumed._command("verify", "recover-read", ["verify"], [output], **kwargs)
    assert len(calls) == 2


def test_completed_command_is_reused_only_while_outputs_match(tmp_path):
    output = tmp_path / "runs" / "evidence.json"
    calls = []

    def execute(spec):
        calls.append(spec)
        output.write_text('{"ran": true}')
        return SimpleNamespace(returncode=0, limit="", tail="")

    phases = EnginePhases(_driver(tmp_path), execute)
    phases._command("audit", "audit-check", ["audit"], [output])
    phases._command("audit", "audit-check", ["audit"], [output])
    assert len(calls) == 1
    output.write_text('{"ran": false}')
    with pytest.raises(EnginePhaseError):
        phases._command("audit", "audit-check", ["audit"], [output])


def test_command_failure_or_missing_output_cannot_create_completion_receipt(tmp_path):
    output = tmp_path / "runs" / "evidence.json"
    result = SimpleNamespace(returncode=0, limit="timeout", tail="stopped")
    phases = EnginePhases(_driver(tmp_path), lambda spec: result)
    with pytest.raises(EnginePhaseError):
        phases._command("audit", "failed-audit", ["audit"], [output])
    receipt = json.loads((phases.directory / "failed-audit.json").read_text())
    assert receipt["status"] != "completed"
    result.limit = ""
    with pytest.raises(EnginePhaseError):
        phases._command("audit", "missing-audit", ["audit"], [output])
    receipt = json.loads((phases.directory / "missing-audit.json").read_text())
    assert receipt["status"] != "completed"


@pytest.mark.parametrize("changed_input", ["context", "findings"])
def test_completed_verification_cannot_be_reused_after_its_evidence_changes(
        tmp_path, changed_input):
    driver = _driver(tmp_path)
    run = driver._final_run()
    context = tmp_path / "voice-notes.md"
    context.write_text("The speaker uses deliberate fragments.")
    output = run / "finished_walk.json"

    def execute(spec):
        output.write_text('{"ran": true}')
        return SimpleNamespace(returncode=0, limit="", tail="")

    phases = EnginePhases(driver, execute)
    args = ["verify", str(run), "--context", str(context)]
    phases._command("verify", "verify-evidence", args, [output])
    if changed_input == "context":
        context.write_text("The speaker uses deliberate fragments and invented names.")
    else:
        (run / "findings.json").write_text(json.dumps({
            "source": str(driver.book), "findings": [{"finding_id": "new-evidence"}]}))
    with pytest.raises(EnginePhaseError):
        phases._command("verify", "verify-evidence", args, [output])


def test_missing_secondary_read_cannot_advance_settlement(tmp_path, monkeypatch):
    driver = _driver(tmp_path)
    run = driver._final_run()
    # Old, incomplete primary files must not stand in for the required two reads.
    for name in ("change_verify.json", "finished_walk.json", "settlement.json"):
        (run / name).write_text('{"ran": false}')
    phases = EnginePhases(driver, lambda spec: pytest.fail("must stop before model work"))
    advanced = []
    monkeypatch.setattr(phases, "_advance", advanced.append)
    monkeypatch.setattr("galley.settle.open_items", lambda run: [])
    monkeypatch.setattr("galley.verify.accepted_text", lambda run: {"body-0000": "Text."})
    monkeypatch.setattr("galley.verify.build_fingerprints", lambda run: {"build": "current"})
    with pytest.raises(EnginePhaseError):
        phases.settle()
    assert advanced == []


def test_exhausted_round_cap_cannot_buy_an_extra_round_for_missing_settlement(tmp_path, monkeypatch):
    from galley.manifest import sha256_file

    driver = _driver(tmp_path)
    run = driver._final_run()
    phases = EnginePhases(driver, lambda spec: pytest.fail("round cap must prevent execution"))
    identity = {"source": sha256_file(driver.book), "run": str(run.resolve()),
                "config": sha256_file(phases._config())}
    (phases.directory / "review-loop.json").write_text(json.dumps({
        "schema_version": 1, "identity": identity, "rounds": 2,
        "prior_ids": ["r-old"], "history": []}))
    monkeypatch.setattr(phases, "_coverage", lambda *args, **kwargs: True)
    monkeypatch.setattr(phases, "_initial_coverage_saved", lambda run: True)
    monkeypatch.setattr(phases, "_advance", lambda state: pytest.fail("missing evidence cannot advance"))
    monkeypatch.setattr("galley.settle.open_items", lambda run: [
        SimpleNamespace(id="r-new", para_id="body-0000")])
    with pytest.raises(EnginePhaseError):
        phases.settle()


def test_unchanged_residuals_stop_repair_and_remain_visible_for_final_review(tmp_path, monkeypatch):
    driver = _driver(tmp_path)
    run = driver._final_run()
    phases = EnginePhases(driver, lambda spec: None)
    commands, advanced = [], []

    def command(phase, name, arguments, outputs, **kwargs):
        commands.append(name)
        for output in outputs:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text('{"fixture": true}')

    monkeypatch.setattr(phases, "_command", command)
    monkeypatch.setattr(phases, "_coverage", lambda *args, **kwargs: True)
    monkeypatch.setattr(phases, "_advance", advanced.append)
    monkeypatch.setattr("galley.settle.open_items", lambda run: [
        SimpleNamespace(id="r-persistent", para_id="body-0000")])
    monkeypatch.setattr("galley.verify.accepted_text", lambda run: {
        f"body-{i:04d}": "Paragraph." for i in range(100)})
    phases.verify()
    commands.clear()
    phases.settle()
    state = json.loads((phases.directory / "review-loop.json").read_text())
    assert commands == ["settle-1"]
    assert state["rounds"] == 1
    assert state["history"][-1]["action"] == "final_review_with_residuals"
    assert state["history"][-1]["open_ids"] == ["r-persistent"]
    assert advanced == ["settled"]


def test_interrupted_broad_reread_finishes_before_resumed_settlement_can_advance(tmp_path, monkeypatch):
    driver = _driver(tmp_path)
    commands, advanced, events = [], [], []
    interrupted = True
    state_path = driver.workspace / "runs" / "driver" / "engine" / "review-loop.json"

    def command(phase, name, arguments, outputs, **kwargs):
        commands.append(name)
        for output in outputs:
            output.write_text('{"fixture": true}')

    def reread(*, cycle=0):
        nonlocal interrupted
        events.append(("verify", cycle))
        assert json.loads(state_path.read_text())["pending_verify_cycle"] == cycle
        if interrupted:
            interrupted = False
            raise EnginePhaseError("Primary completed; independent second read interrupted")

    def open_items(run):
        events.append(("open_items", None))
        return [SimpleNamespace(id="r-persistent", para_id="body-0000")]

    def coordinator():
        phases = EnginePhases(driver, lambda spec: None)
        monkeypatch.setattr(phases, "_command", command)
        monkeypatch.setattr(phases, "_coverage", lambda *args, **kwargs: True)
        monkeypatch.setattr(phases, "_initial_coverage_saved", lambda run: True)
        monkeypatch.setattr(phases, "_advance", advanced.append)
        monkeypatch.setattr(phases, "verify", reread)
        return phases

    monkeypatch.setattr("galley.settle.open_items", open_items)
    with pytest.raises(EnginePhaseError, match="second read interrupted"):
        coordinator().settle()
    state = json.loads(state_path.read_text())
    assert state["rounds"] == 1 and state["pending_verify_cycle"] == 1
    assert advanced == []
    events.clear()
    coordinator().settle()
    assert events[0] == ("verify", 1)
    assert commands == ["settle-1"]
    assert advanced == ["settled"]
    assert not json.loads(state_path.read_text()).get("pending_verify_cycle")
