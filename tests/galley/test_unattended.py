"""No person participates between intake and final handoff."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from docproof.__main__ import main
from galley import driver as gd
from galley.journal import render_journal
from galley.unattended import NOTES_NAME, UNATTENDED_ENV, WORKSPACE_ENV
from .test_driver import FakeSpawner, FIXTURE, MECH_PLAN, _deliverable, _driver, _plan


@pytest.fixture
def book(tmp_path):
    path = tmp_path / "Ford - Book 1.docx"
    path.write_bytes(FIXTURE.read_bytes())
    return path


@pytest.fixture
def workspace(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws)
    _deliverable(ws)
    return ws


class MeteredSpawner(FakeSpawner):
    def __call__(self, spec):
        outcome = super().__call__(spec)
        outcome.num_turns = 3
        return outcome


def test_unattended_ask_records_unicode_in_workspace_without_email(
        workspace, tmp_path, monkeypatch, capsys):
    import app.watch.notify as notify
    monkeypatch.setenv(UNATTENDED_ENV, "1")
    monkeypatch.setenv(WORKSPACE_ENV, str(workspace))
    monkeypatch.setenv("GALLEY_BRAIN_PHASE", "settle")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(notify, "send_question", lambda *a, **k: pytest.fail("email sent"))
    body = tmp_path / "note.md"
    body.write_text("Aragón: preserve the source wording; verify the anchor.")
    assert main(["galley", "ask", "Anchor", "--file", str(body), "--book", "Aragón"]) == 0
    note = json.loads((workspace / "runs" / "driver" / NOTES_NAME).read_text())
    assert note["body"] == body.read_text()
    assert note["phase"] == "settle"
    assert "no reply expected" in note["disposition"]
    assert "no message was sent" in capsys.readouterr().out


def test_unattended_ask_never_waits_for_stdin(monkeypatch):
    import sys
    monkeypatch.setenv(UNATTENDED_ENV, "1")
    class NoRead:
        def read(self):
            pytest.fail("waiting for stdin")
    monkeypatch.setattr(sys, "stdin", NoRead())
    assert main(["galley", "ask", "missing body"]) == 2


def test_explicit_interactive_ask_still_uses_the_selected_channel(monkeypatch):
    import app.watch.notify as notify
    monkeypatch.delenv(UNATTENDED_ENV, raising=False)
    sent = []
    monkeypatch.setattr(notify, "send_question",
                        lambda *a, **k: sent.append((a, k)) or "press@example.test")
    assert main(["galley", "ask", "Plan", "--body", "An explicitly interactive question"]) == 0
    assert len(sent) == 1


@pytest.mark.parametrize("notes_file", ["QUESTIONS.md", "runs/driver/" + NOTES_NAME])
def test_a_question_gets_autonomous_triage_then_the_book_continues(
        book, tmp_path, workspace, notes_file):
    class Notes(MeteredSpawner):
        def __call__(self, spec):
            if spec.phase == "sweeps":
                path = workspace / notes_file
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a") as stream:
                    stream.write("Match count exceeds estimate; narrow the sweep.\n")
            return super().__call__(spec)
    spawn = Notes(workspace)
    result = _driver(book, tmp_path, spawn=spawn,
                     ask=lambda *a: pytest.fail("asked a human"),
                     sleep=lambda *a: pytest.fail("waiting for a reply")).run()
    assert result.outcome == "done", result.reason
    assert not result.asked and not result.recovery_exhausted
    sweeps = [s for s in spawn.calls if s.phase == "sweeps"]
    assert len(sweeps) == 2  # A second local note cannot create a question loop.
    assert sweeps[1].max_turns == sweeps[0].max_turns - 3
    assert "AUTONOMOUS RECOVERY" in sweeps[1].prompt
    assert "Do not rerun completed paid work" in sweeps[1].prompt
    assert "bypass a failed gate" in sweeps[1].prompt
    assert sweeps[0].log_path != sweeps[1].log_path
    assert all(s.env[UNATTENDED_ENV] == "1" for s in spawn.calls)
    journal = render_journal(workspace / "runs", workspace=workspace)
    assert "Automatic recovery" in journal
    assert "Match count exceeds estimate" in journal


@pytest.mark.parametrize("kind", ["exit", "state"])
def test_an_incomplete_phase_recovers_within_its_original_limits(
        book, tmp_path, workspace, kind):
    class Recover(MeteredSpawner):
        def __call__(self, spec):
            if not self.calls:
                self.calls.append(spec)
                return gd.PhaseResult(spec.phase, 3 if kind == "exit" else 0,
                                      spec.log_path, "bad command; inspect the existing checkpoint",
                                      num_turns=7)
            return super().__call__(spec)
    clock = iter([0.0, 12.0, 12.0, 20.0])
    spawn = Recover(workspace)
    result = _driver(book, tmp_path, spawn=spawn, only_phases=["ladder"],
                     max_turns=20, timeout_s=60, clock=lambda: next(clock)).run()
    assert result.outcome == "done", result.reason
    assert len(spawn.calls) == 2
    recovery = spawn.calls[1]
    assert recovery.max_turns == 13
    assert recovery.timeout_s == 48
    assert recovery.argv[recovery.argv.index("--max-turns") + 1] == "13"
    assert recovery.argv[recovery.argv.index("-p") + 1] == recovery.prompt


def test_two_real_failures_block_without_inventing_a_human_question(
        book, tmp_path, workspace):
    spawn = MeteredSpawner(workspace, fail="ladder")
    result = _driver(book, tmp_path, spawn=spawn, only_phases=["ladder"],
                     astra_review=True).run()
    assert result.outcome == "blocked"
    assert result.recovery_exhausted and not result.asked
    assert spawn.phases == ["ladder", "ladder"]
    assert "exited 3" in result.reason
    assert not (workspace / "runs" / "outcome.json").exists()
    assert json.loads((workspace / "runs" / "driver" / "blocked.json").read_text())[
        "editorial_verdict_unchanged"]


@pytest.mark.parametrize("limit,usage", [("max_turns", 10), ("timeout", 1), (None, None)])
def test_recovery_never_grants_fresh_caps_or_guesses_missing_usage(
        book, tmp_path, workspace, limit, usage):
    def fail(spec):
        return gd.PhaseResult(spec.phase, 3, spec.log_path, "failure",
                              limit=limit, num_turns=usage)
    result = _driver(book, tmp_path, spawn=fail, only_phases=["ladder"],
                     astra_review=True, max_turns=10, timeout_s=60).run()
    assert result.outcome == "blocked"
    assert len(result.phases) == 1
    assert result.recovery_exhausted


def test_credentials_are_retried_by_the_service_not_a_question_session(
        book, tmp_path, workspace):
    calls = []
    def fail(spec):
        calls.append(spec)
        return gd.PhaseResult(spec.phase, 1, spec.log_path, "token expired",
                              limit="credentials", num_turns=0)
    with pytest.raises(gd.CredentialsError):
        _driver(book, tmp_path, spawn=fail, only_phases=["ladder"]).run()
    assert len(calls) == 1


def test_a_rejected_draft_is_corrected_before_any_paid_phase(
        book, tmp_path, workspace):
    _plan(workspace, MECH_PLAN.replace("$2.80", "$20.00"))
    class Replan(MeteredSpawner):
        def __call__(self, spec):
            if "automatic plan gate refused" in spec.prompt:
                _plan(workspace)
            return super().__call__(spec)
    spawn = Replan(workspace)
    result = _driver(book, tmp_path, spawn=spawn).run()
    assert result.outcome == "done", result.reason
    assert spawn.phases[:3] == ["profile", "profile", "approve"]
    assert spawn.calls[1].max_turns == spawn.calls[0].max_turns - 3
    assert result.gate["approved"]


def test_recovery_cannot_approve_a_plan_that_stays_over_budget(
        book, tmp_path, workspace):
    _plan(workspace, MECH_PLAN.replace("$2.80", "$20.00"))
    spawn = MeteredSpawner(workspace)
    result = _driver(book, tmp_path, spawn=spawn, astra_review=True).run()
    assert result.outcome == "blocked"
    assert not result.gate["approved"]
    assert spawn.phases == ["profile", "profile"]
    assert not (workspace / "approval.json").exists()


def test_a_frozen_approval_is_never_replanned_by_the_driver(
        book, tmp_path, workspace):
    _plan(workspace, MECH_PLAN.replace("$2.80", "$20.00"))
    approval = workspace / "approval.json"
    frozen = '{"config_sha256":"frozen", "max_spend_usd":10}'
    approval.write_text(frozen)
    spawn = MeteredSpawner(workspace)
    result = _driver(book, tmp_path, spawn=spawn, only_phases=["approve"],
                     astra_review=True).run()
    assert result.outcome == "blocked"
    assert not spawn.calls
    assert approval.read_text() == frozen


def test_repeated_notes_cannot_hide_a_missing_required_checkpoint(
        book, tmp_path, workspace):
    class Notes(MeteredSpawner):
        def __call__(self, spec):
            with (workspace / "QUESTIONS.md").open("a") as stream:
                stream.write("Unable to complete the required work.\n")
            return super().__call__(spec)
    spawn = Notes(workspace, skip_state="ladder")
    result = _driver(book, tmp_path, spawn=spawn, only_phases=["ladder"],
                     astra_review=True).run()
    assert result.outcome == "blocked"
    assert result.recovery_exhausted and not result.asked
    assert "did not advance the ledger" in result.reason
    assert len(spawn.calls) == 2


@pytest.mark.parametrize("broken", ["missing_plan", "invalid_yaml"])
def test_missing_or_malformed_drafts_get_the_same_bounded_repair(
        book, tmp_path, workspace, broken):
    config = workspace / "runs" / "mech.yaml"
    if broken == "missing_plan":
        (workspace / "PLAN.md").unlink()
    else:
        config.write_text("smoothing: [invalid YAML")
    class Replan(MeteredSpawner):
        def __call__(self, spec):
            if "automatic plan gate refused" in spec.prompt:
                _plan(workspace)
                config.write_text("smoothing: {enabled: false}\nrewrite: {enabled: false}\n")
            return super().__call__(spec)
    spawn = Replan(workspace)
    result = _driver(book, tmp_path, spawn=spawn).run()
    assert result.outcome == "done", result.reason
    assert spawn.phases[:3] == ["profile", "profile", "approve"]


def test_changing_the_profile_during_recovery_cannot_enlarge_caps(
        book, tmp_path, workspace):
    (workspace / "profile.json").write_text('{"word_count":40000}')
    class Recover(MeteredSpawner):
        def __call__(self, spec):
            if not self.calls:
                self.calls.append(spec)
                (workspace / "profile.json").write_text('{"word_count":200000}')
                return gd.PhaseResult(spec.phase, 3, spec.log_path, "bad artifact",
                                      num_turns=7)
            return super().__call__(spec)
    spawn = Recover(workspace)
    result = _driver(book, tmp_path, spawn=spawn, only_phases=["ladder"]).run()
    assert result.outcome == "done"
    assert spawn.calls[1].max_turns == spawn.calls[0].max_turns - 7
    assert spawn.calls[1].timeout_s <= spawn.calls[0].timeout_s


def test_an_approved_state_cannot_be_replanned_even_if_the_manifest_is_missing(
        book, tmp_path, workspace):
    _plan(workspace, MECH_PLAN.replace("$2.80", "$20.00"))
    spawn = MeteredSpawner(workspace)
    spawn._advance("plan_approved")
    result = _driver(book, tmp_path, spawn=spawn, only_phases=["approve"],
                     astra_review=True).run()
    assert result.outcome == "blocked"
    assert not spawn.calls


def test_aragon_closing_its_old_engine_question_cannot_hold_the_book(
        book, tmp_path, workspace):
    questions = workspace / "QUESTIONS.md"
    questions.write_text("## ENGINE DEFECT: superseded intake stamp\nOld resume failed.\n")
    class CloseQuestion(MeteredSpawner):
        def __call__(self, spec):
            outcome = super().__call__(spec)
            if spec.phase == "sweeps" and "AUTONOMOUS RECOVERY" not in spec.prompt:
                with questions.open("a") as stream:
                    stream.write("\n### RESOLVED — fixed by the deployed release\n"
                                 "This entry is closed. It is not an open question and needs no answer.\n")
                outcome.num_turns = 75
            return outcome
    spawn = CloseQuestion(workspace)
    result = _driver(book, tmp_path, spawn=spawn).run()
    assert result.outcome == "done", result.reason
    assert not result.asked and not result.recovery_exhausted
    assert "ladder" in spawn.phases
    assert [s.max_turns for s in spawn.calls if s.phase == "sweeps"] == [120, 45]
