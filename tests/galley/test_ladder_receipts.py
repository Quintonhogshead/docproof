"""Cancelled ladder resumes charge cached receipts once and fresh calls fully."""
import dataclasses
import json
from types import SimpleNamespace

import pytest

from docproof.checkpoint import Checkpoint, add_usage
from docproof.models import Usage
from docproof.pipeline import JobCancelled
from docproof.providers import cost_of_usage
from galley.adapters import AdapterResult
from galley.adapters.docproof_ladder import DocproofLadderAdapter
from galley.casefile import CaseFile
from galley.orchestrator import run_galley
from tests.galley.fakes import make_manuscript
from tests.galley.test_ladder_adapter import _lean_cfg

MODEL = "gpt-5.6-luna"


def metered_call(tokens=1000):
    return Usage(input_tokens=tokens, output_tokens=100, api_calls=1,
                 by_model={MODEL: {"input_tokens": tokens, "output_tokens": 100,
                                   "api_calls": 1}})


def price(usage):
    return cost_of_usage(usage, fallback_model=MODEL)


@pytest.mark.parametrize("invalidate,cancel_after", [
    (None, 1), ("fingerprint", 1), ("deleted", 1), (None, 2)])
def test_cancelled_ladder_receipts_survive_resume_without_discounting_new_calls(
        tmp_path, monkeypatch, invalidate, cancel_after):
    import galley.adapters.docproof_ladder as module
    cfg = _lean_cfg()
    cfg.api.model = MODEL
    adapter = DocproofLadderAdapter("source.docx", cfg, object(), workspace=tmp_path)
    state = {"revision": 1, "cancel": True}
    checkpoint_path = tmp_path / "checkpoint.json"
    fetched = []

    def checkpoint(*args):
        result = Checkpoint(checkpoint_path, fingerprint={"revision": state["revision"]})
        result.load()
        return result

    def run_sync(*args, checkpoint, on_checkpoint_usage, **kwargs):
        usage = Usage()
        for n, key in enumerate(("first", "second"), 1):
            cached = checkpoint.get(key)
            if cached is None:
                fetched.append(key)
                checkpoint.put(key, items=[], usage=metered_call(), ok=True)
                cached = checkpoint.get(key)
            add_usage(usage, cached.usage)
            on_checkpoint_usage(key, cached.usage)
            if state["cancel"] and n == cancel_after:
                state["cancel"] = False
                raise JobCancelled(usage=usage)
        return [], usage

    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps({"findings": [{
        "finding_id": "f-1", "para_id": "body-0001", "error_type": "spelling",
        "status": "validated", "confidence": "high",
        "anchor": {"start": 0, "end": 1, "delete_text": "A", "insert_text": "B"},
    }]}))
    monkeypatch.setattr(module, "prepare", lambda *a, **kw: object())
    monkeypatch.setattr(adapter, "_checkpoint", checkpoint)
    monkeypatch.setattr(module, "run_sync", run_sync)
    monkeypatch.setattr(module, "finish", lambda *a, **kw: SimpleNamespace(
        findings_json=findings, warnings=[]))
    manuscript = make_manuscript("A sentence.")
    with pytest.raises(JobCancelled):
        run_galley(manuscript, "T2", 100, tmp_path,
                   adapters={"docproof_ladder": adapter})
    assert CaseFile.load(tmp_path / "casefile.json").budget.spent_usd == \
        pytest.approx(cancel_after * price(metered_call()))
    if invalidate == "fingerprint":
        state["revision"] += 1
    elif invalidate == "deleted":
        checkpoint_path.unlink()
    events = []
    audits = []
    result = run_galley(manuscript, "T2", 100, tmp_path,
                        adapters={"docproof_ladder": adapter},
                        audit=lambda *args: audits.append(args) or [],
                        stop_threshold=price(metered_call()) / 2,
                        notify=lambda kind, payload: events.append((kind, payload)))
    assert fetched == (["first", "second"] if invalidate is None
                       else ["first", "first", "second"])
    assert result.budget.spent_usd == pytest.approx(
        len(fetched) * price(metered_call()))
    # Wave history includes earlier attempt charges; individual actions count
    # only new spend. Cached findings must not appear to have a $0 marginal cost.
    assert result.waves[0].spend_usd == pytest.approx(
        len(fetched) * price(metered_call()))
    assert result.waves[0].actions[0]["cost_usd"] == pytest.approx(
        (len(fetched) - cancel_after) * price(metered_call()))
    assert sum(w.spend_usd for w in result.waves) == result.budget.spent_usd
    assert audits == []
    assert not any(kind == "alarm" and payload["alarm"] == "zero_cost_detector"
                   for kind, payload in events)


def test_cancelled_incremental_adapter_bills_its_actual_rerun(tmp_path):
    class Incremental:
        name = "docproof_ladder"
        calls = 0

        def run(self, manuscript, scope, budget, usage):
            self.calls += 1
            current = metered_call()
            add_usage(usage, dataclasses.asdict(current))
            if self.calls == 1:
                raise JobCancelled()
            return AdapterResult(cost_usd=price(current))

    adapter = Incremental()
    manuscript = make_manuscript("A sentence.")
    with pytest.raises(JobCancelled):
        run_galley(manuscript, "T0", 100, tmp_path,
                   adapters={"docproof_ladder": adapter})
    result = run_galley(manuscript, "T0", 100, tmp_path,
                        adapters={"docproof_ladder": adapter})
    assert adapter.calls == 2
    assert result.budget.spent_usd == pytest.approx(2 * price(metered_call()))


def test_legacy_checkpoint_gets_a_persistent_receipt_identity(tmp_path):
    path = tmp_path / "checkpoint.json"
    checkpoint = Checkpoint(path, fingerprint={"source": "same"})
    checkpoint.put("first", items=[], usage=metered_call(), ok=True)
    lines = path.read_text().splitlines()
    header = json.loads(lines[0])
    header.pop("receipt_id")
    path.write_text(json.dumps(header) + "\n" + "\n".join(lines[1:]) + "\n")
    first_resume = Checkpoint(path, fingerprint={"source": "same"})
    assert first_resume.load() == 1
    first_resume.put("second", items=[], usage=metered_call(), ok=True)
    second_resume = Checkpoint(path, fingerprint={"source": "same"})
    assert second_resume.load() == 2
    assert second_resume.receipt_id == first_resume.receipt_id
