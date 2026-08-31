"""Cover Canvas's HTTP API: open a cover job as layers, edit it, regenerate a
plate, ask the assistant, export the result.

Shaped exactly like app/routes/cover.py — register(app), the same X-Cover-Key
gate, the same job store, the same "the route is HTTP shape only" rule — and
for the same reason: a canvas session is a cover job's second act (spec §3),
lives in that job's own directory, and spends money through the same image
key. Reusing cover.py's `_gate`, `_checked_job_id` and `_image_client` rather
than restating them is deliberate: a second copy of the key plumbing is a
second thing to forget to lock.

Two decisions this module owns:

- **canvas.json, never job.json.** The editing document persists as
  `canvas.json` INSIDE the cover job directory. job.json stays the
  pipeline's manifest, untouched — the canvas must be able to open a job
  archived months ago, and it must never be able to corrupt the record of
  the cover that was actually generated. Canvas spend is likewise the
  canvas's own: `CanvasDoc.cost_usd` counts what the editor spent, and the
  job's ledger keeps counting what the pipeline spent.
- **The fonts are ungated.** Every other endpoint here is behind the cover
  key; `/api/canvas/fonts.css` and `/api/canvas/font/{file}` cannot be,
  because a browser fetches a stylesheet and a webfont with no way to carry
  an `X-Cover-Key` header. They serve nothing but the vendored OFL faces
  that already ship in the wheel — the closed shelf in
  docproof.cover.fonts, by exact filename — so there is nothing behind them
  to protect.

Everything the endpoints actually DO lives in docproof.canvas: `ingest`
converts a job, `ops` is the only way a document ever changes, `regen` owns
the plate verbs — re-roll, inpaint, finalize and ground-the-figure, which
spend money, plus rebalance, which does not — `wrap` turns a front cover
into a full print wrap and owns the panel geometry, and `assistant`
(imported lazily, because it needs an Agent SDK that may not be installed)
owns the AI box.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.routing import Mount

from docproof.canvas import ops as canvas_ops
from docproof.canvas import regen
from docproof.canvas.ingest import CanvasIngestError, ingest
from docproof.canvas.model import CanvasDoc, Wrap, load_doc, save_doc
from docproof.canvas.wrap import WrapError, panels, to_wrap
from docproof.cover import pipeline as cover_pipeline
from docproof.cover.fonts import FAMILIES, font_path

# Guarded exactly the way cover.py guards `make_client`: imaging.py imports
# the OpenAI SDK at module scope, and a test (or a slimmed deployment) that
# never generates an image must still be able to import this module.
try:
    from docproof.cover.imaging import ImagingError
except ImportError:                                        # pragma: no cover
    class ImagingError(RuntimeError):
        """Placeholder when docproof.cover.imaging cannot be imported."""

# The module, not its functions: cover.py's `_gate`/`_image_client` are the
# seam route tests monkeypatch (tests/test_cover_routes.py does exactly
# that), and reaching them through the module object means one patch covers
# both APIs instead of leaving a stale copy bound here.
from . import cover

# The editing document, inside the cover job directory it was ingested from.
CANVAS_FILE = "canvas.json"

# What the export writes. One fixed name, overwritten on every export: the
# canvas IS the document, so an export is a snapshot of it, not a version —
# versions live in canvas.json's own history.
EXPORT_NAME = f"{cover_pipeline.RENDERS_DIR}/canvas_export.png"

# The same snapshot as a print-ready page. A separate name rather than a
# separate directory: one export, two containers, and a person looking in
# renders/ can see at a glance which they have.
EXPORT_PDF_NAME = f"{cover_pipeline.RENDERS_DIR}/canvas_export.pdf"

# What a FRONT-ONLY document's pixels are worth in inches when it is
# exported as a PDF. A front-only canvas states no physical size at all —
# it is the plate resolution the cover job was generated at (Size's own
# docstring) — so the export has to assume one, and 300dpi is the assumption
# the whole product already makes: it is print resolution, it is what §7
# sizes the client composite against, and at it the composer's ebook canvas
# is very nearly a 6x9 front cover plus bleed. A wrap document needs none of
# this guesswork; it says its size in inches.
PRINT_DPI = 300

# What /file/ will serve out of a job directory. Plates and renders only —
# job.json, canvas.json and the manuscript sample all live in the same
# directory and none of them is one. The PDF is here because the print wrap
# is written by the SERVER (the browser composites pixels, not pages), so
# without a way to fetch it back the deliverable would only exist on the
# machine that made it — true on the Mac shell, useless on a hosted one.
_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg",
                ".webp": "image/webp", ".pdf": "application/pdf"}

# Every TTF the shelf declares, filename -> path on disk. Built once at
# import: /api/canvas/font/{file} serves a file ONLY if it is a key here, so
# the endpoint can never be talked into reading anything the closed shelf
# does not name.
def _font_files() -> dict[str, Path]:
    files: dict[str, Path] = {}
    for family, font in FAMILIES.items():
        for style, name in (("regular", font.file), ("italic", font.italic_file),
                            ("bold", font.bold_file)):
            if name:
                files[name] = font_path(family, style)
    return files


FONT_FILES: dict[str, Path] = _font_files()


# -- the job store ------------------------------------------------------------

def _data_root(request: Request) -> Path:
    """The cover job store's root.

    `app.state.cover_data_root` is where app/quest_site.py stashes it and is
    what cover.py reads, so the canvas answers out of exactly the same store
    on that site. The fallback is for the builds that register these routes
    without that state — app/main.py's own app, and the Mac shell built on
    it (app/canvas_desktop.py) — where cover_pipeline.default_root() is the
    same answer quest_site itself computed."""
    root = getattr(request.app.state, "cover_data_root", None)
    return Path(root) if root else cover_pipeline.default_root()


def _job_dir(request: Request, job_id: str) -> tuple[Path, str]:
    """The checked job id and its directory. The id is validated against
    cover.py's own mint-shape regex before it can reach the filesystem —
    same 404, same sentence."""
    job_id = cover._checked_job_id(job_id)
    return cover_pipeline.job_dir(_data_root(request), job_id), job_id


def _canvas_path(job_dir: Path) -> Path:
    return job_dir / CANVAS_FILE


def _load(job_dir: Path, job_id: str) -> CanvasDoc:
    """This job's canvas document, or the right refusal.

    404 when there is no session yet (the client's cue to POST /open), and
    409 when there is one that cannot be read — a corrupt or future-version
    canvas.json is a real state a person has to be told about, not a 500
    that reads like the server broke."""
    path = _canvas_path(job_dir)
    if not path.is_file():
        raise HTTPException(404, detail=(
            f"No canvas session for job {job_id!r} yet — open the cover job "
            f"first."))
    try:
        return load_doc(path)
    except (OSError, ValueError, ValidationError) as e:
        raise HTTPException(409, detail=(
            f"The canvas session for job {job_id!r} could not be read: "
            f"{e}")) from e


def _save(job_dir: Path, doc: CanvasDoc) -> dict[str, Any]:
    """Persist and shape the standard answer. Every mutating endpoint ends
    with this exact pair, so the client always gets the whole document back
    and never has to guess what the server thinks the truth is."""
    save_doc(doc, _canvas_path(job_dir))
    return {"doc": doc.model_dump(mode="json")}


def _default_concept(job_dir: Path) -> int:
    """Which concept a canvas opens on when the caller names none.

    A cover job holds several concepts (§8 of the designer spec) and only a
    finished one has plates to edit, so the first `ready` concept is the
    honest default — it is the one the job page shows first as openable. An
    explicit choice in the manifest wins if a later build ever records one
    (JobState has no such field today; read defensively from the raw JSON so
    adding it needs no change here), and 0 is the last resort so the
    CanvasIngestError from a job with nothing ready still names concept 0
    rather than a number nobody asked for."""
    try:
        raw = json.loads(
            (job_dir / cover_pipeline.JOB_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(raw, dict):
        return 0
    for key in ("chosen_concept", "approved_concept", "chosen", "approved"):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    concepts = raw.get("concepts")
    if isinstance(concepts, list):
        for i, entry in enumerate(concepts):
            if isinstance(entry, dict) and entry.get("status") == "ready":
                return i
    return 0


# -- wire helpers -------------------------------------------------------------

def _first_error(e: ValidationError) -> str:
    """One pydantic error as a sentence — docproof.canvas.ops._first_error's
    rule, restated on this side of the wire: the full ValidationError repr is
    a multi-line report with a documentation URL in it, and a browser toast
    wants the one line saying what was wrong."""
    errors = e.errors()
    if not errors:                                          # pragma: no cover
        return str(e)
    first = errors[0]
    where = ".".join(str(part) for part in first["loc"]) or "the document"
    return f"{where}: {first['msg']}"


def _decode_b64(value: str, what: str) -> bytes:
    """Base64 from the wire, with or without a `data:image/png;base64,`
    prefix. The client's own canvas exports are split off their data URL
    before they are sent, but an assistant, a curl, or a future client is
    just as likely to send the whole URL, and refusing that would be a
    riddle rather than an API."""
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(400, detail=f"That request carried no {what}.")
    payload = value.strip()
    if payload.startswith("data:"):
        _, _, payload = payload.partition(",")
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(400, detail=(
            f"That {what} was not readable base64 ({e}).")) from e


def page_inches(doc: CanvasDoc) -> tuple[float, float]:
    """The physical size this document's export should print at.

    A wrap says it outright — that is what a Wrap IS, and the number the
    printer will check. A front-only document says nothing physical at all,
    so its pixels are read at PRINT_DPI (see that constant for the
    assumption and why it is the honest one)."""
    if doc.wrap is not None:
        return doc.wrap.sheet_in
    return doc.canvas.w / PRINT_DPI, doc.canvas.h / PRINT_DPI


def pdf_bytes(png: bytes, w_in: float, h_in: float) -> bytes:
    """One PNG as a one-page PDF exactly `w_in` x `h_in` inches.

    The page size is the whole point: a print wrap that lands at 98% scale
    is a wrap whose spine misses the fold, and nobody finds out until the
    proof arrives. Pillow sizes a PDF page as `pixels / resolution`, so the
    resolution is DERIVED from the composite it was actually handed rather
    than assumed to be the document's dpi — a client that exported at half
    resolution still gets a page of the right physical size (just softer),
    which is the failure everyone would rather have. Per-axis, via the `dpi`
    tuple, so an off-by-one in the client's rounding cannot stretch the
    page.

    Alpha is flattened onto white because a PDF page has no transparency and
    paper has no alpha channel: the white IS the paper."""
    try:
        with Image.open(io.BytesIO(png)) as image:
            image.load()
            if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
                rgba = image.convert("RGBA")
                page = Image.new("RGB", rgba.size, (255, 255, 255))
                page.paste(rgba, mask=rgba.getchannel("A"))
            else:
                page = image.convert("RGB")
    except (OSError, UnidentifiedImageError, ValueError) as e:
        raise HTTPException(400, detail=(
            f"That export could not be read as an image ({e}).")) from e
    buffer = io.BytesIO()
    page.save(buffer, format="PDF",
              dpi=(page.width / w_in, page.height / h_in))
    return buffer.getvalue()


def _spa_url(app: FastAPI, query: str) -> str:
    """Where app/static/canvas/index.html actually lives on THIS app.

    The SPA loads its stylesheet, its vendored Konva and its modules by
    RELATIVE path, so it has to be served from its own directory rather than
    proxied at /canvas — hence a redirect. The mount point differs by build
    (app/main.py mounts the static tree at "/", app/quest_site.py at
    "/assets"), so it is discovered rather than assumed; a build with no
    static mount at all gets the app/main.py answer."""
    for route in app.routes:
        if isinstance(route, Mount) and isinstance(route.app, StaticFiles):
            for directory in route.app.all_directories:
                if (Path(directory) / "canvas" / "index.html").is_file():
                    prefix = route.path.rstrip("/")
                    return f"{prefix}/canvas/index.html{query}"
    return f"/canvas/index.html{query}"


# -- request bodies -----------------------------------------------------------
# extra="forbid" throughout, the discipline docproof.canvas.model and
# docproof.canvas.ops both keep: a stray or misspelled field from a browser
# or a language model fails with a sentence rather than being silently
# dropped and wondered about later.

class OpenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    concept: int | None = None


class DocBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Validated by hand against CanvasDoc in the handler, not declared as one
    # here, so a bad document answers 422 with ONE sentence naming the field
    # instead of FastAPI's nested union report.
    doc: dict[str, Any]


class OpsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ops: list[dict[str, Any]]


# The quality ladder's three rungs, closed at the door the way ChatBody's
# mode is: a fourth value is a client bug, and 422 with the three names in
# it beats a 502 out of regen.py after the request already reached the money
# layer. Declared once because reroll and inpaint take the same ladder.
Quality = Literal["draft", "final", "session"]


class RerollBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_id: str
    prompt: str | None = None
    quality: Quality = "session"


class InpaintBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_id: str
    instruction: str
    mask_b64: str
    quality: Quality = "session"


class FinalizeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_id: str
    prompt: str | None = None


class GroundBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_id: str
    instruction: str | None = None


class RebalanceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_id: str


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, Any]]
    # The two modes of §6, closed here rather than left to the assistant:
    # a third value is a client bug, and 422 at the door beats a 500 out of
    # a module that (correctly) refuses to guess what a person meant.
    mode: Literal["plan", "act"] = "act"
    snapshot_b64: str | None = None
    model: str | None = None


class WrapBody(BaseModel):
    """The four numbers off the printer's calculator, plus the resolution.

    Declared as plain floats and validated by building a Wrap in the
    handler, so the "everything is positive, dpi is 72-600" rule lives in
    exactly one place (docproof.canvas.model.Wrap) instead of being restated
    here where it could drift."""
    model_config = ConfigDict(extra="forbid")

    trim_w_in: float
    trim_h_in: float
    spine_in: float
    bleed_in: float = 0.125
    dpi: int = 300


class ExportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    png_b64: str
    # The container, not the content: the client composites once and the
    # server either keeps those pixels as they are or wraps them in a page
    # of the right physical size. Closed at the door like every other
    # Literal here — a third value is a client bug.
    format: Literal["png", "pdf"] = "png"


# The 501 the AI box gets when this machine cannot run it. A sentence, not a
# status code: the front-end puts it straight in the transcript.
_NO_ASSISTANT = (
    "The AI box is not available on this machine — it needs the "
    "claude-agent-sdk installed and a logged-in Claude Code to borrow its "
    "session from.")


def register(app: FastAPI) -> None:

    # Registered before /{job_id}: the font endpoints are fixed paths, and a
    # path parameter declared first would swallow "fonts.css" (it would 404
    # on the job-id shape check, which is a confusing way to fail to load a
    # stylesheet).

    @app.get("/api/canvas/fonts.css")
    async def canvas_fonts_css() -> Response:
        """The whole shelf as @font-face rules, one per file the registry
        declares (regular, plus italic and bold where a family ships them).

        Ungated on purpose — see the module docstring: a <link rel=
        stylesheet> cannot carry the cover key, and these are vendored OFL
        faces, not secrets. `font-family` is the FAMILIES key EXACTLY, spaces
        and all, because that string is what a TextLayer stores and what the
        client asks the browser for; anything else renders the cover in a
        fallback face and calls it a day."""
        rules: list[str] = []
        for family, font in FAMILIES.items():
            for name, weight, style in (
                    (font.file, 400, "normal"),
                    (font.bold_file, 700, "normal"),
                    (font.italic_file, 400, "italic")):
                if not name or not FONT_FILES[name].is_file():
                    continue
                rules.append(
                    f'@font-face {{\n'
                    f'  font-family: "{family}";\n'
                    f'  src: url("/api/canvas/font/{name}") format("truetype");\n'
                    f'  font-weight: {weight};\n'
                    f'  font-style: {style};\n'
                    f'  font-display: block;\n'
                    f'}}')
        return Response("\n".join(rules) + "\n", media_type="text/css",
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/canvas/font/{file}")
    async def canvas_font(file: str) -> FileResponse:
        """One TTF off the shelf, by exact filename.

        The allowlist IS the registry (FONT_FILES): a name the shelf does not
        declare 404s before anything touches the filesystem, so this endpoint
        cannot be walked anywhere. Ungated for the same reason fonts.css
        is."""
        path = FONT_FILES.get(file)
        if path is None or not path.is_file():
            raise HTTPException(404, detail=f"No font file named {file!r}.")
        return FileResponse(path, media_type="font/ttf",
                            headers={"Cache-Control": "public, max-age=86400"})

    @app.post("/api/canvas/open")
    async def canvas_open(request: Request, body: OpenBody) -> dict:
        """Open a cover job on the canvas.

        Existing edits always win: if this job already has a canvas.json it
        comes back as it is, and `concept` is ignored rather than silently
        re-ingesting over an afternoon's work. Otherwise the cover job is
        converted (docproof.canvas.ingest) and saved as the session's first
        state."""
        cover._gate(request)
        job_dir, job_id = _job_dir(request, body.job_id)
        if _canvas_path(job_dir).is_file():
            return {"doc": _load(job_dir, job_id).model_dump(mode="json")}
        if not (job_dir / cover_pipeline.JOB_MANIFEST).is_file():
            raise HTTPException(404, detail=f"No cover job {job_id!r} here.")

        concept = (body.concept if body.concept is not None
                   else _default_concept(job_dir))
        try:
            doc = ingest(job_dir, concept=concept)
        except CanvasIngestError as e:
            # 409, not 404: the job is here, it just is not finished enough
            # to edit (no spec, no plate on disk, no such concept). The
            # ingest sentence already names the file, so it passes through.
            raise HTTPException(409, detail=str(e)) from e
        return _save(job_dir, doc)

    @app.get("/api/canvas/{job_id}")
    async def canvas_get(job_id: str, request: Request) -> dict:
        """This job's canvas document. 404 when there is no session yet —
        which is the client's cue to POST /api/canvas/open."""
        cover._gate(request)
        job_dir, job_id = _job_dir(request, job_id)
        return {"doc": _load(job_dir, job_id).model_dump(mode="json")}

    @app.put("/api/canvas/{job_id}")
    async def canvas_put(job_id: str, request: Request, body: DocBody) -> dict:
        """Replace the whole document — the client's own save.

        Everything goes through CanvasDoc, so a document that would not load
        can never be written: a bad field is 422 with the field named, and a
        document belonging to a different job is 409 rather than being
        filed under this one."""
        cover._gate(request)
        job_dir, job_id = _job_dir(request, job_id)
        try:
            doc = CanvasDoc.model_validate(body.doc)
        except ValidationError as e:
            raise HTTPException(422, detail=(
                f"That canvas document does not validate: "
                f"{_first_error(e)}")) from e
        if doc.job_id != job_id:
            raise HTTPException(409, detail=(
                f"That document belongs to job {doc.job_id!r}, not "
                f"{job_id!r}."))
        return _save(job_dir, doc)

    @app.post("/api/canvas/{job_id}/ops")
    async def canvas_ops_apply(job_id: str, request: Request, body: OpsBody
                               ) -> dict:
        """Apply a batch of ops — the one mutation path the UI, the button
        shelf and the assistant all share (§4).

        All-or-nothing (docproof.canvas.ops.apply_many): a batch whose
        fourth op is refused leaves the document exactly as it was, and the
        409 names which op it was."""
        cover._gate(request)
        job_dir, job_id = _job_dir(request, job_id)
        doc = _load(job_dir, job_id)
        try:
            canvas_ops.apply_many(doc, body.ops)
        except canvas_ops.OpError as e:
            raise HTTPException(409, detail=str(e)) from e
        return _save(job_dir, doc)

    @app.post("/api/canvas/{job_id}/wrap")
    async def canvas_wrap(job_id: str, request: Request, body: WrapBody
                          ) -> dict:
        """Turn this front cover into a full paperback wrap (spec §7's v2
        line, designer spec §12).

        One-way and once: the front cover's layers are remapped into the
        FRONT panel of a back+spine+front sheet and the new panels are
        seeded around them (docproof.canvas.wrap.to_wrap). A job that is
        already wrapped answers 409 with the sentence saying so — the fix is
        the `set_wrap` op, which re-measures the spine without touching the
        design.

        Answers with the whole document AND its panel geometry, because the
        client needs both in the same breath: the document to draw, and the
        fold lines and safe margins to draw ON it. Both come from
        docproof.canvas.wrap, so the guides the person sees are the
        geometry the conversion used."""
        cover._gate(request)
        job_dir, job_id = _job_dir(request, job_id)
        doc = _load(job_dir, job_id)
        try:
            spec = Wrap(**body.model_dump())
        except ValidationError as e:
            raise HTTPException(422, detail=(
                f"That wrap does not describe a book: "
                f"{_first_error(e)}")) from e
        try:
            wrapped = to_wrap(doc, spec, job_dir=job_dir)
        except WrapError as e:
            raise HTTPException(409, detail=str(e)) from e
        payload = _save(job_dir, wrapped)
        payload["panels"] = panels(spec)
        return payload

    @app.post("/api/canvas/{job_id}/reroll")
    async def canvas_reroll(job_id: str, request: Request, body: RerollBody
                            ) -> dict:
        """Roll an art layer again (§5). `prompt` present is
        tweak-then-roll; absent is the one-click re-roll.

        `quality` is the ladder rung this click pays for (§8): "draft" while
        the composition is still moving, "final" when it is not, "session"
        (the default) for whatever tier this machine is set to.

        Answers with the whole document AND this one call's price, so the
        toast can say what the click cost while `doc.cost_usd` carries the
        session total (§8)."""
        cover._gate(request)
        job_dir, job_id = _job_dir(request, job_id)
        doc = _load(job_dir, job_id)
        # In the $0 stand-in lane no vendor client exists to build — and
        # building one is exactly what fails on a machine with no image key.
        client = None if regen.fake_active() else cover._image_client()
        try:
            # to_thread because the OpenAI call is a blocking SDK call and
            # this is an async handler — the same reason
            # cover_pipeline._generate_art_slot wraps generate().
            cost = await asyncio.to_thread(
                regen.reroll, job_dir, doc, body.layer_id, client=client,
                prompt=body.prompt, quality=body.quality)
        except (regen.RegenError, ImagingError) as e:
            raise HTTPException(502, detail=str(e)) from e
        payload = _save(job_dir, doc)
        payload["cost_usd"] = cost
        return payload

    @app.post("/api/canvas/{job_id}/inpaint")
    async def canvas_inpaint(job_id: str, request: Request, body: InpaintBody
                             ) -> dict:
        """Repair one drawn region of an art layer's plate (§5).

        `mask_b64` is the client's rasterized region in imaging.edit's own
        convention — transparent where it should regenerate — passed through
        untouched. `quality` is the same ladder rung reroll takes."""
        cover._gate(request)
        job_dir, job_id = _job_dir(request, job_id)
        doc = _load(job_dir, job_id)
        mask_png = _decode_b64(body.mask_b64, "mask")
        client = None if regen.fake_active() else cover._image_client()
        try:
            cost = await asyncio.to_thread(
                regen.inpaint, job_dir, doc, body.layer_id, client=client,
                instruction=body.instruction, mask_png=mask_png,
                quality=body.quality)
        except (regen.RegenError, ImagingError) as e:
            raise HTTPException(502, detail=str(e)) from e
        payload = _save(job_dir, doc)
        payload["cost_usd"] = cost
        return payload

    @app.post("/api/canvas/{job_id}/finalize")
    async def canvas_finalize(job_id: str, request: Request, body: FinalizeBody
                              ) -> dict:
        """Re-render a kept plate at full quality (§5's quality ladder).

        The other end of the draft lane: roll cheap while composing, then
        spend once on the plate you keep. The plate itself anchors the
        re-render (docproof.cover.imaging.refine), so the composition the
        type was arranged against survives — which a fresh generate() at a
        higher tier would not. `prompt` is optional emphasis appended to
        the fixed re-render instruction, never a replacement for it.

        Same gate, same $0-lane client rule and same 502 as reroll."""
        cover._gate(request)
        job_dir, job_id = _job_dir(request, job_id)
        doc = _load(job_dir, job_id)
        client = None if regen.fake_active() else cover._image_client()
        try:
            cost = await asyncio.to_thread(
                regen.finalize, job_dir, doc, body.layer_id, client=client,
                prompt=body.prompt)
        except (regen.RegenError, ImagingError) as e:
            raise HTTPException(502, detail=str(e)) from e
        payload = _save(job_dir, doc)
        payload["cost_usd"] = cost
        return payload

    @app.post("/api/canvas/{job_id}/ground")
    async def canvas_ground(job_id: str, request: Request, body: GroundBody
                            ) -> dict:
        """Ground the figure on an art layer (§5's shelf, designer spec
        §15.23's cardinal rule).

        Unlike inpaint this carries NO mask: the band is the recipe, so the
        server draws it (regen._ground_mask) across the bottom of the
        plate's own pixels. `instruction` is optional scene specifics
        appended to the fixed ground instruction.

        Same gate, same $0-lane client rule and same 502 as reroll."""
        cover._gate(request)
        job_dir, job_id = _job_dir(request, job_id)
        doc = _load(job_dir, job_id)
        client = None if regen.fake_active() else cover._image_client()
        try:
            cost = await asyncio.to_thread(
                regen.ground_figure, job_dir, doc, body.layer_id,
                client=client, instruction=body.instruction)
        except (regen.RegenError, ImagingError) as e:
            raise HTTPException(502, detail=str(e)) from e
        payload = _save(job_dir, doc)
        payload["cost_usd"] = cost
        return payload

    @app.post("/api/canvas/{job_id}/rebalance")
    async def canvas_rebalance(job_id: str, request: Request,
                               body: RebalanceBody) -> dict:
        """Measure an art layer's plate and nudge its levels (§5's shelf).

        The one AI verb that spends nothing: docproof.cover.balance measures
        the plate, a bounded correction lands as a `levels` effect through
        the ops layer (so it undoes like any other edit), and `measured`
        comes back as one sentence for the AI box.

        Two departures from the plate verbs above, both because there is no
        vendor here: no image client is built at all, and a refusal is 409
        rather than 502 — nothing upstream was asked anything, so "bad
        gateway" would name a gateway that was never involved. 409 is what
        /ops already answers when the document cannot take the change.
        Synchronous, not to_thread: this is Pillow arithmetic on one plate,
        not a network call."""
        cover._gate(request)
        job_dir, job_id = _job_dir(request, job_id)
        doc = _load(job_dir, job_id)
        try:
            measured = regen.rebalance(job_dir, doc, body.layer_id)
        except (regen.RegenError, canvas_ops.OpError) as e:
            raise HTTPException(409, detail=str(e)) from e
        payload = _save(job_dir, doc)
        payload["measured"] = measured
        return payload

    @app.post("/api/canvas/{job_id}/chat")
    async def canvas_chat(job_id: str, request: Request, body: ChatBody
                          ) -> dict:
        """One turn of the AI box (§6): Plan critiques, Act edits.

        docproof.canvas.assistant is imported HERE, per request, not at
        module scope: it needs the claude-agent-sdk and a logged-in Claude
        Code to borrow a session from, neither of which a server deployment
        necessarily has — and the rest of this API must keep working on a
        machine that has neither. A missing SDK is 501 with a sentence
        saying what is missing, which the front-end shows in the transcript.

        The assistant is handed a zero-arg `image_client` factory rather than
        a client, so its re-roll and inpaint tools key themselves exactly the
        way this module's own endpoints do (and a turn that never touches a
        plate never builds one, so a missing OpenAI key does not break Plan
        mode)."""
        cover._gate(request)
        job_dir, job_id = _job_dir(request, job_id)
        doc = _load(job_dir, job_id)
        try:
            from docproof.canvas import assistant
        except ImportError as e:
            raise HTTPException(501, detail=_NO_ASSISTANT) from e
        # The module may be importable and still refuse to run (no SDK, no
        # login). Fetched defensively so this handler works against an
        # assistant build that has not declared the exception yet.
        unavailable = getattr(assistant, "AssistantUnavailable", ())
        snapshot = (_decode_b64(body.snapshot_b64, "snapshot")
                    if body.snapshot_b64 else None)
        try:
            result = await assistant.chat(
                job_dir, doc, body.messages, body.mode,
                snapshot_png=snapshot, model=body.model,
                image_client=lambda: cover._image_client())
        except unavailable as e:
            raise HTTPException(501, detail=str(e) or _NO_ASSISTANT) from e
        payload = _save(job_dir, result.doc)
        payload["reply"] = result.reply
        payload["ops_applied"] = result.ops_applied
        payload["cost_usd"] = result.cost_usd
        return payload

    @app.get("/api/canvas/{job_id}/file/{name:path}")
    async def canvas_file(job_id: str, name: str, request: Request
                          ) -> FileResponse:
        """Serve one plate or render out of the job directory.

        `name` is a path, not a filename, because a plate reference IS a
        relative path ("assets/c0_focal.png") — so the traversal defense is
        a strict resolve inside the job directory rather than cover.py's
        no-slashes rule, plus an image-suffix allowlist so job.json and
        canvas.json are not reachable through it. Everything that fails is
        404: a probe should not learn the difference between "outside the
        job" and "not there"."""
        cover._gate(request)
        job_dir, job_id = _job_dir(request, job_id)
        suffix = Path(name).suffix.lower()
        media_type = _IMAGE_TYPES.get(suffix)
        if media_type is None:
            raise HTTPException(404, detail=(
                f"No file named {name!r} on this job."))
        root = job_dir.resolve()
        target = (root / name).resolve()
        if root not in target.parents or not target.is_file():
            raise HTTPException(404, detail=(
                f"No file named {name!r} on this job."))
        return FileResponse(target, media_type=media_type,
                            headers={"Cache-Control": "no-cache"})

    @app.post("/api/canvas/{job_id}/export")
    async def canvas_export(job_id: str, request: Request, body: ExportBody
                            ) -> dict:
        """Keep the client's full-resolution composite with the job (§7).

        The browser has already downloaded its own copy by the time this is
        called; this is the copy that stays with the cover, beside the
        pipeline's own renders, so a finished cover and the job that made it
        are one folder.

        `format: "pdf"` is the print deliverable: the same pixels in a page
        of exactly the right physical size (see pdf_bytes and page_inches).
        It is the one branch that needs the DOCUMENT — the pixels alone
        cannot say how many inches across they are — which is why a PDF
        export of a job with no canvas session 404s where a PNG of the same
        job would be filed happily."""
        cover._gate(request)
        job_dir, job_id = _job_dir(request, job_id)
        if not job_dir.is_dir():
            raise HTTPException(404, detail=f"No cover job {job_id!r} here.")
        png_bytes = _decode_b64(body.png_b64, "export")
        name = EXPORT_NAME if body.format == "png" else EXPORT_PDF_NAME
        if body.format == "pdf":
            w_in, h_in = page_inches(_load(job_dir, job_id))
            payload = pdf_bytes(png_bytes, w_in, h_in)
        else:
            payload = png_bytes
        target = job_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return {"name": name}

    @app.get("/api/canvas/{job_id}/render")
    async def canvas_render(job_id: str, request: Request, w: int = 0
                            ) -> Response:
        """The document as the SERVER sees it: docproof.canvas.render's
        parity composite, as PNG.

        The browser's own canvas is the render of record for interactive
        work; this one exists for everything with no browser in it — a
        headless check that a doc still composes, a measurement pass that
        needs pixels the client never uploaded, a future automation lane.
        `w` scales the output (0 = the doc's own canvas width). Imported
        lazily like the assistant: the renderer needs the cover extra's
        Pillow stack, and a deployment without it still serves every other
        canvas route."""
        cover._gate(request)
        job_dir, job_id = _job_dir(request, job_id)
        doc = _load(job_dir, job_id)
        from docproof.canvas import render as canvas_render_mod
        try:
            image = await asyncio.to_thread(
                canvas_render_mod.render, doc, job_dir,
                width=w if w > 0 else None)
        except canvas_render_mod.RenderError as e:
            raise HTTPException(409, detail=str(e)) from e
        import io as _io
        buf = _io.BytesIO()
        image.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")

    @app.get("/canvas")
    async def canvas_page(request: Request) -> RedirectResponse:
        """The editor's front door, `/canvas?job=<id>`.

        A redirect rather than a FileResponse: the SPA resolves style.css,
        vendor/konva.min.js and its ES modules relative to its own URL, so it
        has to be served from its own directory. The query string rides
        along because the job id is in it. Ungated — the page itself is what
        asks for the cover key."""
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(_spa_url(request.app, query))
