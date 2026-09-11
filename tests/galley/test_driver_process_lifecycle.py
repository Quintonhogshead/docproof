"""Real local subprocesses exercise phase ownership without model calls."""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from types import SimpleNamespace

import pytest

from galley import driver as gd
from galley.driver import PhaseSpec, TIMEOUT_RC, spawn_claude


def _spec(tmp_path, script, *, timeout=5):
    return PhaseSpec("settle", "", tmp_path, tmp_path / "driver.log",
                     [sys.executable, "-c", script], dict(os.environ),
                     timeout_s=timeout)


def test_completed_phase_preserves_structured_result_and_transcript(tmp_path):
    event = {"type": "result", "subtype": "success", "num_turns": 3,
             "is_error": False}
    result = spawn_claude(_spec(tmp_path, f"print({json.dumps(event)!r})"))
    assert result.ok
    assert result.num_turns == 3
    assert result.subtype == "success"
    assert "# result: subtype=success" in result.log_path.read_text()


def test_failed_phase_preserves_exit_code(tmp_path):
    result = spawn_claude(_spec(tmp_path, "import sys; print('failure'); sys.exit(7)"))
    assert result.returncode == 7
    assert not result.ok
    assert "failure" in result.tail


def test_missing_windows_containment_fails_before_starting_phase(tmp_path, monkeypatch):
    spec = _spec(tmp_path, "raise AssertionError('must not start')")
    monkeypatch.setattr(gd, "os", SimpleNamespace(name="nt"))
    monkeypatch.setitem(sys.modules, "win32api", None)
    monkeypatch.setattr(gd.subprocess, "Popen", lambda *a, **kw: pytest.fail("Started uncontained phase"))
    with pytest.raises(gd.DriverError, match="pywin32"):
        spawn_claude(spec)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group integration")
@pytest.mark.parametrize("supervisor_exits", [False, True])
def test_phase_exit_stops_child_even_when_child_ignores_termination(tmp_path, supervisor_exits):
    # The child signals readiness only after installing its SIGTERM handler;
    # the supervisor can then either hang or exit while the child keeps working.
    child = """
import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path('child.pid').write_text(str(os.getpid()))
while True:
    with open('heartbeat', 'a') as f:
        f.write('x')
        f.flush()
    time.sleep(0.01)
"""
    supervisor = f"""
import subprocess, sys, time
from pathlib import Path
subprocess.Popen([sys.executable, '-c', {child!r}])
while not Path('child.pid').exists():
    time.sleep(0.01)
if {supervisor_exits!r}:
    print('{{"type":"result","subtype":"success","num_turns":1}}', flush=True)
else:
    time.sleep(60)
"""
    pid_path = tmp_path / "child.pid"
    try:
        result = spawn_claude(_spec(tmp_path, supervisor, timeout=1))
        assert pid_path.exists(), "Child must be running before testing cleanup"
        if supervisor_exits:
            assert result.ok
        else:
            assert result.returncode == TIMEOUT_RC
            assert result.limit == "timeout"
        heartbeat = tmp_path / "heartbeat"
        # Permit the kernel to finish a write already in flight, then verify
        # that work cannot continue after the phase reports it has ended.
        time.sleep(0.05)
        size = heartbeat.stat().st_size
        time.sleep(0.15)
        assert heartbeat.stat().st_size == size
    finally:
        # Keep the regression test safe when run against the broken version.
        if pid_path.exists():
            try:
                os.kill(int(pid_path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
