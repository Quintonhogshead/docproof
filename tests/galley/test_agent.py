"""The proofing agent (galley/agent.py): discovery, claiming, the run, the
hand-off, and the ledger — with a fake app, a fake Drive and a fake driver, so
nothing is polled, downloaded, spent or spawned.
"""
from __future__ import annotations

import io
import json
import stat
import urllib.error
from pathlib import Path

import pytest

from galley import agent as ga
from galley.outcome import DEFAULT_NEEDS_HUMAN_VALUE

APP = "https://atmosphere-docproof.fly.dev"
TOKEN = "s3cret-token-long-enough-to-be-real"
OAUTH = "sk-ant-oat-whatever"

ENV_TEXT = f"""# Galley agent credentials
export {ga.OAUTH_KEY}="{OAUTH}"
{ga.APP_URL_KEY}={APP}/
{ga.AGENT_TOKEN_KEY}='{TOKEN}'
GOOGLE_REFRESH_TOKEN=1//refresh
"""

BOOK = {"file_id": "drive-1", "name": "Test - Book 1.docx",
        "folder_id": "folder-A", "subfolder_id": "folder-A",
        "author_last": "Test"}
BOOK_2 = {"file_id": "drive-2", "name": "Other - Book One.docx",
          "folder_id": "folder-B", "subfolder_id": "folder-B",
          "author_last": "Other"}


@pytest.fixture()
def env_file(tmp_path) -> Path:
    path = tmp_path / "agent.env"
    path.write_text(ENV_TEXT, encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture()
def env(env_file) -> ga.AgentEnv:
    return ga.read_env(env_file)


class FakeApp:
    """The awaiting endpoint. Records what it was asked, answers what it was
    given, and can refuse the way a real server refuses."""

    def __init__(self, books, *, status: int | None = None):
        self.books = books
        self.status = status
        self.requests: list = []

    def __call__(self, request, timeout=30):
        self.requests.append(request)
        if self.status:
            raise urllib.error.HTTPError(
                request.full_url, self.status, "no", {},
                io.BytesIO(b'{"detail":"Not the proofing agent."}'))
        body = json.dumps({"books": self.books}).encode("utf-8")
        return _Response(body)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeResult:
    def __init__(self, outcome="done", reason="no open items",
                 uploaded=("id-1",)):
        self.outcome = outcome
        self.reason = reason
        self.uploaded = list(uploaded)


def _agent(env, tmp_path, **kw) -> ga.Agent:
    kw.setdefault("workspace_root", tmp_path / "ws")
    kw.setdefault("log", lambda _m: None)
    kw.setdefault("sleep", lambda _s: None)
    return ga.Agent(env=env, **kw)


def _downloader(tmp_path):
    def download(book, dest):
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / book.name
        target.write_bytes(b"a manuscript")
        return target
    return download


# --- the credentials file -----------------------------------------------------

def test_read_env_reads_the_three_keys_and_keeps_the_rest(env_file):
    env = ga.read_env(env_file)
    assert env.oauth_token == OAUTH
    assert env.token == TOKEN
    assert env.app_url == f"{APP}/"
    assert env.awaiting_url == f"{APP}/api/watch/awaiting"
    # Everything else in the file rides along — GOOGLE_REFRESH_TOKEN is how a
    # headless Linux box signs in to Drive without a Keychain.
    assert env.values["GOOGLE_REFRESH_TOKEN"] == "1//refresh"


def test_parse_env_handles_export_quotes_and_comments():
    parsed = ga.parse_env('# note\nexport A="one"\nB=\'two\'\nC=three\nnope\n')
    assert parsed == {"A": "one", "B": "two", "C": "three"}


def test_a_group_readable_env_file_is_refused(env_file):
    env_file.chmod(0o640)
    with pytest.raises(ga.AgentError, match="readable by other accounts"):
        ga.read_env(env_file)
    with pytest.raises(ga.AgentError, match="chmod 600"):
        ga.read_env(env_file)


def test_a_world_readable_env_file_is_refused(env_file):
    # The check is on the real mode, but the stat is injectable so this holds
    # on a filesystem that will not keep permissions.
    fake = type("S", (), {"st_mode": stat.S_IFREG | 0o604})()
    with pytest.raises(ga.AgentError, match="mode 0604"):
        ga.read_env(env_file, stat_fn=lambda _p: fake)


def test_a_missing_or_incomplete_env_file_names_the_fix(tmp_path):
    with pytest.raises(ga.AgentError, match="No agent credentials"):
        ga.read_env(tmp_path / "nope.env")
    half = tmp_path / "half.env"
    half.write_text(f"{ga.OAUTH_KEY}=x\n", encoding="utf-8")
    half.chmod(0o600)
    with pytest.raises(ga.AgentError, match="GALLEY_APP_URL, GALLEY_AGENT_TOKEN"):
        ga.read_env(half)


def test_apply_env_puts_the_file_into_this_process(env):
    environ = {"PATH": "/bin", ga.OAUTH_KEY: "stale"}
    ga.apply_env(env, environ=environ)
    assert environ[ga.OAUTH_KEY] == OAUTH       # the file wins over the shell
    assert environ["GOOGLE_REFRESH_TOKEN"] == "1//refresh"
    assert environ["PATH"] == "/bin"


# --- asking the app -----------------------------------------------------------

def test_fetch_awaiting_sends_the_bearer_token(env):
    app = FakeApp([BOOK])
    books = ga.fetch_awaiting(env, opener=app)
    assert [b.file_id for b in books] == ["drive-1"]
    assert books[0].folder_id == "folder-A"
    assert books[0].name == "Test - Book 1.docx"
    request = app.requests[0]
    assert request.full_url == f"{APP}/api/watch/awaiting"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


def test_a_refused_or_unreachable_app_costs_one_poll_not_the_agent(env):
    assert ga.fetch_awaiting(env, opener=FakeApp([], status=401)) == []

    def boom(_request, timeout=30):
        raise OSError("no route to host")
    assert ga.fetch_awaiting(env, opener=boom) == []


def test_rows_without_an_id_or_a_name_are_ignored(env):
    app = FakeApp([BOOK, {"file_id": "", "name": "x"}, {"file_id": "y"}])
    assert [b.file_id for b in ga.fetch_awaiting(env, opener=app)] == ["drive-1"]


# --- naming -------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("Test - Book 1.docx", "test-book-1"),
    ("Other - Book One.docx", "other-book-one"),
    ("Lichtenstein (and D. DelBello) - Book 1.docx",
     "lichtenstein-and-d-delbello-book-1"),
    ("---.docx", "untitled-book"),
])
def test_slug_for_names_the_workspace_after_the_file(name, expected):
    assert ga.slug_for(name) == expected


