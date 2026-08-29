"""Cover Studio's job store and orchestration.

A job is one book's cover session, persisted as a directory under
`COVER_DATA_PATH` (local default: `cover_jobs/` under cwd — see
`default_root()`): a `job.json` (the single source of truth, atomically
rewritten after every step), the manuscript's grounding sample when one was
uploaded, generated art assets, and rendered covers. See
docs/cover_designer_spec.md §8/§8.1.

`run_job` and `run_revision` are meant to run as detached asyncio background
tasks (the routes create them with `asyncio.create_task` and register them
with `register_task` for stale-job detection — see `check_interrupted`). Both
take `root`/`job_id` rather than an in-memory JobState because a background
task outlives the request that started it: re-reading from disk at each step
is what makes concurrent painting/composing across concepts, and a concurrent
revision on another concept of the same job, safe (see `_commit_concept`).

This module is library-layer, not app-layer: it never reads COVER_KEY or an
API key, and `default_root()` is the one place it reads an environment
variable at all — a plain data-directory default, not a secret. The caller
(app/routes/cover.py) builds the Provider and the imaging client and hands
them in, the same way app/routes/quest.py builds a Provider for
docproof.quest.skin.generate_skin.

docproof.cover.direction / .compose / .imaging are sibling modules built in
parallel and may not exist yet at import time. Each is imported at MODULE
level inside try/except — specifically so a test can monkeypatch these names
directly on this module (`monkeypatch.setattr(pipeline, "run_directions",
fake)`); a lazy import inside a function body would re-import the real thing
on every call and silently ignore the patch. Before those modules land, the
callable placeholders are `None` and calling one raises a clear TypeError
rather than doing the wrong thing quietly. docproof.cover.reality is the one
sibling NOT guarded this way — it was built in the same pass as this file,
by the same hand, so there is no window where it doesn't exist yet; it is
imported plainly, which still leaves it monkeypatchable on this module for
the same reason (`monkeypatch.setattr(pipeline, "distill_reality", fake)`).

Providers (below) bundles one Provider per model role — direction, revision,
reality — because the BRAIN wave (2026-08-29) pins each to its own model
(docproof.cover.direction.DIRECTION_MODEL/REVISION_MODEL,
docproof.cover.reality.REALITY_MODEL). It is built once per request in
app/routes/cover.py and threaded through run_job/run_revision unchanged;
docproof.cover.critique's vision call is deliberately NOT a fourth field
here — it goes through the raw `anthropic` SDK, not the Provider protocol
(see critique.py's own module docstring), so its client is threaded
separately as `critique_client`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from pydantic import ValidationError

from docproof.ingest import IngestError
from docproof.providers import Provider
from docproof.quest.skin import read_sample_source, sample_text
from docproof.utils.files import write_atomic

from .archetypes import ARCHETYPES, Archetype
from .model import (ArtSlot, Brief, ConceptState, CoverSpec, JobState,
                    RenderReport, Zone, build_spec)
from .reality import RealitySheetError, RealityResult, distill_reality

log = logging.getLogger("docproof.cover.pipeline")

try:
    from .direction import (DirectionError, DirectionResult, RevisionError,
                            RevisionResult, revise_spec, run_directions)
except ImportError:                                        # pragma: no cover
    class DirectionError(Exception):
        """Placeholder until docproof.cover.direction lands."""

    class RevisionError(Exception):
        """Placeholder until docproof.cover.direction lands."""

    DirectionResult = RevisionResult = None                 # type: ignore[assignment]
    run_directions = revise_spec = None                     # type: ignore[assignment]

try:
    from .compose import compose, save_renders
except ImportError:                                        # pragma: no cover
    compose = save_renders = None                            # type: ignore[assignment]

try:
    from .critique import CritiqueError, CritiqueResult, run_critique
except ImportError:                                        # pragma: no cover
    class CritiqueError(Exception):
        """Placeholder until docproof.cover.critique lands."""

    CritiqueResult = None                                    # type: ignore[assignment]
    run_critique = None                                      # type: ignore[assignment]

try:
    from .imaging import (CUTOUT_SUFFIX, IMAGE_COST, NEGATIVE_SUFFIX,
                          ImagingError, generate, has_real_alpha, make_client)
except ImportError:                                        # pragma: no cover
    class ImagingError(Exception):
        """Placeholder until docproof.cover.imaging lands."""

    # Kept identical to the pinned table (docs/cover_designer_spec.md §7.2) so
    # cost math and tests work even before imaging.py exists.
    IMAGE_COST = {"1K": 0.03, "2K": 0.05, "4K": 0.08}
    NEGATIVE_SUFFIX = ("Absolutely no text, no letters, no words, no numbers, "
                       "no watermarks, no borders, no frames.")
    generate = has_real_alpha = make_client = None          # type: ignore[assignment]

JOB_MANIFEST = "job.json"
MANUSCRIPT_SAMPLE_NAME = "manuscript_sample.txt"
REALITY_SHEET_NAME = "reality.json"
ASSETS_DIR = "assets"
RENDERS_DIR = "renders"

# The iterating judge loop's round cap (§6.3, iterated — BRAIN wave): a
# "round" is one critique CALL, so this bounds judge spend, not revision
# spend — see _critique_and_revise's own docstring for exactly where the
# cap gates a revision versus just a critique.
MAX_CRITIQUE_ROUNDS = 4


@dataclass(frozen=True)
class Providers:
    """One Provider per model role: the frontier model for art direction,
    the workhorse model for revision (both human-triggered and the
    auto-critique loop), and the workhorse model for manuscript
    distillation. Three separate instances, not one shared Provider, because
    each role is pinned to its OWN model id
    (docproof.cover.direction.DIRECTION_MODEL/REVISION_MODEL,
    docproof.cover.reality.REALITY_MODEL) — see this module's own docstring
    for why critique isn't a fourth field here. Built once per request in
    app/routes/cover.py and threaded through run_job/run_revision
    unchanged."""
    direction: Provider
    revision: Provider
    reality: Provider

# One job's image generations run bounded by this — network-bound SDK calls,
# not memory-bound, so 2 in flight is about latency, not the 512MB box (§7.4).
IMAGE_CONCURRENCY = 2

# The resolution every generation asks for. Not yet a per-job knob (§7.2's
# default); a module constant so it is one place to change.
IMAGE_RESOLUTION = "2K"

# Every asyncio primitive below is created lazily and keyed by the RUNNING
# event loop (plus job_id where relevant): an asyncio.Lock binds to the loop
# it is first awaited on, and a module-level Lock created at import time dies
# with "bound to a different event loop" the moment a second loop appears —
# which never happens under uvicorn (one loop for the process's lifetime,
# so production behavior is identical), but happens immediately in any CLI
# script or test that calls asyncio.run() per job. The registries grow by one
# entry per loop (and per job for the keyed ones) — the same modest tradeoff
# the job_id-keyed registries already accepted.

# One compose() at a time, PROCESS-WIDE (per loop), not per job: Pillow
# buffers (1600x2560x4 ~= 16MB per layer, ~5 layers per compose) are the
# 512MB box's real constraint, and that budget is shared by every job on the
# machine, not owned by one (§7.4).
_COMPOSE_LOCKS: dict[int, asyncio.Lock] = {}

# Serializes job.json read-modify-write across the concurrent tasks that can
# touch one job at once: a job's own concept tasks (painting in parallel) and
# any revision running on a different concept of the same job. asyncio is
# cooperative-single-threaded, so a plain dict get-or-create is not racy.
_STATE_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}

# One image-generation semaphore PER JOB, shared by run_job and every
# concurrent run_revision on that job — a local Semaphore per call would
# bound each call to IMAGE_CONCURRENCY while letting the job's true
# in-flight total exceed it (two ready-concept revisions + a still-painting
# job = 6 calls at once on a 512MB box).
_IMAGE_SEMAPHORES: dict[tuple[int, str], asyncio.Semaphore] = {}


def _loop_id() -> int:
    return id(asyncio.get_running_loop())


def _compose_lock() -> asyncio.Lock:
    return _COMPOSE_LOCKS.setdefault(_loop_id(), asyncio.Lock())


def _state_lock(job_id: str) -> asyncio.Lock:
    return _STATE_LOCKS.setdefault((_loop_id(), job_id), asyncio.Lock())


def _image_semaphore(job_id: str) -> asyncio.Semaphore:
    return _IMAGE_SEMAPHORES.setdefault(
        (_loop_id(), job_id), asyncio.Semaphore(IMAGE_CONCURRENCY))

# job_id -> the background tasks currently working it (the one run_job task,
# and/or any run_revision tasks). Used only for stale-job detection
# (check_interrupted) — a job stuck in a non-terminal state with nothing in
# here was orphaned by a restart (scale-to-zero, a crash) and will never
# finish on its own. Process-local and module-level on purpose, the same call
# app.quest_site.RateLimiter makes: a registry that survived a restart would
# be more machinery than the thing it tracks, and a restart is exactly the
# case this exists to detect.
_LIVE_TASKS: dict[str, set[asyncio.Task]] = {}


# -- data root ----------------------------------------------------------------

def default_root() -> Path:
    """COVER_DATA_PATH if set (Fly: `/data/cover`), else `cover_jobs/` under
    cwd. The one environment read in this module — a data directory default,
    not a secret, so it does not need to live behind app.settings. Callers
    that already have an opinion (the routes, reading `app.state`) pass their
    own root instead of calling this."""
    env = os.environ.get("COVER_DATA_PATH")
    return Path(env) if env else Path("cover_jobs")


def job_dir(root: str | Path, job_id: str) -> Path:
    return Path(root) / job_id


# -- job.json persistence ------------------------------------------------------

def _write_state(root: str | Path, job: JobState) -> None:
    """Atomic rewrite of job.json — the single source of truth, so a poll (or
    a restart) always sees the truth after every step (§8.1)."""
    write_atomic(job_dir(root, job.job_id) / JOB_MANIFEST, job.model_dump_json(indent=2))


def load_job(root: str | Path, job_id: str) -> JobState | None:
    path = job_dir(root, job_id) / JOB_MANIFEST
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("Cover job %s: could not read job.json: %s", job_id, e)
        return None
    try:
        return JobState.model_validate_json(text)
    except ValidationError as e:
        log.warning("Cover job %s: job.json failed to validate: %s", job_id, e)
        return None


def list_jobs(root: str | Path, limit: int = 20) -> list[JobState]:
    """Every job under `root`, newest first by `created` — the "pick up where
    I left off" list (§9). A job whose directory exists but whose job.json is
    unreadable is skipped rather than raised, the same tolerance load_job
    itself gives a torn or corrupt record."""
    root = Path(root)
    if not root.is_dir():
        return []
    jobs = [job for p in root.iterdir() if p.is_dir()
           and (job := load_job(root, p.name)) is not None]
    jobs.sort(key=lambda j: j.created, reverse=True)
    return jobs[:limit]


def total_usd(job: JobState) -> float:
    return sum(float(row.get("usd", 0.0) or 0.0) for row in job.ledger)


# -- stale-job detection (§8) --------------------------------------------------

def register_task(job_id: str, task: asyncio.Task) -> None:
    """Track a background task for one job, so check_interrupted can tell a
    job that is genuinely still working from one a restart orphaned. Call
    right after `asyncio.create_task(run_job(...))` /
    `asyncio.create_task(run_revision(...))`; the task deregisters itself
    when it finishes, successfully or not."""
    tasks = _LIVE_TASKS.setdefault(job_id, set())
    tasks.add(task)

    def _cleanup(finished: asyncio.Task, *, _job_id: str = job_id) -> None:
        live = _LIVE_TASKS.get(_job_id)
        if live is not None:
            live.discard(finished)
            if not live:
                _LIVE_TASKS.pop(_job_id, None)

    task.add_done_callback(_cleanup)


def is_job_alive(job_id: str) -> bool:
    return bool(_LIVE_TASKS.get(job_id))


def check_interrupted(root: str | Path, job: JobState) -> JobState:
    """Scale-to-zero honesty (§8): a job (or a concept mid-revision) stuck in
    a non-terminal state with no live background task was orphaned by a
    restart and will never finish on its own — surfaced here as a plain,
    actionable error instead of a poll that hangs forever. Called by the poll
    route on every GET; a no-op, unchanged return when nothing is stale."""
    if is_job_alive(job.job_id):
        return job
    changed = False
    if job.status in ("directing", "working"):
        job.status = "error"
        job.error = "interrupted — run it again"
        changed = True
    for concept in job.concepts:
        if concept.status in ("queued", "painting", "composing"):
            concept.status = "error"
            concept.error = "interrupted — run it again"
            changed = True
    if changed:
        _write_state(root, job)
    return job


async def _commit_concept(root: str | Path, job_id: str, index: int,
                          concept: ConceptState, ledger_rows: Any = ()) -> None:
    """Merge one concept's new state (and any ledger rows) into the CURRENT
    job.json, under this job's state lock. Re-reads from disk rather than
    trusting an in-memory JobState, because sibling concept tasks (or a
    concurrent revision on another concept) may have written in the
    meantime — see the module docstring."""
    async with _state_lock(job_id):
        job = load_job(root, job_id)
        if job is None or index >= len(job.concepts):
            return
        job.concepts[index] = concept
        if ledger_rows:
            job.ledger.extend(ledger_rows)
        _write_state(root, job)


# -- manuscript handling (§8.1) ------------------------------------------------

def _read_sample(root: str | Path, job_id: str) -> str:
    """The persisted grounding sample, re-read from disk rather than carried
    in memory — nothing large is held between requests (§7.4). Empty string
    for a job with no manuscript, or if the file has gone missing."""
    path = job_dir(root, job_id) / MANUSCRIPT_SAMPLE_NAME
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("Cover job %s: could not re-read its manuscript sample: %s",
                   job_id, e)
        return ""


def new_job_id() -> str:
    """UTC date + 6 hex chars, e.g. `20260828-a1b2c3` (§8)."""
    return f"{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(3)}"


def create_job(root: str | Path, brief: Brief, *,
               manuscript_path: str | Path | None = None,
               manuscript_name: str = "") -> JobState:
    """Create a new job on disk: the brief, plus a manuscript sample when one
    was uploaded.

    `manuscript_path` is a local file the caller has already validated for
    suffix/size (the route's upload handling, `_read_upload`-style) — this
    function does the actual read. The manuscript is read and sampled BEFORE
    anything is written to disk: a file that cannot be read (a corrupt .docx,
    a file with no text) raises IngestError and leaves NOTHING behind — no
    job directory, no job.json — so a 400 here never orphans a job (§8.1).
    Only the sample is ever persisted (`manuscript_sample.txt`), never the
    full manuscript; the sample is all the direction call reads."""
    sample = ""
    word_count = 0
    if manuscript_path is not None:
        ms = read_sample_source(manuscript_path)
        if not ms.text.strip():
            raise IngestError(
                f"{manuscript_name or Path(manuscript_path).name} contains "
                f"no readable text.")
        sample = sample_text(ms.text)
        word_count = ms.word_count

    job_id = new_job_id()
    d = job_dir(root, job_id)
    while d.exists():                      # collision guard; astronomically rare
        job_id = new_job_id()
        d = job_dir(root, job_id)
    d.mkdir(parents=True)
    if sample:
        (d / MANUSCRIPT_SAMPLE_NAME).write_text(sample, encoding="utf-8")

    job = JobState(job_id=job_id, brief=brief, manuscript_name=manuscript_name,
                  word_count=word_count, status="directing",
                  created=datetime.now(timezone.utc).isoformat())
    _write_state(root, job)
    return job


# -- prompt assembly (§7.2, §8) ------------------------------------------------

def _assemble_prompt(slot: ArtSlot, archetype: Archetype) -> str:
    """slot.prompt + the archetype's composition note (steers the art to
    leave room for the type) + the fixed negative suffix — assembled here,
    not in imaging.py, because only the pipeline knows which archetype a
    slot's spec belongs to. A transparent slot also gets the cutout
    directive: without it the model paints a whole scene and calls it a
    cutout (§7.2)."""
    cutout = f" {CUTOUT_SUFFIX}" if slot.transparent else ""
    return f"{slot.prompt} {archetype.composition_note}{cutout} {NEGATIVE_SUFFIX}"


async def _generate_art_slot(image_client: Any, sem: asyncio.Semaphore,
                             d: Path, index: int, art_slot: ArtSlot,
                             archetype: Archetype) -> list[dict]:
    """Generate one art slot's image, save it under the job's assets/, and
    point the slot's `asset` at it. Returns the ledger rows this generation
    produced (the image cost, plus an opaque-fallback note when a
    transparent request came back without real alpha — §7.2/§5.2.3)."""
    prompt = _assemble_prompt(art_slot, archetype)
    async with sem:
        png_bytes = await asyncio.to_thread(
            generate, image_client, prompt, transparent=art_slot.transparent,
            resolution=IMAGE_RESOLUTION)
    rel = f"{ASSETS_DIR}/c{index}_{art_slot.id}.png"
    (d / rel).write_bytes(png_bytes)
    art_slot.asset = rel
    rows = [{"kind": "image", "concept": index,
            "detail": f"concept {index} {art_slot.id} ({IMAGE_RESOLUTION})",
            "usd": IMAGE_COST[IMAGE_RESOLUTION]}]
    if art_slot.transparent and not await asyncio.to_thread(has_real_alpha, png_bytes):
        # The model ignored the transparency request (§7.2 — the feature is
        # in preview). `art_slot.transparent` is deliberately left True, not
        # flipped here: compose._degrade_opaque_focal re-derives this same
        # has_real_alpha check straight from the asset on disk and does the
        # §5.2.3 layer-order swap itself (title drawn on top of the opaque
        # focal instead of under it) — it is written to distrust exactly this
        # kind of pre-decided flag from an upstream step. This ledger row is
        # informational only, so the ledger explains a render the report's
        # own warnings will also carry.
        rows.append({"kind": "image", "concept": index,
                     "detail": (f"concept {index} {art_slot.id} came back "
                               f"without real transparency; the composer will "
                               f"draw the text on top of it instead of "
                               f"underneath"),
                     "usd": 0.0})
    return rows


async def _render(spec: CoverSpec, d: Path, index: int
                  ) -> tuple[RenderReport, list[str]]:
    """compose() + save_renders() — the shared tail of run_job's per-concept
    flow and run_revision. Both Pillow-heavy steps share ONE lock, not just
    compose(): save_renders holds a comparable buffer (the composed image,
    re-encoded to PNG/JPG plus two thumbnails) on the same 512MB box (§7.4),
    so "only one compose-shaped operation at a time" covers both."""
    async with _compose_lock():
        image, report = await asyncio.to_thread(compose, spec, d)
        renders = await asyncio.to_thread(save_renders, image, d, spec.version, index)
    # save_renders writes four files (png, jpg, thumb300, thumb100) but the
    # job record carries only the primary .png per version: the page derives
    # the companion filenames from it, so one entry means one version there.
    return report, renders[:1]


# -- spec diffing: "revising did nothing" made visibly impossible -------------
#
# Two small, pure functions (no I/O — trivial to unit-test directly), shared
# by the auto-critique loop below and by run_revision's own human-revision
# path: diff_spec_fields turns two CoverSpec VERSIONS into a compact,
# human-readable field-level diff, computed from the validated pydantic
# models themselves — never trusted from the model's own prose — so a
# revision that silently no-ops cannot be dressed up as a confident-sounding
# note. _dump_equal_ignoring_bookkeeping answers a narrower question (did
# ANYTHING outside version/notes_log change at all), used to stop the
# auto-critique loop the moment a revision has nothing left to give.

_PALETTE_ROLES: tuple[str, ...] = ("background", "primary", "accent", "text", "scrim")

# ArtSlot/TextSlot fields worth naming in a diff clause. `asset` is
# deliberately excluded from both — a regenerated art path is a PIPELINE
# signal (revise_spec's own asset-diffing already reports it separately: see
# the "kept the existing art" ledger row), not a design change in its own
# right, and `prompt` is reported as "rewritten" rather than dumped in full
# (a prompt runs to whole sentences; nobody wants that in a ledger line).
_ART_DIFF_FIELDS: tuple[str, ...] = ("transparent", "fit", "anchor", "scale",
                                    "offset", "opacity", "blend", "treatment",
                                    "mask_from", "corners", "scatter")
_TEXT_DIFF_FIELDS: tuple[str, ...] = ("font_family", "case", "tracking", "align",
                                     "valign", "max_lines", "size_min",
                                     "size_max", "color_role", "optional", "mode")


def _fmt(value: Any) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


def _zone_clauses(label: str, old: Zone, new: Zone) -> list[str]:
    """Zone moves are reported as a direction and a magnitude (a designer's
    own shorthand — "title zone up 8%"), not four raw fractions; x/y=0 is
    the canvas's top-left corner, matching docproof.cover.model.Zone."""
    clauses = []
    if abs(old.y - new.y) > 1e-9:
        clauses.append(f"{label} zone {'up' if new.y < old.y else 'down'} "
                       f"{round(abs(old.y - new.y) * 100)}%")
    if abs(old.x - new.x) > 1e-9:
        clauses.append(f"{label} zone {'left' if new.x < old.x else 'right'} "
                       f"{round(abs(old.x - new.x) * 100)}%")
    if abs(old.w - new.w) > 1e-9:
        clauses.append(f"{label} zone {'wider' if new.w > old.w else 'narrower'} "
                       f"{round(abs(old.w - new.w) * 100)}%")
    if abs(old.h - new.h) > 1e-9:
        clauses.append(f"{label} zone {'taller' if new.h > old.h else 'shorter'} "
                       f"{round(abs(old.h - new.h) * 100)}%")
    return clauses


