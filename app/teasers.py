"""Durable teaser queue shared by app formatting and DocWatch formatting.

Only saved, reviewed drafts can reach Google. Sol corrections are bounded and
approval is bound to their exact result. Network and per-book writes have process-safe locks.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import re
from pathlib import Path
import sqlite3
import time
import uuid

from docproof import platform_io
from docproof.promo.ingest import read_manuscript
from docproof.providers import strict_json_schema
from docproof.teasers import QWEN_MODEL, SOL_MODEL, VERSION
from docproof.teasers.models import (Draft, Review, Storysheet, WriterBrief, approval_issues,
                                    apply_small_edits, draft_issues, teaser_issues, digest)
from docproof.teasers.pipeline import chunks, validate_story, validate_writer_brief
from docproof.teasers.prompts import writer_prompt

log = logging.getLogger(__name__)
MAX_DRAFTS_PER_CYCLE = 5
MAX_DRAFTS_PER_DAY = 12
INITIAL_WRITER_TOKENS = 16_000
MAX_WRITER_TOKENS = 32_000
LEASE_SECONDS = 240
STATES = ("queued", "brief_ready", "story_ready", "drafted", "approved")


class TeaserError(ValueError):
    pass


# Counted failures back off from two minutes to at most half an hour: a book
# should finish the day it is formatted.
MAX_BACKOFF_SECONDS = 30 * 60
TRANSIENT_CATEGORIES = ("cli_failure", "cli_io", "cli_start", "timeout",
                        "model_unavailable", "subscription_limit")


def is_transient(error):
    """A subscription or transport outage, as opposed to a problem with the book."""
    text = str(error)
    match = re.search(r"Codex subscription review stopped \((\w+)\)", text)
    return bool((match and match.group(1) in TRANSIENT_CATEGORIES) or
                "subscription reviewer is busy" in text or
                "Subscription session" in text)


def transient_delay(error, count):
    if "subscription_limit" in str(error):
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
            row = conn.execute("SELECT id FROM tasks WHERE state IN (?,?,?,?,?) "
                               "AND (worker=? OR lease<?) ORDER BY created LIMIT 1",
                               (*STATES, worker, time.time())).fetchone()
            if not row:
                return None
            conn.execute("UPDATE tasks SET worker=?,lease=? WHERE id=?",
                         (worker, time.time() + LEASE_SECONDS, row["id"]))
        task = self.get(row["id"])
        # Production v2 used one shared brief and a different writer. Restart only
        # unfinished teaser work under the new contract; preserve its full audit.
        if task.get("version") == 2:
            task.setdefault("prior_workflows", []).append({k: task.get(k) for k in
                ("version", "state", "storysheet", "drafts", "reviews", "feedback", "document_id", "document_url", "upload_session")})
            for key in ("storysheet", "writer_brief", "writer_brief_story", "feedback", "resume_state",
                        "writer_token_limit", "error", "generation_times", "document_id", "document_url", "upload_session"):
                task.pop(key, None)
            task.update(version=VERSION, drafts=[], reviews=[], progress="Preparing five individual fact sheets")
            self.save(task, "queued")
            task = self.get(task["id"])
        return task

    def retry(self, task, error, *, delay=None, resume=None, counted=True):
        """Schedule the task's next attempt. An uncounted (transient) failure —
        the subscription busy, down, or rate limited — is not the book's fault:
        it neither grows the backoff nor becomes Sol's editorial feedback."""
        if counted:
            task["failures"] = task.get("failures", 0) + 1
            task["feedback"] = list(task.get("feedback", []))[-20:] + [error[:2000]]
            task.pop("transient_failures", None)
        else:
            task["transient_failures"] = task.get("transient_failures", 0) + 1
            if delay is None:
                delay = transient_delay(error, task["transient_failures"])
        task["error"] = error[:2000]
        task["resume_state"] = resume or task.get("resume_state") or (
            "story_ready" if task["state"] == "generating" else task["state"])
        task["retry_at"] = time.time() + (delay if delay is not None else
                                        min(MAX_BACKOFF_SECONDS, 60 * 2 ** min(task["failures"], 8)))
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
    task["progress"] = "Sol's selected facts are ready for the teaser writer"
    queue.save(task, "story_ready")
    return queue.get(task["id"])


