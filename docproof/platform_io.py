"""OS primitives shared by the native interior worker and subscription runner."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import time
import subprocess
from contextlib import contextmanager

LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8


def flock(file, operation: int) -> None:
    fd = file if isinstance(file, int) else file.fileno()
    if os.name != "nt":
        import fcntl
        fcntl.flock(fd, operation)
        return
    import msvcrt
    # Reserve a byte beyond metadata, so another process can still read owner
    # information. Windows supports locks beyond EOF without extending the file.
    position = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, 0x7ffffffe, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK if operation & LOCK_UN else msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if operation & LOCK_UN or exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if operation & LOCK_NB:
                    raise BlockingIOError(errno.EAGAIN, "Worker lock is held") from exc
                time.sleep(0.1)
    finally:
        os.lseek(fd, position, os.SEEK_SET)


def private_path(path: Path, mode: int) -> None:
    """Protect credentials/evidence with real Windows ACLs, not chmod's read-only bit."""
    if os.name != "nt":
        path.chmod(mode)
        return
    import ntsecuritycon
    import win32api
    import win32security
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid)
    inheritance = (win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE) if path.is_dir() else 0
    acl = win32security.ACL()
    for sid in (user, system):
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, inheritance, ntsecuritycon.FILE_ALL_ACCESS, sid)
    win32security.SetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, acl, None)


def sync_directory(path: Path) -> None:
    # Windows does not permit opening a directory with os.open; files are flushed
    # before their atomic replacement. POSIX also flushes the containing directory.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def user_lock_suffix() -> str:
    if os.name != "nt":
        return str(os.getuid())
    import hashlib
    return hashlib.sha256(str(Path.home()).casefold().encode()).hexdigest()[:16]


def terminate_process_tree(proc, *, force=False) -> None:
    if os.name == "nt":
        import win32job
        # A handle to our own job works even when process enumeration/taskkill
        # is unavailable. It never includes an interactive InDesign/Codex app.
        win32job.TerminateJobObject(proc._docproof_job, 1)
    else:
        import signal
        try:
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass


@contextmanager
def process_job(proc):
    """Contain Windows model subprocesses and terminate any descendants on exit."""
    if os.name != "nt":
        yield
        return
    import win32api
    import win32job
    job = None
    try:
        job = win32job.CreateJobObject(None, '')
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        win32job.AssignProcessToJobObject(job, int(proc._handle))
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        finally:
            if job is not None:
                win32api.CloseHandle(job)
        raise
    try:
        proc._docproof_job = job
        yield
    finally:
        win32api.CloseHandle(job)


def run_bounded(argv, *, capture_output=False, encoding=None, timeout=None, **kwargs):
    """subprocess.run for a Windows helper whose redirector may spawn children."""
    if os.name != 'nt':
        return subprocess.run(argv, capture_output=capture_output, encoding=encoding,
                              timeout=timeout, **kwargs)
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with subprocess.Popen(argv, encoding=encoding, **kwargs) as proc:
        with process_job(proc):
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                terminate_process_tree(proc)
                stdout, stderr = proc.communicate(timeout=5)
                raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from None
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