def diff_spec_fields(old: CoverSpec, new: CoverSpec) -> str:
    """A compact, semicolon-joined, human-readable list of what changed
    between two CoverSpec versions, e.g. "title zone up 8%;
    palette.primary #6b2d3e→#a83250; title size_max 0.1→0.13". Pure
    function — no I/O, no randomness, safe to call on any two specs
    regardless of whether `new` is actually a revision of `old`. Empty
    string when nothing this function looks at differs (version and
    notes_log are never compared — see _dump_equal_ignoring_bookkeeping for
    the stricter, exhaustive version of that same question)."""
    clauses: list[str] = []

    if old.archetype != new.archetype:
        clauses.append(f"archetype {old.archetype}→{new.archetype}")
    if old.concept_name != new.concept_name:
        clauses.append(f'concept_name "{old.concept_name}"→"{new.concept_name}"')
    if old.rationale != new.rationale:
        clauses.append("rationale changed")

    for role in _PALETTE_ROLES:
        ov, nv = getattr(old.palette, role), getattr(new.palette, role)
        if ov != nv:
            clauses.append(f"palette.{role} {ov}→{nv}")

    old_art = {a.id: a for a in old.art}
    for slot in new.art:
        prior = old_art.get(slot.id)
        if prior is None:
            clauses.append(f"{slot.id} art slot added")
            continue
        if prior.prompt != slot.prompt:
            clauses.append(f"{slot.id}.prompt rewritten")
        for field in _ART_DIFF_FIELDS:
            ov, nv = getattr(prior, field), getattr(slot, field)
            if ov != nv:
                clauses.append(f"{slot.id}.{field} {_fmt(ov)}→{_fmt(nv)}")

    for i, scrim in enumerate(new.scrims):
        if i >= len(old.scrims):
            clauses.append(f"scrim[{i}] added")
            continue
        prior = old.scrims[i]
        if prior.strength != scrim.strength:
            clauses.append(f"scrim[{i}].strength "
                           f"{_fmt(prior.strength)}→{_fmt(scrim.strength)}")
        if prior.kind != scrim.kind:
            clauses.append(f"scrim[{i}].kind {prior.kind}→{scrim.kind}")
        if prior.protects != scrim.protects:
            clauses.append(f"scrim[{i}].protects {prior.protects}→{scrim.protects}")

    old_text = {t.id: t for t in old.text}
    for slot in new.text:
        prior = old_text.get(slot.id)
        if prior is None:
            clauses.append(f"{slot.id} text slot added")
            continue
        clauses.extend(_zone_clauses(slot.id, prior.zone, slot.zone))
        for field in _TEXT_DIFF_FIELDS:
            ov, nv = getattr(prior, field), getattr(slot, field)
            if ov != nv:
                clauses.append(f"{slot.id} {field} {_fmt(ov)}→{_fmt(nv)}")
        if prior.shadow != slot.shadow:
            clauses.append(f"{slot.id} shadow changed")
        if prior.stroke != slot.stroke:
            clauses.append(f"{slot.id} stroke changed")

    if [(r.kind, r.ref) for r in old.layers] != [(r.kind, r.ref) for r in new.layers]:
        clauses.append("layer order changed")

    return "; ".join(clauses)


