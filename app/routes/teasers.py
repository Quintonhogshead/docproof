"""Cloud worker and administrator interfaces for formatting-triggered teasers."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Literal

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app import teasers
from app.teaser_delivery import deliver, ensure_folder, token_for
from .watch import agent_gate


def may_manage(request: Request):
    if request.app.state.web:
        from app.auth import require_admin
        require_admin(request)


class SettingsUpdate(BaseModel):
    enabled: bool
    folder_id: str | None = None


class WorkerMessage(BaseModel):
    action: Literal["poll", "heartbeat", "story", "draft", "review", "deliver", "error"]
    worker: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    task_id: str = Field(default="", max_length=32, pattern=r"^[a-f0-9]*$")
    payload: dict = Field(default_factory=dict)


def recover_new_jobs(app, queue):
    """Recover completion/enqueue crashes without backfilling historical books."""
    from app.jobs import JobStore
    from app.settings import Paths
    since = queue.settings().get("enabled_since")
    if not since:
        return
    stores = [app.state.store, JobStore(Paths(Path(app.state.watch.home)).ensure())]
    for store in stores:
        for job in store.all():
            if job.is_prep and job.state == "done" and job.created_at >= since:
                teasers.enqueue_completed(app.state.watch.home, job)


def dispatch(app, message):
    home = app.state.watch.home
    queue = teasers.Queue(home)
    if message.action == "poll":
        queue.recover()
        recover_new_jobs(app, queue)
        return {"task": queue.claim(message.worker)}
    if message.action == "heartbeat":
        queue.heartbeat(message.task_id, message.worker, str(message.payload.get("progress", "")))
        return {"ok": True}
    with teasers.lock(queue.root / (message.task_id + ".lock")):
        task = queue.owned(message.task_id, message.worker)
        if not queue.settings().get("enabled") and message.action not in ("error",):
            raise teasers.TeaserError("Author teaser generation is paused.")
        if message.action == "story":
            task = teasers.accept_story(queue, task, message.payload)
        elif message.action == "draft":
            task = teasers.generate_draft(queue, task)
        elif message.action == "review":
            task = teasers.accept_review(queue, task, message.payload)
        elif message.action == "deliver":
            task = deliver(queue, task, home)
        elif message.action == "error":
            if task["state"] not in ("complete", "retry_wait"):
                queue.retry(task, str(message.payload.get("error", "Worker failed")))
                task = queue.get(task["id"])
        return {"task": task}


def register(app):
    @app.post("/api/teasers/worker")
    async def worker(request: Request):
        agent_gate(request)
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > 4 * 1024 * 1024:
                raise HTTPException(413, "The teaser worker message is too large.")
        try:
            message = WorkerMessage.model_validate_json(raw)
            return await run_in_threadpool(dispatch, app, message)
        except (ValueError, TypeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/teasers", dependencies=[Depends(may_manage)])
    def status():
        queue = teasers.Queue(app.state.watch.home)
        return {"settings": queue.settings(), "tasks": queue.list()}

    @app.put("/api/teasers/settings", dependencies=[Depends(may_manage)])
    def configure(update: SettingsUpdate):
        queue = teasers.Queue(app.state.watch.home)
        if update.folder_id is not None and update.folder_id and not all(
                c.isalnum() or c in "_-" for c in update.folder_id):
            raise HTTPException(400, "Invalid Google folder ID.")
        values = {"enabled": update.enabled}
        if update.folder_id is not None:
            values["folder_id"] = update.folder_id
        # Save destination selection but enable only after Google succeeds.
        if update.enabled:
            if update.folder_id is not None:
                queue.configure(folder_id=update.folder_id)
            folder_id = ensure_folder(queue, token_for(app.state.watch.home))
            values["folder_id"] = folder_id
        return queue.configure(**values)
