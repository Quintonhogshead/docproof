"""Website lifecycle, bounded worker, source snapshots and exact-release approval."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
import uuid

from app.settings import CONFIG_PATH, get_api_key
from app.watch import hubspot
from docproof.config import load_config
from docproof.providers import build_provider, lookup, provider_for
from docproof.utils.files import write_atomic
from .models import Asset, Questionnaire, SiteSpec, SourceBundle
from .pipeline import WebsitePipeline, PipelineConfig, ModelCapability, validate_spec
from .render import build_pages
from .sources import (SourceConfig, SourceError, collect_records, drive_token,
                      fetch_selected, image_bytes, manuscript_bytes, selected_refs)
from .store import WebsiteStore, StoreError, Conflict, digest, identity, now
from .wordpress import WordPressBridge, WordPressBridgeError

log = logging.getLogger("docproof.website")
ASSET_FIELDS = ("id", "filename", "media_type", "sha256", "alt", "approved", "focal_x", "focal_y")


def public_assets(project: dict) -> list[dict]:
    return [{k: a[k] for k in ASSET_FIELDS if k in a} for a in project.get("assets", [])]


def changed_paths(old, new, prefix="") -> list[str]:
    if isinstance(old, dict) and isinstance(new, dict):
        return [p for k in new for p in changed_paths(old.get(k), new[k], f"{prefix}.{k}".strip("."))]
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        return [p for i, (a, b) in enumerate(zip(old, new)) for p in changed_paths(a, b, f"{prefix}.{i}")]
    return [prefix] if old != new else []


class DiskCache(dict):
    def __init__(self, store: WebsiteStore, project_id: str):
        self.store, self.project_id = store, project_id
        super().__init__()

    def get(self, key, default=None):
        value = self.store.cache(self.project_id, str(key))
        return default if value is None else value

    def __getitem__(self, key):
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        self.store.cache(self.project_id, str(key), value)

    def __contains__(self, key):
        return self.get(key) is not None


class WebsiteService:
    def __init__(self, root: Path, watch_home: Path):
        self.store = WebsiteStore(root)
        self.watch_home = Path(watch_home)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._run_lock = threading.Lock()
        self._publish_locks: dict[str, threading.Lock] = {}
        self._publish_guard = threading.Lock()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # Resume only paid-generation work; uncertain remote publishes need reconciliation.
        for p in self.store.list():
            if (p.get("task") or {}).get("state") == "running":
                self.store.mutate(p["id"], lambda x: x["task"].update(state="queued"))
        self._thread = threading.Thread(target=self._work, name="website-studio", daemon=True)
        self._thread.start()

    def stop(self, join: float = 10):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(join)

    def _work(self):
        next_poll = 0.0
        while not self._stop.is_set():
            try:
                settings = self.store.settings()
                if settings.get("automation_enabled") and time.monotonic() >= next_poll:
                    self.tick()
                    next_poll = time.monotonic() + max(60, settings.get("poll_seconds", 120))
                for project in self.store.list():
                    if self._stop.is_set():
                        break
                    if (project.get("task") or {}).get("state") == "queued":
                        self.run_pending(project["id"])
            except Exception:
                log.exception("Website worker failed a pass")
            self._wake.wait(2)
            self._wake.clear()

    def wait_idle(self, timeout: float = 30) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not any((p.get("task") or {}).get("state") in ("queued", "running") for p in self.store.list()):
                return True
            time.sleep(.02)
        return False

    def tick(self):
        """Independent website watcher. Explicit file IDs work in nested production folders."""
        for project in self.store.list():
            if self._stop.is_set():
                return
            if project.get("crm_sync", {}).get("state") == "pending":
                try:
                    self.sync_crm(project["id"])
                except Exception:
                    log.warning("Website CRM synchronization will retry", exc_info=True)
            if not project.get("auto_generate") or not project.get("questionnaire_submission"):
                continue
            if (project.get("task") or {}).get("state") in ("queued", "running"):
                continue
            try:
                current = self.collect(project["id"], "automation") if project["hubspot_project_ids"] else project
                if current["hubspot_project_ids"] and not current.get("hubspot_ready"):
                    continue
                self.maybe_generate(current["id"], "automation")
            except Exception as exc:
                self.store.mutate(project["id"], lambda p: p.update(state="needs_attention", error=str(exc)))

    def put_manuscript(self, project_id: str, name: str, body: bytes, actor: str, source=None) -> dict:
        suffix, text = manuscript_bytes(name, body)
        sha = hashlib.sha256(body).hexdigest()
        folder = self.store.folder(project_id) / "sources"
        folder.mkdir(exist_ok=True)
        path = folder / f"{sha}{suffix}"
        if not path.exists():
            path.write_bytes(body)
        write_atomic(folder / f"{sha}.txt", text)
        metadata = {"filename": Path(name).name, "sha256": sha, "suffix": suffix,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "source": source or {}}
        def update(p):
            p["manuscript"] = metadata
            p["error"] = ""
            self.store.event(p, "manuscript_updated", actor, sha256=sha)
        result = self.store.mutate(project_id, update)
        self.maybe_generate(project_id, actor)
        return result

    def put_asset(self, project_id: str, name: str, body: bytes, alt: str, approved: bool,
                  actor: str, source=None) -> dict:
        clean, mime, suffix = image_bytes(body)
        sha = hashlib.sha256(clean).hexdigest()
        aid = "a-" + sha[:24]
        path = self.store.folder(project_id) / "assets" / f"{aid}{suffix}"
        path.parent.mkdir(exist_ok=True)
        if not path.exists():
            path.write_bytes(clean)
        asset = dict(id=aid, filename=f"{aid}{suffix}", media_type=mime, sha256=sha,
                     alt=alt.strip()[:1000], approved=approved, focal_x=.5, focal_y=.5,
                     source=source or {})
        def update(p):
            old = next((a for a in p["assets"] if a["id"] == aid), None)
            if old:
                # A repeated Drive sync must preserve staff approval/crop/alt decisions.
                asset.update(approved=old["approved"], alt=old["alt"], focal_x=old["focal_x"], focal_y=old["focal_y"])
                p["assets"] = [asset if a["id"] == aid else a for a in p["assets"]]
            else:
                p["assets"].append(asset)
            self.store.event(p, "asset_added", actor, asset_id=aid)
        self.store.mutate(project_id, update)
        return asset

    def asset_path(self, project: dict, asset_id: str) -> tuple[Path, dict]:
        asset = next((a for a in project["assets"] if a["id"] == identity(asset_id)), None)
        if not asset:
            raise StoreError("Website asset not found.")
        path = self.store.folder(project["id"]) / "assets" / Path(asset["filename"]).name
        if not path.is_file():
            raise StoreError("Website asset file is missing.")
        return path, asset

    def save_questionnaire(self, project_id: str, answers: dict, *, submit: bool, actor: str):
        parsed = Questionnaire.model_validate(answers)
        wire = parsed.model_dump()
        if len(json.dumps(wire)) > 150_000:
            raise SourceError("The questionnaire is too long; shorten pasted text.")
        if submit:
            required = [field for field in ("public_name", "bio", "goal", "book_title") if not wire.get(field)]
            if required:
                raise SourceError("Complete these fields before submitting: " + ", ".join(required))
            if not (wire.get("contact_email") or wire.get("contact_url")):
                raise SourceError("Provide an approved public contact email or contact link.")
            if not parsed.rights_confirmed:
                raise SourceError("Confirm permission to publish the supplied material.")
            if not parsed.cover_asset_id and not parsed.allow_cover_fallback:
                raise SourceError("Select the final cover or choose a typography-only fallback.")
        def update(project):
            known = {a["id"] for a in project["assets"]}
            requested = {x for x in [parsed.cover_asset_id, parsed.portrait_asset_id] +
                         [b.cover_asset_id for b in parsed.additional_books] if x}
            if not requested <= known:
                raise SourceError("An image does not belong to this website project.")
            project["questionnaire"] = wire
            if submit:
                # Explicit rights confirmation applies to the chosen images, not every upload.
                for asset in project["assets"]:
                    if asset["id"] in requested:
                        asset["approved"] = True
                project["questionnaire_submission"] = {"id": uuid.uuid4().hex, "submitted_at": now(),
                                                       "actor": actor, "answers": wire}
                self.store.event(project, "questionnaire_submitted", actor)
                project["error"] = ""
        self.store.mutate(project_id, update)
        if submit:
            self.maybe_generate(project_id, actor)
        return wire

    def collect(self, project_id: str, actor: str) -> dict:
        project = self.store.get(project_id)
        collected = collect_records(project)
        cfg = SourceConfig.model_validate(project["source_config"])
        refs = selected_refs(cfg, collected["references"])
        fetched = {}
        if any(refs.get(k) for k in ("manuscript_file_id", "cover_file_id", "portrait_file_id")):
            token = drive_token(self.watch_home)
            for key in ("manuscript_file_id", "cover_file_id", "portrait_file_id"):
                if refs[key]:
                    meta, body = fetch_selected(token, refs[key], folder_id=refs["folder_id"])
                    fetched[key] = (meta, body)
        # Only replace CRM snapshot after every requested source was successfully collected.
        def update(p):
            if p["source_config"] != project["source_config"] or p["hubspot_project_ids"] != project["hubspot_project_ids"]:
                raise Conflict("Source configuration changed while files were being collected.")
            p["hubspot"] = collected["hubspot"]
            p["hubspot_ready"] = collected["ready"]
            p["last_collected_at"] = now()
            p["error"] = ""
            if not p.get("questionnaire_submission"):
                for form_key, hs_key in {"public_name": "author_name", "book_title": "book_title",
                    "book_subtitle": "book_subtitle", "publication_date": "publication_date",
                    "book_description": "book_description", "retailer_url": "retailer_url"}.items():
                    if not p["questionnaire"].get(form_key):
                        p["questionnaire"][form_key] = collected["hubspot"].get(hs_key, "")
        self.store.mutate(project_id, update)
        for key, (meta, body) in fetched.items():
            source = {"drive_file_id": meta["id"], "modified_time": meta.get("modifiedTime", "")}
            if key == "manuscript_file_id":
                self.put_manuscript(project_id, meta["name"], body, actor, source=source)
            else:
                asset = self.put_asset(project_id, meta["name"], body, "", False, actor, source=source)
                field = "cover_asset_id" if key == "cover_file_id" else "portrait_asset_id"
                self.store.mutate(project_id, lambda p, field=field, aid=asset["id"]:
                                  p["questionnaire"].setdefault(field, aid))
        return self.store.get(project_id)

    def bundle(self, project: dict) -> SourceBundle:
        submission = project.get("questionnaire_submission")
        manuscript = project.get("manuscript")
        if not submission or not manuscript:
            raise SourceError("A submitted questionnaire and selected manuscript are required.")
        if project["hubspot_project_ids"] and not project.get("hubspot"):
            raise SourceError("Collect the linked HubSpot project before generating.")
        path = self.store.folder(project["id"]) / "sources" / f"{manuscript['sha256']}.txt"
        if not path.is_file():
            raise SourceError("The saved manuscript is missing. Upload or collect it again.")
        text = path.read_text("utf-8")
        if hashlib.sha256(text.encode()).hexdigest() != manuscript["text_sha256"]:
            raise SourceError("The saved manuscript changed unexpectedly. Upload it again.")
        return SourceBundle(project_id=project["id"], hubspot_project_ids=project["hubspot_project_ids"],
                            hubspot=project.get("hubspot", {}), questionnaire=submission["answers"],
                            manuscript=text, assets=public_assets(project), fingerprint=self.store.source_fingerprint(project))

    def maybe_generate(self, project_id: str, actor: str):
        project = self.store.get(project_id)
        if not project.get("auto_generate") or not project.get("model"):
            return
        if (project.get("task") or {}).get("state") in ("queued", "running"):
            return
        if project["hubspot_project_ids"] and not project.get("hubspot_ready"):
            return
        try:
            bundle = self.bundle(project)
        except SourceError:
            return
        if project.get("last_attempted_fingerprint") == bundle.fingerprint:
            return
        self.enqueue(project_id, model=project["model"], actor=actor)

    def enqueue(self, project_id: str, *, model: str, actor: str, instructions: str = "",
                expected_revision_id: str | None = None) -> dict:
        if not lookup(model):
            raise SourceError("Select a configured model from the model list.")
        limits = self.store.settings().get("model_context_tokens", {})
        if model not in limits:
            raise SourceError("An administrator must set this model's website context limit in Studio settings.")
        self.bundle(self.store.get(project_id))
        def update(p):
            if (p.get("task") or {}).get("state") in ("queued", "running"):
                raise Conflict("This website already has a queued or running generation.")
            if instructions and p.get("draft_revision_id") != expected_revision_id:
                raise Conflict("The draft changed. Reload before requesting a revision.")
            p["model"] = model
            p["task"] = {"id": uuid.uuid4().hex, "state": "queued", "model": model,
                         "instructions": instructions, "actor": actor,
                         "expected_revision_id": p.get("draft_revision_id"), "queued_at": now()}
            p["state"], p["error"] = "queued", ""
            p["last_attempted_fingerprint"] = self.store.source_fingerprint(p)
        result = self.store.mutate(project_id, update)
        self._wake.set()
        return {"state": "queued", "task_id": result["task"]["id"]}

    def run_pending(self, project_id: str):
        with self._run_lock:
            project = self.store.get(project_id)
            task = project.get("task") or {}
            if task.get("state") != "queued":
                return
            def claim(p):
                if not p.get("task") or p["task"]["id"] != task["id"] or p["task"]["state"] != "queued":
                    raise Conflict("Generation was already claimed.")
                p["task"]["state"] = "running"
                p["state"] = "generating"
            self.store.mutate(project_id, claim)
            try:
                bundle = self.bundle(project)
                cfg = load_config(CONFIG_PATH)
                cfg.api.model = task["model"]
                cfg.api.provider = provider_for(task["model"], cfg.api.provider)
                provider = build_provider(cfg, api_key=get_api_key(cfg.api.provider))
                settings = self.store.settings()
                caps = {m: ModelCapability(context_tokens=int(n), max_output_tokens=8192)
                        for m, n in settings.get("model_context_tokens", {}).items()}
                pipeline = WebsitePipeline(provider, PipelineConfig(capabilities=caps,
                    max_cost_usd=settings.get("max_cost_usd", 5.0)))
                previous = self.store.revision(project_id, task["expected_revision_id"]) if task["expected_revision_id"] else None
                def checkpoint(stage, data):
                    self.store.cache(project_id, f"checkpoint:{task['id']}:{stage}", data)
                    self.store.mutate(project_id, lambda p: p["task"].update(stage=stage))
                kwargs = dict(model=task["model"], previous_spec=SiteSpec.model_validate(previous["spec"]) if previous else None,
                              locked_paths=previous.get("locked_paths", []) if previous else [],
                              checkpoint=checkpoint, cache=DiskCache(self.store, project_id))
                if task["instructions"]:
                    kwargs["instructions"] = task["instructions"]
                result = pipeline.run(bundle, **kwargs)
                usage = asdict(result.usage)
                usage["cost_usd"] = result.cost_usd
                revision = self.store.add_revision(project_id, spec=result.spec.model_dump(),
                    validation=result.validation.model_dump(), source_fingerprint=bundle.fingerprint,
                    actor=task["actor"], expected_revision_id=task["expected_revision_id"], usage=usage,
                    locked_paths=kwargs["locked_paths"])
                self.store.mutate(project_id, lambda p: p["task"].update(state="done", revision_id=revision["id"]))
            except Exception as exc:
                log.exception("Website generation failed for %s", project_id)
                def failed(p):
                    p["state"], p["error"] = "generation_failed", str(exc)
                    p["task"]["state"] = "failed"
                self.store.mutate(project_id, failed)

    def edit(self, project_id: str, spec: dict, expected: str | None, actor: str) -> dict:
        project = self.store.get(project_id)
        parsed = SiteSpec.model_validate(spec)
        previous = self.store.revision(project_id, expected) if expected else None
        form = (project.get("questionnaire_submission") or {}).get("answers", {})
        validation = validate_spec(parsed, assets=public_assets(project),
                                   allow_cover_fallback=form.get("allow_cover_fallback", False)).model_dump()
        validation["findings"].append({"code": "staff_fact_review", "path": "", "severity": "warning",
            "message": "Confirm the facts, links, permissions and requested privacy exclusions in this edited draft."})
        paths = sorted(set((previous or {}).get("locked_paths", []) + changed_paths((previous or {}).get("spec", {}), spec)))
        return self.store.add_revision(project_id, spec=parsed.model_dump(), validation=validation,
            source_fingerprint=self.store.source_fingerprint(project), actor=actor,
            expected_revision_id=expected, locked_paths=paths)

    def _bridge(self, project: dict) -> WordPressBridge:
        dest = project.get("destination") or {}
        if not all(dest.get(k) for k in ("url", "site_id", "username", "password_env")):
            raise SourceError("Configure the Bluehost WordPress destination and install the bridge before staging.")
        password = os.environ.get(dest["password_env"], "")
        if not password:
            raise SourceError("The configured WordPress application-password secret is unavailable.")
        return WordPressBridge(dest["url"], dest["site_id"], dest["username"], password)

    def _publication_lock(self, project_id: str):
        with self._publish_guard:
            return self._publish_locks.setdefault(project_id, threading.Lock())

    def _current(self, project_id: str, rid: str):
        p, r = self.store.get(project_id), self.store.revision(project_id, rid)
        if p.get("draft_revision_id") != rid or r["source_fingerprint"] != self.store.source_fingerprint(p):
            raise Conflict("This preview is stale. Generate or save a current revision before publishing.")
        return p, r

    def stage(self, project_id: str, rid: str, actor: str) -> dict:
        with self._publication_lock(project_id):
            project, revision = self._current(project_id, rid)
            bridge = self._bridge(project)
            assets = public_assets(project)
            approved = [a for a in assets if a["approved"]]
            pages = build_pages(revision["spec"], assets=approved, asset_base="__DOCPROOF_ASSETS__",
                                base_url="__DOCPROOF_BASE__", preview=False, release_id=rid)
            packed = [{**a, "bytes": self.asset_path(project, a["id"])[0].read_bytes()} for a in approved]
            receipt = bridge.stage_release(rid, pages, packed, idempotency_key="stage-" + rid, spec=revision["spec"]).as_dict()
            # Bind every staged artifact, including renderer changes between local builds.
            stage = {**receipt, "destination_hash": digest(project["destination"]),
                     "pages_hash": digest(pages), "staged_at": now()}
            def save(p, r):
                if p.get("draft_revision_id") != rid or self.store.source_fingerprint(p) != r["source_fingerprint"]:
                    raise Conflict("Sources changed while staging. Stage a new revision.")
                r["stage"] = stage
                r["staged_preview_url"] = receipt.get("preview_url", "")
                r["approval"], r["approved_at"] = None, None
                self.store.event(p, "revision_staged", actor, revision_id=rid)
            self.store.mutate_revision(project_id, rid, save)
            return stage

    def approve(self, project_id: str, rid: str, actor: str, acknowledged: list[str], expected: str | None):
        def update(p, r):
            if p.get("draft_revision_id") != rid or expected != rid or self.store.source_fingerprint(p) != r["source_fingerprint"]:
                raise Conflict("Sources or draft changed. Review the latest preview before approving.")
            report = r["validation"]
            if report.get("status") != "passed" or any(f.get("severity") == "error" for f in report.get("findings", [])):
                raise Conflict("Resolve failed or unavailable validation checks before approving.")
            required = {f["code"] for f in report.get("findings", []) if f.get("severity") == "warning"}
            if not required <= set(acknowledged):
                raise Conflict("Acknowledge the review findings before approving this revision.")
            if not r.get("stage") or not r.get("staged_preview_url"):
                raise Conflict("Stage and inspect the actual WordPress preview before approving publication.")
            if r["stage"]["destination_hash"] != digest(p["destination"]):
                raise Conflict("The destination changed. Stage and review this site again.")
            approval = {"actor": actor, "approved_at": now(), "revision_id": rid,
                        "content_hash": digest(r["spec"]), "source_fingerprint": r["source_fingerprint"],
                        "destination_hash": digest(p["destination"]), "stage_hash": digest(r["stage"]),
                        "acknowledged_findings": acknowledged}
            r["approval"], r["approved_at"] = approval, approval["approved_at"]
            p["state"] = "approved"
            self.store.event(p, "revision_approved", actor, revision_id=rid)
        return self.store.mutate_revision(project_id, rid, update)

    def publish(self, project_id: str, rid: str, actor: str) -> dict:
        with self._publication_lock(project_id):
            project = self.store.get(project_id)
            if project["hubspot_project_ids"] and project.get("source_config"):
                self.collect(project_id, actor)
            project, revision = self._current(project_id, rid)
            approval = revision.get("approval") or {}
            stage = revision.get("stage") or {}
            if not approval or any((approval.get("content_hash") != digest(revision["spec"]),
                approval.get("source_fingerprint") != self.store.source_fingerprint(project),
                approval.get("destination_hash") != digest(project["destination"]),
                approval.get("stage_hash") != digest(stage))):
                raise Conflict("This exact release and destination need staff approval before publication.")
            assets = [a for a in public_assets(project) if a["approved"]]
            pages = build_pages(revision["spec"], assets=assets, asset_base="__DOCPROOF_ASSETS__",
                                base_url="__DOCPROOF_BASE__", preview=False, release_id=rid)
            if digest(pages) != stage["pages_hash"]:
                raise Conflict("The template renderer changed. Stage and approve a newly rendered revision.")
            bridge = self._bridge(project)
            prior = project.get("live_revision_id") or ""
            deployment = {"id": "publish-" + rid, "release_id": rid, "previous_release_id": prior,
                          "state": "publishing", "started_at": now(), "actor": actor}
            def starting(p):
                p["state"] = "publishing"
                p["deployments"] = [d for d in p["deployments"] if d["id"] != deployment["id"]] + [deployment]
            self.store.mutate(project_id, starting)
            try:
                receipt = bridge.activate(rid, expected_active_release_id=prior, idempotency_key=deployment["id"]).as_dict()
                health = bridge.health(expected_release_id=rid)
                if health.get("status") != "passed":
                    # Only roll back a previously verified release. Do not claim an empty site is restored.
                    recovery = None
                    if prior:
                        bridge.rollback(prior, expected_active_release_id=rid, idempotency_key="recover-" + rid)
                        recovery = bridge.health(expected_release_id=prior)
                    raise Conflict("Live verification failed. " + ("The previous release was restored." if recovery and recovery.get("status") == "passed" else
                                   "The destination requires staff attention; inspect the remote release status."))
                deployment.update(state="live", receipt=receipt, health=health, finished_at=now())
                def finished(p):
                    p.update(state="live", live_revision_id=rid, live_url=p["destination"]["url"], error="")
                    p["deployments"] = [deployment if d["id"] == deployment["id"] else d for d in p["deployments"]]
                    p["crm_sync"] = {"state": "pending"}
                    self.store.event(p, "published", actor, revision_id=rid)
                self.store.mutate(project_id, finished)
                try:
                    self.sync_crm(project_id)
                except Exception as exc:
                    self.store.mutate(project_id, lambda p: p.update(crm_sync={"state": "pending", "error": str(exc)}))
                return deployment
            except Exception as exc:
                remote = None
                try:
                    remote = bridge.status()
                except Exception:
                    pass
                deployment.update(state="publish_failed", error=str(exc), remote_status=remote)
                def failed(p):
                    p.update(state="publish_failed", error=str(exc))
                    p["deployments"] = [deployment if d["id"] == deployment["id"] else d for d in p["deployments"]]
                self.store.mutate(project_id, failed)
                raise

    def rollback(self, project_id: str, rid: str, actor: str):
        with self._publication_lock(project_id):
            project = self.store.get(project_id)
            if not any(d["release_id"] == rid and d["state"] == "live" for d in project["deployments"]):
                raise Conflict("Only a retained, previously verified live release can be restored.")
            bridge = self._bridge(project)
            old = project.get("live_revision_id") or ""
            receipt = bridge.rollback(rid, expected_active_release_id=old,
                                      idempotency_key=f"rollback-{old}-{rid}").as_dict()
            health = bridge.health(expected_release_id=rid)
            if health.get("status") != "passed":
                raise Conflict("Rollback did not pass live verification. Inspect the destination before retrying.")
            def update(p):
                p.update(live_revision_id=rid, state="live", error="")
                self.store.event(p, "rolled_back", actor, revision_id=rid)
            self.store.mutate(project_id, update)
            return {"receipt": receipt, "health": health}

    def sync_crm(self, project_id: str):
        project = self.store.get(project_id)
        cfg = SourceConfig.model_validate(project.get("source_config") or {})
        if not project["hubspot_project_ids"]:
            self.store.mutate(project_id, lambda p: p.update(crm_sync={"state": "not_configured"}))
            return
        token = get_api_key("hubspot")
        if not token or not project.get("live_url"):
            raise SourceError("HubSpot synchronization requires the configured credential and verified live URL.")
        props = {}
        if cfg.website_url_property:
            props[cfg.website_url_property] = project["live_url"]
        if cfg.website_status_property and cfg.live_value:
            props[cfg.website_status_property] = cfg.live_value
        if not props:
            raise SourceError("Configure the allowed website URL/status writeback fields.")
        for record in project["hubspot_project_ids"]:
            hubspot.set_properties(token, cfg.object_type, record, props, allow=set(props))
        self.store.mutate(project_id, lambda p: p.update(crm_sync={"state": "synced", "at": now()}))