def _dump_equal_ignoring_bookkeeping(old: CoverSpec, new: CoverSpec) -> bool:
    """Whether two spec VERSIONS are the same DESIGN — every field except the
    two that mechanically always change on any revise_spec call (`version`
    +1, `notes_log` grows by one entry) regardless of whether the model
    changed anything else. Plain model_dump() equality would be vacuously
    False on every call for exactly that reason. Used by the auto-critique
    loop to detect "the model had nothing more to give" (dump-equality, per
    docs/cover_designer_spec.md's iterating-judge-loop decision) — a
    stricter, exhaustive check than diff_spec_fields, which only names the
    fields it knows how to describe."""
    return (old.model_dump(exclude={"version", "notes_log"})
           == new.model_dump(exclude={"version", "notes_log"}))


# -- critique pass (§6.3, iterated — BRAIN wave) -------------------------------

def _thumb_path(png_path: Path) -> Path:
    """The 100px shelf/search thumbnail save_renders wrote right next to
    this render (BRAIN v2.1 — compose.save_renders's own `_thumb100.png`
    naming: `renders/v{version}_c{concept}.png` ->
    `renders/v{version}_c{concept}_thumb100.png`), computed rather than
    threaded through as a second return value, since it is a pure function
    of the path save_renders already gave back."""
    return png_path.with_name(f"{png_path.stem}_thumb100{png_path.suffix}")