def generate_draft(queue, task, *, provider=None):
    if task["state"] in ("drafted", "approved", "complete"):
        return task
    if task["state"] != "story_ready":
        raise TeaserError("This task cannot generate another draft.")
    if task.get("version", 1) == 3:
        from app.teaser_writer import generate
        return generate(queue, task, provider=provider)
    story = Storysheet.model_validate(task["storysheet"])
    if not story.writer_brief.public_setup or not current_writer_brief(task).author_copy.teasers:
        task.setdefault("prior_storysheets", []).append(task.pop("storysheet"))
        task["progress"] = "Sol is preparing complete copy for Qwen to rephrase"
        queue.save(task, "queued")
        return queue.get(task["id"])
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
        provider = DeepInfraProvider(api_key=key, max_retries=0, effort=None, reasoning_enabled=False)
        provider.client = provider.client.with_options(timeout=840)
    previous = task["drafts"][-1]["content"] if task["drafts"] else None
    retained = {}
    retain_guidance = False
    if previous and task["reviews"]:
        prior = Draft.model_validate(previous)
        review = Review.model_validate(task["reviews"][-1])
        if (review.draft_sha256 == digest(prior) and
                task.get("review_story_hashes", {}).get(digest(review)) == digest(task["storysheet"]) and
                sorted(t.number for t in prior.teasers) == [1, 2, 3, 4, 5] and
                sorted(review.covered_chunk_ids) == [c["id"] for c in task["chunks"]] and
                sorted(o.number for o in review.options) == [1, 2, 3, 4, 5]):
            passed = {o.number for o in review.options if all((o.accurate, o.spoiler_safe,
                       o.clear, o.faithful_voice, o.distinct_angle))}
            passed -= {e.index for e in review.edits if e.field in ("teaser", "angle")}
            retained = {o.number: o for o in prior.teasers if o.number in passed and not teaser_issues(o)}
            retain_guidance = review.guidance_approved and not any(
                e.field not in ("teaser", "angle") for e in review.edits)
            retain_guidance = retain_guidance and not any(
                not issue.startswith("Option ") for issue in draft_issues(prior))
    brief = current_writer_brief(task)
    validate_writer_brief(brief)
    # A previously approved rephrasing can survive only if Sol kept its baseline.
    old_copy = task["drafts"][-1].get("source_copy") if task["drafts"] else None
    new_copy = brief.author_copy.model_dump()
    old_options = {t["number"]: t for t in old_copy["teasers"]} if old_copy else {}
    new_options = {t["number"]: t for t in new_copy["teasers"]}
    retained = {n: t for n, t in retained.items() if old_options.get(n) == new_options.get(n)}
    retain_guidance = retain_guidance and bool(old_copy) and all(
        old_copy.get(k) == v for k, v in new_copy.items() if k != "teasers")
    # The provider receives a strict allowlist: no manuscript passages, private
    # storysheet fields, internal feedback, or rejected copy (which may spoil it).
    approved_copy = {"teasers": [retained[n].model_dump() for n in sorted(retained)]}
    if retain_guidance:
        approved_copy.update({k: v for k, v in previous.items() if k != "teasers"})
    system, user = writer_prompt(new_copy, approved_copy, sorted(retained))
    task["progress"] = "Qwen is rephrasing Sol's finished copy"
    # If the process dies during generation, require reconciliation rather than
    # submitting a duplicate paid request on the next poll.
    task["generation_times"] = recent + [time.time()]
    task.setdefault("writer_handoffs", []).append({"at": time.time(),
        "author_copy": new_copy, "approved_copy": approved_copy,
        "prompt_sha256": digest({"system": system, "user": user})})
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
        # Preserve only saved text that passed the prior source-bound
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
                               "operation": "generation",
                               "source_copy": new_copy,
                               "retained_from": retained_from,
                               "usage": vars(result.usage)})
        task["small_edit_rounds"] = 0
        task["progress"] = "Waiting for Sol to review the Qwen draft"
        queue.save(task, "drafted")
    except Exception as exc:
        queue.retry({**task, "state": "generating"}, str(exc), resume="story_ready")
        raise
    return queue.get(task["id"])


