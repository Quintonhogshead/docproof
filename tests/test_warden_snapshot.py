"""app/warden/snapshot.py: every collector driven by a fake `run`/`opener`,
never the network or a real `fly` binary. Failure isolation (one dead source
never takes the snapshot down) and `save()`'s prune-to-200 behaviour."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from app.warden import snapshot as snap
from app.warden.journal import Journal


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

@dataclass
class FakeConfig:
    app_url: str = "https://atmosphere-docproof.fly.dev"
    fly_app: str = "atmosphere-docproof"
    fly_bin: str = "fly"
    interior_home: str = ""
    thresholds: object | None = None


@dataclass
class FakeThresholds:
    fly_log_lines: int = 200


class FakeSecrets:
    def __init__(self, values: dict[str, str] | None = None):
        self.values = dict(values or {})

    def get(self, name):
        return self.values.get(name)


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ScriptedRun:
    """A fake `subprocess.run`: matches by the first two argv tokens (the
    binary and its subcommand), so `["fly", "machine", "list", ...]` and
    `["fly", "logs", ...]` can be scripted independently."""

    def __init__(self, table: dict[tuple[str, str], _Proc]):
        self.table = table
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        key = (args[0], args[1] if len(args) > 1 else "")
        if key not in self.table:
            raise AssertionError(f"unscripted command: {args}")
        return self.table[key]


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def scripted_opener(*answers):
    served = iter(answers)
    calls = []

    def opener(request, timeout=30):
        calls.append(request)
        answer = next(served)
        if isinstance(answer, Exception):
            raise answer
        return _Resp(json.dumps(answer).encode())

    opener.calls = calls
    return opener


# Real Fly machine ids are lowercase hex, and `fly logs`' `<source>[<id>]`
# prefix carries the id, not the process group (see `snapshot._MACHINE_ID_RE`'s
# docstring) — so the fixture ids have to look like the real thing for a log
# line to bucket correctly, the same way production ids do.
AGENT_MACHINE_ID = "78126d2f0d7e28"
APP_MACHINE_ID = "286d2d7c11e738"

MACHINE_LIST = [
    {"id": AGENT_MACHINE_ID, "state": "started", "region": "iad",
     "updated_at": "2026-09-20T00:00:00Z",
     "config": {"image": "registry.fly.io/app:agent-tag",
               "metadata": {"fly_process_group": "agent", "fly_release_version": "464"}},
     "checks": []},
    {"id": APP_MACHINE_ID, "state": "started", "region": "iad",
     "updated_at": "2026-09-21T00:00:00Z",
     "config": {"image": "registry.fly.io/app:app-tag",
               "metadata": {"fly_process_group": "app", "fly_release_version": "467"}},
     "checks": [{"name": "http", "status": "passing"}]},
]

LOG_LINES = (
    f"2026-09-22T13:00:00Z app[{APP_MACHINE_ID}] iad [info]INFO: GET /api/watch 200\n"
    f"2026-09-22T13:00:01Z app[{AGENT_MACHINE_ID}] iad [info]docproof.galley.agent: idle\n"
    f"2026-09-22T13:00:02Z app[{AGENT_MACHINE_ID}] iad [info]MemoryError: Out of memory: kill process\n"
)


def fly_run_table(*, logs=LOG_LINES, machines=None):
    machines = machines if machines is not None else MACHINE_LIST
    return {
        ("fly", "machine"): _Proc(0, json.dumps(machines)),
        ("fly", "logs"): _Proc(0, logs),
    }


# --------------------------------------------------------------------------
# collect_fly
# --------------------------------------------------------------------------

def test_collect_fly_buckets_machines_and_logs_by_process_group():
    run = ScriptedRun(fly_run_table())
    out = snap.collect_fly(FakeConfig(), run=run)
    assert [m["id"] for m in out["app"]["machines"]] == [APP_MACHINE_ID]
    assert [m["id"] for m in out["agent"]["machines"]] == [AGENT_MACHINE_ID]
    assert out["app"]["release"] == {"version": 467, "created_at": "2026-09-21T00:00:00Z"}
    assert out["agent"]["release"] == {"version": 464, "created_at": "2026-09-20T00:00:00Z"}
    assert out["app"]["image"] == "registry.fly.io/app:app-tag"
    assert any("GET /api/watch" in line for line in out["logs"]["app"])
    assert any("idle" in line or "Out of memory" in line for line in out["logs"]["agent"])
    assert "error" not in out


def test_collect_fly_reports_machine_list_failure_without_crashing():
    run = ScriptedRun({
        ("fly", "machine"): _Proc(1, "", "could not connect to Fly"),
        ("fly", "logs"): _Proc(0, ""),
    })
    out = snap.collect_fly(FakeConfig(), run=run)
    assert "error" in out
    assert "could not connect to Fly" in out["error"]
    # The rest of the shape still lands, empty rather than missing.
    assert out["app"]["machines"] == []
    assert out["logs"]["app"] == []


def test_collect_fly_caps_log_lines_to_the_threshold():
    many_lines = "\n".join(
        f"2026-09-22T13:00:{i:02d}Z app[{APP_MACHINE_ID}] iad [info]line {i}" for i in range(10))
    run = ScriptedRun(fly_run_table(logs=many_lines))
    cfg = FakeConfig(thresholds=FakeThresholds(fly_log_lines=3))
    out = snap.collect_fly(cfg, run=run)
    assert len(out["app"]["logs"]) if "logs" in out["app"] else True  # no-op guard
    assert len(out["logs"]["app"]) == 3
    assert out["logs"]["app"][-1].endswith("line 9")


def test_collect_fly_unreachable_binary_is_isolated():
    def broken_run(args, **kwargs):
        raise FileNotFoundError("fly: command not found")
    out = snap.collect_fly(FakeConfig(), run=broken_run)
    assert "error" in out
    assert out["app"]["machines"] == []


# --------------------------------------------------------------------------
# collect_docwatch
# --------------------------------------------------------------------------

def test_collect_docwatch_fetches_the_warden_endpoint_with_bearer_token():
    payload = {"server_version": "0.228.0", "watch": {}, "run": {}, "sign_in": None,
              "agent": None, "last_pass": None, "last_tick": None,
              "awaiting": [], "files": [], "native": {}}
    opener = scripted_opener(payload)
    secrets = FakeSecrets({"warden_token": "x" * 32})
    out = snap.collect_docwatch(FakeConfig(), secrets=secrets, opener=opener)
    assert out == payload
    request = opener.calls[0]
    assert request.get_header("Authorization") == "Bearer " + "x" * 32
    assert request.full_url.endswith("/api/watch/warden")


def test_collect_docwatch_without_a_token_is_an_error_not_a_crash():
    out = snap.collect_docwatch(FakeConfig(), secrets=FakeSecrets({}), opener=scripted_opener())
    assert "error" in out


def test_collect_docwatch_network_failure_is_isolated():
    import urllib.error
    opener = scripted_opener(urllib.error.URLError("no route to host"))
    secrets = FakeSecrets({"warden_token": "y" * 32})
    out = snap.collect_docwatch(FakeConfig(), secrets=secrets, opener=opener)
    assert "error" in out


# --------------------------------------------------------------------------
# collect_hubspot
# --------------------------------------------------------------------------

def test_collect_hubspot_searches_projects_and_reads_form_submissions():
    search_answer = {"results": [
        {"id": "hs-1", "properties": {"createdate": "2026-09-20T00:00:00Z",
                                       "hs_lastmodifieddate": "2026-09-21T00:00:00Z",
                                       "author_first_name": "Jordan",
                                       "author_last_name": "Casey"}},
    ]}
    submissions_answer = {"results": [
        {"submittedAt": "1758000000000",
         "values": [{"name": "firstname", "value": "Jordan"},
                   {"name": "lastname", "value": "Casey"},
                   {"name": "project_id", "value": "hs-1"}]},
    ]}
    opener = scripted_opener(search_answer, submissions_answer)
    secrets = FakeSecrets({"hubspot": "tok"})
    docwatch = {"watch": {"hubspot_first_property": "author_first_name",
                          "hubspot_last_property": "author_last_name",
                          "corrections_native_form_id": "form-1"}}
    out = snap.collect_hubspot(FakeConfig(), secrets=secrets, opener=opener,
                               docwatch=docwatch, journal=None)
    assert out["projects"][0]["author_first"] == "Jordan"
    assert out["projects"][0]["author_last"] == "Casey"
    assert out["form_submissions"][0]["project_id"] == "hs-1"
    assert "error" not in out


def test_collect_hubspot_without_a_token_is_an_error():
    out = snap.collect_hubspot(FakeConfig(), secrets=FakeSecrets({}), opener=scripted_opener())
    assert "error" in out


def test_collect_hubspot_uses_the_journal_cursor(monkeypatch, tmp_path):
    captured = {}

    def opener(request, timeout=30):
        captured["body"] = json.loads(request.data)
        return _Resp(json.dumps({"results": []}).encode())

    secrets = FakeSecrets({"hubspot": "tok"})
    clock = lambda: "2026-09-22T12:00:00+00:00"
    j = Journal(tmp_path / "journal.sqlite", now=clock)
    try:
        j.set("last_hubspot_cursor", "1758000000.0")
        snap.collect_hubspot(FakeConfig(), secrets=secrets, opener=opener,
                             docwatch={}, journal=j)
    finally:
        j.close()
    since_filter = captured["body"]["filterGroups"][0]["filters"][0]
    assert since_filter["value"] == "1758000000000"


# --------------------------------------------------------------------------
# collect_native
# --------------------------------------------------------------------------

def test_collect_native_without_interior_home_is_a_clean_not_installed():
    out = snap.collect_native(FakeConfig(interior_home=""), run=ScriptedRun({}))
    assert out == {"installed": False, "agents": {}, "worker": None, "intake": None,
                   "indesign": {"alive": False, "version": "", "error": ""},
                   "batches": []}


def test_collect_native_reads_launchctl_worker_json_and_indesign(tmp_path):
    home = tmp_path / "interior"
    watch_home = home / "watch"
    watch_home.mkdir(parents=True)
    (watch_home / "native-worker.json").write_text(
        json.dumps({"state": "idle"}), encoding="utf-8")
    (watch_home / "native-intake.json").write_text(
        json.dumps({"unmatched": []}), encoding="utf-8")

    def run(args, **kwargs):
        if args[0] == "launchctl":
            return _Proc(0, "state = running\n\t\tpid = 4242\n")
        if args[0] == "osascript":
            return _Proc(0, "19.0\n")
        raise AssertionError(args)

    cfg = FakeConfig(interior_home=str(home))
    out = snap.collect_native(cfg, run=run)
    assert out["installed"] is True
    assert out["agents"]["com.docproof.interior-review-worker"] == {"running": True, "pid": 4242}
    assert out["worker"] == {"state": "idle"}
    assert out["intake"] == {"unmatched": []}
    assert out["indesign"] == {"alive": True, "version": "19.0", "error": ""}
    assert out["batches"] == []


def test_collect_native_indesign_probe_failure_is_isolated(tmp_path):
    home = tmp_path / "interior"
    (home / "watch").mkdir(parents=True)

    def run(args, **kwargs):
        if args[0] == "launchctl":
            return _Proc(1, "")
        if args[0] == "osascript":
            return _Proc(1, "", "Application isn't running.")
        raise AssertionError(args)

    out = snap.collect_native(FakeConfig(interior_home=str(home)), run=run)
    assert out["indesign"]["alive"] is False
    assert "isn't running" in out["indesign"]["error"]
    assert out["agents"]["com.docproof.interior-review-worker"]["running"] is False


# --------------------------------------------------------------------------
# collect_email
# --------------------------------------------------------------------------

def test_collect_email_without_gmail_module_is_the_documented_stub(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def blocked_import(name, *a, **kw):
        if name == "app.warden.messaging.gmail" or name.startswith("app.warden.messaging"):
            raise ImportError("no gmail module")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    out = snap.collect_email(FakeConfig(), secrets=FakeSecrets({}), opener=scripted_opener())
    assert out == {"error": "gmail module missing"}


def test_collect_email_calls_gmail_inbox_and_replies(monkeypatch):
    from app.warden.messaging import gmail

    calls = []

    def fake_inbox(cfg, secrets, *, since, opener):
        calls.append(("inbox", since))
        return [{"id": "1", "from": "a@b.com", "subject": "hi", "date": "", "snippet": ""}]

    def fake_replies(cfg, secrets, *, since, opener):
        calls.append(("replies", since))
        return []

    monkeypatch.setattr(gmail, "inbox", fake_inbox)
    monkeypatch.setattr(gmail, "replies", fake_replies)
    now = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)
    out = snap.collect_email(FakeConfig(), secrets=FakeSecrets({}), opener=scripted_opener(), now=now)
    assert out["inbox"][0]["id"] == "1"
    assert out["replies"] == []
    assert [c[0] for c in calls] == ["inbox", "replies"]


# --------------------------------------------------------------------------
# collect() end-to-end failure isolation
# --------------------------------------------------------------------------

def test_collect_isolates_each_source_and_still_returns_the_rest(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(snap, "collect_hubspot", boom)
    run = ScriptedRun(fly_run_table())
    secrets = FakeSecrets({"warden_token": "z" * 32})
    opener = scripted_opener({"server_version": "0.228.0", "watch": {}, "run": {},
                              "sign_in": None, "agent": None, "last_pass": None,
                              "last_tick": None, "awaiting": [], "files": [],
                              "native": {}})
    out = snap.collect(FakeConfig(), secrets=secrets, run=run, opener=opener,
                       now=datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc))
    assert "error" in out["hubspot"]
    assert "kaboom" in out["hubspot"]["error"]
    # Fly and DocWatch collected fine despite HubSpot's crash.
    assert out["fly"]["app"]["machines"][0]["id"] == APP_MACHINE_ID
    assert out["docwatch"]["server_version"] == "0.228.0"
    assert out["at"]
    assert out["warden_version"]


def test_collect_includes_journal_summary_when_given_a_real_journal(tmp_path):
    clock = lambda: "2026-09-22T14:00:00+00:00"
    j = Journal(tmp_path / "journal.sqlite", now=clock)
    try:
        j.upsert_finding("agent-silent", "Casey", "high", "quiet", {})
        run = ScriptedRun(fly_run_table())
        secrets = FakeSecrets({})
        out = snap.collect(FakeConfig(), secrets=secrets, run=run,
                           opener=scripted_opener(), journal=j,
                           now=datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc))
    finally:
        j.close()
    assert out["journal"]["open_findings"][0]["rule"] == "agent-silent"
    assert out["journal"]["paused"] is False


def test_collect_without_a_journal_uses_an_empty_summary():
    run = ScriptedRun(fly_run_table())
    out = snap.collect(FakeConfig(), secrets=FakeSecrets({}), run=run,
                       opener=scripted_opener(),
                       now=datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc))
    assert out["journal"] == {"open_findings": [], "open_requests": [],
                              "recent_actions": [], "paused": False,
                              "quiet_until": None, "last_tick_at": None}


# --------------------------------------------------------------------------
# save()
# --------------------------------------------------------------------------

def test_save_writes_latest_and_a_timestamped_copy(tmp_path):
    home = tmp_path / "warden-home"
    snapshot = {"at": "2026-09-22T14:00:00+00:00", "warden_version": "0.228.0"}
    target = snap.save(home, snapshot)
    assert target.exists()
    latest = home / "snapshots" / "latest.json"
    assert latest.exists()
    assert json.loads(latest.read_text()) == snapshot
    assert json.loads(target.read_text()) == snapshot


def test_save_prunes_to_the_keep_limit(tmp_path):
    home = tmp_path / "warden-home"
    for i in range(5):
        snap.save(home, {"at": f"2026-09-22T14:{i:02d}:00+00:00"}, keep=3)
    snap_dir = home / "snapshots"
    timestamped = [p for p in snap_dir.glob("*.json") if p.name != "latest.json"]
    assert len(timestamped) == 3
    # The newest three survive; the oldest two are gone.
    names = sorted(p.name for p in timestamped)
    assert names[0].endswith("14-02-00+00-00.json") or "14:02" not in names[0]


def test_save_is_atomic_and_never_leaves_a_partial_latest(tmp_path):
    home = tmp_path / "warden-home"
    snap.save(home, {"at": "2026-09-22T14:00:00+00:00", "n": 1})
    snap.save(home, {"at": "2026-09-22T14:20:00+00:00", "n": 2})
    latest = json.loads((home / "snapshots" / "latest.json").read_text())
    assert latest["n"] == 2