def _run_critique_safely(job_id: str, index: int, spec: CoverSpec, brief: Brief,
                         png_path: Path, critique_client: Any,
                         ledger_rows: list[dict], warnings: Sequence[str],
                         round_n: int) -> CritiqueResult | None:
    """run_critique, with §6.3's "a critique failure must never block a
    cover" contract enforced in exactly one place: a CritiqueError here is
    logged and turned into a $0 ledger note, never raised further. Returns
    None on failure (as opposed to a CritiqueResult with passes=True) so the
    caller can tell "no verdict was reached" apart from "the verdict was a
    pass" — the two must not be conflated into ready for the same reason.

    `warnings` (the composer's own RenderReport.warnings for THIS render) is
    threaded straight through to run_critique as `composer_warnings`, which
    labels them into the call's summary — see that module's own docstring.
    `round_n` (1-based) is folded into every ledger row's `detail` so a job
    that iterated several rounds reads as a sequence, not a blur — see
    _critique_and_revise, the only caller.

    The 100px thumbnail (BRAIN v2.1) is read separately from the render, and
    its own OSError is swallowed to None rather than joining the render's —
    a missing thumbnail (an old job, a save_renders fake in a test that only
    writes the primary PNG) must degrade run_critique to one image, never
    turn into a "the critique call failed" ledger note the way a genuinely
    unreadable RENDER does.

    Called directly, not via asyncio.to_thread: run_directions and
    revise_spec (both synchronous network calls, same as this one) are
    already called directly elsewhere in this module — §7.4 only calls out
    image generation and composition as needing thread-offload, and this
    stays consistent with the pattern already shipped for the other two
    model calls."""
    try:
        png_bytes = png_path.read_bytes()
        try:
            thumb_bytes: bytes | None = _thumb_path(png_path).read_bytes()
        except OSError:
            thumb_bytes = None
        verdict = run_critique(png_bytes, thumb_bytes, spec, brief,
                               critique_client, composer_warnings=warnings)
    except (CritiqueError, OSError) as e:
        log.warning("Cover job %s concept %d round %d: critique call "
                   "failed, shipping as composed: %s", job_id, index,
                   round_n, e)
        ledger_rows.append({
            "kind": "critique", "concept": index,
            "detail": (f"concept {index} round {round_n}: critique call "
                      f"failed ({e}); shipped as composed"),
            "usd": 0.0})
        return None
    ledger_rows.append({
        "kind": "critique", "concept": index,
        "detail": (f"concept {index} round {round_n}: passed" if verdict.passes
                  else f"concept {index} round {round_n}: flagged "
                       f"{len(verdict.tells)} tell(s)"),
        "usd": verdict.cost or 0.0})
    return verdict


