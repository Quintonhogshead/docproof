"""Cover Studio's HTTP API: create a job, poll it, revise a concept, download
a file. Mirrors app/routes/quest.py's shape closely — register(app),
_provider() built fresh per call from settings, upload validation in the same
register as quest's _read_upload — with one addition quest.py doesn't need:
every endpoint here is gated behind a shared key (see _gate), because unlike
the skin call (a fraction of a cent), an image generation costs real money.

Rate limiting is deliberately NOT here: app/quest_site.py's guard_cover
middleware owns it, the same way guard_skins owns quest's, keyed on request
path rather than threaded through this module.

The job store and the actual work (the direction call, painting, composing,
revising) live in docproof.cover.pipeline — this module's job is HTTP shape
only: validate the request, build a Provider and an image client, hand both
to a pipeline function as a background task, and shape the response.

Two decisions do live here, because only the HTTP layer can make them per
run, and both are settled at creation, stored on the job, and returned in
its payload.

WHICH PURSE the Claude calls spend from. Every Anthropic-model role can run
on the owner's Claude subscription (docproof.cover.subscription) instead of
on metered API credits, and the lane is chosen by the request, defaulted by
COVER_ANTHROPIC_LANE, resolved once per job — see _requested_lane/
_resolve_lane below.

HOW SHARP the art is rolled. gpt-image has no subscription lane and stays
metered whatever the purse says, so the way to spend less on it is to buy
less: a "draft" job rolls (and bills) every image at 1K instead of 2K, which
is what the owner wants while shopping concepts, sharpening only the keeper
afterwards in Cover Canvas — see _checked_image_quality below. Unlike the
lane, this is create-only: a revision inherits the job's tier and cannot
change it.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import tempfile
from pathlib import Path

import anthropic
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from docproof.config import load_config
from docproof.cover import pipeline as cover_pipeline
from docproof.cover import subscription
from docproof.cover.model import Brief
from docproof.ingest import IngestError
from docproof.providers import build_provider, lookup
from docproof.providers.base import ProviderError

from ..settings import CONFIG_PATH, get_api_key

log = logging.getLogger("docproof.app.cover")

# Guarded because direction.py/reality.py are sibling modules built alongside
# this one (see docproof.cover.pipeline's module docstring for the same
# concern) — a test that never calls _providers() must still be able to
# import this module. DIRECTION_MODEL/REVISION_MODEL/REALITY_MODEL are NOT
# mirrored here the way LUNA_MODEL once was: routes.py needs the REAL model
# ids (to look up each one's vendor via docproof.providers.lookup — see
# _build_role_provider), not a locally-agreed convention, so importing them
# is the only correct choice, not a stylistic one.
try:
    from docproof.cover.direction import REVISION_MODEL
except ImportError:                                        # pragma: no cover
    REVISION_MODEL = "claude-sonnet-5"

try:
    from docproof.cover.director import DIRECTOR_MODEL
except ImportError:                                        # pragma: no cover
    DIRECTOR_MODEL = "claude-fable-5"

try:
    from docproof.cover.imaging import make_client
except ImportError:                                        # pragma: no cover
    make_client = None

MAX_UPLOAD_BYTES = 40 * 1024 * 1024
ALLOWED_SUFFIXES = {".docx", ".txt", ".md"}


async def _read_upload(file: UploadFile) -> tuple[str, bytes]:
    """Validate an uploaded manuscript exactly like quest's own gate
    (app/routes/quest.py's _read_upload) — same suffixes, same cap, the same
    register of human-sentence errors. Raises HTTPException; the caller
    decides what to build from the bytes."""
    name = file.filename or "manuscript"
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(400, detail=(
            f"Cover Studio reads .docx, .txt, or .md files; {name} is a "
            f"{suffix or 'file with no extension'}."))
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, detail=(
            f"{name} is over the {MAX_UPLOAD_BYTES // (1024 * 1024)}MB "
            f"upload limit."))
    if not data:
        raise HTTPException(400, detail=f"{name} is empty.")
    return name, data


def _gate(request: Request) -> None:
    """Every /api/cover/* endpoint's first move (spec §9): inert (503) on a
    deployment that hasn't set COVER_KEY, and answers only a caller holding
    it (401 otherwise). hmac.compare_digest so a wrong guess can't be timed
    against the real key."""
    cover_key = os.environ.get("COVER_KEY")
    if not cover_key:
        raise HTTPException(503, detail=(
            "Cover Studio is not enabled on this deployment."))
    supplied = request.headers.get("X-Cover-Key", "")
    if not hmac.compare_digest(supplied, cover_key):
        raise HTTPException(401, detail=(
            "That key doesn't match — check X-Cover-Key and try again."))


# Exactly what new_job_id() mints — anything else 404s before it can reach
# the filesystem. The sibling `name` parameter gets the same treatment in
# cover_get_file; a job_id of ".." would otherwise resolve job_dir() to the
# store's parent.
_JOB_ID_RE = re.compile(r"^[0-9]{8}-[0-9a-f]{6}$")


def _checked_job_id(job_id: str) -> str:
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(404, detail=f"No cover job {job_id!r} here.")
    return job_id


def _data_root(request: Request) -> Path:
    """The job store's root: `app.state.cover_data_root` where the app pinned
    one (quest_site.py, the Mac shells), else the pipeline's own default.
    The fallback exists because these routes now ride every build via
    routes.register — the main app pins no cover state, and a KeyError off
    app.state would turn the gate's honest 503/401 story into a raw 500."""
    root = getattr(request.app.state, "cover_data_root", None)
    return Path(root) if root else cover_pipeline.default_root()


# -- which purse the Anthropic roles spend from -------------------------------
#
# The owner ran Cover Studio on his own machine and art direction died on
# "Your credit balance is too low" while a Max subscription sat unused. Every
# Anthropic-model role can now run on that subscription instead
# (docproof.cover.subscription), and which purse a run spends from is a
# per-run choice: the request names it, the environment names the default,
# and the resolved answer is stored on the job so a revision cannot silently
# switch purses mid-book. Image generation is untouched — gpt-image has no
# subscription lane and stays metered.

LANE_ENV = "COVER_ANTHROPIC_LANE"

# What a caller may ASK for. "auto" prefers the subscription and falls back to
# the API key; "subscription" is a pin with no fallback (a machine that cannot
# run one is a problem to fix, not to bill around); "api" is the behavior
# every deployment had before this existed.
LANES = ("auto", "subscription", "api")

_LANE_SENTENCE = (
    "anthropic_lane must be \"subscription\" (your Claude subscription), "
    "\"api\" (metered API credits), or \"auto\" (the subscription when this "
    "machine is signed in, otherwise the API key)")


def _checked_lane(value: str | None) -> str:
    """One requested lane, validated like any other body field: junk is a 422
    with the sentence, never a silent fallback to some other purse. Empty (or
    absent) means "the caller has no opinion", which the resolution order
    below answers from the job or the environment."""
    lane = (value or "").strip().lower()
    if not lane:
        return ""
    if lane not in LANES:
        raise HTTPException(422, detail=f"{lane!r} is not a lane — "
                                        f"{_LANE_SENTENCE}.")
    return lane


def _requested_lane(explicit: str = "", stored: str = "") -> str:
    """The lane this call is asking for: the request body first, then the
    lane the job was created on, then the environment, then "auto".

    The stored lane outranks the environment on purpose. A job started on the
    subscription and revised after a restart must not quietly begin spending
    API credits — that switch is exactly the surprise this whole lane exists
    to prevent, and a person who wants it says so in the body.

    A junk value in the environment is refused the same way a junk value in
    the body is, rather than quietly ignored: a deployment that misspelled
    its lane should hear about it, not spend from whichever purse the typo
    happened to fall through to."""
    return (explicit or stored
            or _checked_lane(os.environ.get(LANE_ENV)) or "auto")


def _resolve_lane(requested: str) -> str:
    """The requested lane resolved against this machine: "subscription" or
    "api", logged once for the job rather than once per model call.

    "api" is today's behavior, untouched. "subscription" is a pin: a machine
    that cannot run a subscription turn gets a readable 502 naming the fix,
    never a silent fall back onto a credit balance the owner did not choose
    to spend. "auto" tries the subscription and falls back to the API key,
    which is what a Fly or quest deployment (no CLI, no login) always
    does."""
    if requested == "api":
        log.info("Cover Studio: Anthropic roles on the API-key lane.")
        return "api"
    try:
        subscription.preflight()
    except subscription.SubscriptionUnavailable as e:
        if requested == "subscription":
            raise HTTPException(502, detail=str(e)) from e
        log.info("Cover Studio: Anthropic roles on the API-key lane — the "
                 "Claude subscription lane is not available here (%s)", e)
        return "api"
    log.info("Cover Studio: Anthropic roles on the Claude subscription lane "
             "(no API dollars).")
    return "subscription"


# -- how sharp this job's art is rolled ---------------------------------------
#
# gpt-image-2 sells the same composition at several resolutions, and the
# owner's actual working habit is to shop concepts cheap and sharpen only the
# keeper afterwards (Cover Canvas's Finalize button is the other half of that
# ladder). So a job may be a DRAFT job — every image it ever generates rolled
# at 1K and billed at 1K — or a full job, which is what every job was before
# this existed. The choice is made once, at creation, and stored on the job:
# see docproof.cover.pipeline._image_tier, the one resolver the pipeline
# reads it through.

_IMAGE_QUALITY_SENTENCE = (
    "image_quality must be \"full\" (2K art, about 5 cents an image) or "
    "\"draft\" (1K art, about 3 cents an image — sharpen the keepers "
    "afterwards in Cover Canvas)")


def _checked_image_quality(value: str | None) -> str:
    """One requested image tier, validated like any other body field: junk is
    a 422 with the sentence, never a silent fallback to whichever tier the
    typo happened to fall through to — the whole point of the knob is knowing
    what a run costs. Empty (or absent) means "no opinion", which the
    pipeline reads as the full tier, exactly as every job did before this
    field existed.

    There is no environment default to consult, unlike the Anthropic lane: a
    lane is a property of the MACHINE (is it signed in?), while the tier is a
    property of what this particular cover is FOR, which only the person
    filling in the brief knows."""
    quality = (value or "").strip().lower()
    if not quality:
        return ""
    if quality not in cover_pipeline.IMAGE_QUALITIES:
        raise HTTPException(422, detail=f"{quality!r} is not an image "
                                        f"quality — {_IMAGE_QUALITY_SENTENCE}.")
    return quality


def _is_anthropic(model: str) -> bool:
    """Whether this role's model is served by Anthropic at all. A role
    resolved to another vendor (an OpenAI or Gemini id in the catalog, or a
    model the catalog has never heard of) is untouched by the lane — there is
    no subscription behind those."""
    info = lookup(model)
    return info is not None and info.provider == "anthropic"


def _build_role_provider(model: str, *, role: str, lane: str = "api"):
    """One model role's Provider, built fresh per call — mirrors the site's
    old single-model _provider() (Quest's own quest.py:_provider() still
    works this way for its one cheap model), but resolves the vendor from
    the MODEL itself (build_provider's own `model=` override; see that
    function's docstring) rather than mutating cfg.api.model, so all three
    Cover Studio roles can share one loaded config without stepping on each
    other. `role` is a human word ("direction", "revision", "reality") used
    only in the 503 sentence below.

    On the subscription lane an Anthropic role gets a SubscriptionProvider
    instead — the same Provider protocol, answered by the Claude CLI on the
    owner's login. A non-Anthropic role ignores the lane entirely."""
    if lane == "subscription" and _is_anthropic(model):
        try:
            return subscription.SubscriptionProvider()
        except subscription.SubscriptionUnavailable as e:
            raise HTTPException(502, detail=str(e)) from e
    cfg = load_config(CONFIG_PATH)
    cfg.api.effort = "low"
    try:
        return build_provider(
            cfg, api_key=get_api_key(lookup(model).provider), model=model)
    except ProviderError as e:
        raise HTTPException(503, detail=(
            f"The {role} model is not available: {e}")) from e


def _providers(lane: str = "api") -> cover_pipeline.Providers:
    """One Provider per model role: the frontier model that reads the book
    and assigns the concepts, and the workhorse model that edits a spec from
    a person's revision notes — see docproof.cover.pipeline.Providers' own
    docstring for why two, not one. Built fresh per request.

    `lane` is the resolved purse (see _resolve_lane); it defaults to the
    API-key path so any caller that never learned about lanes behaves
    exactly as it did before."""
    return cover_pipeline.Providers(
        direction=_build_role_provider(DIRECTOR_MODEL, role="director",
                                       lane=lane),
        revision=_build_role_provider(REVISION_MODEL, role="revision",
                                      lane=lane))


def _critique_client(lane: str = "api"):
    """The raw anthropic client for the vision critique call (§6.3) —
    critique.py talks to the anthropic SDK directly rather than through the
    Provider protocol (see that module's own docstring for why), so it needs
    its own client rather than reusing one of the three Providers above.
    Keyed the same way the image client is (§7.2's precedent):
    app.settings.get_api_key, a missing key surfaced as the same
    human-sentence 503 pattern.

    On the subscription lane this is a SubscriptionAnthropicClient instead —
    the same `messages.stream(...)` surface critique.py and planner.py use,
    images included, answered on the owner's Claude login and needing no key
    at all. Both this client's models (CRITIQUE_MODEL, PLANNER_MODEL) are
    Anthropic by construction, so there is no vendor gate to apply here."""
    if lane == "subscription":
        try:
            return subscription.SubscriptionAnthropicClient()
        except subscription.SubscriptionUnavailable as e:
            raise HTTPException(502, detail=str(e)) from e
    key = get_api_key("anthropic")
    if not key:
        raise HTTPException(503, detail=(
            "Cover critique is not configured on this deployment (no "
            "Anthropic key)."))
    return anthropic.Anthropic(api_key=key)


def _image_client():
    """The gpt-image-2 client, keyed the same way the skin call is (§7.2):
    app.settings.get_api_key("openai")."""
    if make_client is None:
        raise HTTPException(503, detail=(
            "Image generation is not available on this deployment yet."))
    key = get_api_key("openai")
    if not key:
        raise HTTPException(503, detail=(
            "Image generation is not configured on this deployment (no "
            "OpenAI key)."))
    return make_client(key)


class ReviseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concept: int
    notes: str = ""
    allow_new_art: bool = False
    # Optional per-revision override of the purse (see _requested_lane).
    # Omitted — the normal case — the job's own stored lane answers, so a
    # client never has to restate it. Validated in the endpoint rather than
    # here so a junk lane reads as the same sentence on both endpoints.
    anthropic_lane: str = ""

    # There is deliberately NO image_quality here. The tier belongs to the
    # JOB and is fixed when the job is created: a revision that could switch
    # horses mid-job would leave one job's ledger quoting two prices for
    # rows that look identical, and no reader could tell which image cost
    # what. `extra="forbid"` above means a client that sends one anyway gets
    # a 422 saying so rather than having it silently ignored.


def register(app: FastAPI) -> None:

    @app.post("/api/cover/jobs", status_code=202)
    async def cover_create_job(request: Request, brief: str = Form(...),
                               manuscript: UploadFile | None = File(None),
                               anthropic_lane: str = Form(""),
                               image_quality: str = Form("")
                               ) -> dict:
        """Create a job: a Brief (JSON, in the `brief` form field) plus an
        optional manuscript for grounding. Spawns run_job in the background
        and returns immediately (§9).

        `anthropic_lane` picks the purse this job's Claude calls spend from —
        "subscription", "api", or "auto" — and is the top of the resolution
        order (body, then COVER_ANTHROPIC_LANE, then "auto"). The resolved
        answer is stored on the job, so every revision of it reuses the same
        purse and the app can show which one a run is on.

        `image_quality` picks how sharp — and how expensive — this job's art
        is rolled: "draft" (1K, ~3 cents an image) or "full" (2K, ~5 cents,
        the default and what every job did before this existed). It is
        stored on the job and fixed there: this is the only endpoint that
        accepts it, because the tier has to hold still for a job's ledger to
        mean anything."""
        _gate(request)
        try:
            brief_obj = Brief.model_validate_json(brief)
        except ValidationError as e:
            raise HTTPException(400, detail=(
                f"That brief didn't validate: {e}")) from e

        # Built before anything touches disk: a missing model/key is a 503
        # regardless of whether this particular job would ever need images,
        # the same all-or-nothing stance quest.py's _provider() takes. The
        # lane is resolved first, once, for the same reason — a pinned
        # subscription this machine cannot run is a 502 before a job exists,
        # not a half-directed job on disk.
        lane = _resolve_lane(_requested_lane(_checked_lane(anthropic_lane)))
        # Validated up here with the lane, before a job directory exists, so
        # a typo'd tier is a 422 and nothing on disk — never a job that
        # already spent a direction call before anyone noticed.
        quality = _checked_image_quality(image_quality)
        providers = _providers(lane)
        image_client = _image_client()
        critique_client = _critique_client(lane)
        root = _data_root(request)

        manuscript_name = ""
        # The director reads the WHOLE book, so the full text is carried from
        # here into run_job as an argument and dropped when the job ends. It
        # is never written to the job store: the temp file dies with this
        # block and only `manuscript_sample.txt` and the director's own
        # assignments survive on disk (§8.1's storage posture, unchanged).
        manuscript_text = ""
        with tempfile.TemporaryDirectory(prefix="cover-job-") as tmp:
            manuscript_path = None
            if manuscript is not None:
                manuscript_name, data = await _read_upload(manuscript)
                manuscript_path = Path(tmp) / Path(manuscript_name).name
                manuscript_path.write_bytes(data)
            try:
                job = cover_pipeline.create_job(
                    root, brief_obj, manuscript_path=manuscript_path,
                    manuscript_name=manuscript_name, anthropic_lane=lane,
                    image_quality=quality)
                if manuscript_path is not None:
                    manuscript_text = cover_pipeline.read_manuscript(
                        manuscript_path)
            except IngestError as e:
                raise HTTPException(400, detail=str(e)) from e

        task = asyncio.create_task(
            cover_pipeline.run_job(root, job.job_id, providers, image_client,
                                   critique_client,
                                   manuscript=manuscript_text))
        cover_pipeline.register_task(job.job_id, task)
        return {"job_id": job.job_id}

    @app.get("/api/cover/jobs/{job_id}")
    async def cover_get_job(job_id: str, request: Request) -> JSONResponse:
        """Poll target (§9): the whole JobState plus total_usd. Checks for a
        job orphaned by a restart before answering, so a stuck poll turns
        into a plain, actionable error instead of hanging forever.

        The JobState dump carries `anthropic_lane` and `image_quality`, so a
        client can show which purse this run's Claude calls are spending from
        and whether its art was rolled at draft quality."""
        _gate(request)
        job_id = _checked_job_id(job_id)
        root = _data_root(request)
        job = cover_pipeline.load_job(root, job_id)
        if job is None:
            raise HTTPException(404, detail=f"No cover job {job_id!r} here.")
        job = cover_pipeline.check_interrupted(root, job)
        payload = job.model_dump(mode="json")
        payload["total_usd"] = cover_pipeline.total_usd(job)
        return JSONResponse(payload, headers={"Cache-Control": "no-cache"})

    @app.post("/api/cover/jobs/{job_id}/revise", status_code=202)
    async def cover_revise(job_id: str, request: Request, body: ReviseBody
                           ) -> dict:
        """Revise one concept: notes, and whether to allow new art. 409
        unless that concept is ready/error — a concept mid-paint has nothing
        stable to revise yet (§9).

        The purse is the job's own stored lane unless this body names one
        (which applies to this revision only — the job's lane is set when the
        job is created, and a one-off override is not a decision about the
        book)."""
        _gate(request)
        job_id = _checked_job_id(job_id)
        root = _data_root(request)
        job = cover_pipeline.load_job(root, job_id)
        if job is None:
            raise HTTPException(404, detail=f"No cover job {job_id!r} here.")
        if not (0 <= body.concept < len(job.concepts)):
            raise HTTPException(404, detail=(
                f"Concept {body.concept} does not exist on this job."))
        if job.concepts[body.concept].status not in ("ready", "error"):
            raise HTTPException(409, detail=(
                "This concept is still being worked on — wait for it to "
                "finish before revising it."))

        lane = _resolve_lane(_requested_lane(
            _checked_lane(body.anthropic_lane), job.anthropic_lane))
        providers = _providers(lane)
        image_client = _image_client()
        # critique_client doubles as the §15.16 replan client: run_revision
        # only ever uses it when allow_new_art is set AND the notes ask for a
        # "replan". Best-effort on purpose — a deployment with no Anthropic
        # key could always revise (the human is the critic here, §6.3), and
        # threading the replan client must not change that: no key → None →
        # replan quietly degrades to the spontaneous path in the pipeline.
        try:
            replan_client = _critique_client(lane)
        except HTTPException:
            replan_client = None
        task = asyncio.create_task(cover_pipeline.run_revision(
            root, job_id, body.concept, body.notes, body.allow_new_art,
            providers, image_client,
            critique_client=replan_client))
        cover_pipeline.register_task(job_id, task)
        return {"job_id": job_id, "concept": body.concept}

    @app.get("/api/cover/jobs/{job_id}/file/{name}")
    async def cover_get_file(job_id: str, name: str, request: Request,
                             concept: int | None = None) -> Response:
        """Serve one render (renders/<name>), or — with ?concept=n — that
        concept's spec as a JSON download. Refuses any name that could climb
        out of the job directory (§9)."""
        _gate(request)
        job_id = _checked_job_id(job_id)
        root = _data_root(request)
        job = cover_pipeline.load_job(root, job_id)
        if job is None:
            raise HTTPException(404, detail=f"No cover job {job_id!r} here.")

        if concept is not None:
            if not (0 <= concept < len(job.concepts)):
                raise HTTPException(404, detail=(
                    f"Concept {concept} does not exist on this job."))
            payload = job.concepts[concept].spec.model_dump_json(indent=2)
            return Response(
                payload, media_type="application/json",
                headers={"Content-Disposition": (
                    f'attachment; filename="{job_id}_c{concept}_spec.json"')})

        if "/" in name or ".." in name:
            raise HTTPException(400, detail="That file name isn't a real render.")
        renders_root = (cover_pipeline.job_dir(root, job_id)
                        / cover_pipeline.RENDERS_DIR).resolve()
        target = (renders_root / name).resolve()
        if target != renders_root and renders_root not in target.parents:
            raise HTTPException(400, detail="That file name isn't a real render.")
        if not target.is_file():
            raise HTTPException(404, detail=f"No file named {name!r} on this job.")
        media_type = "image/jpeg" if target.suffix == ".jpg" else "image/png"
        return FileResponse(target, media_type=media_type,
                            headers={"Cache-Control": "no-cache"})

    @app.get("/api/cover/jobs")
    async def cover_list_jobs(request: Request) -> dict:
        """The last 20 jobs — the "pick up where I left off" list (§9)."""
        _gate(request)
        root = _data_root(request)
        jobs = cover_pipeline.list_jobs(root, limit=20)
        return {"jobs": [{"job_id": j.job_id, "title": j.brief.title,
                          "status": j.status, "created": j.created,
                          "anthropic_lane": j.anthropic_lane,
                          "image_quality": j.image_quality,
                          "total_usd": cover_pipeline.total_usd(j)}
                         for j in jobs]}
