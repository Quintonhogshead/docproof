"""Final review, deterministic handoff, and recovery; no paid/network calls."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from galley import astra_review as ar
from galley import driver as gd
from galley import manifest as gm
from galley import outcome as go
from galley.state_machine import RunStateMachine
from .test_driver import FIXTURE, FakeSpawner, _driver as _legacy_driver


def _driver(book, tmp_path, **kwargs):
    # These cases intentionally exercise the API receipt implementation.
    kwargs.setdefault("astra_transport", "api")
    return _legacy_driver(book, tmp_path, **kwargs)


@pytest.fixture
def snapshot(tmp_path):
    book = tmp_path / "Ford - Book 1.docx"
    book.write_bytes(FIXTURE.read_bytes())
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    run = ws / "runs" / "final"
    run.mkdir(parents=True)
    (run / "final.docx").write_bytes(FIXTURE.read_bytes())
    for name, payload in (("findings.json", {"findings": []}),
                          ("change_verify.json", {}), ("finished_walk.json", {}),
                          ("settlement.json", {"records": [], "open": [], "rounds": 0})):
        (run / name).write_text(json.dumps(payload))
    machine = RunStateMachine.load(ws / "state.json")
    machine.advance("settled", by="test", source_sha256=machine.source_sha256,
                    config_sha256="config")
    machine.save(ws / "state.json")
    return book, ws, run


def receipt(verdict="ready", repair=False):
    return {"schema_version": 1, "status": "completed", "model": "gpt-6-astra",
            "reasoning_effort": "high", "packet_sha256": "a" * 64,
            "response_id": "resp_test", "actual_cost_usd": 1.5, "usage": {},
            "repair_required": repair, "delivery_ready": verdict == "ready" and not repair,
            "review": {"editorial_verdict": verdict,
                       "verdict_reason": "Astra's final editorial judgment.",
                       "revision_review": {"exceptions": []}, "comment_decisions": [],
                       "actions": [{"id": "fix-1"}] if repair else []}}


def install_review(monkeypatch, value):
    calls = []
    def review(run, **kwargs):
        calls.append((Path(run), kwargs))
        value["packet_sha256"] = ar.build_packet(run)["packet_sha256"]
        (Path(run) / "astra-review.json").write_text(json.dumps(value))
        return copy.deepcopy(value)
    monkeypatch.setattr(ar, "review_run", review)
    monkeypatch.setattr(ar, "validate_receipt", lambda *a, **kw: copy.deepcopy(value))
    return calls


def test_default_phase_order_and_explicit_legacy_optout():
    assert gd.select_phases(start="verify") == [
        "verify", "settle", "astra_review", "certify", "deliver"]
    assert "astra_review" not in gd.select_phases(astra_review=False)


def test_review_is_one_direct_call_with_fixed_model_and_high_effort(snapshot, tmp_path, monkeypatch):
    book, ws, run = snapshot
    calls = install_review(monkeypatch, receipt())
    spawn = FakeSpawner(ws)
    result = _driver(book, tmp_path, astra_review=True, only_phases=["astra_review"],
                     spawn=spawn, model="some-other-brain", effort="low").run()
    assert result.outcome == "done"
    assert len(calls) == 1 and calls[0][0] == run
    assert calls[0][1]["budget_usd"] == 25.0
    assert calls[0][1]["max_output_tokens"] == 32768
    assert spawn.calls == []
    assert RunStateMachine.load(ws / "state.json").current == "astra_reviewed"
    assert go.Outcome.load(run).set_by == "gpt-6-astra (final editorial review)"


def test_astra_human_verdict_keeps_its_reason_and_skips_certified_delivery(snapshot, tmp_path, monkeypatch):
    book, ws, run = snapshot
    install_review(monkeypatch, receipt("needs_human", repair=True))
    spawn = FakeSpawner(ws)
    result = _driver(book, tmp_path, astra_review=True, start_phase="astra_review",
                     spawn=spawn).run()
    assert result.outcome == "needs_human" and result.exit_code == 7
    assert result.reason == "Astra's final editorial judgment."
    assert [p.phase for p in result.phases] == ["astra_review"]
    assert spawn.calls == []
    shipped = ws / "handoff" / "Ford - Book 2 - outcome.json"
    assert json.loads(shipped.read_text())["set_by"].startswith("gpt-6-astra")
    assert not (ws / "handoff" / "Ford - Book 2.docx").exists()


def test_ready_repairs_are_reconciled_before_done(snapshot, tmp_path, monkeypatch):
    from galley import astra_reconcile
    book, ws, run = snapshot
    calls = install_review(monkeypatch, receipt(repair=True))
    reconciled = []
    def reconcile(path):
        reconciled.append(Path(path))
        clean = receipt()
        monkeypatch.setattr(ar, "validate_receipt", lambda *a, **kw: clean)
        return clean
    monkeypatch.setattr(astra_reconcile, "reconcile_run", reconcile)
    result = _driver(book, tmp_path, astra_review=True, only_phases=["astra_review"]).run()
    assert result.outcome == "done"
    assert reconciled == [run] and len(calls) == 1


def test_unsupported_repair_blocks_without_human_verdict(snapshot, tmp_path, monkeypatch):
    from galley import astra_reconcile
    book, ws, run = snapshot
    install_review(monkeypatch, receipt(repair=True))
    def reject(_run):
        raise ar.AstraReviewError("The recorded repair overlaps a revision.")
    monkeypatch.setattr(astra_reconcile, "reconcile_run", reject)
    result = _driver(book, tmp_path, astra_review=True, only_phases=["astra_review"]).run()
    assert result.outcome == "blocked" and result.exit_code == 8
    assert not (ws / "runs" / "outcome.json").exists()
    assert not (run / "outcome.json").exists()
    assert "overlaps" in result.reason


def test_api_failure_blocks_without_fallback_and_enrollment_stays_required(snapshot, tmp_path, monkeypatch):
    book, ws, run = snapshot
    def failed(*a, **kw):
        raise ar.AstraReviewError("Response is ambiguous; recover the existing request.")
    monkeypatch.setattr(ar, "review_run", failed)
    monkeypatch.setattr(ar, "validate_receipt", failed)
    result = _driver(book, tmp_path, astra_review=True, only_phases=["astra_review"]).run()
    assert result.outcome == "blocked" and result.handoff == []
    assert (run / go.ASTRA_REQUIRED_NAME).exists()
    assert not (ws / "runs" / "outcome.json").exists()
    with pytest.raises(ar.AstraReviewError):
        go.assess(run)
    assert gm._certify_astra_review(run).status == "fail"
    with pytest.raises(gd.DriverError, match="cannot bypass"):
        _driver(book, tmp_path, only_phases=["certify"]).run()


def test_earlier_infrastructure_failure_never_invents_human_pr(snapshot, tmp_path):
    book, ws, run = snapshot
    result = _driver(book, tmp_path, astra_review=True, only_phases=["ladder"],
                     spawn=FakeSpawner(ws, fail="ladder")).run()
    assert result.outcome == "blocked"
    assert not (ws / "runs" / "outcome.json").exists()


def test_noisy_settlement_reaches_astra_when_final_snapshot_is_available(snapshot, tmp_path, monkeypatch):
    book, ws, run = snapshot
    install_review(monkeypatch, receipt())
    (run / "settlement.json").write_text(json.dumps({
        "convergence": {"quiet": False, "rounds": 3, "last_new_items": 25,
                        "stopped": "round_cap"}}))
    result = _driver(book, tmp_path, astra_review=True,
                     only_phases=["settle", "astra_review"], spawn=FakeSpawner(ws)).run()
    assert result.outcome == "done"
    assert [p.phase for p in result.phases] == ["settle", "astra_review"]


def test_stale_or_conflicting_outcome_cannot_pass_certificate(snapshot, monkeypatch):
    _, _, run = snapshot
    install_review(monkeypatch, receipt())
    (run / go.ASTRA_REQUIRED_NAME).write_text("{}")
    go.Outcome("needs_human", "old threshold result").save(run)
    assert go.assess(run).outcome == "done"
    assert gm._certify_outcome(run).status == "fail"
    go.assess(run).save(run)
    assert gm._certify_outcome(run).status == "pass"


def test_canonical_workspace_state_is_found(snapshot):
    _, _, run = snapshot
    assert gm._certify_run_state(run).status == "pass"


def test_reconciled_delta_is_supplemental_coverage_not_a_restamped_verifier(tmp_path, monkeypatch):
    from galley import verify
    fp = {"accepted_sha256": "new", "paragraph_sha256": {"body-1": "new-p", "body-2": "same"}}
    monkeypatch.setattr(verify, "build_fingerprints", lambda run: fp)
    value = receipt()
    value["reconciliation"] = {"original_accepted_sha256": "old", "current_accepted_sha256": "new",
                               "paragraph_changes": [{"para_id": "body-1", "before_sha256": "old-p",
                                                      "after_sha256": "new-p", "action_ids": ["a1"]}]}
    monkeypatch.setattr(ar, "validate_receipt", lambda *a, **kw: value)
    payload = {"accepted_sha256": "old", "paragraph_sha256": {"body-1": "old-p", "body-2": "same"}}
    original = copy.deepcopy(payload)
    assert gm._binding_problem(tmp_path, "finished_walk.json", payload) == ""
    assert payload == original
    payload["paragraph_sha256"]["body-2"] = "unreviewed"
    assert "lack verification" in gm._binding_problem(tmp_path, "finished_walk.json", payload)


def test_new_workflow_certifies_and_packages_without_later_model_sessions(snapshot, tmp_path, monkeypatch):
    book, ws, run = snapshot
    calls = install_review(monkeypatch, receipt())
    (ws / "approval.json").write_text(json.dumps({
        "config_path": str(Path(__file__).resolve().parents[2] / "config" / "default.yaml")}))
    monkeypatch.setattr(gm, "certify_run", lambda *a, **kw: gm.Certificate([gm.Check("test gate", "pass")]))
    spawn = FakeSpawner(ws)
    result = _driver(book, tmp_path, astra_review=True, start_phase="astra_review", spawn=spawn).run()
    assert result.outcome == "done", result.reason
    assert len(calls) == 1 and spawn.calls == []
    assert RunStateMachine.load(ws / "state.json").current == "delivered"
    package = json.loads((ws / "runs" / "driver" / "package.json").read_text())
    assert len(package["artifacts"]) == 8
    before = {p: Path(p).read_bytes() for p in map(str, result.handoff)}
    second = _driver(book, tmp_path, astra_review=True, start_phase="deliver", spawn=spawn).run()
    assert second.outcome == "done", second.reason
    assert spawn.calls == [] and len(calls) == 1
    assert before == {p: Path(p).read_bytes() for p in before}


def transport_package(tmp_path):
    files = []
    for name in ("Book - outcome.json", "Book.docx", "Book - letter.md"):
        path = tmp_path / name
        path.write_text(name)
        files.append({"name": name, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return {"packet_sha256": "packet", "artifacts": files}


def test_upload_receipts_survive_failure_and_outcome_is_the_last_commit(tmp_path):
    package = transport_package(tmp_path)
    ledger = tmp_path / "delivery.json"
    calls = []
    fail = [True]
    def upload(files, folder):
        name = files[0].name
        calls.append(name)
        if name.endswith("letter.md") and fail[0]:
            raise OSError("temporary failure")
        return ["id-" + name]
    with pytest.raises(OSError):
        gd.publish_verified_handoff(package, "folder", ledger, source_id="source", upload=upload,
                                    verify=lambda *a: True)
    saved = json.loads(ledger.read_text())
    assert saved["status"] == "pending"
    assert saved["artifacts"]["Book.docx"]["verified"] is True
    assert "Book - outcome.json" not in calls
    fail[0] = False
    ids = gd.publish_verified_handoff(package, "folder", ledger, source_id="source", upload=upload,
                                      verify=lambda *a: True)
    assert len(ids) == 3
    assert calls.count("Book.docx") == 1
    assert calls[-1] == "Book - outcome.json"
    assert json.loads(ledger.read_text())["status"] == "delivered"


def test_remote_verification_failure_retains_id_and_does_not_acknowledge(tmp_path):
    package = transport_package(tmp_path)
    ledger = tmp_path / "delivery.json"
    calls = []
    def upload(files, folder):
        calls.append(files[0].name)
        return ["id-" + files[0].name]
    with pytest.raises(gd.DriverError, match="verification failed"):
        gd.publish_verified_handoff(package, "folder", ledger, source_id="source", upload=upload,
                                    verify=lambda *a: False)
    assert len(calls) == 1
    assert json.loads(ledger.read_text())["status"] == "pending"
    gd.publish_verified_handoff(package, "folder", ledger, source_id="source", upload=upload,
                                verify=lambda *a: True)
    assert calls.count("Book.docx") == 1


def test_cli_cannot_override_enrolled_astra_verdict(snapshot, capsys):
    from docproof.__main__ import main
    _, _, run = snapshot
    (run / go.ASTRA_REQUIRED_NAME).write_text("{}")
    assert main(["galley", "outcome", str(run), "--set", "done", "--reason", "override"]) == 2
    assert "cannot override" in capsys.readouterr().err


def test_cli_reconciles_ready_repairs_without_a_second_review(snapshot, monkeypatch):
    from docproof.__main__ import main
    from galley import astra_reconcile
    _, _, run = snapshot
    calls = install_review(monkeypatch, receipt(repair=True))
    reconciled = []
    def apply(path, **kwargs):
        reconciled.append((path, kwargs))
        value = receipt()
        monkeypatch.setattr(ar, "validate_receipt", lambda *a, **kw: value)
        return value
    monkeypatch.setattr(astra_reconcile, "reconcile_run", apply)
    assert main(["galley", "astra-review", str(run), "--transport", "api", "--json"]) == 0
    assert len(calls) == len(reconciled) == 1
    assert go.Outcome.load(run).outcome == "done"


def test_explicit_astra_dispositions_close_old_flags_without_mutating_evidence(snapshot, monkeypatch):
    _, _, run = snapshot
    findings = {"findings": [{"finding_id": "old", "state": "pending"}]}
    problem = {"problem_id": "p1", "para_id": "body-1", "detail": "prior edit flag"}
    residual = {"residual_id": "r1", "para_id": "body-1", "problem": "prior residual"}
    payloads = {"findings.json": findings,
                "change_verify.json": {"ran": False, "problems": [problem], "unread_batches": [{"edits": 2}]},
                "finished_walk.json": {"ran": False, "residuals": [residual], "unread_paragraphs": ["body-1"]},
                "settlement.json": {"open": [residual], "records": [], "rounds": 3}}
    for name, payload in payloads.items():
        (run / name).write_text(json.dumps(payload))
    original = {name: (run / name).read_bytes() for name in payloads}
    value = receipt()
    value["review"]["finding_review"] = {"exceptions": [
        {"finding_id": "finding-000001", "action": "drop", "reason": "Book context resolves it."}]}
    packet = ar.build_packet(run)
    value["review"]["issue_decisions"] = [
        {"issue_id": item["id"], "action": "drop", "reason": "False prior flag."}
        for item in packet["issue_index"]]
    install_review(monkeypatch, value)
    ar.review_run(run)
    assert gm._certify_terminal_states(findings, run).status == "pass"
    assert gm._certify_change_verify(run).status == "pass"
    assert gm._certify_finished_walk(run).status == "pass"
    assert gm._certify_settlement(run).status == "pass"
    assert original == {name: (run / name).read_bytes() for name in original}
    value["review"]["issue_decisions"] = []
    assert gm._certify_change_verify(run).status == "fail"
    assert gm._certify_finished_walk(run).status == "fail"
    assert gm._certify_settlement(run).status == "fail"


def test_human_review_diagnostics_use_verified_frozen_delivery_on_retry(snapshot, tmp_path, monkeypatch):
    book, ws, run = snapshot
    install_review(monkeypatch, receipt("needs_human", repair=True))
    stale = ws / "deliverable"
    stale.mkdir(exist_ok=True)
    (stale / "author-letter.docx").write_bytes(FIXTURE.read_bytes())
    (stale / "stale.docx").write_bytes(FIXTURE.read_bytes())
    calls = []
    fail = [True]
    def upload(files, folder):
        name = files[0].name
        calls.append(name)
        if name.endswith("findings.json") and fail[0]:
            raise OSError("temporary upload error")
        return ["id-" + name]
    first = _driver(book, tmp_path, astra_review=True, start_phase="astra_review",
                    drive_folder_id="folder", upload=upload, verify_upload=lambda *a: True).run()
    assert first.outcome == "needs_human" and not first.uploaded
    package = json.loads((ws / "runs" / "driver" / "package.json").read_text())
    assert package["kind"] == "human_review"
    assert not any(item["name"].endswith(".docx") for item in package["artifacts"])
    before = {item["path"]: Path(item["path"]).read_bytes() for item in package["artifacts"]}
    fail[0] = False
    second = _driver(book, tmp_path, astra_review=True, start_phase="deliver",
                     drive_folder_id="folder", upload=upload, verify_upload=lambda *a: True).run()
    assert second.outcome == "needs_human" and second.uploaded
    assert calls.count("Ford - Book 2 - astra-review.json") == 1
    assert calls[-1].endswith("outcome.json")
    assert before == {path: Path(path).read_bytes() for path in before}
    assert json.loads((ws / "runs" / "driver" / "delivery.json").read_text())["status"] == "delivered"


def test_human_review_packaging_failure_keeps_agent_claim_resumable(snapshot, tmp_path, monkeypatch):
    from galley import agent as ga
    book, ws, run = snapshot
    install_review(monkeypatch, receipt("needs_human", repair=True))
    make_diagnostics = gd.build_diagnostics
    progress = []

    def fail_package(*args, **kwargs):
        raise OSError("diagnostics storage temporarily unavailable")

    monkeypatch.setattr(gd, "build_diagnostics", fail_package)
    result = _driver(book, tmp_path, astra_review=True, start_phase="astra_review",
                     drive_folder_id="folder", progress=progress.append).run()
    assert result.outcome == "blocked" and result.exit_code == 8
    assert result.stopped_at == "deliver" and result.handoff == []
    assert not (ws / "runs" / "driver" / "package.json").exists()
    assert go.Outcome.load(run).outcome == "needs_human"
    assert go.Outcome.load(run).reason == "Astra's final editorial judgment."
    assert any(event.get("event") == "blocked" for event in progress)
    assert not any(event.get("event") == "finished" for event in progress)

    # Feed the actual blocked driver result through the agent's completion
    # path: it must preserve the claim, rather than record terminal FAILED.
    monkeypatch.setattr(ga, "slug_for", lambda *args: ws.name)
    agent = ga.Agent(env=ga.AgentEnv("https://example.invalid", "test", "test"),
                     workspace_root=ws.parent, heartbeat_interval_s=0,
                     download=lambda *_args: book, run_driver=lambda **_kwargs: result,
                     log=lambda _message: None)
    monkeypatch.setattr(agent, "_beat", lambda **_kwargs: None)
    ledger = ga.Ledger(tmp_path / "agent-ledger.json")
    report = ga.RunReport()
    agent.run_book(ga.AwaitingBook("source-book", book.name, "folder", "Ford"), ledger, report)
    assert ledger.state("source-book") == ga.CLAIMED
    assert ledger.pending() == ["source-book"]
    assert ledger.claimed("source-book")["operational_status"] == "blocked"
    assert report.outcome == "blocked"

    # Once local packaging works again, the existing Astra verdict can produce
    # its diagnostic handoff through a delivery-only resume.
    monkeypatch.setattr(gd, "build_diagnostics", make_diagnostics)
    resumed = _driver(book, tmp_path, astra_review=True, start_phase="deliver",
                      drive_folder_id="folder", upload=lambda paths, _folder: ["id-" + paths[0].name],
                      verify_upload=lambda *_args: True).run()
    assert resumed.outcome == "needs_human" and resumed.uploaded
    assert json.loads((ws / "runs" / "driver" / "package.json").read_text())["kind"] == "human_review"


def test_default_workspace_enrollment_blocks_early_heuristic_verdict(snapshot, tmp_path, monkeypatch):
    book, ws, run = snapshot
    monkeypatch.setattr(ar, "validate_receipt", lambda *a, **kw: (_ for _ in ()).throw(
        ar.AstraReviewError("Final review is pending")))
    result = _driver(book, tmp_path, astra_review=True, only_phases=["profile"],
                     spawn=FakeSpawner(ws, fail="profile")).run()
    assert result.outcome == "blocked"
    assert go.requires_astra_review(run)
    with pytest.raises(ar.AstraReviewError, match="pending"):
        go.assess(run)
