"""Durable teaser queue shared by app formatting and DocWatch formatting.

The writer (an open-weight model on DeepInfra) produces every published word;
Sol reads, briefs and judges. A review carries findings only, so the copy in a
saved draft is always the writer's. Only a saved, approved draft can reach
Google. Network and per-book writes have process-safe locks.
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
from docproof.providers import strict_json_schema
from docproof.teasers import WRITER_MODEL, WRITER_PROVIDER, VERSION
from docproof.teasers.models import (Draft, Review, Storysheet, approval_issues, draft_issues,
                                    option_passed, teaser_issues, digest)
from docproof.teasers.pipeline import chunks, validate_story, validate_writer_brief
from docproof.teasers.prompts import writer_prompt

log = logging.getLogger(__name__)
# After this many rejected drafts, Sol rebriefs from its own private findings
# instead of sending the writer around the same brief again.
MAX_DRAFTS_PER_CYCLE = 4
# Writer calls per book per rolling day. The writer is cheap; this is a stop
# against a runaway loop, not a budget.
MAX_DRAFTS_PER_DAY = 30
# Mechanical checks (word counts, item counts) are enforced on the writer in a
# tight loop before Sol ever sees a draft: a miss costs one cheap writer call,
# never a Sol review.
GATE_ROUNDS = 3
# The writer thinks before it writes and its reasoning shares the output
# allowance, so the first allowance is generous and doubles once on truncation.
INITIAL_WRITER_TOKENS = 32_000
MAX_WRITER_TOKENS = 64_000
LEASE_SECONDS = 240
# Failures back off from a minute to a quarter hour, never hours: a book that
# needs four corrections should finish today.
MAX_BACKOFF_SECONDS = 15 * 60
# A worker that found the subscription busy did nothing wrong; it just waits.
YIELD_SECONDS = 120
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

    def retry(self, task, error, *, delay=None, resume=None, counted=True):
        """Park the task and come back later. `counted=False` is a yield, not a
        failure: the subscription was busy or the connection dropped, and
        nothing about the book changed. Errors are recorded for the panel and
        never fed to Sol as editorial feedback — that list is review findings
        only, and a transport hiccup is not a reason to write blander copy."""
        if counted:
            task["failures"] = task.get("failures", 0) + 1
        task["error"] = error[:2000]
        task["resume_state"] = resume or task.get("resume_state") or (
            "story_ready" if task["state"] == "generating" else task["state"])
        if delay is None:
            delay = (min(MAX_BACKOFF_SECONDS, 60 * 2 ** min(task.get("failures", 0), 6))
                     if counted else YIELD_SECONDS)
        task["retry_at"] = time.time() + delay
        task["progress"] = ("Waiting for the subscription reviewer" if not counted else
                            "Automatic retry scheduled; no editorial action required")
        self.save(task, "retry_wait")

    def recover(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT id FROM tasks WHERE state IN ('retry_wait','generating')").fetchall()
        for row in rows:
            with lock(self.root / (row["id"] + ".lock")):
                task = self.get(row["id"])
                if task["state"] == "generating" and task["lease"] < time.time():
                    self.retry(task, "The writer connection was interrupted before its result was saved.",
                               resume="story_ready", delay=300)
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
    task["progress"] = "Sol's brief is ready for the writer"
    queue.save(task, "story_ready")
    return queue.get(task["id"])


def revision_context(task):
    """What the writer gets to see of its last attempt: the draft, Sol's
    public-safe notes, and which options passed and must come back unchanged.
    Only a review of the current draft under the current brief counts; after a
    rebrief the writer starts clean."""
    if not task["drafts"] or not task["reviews"]:
        return None, {}, []
    prior = Draft.model_validate(task["drafts"][-1]["content"])
    review = Review.model_validate(task["reviews"][-1])
    if (review.draft_sha256 != digest(prior)
            or task.get("review_story_hashes", {}).get(digest(review)) != digest(task["storysheet"])
            or sorted(t.number for t in prior.teasers) != [1, 2, 3, 4, 5]
            or sorted(review.covered_chunk_ids) != [c["id"] for c in task["chunks"]]
            or sorted(o.number for o in review.options) != [1, 2, 3, 4, 5]):
        return None, {}, []
    options = {t.number: t for t in prior.teasers}
    retained, notes = {}, list(review.writer_notes)
    for check in review.options:
        if option_passed(check) and not check.writer_notes.strip() and not teaser_issues(options[check.number]):
            retained[check.number] = options[check.number]
        elif check.writer_notes.strip():
            notes.append(f"Option {check.number}: {check.writer_notes.strip()}")
        else:
            notes.append(f"Option {check.number}: revise this option; it did not pass editorial review.")
    return prior.model_dump(), retained, notes


def generate_draft(queue, task, *, provider=None):
    if task["state"] in ("drafted", "approved", "complete"):
        return task
    if task["state"] != "story_ready":
        raise TeaserError("This task cannot generate another draft.")
    story = Storysheet.model_validate(task["storysheet"])
    try:
        validate_writer_brief(story.writer_brief)
    except ValueError:
        # A storysheet from before the brief existed: Sol must brief first.
        task.setdefault("prior_storysheets", []).append(task.pop("storysheet"))
        task["progress"] = "Sol is preparing the writer's brief"
        queue.save(task, "queued")
        return queue.get(task["id"])
    recent = [t for t in task.get("generation_times", []) if t > time.time() - 86400]
    if len(recent) >= MAX_DRAFTS_PER_DAY:
        queue.retry(task, "The daily writer allowance has been used; work resumes automatically.",
                    delay=max(60, recent[0] + 86400 - time.time()), resume="story_ready", counted=False)
        return queue.get(task["id"])
    if provider is None:
        from app.settings import get_api_key
        from docproof.providers.deepinfra_provider import DeepInfraProvider
        key = get_api_key(WRITER_PROVIDER)
        if not key:
            raise TeaserError("Add the DeepInfra key to DocProof's cloud settings.")
        # No effort or reasoning switch: the writer keeps its default thinking.
        provider = DeepInfraProvider(api_key=key, max_retries=0, effort=None)
        provider.client = provider.client.with_options(timeout=840)
    previous, retained, notes = revision_context(task)
    brief = story.writer_brief.model_dump()
    # The writer receives a strict allowlist: the public brief, its own last
    # draft, Sol's public-safe notes, and the numbers of options to keep.
    task["progress"] = "The writer is drafting five teasers and the author guide"
    task.setdefault("writer_handoffs", []).append({"at": time.time(), "brief": brief,
        "notes": notes, "retained": sorted(retained), "previous_sha256": digest(previous) if previous else None})
    # If the process dies during generation, require reconciliation rather than
    # submitting a duplicate paid request on the next poll.
    queue.save(task, "generating")
    token_limit = min(MAX_WRITER_TOKENS, max(INITIAL_WRITER_TOKENS,
                                          task.get("writer_token_limit", INITIAL_WRITER_TOKENS)))
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    required, working, draft = [], previous, None
    try:
        for _ in range(GATE_ROUNDS):
            system, user = writer_prompt(brief, working, notes, sorted(retained), required)
            task["generation_times"] = recent + [time.time()]
            recent = task["generation_times"]
            result = provider.complete_structured(model=WRITER_MODEL, system=system, user=user,
                        schema=strict_json_schema(Draft), schema_name="author_teasers", max_tokens=token_limit)
            got = vars(result.usage)
            for key in usage:
                usage[key] += got.get(key, 0) or 0
            task.setdefault("generation_receipts", []).append({"at": time.time(),
                "max_tokens": token_limit, "stop_reason": result.stop_reason, "usage": got})
            if result.stop_reason == "max_tokens":
                if token_limit >= MAX_WRITER_TOKENS:
                    raise TeaserError("The writer's package did not fit the largest output allowance.")
                token_limit = min(MAX_WRITER_TOKENS, token_limit * 2)
                task["writer_token_limit"] = token_limit
                continue
            if result.stop_reason != "ok" or result.parsed is None:
                raise TeaserError("The writer did not complete the teaser package: " +
                                  (result.error or result.stop_reason))
            draft = Draft.model_validate(result.parsed)
            # Options Sol passed come back exactly as reviewed, whatever the
            # writer returned for them; the assembled package is reviewed whole.
            if retained:
                draft.teasers = [retained.get(o.number, o) for o in draft.teasers]
            required = draft_issues(draft)
            if not required:
                break
            working = draft.model_dump()
        else:
            raise TeaserError("The writer's package failed the mechanical checks after "
                              f"{GATE_ROUNDS} rounds: " + "; ".join(required))
        task["drafts"].append({"content": draft.model_dump(), "sha256": digest(draft),
                               "model": WRITER_MODEL, "provider": WRITER_PROVIDER,
                               "operation": "generation", "brief_sha256": digest(brief),
                               "retained_from": ({"draft_sha256": task["drafts"][-1]["sha256"],
                                                  "options": sorted(retained)} if retained else None),
                               "usage": usage, "gate_rounds": len(task["generation_receipts"])})
        task["progress"] = "Waiting for Sol to review the writer's draft"
        queue.save(task, "drafted")
    except Exception as exc:
        queue.retry({**task, "state": "generating"}, str(exc), resume="story_ready")
        raise
    return queue.get(task["id"])


def accept_review(queue, task, raw):
    review = Review.model_validate(raw)
    if not task["drafts"]:
        raise TeaserError("There is no saved draft to review.")
    draft = Draft.model_validate(task["drafts"][-1]["content"])
    if review.draft_sha256 != digest(draft):
        raise TeaserError("Sol reviewed a different draft; approval was not accepted.")
    same_review = (bool(task["reviews"]) and
                   digest(review) == digest(Review.model_validate(task["reviews"][-1])))
    if task["state"] in ("approved", "complete"):
        if not same_review:
            raise TeaserError("An approved review cannot be replaced.")
        return task
    if task["state"] != "drafted":
        # Recover an acknowledgement lost after a rejected review was saved.
        if same_review:
            return task
        raise TeaserError("This task is not waiting for editorial review.")
    chunk_ids = [c["id"] for c in task["chunks"]]
    issues = approval_issues(draft, review, chunk_ids)
    task["reviews"].append(review.model_dump())
    task.setdefault("review_story_hashes", {})[digest(review)] = digest(task["storysheet"])
    if not issues:
        task["feedback"] = []
        task["progress"] = "Approved; waiting for Google Docs upload"
        queue.save(task, "approved")
        return queue.get(task["id"])
    # Private findings, for Sol's own rebrief. The writer sees writer_notes only.
    task["feedback"] = issues + review.feedback + [
        f"Option {o.number}: {o.feedback}" for o in review.options if o.feedback.strip()]
    if len(task["drafts"]) % MAX_DRAFTS_PER_CYCLE == 0:
        # Sol revisits the angles and facts after a stalled revision cycle;
        # completed manuscript readings remain reusable on the cloud worker.
        task.setdefault("prior_storysheets", []).append(task.pop("storysheet"))
        task["progress"] = "Sol is rebriefing the writer after repeated revisions"
        queue.save(task, "queued")
        return queue.get(task["id"])
    task["progress"] = "The writer is revising from Sol's notes"
    queue.save(task, "story_ready")
    return queue.get(task["id"])