def accept_review(queue, task, raw):
    review = Review.model_validate(raw)
    if not task["drafts"]:
        raise TeaserError("There is no saved draft to review.")
    last = task["drafts"][-1]
    if (task["state"] in ("approved", "complete") and last.get("operation") == "bounded_correction"
            and last.get("review_sha256") == digest(review)
            and last.get("base_sha256") == review.draft_sha256):
        # The same edit-and-approve decision may be resent after a lost response.
        return task
    same_review = (bool(task["reviews"]) and
                   digest(review) == digest(Review.model_validate(task["reviews"][-1])))
    if (task["state"] == "drafted" and same_review and
            task["drafts"][-1].get("operation") == "bounded_correction" and
            task["drafts"][-1].get("base_sha256") == review.draft_sha256):
        # The correction was saved but its acknowledgement was lost.
        return task
    draft = Draft.model_validate(task["drafts"][-1]["content"])
    if review.draft_sha256 != digest(draft):
        raise TeaserError("Sol reviewed a different draft; approval was not accepted.")
    if task["state"] in ("approved", "complete"):
        if not same_review:
            raise TeaserError("An approved review cannot be replaced.")
        return task
    if task["state"] != "drafted":
        # Recover an acknowledgement lost after a rejected review was saved.
        if same_review:
            return task
        raise TeaserError("This task is not waiting for editorial review.")
    issues = approval_issues(draft, review, [c["id"] for c in task["chunks"]])
    task["reviews"].append(review.model_dump())
    task.setdefault("review_story_hashes", {})[digest(review)] = digest(task["storysheet"])
    task["feedback"] = issues + review.feedback + [o.feedback for o in review.options if o.feedback]
    if review.edits:
        try:
            if (sorted(review.covered_chunk_ids) != [c["id"] for c in task["chunks"]] or
                    sorted(o.number for o in review.options) != [1, 2, 3, 4, 5]):
                raise ValueError("Small corrections require a complete review of the manuscript and all five options.")
            if task.get("small_edit_rounds", 0) >= 2 and not review.approved:
                raise ValueError("Two correction rounds have been used; ask Qwen for the remaining revisions.")
            corrected = apply_small_edits(draft, review.edits,
                {p["id"] for c in task["chunks"] for p in c["paragraphs"]},
                max_edits=40, max_words=320)
            corrected_approval = None
            if review.approved:
                corrected_approval = review.model_copy(update={"draft_sha256": digest(corrected), "edits": []})
                remaining = approval_issues(corrected, corrected_approval, [c["id"] for c in task["chunks"]])
                if remaining:
                    raise ValueError("Sol has unresolved concerns after its corrections: " + "; ".join(remaining))
            task["drafts"].append({"content": corrected.model_dump(), "sha256": digest(corrected),
                "model": SOL_MODEL, "provider": "chatgpt-subscription", "operation": "bounded_correction",
                "base_sha256": digest(draft), "review_sha256": digest(review),
                "source_copy": task["drafts"][-1].get("source_copy"),
                "edits": [e.model_dump() for e in review.edits]})
            task["small_edit_rounds"] = task.get("small_edit_rounds", 0) + 1
            if corrected_approval is not None:
                # Sol explicitly approved the result of these exact edits in
                # this same pass. Rebind the decision to the applied text.
                task.setdefault("correction_approvals", []).append(review.model_dump())
                task["reviews"][-1] = corrected_approval.model_dump()
                task["review_story_hashes"][digest(corrected_approval)] = digest(task["storysheet"])
                task["feedback"] = []
                task["progress"] = "Sol corrected and approved the copy; ready for Google Docs"
                queue.save(task, "approved")
                return queue.get(task["id"])
            task["progress"] = "Sol corrected small errors; checking the corrected package against the manuscript"
            queue.save(task, "drafted")
            return queue.get(task["id"])
        except ValueError as exc:
            task["feedback"].append(str(exc))
    task["progress"] = "Approved; waiting for Google Docs upload" if not issues else "Sol is resolving copy revisions"
    state = "approved" if not issues else "brief_ready"
    generations = sum(d.get("operation") != "bounded_correction" for d in task["drafts"])
    if issues and generations and generations % MAX_DRAFTS_PER_CYCLE == 0:
        # Sol revisits the angle and premise after a stalled revision cycle;
        # completed manuscript readings remain reusable on the cloud worker.
        task.setdefault("prior_storysheets", []).append(task.pop("storysheet"))
        queue.retry(task, "Refreshing the editorial brief after repeated revisions.",
                    delay=300, resume="queued")
        return queue.get(task["id"])
    queue.save(task, state)
    return queue.get(task["id"])


def current_writer_brief(task):
    if task.get("writer_brief_story") == digest(task["storysheet"]):
        return WriterBrief.model_validate(task["writer_brief"])
    return Storysheet.model_validate(task["storysheet"]).writer_brief


def accept_writer_brief(queue, task, raw):
    if (not task["drafts"] or not task["reviews"] or
            raw.get("draft_sha256") != task["drafts"][-1]["sha256"] or
            raw.get("review_sha256") != digest(Review.model_validate(task["reviews"][-1]))):
        raise TeaserError("The revised public brief does not match the current draft and review.")
    brief = WriterBrief.model_validate(raw.get("brief"))
    validate_writer_brief(brief)
    if task["state"] == "story_ready" and digest(brief) == digest(current_writer_brief(task)):
        return task
    if task["state"] != "brief_ready":
        raise TeaserError("This task is not waiting for a revised public brief.")
    task["writer_brief"] = brief.model_dump()
    task["writer_brief_story"] = digest(task["storysheet"])
    task["writer_brief_review"] = raw["review_sha256"]
    task["progress"] = "Sol's corrected brief is ready for the teaser writer"
    queue.save(task, "story_ready")
    return queue.get(task["id"])
