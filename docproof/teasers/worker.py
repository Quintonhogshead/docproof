"""Fly-only worker. Sol uses the cloud ChatGPT login; Qwen and Drive stay on the web machine."""
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

from app.teasers import lock, current_writer_brief
from . import pipeline
from .models import Draft, Storysheet, Review, digest

log = logging.getLogger(__name__)


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
            data=json.dumps({"protocol": 3, "action": action, "worker": self.worker,
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


def process(task, client, home, *, runner=None):
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
    try:
        if task["state"] == "queued":
            story = pipeline.analyze(task["chunks"], work, runner=runner, progress=progress,
                                     feedback=task.get("feedback"), attempt=task.get("failures", 0),
                                     public_briefs=task.get("version", 1) == 3)
            task = client.call("story", task["id"], story.model_dump())["task"]
        while task["state"] in ("brief_ready", "story_ready", "drafted", "approved"):
            if task["state"] == "brief_ready":
                story = Storysheet.model_validate(task["storysheet"])
                brief = pipeline.revise_writer_brief(story, current_writer_brief(task), task.get("feedback", []),
                    task["chunks"], work, runner=runner, progress=progress, attempt=task.get("failures", 0))
                task = client.call("brief", task["id"], {"brief": brief.model_dump(),
                    "draft_sha256": task["drafts"][-1]["sha256"],
                    "review_sha256": digest(Review.model_validate(task["reviews"][-1]))})["task"]
            elif task["state"] == "story_ready":
                progress("Writing five distinct teasers from Sol's selected facts")
                task = client.call("draft", task["id"])["task"]
            elif task["state"] == "drafted":
                story = Storysheet.model_validate(task["storysheet"])
                story.writer_brief = current_writer_brief(task)
                draft = Draft.model_validate(task["drafts"][-1]["content"])
                review = pipeline.review(story, draft, task["chunks"], work,
                                         runner=runner, progress=progress, attempt=task.get("failures", 0))
                task = client.call("review", task["id"], review.model_dump())["task"]
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
    # Share the existing serialized cloud login; do not copy an interactive auth cache.
    os.environ.setdefault("GALLEY_CODEX_HOME", "/data/galley-codex")
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
                        client.call("error", task["id"], {"error": str(exc), "transient": is_transient(exc)})
                    except Exception:
                        log.exception("Could not save the teaser error")
            if args.once:
                return
            time.sleep(30)


if __name__ == "__main__":
    main()
