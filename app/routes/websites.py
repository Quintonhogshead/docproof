"""Staff Website Studio endpoints. Invitations use separate, bearer-only routes."""
from __future__ import annotations

from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.auth import current_user
from app.settings import resource_root
from docproof.providers import MODELS
from docproof.website.models import Questionnaire
from docproof.website.render import TEMPLATES, render_page
from docproof.website.service import WebsiteService, public_assets
from docproof.website.sources import SourceConfig, SourceError, MAX_MANUSCRIPT, MAX_IMAGE
from docproof.website.store import Conflict, StoreError, digest, now
from docproof.website.wordpress import WordPressBridgeError


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateProject(Strict):
    author_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    hubspot_project_ids: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("hubspot_project_ids")
    @classmethod
    def project_ids(cls, values):
        if len(values) != len(set(values)) or any(not re.fullmatch(r"[0-9]{1,30}", v) for v in values):
            raise ValueError("Use distinct numeric HubSpot project record IDs.")
        return values


class Destination(Strict):
    url: str = ""
    site_id: str = ""
    username: str = ""
    password_env: str = ""

    @field_validator("url")
    @classmethod
    def destination_url(cls, value):
        if value:
            p = urlsplit(value)
            if p.scheme != "https" or not p.hostname or p.username or p.password or p.query or p.fragment:
                raise ValueError("Use the HTTPS address of the WordPress installation.")
        return value.rstrip("/")

    @field_validator("password_env")
    @classmethod
    def secret_reference(cls, value):
        if value and not re.fullmatch(r"WEBSITE_WP_[A-Z0-9_]{1,100}", value):
            raise ValueError("Use a server secret name starting WEBSITE_WP_; do not paste the password here.")
        return value

    @field_validator("site_id")
    @classmethod
    def site_id_token(cls, value):
        if value and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}", value):
            raise ValueError("Use the site ID configured in the WordPress bridge.")
        return value


class PatchProject(Strict):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    hubspot_project_ids: list[str] | None = None
    source_config: SourceConfig | None = None
    destination: Destination | None = None
    auto_generate: bool | None = None
    model: str | None = None

    @field_validator("hubspot_project_ids")
    @classmethod
    def project_ids(cls, value):
        return CreateProject.project_ids(value) if value is not None else value


class QuestionnaireSave(Strict):
    answers: dict
    submit: bool = False


class Generation(Strict):
    model: str


class RevisionSave(Strict):
    spec: dict
    expected_revision_id: str | None = None


class RevisionRequest(Generation):
    instructions: str = Field(min_length=1, max_length=4000)
    expected_revision_id: str


class Approve(Strict):
    acknowledged_findings: list[str] = Field(default_factory=list)
    expected_revision_id: str


class Rollback(Strict):
    release_id: str


class AssetEdit(Strict):
    alt: str | None = Field(default=None, max_length=1000)
    approved: bool | None = None
    focal_x: float | None = Field(default=None, ge=0, le=1)
    focal_y: float | None = Field(default=None, ge=0, le=1)


class StudioSettings(Strict):
    staff_roles: dict[str, str] = Field(default_factory=dict)
    automation_enabled: bool = False
    poll_seconds: int = Field(default=120, ge=60, le=86400)
    max_cost_usd: float = Field(default=5.0, gt=0, le=500)
    model_context_tokens: dict[str, int] = Field(default_factory=dict)

    @field_validator("staff_roles")
    @classmethod
    def roles(cls, value):
        if any(role not in ("viewer", "editor", "publisher") for role in value.values()):
            raise ValueError("Website roles are viewer, editor, or publisher.")
        return value

    @field_validator("model_context_tokens")
    @classmethod
    def limits(cls, value):
        if any(not 16384 <= limit <= 2_000_000 for limit in value.values()):
            raise ValueError("Configure supported context limits between 16,384 and 2,000,000 tokens.")
        return value


