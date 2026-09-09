"""No live model calls: exercise the actual runner against a fake subprocess."""
import copy
import fcntl
import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from galley import codex_runner as cr
from galley.astra_review import AstraReviewError


SCHEMA = {"type": "object", "properties": {
    "ready": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["ready", "reason"], "additionalProperties": False}
RESULT = {"ready": True, "reason": "All assigned evidence was reviewed."}


class FakeProcess:
    def __init__(self, owner, argv, **kwargs):
        self.owner, self.argv, self.kwargs = owner, argv, kwargs
        self.pid, self.returncode, self.communications = 7654321, 0, 0
        self.is_auth = "login" in argv
        owner.calls.append(self)
        if owner.start_error and not self.is_auth:
            raise OSError("fake spawn failure")

    def communicate(self, data=None, timeout=None):
        self.communications += 1
        self.input = data
        self.timeout = timeout
        if self.is_auth:
            self.returncode = self.owner.auth_returncode
            self.kwargs["stderr"].write(self.owner.auth_text.encode())
            return None, None
        if self.owner.timeout and self.communications == 1:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        self.returncode = -15 if self.owner.timeout else self.owner.returncode
        self.kwargs["stderr"].write(self.owner.stderr.encode())
        self.kwargs["stdout"].write(self.owner.events.encode())
        if self.owner.write_output:
            output = Path(self.argv[self.argv.index("--output-last-message") + 1])
            output.write_text(self.owner.output)
        return None, None


class FakeCLI:
    def __init__(self):
        self.calls = []
        self.auth_text = "Logged in using ChatGPT\n"
        self.auth_returncode, self.returncode = 0, 0
        self.start_error = self.timeout = False
        self.write_output = True
        self.output = json.dumps(RESULT)
        self.stderr = ""
        self.events = '\n'.join(map(json.dumps, [
            {"type": "thread.started", "thread_id": "thread-fake-01"},
            {"type": "item.completed", "item": {"text": "SECRET_TRANSCRIPT_VALUE"}},
            {"type": "turn.completed", "usage": {"input_tokens": 123, "cached_input_tokens": 45,
                                                  "output_tokens": 67, "api_key": "DO_NOT_KEEP"}},
        ])) + "\n"

    def __call__(self, argv, **kwargs):
        return FakeProcess(self, argv, **kwargs)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    cli = FakeCLI()
    monkeypatch.setenv("GALLEY_CODEX_HOME", str(tmp_path / "auth-cache"))
    monkeypatch.setattr(cr.shutil, "which", lambda candidate: "/installed/codex" if candidate else None)
    monkeypatch.setattr(cr.subprocess, "Popen", cli)
    return cli


def run(tmp_path, **kwargs):
    return cr.run_structured("Review all supplied book evidence.", copy.deepcopy(SCHEMA),
                             tmp_path / "work", request_id="chapter-1", **kwargs)


def receipt(tmp_path):
    return json.loads((cr.request_directory(tmp_path / "work", "chapter-1") / "receipt.json").read_text())


def test_check_login_only_probes_private_subscription_auth(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "NEVER_INHERIT")
    monkeypatch.setenv("CODEX_HOME", "/interactive-cache")
    assert cr.check_login(codex_bin="custom-codex", timeout_seconds=12) is None
    assert len(fake.calls) == 1 and fake.calls[0].is_auth
    probe = fake.calls[0]
    assert probe.argv[-2:] == ["login", "status"]
    assert 'forced_login_method="chatgpt"' in probe.argv
    assert probe.kwargs["env"]["CODEX_HOME"] == str(tmp_path / "auth-cache")
    assert "OPENAI_API_KEY" not in probe.kwargs["env"]
    assert probe.kwargs["cwd"] == tmp_path / "auth-cache"
    assert 0 < probe.timeout <= 12
    assert not (tmp_path / "work").exists()


@pytest.mark.parametrize("auth_text", ["Not logged in", "Logged in using an API key"])
def test_check_login_blocks_missing_or_paid_credentials_without_exposing_output(fake, auth_text):
    fake.auth_text = auth_text + " SECRET_AUTH_VALUE"
    with pytest.raises(AstraReviewError, match="subscription login") as failure:
        cr.check_login()
    assert "SECRET_AUTH_VALUE" not in str(failure.value)
    assert len(fake.calls) == 1 and fake.calls[0].is_auth


@pytest.mark.parametrize("timeout", [0, -1, True, "30"])
def test_check_login_rejects_invalid_timeout_before_subprocess(fake, timeout):
    with pytest.raises(AstraReviewError, match="timeout"):
        cr.check_login(timeout_seconds=timeout)
    assert fake.calls == []


def test_uses_only_chatgpt_astra_high_and_reuses_completed_output(fake, tmp_path, monkeypatch):
    leaked_env = {
        "OPENAI_API_KEY": "pay-key", "CODEX_API_KEY": "pay-key-2",
        "OPENAI_BASE_URL": "https://metered-provider.invalid", "ANTHROPIC_API_KEY": "claude-pay",
        "CLAUDE_CODE_OAUTH_TOKEN": "claude-subscription", "MYSTERY_PROVIDER_API_KEY": "other",
        "AZURE_OPENAI_ENDPOINT": "https://other.invalid", "GOOGLE_APPLICATION_CREDENTIALS": "/secret",
        "CODEX_HOME": "/interactive-login", "CUSTOM_AUTH_TOKEN": "secret", "DATABASE_PASSWORD": "secret",
    }
    for key, value in leaked_env.items():
        monkeypatch.setenv(key, value)
    assert run(tmp_path) == RESULT
    assert run(tmp_path) == RESULT
    assert len(fake.calls) == 2
    auth, call = fake.calls
    assert auth.argv[-2:] == ["login", "status"]
    assert call.argv[:4] == ["/installed/codex", "exec", "--model", "gpt-6-astra"]
    assert 'model_reasoning_effort="high"' in call.argv
    assert 'forced_login_method="chatgpt"' in call.argv
    assert 'model_provider="openai"' in call.argv
    assert 'cli_auth_credentials_store="file"' in call.argv
    assert call.argv[call.argv.index("--sandbox") + 1] == "read-only"
    assert {"--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check", "--json"} <= set(call.argv)
    assert call.argv[-1] == "-" and call.input == b"Review all supplied book evidence."
    assert "Review all" not in " ".join(call.argv)
    for child in fake.calls:
        assert all(key not in child.kwargs["env"] for key in leaked_env if key != "CODEX_HOME")
        assert child.kwargs["env"]["CODEX_HOME"] == str(tmp_path / "auth-cache")
        assert child.kwargs["start_new_session"]
    saved = receipt(tmp_path)
    assert saved["status"] == "completed" and saved["submitted"]
    assert saved["usage"] == {"input_tokens": 123, "cached_input_tokens": 45, "output_tokens": 67}
    assert saved["thread_id"] == "thread-fake-01"
    assert "SECRET_TRANSCRIPT_VALUE" not in json.dumps(saved) and "DO_NOT_KEEP" not in json.dumps(saved)
    directory = cr.request_directory(tmp_path / "work", "chapter-1")
    assert directory.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in directory.iterdir())