def test_slug_falls_back_to_the_surname():
    assert ga.slug_for("---.docx", "Redding") == "redding-book"


# --- the loop -----------------------------------------------------------------

def test_discovery_claim_run_handoff_and_ledger(env, tmp_path):
    ran: list[dict] = []

    def run_driver(**kwargs):
        ran.append(kwargs)
        return FakeResult(uploaded=["up-1", "up-2"])

    agent = _agent(env, tmp_path, opener=FakeApp([BOOK]),
                   download=_downloader(tmp_path), run_driver=run_driver)
    report = agent.poll_once()

    assert report.looked_at == 1
    assert report.claimed == "Test - Book 1.docx"
    assert report.outcome == "done"
    # The driver was handed the downloaded book, the slug, and the author's
    # folder to deliver into.
    assert len(ran) == 1
    call = ran[0]
    assert call["slug"] == "test-drive-1"          # surname + Drive id suffix
    assert call["source_id"] == "drive-1"
    assert call["drive_folder_id"] == "folder-A"
    assert Path(call["book"]).name == "Test - Book 1.docx"
    assert Path(call["book"]).read_bytes() == b"a manuscript"
    assert call["workspace_root"] == tmp_path / "ws"
    # …and the credentials reach the driver's environment.
    assert call["env"][ga.OAUTH_KEY] == OAUTH

    entry = ga.Ledger.load(agent.ledger_path).books["drive-1"]
    assert entry["state"] == ga.FINISHED
    assert entry["outcome"] == "done"
    assert entry["slug"] == "test-drive-1"
    assert entry["uploaded"] == ["up-1", "up-2"]


def test_one_book_at_a_time(env, tmp_path):
    ran = []
    agent = _agent(env, tmp_path, opener=FakeApp([BOOK, BOOK_2]),
                   download=_downloader(tmp_path),
                   run_driver=lambda **kw: ran.append(kw) or FakeResult())
    report = agent.poll_once()
    assert len(ran) == 1
    assert report.claimed == "Test - Book 1.docx"
    # The next poll takes the second one.
    report = agent.poll_once()
    assert len(ran) == 2
    assert report.claimed == "Other - Book One.docx"
    assert ran[1]["slug"] == "other-drive-2"