async def _critique_and_revise(job_id: str, index: int, spec: CoverSpec,
                               brief: Brief, d: Path, providers: Providers,
                               critique_client: Any, image_client: Any,
                               sem: asyncio.Semaphore, report: RenderReport,
                               renders: list[str]
                               ) -> tuple[CoverSpec, RenderReport, list[str], list[dict]]:
    """§6.3, ITERATED (BRAIN wave, 2026-08-29): the owner's beta verdict was
    that a single critique-then-revise round left the judge merely
    reporting problems instead of actually fixing them — invisible-to-the-
    user iteration was the fix. Loop up to MAX_CRITIQUE_ROUNDS rounds; a
    "round" is one CRITIQUE CALL, so the cap bounds judge spend, not
    revision spend: a critique that still fails on the very last permitted
    round ships with its tells as leftover warnings rather than buying one
    more revision nothing will ever re-check (the `round_n ==
    MAX_CRITIQUE_ROUNDS` guard below is exactly that gate) — a 4-round cap
    therefore means at most 4 critique calls and at most 3 revisions.

    Three things stop the loop before the cap: the critique call itself
    failing (verdict is None — §6.3's "must never block a cover", unchanged
    from v1), a genuine pass, or a revision that comes back IDENTICAL to
    the spec it started from (_dump_equal_ignoring_bookkeeping — the model
    had nothing left to give, and looping further would only spend money to
    relearn that; noted in the ledger, not recomposed) AND did not also
    trigger an art repaint this round (see below — a repaint changes real
    pixels on disk without changing the spec's `asset` string at all, so
    the identical-spec check is blind to it on purpose and must not be the
    thing that stops the loop when one just happened). A RevisionError is
    equally non-fatal: logged, ledgered, and the concept ships with
    whatever it already has.

    Every round's critique call, and every revision it triggers, gets its
    own ledger row (round-numbered — see _run_critique_safely). Every
    revision that actually recomposes also gets a compact, CODE-COMPUTED
    notes_log entry naming what changed (diff_spec_fields, a pure function
    diffing the validated spec models themselves — never the model's own
    prose) as a SEPARATE "[auto-critique r{n} changed] ..." entry right
    after the "[auto-critique r{n}] <the note that was applied>" entry —
    "revising did nothing" is visibly impossible this way, since the log
    itself measures the effect rather than repeating the request.

    allow_new_art is always False for every auto round — the code-level
    backstop below holds regardless of what the model wrote, the same
    restore-the-prior-asset behavior v1 had. The ONE exception (BRAIN
    v2.1): a slot the JUDGE names in CritiqueResult.art_defects — a
    generation defect a design-only revision can never fix — may repaint,
    at most 2 slots and at most once per concept for the whole job
    (`repaint_used` below), through the same _generate_art_slot path a
    fresh concept's own art uses; `image_client`/`sem` exist on this
    function's signature for exactly that one path.

    Returns (spec, report, renders, ledger_rows) — all three of the first
    unchanged from what was passed in unless at least one round actually
    revised and recomposed. Human-triggered run_revision never calls this
    (§6.3: "the human is the critic there") — it gains the same
    diff_spec_fields note on its own path instead, and never repaints on
    its own initiative."""
    ledger_rows: list[dict] = []
    verdict: CritiqueResult | None = None
    repaint_used = False

    for round_n in range(1, MAX_CRITIQUE_ROUNDS + 1):
        verdict = _run_critique_safely(
            job_id, index, spec, brief, d / renders[-1], critique_client,
            ledger_rows, report.warnings, round_n)
        if verdict is None or verdict.passes:
            break
        if round_n == MAX_CRITIQUE_ROUNDS:
            break         # no budget left to judge one more revision

        try:
            result = revise_spec(spec, verdict.notes, providers.revision)
        except RevisionError as e:
            log.warning("Cover job %s concept %d: auto-critique revision "
                       "failed on round %d, shipping the last composition: "
                       "%s", job_id, index, round_n, e)
            ledger_rows.append({
                "kind": "revision", "concept": index,
                "detail": (f"concept {index} round {round_n}: auto-critique "
                          f"revision failed ({e}); shipped the last "
                          f"composition"),
                "usd": 0.0})
            break

        # revise_spec appends `notes` verbatim as the last notes_log entry
        # (see its own docstring); swap that one entry for the prefixed,
        # round-numbered form rather than appending a second copy of it.
        revised = result.spec.model_copy(update={
            "notes_log": [*result.spec.notes_log[:-1],
                         f"[auto-critique r{round_n}] {verdict.notes}"]})

        # The code-level allow_new_art=False backstop the docstring
        # promises: whatever the revision model wrote, an auto round may
        # never leave a generated slot assetless — revise_spec clears
        # `asset` when a prompt changes (the regenerate signal), and
        # recomposing without restoring it would render that layer blank.
        # Auto rounds never repaint; put the prior art straight back, like
        # run_revision's own allow_new_art=False branch does.
        prior_assets = {slot.id: slot.asset for slot in spec.art}
        restored = False
        for slot in revised.art:
            if slot.prompt and not slot.asset and prior_assets.get(slot.id, ""):
                slot.asset = prior_assets[slot.id]
                restored = True
        if restored:
            ledger_rows.append({
                "kind": "revision", "concept": index,
                "detail": (f"concept {index} round {round_n}: the "
                          f"auto-critique revision asked for new art; kept "
                          f"the existing art (auto rounds never repaint)"),
                "usd": 0.0})

        # BRAIN v2.1's one-repaint escape hatch: a design-only revision can
        # never fix a defective GENERATED image (a surreal blob, an eye
        # baked into a lighthouse lantern). When the verdict named
        # art_defects and this concept hasn't spent its one repaint yet,
        # clear up to 2 of those flagged slots' assets and repaint them for
        # real, through the same _generate_art_slot path a fresh concept's
        # own art uses — the same prompt, or the one the revision above
        # just rewrote if it touched that slot (regeneration re-rolls the
        # image either way). A hallucinated or non-generatable slot id is
        # dropped rather than raised, same tolerance direction._validate_
        # direction gives a stray art_prompts entry. This deliberately runs
        # AFTER the restore-backstop above and re-clears regardless of what
        # it just restored — simpler than teaching that loop about
        # art_defects, and the net effect (a fresh paint) is the same.
        repainted = False
        if verdict.art_defects and not repaint_used:
            slot_map = {s.id: s for s in revised.art}
            eligible = [sid for sid in verdict.art_defects
                       if (s := slot_map.get(sid)) is not None and s.prompt]
            flagged = eligible[:2]
            if flagged:
                archetype = ARCHETYPES[revised.archetype]
                for sid in flagged:
                    slot_map[sid].asset = ""
                for slot_rows in await asyncio.gather(*(
                        _generate_art_slot(image_client, sem, d, index,
                                           slot_map[sid], archetype)
                        for sid in flagged)):
                    ledger_rows.extend(slot_rows)
                for sid in flagged:
                    ledger_rows.append({
                        "kind": "image", "concept": index,
                        "detail": (f"concept {index} slot {sid} repainted "
                                  f"on judge's flag"),
                        "usd": 0.0})
                repaint_used = True
                repainted = True

        # A repaint changes real pixels on disk without changing the spec's
        # `asset` STRING at all (_generate_art_slot writes to a fixed,
        # deterministic path per slot — see its own docstring), so the
        # identical-spec check below is blind to it by construction; a
        # repaint this round must never let that check be the thing that
        # stops the loop, or the fresh paint would ship uncritiqued.
        if not repainted and _dump_equal_ignoring_bookkeeping(spec, revised):
            ledger_rows.append({
                "kind": "revision", "concept": index,
                "detail": (f"concept {index} round {round_n}: the revision "
                          f"produced an identical spec; stopped (the model "
                          f"has nothing more to give)"),
                "usd": result.cost or 0.0})
            break

        diff_note = diff_spec_fields(spec, revised) or "(no field-level change detected)"
        revised = revised.model_copy(update={
            "notes_log": [*revised.notes_log,
                         f"[auto-critique r{round_n} changed] {diff_note}"]})

        ledger_rows.append({
            "kind": "revision", "concept": index,
            "detail": f"concept {index} round {round_n}: auto-critique revision",
            "usd": result.cost or 0.0})

        new_report, new_renders = await _render(revised, d, index)
        renders = [*renders, *new_renders]
        spec, report = revised, new_report
        # loop continues: round_n + 1 critiques THIS round's recomposed render

    if verdict is not None and verdict.tells:
        report = report.model_copy(
            update={"warnings": [*report.warnings, *verdict.tells]})

    return spec, report, renders, ledger_rows


