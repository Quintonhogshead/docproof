"""The unattended driver: sequencing, the plan gate, failure handling, state
advancement, and the DocWatch hand-off — all against a fake spawner and a temp
workspace, so no headless session and no Drive call is ever made.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from galley import driver as gd
from galley.state_machine import RunStateMachine

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tiny_novel.docx"

MECH_PLAN = """# PLAN — Ford, Book 1 · general_fiction · 40,000 words

## Wave 0 — $0 deterministic floor
0a. 16 shipped sweeps
0b. Number policy floor

## Wave 1 — mechanical
1a. Luna ladder    $1.40
1b. Sonnet proofread fleet  $0

No copy-edit flights, no merge desk, no wave-2 re-read: mechanical only.

TOTAL est. $2.80 · CAP $10 · stop: $2/finding marginal
"""

COPYEDIT_PLAN = MECH_PLAN.replace(
    "1b. Sonnet proofread fleet  $0",
    "1b. Sonnet proofread fleet  $0\n2a. Copy-edit flights, 6 lenses  $0.30")


class FakeSpawner:
    """Records the phases it was asked to run; optionally fails one, and
    advances the run state machine the way a real session's prompt does."""

    def __init__(self, workspace: Path, *, fail: str | None = None,
                 rc: int = 3, skip_state: str | None = None):
        self.workspace = workspace
        self.fail = fail
        self.rc = rc
        self.skip_state = skip_state
        self.calls: list[gd.PhaseSpec] = []

    def __call__(self, spec: gd.PhaseSpec) -> gd.PhaseResult:
        self.calls.append(spec)
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        spec.log_path.write_text(f"phase {spec.phase}\nline two\nline three\n",
                                 encoding="utf-8")
        if spec.phase == self.fail:
            return gd.PhaseResult(spec.phase, self.rc, spec.log_path,
                                  gd.tail_of(spec.log_path))
        state = gd.REQUIRED_STATE.get(spec.phase)
        if state and spec.phase != self.skip_state:
            self._advance(state)
        return gd.PhaseResult(spec.phase, 0, spec.log_path,
                              gd.tail_of(spec.log_path))

    @property
    def phases(self) -> list[str]:
        return [c.phase for c in self.calls]

    def _advance(self, state: str) -> None:
        path = self.workspace / "state.json"
        machine = RunStateMachine.load(path) if path.is_file() \
            else RunStateMachine()
        machine.advance(state, at="2026-09-03T00:00:00Z", by="fake",
                        source_sha256="s", config_sha256="c")
        machine.save(path)


@pytest.fixture()
def book(tmp_path) -> Path:
    """The fixture manuscript under the house name proofing reads: the
    developmental edit, "<surname> - Book 1"."""
    dest = tmp_path / "Ford - Book 1.docx"
    dest.write_bytes(FIXTURE.read_bytes())
    return dest


def _driver(book: Path, tmp_path: Path, **kw) -> gd.Driver:
    kw.setdefault("workspace_root", tmp_path / "ws")
    kw.setdefault("env", {"CLAUDE_CODE_OAUTH_TOKEN": "tok", "PATH": "/bin"})
    kw.setdefault("log", lambda _m: None)
    return gd.Driver(book=book, slug="ford-book-1", **kw)


def _plan(ws: Path, text: str = MECH_PLAN) -> None:
    (ws / "PLAN.md").write_text(text, encoding="utf-8")


# --- phase selection ---------------------------------------------------------

def test_mechanical_order_omits_the_copyedit_phases():
    assert gd.select_phases() == list(gd.MECHANICAL_PHASES)
    assert "flights" not in gd.MECHANICAL_PHASES
    assert "reread" not in gd.MECHANICAL_PHASES
    assert gd.select_phases(mechanical_only=False) == list(gd.ALL_PHASES)


def test_from_phase_slices_the_order():
    assert gd.select_phases(start="verify") == ["verify", "settle", "certify",
                                                "deliver"]


def test_only_phases_stay_in_order():
    assert gd.select_phases(only=["certify", "ladder"]) == ["ladder", "certify"]


def test_a_copyedit_phase_is_refused_in_mechanical_mode():
    with pytest.raises(gd.DriverError, match="copy-edit scope"):
        gd.select_phases(only=["flights"])
    with pytest.raises(gd.DriverError, match="copy-edit scope"):
        gd.phase_prompt("flights", "Book.docx")
    assert gd.phase_prompt("flights", "Book.docx", mechanical_only=False)


