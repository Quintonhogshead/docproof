"""Independent saved readers must reach settlement without claiming fresh coverage."""
import json

import pytest

import docproof.__main__ as cli
from galley import plan_ledger, verify
from galley.settle import open_items
from galley.settlement_inputs import (CANDIDATES, register_verification_source,
                                      verification_dirs)
from tests.galley.test_settle import (_accepted, _build, _manuscript, _para_ids,
                                     _replay_config, _settle, _walk)
from tests.galley.test_verify_recovery import Provider


@pytest.fixture(autouse=True)
def isolated_coverage(monkeypatch):
    monkeypatch.setattr(verify, "UNREAD", [])
    monkeypatch.setattr(verify, "UNREAD_BATCHES", [])
    monkeypatch.setattr(verify, "_LOSSES", [])


def _read(run, output, pass_id, provider, cfg, monkeypatch):
    monkeypatch.setattr(cli, "build_provider", lambda *a, **kw: provider)
    return cli.main(["galley", "verify", str(run), "--out", str(output),
                     "--config", cfg, "--model", "gpt-5.6-luna",
                     "--verification-pass", pass_id,
                     "--required-verification-passes", "primary,type-compare"])


@pytest.mark.parametrize("separate", [False, True])
def test_independent_read_survives_output_reuse_and_reaches_cli_settlement(
        tmp_path, monkeypatch, separate):
    src = _manuscript(tmp_path, ["The letter was hard to recieve."])
    pid = _para_ids(src)[0][0]
    run = _build(tmp_path, src, [])
    second = tmp_path / "second" if separate else run
    cfg = _replay_config(tmp_path)
    assert _read(run, run, "primary", Provider(), cfg, monkeypatch) == 0
    finding = {"para_id": pid, "quote": "recieve", "problem": "Misspelling",
               "suggestion": "receive", "severity": "high"}
    provider = Provider({"findings": [finding]})
    # Exit 1 is a complete read with findings to settle.
    assert _read(run, second, "type-compare", provider, cfg, monkeypatch) == 1
    assert len(provider.calls) == 1
    snapshots = {p: p.read_bytes() for d in verification_dirs(run)[1:]
                 for p in d.glob("*.json")}
    # A subsequent legitimate read can replace the convenient output files.
    # It cannot erase a previous reader's unresolved candidate.
    assert _read(run, second, "primary", Provider(), cfg, monkeypatch) == 0
    assert json.loads((second / "finished_walk.json").read_text())["residuals"] == []
    assert len(open_items(run)) == 1
    ledger = json.loads((run / CANDIDATES).read_text())
    candidate = next(c for c in ledger["candidates"] if c["evidence"].get("quote") == "recieve")
    read = next(r for r in ledger["reads"] if r["read_id"] == candidate["read_id"])
    assert read["policy"]["pass_id"] == "type-compare"
    assert read["policy"]["required_pass_ids"] == ["primary", "type-compare"]
    assert read["coverage_authority"] is False
    assert candidate["disposition"] == "open"
    assert _settle(tmp_path, run, src) == 0
    assert "receive" in _accepted(run)[pid]
    assert not open_items(run)
    final = json.loads((run / CANDIDATES).read_text())
    disposed = next(c for c in final["candidates"] if c["candidate_id"] == candidate["candidate_id"])
    assert disposed["disposition"] in ("add", "absorb")
    assert disposed["settlement_record"]["residual_id"] == candidate["residual_id"]
    assert all(p.read_bytes() == data for p, data in snapshots.items())
    primary = json.loads((run / "finished_walk.json").read_text())
    assert primary["unverified_paragraphs"] == [pid]
    assert "verification_provenance" not in primary


def test_registry_rejects_mutated_snapshot_and_mixed_gate_pair(tmp_path):
    src = _manuscript(tmp_path)
    run = _build(tmp_path, src, [])
    _walk(run, [])
    register_verification_source(run, run)
    snapshot = verification_dirs(run)[1] / "finished_walk.json"
    data = json.loads(snapshot.read_text())
    data["residuals"] = [{"quote": "made up"}]
    snapshot.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="changed"):
        open_items(run)
    other = _build(tmp_path, src, [], out="other")
    _walk(other, [])
    walk = json.loads((other / "finished_walk.json").read_text())
    walk["verification_pair_id"] = "only-one-gate-was-written"
    (other / "finished_walk.json").write_text(json.dumps(walk))
    with pytest.raises(ValueError, match="artifact pair"):
        register_verification_source(other, other)


def test_unbound_legacy_read_retains_candidate_without_receiving_proof(tmp_path):
    src = _manuscript(tmp_path)
    run = _build(tmp_path, src, [])
    _walk(run, [{"para_id": "p", "quote": "legacy", "problem": "check"}])
    register_verification_source(run, run)
    ledger = json.loads((run / CANDIDATES).read_text())
    assert all(r["coverage_status"] == "unbound" and r["policy"] is None
               for r in ledger["reads"])
    assert ledger["candidates"][0]["disposition"] == "open"
    assert "verification_provenance" not in json.loads((run / "finished_walk.json").read_text())


