"""A POSIX command supervisor whose receipt survives loss of its coordinator.

Both supervisor and command claim the reservation before doing work. A late
child cannot run after reconciliation has closed its abandoned reservation.
Kernel process birth identities distinguish a dead process from a reused PID.
This accounts for execution time only; model spending keeps its own ledger.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def _darwin_info(pid):
    # proc_bsdinfo from the SDK's sys/proc_info.h (PROC_PIDTBSDINFO=3).
    class BSDInfo(ctypes.Structure):
        _fields_ = [("prefix", ctypes.c_uint32 * 12),
                    ("names", ctypes.c_char * 48),
                    ("suffix", ctypes.c_uint32 * 6),
                    ("started_sec", ctypes.c_uint64),
                    ("started_usec", ctypes.c_uint64)]
    info = BSDInfo()
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    result = libproc.proc_pidinfo(pid, 3, ctypes.c_uint64(0),
                                 ctypes.byref(info), ctypes.sizeof(info))
    if result == ctypes.sizeof(info):
        return f"{info.started_sec}:{info.started_usec}", info.prefix[1] == 5  # SZOMB
    return None, False


def clock_identity() -> str | None:
    try:
        if sys.platform.startswith("linux"):
            return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if sys.platform == "darwin":
            libc = ctypes.CDLL(None, use_errno=True)
            stamp = (ctypes.c_long * 2)()
            size = ctypes.c_size_t(ctypes.sizeof(stamp))
            if libc.sysctlbyname(b"kern.boottime", ctypes.byref(stamp),
                                  ctypes.byref(size), None, 0) == 0:
                return f"darwin:{stamp[0]}:{stamp[1]}"
            # Some app sandboxes deny kern.boottime but permit process birth
            # information. PID 1's kernel birth stamp also identifies this boot.
            init, _ = _darwin_info(1)
            return f"darwin-init:{init}" if init else None
    except OSError:
        pass
    return None


def process_identity(pid: int | None = None) -> dict:
    pid = os.getpid() if pid is None else pid
    birth = None
    dead = False
    boot = clock_identity()
    try:
        if sys.platform.startswith("linux"):
            # comm (field 2) may contain spaces and parentheses. Fields after
            # its final ')' begin at state (3); starttime is field 22.
            stat = Path(f"/proc/{pid}/stat").read_text()
            fields = stat[stat.rfind(')') + 2:].split()
            dead = fields[0] in ("Z", "X")
            if boot:
                birth = f"{boot}:{fields[19]}"
        elif sys.platform == "darwin":
            started, dead = _darwin_info(pid)
            if started:
                birth = f"darwin:{started}"
    except (OSError, IndexError, ValueError):
        pass
    return {"pid": pid, "birth": birth, "dead": dead}


def process_alive(identity: dict | None) -> bool | None:
    """True=the recorded process lives; False=provably gone; None=unknown."""
    if not isinstance(identity, dict) or type(identity.get("pid")) is not int:
        return None
    pid = identity["pid"]
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError as exc:
        return False if exc.errno == errno.ESRCH else None
    current = process_identity(pid)
    if current.get("dead"):
        return False  # An unreaped zombie cannot execute or consume more budget.
    if not identity.get("birth") or not current.get("birth"):
        return None
    return current["birth"] == identity["birth"]


def command_hash(argv: list[str]) -> str:
    return hashlib.sha256(json.dumps(argv, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required")
    from galley.execution_budget import ExecutionBudget, ExecutionBudgetError
    budget = ExecutionBudget(Path(args.budget), args.source)
    try:
        row = budget.claim_process(args.key, command, child=args.child)
    except ExecutionBudgetError as exc:
        print(str(exc), file=sys.stderr)
        return 125
    if args.child:
        # Keep the same PID and kernel birth identity when entering the real
        # executable. No work can start before the durable claim above.
        try:
            os.execvpe(command[0], command, os.environ)
        except OSError as exc:
            print(f"Could not start command: {exc}", file=sys.stderr)
            return 127
    child_argv = [sys.executable, "-m", "galley.process_receipt", "--budget",
                  str(budget.path), "--source", args.source, "--key", args.key,
                  "--child", "--", *command]
    started = time.monotonic()
    proc = None
    child_identity = None
    interrupted = None

    def kill_command_group():
        if proc is not None:
            # A surviving group retains its original group ID even after its
            # leader exits. Do not touch a newly reused leader PID, however.
            current = process_identity(proc.pid)
            if (child_identity and child_identity.get("birth") and
                    current.get("birth") and
                    child_identity["birth"] != current["birth"]):
                return
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def stop(signum, _frame):
        nonlocal interrupted
        interrupted = signum
        kill_command_group()

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    returncode, limit = 125, None
    try:
        proc = subprocess.Popen(child_argv, start_new_session=True)
        child_identity = process_identity(proc.pid)
        if interrupted is not None:
            stop(interrupted, None)
        try:
            returncode = proc.wait(timeout=row["reserved_seconds"])
        except subprocess.TimeoutExpired:
            stop(signal.SIGTERM, None)
            returncode = proc.wait()
            limit = "timeout"
        if interrupted is not None and limit is None:
            limit = "interrupted"
    except OSError as exc:
        print(f"Could not start command supervisor: {exc}", file=sys.stderr)
    finally:
        # A command may exit successfully after backgrounding descendants.
        # They still belong to this operation and must stop before its receipt
        # releases the reserved budget, including after coordinator loss.
        kill_command_group()
        if proc is not None and proc.poll() is None:
            stop(signal.SIGTERM, None)
            proc.wait()
        elapsed = max(0.0, time.monotonic() - started)
        if limit == "timeout":
            elapsed = max(elapsed, row["reserved_seconds"])
        budget.complete_process(args.key, seconds=elapsed,
                                returncode=returncode, limit=limit)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if limit == "timeout":
        return 124
    return returncode if returncode >= 0 else 128 - returncode


if __name__ == "__main__":
    raise SystemExit(main())