def test_the_mechanical_prompt_forbids_the_copyedit_lines():
    prompt = gd.phase_prompt("profile", "Ford - Book Original.docx")
    assert "Ford - Book Original.docx" in prompt
    assert "MECHANICAL PROOFREADING ONLY" in prompt
    assert "NO copy-edit flights" in prompt
    # …and the same prompt without the scope note is the launcher's original.
    plain = gd.phase_prompt("profile", "B.docx", mechanical_only=False)
    assert "MECHANICAL" not in plain
    assert "STOP at the plan gate" in plain


# --- environment isolation ---------------------------------------------------

def test_build_env_strips_api_keys_and_fronts_the_wrapper(tmp_path):
    env = gd.build_env({"CLAUDE_CODE_OAUTH_TOKEN": " to\nken ",
                        "ANTHROPIC_API_KEY": "sk-a", "OPENAI_API_KEY": "sk-o",
                        "PATH": "/usr/bin"}, wrapbin=tmp_path / "bin")
    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "token"
    assert env["PATH"].startswith(f"{tmp_path / 'bin'}:")
    assert env["DOCPROOF_CANDIDATE_APPLY"] == "1"


def test_build_env_refuses_without_the_subscription_token():
    with pytest.raises(gd.DriverError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        gd.build_env({"PATH": "/usr/bin"})


# --- workspace seeding -------------------------------------------------------

def test_seed_workspace_builds_what_launch_sh_builds(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1",
                           workspace_root=tmp_path / "ws")
    for rel in ("CLAUDE.md", "KNOBS.md", "source/Ford - Book 1.docx",
                ".claude/settings.local.json",
                ".claude/skills/draft-plan/SKILL.md", "runs", "deliverable"):
        assert (ws / rel).exists(), rel
    seeded = json.loads((ws / ".claude/settings.local.json").read_text("utf-8"))
    assert "Bash(docproof:*)" in seeded["permissions"]["allow"]
    # Idempotent, and a workspace's own settings win.
    (ws / ".claude/settings.local.json").write_text('{"mine": true}', "utf-8")
    gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    assert json.loads((ws / ".claude/settings.local.json").read_text("utf-8")) \
        == {"mine": True}


# --- the plan gate -----------------------------------------------------------

def test_plan_total_and_scope(tmp_path):
    path = tmp_path / "PLAN.md"
    path.write_text(MECH_PLAN, encoding="utf-8")
    plan = gd.read_plan(path)
    assert plan.total_usd == pytest.approx(2.80)
    assert plan.mechanical_only, plan.copyedit_lines

    path.write_text(COPYEDIT_PLAN, encoding="utf-8")
    plan = gd.read_plan(path)
    assert not plan.mechanical_only
    assert "Copy-edit flights" in plan.copyedit_lines[0]


def test_gate_approves_only_a_priced_mechanical_plan_inside_budget():
    plan = gd.PlanSummary(2.80, [], MECH_PLAN)
    ok, why = gd.gate_decision(plan, 20.0)
    assert ok and "$2.80" in why
    ok, why = gd.gate_decision(plan, 1.0)
    assert not ok and "over the $1.00 budget" in why
    ok, why = gd.gate_decision(gd.PlanSummary(None, [], ""), 20.0)
    assert not ok and "no parseable TOTAL" in why
    ok, why = gd.gate_decision(gd.PlanSummary(1.0, ["2a. flights $9"], ""), 20.0)
    assert not ok and "copy-edit lane in scope" in why


def test_auto_gate_approves_and_runs_the_rest(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _deliverable(ws)
    _plan(ws)
    spawn = FakeSpawner(ws)
    result = _driver(book, tmp_path, spawn=spawn).run()
    assert result.outcome == "done", result.reason
    assert result.exit_code == 0
    assert spawn.phases == list(gd.MECHANICAL_PHASES)
    assert result.gate["approved"] is True
    # The approval is recorded where the `approve` phase reads it back.
    assert "Approved by galley drive (auto)" in (ws / "PLAN.md").read_text()


def test_auto_gate_refuses_a_copyedit_plan_and_stops_before_spending(
        book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws, COPYEDIT_PLAN)
    spawn = FakeSpawner(ws)
    result = _driver(book, tmp_path, spawn=spawn).run()
    assert result.outcome == "needs_human"
    assert result.exit_code == 7
    assert result.stopped_at == "approve"
    assert "copy-edit lane in scope" in result.reason
    # profile ran; nothing past the gate did.
    assert spawn.phases == ["profile"]
    outcome = json.loads((ws / "runs" / "outcome.json").read_text("utf-8"))
    assert outcome["outcome"] == "needs_human"
    assert outcome["hubspot"]["value"] == "Needs Human PR"


def test_auto_gate_refuses_a_plan_over_budget(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws)
    spawn = FakeSpawner(ws)
    result = _driver(book, tmp_path, spawn=spawn, budget_usd=1.0).run()
    assert result.outcome == "needs_human"
    assert "over the $1.00 budget" in result.reason
    assert spawn.phases == ["profile"]


def test_manual_gate_stops_after_the_plan(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws)
    spawn = FakeSpawner(ws)
    result = _driver(book, tmp_path, spawn=spawn, approve="manual").run()
    assert result.outcome == "needs_human"
    assert "--approve manual" in result.reason
    assert spawn.phases == ["profile"]


def test_manual_single_phase_still_runs_that_phase(book, tmp_path):
    """Today's behaviour: `PHASE=approve galley-run.sh` runs approve, because a
    human already handled the gate outside the driver."""
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws)
    spawn = FakeSpawner(ws)
    result = _driver(book, tmp_path, spawn=spawn, approve="manual",
                     only_phases=["approve"]).run()
    assert spawn.phases == ["approve"]
    assert result.outcome == "done"


# --- the email gate ----------------------------------------------------------

def test_email_gate_sends_polls_and_proceeds_on_an_approved_reply(
        book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _deliverable(ws)
    _plan(ws, COPYEDIT_PLAN)               # refused by policy -> escalates
    sent: list[tuple[str, str, str]] = []

    def ask(subject, body, slug):
        sent.append((subject, body, slug))
        return "quinton@atmospherepress.com"

    ticks = {"n": 0}

    def sleep(_s):
        # The human answers on the second poll.
        ticks["n"] += 1
        if ticks["n"] == 2:
            questions = ws / "QUESTIONS.md"
            questions.write_text(questions.read_text("utf-8") + "\nAPPROVED\n",
                                 encoding="utf-8")

    spawn = FakeSpawner(ws)
    result = _driver(book, tmp_path, spawn=spawn, approve="email", ask=ask,
                     sleep=sleep, poll_interval_s=0.0,
                     reply_timeout_s=1e6).run()
    assert sent and sent[0][0] == "Plan gate — ford-book-1"
    assert gd.GATE_TOKEN_PREFIX in sent[0][1]
    assert result.gate["reply"] == "approved"
    assert spawn.phases == list(gd.MECHANICAL_PHASES)
    assert result.outcome == "done"


def test_email_gate_stops_on_a_declined_reply(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws, COPYEDIT_PLAN)

    def ask(_s, _b, _k):
        return "someone@example.com"

    def sleep(_s):
        questions = ws / "QUESTIONS.md"
        questions.write_text(questions.read_text("utf-8") + "\nDECLINED\n",
                             encoding="utf-8")

    spawn = FakeSpawner(ws)
    result = _driver(book, tmp_path, spawn=spawn, approve="email", ask=ask,
                     sleep=sleep, poll_interval_s=0.0,
                     reply_timeout_s=1e6).run()
    assert result.outcome == "needs_human"
    assert "declined by reply" in result.reason
    assert spawn.phases == ["profile"]


def test_email_gate_times_out_into_needs_human(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws, COPYEDIT_PLAN)
    clock = {"t": 0.0}

    def tick():
        clock["t"] += 10.0
        return clock["t"]

    spawn = FakeSpawner(ws)
    result = _driver(book, tmp_path, spawn=spawn, approve="email",
                     ask=lambda *_a: "someone@example.com",
                     sleep=lambda _s: None, clock=tick,
                     poll_interval_s=0.0, reply_timeout_s=20.0).run()
    assert result.outcome == "needs_human"
    assert "no reply" in result.reason
    assert (ws / "QUESTIONS.md").exists()


def test_email_gate_stops_when_the_question_cannot_be_sent(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws, COPYEDIT_PLAN)

    def ask(*_a):
        raise ValueError("No notify address is set.")

    result = _driver(book, tmp_path, spawn=FakeSpawner(ws), approve="email",
                     ask=ask).run()
    assert result.outcome == "needs_human"
    assert "could not be sent" in result.reason
    assert "QUESTIONS.md" in result.reason


def test_reply_after_ignores_words_before_the_marker():
    text = "APPROVED (an older answer)\n\nGALLEY-GATE x\ninstructions\n"
    assert gd.reply_after(text, "GALLEY-GATE x") is None
    assert gd.reply_after(text + "\nAPPROVED\n", "GALLEY-GATE x") == "approved"
    assert gd.reply_after(text + "\n- declined, too expensive\n",
                          "GALLEY-GATE x") == "declined"


# --- failure handling --------------------------------------------------------

def test_a_failing_phase_stops_the_run_and_names_the_log(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws)
    spawn = FakeSpawner(ws, fail="ladder", rc=5)
    result = _driver(book, tmp_path, spawn=spawn).run()
    assert result.outcome == "needs_human"
    assert result.stopped_at == "ladder"
    assert "exited 5" in result.reason
    assert "line three" in result.reason            # the log tail
    assert spawn.phases == ["profile", "approve", "sweeps", "ladder"]
    outcome = json.loads((ws / "runs" / "outcome.json").read_text("utf-8"))
    assert outcome["outcome"] == "needs_human"
    assert outcome["set_by"] == "galley drive"
    ledger = json.loads(
        (ws / "runs" / gd.DRIVER_DIR / "driver.json").read_text("utf-8"))
    assert ledger["stopped_at"] == "ladder"
    assert [p["phase"] for p in ledger["phases"]][-1] == "ladder"


def test_a_phase_that_did_not_advance_the_ledger_stops_the_run(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws)
    spawn = FakeSpawner(ws, skip_state="ladder")
    result = _driver(book, tmp_path, spawn=spawn).run()
    assert result.outcome == "needs_human"
    assert result.stopped_at == "ladder"
    assert "did not advance the ledger" in result.reason
    assert "'mechanical_complete'" in result.reason


def test_the_state_gate_can_be_turned_off(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _deliverable(ws)
    _plan(ws)
    spawn = FakeSpawner(ws, skip_state="ladder")
    result = _driver(book, tmp_path, spawn=spawn, state_gate=False).run()
    assert spawn.phases == list(gd.MECHANICAL_PHASES)
    assert result.outcome == "done"


def test_from_phase_resumes_without_re_running_the_gate(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws)
    spawn = FakeSpawner(ws)
    result = _driver(book, tmp_path, spawn=spawn, start_phase="verify").run()
    assert spawn.phases == ["verify", "settle", "certify", "deliver"]
    assert result.gate == {}


# --- the outcome the run itself reached --------------------------------------

def test_a_needs_human_run_outcome_is_not_papered_over(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws)
    _deliverable(ws)
    (ws / "deliverable" / "outcome.json").write_text(json.dumps({
        "outcome": "needs_human", "reason": "most sentences must be rewritten",
        "evidence": {}, "hubspot": {}, "set_by": "assess"}), encoding="utf-8")
    result = _driver(book, tmp_path, spawn=FakeSpawner(ws)).run()
    assert result.outcome == "needs_human"
    assert result.reason == "most sentences must be rewritten"
    assert result.exit_code == 7
    # …and the hand-off still happened: DocWatch needs the files either way.
    assert [p.name for p in result.handoff][0] == "Ford - Book 2.docx"


# --- the hand-off ------------------------------------------------------------

def _deliverable(ws: Path) -> None:
    d = ws / "deliverable"
    d.mkdir(parents=True, exist_ok=True)
    (d / "Ford - Atmosphere Press Proofreader.docx").write_bytes(
        FIXTURE.read_bytes())
    (d / "Ford - Atmosphere Press Proofreader Change Log.docx").write_bytes(
        b"not the manuscript")
    (d / "letter.md").write_text("# Letter\n", encoding="utf-8")
    (d / "style-sheet.md").write_text("# Style sheet\n", encoding="utf-8")
    (d / gd.DECISION_LOG_NAME).write_text("# Decision log\n", encoding="utf-8")
    (d / "verification.md").write_text("# Verification\n", encoding="utf-8")
    (d / "outcome.json").write_text(json.dumps({
        "outcome": "done", "reason": "no open items", "evidence": {},
        "hubspot": {"value": "Proofing Complete"}, "set_by": "assess"}),
        encoding="utf-8")


@pytest.mark.parametrize("source_name,expected", [
    ("Ford - Book 1.docx", "Ford - Book 2"),
    ("Lichtenstein (and Dolores DelBello) - Book 1.docx",
     "Lichtenstein (and Dolores DelBello) - Book 2"),
    ("Johnson — Book 1.docx", "Johnson - Book 2"),
    ("Johnson - book-1.docx", "Johnson - Book 2"),
    # A source still at an earlier stage in the series resolves too, so a run
    # driven at an odd file never produces "Ford - Book Original - Book 2".
    ("Ford - Book Original.docx", "Ford - Book 2"),
    ("Ford - book 0.docx", "Ford - Book 2"),
    ("Something Else.docx", "Something Else - Book 2"),
])
def test_handoff_base_follows_the_house_series(source_name, expected):
    assert gd.handoff_base(source_name) == expected


def test_handoff_writes_the_contract_files(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _deliverable(ws)
    out = tmp_path / "handoff"
    written = gd.build_handoff(
        ws, book.name, out,
        outcome_sources=[ws / "deliverable" / "outcome.json"])
    assert sorted(p.name for p in written) == [
        "Ford - Book 2 - decision-log.md",
        "Ford - Book 2 - letter.md",
        "Ford - Book 2 - outcome.json",
        "Ford - Book 2 - style-sheet.md",
        "Ford - Book 2 - verification.md",
        "Ford - Book 2.docx",
    ]
    # The change log is NOT the manuscript.
    assert (out / "Ford - Book 2.docx").read_bytes() == FIXTURE.read_bytes()
    assert json.loads((out / "Ford - Book 2 - outcome.json").read_text(
        "utf-8"))["outcome"] == "done"


def test_handoff_refuses_a_partial_folder(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _deliverable(ws)
    (ws / "deliverable" / "letter.md").unlink()
    with pytest.raises(gd.DriverError, match="no editor's letter"):
        gd.build_handoff(ws, book.name, tmp_path / "handoff",
                         outcome_sources=[ws / "deliverable" / "outcome.json"])


def test_a_full_run_hands_off_and_uploads(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws)
    _deliverable(ws)
    uploaded: list[tuple[str, str]] = []

    def upload(files, folder_id):
        uploaded.extend((p.name, folder_id) for p in files)
        return [f"id-{i}" for i, _ in enumerate(files)]

    result = _driver(book, tmp_path, spawn=FakeSpawner(ws),
                     drive_folder_id="folder-9", upload=upload,
                     handoff_dir=tmp_path / "handoff").run()
    assert result.outcome == "done"
    assert len(result.handoff) == 6
    assert len(result.uploaded) == 6
    assert {f for _n, f in uploaded} == {"folder-9"}
    assert ("Ford - Book 2.docx", "folder-9") in uploaded


def test_a_failed_upload_leaves_the_files_and_names_the_fix(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws)
    _deliverable(ws)

    def upload(_files, _folder):
        raise RuntimeError("not signed in")

    result = _driver(book, tmp_path, spawn=FakeSpawner(ws),
                     drive_folder_id="folder-9", upload=upload,
                     handoff_dir=tmp_path / "handoff").run()
    assert result.outcome == "needs_human"
    assert "docproof-watch auth" in result.reason
    assert (tmp_path / "handoff" / "Ford - Book 2.docx").is_file()


def test_a_missing_deliverable_is_a_handoff_failure(book, tmp_path):
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws)
    result = _driver(book, tmp_path, spawn=FakeSpawner(ws)).run()
    assert result.outcome == "needs_human"
    assert result.stopped_at == "deliver"
    assert "hand-off failed" in result.reason


def test_a_phase_that_escalates_a_question_stops_the_run(book, tmp_path):
    """`galley ask` exits 0, so an escalation must be read off QUESTIONS.md —
    otherwise a session that asked a blocking question and carried on looks
    exactly like one that had nothing to ask."""
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws)
    _deliverable(ws)

    class Asking(FakeSpawner):
        def __call__(self, spec):
            if spec.phase == "sweeps":
                (self.workspace / "QUESTIONS.md").write_text(
                    "## 2026-09-03 blast radius\nThe reflection sweep hits 250 "
                    "sites, not 25.\n", encoding="utf-8")
            return super().__call__(spec)

    spawn = Asking(ws)
    result = _driver(book, tmp_path, spawn=spawn).run()
    assert result.outcome == "needs_human"
    assert result.stopped_at == "sweeps"
    assert "nobody to answer it" in result.reason
    assert "blast radius" in result.reason
    assert spawn.phases == ["profile", "approve", "sweeps"]


def test_the_email_gate_question_is_not_read_as_a_phase_escalation(book,
                                                                   tmp_path):
    """The driver's own gate entry lands in QUESTIONS.md before `approve`
    spawns; it must not then stop the run as if the session had asked it."""
    ws = gd.seed_workspace(book, "ford-book-1", workspace_root=tmp_path / "ws")
    _plan(ws, COPYEDIT_PLAN)
    _deliverable(ws)

    def sleep(_s):
        questions = ws / "QUESTIONS.md"
        questions.write_text(questions.read_text("utf-8") + "\nAPPROVED\n",
                             encoding="utf-8")

    spawn = FakeSpawner(ws)
    result = _driver(book, tmp_path, spawn=spawn, approve="email",
                     ask=lambda *_a: "someone@example.com", sleep=sleep,
                     poll_interval_s=0.0, reply_timeout_s=1e6).run()
    assert result.outcome == "done", result.reason
    assert spawn.phases == list(gd.MECHANICAL_PHASES)