def test_a_finished_book_is_never_run_twice(env, tmp_path):
    ran = []
    agent = _agent(env, tmp_path, opener=FakeApp([BOOK]),
                   download=_downloader(tmp_path),
                   run_driver=lambda **kw: ran.append(kw) or FakeResult())
    agent.poll_once()
    # The app still lists it (DocWatch has not ticked yet). It is not re-read.
    report = agent.poll_once()
    assert len(ran) == 1
    assert report.claimed == ""
    assert report.skipped == ["Test - Book 1.docx (finished)"]


def test_a_failed_book_is_never_retried(env, tmp_path):
    ran = []

    def crash(**kwargs):
        ran.append(kwargs)
        return FakeResult("needs_human", "Astra found extensive editorial work.")

    agent = _agent(env, tmp_path, opener=FakeApp([BOOK]),
                   download=_downloader(tmp_path), run_driver=crash,
                   upload=lambda files, folder: [f"up-{p.name}" for p in files])
    agent.poll_once()
    report = agent.poll_once()
    assert len(ran) == 1
    assert report.skipped == ["Test - Book 1.docx (failed)"]


def test_a_needs_human_run_is_recorded_as_failed_but_not_an_error(env, tmp_path):
    agent = _agent(env, tmp_path, opener=FakeApp([BOOK]),
                   download=_downloader(tmp_path),
                   run_driver=lambda **kw: FakeResult(
                       "needs_human", "most sentences must be rewritten"))
    report = agent.poll_once()
    assert report.outcome == "needs_human"
    entry = ga.Ledger.load(agent.ledger_path).books["drive-1"]
    assert entry["state"] == ga.FAILED
    assert entry["reason"] == "most sentences must be rewritten"


# --- failures come back with an answer ----------------------------------------

def test_a_crashed_driver_blocks_without_an_editorial_verdict(env, tmp_path):
    uploaded: list[tuple[str, str]] = []

    def upload(files, folder_id):
        uploaded.extend((p.name, folder_id) for p in files)
        return [f"id-{i}" for i, _ in enumerate(files)]

    def crash(**_kwargs):
        raise RuntimeError("the ladder died")

    agent = _agent(env, tmp_path, opener=FakeApp([BOOK]),
                   download=_downloader(tmp_path), run_driver=crash,
                   upload=upload)
    report = agent.poll_once()

    assert report.outcome == "blocked"
    assert "the ladder died" in report.reason
    assert uploaded == []
    assert agent.ledger().state("drive-1") == ga.CLAIMED
    assert not (tmp_path / "ws" / "test-drive-1" / "runs" / "outcome.json").exists()


def test_a_failed_download_blocks_without_a_manuscript_verdict(env, tmp_path):
    uploaded: list[str] = []

    def download(_book, _dest):
        raise OSError("Google said no")

    agent = _agent(env, tmp_path, opener=FakeApp([BOOK]), download=download,
                   run_driver=lambda **kw: FakeResult(),
                   upload=lambda files, folder: uploaded.extend(
                       p.name for p in files) or ["id-0"])
    report = agent.poll_once()
    assert report.outcome == "blocked"
    assert "could not download" in report.reason
    assert uploaded == []
    entry = ga.Ledger.load(agent.ledger_path).books["drive-1"]
    assert entry["state"] == ga.CLAIMED


def test_an_undeliverable_verdict_is_owed_not_lost(env, tmp_path):
    """Drive refusing the upload must not leave the agent looping on the
    BOOK — the proofread is over — but the verdict is owed: a durable pending
    delivery, retried on later polls without rerunning anything
    (GALLEY-005), and a person is told in the log."""
    said: list[str] = []

    def upload(_files, _folder):
        raise RuntimeError("Drive is down")

    artifact = tmp_path / "Test - Book 2 - outcome.json"
    artifact.write_text('{"outcome":"needs_human"}')
    def reviewed(**kwargs):
        result = FakeResult("needs_human", "Astra requests an editor.", uploaded=())
        result.handoff = [artifact]
        return result

    agent = _agent(env, tmp_path, opener=FakeApp([BOOK]),
                   download=_downloader(tmp_path),
                   run_driver=reviewed,
                   upload=upload, log=said.append)
    agent.poll_once()
    entry = ga.Ledger.load(agent.ledger_path).books["drive-1"]
    assert entry["state"] == ga.PENDING_DELIVERY
    assert any("could not be uploaded" in line for line in said)
    # the next poll retries delivery, never the (crashed) proofread
    report = agent.poll_once()
    assert report.skipped == ["Test - Book 1.docx (pending_delivery)"]


