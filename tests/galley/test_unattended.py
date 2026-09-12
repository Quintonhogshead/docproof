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


# --- continuation after a cap ------------------------------------------------

class CappedThenDone(MeteredSpawner):
    """The first session hits its turn cap after writing evidence; the next
    completes. Records the Claude Code session id like the real spawner."""

    def __init__(self, workspace, *, caps=1, progress=True, limit="max_turns"):
        super().__init__(workspace)
        self.caps, self.progress, self.limit = caps, progress, limit

    def __call__(self, spec):
        if len(self.calls) < self.caps:
            self.calls.append(spec)
            spec.log_path.parent.mkdir(parents=True, exist_ok=True)
            spec.log_path.write_text("cut off\n", encoding="utf-8")
            if self.progress:
                out = spec.workspace / "runs" / "final" / "run.log"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(f"read {len(self.calls)}\n", encoding="utf-8")
            return gd.PhaseResult(spec.phase, 1, spec.log_path, "cut off",
                                  limit=self.limit, num_turns=spec.max_turns,
                                  session_id=f"sess-{len(self.calls)}")
        return super().__call__(spec)


def test_a_capped_session_that_advanced_the_book_gets_one_bounded_continuation(
        book, tmp_path, workspace, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    spawn = CappedThenDone(workspace)
    result = _driver(book, tmp_path, spawn=spawn, only_phases=["ladder"],
                     max_turns=80, timeout_s=3600, clock=lambda: 0.0).run()
    assert result.outcome == "done", result.reason
    assert spawn.phases == ["ladder", "ladder"]
    first, second = spawn.calls
    assert first.max_turns == 80
    # Half the base cap, granted on top of the exhausted original; the wall
    # clock the first session never used carries over plus its own half.
    assert second.max_turns == 40
    assert second.timeout_s == 3600 + 1800
    assert second.argv[second.argv.index("--max-turns") + 1] == "40"
    assert "--resume" not in second.argv          # no transcript on disk
    assert second.prompt.startswith(first.prompt)  # fresh session, full prompt
    assert result.recovery[0]["resumed_session"] == ""
    budget = json.loads((workspace / "runs" / "driver" / "execution-budget.json").read_text())
    assert [g["turns"] for g in budget["grants"]] == [40]


def test_a_continuation_resumes_the_conversation_when_its_transcript_exists(
        book, tmp_path, workspace, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    encoded = gd.re.sub(r"[^A-Za-z0-9]", "-", str(workspace.resolve()))
    path = home / ".claude" / "projects" / encoded / "sess-1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{}\n")
    assert gd.session_transcript(workspace, "sess-1", str(home)) == path
    spawn = CappedThenDone(workspace)
    result = _driver(book, tmp_path, spawn=spawn, only_phases=["ladder"],
                     max_turns=80, timeout_s=3600).run()
    assert result.outcome == "done", result.reason
    second = spawn.calls[1]
    assert second.argv[second.argv.index("--resume") + 1] == "sess-1"
    assert second.argv.index("--resume") == second.argv.index("-p") + 2
    assert not second.prompt.startswith(spawn.calls[0].prompt)
    assert "continue the same phase" in second.prompt
    assert result.recovery[0]["resumed_session"] == "sess-1"


@pytest.mark.parametrize("limit", ["max_turns", "timeout"])
def test_a_capped_session_that_changed_nothing_earns_no_continuation(
        book, tmp_path, workspace, limit):
    spawn = CappedThenDone(workspace, caps=3, progress=False, limit=limit)
    result = _driver(book, tmp_path, spawn=spawn, only_phases=["ladder"],
                     astra_review=True, max_turns=80, timeout_s=3600).run()
    assert result.outcome == "blocked"
    assert result.recovery_exhausted
    assert spawn.phases == ["ladder"]
    assert "grants" not in json.loads(
        (workspace / "runs" / "driver" / "execution-budget.json").read_text())


def test_continuations_are_bounded_and_durable_across_resumes(
        book, tmp_path, workspace):
    spawn = CappedThenDone(workspace, caps=10)
    result = _driver(book, tmp_path, spawn=spawn, only_phases=["ladder"],
                     astra_review=True, max_turns=80, timeout_s=3600).run()
    assert result.outcome == "blocked"
    assert result.recovery_exhausted
    assert spawn.phases == ["ladder"] * (1 + gd.RECOVERY_MAX_GRANTS)
    # A later poll resumes the same source revision: the grants are spent.
    again = CappedThenDone(workspace, caps=10)
    resumed = _driver(book, tmp_path, spawn=again, only_phases=["ladder"],
                      astra_review=True, max_turns=80, timeout_s=3600).run()
    assert resumed.outcome == "blocked" and resumed.recovery_exhausted
    assert again.phases == []
    assert "exhausted" in resumed.reason


def test_a_recovery_below_the_floor_stops_with_evidence_instead_of_launching(
        book, tmp_path, workspace):
    class Recover(MeteredSpawner):
        def __call__(self, spec):
            if not self.calls:
                self.calls.append(spec)
                return gd.PhaseResult(spec.phase, 3, spec.log_path,
                                      "bad command", num_turns=195)
            return super().__call__(spec)
    spawn = Recover(workspace)
    result = _driver(book, tmp_path, spawn=spawn, only_phases=["ladder"],
                     astra_review=True, max_turns=200, timeout_s=3600).run()
    assert result.outcome == "blocked"
    assert result.recovery_exhausted
    assert spawn.phases == ["ladder"]
    assert "below the recovery floor" in result.reason


def test_sessions_carry_the_phase_wall_clock_as_their_bash_ceiling(
        book, tmp_path, workspace):
    spawn = MeteredSpawner(workspace)
    _driver(book, tmp_path, spawn=spawn, only_phases=["ladder"],
            timeout_s=5400).run()
    env = spawn.calls[0].env
    assert env["BASH_MAX_TIMEOUT_MS"] == env["BASH_DEFAULT_TIMEOUT_MS"] == str(5400 * 1000)


# --- the edit guard -----------------------------------------------------------

def test_verify_and_settle_sessions_install_the_edit_guard_hook(
        book, tmp_path, workspace):
    spawn = MeteredSpawner(workspace)
    _driver(book, tmp_path, spawn=spawn, only_phases=["ladder", "verify", "settle"]).run()
    by_phase = {c.phase: c.argv for c in spawn.calls}
    assert "--settings" not in by_phase["ladder"]
    for phase in gd.EDIT_GUARDED_PHASES:
        argv = by_phase[phase]
        settings = json.loads(argv[argv.index("--settings") + 1])
        hook = settings["hooks"]["PreToolUse"][0]
        assert hook["matcher"] == "Edit|Write|MultiEdit|NotebookEdit"
        assert hook["hooks"][0]["command"].endswith("-m galley.edit_guard")
    off = MeteredSpawner(workspace)
    _driver(book, tmp_path, spawn=off, only_phases=["settle"], edit_guard=False).run()
    assert "--settings" not in off.calls[0].argv


@pytest.mark.parametrize("tool,path,blocked", [
    ("Edit", "/ws/runs/curated/findings.json", True),
    ("Write", "/ws/runs/final/settlement.json", True),
    ("Edit", "/ws/runs/final/Ford - Book 1 - proofread.docx", True),
    ("Write", "/ws/state.json", True),
    ("Write", "/ws/runs/SETTLE.md", False),
    ("Write", "/ws/QUESTIONS.md", False),
    ("Write", "/home/.claude/projects/x/memory/MEMORY.md", False),
    ("Bash", "/ws/runs/curated/findings.json", False),
    ("Read", "/ws/runs/curated/findings.json", False),
])
def test_edit_guard_refuses_engine_evidence_and_manuscripts_only(tool, path, blocked):
    from galley import edit_guard
    code, message = edit_guard.decide({"tool_name": tool, "tool_input": {"file_path": path}})
    assert (code == edit_guard.BLOCK_EXIT) is blocked
    assert bool(message) is blocked
    if blocked:
        assert "docproof galley" in message


def test_edit_guard_main_reads_the_hook_payload_from_stdin(monkeypatch, capsys):
    import io
    from galley import edit_guard
    payload = json.dumps({"tool_name": "Edit", "tool_input": {
        "file_path": "/ws/runs/curated/findings.json"}})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert edit_guard.main() == edit_guard.BLOCK_EXIT
    assert "findings.json" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert edit_guard.main() == 0


# --- settle scales with what verify left open ---------------------------------

def test_settle_caps_scale_with_open_items_not_only_words(book, tmp_path, workspace):
    driver = _driver(book, tmp_path, only_phases=["settle"])
    assert driver.open_items() is None
    assert driver.turns_for("settle") == gd.PHASE_MAX_TURNS["settle"]
    run = workspace / "runs" / "final"
    run.mkdir(parents=True, exist_ok=True)
    (run / "findings.json").write_text("{}")
    (run / "change_verify.json").write_text(json.dumps(
        {"problems": [{"i": n} for n in range(60)]}))
    (run / "finished_walk.json").write_text(json.dumps(
        {"residuals": [{"i": n} for n in range(60)]}))
    assert driver.open_items() == 120
    assert driver.scale_for("settle") == 3.0
    assert driver.turns_for("settle") == gd.PHASE_MAX_TURNS["settle"] * 3
    assert driver.timeout_for("settle") == gd.PHASE_TIMEOUT_S["settle"] * 3
    # Verify does not scale with settle's items, and the ceiling still holds.
    assert driver.scale_for("verify") == 1.0
    assert gd.items_factor(10_000) == gd.LENGTH_SCALE_MAX
    assert gd.items_factor(None) == 1.0
    # An explicit override is still taken exactly.
    assert _driver(book, tmp_path, max_turns=50).turns_for("settle") == 50