@pytest.mark.parametrize("auth_text,returncode", [
    ("Logged in using an API key", 0), ("Not logged in", 1),
    ("Logged in using ChatGPT\nLogged in using API key", 0),
    ("ChatGPT", 0), ("Logged in using ChatGPT", 1),
])
def test_authentication_failure_cannot_spend_or_fall_back(fake, tmp_path, auth_text, returncode):
    fake.auth_text, fake.auth_returncode = auth_text + " SECRET_TOKEN", returncode
    with pytest.raises(AstraReviewError, match="subscription login"):
        run(tmp_path)
    assert len(fake.calls) == 1 and fake.calls[0].is_auth
    assert receipt(tmp_path)["submitted"] is False
    assert "SECRET_TOKEN" not in json.dumps(receipt(tmp_path))
    fake.auth_text, fake.auth_returncode = "Logged in using ChatGPT", 0
    assert run(tmp_path) == RESULT
    assert len(fake.calls) == 3


def test_prompt_schema_and_result_are_pinned(fake, tmp_path):
    assert run(tmp_path) == RESULT
    with pytest.raises(AstraReviewError, match="different evidence"):
        cr.run_structured("Changed book.", SCHEMA, tmp_path / "work", request_id="chapter-1")
    changed_schema = copy.deepcopy(SCHEMA)
    changed_schema["properties"]["ready"] = {"type": "integer"}
    with pytest.raises(AstraReviewError, match="different evidence"):
        cr.run_structured("Review all supplied book evidence.", changed_schema,
                          tmp_path / "work", request_id="chapter-1")
    directory = cr.request_directory(tmp_path / "work", "chapter-1")
    (directory / "result.json").write_text(json.dumps({**RESULT, "ready": False}))
    with pytest.raises(AstraReviewError, match="has changed"):
        run(tmp_path)
    assert len(fake.calls) == 2