def test_registration_rejects_known_other_source(tmp_path):
    src = _manuscript(tmp_path)
    run = _build(tmp_path, src, [])
    output = tmp_path / "different-book"
    output.mkdir()
    _walk(output, [])
    payload = json.loads((output / "finished_walk.json").read_text())
    payload["source_sha256"] = "not-this-source"
    (output / "finished_walk.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="different source"):
        register_verification_source(run, output)
    assert not (run / "verification-sources.json").exists()


def test_final_review_indexes_and_closes_exact_registered_candidate(tmp_path, monkeypatch):
    import copy
    from galley import astra_review, manifest
    from galley.settle import Settlement, SettlementRecord
    from galley.settlement_inputs import unresolved_candidates
    src = _manuscript(tmp_path, ["The letter was hard to receive."])
    pid = _para_ids(src)[0][0]
    run = _build(tmp_path, src, [])
    _walk(run, [])
    Settlement().save(run)
    second = tmp_path / "second"
    second.mkdir()
    row = {"para_id": pid, "quote": "receive", "problem": "Questionable spelling"}
    _walk(second, [row])
    register_verification_source(run, second)
    candidate = unresolved_candidates(run)[0]
    packet = astra_review.build_packet(run)
    indexed = [issue for issue in packet["issue_index"] if any(
        source["artifact"] == CANDIDATES for source in issue["sources"])]
    assert len(indexed) == 1
    assert packet["artifacts"][CANDIDATES]["open_candidates"] == [candidate]
    review = {"review": {"issue_decisions": [{"issue_id": indexed[0]["id"],
                                             "action": "drop"}]}}
    monkeypatch.setattr(manifest, "_astra_editorial_snapshot", lambda _run: (review, packet))
    assert manifest._certify_settlement(run).status == "pass"
    changed = copy.deepcopy(packet)
    changed["artifacts"][CANDIDATES]["open_candidates"][0]["evidence"]["quote"] = "different evidence"
    monkeypatch.setattr(manifest, "_astra_editorial_snapshot", lambda _run: (review, changed))
    assert manifest._certify_settlement(run).status == "fail"
    monkeypatch.setattr(manifest, "_astra_editorial_snapshot", lambda _run: None)
    assert manifest._certify_settlement(run).status == "fail"
    # Settled history remains in the ledger, without forcing another judgment.
    settlement = Settlement(records=[SettlementRecord(
        candidate["residual_id"], 1, "drop", None, "", "", "correct_spelling", "deterministic")])
    settlement.save(run)
    assert unresolved_candidates(run) == []
    final_packet = astra_review.build_packet(run)
    assert final_packet["artifacts"][CANDIDATES]["registered_candidates"] == 1
    assert final_packet["artifacts"][CANDIDATES]["open_candidates"] == []
    assert final_packet["issue_index"] == []
    assert manifest._certify_settlement(run).status == "pass"


@pytest.mark.parametrize("kind", ["missing", "empty-directory", "parent", "absolute", "symlink"])
def test_plan_ran_requires_real_workspace_evidence(tmp_path, kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "outside.json"
    external.write_text("{}")
    if kind == "empty-directory":
        (workspace / "artifact").mkdir()
    elif kind == "symlink":
        (workspace / "artifact").symlink_to(external)
    evidence = {"parent": "../outside.json", "absolute": str(external)}.get(kind, "artifact")
    ledger = {"1": {"status": "ran", "evidence": evidence}}
    status, detail = plan_ledger.check("1. continuity $0", ledger,
                                       ledger_exists=True, workspace=workspace)
    assert status == "fail" and "artifact" in detail
    assert plan_ledger.not_done("1. continuity $0", ledger, workspace=workspace)


def test_plan_evidence_is_relative_to_workspace_not_cwd(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "evidence.json").write_text("{}")
    monkeypatch.chdir(tmp_path)
    ledger = {"1": {"status": "ran", "evidence": "evidence.json"},
              "2": {"status": "skipped", "reason": "Not applicable"},
              "3": {"status": "deferred", "reason": "Follow-up review"}}
    assert plan_ledger.check("1. Read $0\n2. Scan $0\n3. Follow-up $0", ledger,
                             ledger_exists=True, workspace=workspace)[0] == "pass"


def test_settlement_budget_stop_is_not_a_revert_or_editorial_disposition():
    from docproof.models import Usage
    from docproof.resource_ledger import ResourceBudgetExceeded
    from galley.settle import Residual, second_look
    class Exhausted:
        def complete_structured(self, **kwargs):
            raise ResourceBudgetExceeded("book allowance exhausted")
    item = Residual("test", "edit_damage", "p", "text", "problem", "fix")
    with pytest.raises(ResourceBudgetExceeded, match="allowance"):
        second_look(item, "text", Exhausted(), "model", Usage())


def test_complete_secondary_pass_checks_nonempty_rows_and_every_window(tmp_path):
    from docproof.models import Usage
    src = _manuscript(tmp_path, ["The letter was hard to recieve."])
    pid = _para_ids(src)[0][0]
    run = _build(tmp_path, src, [{"para_id": pid, "original_text": "letter",
                                "corrected_text": "note", "confidence": "high"}])
    output = tmp_path / "second"
    policy = {"engine": "provider", "pass_id": "type-compare",
              "required_pass_ids": ("primary", "type-compare"),
              "policy_id": verify.VERIFICATION_POLICY, "config_sha256": "a" * 64,
              "context": "Keep the author's voice."}
    provider = Provider(
        {"problems": [{"index": 1, "verdict": "wrong_rule", "detail": "Questioned edit", "fix": "letter"}]},
        {"findings": [{"para_id": pid, "quote": "recieve", "problem": "Misspelling",
                       "suggestion": "receive", "severity": "high"}]})
    changes = verify.verify_run(run, provider, "gpt-5.6-luna", Usage(), run_walk=False, **policy)
    walk = verify.verify_run(run, provider, "gpt-5.6-luna", Usage(), run_changes=False, **policy)
    verify.write_artifacts(output, changes, walk, model="gpt-5.6-luna", engine="provider",
                           usage_changes=Usage(), usage_walk=Usage(), applied=1, paragraphs=1,
                           source_run_dir=run)
    def complete(**overrides):
        return verify.validate_complete_pass(run, output, provider, "gpt-5.6-luna", **{**policy, **overrides})
    assert complete()
    assert not complete(pass_id="primary")
    assert not complete(config_sha256="b" * 64)
    assert not complete(context="Different voice rules")
    assert len(provider.calls) == 2  # Validation itself made no model call.
    for filename, key, field in (("change_verify.json", "problems", "fix"),
                                  ("finished_walk.json", "residuals", "suggestion")):
        path = output / filename
        before = path.read_bytes()
        payload = json.loads(before)
        payload[key][0][field] = "altered after the read"
        path.write_text(json.dumps(payload))
        assert not complete()
        path.write_bytes(before)
    proof = walk.verification_provenance["walk"]
    key = next(iter(proof["windows"]))
    checkpoint = run / verify._CHECKPOINT_DIR / proof["scope"] / proof["invocation_id"] / (key + ".json")
    saved = checkpoint.read_bytes()
    checkpoint.unlink()
    assert not complete()
    checkpoint.write_bytes(saved)
    assert complete()
    assert len(provider.calls) == 2


@pytest.mark.parametrize("suggestion,problem,conflict", [
    ("receive", "Misspelling", False),
    ("retrieve", "Misspelling", True),
    ("receive", "The author may mean a different action", True),
])
def test_same_quote_conflicts_require_exact_final_decision_without_settle_loop(
        tmp_path, monkeypatch, suggestion, problem, conflict):
    from galley import astra_review, manifest
    from galley.settlement_inputs import unresolved_candidates
    src = _manuscript(tmp_path, ["The letter was hard to recieve."])
    pid = _para_ids(src)[0][0]
    run = _build(tmp_path, src, [])
    cfg = _replay_config(tmp_path)
    first = {"para_id": pid, "quote": "recieve", "problem": "Misspelling",
             "suggestion": "receive", "severity": "high"}
    second = {**first, "problem": problem, "suggestion": suggestion, "severity": "medium"}
    assert _read(run, run, "primary", Provider({"findings": [first]}), cfg, monkeypatch) == 1
    # Medium residuals return success but still require explicit disposition.
    assert _read(run, tmp_path / "second", "type-compare", Provider({"findings": [second]}), cfg, monkeypatch) == 0
    assert _settle(tmp_path, run, src) == 0
    assert "receive" in _accepted(run)[pid]
    assert open_items(run) == []  # Preserve stable IDs; no repeated settle cycle.
    pending = unresolved_candidates(run)
    assert len(pending) == int(conflict)
    ledger = json.loads((run / CANDIDATES).read_text())
    applied = [row for row in ledger["candidates"] if row["disposition"] != "open"]
    assert len(applied) == (1 if conflict else 2)
    assert applied[0]["settlement_record"]["input_evidence"]["suggestion"] == "receive"
    if conflict:
        # Existing settlements can identify their input through residuals_seen
        # without having the newly added per-record evidence field.
        settlement_path = run / "settlement.json"
        legacy = json.loads(settlement_path.read_text())
        for record in legacy["records"]:
            record.pop("input_evidence", None)
        settlement_path.write_text(json.dumps(legacy))
        pending = unresolved_candidates(run)
        assert len(pending) == 1
    packet = astra_review.build_packet(run)
    assert packet["artifacts"][CANDIDATES]["open_candidates"] == pending
    if conflict:
        assert pending[0]["evidence"]["suggestion"] == suggestion
        assert pending[0]["evidence"]["problem"] == problem
        assert pending[0]["reason"] == "conflicting_reader_evidence_requires_final_review"
        assert manifest._certify_settlement(run).status == "fail"
        review = {"review": {"issue_decisions": [
            {"issue_id": issue["id"], "action": "drop", "reason": "Exact evidence resolved in final review"}
            for issue in packet["issue_index"]]}}
        monkeypatch.setattr(manifest, "_astra_editorial_snapshot", lambda _run: (review, packet))
    assert manifest._certify_settlement(run).status == "pass"
