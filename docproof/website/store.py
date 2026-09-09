"""Durable website records independent of temporary proofreading jobs."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import threading
from typing import Callable
import uuid

from docproof.utils.files import write_atomic

_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identity(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid website record identifier.")
    return value


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


class StoreError(ValueError):
    pass


class Conflict(StoreError):
    pass


class WebsiteStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.projects = self.root / "projects"
        self.projects.mkdir(parents=True, exist_ok=True)
        (self.root / "invitations").mkdir(exist_ok=True)
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self):
        # File lock also serializes a maintenance process against the web worker.
        with self._lock:
            with (self.root / ".lock").open("a+") as handle:
                try:
                    import fcntl
                except ImportError:  # Windows desktop has one process/folder lock.
                    fcntl = None
                if fcntl:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def folder(self, project_id: str) -> Path:
        return self.projects / identity(project_id)

    def _read(self, path: Path, default=None):
        if not path.is_file():
            return default
        return json.loads(path.read_text("utf-8"))

    def _write(self, path: Path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps(value, ensure_ascii=False, indent=2))

    def get(self, project_id: str) -> dict:
        data = self._read(self.folder(project_id) / "project.json")
        if data is None:
            raise StoreError("Website project not found.")
        return data

    def list(self) -> list[dict]:
        return sorted([self._read(p) for p in self.projects.glob("*/project.json")],
                      key=lambda p: p["updated_at"], reverse=True)

    def create(self, *, author_id: str, title: str, hubspot_project_ids: list[str], actor: str) -> dict:
        with self.transaction():
            if any(p["author_id"] == author_id for p in self.list()):
                raise Conflict("This author already has a website project. Open it to add another book.")
            project_id = uuid.uuid4().hex
            project = dict(id=project_id, author_id=author_id, title=title,
                           hubspot_project_ids=hubspot_project_ids, state="waiting_for_inputs",
                           error="", model="", auto_generate=False, source_config={}, destination={},
                           hubspot={}, manuscript=None, assets=[], questionnaire={},
                           questionnaire_submission=None, revisions=[], invitations=[], task=None,
                           draft_revision_id=None, live_revision_id=None, live_url="",
                           deployments=[], crm_sync={"state": "not_configured"},
                           created_by=actor, created_at=now(), updated_at=now(), events=[])
            self._write(self.folder(project_id) / "project.json", project)
            return project

    def mutate(self, project_id: str, change: Callable[[dict], None]) -> dict:
        with self.transaction():
            project = self.get(project_id)
            change(project)
            project["updated_at"] = now()
            self._write(self.folder(project_id) / "project.json", project)
            return project

    def event(self, project: dict, kind: str, actor: str, **details):
        project.setdefault("events", []).append(dict(at=now(), kind=kind, actor=actor, **details))
        project["events"] = project["events"][-250:]

    def source_fingerprint(self, project: dict) -> str:
        return digest({"author_id": project["author_id"],
                       "hubspot_project_ids": project["hubspot_project_ids"],
                       "hubspot": project.get("hubspot", {}),
                       "source_config": project.get("source_config", {}),
                       "questionnaire_submission": project.get("questionnaire_submission"),
                       "manuscript": project.get("manuscript"), "assets": project.get("assets", [])})

    def revision(self, project_id: str, revision_id: str) -> dict:
        revision = self._read(self.folder(project_id) / "revisions" / f"{identity(revision_id)}.json")
        if revision is None:
            raise StoreError("Website revision not found.")
        return revision

    def add_revision(self, project_id: str, *, spec: dict, validation: dict,
                     source_fingerprint: str, actor: str, expected_revision_id: str | None,
                     usage=None, locked_paths: list[str] | None = None) -> dict:
        with self.transaction():
            project = self.get(project_id)
            if project.get("draft_revision_id") != expected_revision_id:
                raise Conflict("This draft changed while you were working. Reload before saving.")
            if self.source_fingerprint(project) != source_fingerprint:
                raise Conflict("The source material changed during this run. Generate a fresh draft.")
            rid = uuid.uuid4().hex
            revision = dict(id=rid, spec=spec, source_fingerprint=source_fingerprint,
                            validation=validation, created_at=now(), created_by=actor,
                            approved_at=None, approval=None, stage=None, staged_preview_url="",
                            usage=usage or {}, locked_paths=locked_paths or [],
                            parent_revision_id=expected_revision_id)
            revision["content_hash"] = digest(spec)
            self._write(self.folder(project_id) / "revisions" / f"{rid}.json", revision)
            project["revisions"].append(rid)
            project["draft_revision_id"] = rid
            project["state"] = "ready_for_staff_review"
            project["error"] = ""
            project["updated_at"] = now()
            self.event(project, "revision_created", actor, revision_id=rid)
            self._write(self.folder(project_id) / "project.json", project)
            return revision

    def mutate_revision(self, project_id: str, rid: str, change: Callable[[dict, dict], None]) -> dict:
        with self.transaction():
            project = self.get(project_id)
            revision = self.revision(project_id, rid)
            change(project, revision)
            project["updated_at"] = now()
            self._write(self.folder(project_id) / "revisions" / f"{identity(rid)}.json", revision)
            self._write(self.folder(project_id) / "project.json", project)
            return revision

    def issue_invitation(self, project_id: str, actor: str, days: int = 14) -> dict:
        token = secrets.token_urlsafe(36)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        invitation = dict(id=uuid.uuid4().hex, token_hash=token_hash,
                          expires_at=(datetime.now(timezone.utc) + timedelta(days=days)).isoformat(),
                          revoked=False, created_at=now(), created_by=actor)
        with self.transaction():
            project = self.get(project_id)
            project["invitations"].append(invitation)
            project["updated_at"] = now()
            self._write(self.folder(project_id) / "project.json", project)
            self._write(self.root / "invitations" / f"{token_hash}.json",
                        {"project_id": project_id, "invitation_id": invitation["id"]})
        return {"token": token, "id": invitation["id"], "expires_at": invitation["expires_at"]}

    def invited(self, token: str) -> tuple[dict, dict]:
        if not token or len(token) > 128:
            raise StoreError("This questionnaire invitation is invalid or has expired.")
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        link = self._read(self.root / "invitations" / f"{token_hash}.json")
        if not link:
            raise StoreError("This questionnaire invitation is invalid or has expired.")
        project = self.get(link["project_id"])
        invitation = next((i for i in project["invitations"] if i["id"] == link["invitation_id"]), None)
        if (not invitation or invitation["revoked"] or
                datetime.fromisoformat(invitation["expires_at"]) <= datetime.now(timezone.utc) or
                not secrets.compare_digest(invitation["token_hash"], token_hash)):
            raise StoreError("This questionnaire invitation is invalid or has expired.")
        return project, invitation

    def settings(self) -> dict:
        return self._read(self.root / "settings.json", {"staff_roles": {}, "automation_enabled": False,
                            "poll_seconds": 120, "max_cost_usd": 5.0, "model_context_tokens": {}})

    def save_settings(self, settings: dict):
        with self.transaction():
            self._write(self.root / "settings.json", settings)

    def cache(self, project_id: str, key: str, value=None):
        path = self.folder(project_id) / "cache" / f"{digest(key)}.json"
        if value is not None:
            self._write(path, value)
        return self._read(path)
