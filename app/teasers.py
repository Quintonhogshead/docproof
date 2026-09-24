"""Durable teaser queue shared by app formatting and DocWatch formatting.

The Fly worker runs both models: DeepSeek V4 Pro writes five teasers from the
whole manuscript, Opus 5.5 adjudicates them against it. This side keeps the
queue, re-checks everything the worker hands back, applies the adjudicator's
exact corrections, and publishes only a package the adjudicator passed.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
import time

from docproof import platform_io
from docproof.promo.ingest import read_manuscript
from docproof.subscription_limits import is_usage_limited
from docproof.teasers import ADJUDICATOR_MODEL, VERSION, WRITER_MODEL
from docproof.teasers.models import (Adjudication, Draft, Manuscript, apply_adjudication,
                                     bibliographic, digest, draft_issues, rewrite_options)

log = logging.getLogger(__name__)
# A book whose options keep coming back for rewrites starts over from a fresh write.
MAX_REWRITE_ROUNDS = 2
# Each writer call reads the whole book with reasoning on; this bounds a bad day's bill.
MAX_WRITER_CALLS_PER_DAY = 12
LEASE_SECONDS = 240
STATES = ("queued", "drafted", "revise", "approved")
# Earlier workflows' working states; such a task restarts from its manuscript.
LEGACY_STATES = ("brief_ready", "story_ready", "generating")


class TeaserError(ValueError):
    pass


# Counted failures back off from two minutes to at most half an hour: a book
# should finish the day it is formatted.
MAX_BACKOFF_SECONDS = 30 * 60


def is_transient(error):
    """A subscription or transport outage, as opposed to a problem with the book."""
    text = str(error)
    return is_usage_limited(text) or any(marker in text for marker in (
        "DeepInfra call failed", "not logged in", "Claude turn did not complete",
        "needs Claude Code", "Claude Agent SDK"))


def transient_delay(error, count):
    if is_usage_limited(str(error)):
        return 15 * 60
    # Two minutes while it may be a blip; a longer outage is polled every 15.
    return 120 if count <= 10 else 15 * 60


@contextmanager
def lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        platform_io.flock(stream, platform_io.LOCK_EX)
        try:
            yield
        finally:
            platform_io.flock(stream, platform_io.LOCK_UN)


def _text_of(task):
    """The manuscript text; tasks queued before v4 kept it as evidence chunks."""
    if task.get("manuscript"):
        return task["manuscript"]
    return "\n".join(p["text"] for c in task.get("chunks", []) for p in c["paragraphs"])


class Queue:
    def __init__(self, home):
        self.root = Path(home) / "teasers"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "queue.sqlite3"
        with self.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, "
                         "state TEXT NOT NULL, worker TEXT NOT NULL DEFAULT '', "
                         "lease REAL NOT NULL DEFAULT 0, created REAL NOT NULL, payload TEXT NOT NULL)")

    def connect(self):
        conn = sqlite3.connect(self.db, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def settings(self):
        path = self.root / "settings.json"
        return json.loads(path.read_text()) if path.exists() else {"enabled": False}

    def configure(self, **values):
        with lock(self.root / "settings.lock"):
            settings = self.settings()
            if values.get("enabled") and not settings.get("enabled"):
                settings["enabled_since"] = datetime.now(timezone.utc).isoformat()
            settings.update(values)
            path = self.root / "settings.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(settings, indent=2))
            temporary.replace(path)
            return settings

    def add(self, job):
        if not self.settings().get("enabled") or not job.is_prep or job.state not in ("running", "done"):
            return None
        source = Path(job.source_path)
        identity = digest({"job_id": job.id, "source_path": str(source.resolve())})[:32]
        with lock(self.root / (identity + ".lock")):
            if self.get(identity):
                return identity
            manuscript = read_manuscript(source)
            if not manuscript.text.strip():
                raise TeaserError("The formatting manuscript contains no readable text.")
            task = {"id": identity, "job_id": job.id, "book_label": Path(job.filename).stem,
                    "owner": job.owner_id, "source_sha256": digest(manuscript.text),
                    "version": VERSION, "manuscript": manuscript.text, "drafts": [],
                    "adjudications": [], "progress": "Waiting for the cloud teaser worker"}
            with self.connect() as conn:
                conn.execute("INSERT OR IGNORE INTO tasks(id,state,created,payload) VALUES (?,?,?,?)",
                             (identity, "queued", time.time(), json.dumps(task)))
        return identity

    def get(self, task_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return None
        return {**json.loads(row["payload"]), "state": row["state"],
                "worker": row["worker"], "lease": row["lease"]}

    def save(self, task, state=None):
        payload = {k: v for k, v in task.items() if k not in ("state", "worker", "lease")}
        with self.connect() as conn:
            conn.execute("UPDATE tasks SET state=?,payload=? WHERE id=?",
                         (state or task["state"], json.dumps(payload), task["id"]))

    def claim(self, worker):
        if not self.settings().get("enabled"):
            return None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT id FROM tasks WHERE state IN (?,?,?,?) "
                               "AND (worker=? OR lease<?) ORDER BY created LIMIT 1",
                               (*STATES, worker, time.time())).fetchone()
            if not row:
                return None
            conn.execute("UPDATE tasks SET worker=?,lease=? WHERE id=?",
                         (worker, time.time() + LEASE_SECONDS, row["id"]))
        task = self.get(row["id"])
        if task.get("version", 1) < VERSION:
            # Earlier workflows (Sol fact sheets, Qwen rephrasing) restart under
            # this one from their saved manuscript; their audit trail is kept.
            archived = {k: task.pop(k) for k in list(task) if k not in (
                "id", "job_id", "book_label", "owner", "source_sha256", "prior_workflows",
                "state", "worker", "lease", "created")}
            text = _text_of(archived)
            archived.pop("chunks", None)
            task.setdefault("prior_workflows", []).append(archived)
            task.update(version=VERSION, manuscript=text, drafts=[], adjudications=[],
                        progress="Restarting under the DeepSeek writer and Opus adjudicator")
            self.save(task, "queued")
            task = self.get(task["id"])
        if task["state"] in ("queued", "revise"):
            recent = [t for t in task.get("generation_times", []) if t > time.time() - 86400]
            if len(recent) >= MAX_WRITER_CALLS_PER_DAY:
                self.retry(task, "Daily writer allowance reached; resuming automatically.",
                           delay=max(60, recent[0] + 86400 - time.time()), counted=False)
                return None
        return task

    def retry(self, task, error, *, delay=None, resume=None, counted=True):
        """Schedule the task's next attempt. An uncounted (transient) failure —
        the subscription busy or rate limited, DeepInfra down — is not the
        book's fault and does not grow the backoff."""
        if counted:
            task["failures"] = task.get("failures", 0) + 1
            task.pop("transient_failures", None)
        else:
            task["transient_failures"] = task.get("transient_failures", 0) + 1
            if delay is None:
                delay = transient_delay(error, task["transient_failures"])
        task["error"] = error[:2000]
        task["resume_state"] = resume or task.get("resume_state") or task["state"]
        task["retry_at"] = time.time() + (delay if delay is not None else
                                        min(MAX_BACKOFF_SECONDS, 60 * 2 ** min(task["failures"], 8)))
        task["progress"] = "Automatic retry scheduled; no editorial action required"
        self.save(task, "retry_wait")

    def recover(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT id FROM tasks WHERE state IN ('retry_wait',?,?,?)",
                                LEGACY_STATES).fetchall()
        for row in rows:
            with lock(self.root / (row["id"] + ".lock")):
                task = self.get(row["id"])
                if task["state"] in LEGACY_STATES:
                    self.save(task, "queued")
                elif task["state"] == "retry_wait" and task.get("retry_at", 0) <= time.time():
                    state = task.pop("resume_state", "queued")
                    if state not in STATES:
                        state = "queued"
                    task.pop("error", None)
                    task["progress"] = "Resuming automated teaser generation"
                    self.save(task, state)

    def owned(self, task_id, worker):
        task = self.get(task_id)
        if not task or task["worker"] != worker or task["lease"] < time.time():
            raise TeaserError("This worker no longer owns that teaser task.")
        return task

    def heartbeat(self, task_id, worker, progress):
        with self.connect() as conn:
            changed = conn.execute("UPDATE tasks SET lease=? WHERE id=? AND worker=? AND lease>=?",
                                   (time.time() + LEASE_SECONDS, task_id, worker, time.time())).rowcount
        if not changed:
            raise TeaserError("This worker no longer owns that teaser task.")
        # Heartbeats never write the full payload, which would race a draft save.
        (self.root / (task_id + ".progress.json")).write_text(json.dumps({
            "progress": progress[:250], "at": time.time()}))

    def list(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT id FROM tasks ORDER BY created DESC LIMIT 200").fetchall()
        result = []
        for row in rows:
            task = self.get(row["id"])
            item = {k: task.get(k) for k in ("id", "job_id", "book_label", "owner", "state",
                    "progress", "error", "retry_at", "document_url", "guide_url", "folder_url")}
            path = self.root / (task["id"] + ".progress.json")
            if path.exists() and task["state"] in STATES:
                try:
                    item.update(json.loads(path.read_text()))
                except ValueError:
                    pass
            result.append(item)
        return result


def enqueue_completed(home, job):
    """A teaser failure never changes the completed formatting result."""
    try:
        return Queue(home).add(job)
    except Exception:
        log.exception("Could not enqueue author teasers for %s", job.id)
        return None


def current_draft(task):
    return Draft.model_validate(task["drafts"][-1]["content"])


def accept_draft(queue, task, payload):
    """A package from the writer: a fresh write (queued) or a rewrite of the
    options the adjudicator returned (revise)."""
    draft = bibliographic(Draft.model_validate(payload.get("draft")), Manuscript(_text_of(task)))
    if task["drafts"] and task["drafts"][-1]["sha256"] == digest(draft) and task["state"] == "drafted":
        return task  # The save succeeded; its acknowledgement was lost.
    if task["state"] not in ("queued", "revise"):
        raise TeaserError("This task is not waiting for a teaser package.")
    issues = draft_issues(draft)
    if issues:
        raise TeaserError("The package fails the content checks: " + "; ".join(issues))
    operation = "generation"
    if task["state"] == "revise":
        previous = current_draft(task)
        flagged = set(task["rewrite"]["options"])
        kept = {t.number: t for t in previous.teasers}
        if any(t != kept[t.number] for t in draft.teasers if t.number not in flagged):
            raise TeaserError("A rewrite may change only the options the adjudicator returned.")
        draft = draft.model_copy(update={"title": previous.title, "author": previous.author})
        operation = "rewrite"
    receipts = list(payload.get("receipts") or [])
    task.setdefault("generation_receipts", []).extend(receipts)
    task["generation_times"] = [t for t in task.get("generation_times", []) if t > time.time() - 86400] + \
        [r.get("at", time.time()) for r in receipts]
    task["drafts"].append({"content": draft.model_dump(), "sha256": digest(draft), "model": WRITER_MODEL,
                           "provider": "deepinfra", "operation": operation, "receipts": receipts})
    if operation == "generation":
        task["rewrite_rounds"] = 0
    task.pop("rewrite", None)
    task.pop("error", None)
    task["progress"] = "Opus 5.5 is checking the teasers against the manuscript"
    queue.save(task, "drafted")
    return queue.get(task["id"])


def accept_adjudication(queue, task, payload):
    ruling = Adjudication.model_validate(payload.get("ruling"))
    if task["adjudications"] and task["adjudications"][-1]["sha256"] == digest(ruling) and \
            task["state"] in ("approved", "revise", "complete", "retry_wait"):
        return task  # Already applied; its acknowledgement was lost.
    if task["state"] != "drafted":
        raise TeaserError("This task is not waiting for adjudication.")
    draft = current_draft(task)
    try:
        corrected = apply_adjudication(draft, ruling, Manuscript(_text_of(task)))
    except ValueError as exc:
        raise TeaserError(f"The adjudication was refused: {exc}") from exc
    task["adjudications"].append({"ruling": ruling.model_dump(), "sha256": digest(ruling),
                                  "draft_sha256": digest(draft), "model": ADJUDICATOR_MODEL,
                                  "receipts": list(payload.get("receipts") or [])})
    if ruling.corrections:
        task["drafts"].append({"content": corrected.model_dump(), "sha256": digest(corrected),
                               "model": ADJUDICATOR_MODEL, "provider": "claude-subscription",
                               "operation": "correction", "base_sha256": digest(draft),
                               "adjudication_sha256": digest(ruling),
                               "corrections": [c.model_dump() for c in ruling.corrections]})
    notes = rewrite_options(ruling)
    if not notes:
        task["progress"] = "Adjudicated; waiting for Google Docs upload"
        queue.save(task, "approved")
        return queue.get(task["id"])
    task["rewrite_rounds"] = task.get("rewrite_rounds", 0) + 1
    if task["rewrite_rounds"] > MAX_REWRITE_ROUNDS:
        queue.retry(task, f"Option(s) {', '.join(map(str, sorted(notes)))} still failed adjudication "
                    f"after {MAX_REWRITE_ROUNDS} rewrites; writing a fresh package.", resume="queued")
        return queue.get(task["id"])
    task["rewrite"] = {"options": sorted(notes), "notes": {str(n): note for n, note in notes.items()}}
    task["progress"] = f"DeepSeek is rewriting option(s) {', '.join(map(str, sorted(notes)))}"
    queue.save(task, "revise")
    return queue.get(task["id"])


def approval_issues(task):
    """Publish only the exact package the last adjudication passed."""
    if not task["drafts"] or not task["adjudications"]:
        return ["There is no adjudicated package."]
    last, ruling = task["drafts"][-1], task["adjudications"][-1]
    decision = Adjudication.model_validate(ruling["ruling"])
    issues = draft_issues(Draft.model_validate(last["content"]))
    if rewrite_options(decision):
        issues.append("The adjudicator returned options for rewriting.")
    if decision.corrections:
        bound = (last.get("operation") == "correction" and last.get("adjudication_sha256") == ruling["sha256"]
                 and last.get("base_sha256") == ruling["draft_sha256"])
    else:
        bound = last["sha256"] == ruling["draft_sha256"]
    if not bound:
        issues.append("The package to publish is not the one the adjudicator passed.")
    return issues
