"""A rejected subscription token is the machine's problem, not the book's.

On 2026-09-07 the Fly agent's `GALLEY_OAUTH_TOKEN` died; every profile phase
exited 1 with "Failed to authenticate. API Error: 401 OAuth access token is
invalid", and the driver wrote each book off as needs_human. Nothing had been
read. The driver must recognise that output, write no verdict, and raise so
the agent holds the queue instead.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from galley import driver as gd
from .test_driver import FIXTURE, FakeSpawner, MECH_PLAN, _driver, _plan

AUTH_TAIL = ("# galley driver: phase profile at 2026-09-07T13:00:00Z\n"
             "Failed to authenticate. API Error: 401 OAuth access token is "
             "invalid.\n")


@pytest.fixture()
def book(tmp_path) -> Path:
    dest = tmp_path / "Ford - Book 1.docx"
    dest.write_bytes(FIXTURE.read_bytes())
    return dest


def _ws(book: Path, tmp_path: Path) -> Path:
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws, MECH_PLAN)
    return ws


class RefusesToSignIn(FakeSpawner):
    """Claude Code with a dead token: one line of output, exit 1."""

    def __call__(self, spec):
        self.calls.append(spec)
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        spec.log_path.write_text(AUTH_TAIL, encoding="utf-8")
        tail = gd.transcript_tail(gd.tail_of(spec.log_path))
        return gd.PhaseResult(spec.phase, 1, spec.log_path, tail,
                              limit=gd.session_limit(None, tail))


@pytest.mark.parametrize("line", [
    "Failed to authenticate. API Error: 401 OAuth access token is invalid.",
    "API Error: 401 {\"type\":\"error\",\"error\":{\"type\":"
    "\"authentication_error\",\"message\":\"invalid x-api-key\"}}",
    "Not logged in. Please run /login.",
    "OAuth token has expired",
])
def test_the_sign_in_failures_claude_code_prints_are_recognised(line):
    assert gd.detect_credential_failure(line)


@pytest.mark.parametrize("line", [
    "phase profile\nline two\nline three",
    "Reached max turns (40)",
    "the author wrote 'unauthorized' in chapter 4",
])
def test_ordinary_output_is_not_a_credentials_failure(line):
    assert not gd.detect_credential_failure(line)


def test_session_limit_reads_the_structured_error_first():
    result = {"type": "result", "subtype": "error_during_execution",
              "is_error": True,
              "result": "Failed to authenticate. API Error: 401 OAuth "
                        "access token is invalid."}
    assert gd.session_limit(result, "") == "credentials"
    assert gd.session_limit(None, AUTH_TAIL) == "credentials"
    assert gd.session_limit({"type": "result", "subtype": "success",
                             "is_error": False}, "all good") is None


def test_a_rejected_token_raises_and_writes_no_verdict(book, tmp_path):
    ws = _ws(book, tmp_path)
    spawn = RefusesToSignIn(ws)
    events = []
    drv = _driver(book, tmp_path, spawn=spawn, progress=events.append)

    with pytest.raises(gd.CredentialsError) as caught:
        drv.run()

    assert "CLAUDE_CODE_OAUTH_TOKEN" in str(caught.value)
    assert "401 OAuth access token is invalid" in str(caught.value)
    # One session was attempted; nothing after it.
    assert spawn.phases == ["profile"]
    # No verdict, no hand-off: the book was never read.
    assert not (ws / "runs" / "outcome.json").exists()
    assert not (ws / "handoff").exists()
    # The agent hears "credentials", never "stopped".
    kinds = [e["event"] for e in events]
    assert "credentials" in kinds and "stopped" not in kinds
    # The run state did not move, so a resume starts the same phase.
    from galley.state_machine import RunStateMachine
    assert not RunStateMachine.load(ws / "state.json").current
    assert isinstance(caught.value, gd.DriverError)