def test_the_claim_is_written_before_the_work(env, tmp_path):
    """The guard against reading one novel twice: if the process dies mid-run
    the ledger already says the book is claimed."""
    seen: list[str] = []

    def run_driver(**kwargs):
        seen.append(ga.Ledger.load(
            Path(kwargs["workspace_root"]) / ga.LEDGER_NAME).state("drive-1"))
        return FakeResult()

    agent = _agent(env, tmp_path, opener=FakeApp([BOOK]),
                   download=_downloader(tmp_path), run_driver=run_driver)
    agent.poll_once()
    assert seen == [ga.CLAIMED]


# --- resuming a crash ---------------------------------------------------------

def _state(ws: Path, slug: str, state: str) -> None:
    from galley.state_machine import RunStateMachine
    path = ws / slug
    path.mkdir(parents=True, exist_ok=True)
    machine = RunStateMachine()
    from galley.driver import MECHANICAL_PHASES, REQUIRED_STATE
    for phase in MECHANICAL_PHASES:
        need = REQUIRED_STATE.get(phase)
        if not need:
            continue
        machine.advance(need, at="t", by="test", source_sha256="s",
                        config_sha256="c")
        if need == state:
            break
    machine.save(path / "state.json")


def test_a_claimed_book_resumes_from_the_phase_after_the_ledger(env, tmp_path):
    ran = []
    agent = _agent(env, tmp_path, opener=FakeApp([BOOK]),
                   download=_downloader(tmp_path),
                   run_driver=lambda **kw: ran.append(kw) or FakeResult())
    # A previous run claimed it and got as far as the mechanical wave.
    ledger = agent.ledger()
    ledger.record("drive-1", ga.CLAIMED, name=BOOK["name"], slug="test-drive-1")
    _state(agent.root, "test-drive-1", "mechanical_complete")

    agent.poll_once()
    assert len(ran) == 1
    # ladder is done, so the run picks up at the phase after it.
    assert ran[0]["start_phase"] == "audit"


def test_a_claimed_book_with_no_state_starts_from_the_beginning(env, tmp_path):
    ran = []
    agent = _agent(env, tmp_path, opener=FakeApp([BOOK]),
                   download=_downloader(tmp_path),
                   run_driver=lambda **kw: ran.append(kw) or FakeResult())
    agent.ledger().record("drive-1", ga.CLAIMED, name=BOOK["name"],
                          slug="test-drive-1")
    agent.poll_once()
    assert "start_phase" not in ran[0]


def test_resume_phase_reads_the_state_machine(env, tmp_path):
    agent = _agent(env, tmp_path)
    assert agent.resume_phase("nothing-here") == ""
    _state(agent.root, "s1", "settled")
    assert agent.resume_phase("s1") == "astra_review"
    _state(agent.root, "s2", "delivered")
    assert agent.resume_phase("s2") == "deliver"  # delivery-only recovery


# --- the ledger ---------------------------------------------------------------

def test_the_ledger_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("{not json", encoding="utf-8")
    ledger = ga.Ledger.load(path)
    assert ledger.books == {}
    ledger.record("a", ga.CLAIMED, name="A")
    assert ga.Ledger.load(path).state("a") == ga.CLAIMED


def test_pending_lists_claimed_but_unfinished(tmp_path):
    ledger = ga.Ledger.load(tmp_path / "l.json")
    ledger.record("a", ga.CLAIMED)
    ledger.record("b", ga.FINISHED)
    ledger.record("c", ga.CLAIMED)
    assert ledger.pending() == ["a", "c"]


def test_status_reports_the_ledger(env, tmp_path):
    agent = _agent(env, tmp_path)
    agent.ledger().record("drive-1", ga.CLAIMED, name="Test - Book 1.docx")
    state = agent.status()
    assert state["app"] == f"{APP}/api/watch/awaiting"
    assert state["pending"] == ["drive-1"]
    assert state["books"]["drive-1"]["name"] == "Test - Book 1.docx"


# --- the drive-folder override ------------------------------------------------

def test_the_folder_override_wins_for_a_rehearsal(env, tmp_path):
    ran = []
    agent = _agent(env, tmp_path, opener=FakeApp([BOOK]),
                   download=_downloader(tmp_path),
                   drive_folder_override="my-test-folder",
                   run_driver=lambda **kw: ran.append(kw) or FakeResult())
    agent.poll_once()
    assert ran[0]["drive_folder_id"] == "my-test-folder"


# --- the heartbeat and the agent's own alerts ----------------------------------

class Observed:
    """The two observability seams, recorded."""

    def __init__(self):
        self.beats: list[dict] = []
        self.alerts: list[tuple[str, str]] = []

    def beat(self, payload):
        self.beats.append(dict(payload))

    def alert(self, subject, body):
        self.alerts.append((subject, body))

    def states(self):
        return [b.get("state") for b in self.beats]