# -- run_job: directing, then per-concept painting/composing (§8) -------------

async def _paint_and_compose(root: str | Path, job_id: str, index: int,
                             providers: Providers, image_client: Any,
                             critique_client: Any,
                             sem: asyncio.Semaphore) -> None:
    """One concept's whole life after directing: painting -> composing ->
    critique (§6.3) -> ready, or error. A failure here (an ImagingError, or
    anything else) is caught and recorded on THIS concept alone — siblings
    are unaffected, because each concept is an independent task under the
    same gather (§8)."""
    job = load_job(root, job_id)
    if job is None or index >= len(job.concepts):
        return
    brief = job.brief
    concept = job.concepts[index]
    try:
        concept.status = "painting"
        await _commit_concept(root, job_id, index, concept)

        spec = concept.spec
        archetype = ARCHETYPES[spec.archetype]
        d = job_dir(root, job_id)
        (d / ASSETS_DIR).mkdir(parents=True, exist_ok=True)

        ledger_rows: list[dict] = []
        # All of one concept's generations in flight together — the per-job
        # semaphore (not this gather) is what bounds actual concurrency, so
        # awaiting them one by one would just serialize what the semaphore
        # already meters (11 of 15 archetypes declare 2+ generatable slots).
        gen_slots = [s for s in spec.art if s.prompt and not s.asset]
        for rows in await asyncio.gather(*(
                _generate_art_slot(image_client, sem, d, index, s, archetype)
                for s in gen_slots)):
            ledger_rows.extend(rows)

        concept.status = "composing"
        await _commit_concept(root, job_id, index, concept, ledger_rows)

        report, renders = await _render(spec, d, index)
        spec, report, renders, critique_rows = await _critique_and_revise(
            job_id, index, spec, brief, d, providers, critique_client,
            image_client, sem, report, renders)

        concept.spec = spec
        concept.status = "ready"
        concept.report = report
        concept.renders = [*concept.renders, *renders]
        await _commit_concept(root, job_id, index, concept, critique_rows)
    except Exception as e:  # noqa: BLE001 - one concept's failure must not kill siblings
        concept.status = "error"
        concept.error = f"This concept could not be finished: {e}"
        await _commit_concept(root, job_id, index, concept)
        log.warning("Cover job %s concept %d failed: %s", job_id, index, e)


