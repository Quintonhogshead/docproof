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
        raise RuntimeError("the ladder died")

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

def test_a_crashed_driver_uploads_a_needs_human_outcome(env, tmp_path):
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

    assert report.outcome == "needs_human"
    assert "the ladder died" in report.reason
    # The verdict reached the author's folder under the Book 2 name DocWatch
    # looks for, so the next tick moves the book on.
    names = [n for n, _f in uploaded]
    assert "Test - Book 2 - outcome.json" in names
    assert {f for _n, f in uploaded} == {"folder-A"}
    written = json.loads(
        (tmp_path / "ws" / "test-drive-1" / "runs" / "outcome.json"
         ).read_text("utf-8"))
    assert written["outcome"] == "needs_human"
    assert written["set_by"] == "galley agent"
    assert written["hubspot"]["value"] == DEFAULT_NEEDS_HUMAN_VALUE


def test_a_failed_download_also_comes_back_with_a_verdict(env, tmp_path):
    uploaded: list[str] = []

    def download(_book, _dest):
        raise OSError("Google said no")

    agent = _agent(env, tmp_path, opener=FakeApp([BOOK]), download=download,
                   run_driver=lambda **kw: FakeResult(),
                   upload=lambda files, folder: uploaded.extend(
                       p.name for p in files) or ["id-0"])
    report = agent.poll_once()
    assert report.outcome == "needs_human"
    assert "could not download" in report.reason
    assert "Test - Book 2 - outcome.json" in uploaded
    entry = ga.Ledger.load(agent.ledger_path).books["drive-1"]
    assert entry["state"] == ga.FAILED


def test_an_undeliverable_verdict_is_owed_not_lost(env, tmp_path):
    """Drive refusing the upload must not leave the agent looping on the
    BOOK — the proofread is over — but the verdict is owed: a durable pending
    delivery, retried on later polls without rerunning anything
    (GALLEY-005), and a person is told in the log."""
    said: list[str] = []

    def upload(_files, _folder):
        raise RuntimeError("Drive is down")

    agent = _agent(env, tmp_path, opener=FakeApp([BOOK]),
                   download=_downloader(tmp_path),
                   run_driver=lambda **kw: (_ for _ in ()).throw(
                       RuntimeError("boom")),
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
    assert agent.resume_phase("s1") == "certify"
    _state(agent.root, "s2", "delivered")
    assert agent.resume_phase("s2") == ""      # nothing left to do


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