def register(app: FastAPI):
    service = WebsiteService(app.state.paths.root / "websites", app.state.watch.home)
    app.state.website_service = service
    store = service.store

    def staff(request: Request, action="view"):
        if not app.state.web:
            return {"id": "local", "role": "admin", "admin": True}
        user = current_user(request)
        role = "admin" if user.is_admin else store.settings()["staff_roles"].get(user.id)
        allowed = {"view": {"viewer", "editor", "publisher", "admin"},
                   "edit": {"editor", "publisher", "admin"},
                   "publish": {"publisher", "admin"}, "admin": {"admin"}}
        if role not in allowed[action]:
            raise HTTPException(403, "An administrator must grant you the appropriate Website Studio role.")
        return {"id": user.id, "role": role, "admin": bool(user.is_admin)}

    def origin(request):
        value = request.headers.get("origin")
        if value and urlsplit(value).netloc != request.url.netloc:
            raise HTTPException(403, "Open Website Studio on this site's own address before saving.")

    def gate(action):
        def dependency(request: Request):
            if request.method not in ("GET", "HEAD"):
                origin(request)
            return staff(request, action)
        return dependency

    view, edit, publisher, admin = (gate(a) for a in ("view", "edit", "publish", "admin"))

    @app.exception_handler(StoreError)
    async def store_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409 if isinstance(exc, Conflict) else 404)

    @app.exception_handler(SourceError)
    async def source_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(WordPressBridgeError)
    async def wordpress_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(ValidationError)
    async def schema_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    def summary(p):
        return {k: p.get(k) for k in ("id", "title", "author_id", "state", "error", "model", "auto_generate",
            "live_revision_id", "draft_revision_id", "updated_at", "live_url", "hubspot_project_ids", "crm_sync", "task")}

    @app.get("/api/websites")
    def projects(actor=Depends(view)):
        return {"projects": [summary(p) for p in store.list()], "templates": TEMPLATES,
                "models": [{"id": m.id, "display": m.display} for m in MODELS],
                "web": app.state.web, "can_admin": actor["admin"], "role": actor["role"]}

    @app.get("/api/websites/settings")
    def settings(actor=Depends(admin)):
        return store.settings()

    @app.put("/api/websites/settings")
    def save_settings(payload: StudioSettings, actor=Depends(admin)):
        store.save_settings(payload.model_dump())
        service._wake.set()
        return store.settings()

    @app.post("/api/websites/projects")
    def create(payload: CreateProject, actor=Depends(edit)):
        return store.create(**payload.model_dump(), actor=actor["id"])

    @app.get("/api/websites/projects/{project_id}")
    def details(project_id: str, actor=Depends(view)):
        p = store.get(project_id)
        public = {**p, "invitations": [{k: v for k, v in i.items() if k != "token_hash"} for i in p["invitations"]]}
        revisions = [store.revision(project_id, rid) for rid in reversed(p["revisions"])]
        return {"project": public, "questionnaire": Questionnaire.model_validate(p["questionnaire"]).model_dump(),
                "assets": public_assets(p), "revisions": revisions,
                "draft": store.revision(project_id, p["draft_revision_id"]) if p.get("draft_revision_id") else None,
                "invitations": public["invitations"], "settings": store.settings() if actor["admin"] else {}}

    @app.patch("/api/websites/projects/{project_id}")
    def patch(project_id: str, payload: PatchProject, actor=Depends(edit)):
        updates = payload.model_dump(exclude_none=True)
        if any(k in updates for k in ("destination", "source_config")) and not actor["admin"]:
            raise HTTPException(403, "Only an administrator can configure CRM sources and publication destinations.")
        def apply(p):
            if any(k in updates for k in ("destination", "source_config", "hubspot_project_ids")) and (p.get("task") or {}).get("state") == "running":
                raise Conflict("Wait for this generation to finish before changing its sources.")
            p.update(updates)
            store.event(p, "project_updated", actor["id"])
        result = store.mutate(project_id, apply)
        service.maybe_generate(project_id, actor["id"])
        return result

    @app.post("/api/websites/projects/{project_id}/invitations")
    def invite(project_id: str, request: Request, actor=Depends(edit)):
        result = store.issue_invitation(project_id, actor["id"])
        return {"id": result["id"], "url": str(request.base_url).rstrip("/") + "/website-form#" + result["token"],
                "expires_at": result["expires_at"]}

    @app.post("/api/websites/projects/{project_id}/invitations/{invitation_id}/revoke")
    def revoke(project_id: str, invitation_id: str, actor=Depends(edit)):
        def apply(p):
            matches = [i for i in p["invitations"] if i["id"] == invitation_id]
            if not matches:
                raise StoreError("Invitation not found.")
            matches[0]["revoked"] = True
            store.event(p, "invitation_revoked", actor["id"], invitation_id=invitation_id)
        store.mutate(project_id, apply)
        return {"ok": True}

    @app.post("/api/websites/projects/{project_id}/manuscript")
    async def manuscript(project_id: str, file: UploadFile = File(...), actor=Depends(edit)):
        store.get(project_id)
        body = await file.read(MAX_MANUSCRIPT + 1)
        return service.put_manuscript(project_id, file.filename or "book", body, actor["id"])

    @app.post("/api/websites/projects/{project_id}/assets")
    async def asset(project_id: str, file: UploadFile = File(...), alt: str = Form(""),
                    approved: bool = Form(False), actor=Depends(edit)):
        store.get(project_id)
        return service.put_asset(project_id, file.filename or "image", await file.read(MAX_IMAGE + 1), alt, approved, actor["id"])

    @app.patch("/api/websites/projects/{project_id}/assets/{asset_id}")
    def asset_update(project_id: str, asset_id: str, payload: AssetEdit, actor=Depends(edit)):
        def apply(p):
            matches = [a for a in p["assets"] if a["id"] == asset_id]
            if not matches:
                raise StoreError("Asset not found.")
            matches[0].update(payload.model_dump(exclude_none=True))
            store.event(p, "asset_updated", actor["id"], asset_id=asset_id)
        return store.mutate(project_id, apply)["assets"]

    @app.get("/api/websites/projects/{project_id}/assets/{asset_id}")
    def asset_file(project_id: str, asset_id: str, actor=Depends(view)):
        path, data = service.asset_path(store.get(project_id), asset_id)
        return FileResponse(path, media_type=data["media_type"], headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @app.put("/api/websites/projects/{project_id}/questionnaire")
    def answers(project_id: str, payload: QuestionnaireSave, actor=Depends(edit)):
        return service.save_questionnaire(project_id, payload.answers, submit=payload.submit, actor=actor["id"])

    @app.post("/api/websites/projects/{project_id}/collect")
    def collect(project_id: str, actor=Depends(edit)):
        return service.collect(project_id, actor["id"])

    @app.post("/api/websites/projects/{project_id}/generate")
    def generate(project_id: str, payload: Generation, actor=Depends(edit)):
        return service.enqueue(project_id, model=payload.model, actor=actor["id"])

    @app.post("/api/websites/projects/{project_id}/revise")
    def revise(project_id: str, payload: RevisionRequest, actor=Depends(edit)):
        return service.enqueue(project_id, model=payload.model, actor=actor["id"],
                               instructions=payload.instructions, expected_revision_id=payload.expected_revision_id)

    @app.post("/api/websites/projects/{project_id}/revisions")
    def save_revision(project_id: str, payload: RevisionSave, actor=Depends(edit)):
        return service.edit(project_id, payload.spec, payload.expected_revision_id, actor["id"])

    @app.get("/api/websites/projects/{project_id}/revisions/{rid}/preview/{page}")
    def preview(project_id: str, rid: str, page: str, actor=Depends(view)):
        if page not in ("home", "about", "books", "contact"):
            raise HTTPException(404, "Page not found")
        project, revision = store.get(project_id), store.revision(project_id, rid)
        content = render_page(revision["spec"], page, assets=public_assets(project),
                  asset_base=f"/api/websites/projects/{project_id}/assets",
                  base_url=f"/api/websites/projects/{project_id}/revisions/{rid}/preview", preview=True, release_id=rid)
        return HTMLResponse(content, headers={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex, nofollow",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"})

    @app.post("/api/websites/projects/{project_id}/revisions/{rid}/stage")
    def stage(project_id: str, rid: str, actor=Depends(publisher)):
        return service.stage(project_id, rid, actor["id"])

    @app.post("/api/websites/projects/{project_id}/revisions/{rid}/approve")
    def approve(project_id: str, rid: str, payload: Approve, actor=Depends(publisher)):
        return service.approve(project_id, rid, actor["id"], payload.acknowledged_findings, payload.expected_revision_id)

    @app.post("/api/websites/projects/{project_id}/revisions/{rid}/publish")
    def publish(project_id: str, rid: str, actor=Depends(publisher)):
        return service.publish(project_id, rid, actor["id"])

    @app.post("/api/websites/projects/{project_id}/rollback")
    def rollback(project_id: str, payload: Rollback, actor=Depends(publisher)):
        return service.rollback(project_id, payload.release_id, actor["id"])

    @app.get("/website-form")
    def form_shell():
        return FileResponse(resource_root() / "app/static/websites/questionnaire.html", headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store", "X-Robots-Tag": "noindex"})

    # Bearer-only author access is deliberately outside /api, whose session gate stays deny-by-default.
    limits = {}
    def invitation(request: Request):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(401, "Open your questionnaire invitation link.")
        key = digest(auth)
        stamp = time.monotonic()
        count, until = limits.get(key, (0, stamp + 60))
        if stamp > until:
            count, until = 0, stamp + 60
        if count >= 120:
            raise HTTPException(429, "Please wait a minute before trying again.")
        limits[key] = count + 1, until
        if len(limits) > 1000:
            for expired in [k for k, (_, expires) in limits.items() if expires < stamp]:
                limits.pop(expired, None)
        try:
            return store.invited(auth[7:])
        except StoreError as exc:
            raise HTTPException(401, str(exc)) from exc

    @app.get("/website-forms/session")
    def form_get(request: Request):
        p, inv = invitation(request)
        return JSONResponse({"questionnaire": Questionnaire.model_validate(p["questionnaire"]).model_dump(),
                "project_title": p["title"], "assets": public_assets(p), "templates": TEMPLATES,
                "submitted": bool(p.get("questionnaire_submission")), "expires_at": inv["expires_at"]},
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.put("/website-forms/session")
    def form_save(payload: QuestionnaireSave, request: Request):
        p, inv = invitation(request)
        return service.save_questionnaire(p["id"], payload.answers, submit=False, actor="invitation:" + inv["id"])

    @app.post("/website-forms/submit")
    def form_submit(payload: QuestionnaireSave, request: Request):
        p, inv = invitation(request)
        service.save_questionnaire(p["id"], payload.answers, submit=True, actor="invitation:" + inv["id"])
        return {"submitted": True}

    @app.post("/website-forms/assets")
    async def form_asset(request: Request, file: UploadFile = File(...), alt: str = Form("")):
        p, inv = invitation(request)
        return service.put_asset(p["id"], file.filename or "image", await file.read(MAX_IMAGE + 1),
                                 alt, False, "invitation:" + inv["id"])
