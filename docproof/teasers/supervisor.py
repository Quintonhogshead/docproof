"""Run the teaser sidecar beside the existing Fly book worker, with clean shutdown."""
import os
import signal
import subprocess
import sys
import time


def main():
    command = sys.argv[1:]
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("A main worker command is required.")
    children = []
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    primary = subprocess.Popen(command, start_new_session=True)
    children.append(primary)
    sidecar = None
    next_start = 0
    try:
        while not stopping and primary.poll() is None:
            if (sidecar is None or sidecar.poll() is not None) and time.monotonic() >= next_start:
                if sidecar in children:
                    children.remove(sidecar)
                sidecar = subprocess.Popen([sys.executable, "-m", "docproof.teasers.worker"],
                                           start_new_session=True)
                children.append(sidecar)
                next_start = time.monotonic() + 30
            time.sleep(1)
    finally:
        stop(None, None)
        for child in children:
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
    return primary.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())
