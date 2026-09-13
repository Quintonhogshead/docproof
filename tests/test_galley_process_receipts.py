"""Code commands survive coordinator loss without inventing time or spend credit."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from galley.execution_budget import ExecutionBudget, ExecutionBudgetError
from galley import process_receipt as process


def _budget(tmp_path):
    return ExecutionBudget(tmp_path / 'budget.json', 'source-sha')


def _attempt(budget):
    return json.loads(budget.path.read_text())['attempts'][-1]


def _mutate(budget, **values):
    data = json.loads(budget.path.read_text())
    data['attempts'][-1].update(values)
    budget.path.write_text(json.dumps(data))


def _reserved(tmp_path, turns=0):
    budget = _budget(tmp_path)
    key, _, _ = budget.reserve('code-verify' if not turns else 'verify', turns, 100,
                               log_path=tmp_path / 'attempt.log')
    return budget, key


def test_command_supervisor_records_actual_completion_without_parent_finish(tmp_path):
    budget, key = _reserved(tmp_path)
    output = tmp_path / 'output.txt'
    command = [sys.executable, '-c', 'from pathlib import Path; import sys; '
               'Path(sys.argv[1]).write_text("completed"); raise SystemExit(3)', str(output)]
    result = subprocess.run(budget.command_argv(key, command), capture_output=True, text=True)
    assert result.returncode == 3, result.stderr
    assert output.read_text() == 'completed'
    row = _attempt(budget)
    assert row['status'] == 'completed' and row['completion']['returncode'] == 3
    assert 0 < row['seconds'] < 100
    assert row['owner']['pid'] != row['child']['pid']
    assert Path(row['log']).with_suffix('.process.json').is_file()
    assert _budget(tmp_path).remaining('code-verify', 0, 100) == (0, 100 - row['seconds'])
    # The coordinator's late finish cannot refund this measured work.
    budget.finish(key, turns=0, seconds=0, status='completed')
    assert _attempt(budget)['seconds'] == row['seconds']


def test_durable_completion_repairs_parent_and_budget_write_interruption(tmp_path):
    budget, key = _reserved(tmp_path)
    command = [sys.executable, '-c', 'pass']
    result = subprocess.run(budget.command_argv(key, command), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    seconds = _attempt(budget)['seconds']
    # Simulate interruption after the independent receipt reached disk but
    # before the corresponding budget update was committed.
    _mutate(budget, status='running', seconds=100, completion=None)
    resumed = _budget(tmp_path)
    resumed.reconcile()
    assert _attempt(budget)['status'] == 'recovered'
    assert resumed.remaining('code-verify', 0, 100) == (0, 100 - seconds)


def test_command_completes_after_coordinator_exits_without_finishing_budget(tmp_path):
    budget = _budget(tmp_path)
    started, release, output = (tmp_path / name for name in ('started', 'release', 'output'))
    work = ('from pathlib import Path; import sys,time; '
            'start,release,out=map(Path,sys.argv[1:]); start.write_text("started"); '
            'deadline=time.monotonic()+5\n'
            'while not release.exists() and time.monotonic()<deadline: time.sleep(.01)\n'
            'out.write_text("completed")')
    command = [sys.executable, '-c', work, str(started), str(release), str(output)]
    coordinator = ('from pathlib import Path; import json,os,subprocess,sys,time; '
        'from galley.execution_budget import ExecutionBudget; '
        'b=ExecutionBudget(Path(sys.argv[1]),"source-sha"); '
        'key,_,_=b.reserve("code-verify",0,100,log_path=Path(sys.argv[2])); '
        'subprocess.Popen(b.command_argv(key,json.loads(sys.argv[3])), '
        'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True); '
        'deadline=time.monotonic()+5\n'
        'while not Path(sys.argv[4]).exists() and time.monotonic()<deadline: time.sleep(.01)\n'
        'os._exit(0)')
    result = subprocess.run([sys.executable, '-c', coordinator, str(budget.path),
        str(tmp_path / 'attempt.log'), json.dumps(command), str(started)], timeout=10)
    assert result.returncode == 0 and started.exists()
    with pytest.raises(ExecutionBudgetError):
        budget.assert_available('code-verify')
    release.write_text('continue')
    deadline = time.monotonic() + 5
    while _attempt(budget)['status'] == 'running' and time.monotonic() < deadline:
        time.sleep(.01)
    assert output.read_text() == 'completed'
    assert _attempt(budget)['status'] == 'completed'
    assert budget.remaining('code-verify', 0, 100)[1] > 0


@pytest.mark.parametrize('field,value', [('reservation_id', 'other'),
    ('source_sha256', 'other'), ('command_sha256', 'other'), ('seconds', -1)])
def test_mismatched_completion_cannot_release_a_reservation(tmp_path, monkeypatch, field, value):
    budget, key = _reserved(tmp_path)
    budget.command_argv(key, ['command'])
    row = _attempt(budget)
    receipt = dict(schema_version=1, reservation_id=key, source_sha256='source-sha',
                   command_sha256=row['command_sha256'], seconds=1, returncode=0,
                   finished_at_ns=time.time_ns())
    receipt[field] = value
    Path(row['log']).with_suffix('.process.json').write_text(json.dumps(receipt))
    monkeypatch.setattr(process, 'process_alive', lambda _: None)
    budget.reconcile()
    assert budget.remaining('code-verify', 0, 100) == (0, 0)
    assert _attempt(budget)['status'] == 'running'


@pytest.mark.parametrize('state', [True, None])
def test_active_or_unknown_owner_blocks_new_work_and_nested_cli_receipts(tmp_path, monkeypatch, state):
    budget, key = _reserved(tmp_path)
    budget.command_argv(key, ['command'])
    monkeypatch.setattr(process, 'process_alive', lambda _: state)
    Path(_attempt(budget)['log']).with_suffix('.stream.jsonl').write_text(
        json.dumps({'num_turns': 0, 'duration_ms': 1000}))
    budget.reconcile(json.loads)
    assert budget.active(key) is state
    with pytest.raises(ExecutionBudgetError, match='active or unknown'):
        budget.assert_available('code-verify')
    with pytest.raises(ExecutionBudgetError, match='reconciliation'):
        budget.reserve('code-verify', 0, 100, log_path=tmp_path / 'next.log')


def test_dead_supervisor_does_not_release_a_live_child(tmp_path, monkeypatch):
    budget, key = _reserved(tmp_path)
    _mutate(budget, owner={'pid': 100, 'birth': 'old'}, child={'pid': 200, 'birth': 'live'})
    monkeypatch.setattr(process, 'process_alive', lambda owner: owner['pid'] == 200)
    budget.reconcile()
    assert budget.active(key) is True
    assert budget.remaining('code-verify', 0, 100) == (0, 0)


def test_provably_dead_code_owner_charges_elapsed_upper_bound_and_allows_only_remainder(tmp_path, monkeypatch):
    budget, key = _reserved(tmp_path)
    _mutate(budget, clock_id='boot', reserved_monotonic_ns=10_000_000_000)
    monkeypatch.setattr(process, 'clock_identity', lambda: 'boot')
    monkeypatch.setattr(process, 'process_alive', lambda _: False)
    monkeypatch.setattr(time, 'monotonic_ns', lambda: 35_000_000_000)
    budget.reconcile()
    row = _attempt(budget)
    assert row['status'] == 'abandoned' and row['completion_known'] is False
    assert row['seconds'] == 25
    assert budget.active(key) is False
    assert budget.remaining('code-verify', 0, 100) == (0, 75)
    budget.assert_available('code-verify')
    _, turns, seconds = budget.reserve('code-verify', 0, 1000, log_path=tmp_path / 'next.log')
    assert (turns, seconds) == (0, 75)


def test_reboot_does_not_invent_unused_time_and_unknown_model_turns_stay_charged(tmp_path, monkeypatch):
    budget, key = _reserved(tmp_path)
    _mutate(budget, clock_id='previous-boot')
    monkeypatch.setattr(process, 'clock_identity', lambda: 'new-boot')
    monkeypatch.setattr(process, 'process_alive', lambda _: False)
    budget.reconcile()
    assert _attempt(budget)['status'] == 'abandoned'
    assert budget.remaining('code-verify', 0, 100) == (0, 0)
    model_key, _, _ = budget.reserve('verify', 10, 100, log_path=tmp_path / 'model.log')
    budget.reconcile()
    assert budget.remaining('verify', 10, 100) == (0, 0)
    assert _attempt(budget)['id'] == model_key and _attempt(budget)['status'] == 'running'


def test_reused_pid_does_not_count_as_the_original_process(monkeypatch):
    monkeypatch.setattr(process.os, 'kill', lambda *_: None)
    monkeypatch.setattr(process, 'process_identity', lambda pid: {'pid': pid, 'birth': 'new-birth'})
    assert process.process_alive({'pid': 123, 'birth': 'old-birth'}) is False
    assert process.process_alive({'pid': 123, 'birth': 'new-birth'}) is True
    assert process.process_alive({'pid': 123, 'birth': None}) is None


def test_zombie_owner_is_provably_finished_even_before_its_parent_reaps_it(monkeypatch):
    monkeypatch.setattr(process.os, 'kill', lambda *_: None)
    monkeypatch.setattr(process, 'process_identity', lambda pid: {
        'pid': pid, 'birth': 'same-birth', 'dead': True})
    assert process.process_alive({'pid': 123, 'birth': 'same-birth'}) is False


def test_delayed_supervisor_and_child_cannot_launch_after_reconciliation(tmp_path, monkeypatch):
    budget, key = _reserved(tmp_path)
    command = ['command']
    budget.command_argv(key, command)
    monkeypatch.setattr(process, 'process_alive', lambda _: False)
    budget.reconcile()
    for child in (False, True):
        with pytest.raises(ExecutionBudgetError, match='closed'):
            budget.claim_process(key, command, child=child)


def test_command_identity_and_unique_claim_are_enforced(tmp_path):
    budget, key = _reserved(tmp_path)
    budget.command_argv(key, ['original'])
    with pytest.raises(ExecutionBudgetError, match='differs'):
        budget.command_argv(key, ['changed'])
    with pytest.raises(ExecutionBudgetError, match='does not match'):
        budget.claim_process(key, ['changed'])
    budget.claim_process(key, ['original'])
    with pytest.raises(ExecutionBudgetError, match='already claimed'):
        budget.claim_process(key, ['original'])
    budget.claim_process(key, ['original'], child=True)
    with pytest.raises(ExecutionBudgetError, match='unique supervisor'):
        budget.claim_process(key, ['original'], child=True)


def test_supervisor_timeout_kills_command_and_preserves_full_time_charge(tmp_path):
    budget = _budget(tmp_path)
    key, _, _ = budget.reserve('code-verify', 0, .25, log_path=tmp_path / 'attempt.log')
    command = [sys.executable, '-c', 'import time; time.sleep(10)']
    result = subprocess.run(budget.command_argv(key, command), capture_output=True, text=True, timeout=10)
    assert result.returncode == 124, result.stderr
    row = _attempt(budget)
    assert row['completion']['limit'] == 'timeout'
    assert row['seconds'] >= .25
    assert budget.remaining('code-verify', 0, .25)[1] <= 0


def test_malformed_coordinator_stream_does_not_interrupt_other_reconciliation(tmp_path):
    budget, key = _reserved(tmp_path, turns=10)
    Path(_attempt(budget)['log']).with_suffix('.stream.jsonl').write_text('{broken')
    budget.reconcile(json.loads)
    assert budget.remaining('verify', 10, 100) == (0, 0)


def test_explicit_cancellation_terminates_the_registered_command_group(tmp_path):
    budget, key = _reserved(tmp_path)
    ready, output = tmp_path / 'ready', tmp_path / 'should-not-exist'
    grandchild_command = [sys.executable, '-c', 'import time; from pathlib import Path; '
        f'time.sleep(10); Path({str(output)!r}).write_text("leaked")']
    work = ('from pathlib import Path; import subprocess,sys,time; '
        f'p=subprocess.Popen({grandchild_command!r}); '
        'Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(10)')
    argv = budget.command_argv(key, [sys.executable, '-c', work, str(ready), str(output)])
    supervisor = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and supervisor.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        grandchild = process.process_identity(int(ready.read_text()))
        assert process.process_alive(grandchild) is True
        budget.terminate_process(key)
        assert supervisor.wait(timeout=5) != 0
        row = _attempt(budget)
        assert row['stop_requested'] is True
        assert row['completion']['returncode'] < 0
        assert process.process_alive(row['child']) is False
        assert process.process_alive(grandchild) is False
        assert not output.exists()
    finally:
        if supervisor.poll() is None:
            budget.terminate_process(key)
            supervisor.wait(timeout=5)
        supervisor.stderr.close()


def test_termination_never_signals_a_reused_pid_and_blocks_late_children(tmp_path, monkeypatch):
    budget, key = _reserved(tmp_path)
    command = ['command']
    budget.command_argv(key, command)
    _mutate(budget, supervisor_claimed=True, child={'pid': 123, 'birth': 'original'})
    monkeypatch.setattr(process, 'process_identity', lambda pid: {'pid': pid, 'birth': 'different'})
    monkeypatch.setattr(process.os, 'killpg', lambda *_: pytest.fail('signalled a reused PID'))
    budget.terminate_process(key)
    assert _attempt(budget)['stop_requested'] is True
    with pytest.raises(ExecutionBudgetError, match='closed'):
        budget.claim_process(key, command, child=True)


def test_wait_rejoins_live_work_without_a_new_reservation_or_spend(tmp_path, monkeypatch):
    budget, key = _reserved(tmp_path)
    monkeypatch.setattr(process, 'process_alive', lambda _: True)
    calls, announced = [], []
    def sleep(seconds):
        calls.append(seconds)
        budget.finish(key, turns=0, seconds=12, status='completed')
    budget.wait_available('code-verify', sleep=sleep, on_wait=announced.append)
    data = json.loads(budget.path.read_text())
    assert len(data['attempts']) == 1
    assert len(calls) == len(announced) == 1
    assert announced[0].key == key and announced[0].known_active is True
    assert budget.remaining('code-verify', 0, 100) == (0, 88)


def test_wait_never_extends_an_existing_deadline_or_waits_on_unknown_owner(tmp_path, monkeypatch):
    budget, key = _reserved(tmp_path)
    monkeypatch.setattr(process, 'process_alive', lambda _: True)
    _mutate(budget, reserved_monotonic_ns=time.monotonic_ns() - 101_000_000_000)
    with pytest.raises(ExecutionBudgetError, match='reserved deadline'):
        budget.wait_available('code-verify', sleep=lambda _: pytest.fail('extended deadline'))
    monkeypatch.setattr(process, 'process_alive', lambda _: None)
    with pytest.raises(ExecutionBudgetError, match='active or unknown'):
        budget.wait_available('code-verify', sleep=lambda _: pytest.fail('waited on unknown owner'))


def test_spawn_setup_failure_explicitly_proves_no_command_started(tmp_path):
    from galley import driver as gd
    spec = gd.PhaseSpec('verify', '', tmp_path, tmp_path / 'phase.log',
                       [str(tmp_path / 'missing-executable')], dict(os.environ))
    with pytest.raises(gd.ProcessNotStartedError) as failure:
        gd.spawn_claude(spec)
    assert isinstance(failure.value.__cause__, FileNotFoundError)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permits unlinking the open output stream')
def test_a_missing_log_after_execution_is_not_a_zero_usage_setup_failure(tmp_path):
    from galley import driver as gd
    marker = tmp_path / 'executed'
    stream = tmp_path / 'phase.stream.jsonl'
    script = ('from pathlib import Path; import sys; '
              'Path(sys.argv[1]).write_text("work happened"); Path(sys.argv[2]).unlink()')
    spec = gd.PhaseSpec('verify', '', tmp_path, tmp_path / 'phase.log',
                       [sys.executable, '-c', script, str(marker), str(stream)], dict(os.environ))
    with pytest.raises(FileNotFoundError):
        gd.spawn_claude(spec)
    assert marker.read_text() == 'work happened'


def test_windows_retains_its_native_job_path_without_posix_process_protocol(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from galley import execution_budget as budgets
    budget, key = _reserved(tmp_path)
    command = ['docproof', 'galley', 'verify', 'run']
    monkeypatch.setattr(budgets, 'os', SimpleNamespace(name='nt'))
    assert budget.command_argv(key, command) == command
    assert 'process_protocol' not in _attempt(budget)
    assert 'command_sha256' not in _attempt(budget)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX supervisor group containment')
def test_successful_command_cannot_leave_a_background_descendant_running(tmp_path):
    budget, key = _reserved(tmp_path)
    ready, release = tmp_path / 'ready', tmp_path / 'release'
    background = [sys.executable, '-c', 'import time; time.sleep(30)']
    script = ('from pathlib import Path; import subprocess,sys,time; '
        f'p=subprocess.Popen({background!r}); '
        'Path(sys.argv[1]).write_text(str(p.pid)); deadline=time.monotonic()+5\n'
        'while not Path(sys.argv[2]).exists() and time.monotonic()<deadline: time.sleep(.01)')
    command = [sys.executable, '-c', script, str(ready), str(release)]
    supervisor = subprocess.Popen(budget.command_argv(key, command),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    descendant = None
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and supervisor.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        descendant = process.process_identity(int(ready.read_text()))
        assert process.process_alive(descendant) is True
        release.write_text('exit normally')
        assert supervisor.wait(timeout=5) == 0
        assert _attempt(budget)['completion']['returncode'] == 0
        assert process.process_alive(descendant) is False
    finally:
        if supervisor.poll() is None:
            budget.terminate_process(key)
            supervisor.wait(timeout=5)
        if descendant and process.process_alive(descendant) is True:
            os.kill(descendant['pid'], 9)