def _observed_agent(env, tmp_path, obs, **kw):
    kw.setdefault("heartbeat", obs.beat)
    kw.setdefault("alert", obs.alert)
    kw.setdefault("heartbeat_interval_s", 0)      # no timer thread in tests
    kw.setdefault("host", "test-box")
    return _agent(env, tmp_path, **kw)


def test_a_run_is_narrated_to_the_drawer(env, tmp_path):
    obs = Observed()

    def run_driver(**kwargs):
        # The driver reports phase boundaries through the hook it was handed.
        progress = kwargs["progress"]
        progress({"event": "phase_start", "phase": "profile",
                  "model": "claude-opus-5", "effort": None})
        progress({"event": "phase_end", "phase": "profile", "ok": True,
                  "num_turns": 12, "limit": None})
        progress({"event": "gate", "approved": True, "reason": "inside budget"})
        progress({"event": "phase_start", "phase": "settle",
                  "model": "claude-fable-5-1", "effort": "high"})
        progress({"event": "finished", "outcome": "done", "reason": ""})
        return FakeResult(uploaded=["up-1"])

    agent = _observed_agent(env, tmp_path, obs, opener=FakeApp([BOOK]),
                            download=_downloader(tmp_path),
                            run_driver=run_driver)
    agent.poll_once()

    assert obs.states()[0] is None or obs.states()[0] in ("idle", "running")
    running = [b for b in obs.beats if b.get("state") == "running"]
    assert running[0]["book"] == "Test - Book 1.docx"
    assert running[0]["slug"] == "test-drive-1"
    assert running[0]["run_started_at"]
    phases = [b for b in running if b.get("phase")]
    assert phases[0]["phase"] == "profile"
    assert phases[0]["model"] == "claude-opus-5"
    assert phases[0]["phase_started_at"]
    assert any(b.get("gate") == "approved" for b in obs.beats)
    settle = [b for b in obs.beats if b.get("phase") == "settle"]
    assert settle and settle[0]["effort"] == "high"
    last = obs.beats[-1]
    assert last["state"] == "idle"
    assert last["last_outcome"] == "done"
    assert last["last_book"] == "Test - Book 1.docx"
    assert last["delivery"] == "uploaded"
    # Every beat says who and from where.
    assert {b["agent"] for b in obs.beats} == {"test-box"}
    assert all(b["at"] and b["version"] for b in obs.beats)


def test_boot_is_announced_once_by_email_and_heartbeat(env, tmp_path):
    obs = Observed()
    agent = _observed_agent(env, tmp_path, obs, opener=FakeApp([]))
    agent.announce()
    assert obs.states() == ["starting"]
    assert obs.beats[0]["started_at"]
    assert len(obs.alerts) == 1
    subject, body = obs.alerts[0]
    assert "started on test-box" in subject
    assert env.awaiting_url in body


def test_a_broken_poll_alerts_once_and_again_on_recovery(env, tmp_path):
    obs = Observed()
    app = FakeApp([], status=401)
    agent = _observed_agent(env, tmp_path, obs, opener=app)
    agent.poll_once()
    agent.poll_once()
    agent.poll_once()
    assert len(obs.alerts) == 1                    # not one per retry
    assert "cannot reach DocProof" in obs.alerts[0][0]
    assert "HTTP 401" in obs.alerts[0][1]
    assert obs.beats[-1]["last_poll_error"].startswith("the app refused")
    assert obs.beats[-1]["state"] == "idle"

    app.status = None                              # the server is back
    agent.poll_once()
    assert len(obs.alerts) == 2
    assert "reachable again" in obs.alerts[1][0]
    assert obs.beats[-1]["last_poll_error"] == ""
    agent.poll_once()
    assert len(obs.alerts) == 2                    # quiet while healthy


