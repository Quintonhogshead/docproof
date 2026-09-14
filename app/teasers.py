"""Durable teaser queue shared by app formatting and DocWatch formatting.

Only Qwen output saved by this server can reach Google. Worker reviews are bound
to its exact hash. Network writes and per-book mutations have process-safe locks.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
import time
import uuid

from docproof import platform_io
from docproof.promo.ingest import read_manuscript
from docproof.providers import strict_json_schema
from docproof.teasers import QWEN_MODEL, VERSION
from docproof.teasers.models import Draft, Review, Storysheet, approval_issues, draft_issues, digest
from docproof.teasers.pipeline import chunks, evidence_for, validate_story
from docproof.teasers.prompts import writer_prompt

log = logging.getLogger(__name__)
MAX_DRAFTS_PER_CYCLE = 5
MAX_DRAFTS_PER_DAY = 12
INITIAL_WRITER_TOKENS = 16_000
MAX_WRITER_TOKENS = 32_000
LEASE_SECONDS = 240
STATES = ("queued", "story_ready", "drafted", "approved")


class TeaserError(ValueError):
    pass


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
        if not self.settings().get("enabled") or not job.is_prep or job.state != "done":
            return None
        source = Path(job.source_path)
        identity = digest({"job_id": job.id, "source_path": str(source.resolve())})[:32]
        with lock(self.root / (identity + ".lock")):
            if self.get(identity):
                return identity
            manuscript = read_manuscript(source)
            source_chunks = chunks(manuscript.text)
            task = {"id": identity, "job_id": job.id, "book_label": Path(job.filename).stem,
                    "owner": job.owner_id, "source_sha256": digest(manuscript.text),
                    "version": VERSION, "chunks": source_chunks, "drafts": [],
                    "reviews": [], "progress": "Waiting for the cloud teaser worker"}
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
        return self.get(row["id"])

    def retry(self, task, error, *, delay=None, resume=None):
        task["failures"] = task.get("failures", 0) + 1
        task["error"] = error[:2000]
        task["feedback"] = list(task.get("feedback", []))[-20:] + [error[:2000]]
        task["resume_state"] = resume or task.get("resume_state") or (
            "story_ready" if task["state"] == "generating" else task["state"])
        task["retry_at"] = time.time() + (delay if delay is not None else
                                        min(6 * 3600, 60 * 2 ** min(task["failures"], 8)))
        task["progress"] = "Automatic retry scheduled; no editorial action required"
        self.save(task, "retry_wait")

    def recover(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT id FROM tasks WHERE state IN ('retry_wait','generating')").fetchall()
        for row in rows:
            with lock(self.root / (row["id"] + ".lock")):
                task = self.get(row["id"])
                if task["state"] == "generating" and task["lease"] < time.time():
                    self.retry(task, "The Qwen connection was interrupted before its result was saved.",
                               resume="story_ready", delay=900)
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
                    "progress", "error", "retry_at", "document_url")}
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


def accept_story(queue, task, raw):
    story = Storysheet.model_validate(raw)
    validate_story(story, task["chunks"])
    if task.get("storysheet"):
        if digest(task["storysheet"]) != digest(story):
            raise TeaserError("The saved storysheet is immutable for this run.")
        return task
    if task["state"] != "queued":
        raise TeaserError("This task is not waiting for a storysheet.")
    task["storysheet"] = story.model_dump()
    task["progress"] = "Storysheet ready; waiting for Qwen"
    queue.save(task, "story_ready")
    return queue.get(task["id"])


def generate_draft(queue, task, *, provider=None):
    if task["state"] in ("drafted", "approved", "complete"):
        return task
    if task["state"] != "story_ready":
        raise TeaserError("This task cannot generate another draft.")
    recent = [t for t in task.get("generation_times", []) if t > time.time() - 86400]
    if len(recent) >= MAX_DRAFTS_PER_DAY:
        queue.retry(task, "The daily generation allowance has been used; retries resume automatically.",
                    delay=max(60, recent[0] + 86400 - time.time()), resume="story_ready")
        return queue.get(task["id"])
    if provider is None:
        from app.settings import get_api_key
        from docproof.providers.deepinfra_provider import DeepInfraProvider
        key = get_api_key("deepinfra")
        if not key:
            raise TeaserError("Add the DeepInfra key to DocProof's cloud settings.")
        provider = DeepInfraProvider(api_key=key, max_retries=0, effort=None)
        provider.client = provider.client.with_options(timeout=840)
    story = Storysheet.model_validate(task["storysheet"])
    previous = task["drafts"][-1]["content"] if task["drafts"] else None
    retained = {}
    retain_guidance = False
    if previous and task["reviews"]:
        prior = Draft.model_validate(previous)
        review = Review.model_validate(task["reviews"][-1])
        if (review.draft_sha256 == digest(prior) and not draft_issues(prior) and
                sorted(review.covered_chunk_ids) == [c["id"] for c in task["chunks"]] and
                sorted(o.number for o in review.options) == [1, 2, 3, 4, 5]):
            passed = {o.number for o in review.options if all((o.accurate, o.spoiler_safe,
                       o.clear, o.faithful_voice, o.distinct_angle))}
            retained = {o.number: o for o in prior.teasers if o.number in passed}
            retain_guidance = review.guidance_approved
    system, user = writer_prompt(story.model_dump(), evidence_for(story.public_facts, task["chunks"]),
                                previous, task.get("feedback"), sorted(retained))
    task["progress"] = "Qwen is writing five teasers and author guidance"
    # If the process dies during generation, require reconciliation rather than
    # submitting a duplicate paid request on the next poll.
    task["generation_times"] = recent + [time.time()]
    queue.save(task, "generating")
    token_limit = min(MAX_WRITER_TOKENS, max(INITIAL_WRITER_TOKENS,
                                          task.get("writer_token_limit", INITIAL_WRITER_TOKENS)))
    try:
        result = provider.complete_structured(model=QWEN_MODEL, system=system, user=user,
                    schema=strict_json_schema(Draft), schema_name="author_teasers", max_tokens=token_limit)
        task.setdefault("generation_receipts", []).append({"at": time.time(),
            "max_tokens": token_limit, "stop_reason": result.stop_reason, "usage": vars(result.usage)})
        if result.stop_reason == "max_tokens":
            task["writer_token_limit"] = min(MAX_WRITER_TOKENS, token_limit * 2)
            queue.retry({**task, "state": "generating"},
                        "Qwen's package was incomplete; retrying with a larger output allowance.",
                        resume="story_ready", delay=30)
            return queue.get(task["id"])
        if result.stop_reason != "ok" or result.parsed is None:
            raise TeaserError("Qwen did not complete the teaser package: " +
                              (result.error or result.stop_reason))
        draft = Draft.model_validate(result.parsed)
        # Preserve only Qwen-authored text that passed the prior source-bound
        # review. The assembled package gets a new hash and a complete new review.
        if retained:
            draft.teasers = [retained.get(o.number, o) for o in draft.teasers]
        if retain_guidance:
            draft = Draft.model_validate({**previous, "teasers": [o.model_dump() for o in draft.teasers]})
        retained_from = ({"draft_sha256": task["drafts"][-1]["sha256"],
                          "model": task["drafts"][-1]["model"],
                          "options": sorted(retained), "guidance": retain_guidance}
                         if retained or retain_guidance else None)
        task["drafts"].append({"content": draft.model_dump(), "sha256": digest(draft),
                               "model": QWEN_MODEL, "provider": "deepinfra",
                               "retained_from": retained_from,
                               "usage": vars(result.usage)})
        task["progress"] = "Waiting for Sol to review the Qwen draft"
        queue.save(task, "drafted")
    except Exception as exc:
        queue.retry({**task, "state": "generating"}, str(exc), resume="story_ready")
        raise
    return queue.get(task["id"])


def accept_review(queue, task, raw):
    review = Review.model_validate(raw)
    if not task["drafts"]:
        raise TeaserError("There is no Qwen draft to review.")
    draft = Draft.model_validate(task["drafts"][-1]["content"])
    if review.draft_sha256 != digest(draft):
        raise TeaserError("Sol reviewed a different draft; approval was not accepted.")
    if task["state"] in ("approved", "complete"):
        if digest(raw) != digest(task["reviews"][-1]):
            raise TeaserError("An approved review cannot be replaced.")
        return task
    if task["state"] != "drafted":
        # Recover an acknowledgement lost after a rejected review was saved.
        if task["reviews"] and digest(raw) == digest(task["reviews"][-1]):
            return task
        raise TeaserError("This task is not waiting for editorial review.")
    issues = approval_issues(draft, review, [c["id"] for c in task["chunks"]])
    task["reviews"].append(review.model_dump())
    task["feedback"] = issues + review.feedback + [o.feedback for o in review.options if o.feedback]
    task["progress"] = "Approved; waiting for Google Docs upload" if not issues else "Qwen revisions needed"
    state = "approved" if not issues else "story_ready"
    if issues and len(task["drafts"]) % MAX_DRAFTS_PER_CYCLE == 0:
        # Sol revisits the angle and premise after a stalled revision cycle;
        # completed manuscript readings remain reusable on the cloud worker.
        task.setdefault("prior_storysheets", []).append(task.pop("storysheet"))
        queue.retry(task, "Refreshing the editorial brief after repeated revisions.",
                    delay=300, resume="queued")
        return queue.get(task["id"])
    queue.save(task, state)
    return queue.get(task["id"])
