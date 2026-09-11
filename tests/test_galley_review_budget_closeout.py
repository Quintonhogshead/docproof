"""An exhausted real allowance can close accounting, never invent coverage."""
import hashlib
import json

import pytest

from docproof import resource_ledger as ledger
from docproof.editmap import build_editmap
from galley.engine_phases import EnginePhases, EnginePhaseError
from galley import verify
from tests.test_galley_engine_phases import _driver, _complete_verification


@pytest.fixture
def ready(tmp_path, monkeypatch):
    driver = _driver(tmp_path)
    phases = EnginePhases(driver, lambda spec: pytest.fail("Budget closeout executed a command"))
    run = _complete_verification(phases, monkeypatch)
    build_editmap(verify.paragraph_views(run)[0], []).save(run / "editmap.json")
    # Establish the ordinary immutable original two-pass archive locally.
    with monkeypatch.context() as patch:
        patch.setattr(phases, "_command", lambda *args, **kwargs: None)
        phases.verify()
    advances = []
    monkeypatch.setattr(phases, "_advance", advances.append)
    return phases, run, advances


def receipt(phases, *, limit_calls=1, limit_output=100, output=5,
            status="completed", source=None):
    path = phases.directory.parent / "resources.jsonl"
    source = source or hashlib.sha256(phases.driver.book.read_bytes()).hexdigest()
    context = ledger.context_env(path, source, "a" * 64)
    context.update({ledger.GROUP_ENV: "review", ledger.MAX_CALLS_ENV: str(limit_calls),
                    ledger.MAX_OUTPUT_ENV: str(limit_output)})
    with ledger.use_context(context):
        args = dict(receipt_id="one", operation_id="one", model="test-model", transport="test")
        ledger.append_usage(**args, status="started", max_output_tokens=min(output, limit_output))
        if status != "started":
            ledger.append_usage(**args, status=status, usage=({"input_tokens": 1,
                "output_tokens": output, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "thinking_tokens": 0}
                if status == "completed" else None))
    return path


def evidence(phases, run):
    paths = list((run / verify._CHECKPOINT_DIR).rglob("*.json"))
    paths += list((phases.directory / "coverage").rglob("*.json"))
    paths += [phases.directory / "initial-coverage.json", run / "findings.json",
              verify.deliverable_docx(run), run / "editmap.json",
              run / "change_verify.json", run / "finished_walk.json",
              run / "verification" / "type-compare" / "change_verify.json",
              run / "verification" / "type-compare" / "finished_walk.json"]
    return {p: p.read_bytes() for p in paths}


def other_receipt(phases, *, status="completed", group="book"):
    path = phases.directory.parent / "resources.jsonl"
    context = ledger.context_env(path, hashlib.sha256(phases.driver.book.read_bytes()).hexdigest(), "a" * 64)
    context.update({ledger.GROUP_ENV: group, ledger.MAX_CALLS_ENV: "10",
                    ledger.MAX_OUTPUT_ENV: "100"})
    with ledger.use_context(context):
        args = dict(receipt_id=group, operation_id=group, model="test-model", transport="test")
        ledger.append_usage(**args, status="started", max_output_tokens=5)
        if status != "started":
            ledger.append_usage(**args, status=status, usage=({"input_tokens": 1,
                "output_tokens": 5, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "thinking_tokens": 0}
                if status == "completed" else None))
    return path


def test_historical_book_unknowns_remain_charged_without_blocking_closeout(ready):
    phases, run, advances = ready
    receipt(phases)
    path = other_receipt(phases, status="error")
    original = path.read_bytes()
    phases.settle()
    assert advances == ["settled"]
    assert (run / "settlement.json").exists()
    assert path.read_bytes() == original
    book = ledger.summarize(path)["groups"]["book"]
    assert book["unknown_output_attempts"] == 1
    assert book["charged_output_tokens"] == book["reserved_output_tokens"] == 5


def test_active_book_call_blocks_closeout(ready):
    phases, run, advances = ready
    receipt(phases)
    other_receipt(phases, status="started")
    with pytest.raises((EnginePhaseError, ValueError), match="[Oo]utstanding|[Aa]ctive"):
        phases.settle()
    assert not (run / "settlement.json").exists() and not advances


def test_manual_rewind_after_later_model_usage_does_not_replace_original_closeout(ready):
    phases, run, advances = ready
    receipt(phases)
    phases.settle()
    original = (run / "settlement.json").read_bytes()
    other_receipt(phases, group="astra")
    phases.verify()
    with pytest.raises(ValueError):
        phases.settle()
    assert (run / "settlement.json").read_bytes() == original
    assert advances == ["settled"]


