"""Transport routing only: no Codex process, network, or paid review."""
import copy
import json
import sys
from types import SimpleNamespace

import pytest

from docproof.__main__ import main
from galley import astra_review as ar
from galley import driver as gd
from galley import outcome as go
from .test_astra_workflow import snapshot, receipt, install_review, _driver


@pytest.fixture
def subscription(monkeypatch):
    calls = []
    value = dict(receipt(), transport="codex", actual_cost_usd=None)

    def review(run, **kwargs):
        calls.append((run, kwargs))
        value["packet_sha256"] = ar.build_packet(run)["packet_sha256"]
        (run / "astra-review.json").write_text(json.dumps(value))
        return copy.deepcopy(value)

    def no_api(*args, **kwargs):
        pytest.fail("Subscription routing attempted an API call")

    module = SimpleNamespace(review_run=review, plan_review=lambda packet, **kw:
        {"transport": "codex", "counts": packet["counts"], **kw})
    monkeypatch.setitem(sys.modules, "galley.astra_subscription", module)
    monkeypatch.setattr(ar, "review_run", no_api)
    monkeypatch.setattr(ar, "validate_receipt", lambda *a, **kw: copy.deepcopy(value))
    return module, calls


def test_cli_defaults_to_subscription_and_pins_chunk_configuration(snapshot, subscription):
    _, _, run = snapshot
    _, calls = subscription
    assert main(["galley", "astra-review", str(run), "--chunk-bytes", "120000"]) == 0
    assert len(calls) == 1 and calls[0][1]["max_chunk_bytes"] == 120000
    marker = json.loads((run / go.ASTRA_REQUIRED_NAME).read_text())
    assert marker["transport"] == "codex" and marker["max_chunk_bytes"] == 120000


def test_subscription_dry_run_is_offline_and_does_not_enroll(snapshot, subscription, monkeypatch, capsys):
    _, _, run = snapshot
    _, calls = subscription
    monkeypatch.setattr(ar, "estimate_review", lambda *a, **kw: pytest.fail("API estimate used"))
    assert main(["galley", "astra-review", str(run), "--dry-run", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["transport"] == "codex" and plan["counts"]["paragraphs"] > 0
    assert not calls and not (run / go.ASTRA_REQUIRED_NAME).exists()


def test_driver_defaults_to_subscription_without_claude_credentials(snapshot, subscription, tmp_path):
    book, ws, run = snapshot
    _, calls = subscription
    result = _driver(book, tmp_path, astra_review=True, astra_transport=None,
                     only_phases=["astra_review"], env={}).run()
    assert result.outcome == "done" and len(calls) == 1
    assert calls[0][0] == run
    assert json.loads((ws / go.ASTRA_REQUIRED_NAME).read_text())["transport"] == "codex"


def test_subscription_failure_remains_operational_without_api_fallback(snapshot, subscription, tmp_path):
    book, _, run = snapshot
    module, _ = subscription
    def failed(*a, **kw):
        raise ar.AstraReviewError("Subscription authentication is required")
    module.review_run = failed
    result = _driver(book, tmp_path, astra_review=True, astra_transport=None,
                     only_phases=["astra_review"], env={}).run()
    assert result.outcome == "blocked" and result.exit_code == 8
    assert not (run / "outcome.json").exists()


def test_default_cli_recovers_existing_api_receipt_through_api(snapshot, monkeypatch):
    _, _, run = snapshot
    (run / "astra-review.json").write_text(json.dumps({"status": "pending", "response_id": "resp-existing"}))
    calls = install_review(monkeypatch, receipt())
    assert main(["galley", "astra-review", str(run)]) == 0
    assert len(calls) == 1 and calls[0][1]["budget_usd"] == 25
    assert json.loads((run / go.ASTRA_REQUIRED_NAME).read_text())["transport"] == "api"


def test_submitted_transport_and_chunks_cannot_change(snapshot, subscription):
    _, _, run = snapshot
    _, calls = subscription
    assert main(["galley", "astra-review", str(run), "--chunk-bytes", "120000"]) == 0
    original = (run / go.ASTRA_REQUIRED_NAME).read_text()
    assert main(["galley", "astra-review", str(run), "--transport", "api"]) == 8
    assert main(["galley", "astra-review", str(run), "--chunk-bytes", "130000"]) == 8
    assert (run / go.ASTRA_REQUIRED_NAME).read_text() == original and len(calls) == 1
    assert gd.astra_review_settings(run)["max_chunk_bytes"] == 120000


def test_driver_parser_exposes_explicit_api_transport(snapshot, capsys):
    book, ws, _ = snapshot
    assert main(["galley", "drive", "--book", str(book), "--slug", ws.name,
                 "--workspace-root", str(ws.parent), "--astra-transport", "api", "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["astra_transport"] == "api"


def test_direct_subscription_receipt_preserves_its_custom_chunk_size(snapshot):
    _, _, run = snapshot
    (run / "astra-review.json").write_text(json.dumps({
        "status": "pending", "transport": "codex", "max_chunk_bytes": 120000}))
    settings = gd.astra_review_settings(run, persist=True)
    assert settings["transport"] == "codex" and settings["max_chunk_bytes"] == 120000


def test_driver_dry_run_reports_saved_api_routing(snapshot, capsys):
    book, ws, run = snapshot
    (run / "astra-review.json").write_text(json.dumps({"status": "pending", "response_id": "resp-existing"}))
    assert main(["galley", "drive", "--book", str(book), "--slug", ws.name,
                 "--workspace-root", str(ws.parent), "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["astra_transport"] == "api"
