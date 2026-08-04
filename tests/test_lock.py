"""One DocProof per app home.

The failure this prevents is expensive rather than cosmetic: a second copy
adopts the first's in-flight review, resets its progress, and pays a vendor to
review the same manuscript twice. Some of these tests take the lock from a
real second process, because that is the only way to prove a kernel lock does
what the docstring claims.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from app.lock import FolderInUse, FolderLock, LOCK_NAME
from app.main import create_app

ROOT = "import sys; sys.path.insert(0, %r)" % str(
    __import__("pathlib").Path(__file__).resolve().parent.parent)


def _holder_script(root: str, hold_seconds: float = 30) -> str:
    """A second process that takes the lock and sits on it."""
    return textwrap.dedent(f"""
        {ROOT}
        import time
        from app.lock import FolderLock
        lock = FolderLock({root!r}).acquire()
        print("held", flush=True)
        time.sleep({hold_seconds})
    """)


@pytest.fixture
def holder(tmp_path):
    """A live second DocProof holding tmp_path, killed when the test ends."""
    proc = subprocess.Popen([sys.executable, "-c", _holder_script(str(tmp_path))],
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held", "holder never started"
    yield proc
    proc.kill()
    proc.wait(timeout=10)


# --- the lock itself ----------------------------------------------------------

def test_the_first_copy_gets_the_folder(tmp_path):
    lock = FolderLock(tmp_path).acquire()
    assert lock.held
    assert (tmp_path / LOCK_NAME).is_file()
    lock.release()
    assert not lock.held


def test_a_second_copy_is_refused_and_told_who_has_it(tmp_path, holder):
    with pytest.raises(FolderInUse) as caught:
        FolderLock(tmp_path).acquire()

    assert caught.value.owner["pid"] == holder.pid
    message = str(caught.value)
    assert str(tmp_path) in message
    # The message has to be actionable, not just a refusal: which copy to go
    # and close, and what to do if you meant to run two.
    assert str(holder.pid) in message
    assert "terminal" in message              # not the packaged app
    assert "Quit the other one" in message
    assert "--home" in message


def test_the_folder_is_free_once_the_holder_dies(tmp_path, holder):
    """The whole reason this is a kernel lock and not a PID in a file: no
    cleanup runs when a process is killed, and the lock still has to lift."""
    with pytest.raises(FolderInUse):
        FolderLock(tmp_path).acquire()

    holder.kill()
    holder.wait(timeout=10)

    lock = FolderLock(tmp_path).acquire()     # must not raise
    assert lock.held
    lock.release()


def test_a_released_folder_can_be_claimed_again(tmp_path):
    FolderLock(tmp_path).acquire().release()
    second = FolderLock(tmp_path).acquire()
    assert second.held
    second.release()


def test_a_leftover_lock_file_is_not_mistaken_for_a_live_one(tmp_path):
    """The file outlives the lock on purpose — deleting it would race a copy
    that has already claimed the folder. So a stale file must mean nothing."""
    FolderLock(tmp_path).acquire().release()
    assert (tmp_path / LOCK_NAME).is_file()
    owner = json.loads((tmp_path / LOCK_NAME).read_text())
    assert owner["pid"]                        # a real, now-irrelevant pid

    lock = FolderLock(tmp_path).acquire()      # must not raise
    assert lock.held
    lock.release()


def test_releasing_twice_is_harmless(tmp_path):
    lock = FolderLock(tmp_path).acquire()
    lock.release()
    lock.release()


def test_releasing_a_lock_never_taken_is_harmless(tmp_path):
    FolderLock(tmp_path).release()


def test_acquiring_twice_from_one_process_is_not_self_deadlock(tmp_path):
    """A process cannot lock itself out of its own folder."""
    lock = FolderLock(tmp_path)
    assert lock.acquire() is lock.acquire()
    lock.release()


def test_it_works_as_a_context_manager(tmp_path):
    with FolderLock(tmp_path) as lock:
        assert lock.held
    assert not lock.held


def test_different_folders_do_not_collide(tmp_path):
    a = FolderLock(tmp_path / "a").acquire()
    b = FolderLock(tmp_path / "b").acquire()
    assert a.held and b.held
    a.release()
    b.release()


# --- the app ------------------------------------------------------------------

def test_a_second_app_over_a_taken_folder_refuses_to_build(tmp_path, holder):
    """The bug in full: this is what let a checkout's server start alongside
    the packaged app, adopt its running review and bill for it twice."""
    with pytest.raises(FolderInUse):
        create_app(tmp_path)


def test_an_app_that_runs_nothing_does_not_claim_the_folder(tmp_path, holder):
    """Only the runner does damage. An app built without one just reads, so it
    must not be blocked — every test in this suite depends on that."""
    app = create_app(tmp_path, start_runner=False)
    assert app.state.lock is None


def test_the_packaged_app_and_a_terminal_copy_are_named_differently(
        monkeypatch, tmp_path):
    """"Quit the other copy" is only useful if you know which one it is."""
    from app import lock as locklib

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert "DocProof app" in locklib.describe_owner(
        locklib._describe(tmp_path))

    monkeypatch.delattr(sys, "frozen", raising=False)
    assert "terminal" in locklib.describe_owner(locklib._describe(tmp_path))