@pytest.mark.parametrize("bound", ["calls", "output", "output_overage"])
def test_actual_persisted_cap_closes_without_spend_or_rewriting_evidence(ready, bound):
    phases, run, advances = ready
    path = receipt(phases, limit_calls=1 if bound == "calls" else 100,
                   limit_output=100 if bound == "calls" else 5,
                   output=9 if bound == "output_overage" else 5)
    protected = {**evidence(phases, run), path: path.read_bytes()}
    phases.verify()  # Original coverage is already proved; no command or archive rewrite.
    phases.settle()
    first = (run / "settlement.json").read_bytes()
    phases.settle()  # Recovery is idempotent and does not reopen the allowance.
    assert (run / "settlement.json").read_bytes() == first
    assert advances == ["settled", "settled"]
    assert all(p.read_bytes() == data for p, data in protected.items())
    assert ledger.summarize(path)["groups"]["review"]["calls"] == 1
    state = json.loads((phases.directory / "review-loop.json").read_text())
    assert state["rounds"] == 0
    assert state["budget_closeout"]["final_review_required"] is True


@pytest.mark.parametrize("calls,output,charged", [(2, 100, 5), (100, 6, 5)])
def test_remaining_allowance_is_not_falsely_declared_exhausted(ready, calls, output, charged):
    phases, _run, _advances = ready
    receipt(phases, limit_calls=calls, limit_output=output, output=charged)
    assert phases._exhausted_review_budget() is None


@pytest.mark.parametrize("status", ["started", "error"])
def test_unresolved_reservations_block_closeout(ready, status):
    phases, run, advances = ready
    receipt(phases, status=status)
    with pytest.raises(EnginePhaseError, match="unresolved usage"):
        phases.settle()
    assert not (run / "settlement.json").exists() and not advances


@pytest.mark.parametrize("damage", ["missing_archive", "archive_hash", "primary_proof",
                                     "second_proof", "source", "text", "edit_payload",
                                     "dirty", "pending_cycle", "ledger_source"])
def test_missing_or_changed_proof_never_becomes_budget_closeout(ready, damage):
    phases, run, advances = ready
    receipt(phases, source="b" * 64 if damage == "ledger_source" else None)
    if damage == "missing_archive":
        (phases.directory / "initial-coverage.json").unlink()
    elif damage == "archive_hash":
        path = next((phases.directory / "coverage").rglob("finished_walk.json"))
        path.write_bytes(path.read_bytes() + b"\n")
    elif damage == "source":
        phases.driver.book.write_bytes(phases.driver.book.read_bytes() + b"changed")
    elif damage == "text":
        import docx
        path = verify.deliverable_docx(run)
        document = docx.Document(path)
        document.paragraphs[0].text = "Different current text."
        document.save(path)
    elif damage == "edit_payload":
        path = run / "findings.json"
        payload = json.loads(path.read_text())
        payload["findings"].append({"para_id": "body-0001", "original_text": "quiet",
                                    "corrected_text": "loud", "applied": True})
        path.write_text(json.dumps(payload))
    elif damage == "pending_cycle":
        from galley.manifest import sha256_file
        (phases.directory / "review-loop.json").write_text(json.dumps({
            "identity": {"source": sha256_file(phases.driver.book), "run": str(run.resolve()),
                         "config": sha256_file(phases._config())}, "rounds": 0,
            "prior_ids": [], "history": [], "pending_verify_cycle": 1}))
    elif damage != "ledger_source":
        directory = run / "verification" / "type-compare" if damage == "second_proof" else run
        path = directory / "finished_walk.json"
        payload = json.loads(path.read_text())
        if damage == "dirty":
            payload["unverified_paragraphs"] = ["body-0001"]
        else:
            payload["verification_provenance"]["proof_sha256"] = "0" * 64
        path.write_text(json.dumps(payload))
    with pytest.raises(EnginePhaseError):
        phases.settle()
    assert not (run / "settlement.json").exists() and not advances


def test_exhausted_budget_does_not_start_a_new_verification_cycle(ready):
    phases, _run, _advances = ready
    receipt(phases)
    with pytest.raises(EnginePhaseError, match="fresh verification cycle"):
        phases.verify(cycle=1)


def test_cycle_zero_resume_rejects_invalid_current_proof_without_executing(ready):
    phases, run, _advances = ready
    receipt(phases)
    path = run / "verification" / "type-compare" / "finished_walk.json"
    payload = json.loads(path.read_text())
    payload["verification_provenance"]["proof_sha256"] = "0" * 64
    path.write_text(json.dumps(payload))
    with pytest.raises(EnginePhaseError, match="current full coverage"):
        phases.verify()