async def run_job(root: str | Path, job_id: str, providers: Providers,
                  image_client: Any, critique_client: Any) -> None:
    """The whole job flow (§8): distill a reality sheet from the manuscript
    sample when there is one, one direction call, a CoverSpec built per
    concept, then every concept painted and composed independently and in
    parallel. Meant to run as a detached background task — register it with
    register_task right after creating it, so an interrupted run is
    detectable rather than a poll that hangs forever."""
    job = load_job(root, job_id)
    if job is None:
        log.warning("run_job: cover job %s vanished before it could start", job_id)
        return

    # Reality-sheet distillation (docproof.cover.reality, BRAIN wave): ONCE
    # per job, before directing, so every concept's direction call reads the
    # SAME curated sheet rather than each re-reading raw prose. Runs only
    # when there is a sample to distill; `sample` is reassigned to the
    # rendered sheet on success so run_directions below receives it as its
    # ordinary manuscript_sample argument — same parameter, no signature
    # change there (see direction.py's _sample_rule docstring). A
    # distillation failure is deliberately non-fatal: falls back to the raw
    # sample, exactly what every job did before this module existed, logged
    # and ledgered rather than ever blocking the job on it.
    sample = _read_sample(root, job_id)
    if sample:
        try:
            reality_result: RealityResult = distill_reality(
                sample, providers.reality)
        except RealitySheetError as e:
            log.warning("Cover job %s: reality-sheet distillation failed; "
                       "falling back to the raw manuscript sample: %s",
                       job_id, e)
            job.ledger.append({
                "kind": "reality",
                "detail": (f"reality-sheet distillation failed ({e}); used "
                          f"the raw manuscript sample instead"),
                "usd": 0.0})
        else:
            sample = reality_result.rendered
            (job_dir(root, job_id) / REALITY_SHEET_NAME).write_text(
                reality_result.sheet.model_dump_json(indent=2),
                encoding="utf-8")
            job.ledger.append({
                "kind": "reality",
                "detail": f"distilled a reality sheet via {reality_result.model}",
                "usd": reality_result.cost or 0.0})

    try:
        result: DirectionResult = run_directions(
            job.brief, providers.direction, n=job.brief.concepts,
            manuscript_sample=sample)
    except DirectionError as e:
        job.status, job.error = "error", str(e)
        _write_state(root, job)
        return
    except Exception as e:  # noqa: BLE001 - the job must end visibly, not hang
        log.exception("Cover job %s: directing failed unexpectedly", job_id)
        job.status = "error"
        job.error = f"Something went wrong starting this job: {e}"
        _write_state(root, job)
        return

    try:
        job.concepts = [
            ConceptState(spec=build_spec(d, job.brief, ARCHETYPES[d.archetype]),
                        status="queued")
            for d in result.directions]
    except Exception as e:  # noqa: BLE001 - a bad archetype must fail visibly
        # Without this guard a build_spec validation error dies inside a
        # detached task, the job never leaves "directing", and the next poll
        # mislabels it "interrupted" — hiding the real cause (most likely a
        # malformed archetype file) behind a retry that fails identically.
        log.exception("Cover job %s: building concept specs failed", job_id)
        job.status = "error"
        job.error = (f"A design concept could not be assembled from its "
                     f"archetype: {e}")
        _write_state(root, job)
        return
    job.status = "working"
    job.ledger.append({"kind": "direction",
                       "detail": f"{len(result.directions)} concepts via {result.model}",
                       "usd": result.cost or 0.0})
    # A generatable slot with no prompt ships as a blank layer — legal (a
    # direction may lean procedural on purpose) but worth saying out loud:
    # the first live v2 batch shipped EVERY cover artless because the
    # direction's prompts named slots that don't exist and were dropped.
    for i, concept in enumerate(job.concepts):
        archetype = ARCHETYPES[concept.spec.archetype]
        generatable = {a.id for a in archetype.art if a.generatable}
        promptless = sorted(
            s.id for s in concept.spec.art
            if s.id in generatable and not s.prompt and not s.procedural)
        if promptless:
            job.ledger.append({
                "kind": "direction", "concept": i,
                "detail": (f"concept {i}: no art prompt reached "
                          f"{', '.join(promptless)} — those layers render "
                          f"empty"),
                "usd": 0.0})
    _write_state(root, job)

    sem = _image_semaphore(job_id)
    await asyncio.gather(*(
        _paint_and_compose(root, job_id, i, providers, image_client,
                           critique_client, sem)
        for i in range(len(job.concepts))))

    job = load_job(root, job_id)
    if job is not None:
        job.status = "ready"
        _write_state(root, job)