def test_a_crashed_run_beats_its_reason_and_ships_the_evidence(env, tmp_path):
    import zipfile

    obs = Observed()
    uploaded: list[str] = []

    def upload(files, folder_id):
        uploaded.extend(p.name for p in files)
        return [f"id-{i}" for i, _ in enumerate(files)]

    def crash(**kwargs):
        # Something the driver managed to write before dying.
        ws = Path(kwargs["workspace_root"]) / kwargs["slug"]
        (ws / "runs" / "driver").mkdir(parents=True, exist_ok=True)
        (ws / "runs" / "driver" / "ladder.log").write_text(
            "phase ladder\nboom\n", encoding="utf-8")
        (ws / "PLAN.md").write_text("# plan\n", encoding="utf-8")
        raise RuntimeError("the ladder died")

    agent = _observed_agent(env, tmp_path, obs, opener=FakeApp([BOOK]),
                            download=_downloader(tmp_path), run_driver=crash,
                            upload=upload)
    (tmp_path / "ws").mkdir(exist_ok=True)
    (tmp_path / "ws" / ga.LOG_NAME).write_text("agent log line\n",
                                                encoding="utf-8")
    agent.poll_once()

    last = obs.beats[-1]
    assert last["state"] == "idle"
    assert last["last_outcome"] == "blocked"
    assert "the ladder died" in last["last_reason"]
    assert uploaded == []
    # Diagnostics remain locally available without an invented author verdict.
    assert (tmp_path / "ws" / "test-drive-1" / "runs" / "driver" / "ladder.log").exists()


def test_an_abandoned_delivery_is_shouted_about(env, tmp_path):
    obs = Observed()
    agent = _observed_agent(env, tmp_path, obs, opener=FakeApp([]))
    ledger = agent.ledger()
    ledger.record("drive-9", ga.PENDING_DELIVERY, name="Nine - Book 1.docx",
                  slug="nine-drive-9", folder_id="folder-Z", outcome="done",
                  handoff_files=[str(tmp_path / "x.docx")],
                  delivery_attempts=ga.MAX_DELIVERY_ATTEMPTS,
                  next_delivery_at=0, delivery_error="Drive said no")
    report = ga.RunReport()
    agent.retry_deliveries(ledger, report, now=10)
    assert report.skipped == ["Nine - Book 1.docx (delivery abandoned)"]
    assert len(obs.alerts) == 1
    subject, body = obs.alerts[0]
    assert "Nine - Book 1.docx" in subject
    assert "folder-Z" in body and "Drive said no" in body
    assert obs.beats[-1]["last_error"].startswith("Nine - Book 1.docx")


def test_the_live_beat_reads_the_running_session(env, tmp_path):
    obs = Observed()
    agent = _observed_agent(env, tmp_path, obs, opener=FakeApp([]))
    ws = tmp_path / "ws" / "slug-1"
    (ws / "runs" / "driver").mkdir(parents=True)
    (ws / "runs" / "driver" / "settle.stream.jsonl").write_text(
        '{"type":"system"}\n{"type":"assistant","x":1}\n'
        '{"type":"user"}\n{"type":"assistant","x":2}\n', encoding="utf-8")
    (ws / "runs" / "r1").mkdir()
    (ws / "runs" / "r1" / "settlement.json").write_text(
        json.dumps({"rounds": 2}), encoding="utf-8")
    agent._status.update({"state": "running", "phase": "settle"})
    agent._live_beat("slug-1")
    beat = obs.beats[-1]
    assert beat["turns"] == 2
    assert beat["settle_rounds"] == 2
    assert beat["last_activity_at"]
    assert beat["phase"] == "settle"


def test_the_default_heartbeat_posts_to_the_app(env):
    posted: list = []

    class Opener:
        def __call__(self, request, timeout=30):
            posted.append((request.full_url, request.get_method(),
                           request.get_header("Authorization"),
                           json.loads(request.data.decode("utf-8"))))
            return _Response(b'{"ok": true}')

    assert ga.post_status(env, {"state": "idle"}, opener=Opener()) is True
    url, method, auth, body = posted[0]
    assert url == env.status_url == f"{APP}/api/watch/agent"
    assert method == "POST"
    assert auth == f"Bearer {TOKEN}"
    assert body == {"state": "idle"}
    # A dead app costs a warning, never the run.
    assert ga.post_status(env, {"state": "idle"},
                          opener=FakeApp([], status=500)) is False


# ===========================================================================
# Packaging: the Fly agent image can actually open the subagent lane
# ===========================================================================

