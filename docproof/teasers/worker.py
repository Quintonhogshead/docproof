"""Fly-only worker: DeepSeek V4 Pro writes, Opus 5.5 adjudicates, the web machine
checks, stores and delivers."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
import uuid

from app.teasers import lock
from . import adjudicator, writer
from .models import Draft

log = logging.getLogger(__name__)
PROTOCOL = 4


class Client:
    def __init__(self, url, token, worker):
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("The cloud teaser server must use an HTTPS URL without embedded credentials.")
        if len(token) < 24:
            raise ValueError("The cloud teaser worker needs DocProof's agent credential.")
        self.url, self.token, self.worker = url.rstrip("/"), token, worker

    def call(self, action, task_id="", payload=None):
        request = urllib.request.Request(self.url + "/api/teasers/worker", method="POST",
            data=json.dumps({"protocol": PROTOCOL, "action": action, "worker": self.worker,
                            "task_id": task_id, "payload": payload or {}}).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                try:
                    detail = json.load(exc).get("detail", "The cloud task must retry.")
                except ValueError:
                    detail = "The cloud task must retry."
                raise ValueError(str(detail)) from exc
            raise


def process(task, client, home, *, write_with=None, adjudicate_with=None):
    work = Path(home) / "books" / task["id"]
    work.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    activity = {"progress": "Starting teaser work", "lost": None}

    def progress(text):
        if activity["lost"]:
            raise RuntimeError("The worker lost its task lease; work has paused.")
        activity["progress"] = text
        client.call("heartbeat", task["id"], {"progress": text})

    def heartbeat():
        while not stop.wait(45):
            try:
                client.call("heartbeat", task["id"], {"progress": activity["progress"]})
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403, 409):
                    activity["lost"] = str(exc)
                    return
            except Exception:
                log.warning("Teaser heartbeat could not reach DocProof")

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    manuscript = task["manuscript"]
    try:
        while task["state"] in ("queued", "revise", "drafted", "approved"):
            if task["state"] == "queued":
                draft, receipts = writer.write(manuscript, work, writer=write_with,
                                               attempt=task.get("failures", 0), progress=progress)
                task = client.call("draft", task["id"], {"draft": draft.model_dump(),
                                                         "receipts": receipts})["task"]
            elif task["state"] == "revise":
                current = Draft.model_validate(task["drafts"][-1]["content"])
                notes = {int(n): note for n, note in task["rewrite"]["notes"].items()}
                draft, receipts = writer.rewrite(manuscript, current, notes, work,
                                                 writer=write_with, progress=progress)
                task = client.call("draft", task["id"], {"draft": draft.model_dump(),
                                                         "receipts": receipts})["task"]
            elif task["state"] == "drafted":
                draft = Draft.model_validate(task["drafts"][-1]["content"])
                ruling, _, receipts = adjudicator.adjudicate(manuscript, draft, work,
                                                             lane=adjudicate_with, progress=progress)
                task = client.call("adjudication", task["id"], {"ruling": ruling.model_dump(),
                                                                "receipts": receipts})["task"]
            elif task["state"] == "approved":
                progress("Uploading and verifying the author Google Doc")
                task = client.call("deliver", task["id"])["task"]
        return task
    finally:
        stop.set()
        thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path("/data/docproof-teasers"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not os.environ.get("FLY_APP_NAME"):
        raise SystemExit("The production teaser worker runs on Fly, never on a user's Mac.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.home.mkdir(parents=True, exist_ok=True)
    identity = args.home / "worker-id.txt"
    with lock(args.home / "worker.lock"):
        if not identity.exists():
            identity.write_text("fly-teasers-" + uuid.uuid4().hex)
        client = Client(os.environ.get("GALLEY_APP_URL", ""),
                        os.environ.get("DOCPROOF_AGENT_TOKEN", ""), identity.read_text().strip())
        while True:
            task = None
            try:
                task = client.call("poll").get("task")
                if task:
                    log.info("Working on teaser task %s", task["id"])
                    result = process(task, client, args.home)
                    log.info("Teaser task %s: %s", task["id"], result["state"])
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                # A lost response may have saved its work on the server; poll
                # the durable state before deciding whether anything must retry.
                log.exception("The teaser server connection was interrupted")
            except Exception as exc:
                log.exception("Teaser task will retry automatically")
                if task:
                    try:
                        from app.teasers import is_transient
                        from docproof.agent_lane import AgentLaneUnavailable
                        from docproof.subscription_limits import UsageLimitError
                        transient = isinstance(exc, (writer.WriterUnavailable, AgentLaneUnavailable,
                                                     UsageLimitError)) or is_transient(exc)
                        client.call("error", task["id"], {"error": str(exc), "transient": transient})
                    except Exception:
                        log.exception("Could not save the teaser error")
            if args.once:
                return
            time.sleep(30)


if __name__ == "__main__":
    main()
