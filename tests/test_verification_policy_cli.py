"""Independent verification policies must reach both CLI model gates intact."""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import docx
import pytest

import docproof.__main__ as cli
import docproof.contract as contract
from docproof.models import Usage
from docproof.providers import NormalizedUsage, ProviderResult
from galley import verify


@pytest.fixture
def run(tmp_path):
    target = tmp_path / "run"
    target.mkdir()
    book = docx.Document()
    book.add_paragraph("The author chose these words deliberately.")
    book.save(target / "book.docx")
    (target / "findings.json").write_text(json.dumps({"findings": []}))
    return target


@pytest.fixture
def routed_invocation(monkeypatch):
    @contextmanager
    def invocation(*args, **kwargs):
        yield SimpleNamespace(command_id="a" * 32, mark_complete=lambda *args: True)

    monkeypatch.setattr(verify, "verification_invocation", invocation)


def test_explicit_independent_pass_policy_reaches_each_gate(run, monkeypatch, routed_invocation):
    calls = []

    def record(*args, **kwargs):
        calls.append(kwargs)
        return verify.VerifyRunResult([], [], kwargs["run_changes"], kwargs["run_walk"])

    monkeypatch.setattr(verify, "verify_run", record)
    monkeypatch.setattr(cli, "build_provider", lambda *a, **k: object())
    monkeypatch.setattr(cli, "_galley_spend_guard", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_galley_over_budget", lambda *a, **k: None)
    assert cli.main([
        "galley", "verify", str(run), "--engine", "provider",
        "--verification-pass", "sense", "--verification-policy", "two-reads-v1",
        "--required-verification-passes", "mechanics,sense",
    ]) == 0
    assert len(calls) == 2
    for call in calls:
        assert call["engine"] == "provider"
        assert call["pass_id"] == "sense"
        assert call["policy_id"] == "two-reads-v1"
        assert call["required_pass_ids"] == ("mechanics", "sense")
        assert len(call["config_sha256"]) == 64
        assert call["command_id"] == "a" * 32


@pytest.mark.parametrize("command", ["verify", "settle"])
@pytest.mark.parametrize("flags, message", [
    (["--verification-pass", "sense"], "must be included"),
    (["--required-verification-passes", "primary,primary"], "distinct"),
    (["--required-verification-passes", "primary,"], "nonempty"),
    (["--verification-policy", ""], "1–128"),
    (["--verification-pass", "../primary"], "1–128"),
])
def test_invalid_policy_stops_before_a_provider_is_created(
        run, monkeypatch, capsys, command, flags, message):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid verification policy reached a model lane")

    monkeypatch.setattr(cli, "build_provider", forbidden)
    monkeypatch.setattr(cli, "_resolve_engine", forbidden)
    args = ["galley", command, str(run), *flags]
    if command == "settle":
        args += ["--source", str(run / "book.docx")]
    assert cli.main(args) == 2
    assert message in capsys.readouterr().err


def test_settle_receives_the_same_required_policy(run, monkeypatch):
    class Captured(Exception):
        pass

    def capture(*args, options, **kwargs):
        assert options.verification_pass == "sense"
        assert options.verification_policy == "two-reads-v1"
        assert options.required_verification_passes == ("mechanics", "sense")
        assert len(options.verification_config_sha256) == 64
        raise Captured

    monkeypatch.setattr("galley.settle.Settler", capture)
    monkeypatch.setattr(cli, "_resolve_engine", lambda *a, **k: ("none", None, ""))
    with pytest.raises(Captured):
        cli.main([
            "galley", "settle", str(run), "--source", str(run / "book.docx"),
            "--verification-pass", "sense", "--verification-policy", "two-reads-v1",
            "--required-verification-passes", "mechanics,sense",
        ])


def test_resumed_cli_accounts_for_saved_and_new_usage(run, monkeypatch, routed_invocation):
    observed_costs = []
    artifact_inputs = []
    render = contract.build_envelope

    def record_artifact(*args, usage, **kwargs):
        artifact_inputs.append(usage.input_tokens)
        return render(*args, usage=usage, **kwargs)

    def record(_run, _provider, model, usage, **kwargs):
        scale = 1 if kwargs["run_changes"] else 2
        usage.add(NormalizedUsage(input_tokens=10 * scale, output_tokens=scale), model)
        recovered = Usage()
        recovered.add(NormalizedUsage(input_tokens=100 * scale, output_tokens=10 * scale), model)
        return verify.VerifyRunResult([], [], kwargs["run_changes"], kwargs["run_walk"],
                                      recovered_usage=recovered)

    monkeypatch.setattr(verify, "verify_run", record)
    monkeypatch.setattr(contract, "build_envelope", record_artifact)
    monkeypatch.setattr(cli, "build_provider", lambda *a, **k: object())
    monkeypatch.setattr(cli, "_galley_spend_guard", lambda *a, **k: None)
    monkeypatch.setattr("docproof.providers.cost_of_usage",
                        lambda usage, **kwargs: float(usage.input_tokens))
    monkeypatch.setattr(cli, "_galley_over_budget",
                        lambda args, cost: observed_costs.append(cost))
    assert cli.main(["galley", "verify", str(run), "--engine", "provider"]) == 0
    assert observed_costs == [330.0]
    # Each gate's durable cost also includes the calls made before interruption.
    assert artifact_inputs == [110, 220]


def test_cli_recovers_prior_gate_and_commits_only_after_side_output(run, tmp_path, monkeypatch):
    """Exercise the real CLI recovery boundary, including --out and repeat reads."""
    pid = next(iter(verify.accepted_text(run)))
    (run / "findings.json").write_text(json.dumps({"findings": [{
        "para_id": pid, "original_text": "teh", "corrected_text": "the",
        "status": "validated", "error_type": "spelling"}]}))

    class Provider:
        name = "test-provider"

        def __init__(self, interrupt_at=None):
            self.calls = []
            self.interrupt_at = interrupt_at

        def complete_structured(self, **kwargs):
            self.calls.append(kwargs["schema_name"])
            if len(self.calls) == self.interrupt_at:
                raise RuntimeError("transport interrupted during walk")
            return ProviderResult(parsed={kwargs["schema_name"]: []},
                                  usage=NormalizedUsage(1000, 200, billed=False))

    monkeypatch.setattr(verify, "UNREAD", [])
    monkeypatch.setattr(verify, "UNREAD_BATCHES", [])
    monkeypatch.setattr(verify, "_LOSSES", [])
    monkeypatch.setattr(cli, "_galley_spend_guard", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_galley_over_budget", lambda *a, **k: None)
    provider = Provider(interrupt_at=2)
    monkeypatch.setattr(cli, "build_provider", lambda *a, **k: provider)
    out = tmp_path / "reports"
    args = ["galley", "verify", str(run), "--engine", "provider", "--out", str(out)]
    with pytest.raises(RuntimeError, match="transport interrupted"):
        cli.main(args)
    provider = Provider()
    assert cli.main(args) == 0
    assert provider.calls == ["findings"]
    artifact = json.loads((out / "change_verify.json").read_text())
    assert artifact["build_sha256"] == verify.build_fingerprints(run)["build_sha256"]
    assert artifact["cost"]["total_usd"] == 0.0
    assert cli.main(args) == 0
    assert provider.calls == ["findings", "problems", "findings"]


def test_resumed_settle_cli_reports_saved_usage_without_changing_new_call_limits(run, monkeypatch):
    from galley.settle import Settlement, SettleResult

    model = "gpt-5.6-luna"
    current, recovered = Usage(), Usage()
    current.add(NormalizedUsage(10, 1), model)
    recovered.add(NormalizedUsage(100, 10), model)
    result = SettleResult(Settlement(), usage=current, recovered_usage=recovered)
    costs, envelope_inputs = [], []

    def envelope(*args, usage, **kwargs):
        envelope_inputs.append(usage.input_tokens)
        return {}

    monkeypatch.setattr("galley.settle.Settler", lambda *a, **k: SimpleNamespace(run=lambda: result))
    monkeypatch.setattr(cli, "_resolve_engine", lambda *a, **k: ("none", None, model))
    monkeypatch.setattr("galley.outcome.requires_astra_review", lambda *a: True)
    monkeypatch.setattr("docproof.providers.cost_of_usage", lambda usage, **k: float(usage.input_tokens))
    monkeypatch.setattr(cli, "_galley_over_budget", lambda args, cost: costs.append(cost))
    monkeypatch.setattr(cli, "_envelope", envelope)
    assert cli.main(["galley", "settle", str(run), "--source", str(run / "book.docx"), "--json"]) == 0
    assert costs == [110.0] and envelope_inputs == [110]
    assert result.usage.api_calls == 1 and result.recovered_usage.api_calls == 1