@pytest.mark.parametrize("output", ["not json", "{}", "[]", '{"ready":true,"reason":"ok","extra":0}',
                                  '{"ready":1,"reason":"ok"}'])
def test_invalid_output_is_operational_and_never_replayed(fake, tmp_path, output):
    fake.output = output
    with pytest.raises(AstraReviewError, match="invalid structured") as failure:
        run(tmp_path)
    assert failure.value.operational and not failure.value.retryable
    assert receipt(tmp_path)["failure_category"] == "invalid_output"
    assert "editorial_verdict" not in receipt(tmp_path)
    with pytest.raises(AstraReviewError, match="previous Codex review"):
        run(tmp_path)
    assert len(fake.calls) == 2


def test_missing_final_output_blocks_without_replay(fake, tmp_path):
    fake.write_output = False
    with pytest.raises(AstraReviewError, match="invalid structured"):
        run(tmp_path)
    with pytest.raises(AstraReviewError, match="previous Codex review"):
        run(tmp_path)
    assert len(fake.calls) == 2


def test_timeout_kills_process_group_and_blocks_replay(fake, tmp_path, monkeypatch):
    fake.timeout = True
    killed = []
    monkeypatch.setattr(cr.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    with pytest.raises(AstraReviewError, match="timeout"):
        run(tmp_path)
    assert killed == [(7654321, cr.signal.SIGTERM)]
    assert receipt(tmp_path)["failure_category"] == "timeout"
    with pytest.raises(AstraReviewError, match="previous Codex review"):
        run(tmp_path)
    assert len(fake.calls) == 2


def test_rate_limit_is_generic_and_never_leaks_raw_diagnostics(fake, tmp_path):
    fake.returncode = 1
    fake.stderr = "Usage limit reached. SECRET_AUTH_VALUE user@email.invalid"
    with pytest.raises(AstraReviewError, match="subscription_limit") as failure:
        run(tmp_path)
    assert "SECRET_AUTH_VALUE" not in str(failure.value)
    assert "SECRET_AUTH_VALUE" not in json.dumps(receipt(tmp_path))
    with pytest.raises(AstraReviewError, match="previous Codex review"):
        run(tmp_path)
    assert len(fake.calls) == 2


def test_explicit_reset_archives_exited_failure_and_authorizes_one_new_attempt(fake, tmp_path):
    fake.returncode, fake.stderr = 1, "Usage limit reached."
    with pytest.raises(AstraReviewError, match="subscription_limit"):
        run(tmp_path)
    old_receipt = receipt(tmp_path)
    assert old_receipt["process_exited"] and old_receipt["exit_code"] == 1
    reset = cr.reset_failed_request(tmp_path / "work", "chapter-1",
                                    reason="Subscription allowance reset; operator authorized another attempt.")
    assert len(fake.calls) == 2  # Reset itself never calls Codex.
    archive = Path(reset["archive"])
    assert json.loads((archive / "receipt.json").read_text()) == old_receipt
    assert json.loads((archive / "final.json").read_text()) == RESULT
    assert reset["previous_attempt"] == 1 and reset["next_attempt"] == 2
    assert receipt(tmp_path)["submitted"] is False
    fake.returncode, fake.stderr = 0, ""
    assert run(tmp_path) == RESULT
    assert run(tmp_path) == RESULT
    assert len(fake.calls) == 4
    assert receipt(tmp_path)["attempt"] == 2
    assert receipt(tmp_path)["retry_authorization"]["previous_receipt_sha256"] == cr._hash(old_receipt)
    with pytest.raises(AstraReviewError, match="confirmed exited"):
        cr.reset_failed_request(tmp_path / "work", "chapter-1", reason="Try again")


@pytest.mark.parametrize("mutate", [
    {"status": "running"}, {"process_exited": False}, {"exit_code": None},
    {"exit_code": True}, {"status": "completed"},
])
def test_explicit_reset_refuses_ambiguous_or_completed_generations(fake, tmp_path, mutate):
    fake.returncode = 1
    with pytest.raises(AstraReviewError):
        run(tmp_path)
    old = receipt(tmp_path)
    old.update(mutate)
    directory = cr.request_directory(tmp_path / "work", "chapter-1")
    (directory / "receipt.json").write_text(json.dumps(old))
    with pytest.raises(AstraReviewError, match="confirmed exited"):
        cr.reset_failed_request(tmp_path / "work", "chapter-1", reason="Operator requested retry")
    assert receipt(tmp_path) == old and len(fake.calls) == 2
    assert not (directory / "attempts").exists()


def test_explicit_reset_requires_operator_reason(fake, tmp_path):
    with pytest.raises(AstraReviewError, match="operator reason"):
        cr.reset_failed_request(tmp_path / "work", "chapter-1", reason=" ")
    assert fake.calls == []


def test_spawn_failure_is_safe_to_try_after_repair(fake, tmp_path):
    fake.start_error = True
    with pytest.raises(AstraReviewError, match="could not start"):
        run(tmp_path)
    assert receipt(tmp_path)["submitted"] is False
    fake.start_error = False
    assert run(tmp_path) == RESULT
    assert len(fake.calls) == 4


def test_interrupted_record_is_not_resubmitted(fake, tmp_path):
    run(tmp_path)
    directory = cr.request_directory(tmp_path / "work", "chapter-1")
    record = receipt(tmp_path)
    record.update(status="running")
    (directory / "receipt.json").write_text(json.dumps(record))
    with pytest.raises(AstraReviewError, match="previous Codex review"):
        run(tmp_path)
    assert len(fake.calls) == 2


def test_auth_cache_lock_serializes_separate_workspaces(fake, tmp_path, monkeypatch):
    auth_home = tmp_path / "auth-cache"
    auth_home.mkdir()
    with (auth_home / ".galley-review.lock").open("w") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        clock = iter([0, 0, 2])
        monkeypatch.setattr(cr.time, "monotonic", lambda: next(clock))
        monkeypatch.setattr(cr.time, "sleep", lambda delay: None)
        with pytest.raises(AstraReviewError, match="reviewer is busy"):
            run(tmp_path, timeout_seconds=1)
        assert fake.calls == []
    assert not (tmp_path / "work").exists()


def test_dedicated_auth_home_never_inherits_interactive_cache(monkeypatch, tmp_path):
    monkeypatch.delenv("GALLEY_CODEX_HOME", raising=False)
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    monkeypatch.delenv("FLY_MACHINE_ID", raising=False)
    monkeypatch.setenv("CODEX_HOME", "/interactive/cache")
    monkeypatch.setattr(cr.Path, "home", classmethod(lambda cls: tmp_path))
    assert cr.codex_home() == tmp_path / ".galley" / "codex"
    monkeypatch.setenv("FLY_MACHINE_ID", "worker")
    assert cr.codex_home() == Path("/data/galley-codex")
    monkeypatch.setenv("GALLEY_CODEX_HOME", str(tmp_path / "worker-auth"))
    assert cr.codex_home() == tmp_path / "worker-auth"


@pytest.mark.parametrize("schema", [
    {"type": "number"}, {"type": "object", "properties": {}},
    {**SCHEMA, "additionalProperties": True},
    {**SCHEMA, "required": ["ready"]}, {**SCHEMA, "anyOf": []},
    {"type": "array", "items": {"type": "string"}},
])
def test_unsupported_schema_is_rejected_before_subprocess(fake, tmp_path, schema):
    with pytest.raises(AstraReviewError):
        cr.run_structured("Review.", schema, tmp_path, request_id="bad-schema")
    assert fake.calls == []


def test_request_ids_cannot_escape_work_directory(fake, tmp_path):
    request_id = "../../auth-cache/overwritten"
    assert cr.run_structured("Review.", SCHEMA, tmp_path / "work", request_id=request_id) == RESULT
    request_dir = cr.request_directory(tmp_path / "work", request_id)
    assert request_dir.parent == tmp_path / "work" / "codex-requests"
    assert not (tmp_path / "auth-cache" / "overwritten").exists()


def test_transcript_extraction_discards_oversized_and_unrecognized_events():
    raw = b"x" * (cr._TAIL_BYTES + 20) + b"\n"
    raw += json.dumps({"type": "SECRET_TOKEN"}).encode() + b"\n"
    raw += json.dumps({"type": "turn.completed", "usage": {"input_tokens": True,
                                                           "output_tokens": 8}}).encode() + b"\n"
    assert cr._safe_events(io.BytesIO(raw)) == {"event_counts": {"turn.completed": 1},
                                              "usage": {"output_tokens": 8}}