# -- run_revision (§6.2, §8) ---------------------------------------------------

async def run_revision(root: str | Path, job_id: str, concept_index: int,
                       notes: str, allow_new_art: bool, providers: Providers,
                       image_client: Any) -> None:
    """revise_spec -> (maybe) regenerate the art it cleared -> recompose ->
    ready. Meant to run as a detached background task, exactly like run_job;
    register it with register_task too, so a revision interrupted mid-flight
    is detectable the same way an interrupted job is.

    `allow_new_art=False` keeps every existing asset even when the notes
    changed a prompt: revise_spec clears `asset` on any art slot whose prompt
    or transparency changed (that clearing is the regenerate signal), so this
    puts the prior path straight back and logs why nothing was repainted.

    Gains the same code-computed diff note the auto-critique loop writes
    (diff_spec_fields — see _critique_and_revise's own docstring): a human
    revision's notes_log entry is the human's own words, which say what was
    ASKED for, not necessarily what actually changed, so a "[changed] ..."
    entry naming the real field-level diff rides along right after it."""
    job = load_job(root, job_id)
    if job is None or concept_index >= len(job.concepts):
        return
    concept = job.concepts[concept_index]
    prior_assets = {slot.id: slot.asset for slot in concept.spec.art}

    try:
        result: RevisionResult = revise_spec(concept.spec, notes, providers.revision)
        spec = result.spec
        # Validate BEFORE anything is committed: a revision may legitimately
        # switch archetypes (the notes can ask for it), but an unknown key
        # stored on the concept would make every retry fail identically —
        # the prior version must survive a typo'd note untouched.
        if spec.archetype not in ARCHETYPES:
            raise RevisionError(
                f"The revision switched to archetype {spec.archetype!r}, "
                f"which is not one of the shipped archetypes; the previous "
                f"version is unchanged. (Real keys: "
                f"{', '.join(sorted(ARCHETYPES))}.)")
        ledger_rows = [{"kind": "revision", "concept": concept_index,
                        "detail": f"concept {concept_index}: {notes or '(retry)'}",
                        "usd": result.cost or 0.0}]

        # revise_spec clears `asset` on exactly the slots that need a new
        # image; a slot with no prompt at all (procedural — big_type's
        # background, the grain texture) has an empty `asset` on purpose and
        # must never be sent to imaging.generate() with a near-blank prompt.
        cleared = [s for s in spec.art if s.prompt and not s.asset]
        if cleared and not allow_new_art:
            for slot in spec.art:
                prior = prior_assets.get(slot.id, "")
                if not slot.asset and prior:
                    slot.asset = prior
            ledger_rows.append({
                "kind": "revision", "concept": concept_index,
                "detail": (f"concept {concept_index}: art change requested but "
                          f"art regen is off; kept the existing art"),
                "usd": 0.0})
            cleared = []

        # The code-computed diff note (see this function's own docstring):
        # a SEPARATE notes_log entry naming what actually changed, computed
        # by diffing the validated spec models — never the human's own
        # words, which say what was asked for, not what happened.
        diff_note = diff_spec_fields(concept.spec, spec)
        if diff_note:
            spec = spec.model_copy(update={
                "notes_log": [*spec.notes_log, f"[changed] {diff_note}"]})

        concept.spec = spec
        concept.status = "painting" if cleared else "composing"
        concept.error = None
        await _commit_concept(root, job_id, concept_index, concept, ledger_rows)

        if cleared:
            d = job_dir(root, job_id)
            (d / ASSETS_DIR).mkdir(parents=True, exist_ok=True)
            archetype = ARCHETYPES[spec.archetype]
            sem = _image_semaphore(job_id)
            image_rows: list[dict] = []
            for rows in await asyncio.gather(*(
                    _generate_art_slot(image_client, sem, d, concept_index,
                                       s, archetype)
                    for s in cleared)):
                image_rows.extend(rows)
            concept.status = "composing"
            await _commit_concept(root, job_id, concept_index, concept, image_rows)

        d = job_dir(root, job_id)
        report, renders = await _render(spec, d, concept_index)

        concept.status = "ready"
        concept.report = report
        concept.renders = [*concept.renders, *renders]
        await _commit_concept(root, job_id, concept_index, concept)
    except RevisionError as e:
        concept.status = "error"
        concept.error = str(e)
        await _commit_concept(root, job_id, concept_index, concept)
    except Exception as e:  # noqa: BLE001 - a revision failure must not corrupt the concept
        concept.status = "error"
        concept.error = f"This revision could not be finished: {e}"
        await _commit_concept(root, job_id, concept_index, concept)
        log.warning("Cover job %s revision on concept %d failed: %s",
                   job_id, concept_index, e)


__all__ = [
    "ASSETS_DIR", "IMAGE_CONCURRENCY", "IMAGE_RESOLUTION", "JOB_MANIFEST",
    "MANUSCRIPT_SAMPLE_NAME", "MAX_CRITIQUE_ROUNDS", "REALITY_SHEET_NAME",
    "RENDERS_DIR", "Providers",
    "check_interrupted", "create_job", "default_root", "diff_spec_fields",
    "is_job_alive", "job_dir", "list_jobs", "load_job", "new_job_id",
    "register_task", "run_job", "run_revision", "total_usd",
]