def test_image_installs_the_agent_sdk_the_subagent_lane_needs():
    """The gap that stopped Test - Book One's ladder on 2026-09-07.

    claude-agent-sdk was declared only under the `canvas` extra (cover
    generation), while the Dockerfile installed `.[app,languagetool]` — so the
    agent image carried the Claude Code CLI but not the SDK that drives it,
    and `api.claude_lane: subagent` refused with ModuleNotFoundError before
    the first paid read. The extra and the image install must stay in step.
    """
    import tomllib

    root = Path(__file__).resolve().parents[2]
    data = tomllib.loads(
        (root / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"]["optional-dependencies"]
    assert any(dep.startswith("claude-agent-sdk")
               for dep in extras["galley"])

    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    installs = [ln for ln in dockerfile.splitlines()
                if "pip install" in ln and ".[" in ln]
    assert installs, "the Dockerfile no longer pip-installs an extras set"
    for line in installs:
        assert "galley" in line, f"galley extra missing from: {line.strip()}"
    # The SDK drives the CLI; an image with one and not the other is the bug.
    assert "claude.ai/install.sh" in dockerfile


# --- a rejected subscription token holds the queue -----------------------------

AUTH_ERROR = ("phase profile could not sign in to Claude Code — the "
              "subscription token (CLAUDE_CODE_OAUTH_TOKEN) is expired or "
              "revoked; last lines of profile.log:\nFailed to authenticate. "
              "API Error: 401 OAuth access token is invalid.")


def _refusing_driver(**_kwargs):
    from galley.driver import CredentialsError
    raise CredentialsError(AUTH_ERROR)


def test_a_rejected_token_holds_the_book_instead_of_failing_it(env, tmp_path):
    """2026-09-07: the Fly token died and every awaiting book was written
    off as needs_human, one per poll, with nothing read."""
    obs = Observed()
    uploaded = []
    agent = _observed_agent(env, tmp_path, obs, opener=FakeApp([BOOK, BOOK_2]),
                            download=_downloader(tmp_path),
                            run_driver=_refusing_driver,
                            upload=lambda files, folder: uploaded.extend(files))
    report = agent.poll_once()

    assert report.outcome == "held"
    assert "401" in report.reason
    # No verdict was written or delivered; DocWatch keeps waiting, correctly.
    assert not uploaded
    assert not (tmp_path / "ws" / "test-drive-1" / "runs" / "outcome.json"
                ).exists()
    # The claim stands, so the book resumes once the token works.
    assert agent.ledger().state("drive-1") == ga.CLAIMED
    assert agent.ledger().state("drive-2") == ""
    last = obs.beats[-1]
    assert last["state"] == "halted"
    assert last["held_book"] == BOOK["name"]
    assert "401" in last["credentials_error"]
    # One alarm, and it says what to do.
    assert len(obs.alerts) == 1
    subject, body = obs.alerts[0]
    assert "token rejected" in subject
    assert "claude setup-token" in body and "GALLEY_OAUTH_TOKEN" in body
    assert "nothing has been marked needs_human" in body


def test_while_halted_no_further_book_is_claimed(env, tmp_path):
    obs = Observed()
    ran = []
    checks = []

    def preflight(values):
        checks.append(values[ga.OAUTH_KEY])
        return "Claude Code refused the subscription token: 401"

    agent = _observed_agent(env, tmp_path, obs, opener=FakeApp([BOOK, BOOK_2]),
                            download=_downloader(tmp_path),
                            run_driver=_refusing_driver, preflight=preflight)
    agent.poll_once()                           # claims BOOK, gets refused
    second = agent.poll_once()
    third = agent.poll_once()

    assert second.halted and third.halted
    assert second.claimed == "" and third.claimed == ""
    assert agent.ledger().state("drive-2") == ""            # never touched
    assert obs.beats[-1]["state"] == "halted"
    assert obs.beats[-1]["awaiting"] == 2
    assert len(obs.alerts) == 1                 # not one per poll
    assert checks == [OAUTH, OAUTH]             # re-checked each poll


def test_a_replaced_token_resumes_the_held_book(env_file, tmp_path):
    env = ga.read_env(env_file)
    obs = Observed()
    ran = []
    token_ok = {"value": False}

    def run_driver(**kw):
        ran.append(kw)
        if not token_ok["value"]:
            _refusing_driver()
        return FakeResult(uploaded=["up-1"])

    def preflight(values):
        return "" if values[ga.OAUTH_KEY] == "sk-ant-oat-fresh" else "refused"

    agent = _observed_agent(env, tmp_path, obs, opener=FakeApp([BOOK]),
                            download=_downloader(tmp_path),
                            run_driver=run_driver, preflight=preflight)
    agent.poll_once()                           # refused
    agent.poll_once()                           # still the old token: held
    assert len(ran) == 1

    # The operator rotates the token in the credentials file.
    env_file.write_text(ENV_TEXT.replace(OAUTH, "sk-ant-oat-fresh"),
                        encoding="utf-8")
    token_ok["value"] = True
    report = agent.poll_once()

    assert not report.halted
    assert report.outcome == "done"
    assert len(ran) == 2
    # The driver was handed the new token, and resumed the same claim.
    assert ran[1]["env"][ga.OAUTH_KEY] == "sk-ant-oat-fresh"
    assert agent.ledger().state("drive-1") == ga.FINISHED
    assert any("signed in again" in s for s, _b in obs.alerts)
    assert obs.beats[-1]["state"] == "idle"
    assert not obs.beats[-1].get("credentials_error")


def test_boot_checks_the_token_before_any_book(env, tmp_path):
    obs = Observed()
    ran = []
    agent = _observed_agent(env, tmp_path, obs, opener=FakeApp([BOOK]),
                            download=_downloader(tmp_path),
                            run_driver=lambda **kw: ran.append(kw) or FakeResult(),
                            preflight=lambda _v: "Claude Code refused the "
                                                 "subscription token: 401")
    agent.announce()
    report = agent.poll_once()

    assert obs.beats[-1]["state"] == "halted"
    assert "token is rejected" in obs.alerts[0][0]
    assert "claude setup-token" in obs.alerts[0][1]
    assert report.halted and not ran
    assert agent.ledger().state("drive-1") == ""


def test_boot_with_a_working_token_is_quiet(env, tmp_path):
    obs = Observed()
    agent = _observed_agent(env, tmp_path, obs, opener=FakeApp([]),
                            preflight=lambda _v: "")
    agent.announce()
    assert obs.states() == ["starting"]
    assert "rejected" not in obs.alerts[0][0]


# --- the sign-in check itself --------------------------------------------------

class _Proc:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_check_credentials_runs_one_cheap_turn_on_the_token():
    seen = {}

    def runner(argv, **kw):
        seen["argv"], seen["env"] = argv, kw["env"]
        return _Proc(0, '{"type":"result","subtype":"success","result":"ok"}')

    values = {ga.OAUTH_KEY: OAUTH, "ANTHROPIC_API_KEY": "sk-api"}
    assert ga.check_credentials(values, runner=runner) == ""
    assert seen["argv"][:2] == ["claude", "-p"]
    assert "--max-turns" in seen["argv"] and "1" in seen["argv"]
    assert seen["env"][ga.OAUTH_KEY] == OAUTH
    assert "ANTHROPIC_API_KEY" not in seen["env"]     # signs in on the token


def test_check_credentials_names_the_refusal():
    runner = lambda argv, **kw: _Proc(1, "", "Failed to authenticate. API "
                                             "Error: 401 OAuth access token "
                                             "is invalid.")
    error = ga.check_credentials({ga.OAUTH_KEY: OAUTH}, runner=runner)
    assert error.startswith("Claude Code refused the subscription token")
    assert "401" in error


def test_check_credentials_reports_other_failures_without_blaming_the_token(
        monkeypatch):
    monkeypatch.delenv(ga.OAUTH_KEY, raising=False)
    runner = lambda argv, **kw: _Proc(2, "", "some other crash")
    error = ga.check_credentials({ga.OAUTH_KEY: OAUTH}, runner=runner)
    assert "exited 2" in error and "some other crash" in error

    def missing(argv, **kw):
        raise FileNotFoundError("claude")
    assert "not installed" in ga.check_credentials({ga.OAUTH_KEY: OAUTH},
                                                   runner=missing)
    assert "is not set" in ga.check_credentials({}, runner=runner)


# --- forgetting a book this machine wrote off --------------------------------

def test_forget_drops_the_ledger_entry_so_the_book_runs_again(env, tmp_path):
    ran = []
    agent = _agent(env, tmp_path, opener=FakeApp([BOOK]),
                   download=_downloader(tmp_path),
                   run_driver=lambda **kw: ran.append(kw) or FakeResult())
    agent.ledger().record("drive-1", ga.FAILED, name=BOOK["name"],
                          slug="test-drive-1", outcome="needs_human")
    agent.poll_once()
    assert not ran                                   # failed: never retried

    assert agent.forget(BOOK["name"]) == BOOK["name"]
    assert agent.ledger().state("drive-1") == ""
    agent.poll_once()
    assert len(ran) == 1                             # claimed afresh
    assert agent.ledger().state("drive-1") == ga.FINISHED


def test_forget_by_id_and_its_refusals(env, tmp_path):
    agent = _agent(env, tmp_path, opener=FakeApp([]))
    agent.ledger().record("drive-1", ga.FAILED, name="Same.docx", slug="a")
    agent.ledger().record("drive-2", ga.FAILED, name="Same.docx", slug="b")
    with pytest.raises(ga.AgentError, match="2 ledger entries"):
        agent.forget("Same.docx")
    assert agent.forget("drive-2") == "Same.docx"
    assert agent.ledger().state("drive-1") == ga.FAILED
    with pytest.raises(ga.AgentError, match="No book in the ledger"):
        agent.forget("nope")
