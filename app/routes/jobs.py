"""The work: creating jobs, watching them, and collecting what they wrote."""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from typing import get_args

from docproof import batch as batchlib
from docproof.config import (SmoothingConfig, examination_graph_killed,
                             examination_judgment_killed, load_config)
from docproof.formats import get_format
from docproof.models import Usage
from docproof.providers import build_provider, cost_of_usage, estimate_cost, lookup
from docproof.profiles import DETECTOR_ONLY, PROFILE_KEYS
from docproof.variants import VARIANT_KEYS

from . import common
from .. import features as featureslib
from .. import settings as settingslib
from ..auth import owner_for
from ..jobs import Job, JobRunner, JobStore, read_usage
from ..prompts import list_prompts
from ..report import build_report
from ..settings import CONFIG_PATH, ERROR_DIR, Paths
from ..spending import LedgerEntry
from ..usage import build_usage

log = logging.getLogger("docproof.app.routes.jobs")

# The smoothing dials' valid values, read straight off SmoothingConfig's Literals
# so the API's accepted set can never drift from what the config will accept.
_RESTRAINT_LEVELS = get_args(
    SmoothingConfig.model_fields["proposer_restraint"].annotation)
_HARSHNESS_LEVELS = get_args(
    SmoothingConfig.model_fields["judge_harshness"].annotation)


class JobRequest(BaseModel):
    file_ids: list[str] = Field(min_length=1)
    # Empty is allowed so a corrections job — which runs no model — need not send
    # one. Review and prep still require a real model: `lookup("")` returns None
    # and the create route 400s, exactly as it did when this had no default.
    model: str = ""
    mode: str = "batch"                       # "now" | "batch"
    schedule_at: str | None = None            # "HH:MM" local
    min_confidence: str = "medium"
    # A named server-enforced run boundary. Unlike `preset` (a display label),
    # this is applied to the resolved Config and frozen into batch manifests.
    profile: str = ""
    # Which English this manuscript is written in. Empty keeps the config's own
    # variant, so an older page that doesn't send it — and the watcher, which
    # never does — behaves exactly as before.
    variant: str = ""
    # Reasoning depth for this run. None falls back to the saved default, so an
    # older page that doesn't send it keeps whatever the defaults screen set.
    effort: str | None = None
    # Which model reads the whole book for the glossary pass. None falls back to
    # the saved default; "off" turns the pass off for this run.
    glossary_model: str | None = None
    # file_id → chunk ids to review. A file absent from this map, or a null
    # entry, means the whole document.
    selections: dict[str, list[str] | None] | None = None
    # What to do with these documents: review them for errors, prepare them for
    # the house InDesign template, or apply a corrections list to an exported
    # InDesign file.
    kind: str = "review"                      # "review" | "prep" | "corrections"
    # Corrections only: the corrections list, as the JSON the parser accepts
    # (a list of {find, replace, …}, or [find, replace] pairs). Empty on every
    # other kind. The IDML it edits is the single uploaded file.
    corrections: str = ""
    # Corrections from a marked-up PDF: the reviewer comments the edits were read
    # from, as JSON [{id, page, kind, instruction, anchor}, …]. Kept so the change
    # log can account for every comment — including ones no edit was made for.
    # Empty when the list was typed or pasted, not read off a proof.
    corrections_comments: str = ""
    # Corrections from a marked-up PDF: the text of each page of the proof, in
    # order, as JSON. The page map aligns these against the book so a mark narrows
    # to the text its own page set. Empty when the list was typed or pasted.
    corrections_pages: str = ""
    # Corrections only: run the model sanity gate over the edits before applying.
    # It is a grammar safety net — it holds an edit back only when, applied as the
    # author's mark asked, it would leave a broken sentence, never over an editorial
    # call. None defaults it on; an explicit false turns it off.
    corrections_sanity: bool | None = None
    # Corrections only: run the second look before applying — a stronger model
    # re-reads the notes the extractor left as queries and commits the delegated
    # ones (an either/or the reviewer offered, a conditional the page settles) to
    # concrete edits. None defaults it on; an explicit false turns it off.
    corrections_second_look: bool | None = None
    # Corrections only: run the last tier — a frontier model given the whole book
    # for the queries even the second look declined. None follows the second-look
    # flag (on by default); an explicit true/false controls it on its own, so its
    # frontier spend can be asked for or refused separately from the second look.
    corrections_escalate: bool | None = None
    prep_output: str = "book"       # "book" | "indesign" | "tracked" | "both" | "all"
    # Book output only: the operator's answers for the sketch. Empty means
    # "let the detector read them off the opening pages".
    prep_subject: str = ""
    prep_title: str = ""
    prep_author: str = ""
    # Per-run pass toggles, {feature_id: on}. Omitted or empty leaves the config
    # defaults. Unknown ids are refused, not ignored — see create_jobs.
    features: dict[str, bool] | None = None
    # Per-run per-category tuning: {category_id: {passes?, token_budget?}}, each
    # an int >= 1. Unknown ids and bad values are refused in create_jobs, not
    # ignored. Omitted/empty leaves the config's per-category defaults.
    category_knobs: dict[str, dict] | None = None
    # Multi-round review: review the manuscript this many times, each round
    # reading the previous round's corrections. None falls back to the saved
    # default; 1 is the ordinary single review. judge_prompt is the panel-edited
    # judge instructions; empty means the built-in default.
    rounds: int | None = None
    judge_prompt: str = ""
    # The between-round judge model. None/empty falls back to the config default.
    judge_model: str | None = None
    # The continuity read's editable system prompt; empty means the built-in
    # default. continuity_only runs that whole-book contradiction read on its
    # own, with every detector pass and sweep stripped off — and, with it, the
    # chapter-scoped read below.
    continuity_prompt: str = ""
    continuity_only: bool = False
    # The whole-book continuity model. None means the config default (the house
    # reviewer); a pick opts into a frontier whole-book read, like the glossary's.
    continuity_model: str | None = None
    # The chapter-continuity reader's editable system prompt; empty means the
    # built-in default. Its on/off rides the `features` map like every other pass.
    chapter_continuity_prompt: str = ""
    # The chapter-continuity model (one pick sets both reader and judge) and the
    # 1–5 sensitivity dial. None means the config default for each.
    chapter_continuity_model: str | None = None
    chapter_continuity_sensitivity: int | None = None
    # The judge gates' models and editable instructions. None/empty falls back to
    # the config default; each gate itself is a feature toggle.
    meaning_model: str | None = None
    meaning_prompt: str = ""
    fix_model: str | None = None
    fix_prompt: str = ""
    # A label for which effort tier the picker applied, carried onto the job card
    # only. The tier is a client-side macro over the controls above, so this
    # never changes how the job runs; empty means a custom or older submission.
    preset: str = ""
    # Smoothing pass tuning. Only bites when the `smoothing` feature is on; None
    # falls back to the config default, so an older page — and the watcher —
    # keep today's behaviour. proposer_restraint = how much the line editor
    # surfaces; judge_harshness = how hard the taste judge culls it.
    proposer_restraint: str | None = None    # "restrained" | "open"
    judge_harshness: str | None = None       # lenient|balanced|strict|severe


# The model that reads a free-form corrections list into an exact edit list.
# Luna, the stronger house reader — not the config's bare api.model default
# (Haiku): turning vague prose into anchored find/replace is the hard part, and a
# person reviews the draft before anything is applied, so the quality is worth
# the small cost. The redlined-Word path is deterministic and uses no model at all.
CORRECTIONS_EXTRACT_MODEL = "gpt-5.6-luna"

# The model for the opt-in second look over the extractor's queries. Sol, the
# strong sibling on the same key Luna's extraction already proved is set: the
# pass exists to make the editorial calls the reviewer delegated ("em dash or
# semicolon — pick"), which is judgment work, and it reads a handful of notes
# per book, so the stronger model costs cents where it costs anything.
CORRECTIONS_SECOND_LOOK_MODEL = "gpt-5.6-sol"

# The model for the last tier — the queries even the second look would not
# answer, put up with the passage around the mark and every occurrence in the
# book of the terms the note names. Opus rather than Fable: the work is reading
# a lot of gathered evidence and being honest about what it does not settle,
# which Opus does at half Fable's price, and the tier reads a handful of notes
# per book either way. Change this one line to "claude-fable-5" for the ceiling.
# A book with no Anthropic key falls back to the second look's model, so the
# tier still runs rather than silently not existing.
CORRECTIONS_ESCALATE_MODEL = "claude-opus-5"

# How many marked-up comments to read in one model call. A proof can carry
# hundreds; reading them all at once overruns the model's output ceiling and
# truncates, silently losing every edit. A bounded batch keeps each call small,
# fast and safe — and gives the panel something to show progress against. The
# panel reads the batches back and extracts them one at a time so the reviewer
# watches the count climb instead of a single long, silent wait.
CORRECTIONS_PDF_BATCH_SIZE = 40


class ExtractListRequest(BaseModel):
    """A free-form corrections list to read into a draft edit list."""
    text: str


class ResolveRequest(BaseModel):
    """One flagged correction acted on from the review screen. Exactly one of
    the four actions: a clicked *option* (a concrete placement the report
    offered — deterministic, free), a typed *answer* for the model to
    transcribe into an exact edit, *suggestion* (one click carrying out the
    advice a model pass already wrote on this flag, through the same typed
    path), or *dismiss* (deliberately leave it — recorded as set aside, nothing
    written). *reopen* undoes a dismiss and nothing else."""
    item_id: str
    option_id: str = ""
    text: str = ""
    suggestion: bool = False
    dismiss: bool = False
    reopen: bool = False
    # The manual editor's save: the paragraph's address, the text it loaded
    # (the staleness guard), the text as the designer edited it, the desired
    # bold/italic runs, and the section-break asks. See resolve.apply_manual.
    manual: dict | None = None


class ParagraphRequest(BaseModel):
    """The manual editor asking for one paragraph, with the text the report
    recorded so a renumbered line is relocated rather than misread."""
    story_id: str
    paragraph: int
    expect: str = ""


class EditRequest(BaseModel):
    """A touch-up save: a manual edit that answers no flag. Same `manual`
    payload as a resolution's."""
    manual: dict


class SuggestRequest(BaseModel):
    """One flag the designer wants the model's fix for, shown before accepting."""
    item_id: str


class RejudgeRequest(BaseModel):
    """Which gates to run over a finished review, and on what. Nothing else is
    re-run, so the switches here ARE the job — an empty request is refused
    rather than quietly rebuilding the same document."""
    meaning_check: bool = False
    fix_check: bool = False
    meaning_model: str | None = None
    fix_model: str | None = None
    meaning_prompt: str = ""
    fix_prompt: str = ""


# The states a job stays in for good: it has stopped, so it can be removed from
# the results list. Everything else is still moving and must be aborted first.
_TERMINAL = ("done", "failed", "cancelled")


def _result_name(job: Job, which: str) -> str | None:
    """What a job called the file the user is asking for."""
    stem = Path(job.filename).stem or "document"
    names = {
        # "docx" is the old name for this route, kept so a page left open
        # across an upgrade keeps working.
        "document": get_format(job.filename).reviewed_name(job.filename),
        "docx": get_format(job.filename).reviewed_name(job.filename),
        "changes": get_format(job.filename).change_log_name(job.filename),
        "summary": "summary.md",
        "findings": "findings.json",
        "examination": "examination-coverage.md",
        "examination-json": "examination-coverage.json",
        "examination-ledger": "examination-ledger.jsonl.gz",
        "examination-evaluation": "examination-evaluation.json",
        "book": f"book_{stem}.docx",
        # The InDesign deliverable is now an IDML the designer opens directly —
        # no Place step. (Kept name "indesign" so existing buttons/routing work.)
        "indesign": f"tagged_{stem}.idml",
        "tracked": f"tracked_{stem}.docx",
        "notes": "prep_notes.md",
        "prep": "prep.json",
        "placed": f"placed_{stem}.indd",
        # Corrections: the corrected IDML the designer reopens, and its report.
        "corrected": f"{stem}_corrected.idml",
        "corrections-notes": "corrections_notes.md",
        "corrections": "corrections.json",
    }
    if job.is_corrections and which in ("document", "docx"):
        # One "open it" button gives back the corrected file, whatever the
        # generic caller asked for.
        return names["corrected"]
    if job.is_prep and which in ("document", "docx"):
        # Whatever this job actually wrote, so one "open it" button works for
        # any output choice — counting the archive as well as the disk, so a
        # restored prep job (no local results yet) still resolves to the file it
        # really produced rather than crashing on a None results_dir.
        def _present(n: str) -> bool:
            if job.results_dir and (Path(job.results_dir) / n).is_file():
                return True
            return n in job.drive_files
        return next((n for n in (names["book"], names["indesign"],
                                 names["tracked"])
                     if _present(n)), names["book"])
    return names.get(which)


def _result_path(job: Job, which: str) -> Path:
    """The file on disk behind one of the result buttons.

    Raises the same 404s the download route has always raised: an unknown name
    and a name whose file was moved or deleted are different problems, and the
    person reading the message is looking at their own Documents folder."""
    name = _result_name(job, which)
    if name is None:
        raise HTTPException(404, "Unknown file")
    path = Path(job.results_dir) / name
    if not path.is_file():
        raise HTTPException(404, f"{name} is missing")
    return path


def _create_corrections(req: JobRequest, owner: str, paths: Paths,
                        runner: JobRunner, *, effort: str = "medium") -> dict:
    """Create a corrections job: one exported IDML, one corrections list.

    Deterministic and free, so none of the review/prep vetting (models, keys,
    rounds, the spend cap) applies — but the two inputs are checked here, in
    front of the person who chose them, rather than failing the run at write
    time: exactly one .idml, and a corrections list that parses to at least one
    edit."""
    if len(req.file_ids) != 1:
        raise HTTPException(
            400, "Corrections take one InDesign file at a time — a corrections "
                 "list is specific to the book it was written for.")
    source = common.resolve_upload(paths, req.file_ids[0], owner)
    if source is None:
        raise HTTPException(404, f"Uploaded file {req.file_ids[0]!r} is gone")
    if source.suffix.lower() != ".idml":
        raise HTTPException(
            400, "Corrections are applied to an InDesign file — export the "
                 ".indd as IDML (File → Export → InDesign Markup) and upload "
                 "that.")
    if not req.corrections.strip():
        raise HTTPException(400, "Paste the corrections list to apply.")
    # Parse now so a list that is malformed as a whole (not valid JSON), or that
    # yields nothing usable, is refused here rather than as a failed job later.
    # Partial issues are fine — the run applies the good edits and reports the
    # rest — so only an empty result is turned away.
    from docproof.corrections.parse import parse_edits
    try:
        parsed = parse_edits(req.corrections)
    except ValueError as e:
        raise HTTPException(400, f"The corrections list could not be read: {e}")
    if not parsed.edits:
        detail = (parsed.issues[0].reason if parsed.issues
                  else "the list held no corrections")
        raise HTTPException(400, f"No usable corrections were found — {detail}.")

    # The reviewer comments, when the list came from a marked-up PDF, are kept as
    # sent so the finished change log can account for every one. A malformed blob
    # is dropped rather than failing the job — the edits still apply; the ledger
    # just falls back to edit-only, as it does for a typed list.
    comments = ""
    if req.corrections_comments.strip():
        try:
            if isinstance(json.loads(req.corrections_comments), list):
                comments = req.corrections_comments
        except (json.JSONDecodeError, ValueError):
            comments = ""

    # The proof's page texts, kept the same way and for the same reason: they only
    # narrow where an edit may land, so a malformed blob costs precision, never a
    # correction, and is dropped rather than failing the job.
    pages = ""
    if req.corrections_pages.strip():
        try:
            if isinstance(json.loads(req.corrections_pages), list):
                pages = req.corrections_pages
        except (json.JSONDecodeError, ValueError):
            pages = ""

    group_id = datetime.now(timezone.utc).strftime("g%Y%m%d%H%M%S")
    job = Job(
        id=batchlib.new_job_id(source.name),
        filename=source.name,
        source_path=str(source),
        model="",                             # deterministic; no model runs
        mode="now",                           # never batched
        group_id=group_id,
        kind="corrections",
        corrections=req.corrections,
        corrections_comments=comments,
        corrections_pages=pages,
        corrections_sanity=(True if req.corrections_sanity is None
                            else bool(req.corrections_sanity)),
        corrections_second_look=(True if req.corrections_second_look is None
                                 else bool(req.corrections_second_look)),
        corrections_escalate=((True if req.corrections_second_look is None
                               else bool(req.corrections_second_look))
                              if req.corrections_escalate is None
                              else bool(req.corrections_escalate)),
        effort=effort,
        created_at=datetime.now(timezone.utc).isoformat(),
        owner_id=owner,
    )
    return {"jobs": [runner.enqueue(job).to_api()], "group_id": group_id}


def _edits_to_corrections_json(edits) -> str:
    """Serialize an extracted `Edit` list back into the corrections JSON the
    textarea holds, so a person reviews and edits it before anything is applied.
    Fields at their default are left out so the result reads clean."""
    rows = []
    for e in edits:
        row = {"find": e.find, "replace": e.replace}
        if e.context:
            row["context"] = e.context     # keeps the anchor across review→apply
        if e.instruction:
            row["instruction"] = e.instruction
        if e.kind != "mechanical":         # model.MECHANICAL
            row["kind"] = e.kind
        if e.occurrence:
            row["occurrence"] = e.occurrence
        if e.source:
            row["source"] = e.source       # ties the edit to its PDF comment id
        if e.format:
            row["format"] = e.format       # italics are an edit, not a design note
        if e.paragraph:
            row["paragraph"] = e.paragraph  # a forced break, a keep, a para added
        if e.paragraph_style:
            row["paragraph_style"] = e.paragraph_style
        rows.append(row)
    return json.dumps(rows, indent=2, ensure_ascii=False)


def _extract_response(result) -> dict:
    """A parsed corrections source, shaped for the panel: the JSON to drop into
    the textarea, the edit count, and any entries that could not be read."""
    return {
        "json": _edits_to_corrections_json(result.edits),
        "count": len(result.edits),
        "issues": [{"index": i.index, "reason": i.reason} for i in result.issues],
    }


def _record_extract_spend(app, owner: str, model: str, usage: Usage, *,
                          filename: str = "corrections list") -> None:
    """Snapshot a corrections extraction's (small) model spend to the ledger, so
    it shows in the dashboard rather than being invisible. Filed under the
    corrections kind; best-effort, never fails the request."""
    try:
        cost = cost_of_usage(usage, fallback_model=model, batch=False)
        stamp = datetime.now(timezone.utc)
        app.state.runner.ledger.record(LedgerEntry(
            id=f"cx-{stamp.strftime('%Y%m%d%H%M%S%f')}",
            filename=filename, kind="corrections", model=model,
            mode="now", source="app", owner_id=owner,
            created_at=stamp.isoformat(), words=0,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens,
            cache_write_tokens=usage.cache_creation_input_tokens,
            api_calls=usage.api_calls, cost=cost or 0.0))
    except Exception:                      # noqa: BLE001 - spend logging is not the job
        log.warning("Could not record corrections-extraction spend", exc_info=True)


def _build_extract_provider():
    """The Luna provider the corrections extractor runs on, or a 400 the caller
    surfaces when no key is set. Shared by the single-source and batched paths."""
    cfg = load_config(CONFIG_PATH)
    model = CORRECTIONS_EXTRACT_MODEL
    cfg.api.model = model                     # so build_provider builds the OpenAI client
    info = lookup(model)
    key = settingslib.get_api_key(info.provider) if info else None
    if not key:
        raise HTTPException(
            400, "No API key is set for the extraction model "
                 f"({info.display if info else model}). Paste the corrections as "
                 "JSON, or upload a redlined Word file instead.")
    return build_provider(cfg, api_key=key), model


def _build_resolve_provider():
    """The `(provider, model)` the review screen's typed answers are transcribed
    by: the second-look model (the pass this most resembles — carrying out a
    settled editorial call), falling back to the extraction model when only its
    vendor has a key. A 400 the screen can show when neither does."""
    cfg = load_config(CONFIG_PATH)
    for model in (CORRECTIONS_SECOND_LOOK_MODEL, CORRECTIONS_EXTRACT_MODEL):
        info = lookup(model)
        if info and settingslib.get_api_key(info.provider):
            cfg.api.model = model
            return build_provider(cfg, api_key=settingslib.get_api_key(
                info.provider)), model
    raise HTTPException(
        400, "No API key is set for a model that can carry out a typed answer "
             "— add one in Settings, or pick one of the offered placements "
             "instead.")


# One resolution at a time per job: two clicks racing would both rewrite the
# corrected IDML and its report. Process-local, like the app's other runtime
# state — there is one server over one job folder.
_resolve_locks: dict[str, threading.Lock] = {}
_resolve_locks_guard = threading.Lock()


def _resolve_lock(job_id: str) -> threading.Lock:
    with _resolve_locks_guard:
        return _resolve_locks.setdefault(job_id, threading.Lock())


def _extract_with_model(app, owner: str, source_text: str) -> dict:
    """Read a corrections source (a prose list, or the text pulled from a PDF's
    comments) into a draft edit list with Luna, recording the small spend. The
    shared body of the list and PDF extract endpoints."""
    from docproof.corrections.extract import ExtractionError, extract_edits
    provider, model = _build_extract_provider()
    usage = Usage()
    try:
        result = extract_edits(source_text, provider, model=model, usage=usage)
    except ExtractionError as e:
        raise HTTPException(502, f"The model could not read that: {e}")
    _record_extract_spend(app, owner, model, usage)
    return _extract_response(result)


def _extract_batches_with_model(app, owner: str, sources: list[str]) -> dict:
    """Read a set of bounded corrections sources (the batches of a marked-up PDF)
    into one draft edit list, one model call per batch so no call can truncate.
    Every batch's spend is rolled into one ledger entry. Used by the server-side
    `extract-pdf` fallback; the panel drives the same batches itself (via
    `read-pdf` + `extract-list`) so the reviewer sees the count climb live."""
    from docproof.corrections.extract import ExtractionError, extract_edits_batched
    provider, model = _build_extract_provider()
    usage = Usage()
    try:
        result = extract_edits_batched(sources, provider, model=model, usage=usage)
    except ExtractionError as e:
        raise HTTPException(502, f"The model could not read that: {e}")
    _record_extract_spend(app, owner, model, usage)
    return _extract_response(result)


def register(app: FastAPI) -> None:

    def _owned_job(job_id: str, owner: str) -> Job | None:
        """A job the caller is allowed to see, or None — for a 404 that reads
        the same whether the job is missing or someone else's, so a job id
        can't be probed for existence. On the desktop build there are no owners
        to check; the one local user sees everything, as before."""
        job = app.state.store.get(job_id)
        if job is None:
            return None
        if app.state.web and job.owner_id != owner:
            return None
        return job

    def _resolve_result(job: Job, name: str) -> Path | None:
        """The local path for one of a job's files, fetched back from the Drive
        archive and re-cached if the local copy is gone — the thing that lets a
        results tab still work after a redeploy wiped the folder, or after a
        volume loss left the record with no local results at all. None only when
        the file is in neither place; the caller answers the 404.

        The happy path — the file is on disk — costs one `is_file` and never
        touches Drive."""
        from ..watch import archive as archivelib
        if job.results_dir and (Path(job.results_dir) / name).is_file():
            return Path(job.results_dir) / name
        runner = app.state.runner
        if runner.notify_home is None or not job.drive_files.get(name):
            return None
        # A restored job has no results folder yet; claim one on the volume so the
        # fetched file (and its siblings, next click) has a durable home.
        dest_dir = (Path(job.results_dir) if job.results_dir
                    else runner.results_dir(job))
        fetched = archivelib.fetch_file(runner.notify_home, job, name, dest_dir)
        if fetched is not None and fetched.is_file():
            if not job.results_dir:
                app.state.store.update(job.id, results_dir=str(dest_dir))
            return fetched
        return None

    def _ensure_source(job: Job) -> Job:
        """Make sure the submitted manuscript is on disk before a re-run needs
        it, fetching `source - <name>` back from the archive when a wipe took the
        original. Leaves the job untouched when the source is already there or the
        archive cannot supply it — the run then fails as it would have."""
        from ..watch import archive as archivelib
        if job.source_path and Path(job.source_path).is_file():
            return job
        runner = app.state.runner
        original = Path(job.filename).name
        if runner.notify_home is None or not job.drive_files.get(f"source - {original}"):
            return job
        dest_dir = (app.state.paths.uploads / (job.owner_id or "local")
                    / f"restored-{job.id}")
        got = archivelib.fetch_file(runner.notify_home, job,
                                    f"source - {original}", dest_dir,
                                    save_as=original)
        if got is not None and got.is_file():
            return app.state.store.update(job.id, source_path=str(got)) or job
        return job

    @app.post("/api/jobs")
    def create_jobs(req: JobRequest,
                    owner: str = Depends(owner_for)) -> dict:
        paths: Paths = app.state.paths
        runner: JobRunner = app.state.runner
        if req.mode not in ("now", "batch"):
            raise HTTPException(400, "mode must be 'now' or 'batch'")
        if req.kind not in ("review", "prep", "corrections"):
            raise HTTPException(
                400, "kind must be 'review', 'prep' or 'corrections'")
        if req.profile and req.profile not in PROFILE_KEYS:
            raise HTTPException(
                400, f"profile must be one of {', '.join(PROFILE_KEYS)}")
        # A profile is a review-only concept, but corrections and prep share the
        # review create form, whose tier picker is always populated — so an
        # "apply corrections" started while a profile-bearing review tier is
        # selected (the detector-only batch profile) carries that profile along.
        # It is inert off a review, so it is dropped rather than refused: a hard
        # 400 here blocked a designer from applying corrections whenever such a
        # tier was live (e.g. an overnight detector-only batch). Kept durable on
        # the server so a browser still serving cached JS is not stranded.
        if req.profile and req.kind != "review":
            req.profile = ""
        if req.effort is not None and req.effort not in settingslib.EFFORT_LEVELS:
            raise HTTPException(
                400, f"effort must be one of {', '.join(settingslib.EFFORT_LEVELS)}")
        # Corrections is its own short path: one exported IDML plus a corrections
        # list, applied deterministically. It shares nothing with the review/prep
        # vetting below (models, keys, rounds, the spend cap), so it is handled here
        # and returns before any of it — but its opt-in model passes (extraction,
        # the second look, the last tier) still read a reasoning effort, so the
        # picker's choice is resolved and carried onto the job the same way review's
        # is, rather than every corrections run silently taking the "low" default.
        if req.kind == "corrections":
            effort = req.effort or app.state.settings.effort
            return _create_corrections(req, owner, paths, runner, effort=effort)
        if req.prep_output not in ("book", "indesign", "tracked", "both", "all"):
            raise HTTPException(
                400, "prep_output must be 'book', 'indesign', 'tracked', "
                     "'both' or 'all'")
        unknown = featureslib.unknown_features(req.features)
        if unknown:
            raise HTTPException(
                400, f"Unknown feature(s): {', '.join(sorted(unknown))}")
        # Prep reads its windows in order — a paragraph's meaning depends on
        # what came before it — so there is no batch form of it to offer.
        mode = "now" if req.kind == "prep" else req.mode
        if req.effort is not None and req.effort not in settingslib.EFFORT_LEVELS:
            raise HTTPException(
                400, f"effort must be one of {', '.join(settingslib.EFFORT_LEVELS)}")
        # Refused here rather than left to Config's Literal, which would raise
        # mid-run — after the upload, and on the batch path days after submit.
        if req.variant and req.variant not in VARIANT_KEYS:
            raise HTTPException(
                400, f"variant must be one of {', '.join(VARIANT_KEYS)}")
        # Smoothing dials — vetted here, not left to Config's Literal, for the
        # same reason variant is: a bad value would otherwise raise mid-run.
        if (req.proposer_restraint is not None
                and req.proposer_restraint not in _RESTRAINT_LEVELS):
            raise HTTPException(
                400, f"proposer_restraint must be one of "
                     f"{', '.join(_RESTRAINT_LEVELS)}")
        if (req.judge_harshness is not None
                and req.judge_harshness not in _HARSHNESS_LEVELS):
            raise HTTPException(
                400, f"judge_harshness must be one of "
                     f"{', '.join(_HARSHNESS_LEVELS)}")
        # Per-category knobs — vetted here (like the smoothing dials and variant)
        # rather than left to Config's validator, which would raise mid-run,
        # after the upload and on the batch path days after submit. Each id must
        # be a real category (content-based, so it survives a later collect); each
        # passes/token_budget an int >= 1.
        if req.category_knobs:
            valid_ids = {c["id"]
                         for c in load_config(CONFIG_PATH).category_states()}
            for cid, knob in req.category_knobs.items():
                if cid not in valid_ids:
                    raise HTTPException(400, f"Unknown category {cid!r}")
                if not isinstance(knob, dict) or (
                        set(knob) - {"passes", "token_budget"}):
                    raise HTTPException(
                        400, f"category {cid!r}: only 'passes' and "
                             f"'token_budget' may be set")
                for name in ("passes", "token_budget"):
                    val = knob.get(name)
                    if val is not None and (not isinstance(val, int)
                                            or isinstance(val, bool) or val < 1):
                        raise HTTPException(
                            400, f"category {cid!r}: {name} must be an "
                                 f"integer >= 1")
        effort = req.effort or app.state.settings.effort
        # Multi-round review is a review-only knob; prep is always a single pass.
        rounds = req.rounds if req.rounds is not None else app.state.settings.rounds
        if req.kind == "prep":
            rounds = 1
        if not 1 <= rounds <= 4:
            raise HTTPException(400, "rounds must be between 1 and 4")
        if req.profile == DETECTOR_ONLY and (
                rounds != 1 or req.continuity_only):
            raise HTTPException(
                400, "detector-only requires one review round and cannot be "
                     "combined with continuity-only")
        # Phase 1B is deliberately a narrow experiment on Fly: an administrator
        # may run it synchronously over one ordinary review. It never enters the
        # batch collector (whose checkpoint lifecycle is different), prep, or a
        # multi-round run. Enforce that at the API boundary as well as in the UI
        # so a hand-written request cannot create an unsupported run.
        _house = None
        judgment_asked = (req.features or {}).get("examination_judgment")
        if judgment_asked is None:
            _house = load_config(CONFIG_PATH)
            judgment_on = _house.examination_graph.judgment.enabled
        else:
            judgment_on = bool(judgment_asked)
        if judgment_on:
            if app.state.web:
                user = app.state.accounts.get_user(owner)
                if not (user and user.is_admin):
                    raise HTTPException(
                        403, "The independent examination judge is currently "
                             "an administrator-only experiment.")
            if examination_graph_killed() or examination_judgment_killed():
                raise HTTPException(
                    409, "The independent examination judge is disabled by "
                         "the Fly deployment kill switch.")
            _house = _house or load_config(CONFIG_PATH)
            graph_asked = (req.features or {}).get("examination_graph")
            graph_on = (_house.examination_graph.enabled
                        if graph_asked is None else bool(graph_asked))
            if not graph_on:
                raise HTTPException(
                    400, "The independent examination judge requires the "
                         "shadow examination coverage ledger to stay on.")
            if req.kind != "review" or mode != "now" or rounds != 1:
                raise HTTPException(
                    400, "The independent examination judge currently requires "
                         "one review round run right now (not overnight batch).")
            judgment_cfg = _house.examination_graph.judgment
            for role, model_id in (("primary", judgment_cfg.primary_model),
                                   ("escalation", judgment_cfg.escalation_model)):
                if not model_id:
                    continue
                jinfo = lookup(model_id)
                if jinfo is None:
                    raise HTTPException(
                        400, f"Unknown {role} examination model {model_id!r}")
                if not settingslib.get_api_key(jinfo.provider):
                    raise HTTPException(
                        400, f"No API key saved for {jinfo.display} (the {role} "
                             "examination model). Add one in Settings first.")
        # The judge only runs with 2+ rounds, so only vet its model then: it must
        # be a real catalog model and its vendor must have a key on file.
        if req.judge_model and rounds > 1:
            jinfo = lookup(req.judge_model)
            if jinfo is None:
                raise HTTPException(400, f"Unknown judge model {req.judge_model!r}")
            if not settingslib.get_api_key(jinfo.provider):
                raise HTTPException(
                    400, f"No API key saved for {jinfo.display} (the judge "
                         f"model). Add one in Settings first.")
        # The judge gates' models. A pick is applied to the run config whether or
        # not its gate is on, so the model itself is ALWAYS checked against the
        # catalog — an unvetted id must never reach a config. Whether a key is on
        # file only matters if the gate will actually run, and that is the
        # config's answer, not the request's: the panel sends the switch, but a
        # house config could ship a gate on with the switch untouched.
        for _gate, _picked in (("meaning_check", req.meaning_model),
                               ("fix_check", req.fix_model)):
            if not _picked:
                continue
            ginfo = lookup(_picked)
            if ginfo is None:
                raise HTTPException(
                    400, f"Unknown {_gate.replace('_', '-')} model {_picked!r}")
            asked = (req.features or {}).get(_gate)
            if asked is None:
                _house = _house or load_config(CONFIG_PATH)
                gate_on = getattr(_house, _gate).enabled
            else:
                gate_on = bool(asked)
            if gate_on and not settingslib.get_api_key(ginfo.provider):
                raise HTTPException(
                    400, f"No API key saved for {ginfo.display} (the "
                         f"{_gate.replace('_', '-')} model). Add one in "
                         f"Settings first.")
        info = lookup(req.model)
        if info is None:
            raise HTTPException(400, f"Unknown model {req.model!r}")
        if not settingslib.get_api_key(info.provider):
            raise HTTPException(
                400, f"No API key saved for {info.display}. Add one in "
                     f"Settings first.")
        # The chapter-continuity model, if picked: reject an unknown id, and
        # demand a key only when the pass will actually run (its switch, or
        # continuity-only, or a house config that ships it on).
        if req.chapter_continuity_model:
            ccinfo = lookup(req.chapter_continuity_model)
            if ccinfo is None:
                raise HTTPException(
                    400, f"Unknown chapter-continuity model "
                         f"{req.chapter_continuity_model!r}")
            cc_on = (req.features or {}).get("chapter_continuity")
            if cc_on is None:
                _house = _house or load_config(CONFIG_PATH)
                cc_on = _house.chapter_continuity.enabled
            if (bool(cc_on) or req.continuity_only) and \
                    not settingslib.get_api_key(ccinfo.provider):
                raise HTTPException(
                    400, f"No API key saved for {ccinfo.display} (the "
                         f"chapter-continuity model). Add one in Settings first.")
        # The whole-book continuity model, same rule: reject an unknown id, and
        # demand a key only when the read will run (its switch, or continuity-only,
        # or a house config that ships it on).
        if req.continuity_model:
            cinfo = lookup(req.continuity_model)
            if cinfo is None:
                raise HTTPException(
                    400, f"Unknown continuity model {req.continuity_model!r}")
            c_on = (req.features or {}).get("continuity")
            if c_on is None:
                _house = _house or load_config(CONFIG_PATH)
                c_on = _house.continuity.enabled
            if (bool(c_on) or req.continuity_only) and \
                    not settingslib.get_api_key(cinfo.provider):
                raise HTTPException(
                    400, f"No API key saved for {cinfo.display} (the "
                         f"continuity model). Add one in Settings first.")

        # Every id is resolved before any job is enqueued: a 404 halfway
        # through used to leave the earlier files already running — the page
        # said failure, the retry ran them twice, and twice was billed twice.
        sources = {}
        for file_id in req.file_ids:
            source = common.resolve_upload(paths, file_id, owner)
            if source is None:
                raise HTTPException(404, f"Uploaded file {file_id!r} is gone")
            sources[file_id] = source

        # Spend cap (web build only). The estimate for this very submission
        # counts, so one oversized review can't slip past a nearly-spent cap;
        # administrators and the desktop build have no cap and skip all of this.
        if app.state.web:
            cap = common.cap_for(app.state.accounts.get_user(owner))
            if cap is not None:
                spent = common.month_spend(app.state.store, owner)
                totals = common.token_totals(paths, req.file_ids, owner)
                est = 0.0
                if totals:
                    inp, reqs = totals
                    est = estimate_cost(
                        req.model, input_tokens=inp,
                        output_tokens=reqs * common.OUTPUT_TOKEN_GUESS,
                        batch=(mode == "batch")) or 0.0
                # Multi-round review runs the whole review once per round, so the
                # cap must count all of them (the between-round judge is on top of
                # this, so this stays a floor).
                est *= rounds
                if spent + est > cap:
                    raise HTTPException(
                        402, f"This review would put you over your "
                             f"${cap:.2f} monthly limit — you have used "
                             f"${spent:.2f} so far this month. Ask an "
                             f"administrator to raise it.")

        group_id = datetime.now(timezone.utc).strftime("g%Y%m%d%H%M%S")
        created = []
        for file_id in req.file_ids:
            source = sources[file_id]
            job = Job(
                id=batchlib.new_job_id(source.name),
                filename=source.name,
                source_path=str(source),
                model=req.model,
                mode=mode,
                group_id=group_id,
                schedule_at=req.schedule_at if mode == "batch" else None,
                min_confidence=req.min_confidence,
                profile=req.profile,
                variant=req.variant,
                effort=effort,
                glossary_model=req.glossary_model or app.state.settings.glossary_model,
                features=req.features or {},
                category_knobs=req.category_knobs or {},
                rounds=rounds,
                judge_prompt=req.judge_prompt,
                judge_model=req.judge_model or "",
                continuity_prompt=req.continuity_prompt,
                continuity_only=req.continuity_only,
                continuity_model=req.continuity_model or "",
                chapter_continuity_prompt=req.chapter_continuity_prompt,
                chapter_continuity_model=req.chapter_continuity_model or "",
                chapter_continuity_sensitivity=req.chapter_continuity_sensitivity,
                meaning_model=req.meaning_model or "",
                meaning_prompt=req.meaning_prompt,
                fix_model=req.fix_model or "",
                fix_prompt=req.fix_prompt,
                preset=req.preset,
                proposer_restraint=req.proposer_restraint or "restrained",
                judge_harshness=req.judge_harshness or "strict",
                selection=(req.selections or {}).get(file_id) or None,
                created_at=datetime.now(timezone.utc).isoformat(),
                kind=req.kind,
                prep_output=req.prep_output,
                prep_subject=req.prep_subject.strip(),
                prep_title=req.prep_title.strip(),
                prep_author=req.prep_author.strip(),
                owner_id=owner,
            )
            created.append(runner.enqueue(job).to_api())
        return {"jobs": created, "group_id": group_id}

    @app.get("/api/features")
    def features(owner: str = Depends(owner_for)) -> dict:
        """The per-run pass switches to render, each with the value this run
        would take if left untouched. Read off a config built the way a review's
        is — the shipped defaults plus the two settings-backed toggles — so the
        panel shows the real baseline, not the bare code default."""
        cfg = load_config(CONFIG_PATH)
        cfg.comments = app.state.settings.comments
        cfg.report_explanations = app.state.settings.explanations
        # Rounds is not a boolean feature (a count + an editable prompt), so it
        # rides alongside the catalog rather than in it. judge_prompt_default is
        # what the panel pre-fills its textarea placeholder with; an empty submit
        # keeps the engine's built-in default.
        from docproof.verifier import default_judge_prompt
        # Continuity likewise carries an editable prompt (its reader's system
        # prompt) and a run-alone switch, so its default rides alongside the
        # catalog the same way the round judge's does.
        from docproof.continuity import (default_chapter_continuity_prompt,
                                          default_continuity_prompt)
        # Each judge gate is a boolean in the catalog above, but its judge also
        # carries an editable prompt, so those defaults ride alongside the same
        # way the round judge's does.
        from docproof.judges import default_prompt
        # Per-category knob rows: each defined category's stable id, its member
        # types' human names, and the passes / chunk size it would use if left
        # untouched, so the panel renders a row pre-filled with the real baseline
        # (the way rounds rides alongside the feature catalog rather than in it).
        knob_names = {r["key"]: r["name"] for r in list_prompts(
            ERROR_DIR, app.state.paths.prompts, list(cfg.error_type_keys))}
        categories = cfg.category_states()
        for cat in categories:
            cat["names"] = [knob_names.get(k, k) for k in cat["keys"]]
        user = app.state.accounts.get_user(owner) if app.state.web else None
        include_admin = not app.state.web or bool(user and user.is_admin)
        return {"features": featureslib.feature_catalog(
                    cfg, include_admin=include_admin),
                "categories": categories,
                "rounds": {"default": app.state.settings.rounds, "max": 4,
                           "judge_prompt_default": default_judge_prompt()},
                "continuity": {"prompt_default": default_continuity_prompt()},
                "chapter_continuity": {
                    "prompt_default": default_chapter_continuity_prompt()},
                "meaning": {"prompt_default": default_prompt("meaning")},
                "fix": {"prompt_default": default_prompt("fix")}}

    def _card(job: Job) -> dict:
        """A job as the results card needs it: its own fields plus whether the
        "Finish collecting" affordance applies (a failed batch whose vendor
        results are still there to re-collect, cheaply, instead of resubmitting).
        The flag is computed here because it depends on batch state on disk that
        the plain `to_api` can't see."""
        d = job.to_api()
        d["recoverable"] = app.state.runner.can_recover(job)
        # Same reason: whether a finished review still has the record and the
        # manuscript a re-judge needs is a question about files on disk.
        d["rejudgeable"] = app.state.runner.can_rejudge(job)
        return d

    @app.get("/api/jobs")
    def list_jobs(owner: str = Depends(owner_for)) -> dict:
        # Promo has its own panel, so its jobs stay out of the Results list —
        # a promo run is not a reviewed document and its card would not render
        # as one. It is still reachable by id (download, delete) from there.
        scope = owner if app.state.web else None
        return {"jobs": [_card(j) for j in app.state.store.all(scope)
                         if not j.is_promo]}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, owner: str = Depends(owner_for)) -> dict:
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such review")
        return _card(job)

    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: str, owner: str = Depends(owner_for)) -> dict:
        store: JobStore = app.state.store
        runner: JobRunner = app.state.runner
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such review")
        if job.state != "failed":
            raise HTTPException(400, "Only reviews that need attention can be "
                                     "retried.")
        # A retry re-runs from the manuscript, so make sure it is on disk — a
        # wipe (or a restore from the archive) may have taken it; the archive has
        # a copy when the source was kept.
        _ensure_source(job)
        store.update(job_id, state="queued", error=None, done=0)
        return runner.enqueue(store.get(job_id)).to_api()

    @app.post("/api/jobs/{job_id}/recover")
    def recover(job_id: str, owner: str = Depends(owner_for)) -> dict:
        """Finish an overnight review that failed *after* its batch completed.

        The batch is already billed and its results still sit at the vendor, so
        this re-collects them instead of running the review again — unlike
        Retry, which resubmits a fresh batch and pays twice. Only offered for a
        failed review that still has a completed batch waiting to be collected;
        an audit failure is refused here (that one has "Download anyway")."""
        runner: JobRunner = app.state.runner
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such review")
        if job.state != "failed":
            raise HTTPException(400, "Only reviews that need attention can be "
                                     "recovered.")
        updated = runner.recover(job_id)
        if updated is None:
            raise HTTPException(
                400, "There is no completed overnight batch to recover for this "
                     "review — use Retry to run it again.")
        return updated.to_api()

    @app.post("/api/jobs/{job_id}/archive")
    def archive_now(job_id: str, owner: str = Depends(owner_for)) -> dict:
        """Try pushing a job's outputs to the Drive archive again, now.

        For the "Retry archive" affordance on a job whose automatic attempts
        gave up (Drive kept refusing). Resets the attempt count so the try is
        fresh, then archives inline — the same idempotent write the ticker does,
        so a partial earlier attempt is resumed, not duplicated. Best-effort:
        the card comes back with the new archive state either way."""
        store: JobStore = app.state.store
        runner: JobRunner = app.state.runner
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such review")
        if runner.notify_home is None:
            raise HTTPException(400, "The Drive archive is not set up.")
        from ..watch import archive as archivelib
        from ..watch.settings import WatchSettings
        if not archivelib.is_enabled(WatchSettings.load(runner.notify_home)):
            raise HTTPException(
                400, "The Drive archive is switched off. Turn it on under "
                     "DocWatch first.")
        store.update(job_id, archive="", archive_attempts=0, archive_error="")
        runner.archive_job(job_id)
        return _card(store.get(job_id))

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str, owner: str = Depends(owner_for)) -> dict:
        """Abort a job. A queued or scheduled one is pulled back before it
        spends anything; a running one is told to stop, and the worker halts it
        between calls — cancelling every call not yet started, so the abort
        stops the spend, not just the folding.

        An overnight batch already at the vendor is the one thing that can't be
        recalled: it will bill whether or not we collect it, so it is refused
        rather than pretending to stop. A job in its brief file-writing
        (collecting) moment is past the point of stopping too."""
        store: JobStore = app.state.store
        runner: JobRunner = app.state.runner
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such review")

        if job.state == "running":
            # The worker owns the state now; signal it and let it flip the job
            # to cancelled when it next comes up for air. The record still reads
            # "running" for the moment, which is honest — it is, until it stops.
            runner.request_cancel(job_id)
            return store.get(job_id).to_api()

        if job.state not in ("queued", "scheduled"):
            raise HTTPException(
                400, "This one is already past the point of stopping — an "
                     "overnight batch can't be recalled, and a finished job "
                     "has nothing left to cancel.")
        updated = store.update_if(job_id, expect=job.state, state="cancelled",
                                  error=None)
        if updated is None:
            # It moved on in the instant between the check above and this one.
            # If it started running, catch it there instead of failing the call.
            if (moved := store.get(job_id)) is not None and moved.state == "running":
                runner.request_cancel(job_id)
                return moved.to_api()
            raise HTTPException(
                400, "This one just moved on, so there is nothing left to "
                     "cancel.")
        # A cancelled job never runs again, so any checkpoint a failed earlier
        # attempt left behind is just clutter in the job folder now.
        runner.discard_checkpoint(job_id)
        return updated.to_api()

    @app.post("/api/jobs/clear")
    def clear_jobs(owner: str = Depends(owner_for)) -> dict:
        """Clear every finished job from the results list at once — the ones
        that are done, failed, or were cancelled. Active work is left alone.
        Removes each job's produced documents along with its record."""
        store: JobStore = app.state.store
        runner: JobRunner = app.state.runner
        scope = owner if app.state.web else None
        removed = [job.id for job in store.all(scope)
                   if job.state in _TERMINAL and runner.delete_job(job.id)]
        return {"removed": removed}

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str, owner: str = Depends(owner_for)) -> dict:
        """Remove one finished job from the results list, produced documents
        and all. Only a job that has stopped — done, failed, or cancelled — can
        be removed; abort a running one first."""
        runner: JobRunner = app.state.runner
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such review")
        if job.state not in _TERMINAL:
            raise HTTPException(
                400, "This one is still active. Abort it first, then remove it.")
        runner.delete_job(job_id)
        return {"ok": True, "id": job_id}

    @app.post("/api/jobs/{job_id}/rejudge")
    def rejudge(job_id: str, req: RejudgeRequest,
                owner: str = Depends(owner_for)) -> dict:
        """Run the judge gates over a review that already ran.

        No detector call is made — the corrections come off the record the
        original run left — so a manuscript proofread before these gates existed
        can be gated now for the price of the gates alone. The result lands
        beside the original as its own review; the first is never overwritten."""
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such review.")
        if not app.state.runner.can_rejudge(job):
            raise HTTPException(
                409, "This review can't be re-judged — it needs to have "
                     "finished, kept its findings record, and still have the "
                     "manuscript it read.")
        gates = {"meaning_check": req.meaning_check,
                 "fix_check": req.fix_check}
        if not any(gates.values()):
            raise HTTPException(
                400, "Switch on the meaning check, the fix check, or both — a "
                     "re-judge runs the gates and nothing else.")
        models = {}
        for gate, picked in (("meaning_check", req.meaning_model),
                             ("fix_check", req.fix_model)):
            if not (picked and gates[gate]):
                continue
            info = lookup(picked)
            if info is None:
                raise HTTPException(400, f"Unknown model {picked!r}")
            if not settingslib.get_api_key(info.provider):
                raise HTTPException(
                    400, f"No API key saved for {info.display}. Add one in "
                         f"Settings first.")
            models[gate] = picked
        _ensure_source(job)
        updated = app.state.runner.rejudge(
            job_id, gates=gates, models=models,
            prompts={"meaning_check": req.meaning_prompt,
                     "fix_check": req.fix_prompt})
        if updated is None:
            raise HTTPException(409, "This review can't be re-judged.")
        return updated.to_api()

    @app.post("/api/jobs/{job_id}/download-anyway")
    def download_anyway(job_id: str, owner: str = Depends(owner_for)) -> dict:
        """Write the document for a review that failed the reject-all audit.

        The integrity check found text that changed without a tracked change
        around it; this hands the file over anyway, clearly flagged, reusing the
        review already paid for (no new model calls). The user has seen the
        mismatch on the results card before pressing this."""
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No such review.")
        if job.state != "failed":
            raise HTTPException(409, "This review did not fail the audit.")
        # The replay re-ingests the manuscript, so fetch it back from the archive
        # if a wipe took it (the checkpoint the replay also needs is another
        # matter — one that survived only a results wipe, not a whole-volume loss,
        # still has its checkpoint in the job folder).
        _ensure_source(job)
        updated = app.state.runner.download_anyway(job_id)
        if updated is None:
            raise HTTPException(409, "This review can't be written out.")
        return updated.to_api()

    @app.get("/api/jobs/{job_id}/file/{which}")
    def download(job_id: str, which: str, owner: str = Depends(owner_for)):
        """Serve a result over HTTP — the browser build's way of handing a file
        over, and the fallback when the desktop window cannot open one.

        Not gated on a local results folder any more: a job whose folder was
        wiped by a redeploy, or one restored from the archive with no local
        results at all, still serves — `_resolve_result` fetches the file back
        from Drive and re-caches it."""
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No results for this review yet")
        name = _result_name(job, which)
        if name is None:
            raise HTTPException(404, "Unknown file")
        path = _resolve_result(job, name)
        if path is None:
            raise HTTPException(404, f"{name} is missing")
        return FileResponse(path, filename=path.name)

    @app.post("/api/jobs/{job_id}/open/{which}")
    def open_result(job_id: str, which: str, reveal: bool = False,
                    owner: str = Depends(owner_for)) -> dict:
        """Open a result in Word, InDesign, or the Finder.

        This is the desktop window's version of the download route: the file
        already exists in the user's own Documents folder, so the honest thing
        to do with "Open in Word" is open it in Word."""
        job = _owned_job(job_id, owner)
        if job is None or not job.results_dir:
            raise HTTPException(404, "No results for this review yet")
        path = _result_path(job, which)
        if sys.platform != "darwin":
            # Nothing to hand the file to; the page falls back to downloading.
            raise HTTPException(501, "This build can only open files on a Mac.")
        common.open_path(path, reveal=reveal)
        return {"ok": True, "opened": path.name}

    @app.post("/api/jobs/{job_id}/place")
    def place_in_indesign(job_id: str,
                          owner: str = Depends(owner_for)) -> dict:
        """Open the InDesign deliverable.

        The InDesign output is now an IDML that *is* the placed document —
        InDesign turns it into an INDD on open — so there is no separate Place
        step to run. On the desktop this just opens the file in InDesign; the
        web build hands the .idml over as a download instead (this endpoint is
        desktop-only)."""
        job = _owned_job(job_id, owner)
        if job is None or not job.results_dir:
            raise HTTPException(404, "No results for this job yet")
        if not job.is_prep:
            raise HTTPException(
                400, "Only a prepared manuscript has an InDesign file.")
        if job.state != "done":
            raise HTTPException(400, "This one is not finished yet.")
        idml = _result_path(job, "indesign")
        common.open_path(idml, reveal=True)
        return {"ok": True, "filename": idml.name, "path": str(idml)}

    @app.get("/api/prep/reflow-script")
    def reflow_script() -> FileResponse:
        """The one-time InDesign reflow script the designer installs.

        A DocProof IDML opens with the whole book in one primary text frame;
        InDesign only reflows on edit, not on open, so the designer runs this
        once to thread the book across pages. Shipped in the config resources so
        the same file backs the desktop and web builds."""
        from ..settings import resource_root
        path = resource_root() / "config" / "prep" / "reflow.jsx"
        if not path.is_file():
            raise HTTPException(404, "The reflow script is missing from this "
                                     "build.")
        return FileResponse(path, filename="DocProof-reflow.jsx",
                            media_type="text/javascript")

    @app.get("/api/jobs/{job_id}/prep")
    def prep_notes(job_id: str, owner: str = Depends(owner_for)) -> dict:
        """What prep did, read back for the results screen."""
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No results for this job yet")
        path = _resolve_result(job, "prep.json")
        if path is None:
            raise HTTPException(404, "This job has no prep notes")
        data = json.loads(path.read_text("utf-8"))
        # Which deliverables exist, counting the archive so a restored job's
        # buttons still light up (their first click fetches the file back).
        def _present(name: str) -> bool:
            if job.results_dir and (Path(job.results_dir) / name).is_file():
                return True
            return name in job.drive_files
        data["files"] = {kind: _present(name)
                         for kind, name in
                         (("book", f"book_{Path(job.filename).stem}.docx"),
                          ("indesign", f"tagged_{Path(job.filename).stem}.idml"),
                          ("tracked", f"tracked_{Path(job.filename).stem}.docx"))}
        return data

    @app.get("/api/jobs/{job_id}/corrections")
    def corrections_report(job_id: str,
                           owner: str = Depends(owner_for)) -> dict:
        """What the corrections run applied, refused and found unaccounted, read
        back for the results screen. The same corrections.json the report writer
        left behind (docproof.corrections.report)."""
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No results for this job yet")
        path = _resolve_result(job, "corrections.json")
        if path is None:
            raise HTTPException(404, "This job has no corrections report")
        return json.loads(path.read_text("utf-8"))

    def _paragraph_state_for(job_id: str, owner: str, story_id: str,
                             paragraph: int, expect: str = "") -> dict:
        """One paragraph of the corrected file, as the manual editor loads it:
        the text as it is *now* (after any earlier resolutions), its bold and
        italic runs, and whether a section break sits either side. `expect` is
        the text the report recorded — when a resolution since has renumbered
        the paragraphs, the line is relocated by it rather than opened at a
        neighbour's index."""
        from docproof.corrections.idml import read_stories
        from docproof.corrections.resolve import ResolveError, paragraph_state
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No results for this job yet")
        if not job.is_corrections or job.state != "done":
            raise HTTPException(400, "Only a finished corrections job can be "
                                     "edited.")
        corrected = _resolve_result(job, _result_name(job, "corrected"))
        if corrected is None:
            raise HTTPException(404, "The corrected file is missing")
        try:
            return paragraph_state(read_stories(corrected), story_id,
                                   paragraph, expect=expect)
        except ResolveError as e:
            raise HTTPException(409, str(e))

    @app.get("/api/jobs/{job_id}/corrections/paragraph")
    def corrections_paragraph(job_id: str, story_id: str, paragraph: int,
                              owner: str = Depends(owner_for)) -> dict:
        return _paragraph_state_for(job_id, owner, story_id, paragraph)

    @app.post("/api/jobs/{job_id}/corrections/paragraph")
    def corrections_paragraph_post(job_id: str, req: ParagraphRequest,
                                   owner: str = Depends(owner_for)) -> dict:
        # The POST twin exists because `expect` is a whole paragraph — too
        # long to belong in a query string.
        return _paragraph_state_for(job_id, owner, req.story_id,
                                    req.paragraph, req.expect)

    @app.post("/api/jobs/{job_id}/corrections/edit")
    def edit_correction(job_id: str, req: EditRequest,
                        owner: str = Depends(owner_for)) -> dict:
        """A hand edit that answers no flag: the designer touching up one of
        the *applied* corrections (or any line the editor opened) from the
        review screen. Applies exactly what the editor holds, records it as a
        touch-up in the report, and moves no counter — it resolved nothing."""
        from docproof.corrections.resolve import (ResolveError, apply_manual,
                                                  record_touchup)
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No results for this job yet")
        if not job.is_corrections or job.state != "done":
            raise HTTPException(400, "Only a finished corrections job can be "
                                     "edited.")
        with _resolve_lock(job_id):
            json_path = _resolve_result(job, "corrections.json")
            if json_path is None:
                raise HTTPException(404, "This job has no corrections report")
            corrected_name_ = _result_name(job, "corrected")
            corrected = _resolve_result(job, corrected_name_)
            if corrected is None:
                raise HTTPException(404, f"{corrected_name_} is missing")
            try:
                result = apply_manual(corrected, req.manual)
                record_touchup(json_path, result)
            except ResolveError as e:
                raise HTTPException(422, str(e))
            names = [corrected_name_, "corrections.json",
                     "corrections_notes.md"]
            threading.Thread(
                target=lambda: app.state.runner.refresh_archive(job_id, names),
                name=f"docproof-archive-refresh-{job_id}",
                daemon=True).start()
            return {"ok": True, "resolution": result}

    @app.post("/api/jobs/{job_id}/corrections/suggest")
    def suggest_correction(job_id: str, req: SuggestRequest,
                           owner: str = Depends(owner_for)) -> dict:
        """Ask the model for a fix for one flag, shown before it is accepted.

        The flag's stored advice — or, when it has none, an explicit "you
        decide" from the designer — is adjudicated into an exact edit and
        dry-run against the corrected book; the result lands in the flag's
        options as a marked, clickable placement. Nothing is applied here: the
        designer reads the highlighted preview and accepts it with the same
        click as any other placement (or edits it first). The small model
        spend is recorded; a decline comes back as the sentence to show."""
        from docproof.corrections.resolve import (ResolveError,
                                                  materialize_suggestion,
                                                  _write_updated)
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No results for this job yet")
        if not job.is_corrections or job.state != "done":
            raise HTTPException(400, "Only a finished corrections job has "
                                     "flags to resolve.")
        with _resolve_lock(job_id):
            json_path = _resolve_result(job, "corrections.json")
            if json_path is None:
                raise HTTPException(404, "This job has no corrections report")
            corrected = _resolve_result(job, _result_name(job, "corrected"))
            if corrected is None:
                raise HTTPException(404, "The corrected file is missing")
            payload = json.loads(json_path.read_text("utf-8"))
            item = next((q for q in payload.get("queue") or []
                         if q.get("id") == req.item_id), None)
            if item is None:
                raise HTTPException(404, "This flag is not in the report.")
            if item.get("resolved"):
                raise HTTPException(409, "This flag was already resolved.")
            provider, model = _build_resolve_provider()
            usage = Usage()
            try:
                option = materialize_suggestion(item, corrected, provider,
                                                model=model, usage=usage)
            except ResolveError as e:
                _record_extract_spend(app, owner, model, usage,
                                      filename="corrections review")
                raise HTTPException(422, str(e))
            _record_extract_spend(app, owner, model, usage,
                                  filename="corrections review")
            options = item.setdefault("options", [])
            option["id"] = (f"{item['id']}-s"
                            f"{sum(1 for o in options if o.get('suggested')) + 1}")
            if item.get("page_label"):
                option["page_label"] = item["page_label"]
            options.append(option)
            _write_updated(Path(json_path), payload)
            return {"item": item}

    @app.post("/api/jobs/{job_id}/corrections/resolve")
    def resolve_correction(job_id: str, req: ResolveRequest,
                           owner: str = Depends(owner_for)) -> dict:
        """Resolve one flagged correction from the review screen, editing the
        corrected IDML in place.

        Two shapes, exactly one per call. A clicked *option* is a placement the
        report already materialized — the exact span and the exact replacement —
        so applying it is deterministic and free. A typed *answer* is the
        designer's decision in plain words; a model transcribes it into an
        exact edit against the book's own text (a small spend, recorded), and
        the edit applies through the same anchoring machinery a run uses — or
        is declined back with the reason, nothing written. Either way the
        report is updated beside the file, so the change log and the download
        never disagree."""
        from docproof.corrections.resolve import (ResolveError,
                                                  adjudicate_instruction,
                                                  apply_edit_to_corrected,
                                                  apply_manual, apply_option,
                                                  queue_counts,
                                                  record_resolution,
                                                  reopen_dismissed,
                                                  suggestion_instruction)
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No results for this job yet")
        if not job.is_corrections:
            raise HTTPException(400, "Only a corrections job has flags to "
                                     "resolve.")
        if job.state != "done":
            raise HTTPException(409, "This one is not finished yet.")
        asked = [bool(req.option_id), bool(req.text.strip()),
                 req.suggestion, req.dismiss, req.reopen,
                 req.manual is not None]
        if sum(asked) != 1:
            raise HTTPException(400, "Pick one of the offered placements, type "
                                     "what to do, apply the suggestion, edit "
                                     "the line, or ignore it — one action per "
                                     "call.")
        with _resolve_lock(job_id):
            json_path = _resolve_result(job, "corrections.json")
            if json_path is None:
                raise HTTPException(404, "This job has no corrections report")
            corrected_name_ = _result_name(job, "corrected")
            corrected = _resolve_result(job, corrected_name_)
            if corrected is None:
                raise HTTPException(404, f"{corrected_name_} is missing")
            payload = json.loads(json_path.read_text("utf-8"))
            item = next((q for q in payload.get("queue") or []
                         if q.get("id") == req.item_id), None)
            if item is None:
                raise HTTPException(
                    404, "This flag is not in the report — it may predate "
                         "in-app resolutions; re-run the corrections to get "
                         "them.")
            if item.get("resolved") and not req.reopen:
                raise HTTPException(409, "This flag was already resolved.")
            try:
                if req.reopen:
                    updated = reopen_dismissed(json_path, req.item_id)
                elif req.dismiss:
                    # Nothing is written; the flag is recorded as deliberately
                    # set aside, and can be put back with `reopen`.
                    updated = record_resolution(json_path, req.item_id,
                                                {"kind": "dismissed"})
                elif req.option_id:
                    option = next((o for o in item.get("options") or []
                                   if o.get("id") == req.option_id), None)
                    if option is None:
                        raise HTTPException(404, "No such placement for this "
                                                 "flag.")
                    result = apply_option(corrected, option)
                    # Accepting a placement the model suggested is recorded as
                    # a suggestion, so the log says whose call it carried out.
                    resolution = {"kind": ("suggestion"
                                           if option.get("suggested")
                                           else "option"),
                                  "option_id": req.option_id,
                                  "edit_id": option.get("edit_id", ""),
                                  "note": option.get("note", ""),
                                  **result}
                    updated = record_resolution(json_path, req.item_id,
                                                resolution)
                elif req.manual is not None:
                    # The designer edited the line themselves — deterministic,
                    # free, and exactly what the editor showed.
                    result = apply_manual(corrected, req.manual)
                    resolution = {"kind": "manual", **result}
                    updated = record_resolution(json_path, req.item_id,
                                                resolution)
                else:
                    # A typed answer, or the one-click form of it: carrying out
                    # the advice a model pass already wrote on this flag.
                    text = (suggestion_instruction(item) if req.suggestion
                            else req.text.strip())
                    from docproof.corrections.idml import read_stories
                    provider, model = _build_resolve_provider()
                    usage = Usage()
                    edit, note = adjudicate_instruction(
                        item, text, provider, model=model, usage=usage,
                        stories=read_stories(corrected))
                    _record_extract_spend(app, owner, model, usage,
                                          filename="corrections review")
                    if edit is None:
                        # An answered decline, not a failure: nothing was
                        # written, and the reason is the designer's to act on.
                        raise HTTPException(422, f"Not applied — {note}")
                    result = apply_edit_to_corrected(corrected, edit)
                    resolution = {"kind": ("suggestion" if req.suggestion
                                           else "typed"),
                                  "text": text, "note": note, **result}
                    updated = record_resolution(json_path, req.item_id,
                                                resolution)
            except ResolveError as e:
                raise HTTPException(422, str(e))
            counts = queue_counts(updated)
            app.state.store.update(
                job_id, applied=counts["applied"], flags=counts["flags"],
                unresolved=counts["unresolved"])
            # The archive holds the pre-resolution copies; refresh them in the
            # background so a wiped volume never serves a stale deliverable
            # back. Best-effort, off the request thread. A dismiss or reopen
            # touched only the report, so only the report is re-pushed.
            names = ["corrections.json", "corrections_notes.md"]
            if not (req.dismiss or req.reopen):
                names.insert(0, corrected_name_)
            threading.Thread(
                target=lambda: app.state.runner.refresh_archive(job_id, names),
                name=f"docproof-archive-refresh-{job_id}", daemon=True).start()
            fresh = next((q for q in updated.get("queue") or []
                          if q.get("id") == req.item_id), None)
            return {"item": fresh, "counts": counts}

    @app.post("/api/corrections/extract-docx")
    async def extract_corrections_docx(
            file: UploadFile, owner: str = Depends(owner_for)) -> dict:
        """Read a corrections Word file into a draft edit list.

        Two kinds of file arrive here, and this reads both. A *redlined* one is
        deterministic — the tracked changes ARE the before/after, so no model
        and no cost. A file that is simply a *list* of corrections ("p. 12,
        change 'teh' to 'the'") carries no redline at all: rather than refusing
        it, its text goes to the same model that reads a pasted list, which is
        what a person dropping it plainly meant. Either way the result fills
        the corrections textarea for a person to review; nothing is applied
        here."""
        from docproof.corrections.from_word import edits_from_docx, text_from_docx
        name = Path(file.filename or "corrections.docx").name
        if not name.lower().endswith(".docx"):
            raise HTTPException(
                400, "Upload a Word (.docx) file — either tracked changes or a "
                     "typed list of corrections.")
        data = await file.read()
        tmp = Path(tempfile.mkstemp(suffix=".docx")[1])
        try:
            tmp.write_bytes(data)
            try:
                result = edits_from_docx(tmp)
                text = "" if (result.edits or result.issues) else text_from_docx(tmp)
            except Exception as e:         # noqa: BLE001 - a bad/again unreadable upload
                raise HTTPException(400, f"Could not read {name}: {e}")
        finally:
            tmp.unlink(missing_ok=True)
        if not result.edits and not result.issues:
            if not text.strip():
                raise HTTPException(
                    400, f"{name} has no tracked changes and no text to read. "
                         "Either mark the corrections with Track Changes on, or "
                         "type them out as a list, and upload it again.")
            # The model read, off the event loop: it is a network call of a few
            # seconds, and this route is async.
            return await run_in_threadpool(_extract_with_model, app, owner, text)
        return _extract_response(result)

    @app.post("/api/corrections/extract-list")
    def extract_corrections_list(
            req: ExtractListRequest, owner: str = Depends(owner_for)) -> dict:
        """Read a free-form corrections list (prose, a pasted table) into a draft
        edit list with the house model. The corrections job itself stays free and
        deterministic — this is the one place a model touches the flow, and its
        output is reviewed by a person before anything is applied."""
        if not req.text.strip():
            raise HTTPException(400, "Paste the corrections list to read.")
        return _extract_with_model(app, owner, req.text)

    async def _read_proof_or_400(file: UploadFile):
        """The comments and page texts read off an uploaded PDF proof, or a 400 the
        caller surfaces: a wrong file type, an unreadable file, or a proof with no
        comment layer at all (flattened or scanned). Deterministic — no model and
        no cost. Shared by the batched panel read and the server-side extract
        fallback."""
        from docproof.corrections.from_pdf import read_pdf
        name = Path(file.filename or "proof.pdf").name
        if not name.lower().endswith(".pdf"):
            raise HTTPException(400, "Upload a PDF proof with comments.")
        data = await file.read()
        tmp = Path(tempfile.mkstemp(suffix=".pdf")[1])
        try:
            tmp.write_bytes(data)
            try:
                proof = read_pdf(tmp)
            except Exception as e:         # noqa: BLE001 - an unreadable upload
                raise HTTPException(400, f"Could not read {name}: {e}")
        finally:
            tmp.unlink(missing_ok=True)
        comments = list(proof.comments)
        if not comments:
            raise HTTPException(
                400, f"No comments found in {name}. The corrections have to be "
                     "PDF comments or highlights — a flattened or scanned proof "
                     "has no comment layer to read.")
        return proof

    def _book_pages_for(file_id: str, owner: str, page_texts: list[str],
                        wanted: set[int]) -> dict[int, str]:
        """The book's own text for each page a comment sits on, read out of the
        designer's staged IDML.

        This is what the extractor should be quoting from. Without it the model is
        shown only the PDF's rendering of a page and asked to produce an anchor that
        matches the IDML exactly — a document it has never seen — which is where the
        unmatchable anchors came from. Best-effort: no file id, an unreadable
        export, or a page the map cannot place simply means that page's comments are
        read the old way."""
        if not file_id or not page_texts:
            return {}
        path = common.resolve_upload(app.state.paths, file_id, owner)
        if path is None or path.suffix.lower() != ".idml":
            return {}
        try:
            from docproof.corrections.idml import read_stories
            from docproof.corrections.pagemap import build_page_map, page_book_text
            stories = read_stories(path)
            page_map = build_page_map(stories, page_texts)
            return {p: page_book_text(stories, page_map, p) for p in sorted(wanted)
                    if page_map.knows(p)}
        except Exception:              # noqa: BLE001 - a nicety, never the job
            log.warning("Could not read the book text for the corrections read",
                        exc_info=True)
            return {}

    @app.post("/api/corrections/read-pdf")
    async def read_corrections_pdf(
            file: UploadFile, file_id: str = Form(""),
            owner: str = Depends(owner_for)) -> dict:
        """Read a marked-up PDF proof's comments and hand back the batches to
        turn into edits — deterministic, instant, no model and no cost.

        This is the first half of the panel's read: pypdf pulls every comment and
        the manuscript line it points at, and the comments are split into bounded
        batches. The panel then reads each batch into edits (via `extract-list`),
        showing the count climb as it goes, so a proof with hundreds of marks
        fills in steadily instead of hanging on one long, silent — and, past the
        model's output ceiling, truncating — call."""
        from docproof.corrections.from_pdf import comments_source_batches
        from docproof.corrections.instructions import edits_from_comments
        proof = await _read_proof_or_400(file)
        comments = list(proof.comments)
        # Most of a proof's marks are a function of the span they sit on —
        # "Lowercase", "Replace comma with period", a bare "wouldn't". Those are
        # resolved here, exactly, for nothing: the model is only asked about the
        # notes no rule is certain of, so it neither invents a `find` for the easy
        # ones nor gets billed for them.
        # The book's own words for every page a comment sits on. The model quotes
        # the file it is editing instead of a rendering of it — and the rules count
        # a repeated word's copies over the same text, because that ordinal is read
        # back after `apply` has narrowed to the page's run of the book (a running
        # head and a hyphenated word are copies the book does not have). The proof's
        # own page texts ride along too: the mark's offset was measured against them,
        # so they are where `_ordinal` locates which copy the mark sits on.
        book = _book_pages_for(file_id, owner, list(proof.page_texts),
                              {c.page for c in comments})
        resolved, unresolved = edits_from_comments(
            comments, pages=book, pdf_pages=list(proof.page_texts))
        batches = comments_source_batches(unresolved, CORRECTIONS_PDF_BATCH_SIZE,
                                          pages=book)
        # The comments themselves ride back too, so the panel can carry them into
        # the job and the finished change log can account for every one — including
        # any the model turns into no edit. The page texts ride with them for the
        # page map: they are what turn "marked on page 49" into a run of book text,
        # which is the difference between an edit that lands and one that can only
        # be flagged. A novel's worth is a few hundred kilobytes of JSON — large
        # for a payload, small next to the PDF it was read from.
        # `offset` rides along so the job can settle which copy of a repeated word a
        # model-read edit meant — the ordinal the rules already carry, filled in for
        # the model's edits at apply time from the mark's own position.
        items = [{"id": c.id, "page": c.page, "kind": c.kind,
                  "instruction": c.instruction, "anchor": c.anchor,
                  "offset": c.offset}
                 for c in comments]
        return {"count": len(comments), "batches": batches, "comments": items,
                "pages": list(proof.page_texts),
                "resolved": resolved, "resolved_count": len(resolved)}

    @app.post("/api/corrections/extract-pdf")
    async def extract_corrections_pdf(
            file: UploadFile, file_id: str = Form(""),
            owner: str = Depends(owner_for)) -> dict:
        """Read a commented PDF proof into a draft edit list, in one request.

        Deterministic reading (pypdf pulls each comment and the manuscript text
        it points at); Luna then turns each pair into an exact edit, reviewed by
        a person before it is applied. A flattened or scanned proof carries no
        comment layer and is turned away rather than guessed at.

        The comments are read in bounded batches — one model call each — so a
        proof with hundreds of marks cannot overrun the output ceiling and
        truncate. The panel prefers `read-pdf` + `extract-list` for live progress;
        this stays as the single-request path for other callers."""
        from docproof.corrections.from_pdf import comments_source_batches
        from docproof.corrections.instructions import edits_from_comments
        proof = await _read_proof_or_400(file)
        comments = list(proof.comments)
        book = _book_pages_for(file_id, owner, list(proof.page_texts),
                              {c.page for c in comments})
        resolved, unresolved = edits_from_comments(
            comments, pages=book, pdf_pages=list(proof.page_texts))
        batches = comments_source_batches(unresolved, CORRECTIONS_PDF_BATCH_SIZE,
                                          pages=book)
        response = _extract_batches_with_model(app, owner, batches)
        # The rule-resolved edits lead the list; the model's follow. Both are the
        # same shape, and a person reviews the whole list before it is applied.
        if resolved:
            merged = resolved + json.loads(response["json"])
            response["json"] = json.dumps(merged, indent=2, ensure_ascii=False)
            response["count"] = len(merged)
        response["comments"] = [
            {"id": c.id, "page": c.page, "kind": c.kind,
             "instruction": c.instruction, "anchor": c.anchor,
             "offset": c.offset} for c in comments]
        response["pages"] = list(proof.page_texts)
        return response

    @app.get("/api/usage")
    def usage(owner: str = Depends(owner_for)) -> dict:
        """Tokens, calls and estimated spend, from every source on the one card.

        Both job stores count — the app's and the watcher's — and within each,
        live jobs and the ledger snapshots of cleared ones alike, because a
        cleared job's cost still came off the card. On the desktop that is the
        whole machine. On the web it is scoped: a regular user sees their own app
        jobs, and an administrator sees the full bill — every user's jobs and the
        watcher's — because the watcher belongs to the organisation, not to any
        one user."""
        web = app.state.web
        user = app.state.accounts.get_user(owner) if web else None
        if web and not (user and user.is_admin):
            return build_usage(common.store_spend(app.state.store, owner),
                               read_usage)
        rows = (common.store_spend(app.state.store)
                + common.watch_spend(app.state.watch))
        return build_usage(rows, read_usage)

    @app.get("/api/jobs/{job_id}/report")
    def report(job_id: str, owner: str = Depends(owner_for)) -> dict:
        """The findings, read back as prose rather than as a record."""
        job = _owned_job(job_id, owner)
        if job is None:
            raise HTTPException(404, "No results for this review yet")
        path = _resolve_result(job, "findings.json")
        if path is None:
            raise HTTPException(404, "This review has no findings file")
        cfg = load_config(CONFIG_PATH)
        rows = list_prompts(ERROR_DIR, app.state.paths.prompts,
                            list(cfg.error_type_keys))
        return build_report(path, {r["key"]: r["name"] for r in rows})

    @app.post("/api/tick")
    def tick(owner: str = Depends(owner_for)) -> dict:
        """Advance scheduled and in-flight work now instead of waiting for the
        next timer. The Jobs screen calls this when the user opens it.

        The tick itself advances everyone's batch work — it is the one shared
        clock — but the list it returns is only the caller's own, the same as
        /api/jobs."""
        app.state.runner.tick_once()
        scope = owner if app.state.web else None
        # Built with `_card`, exactly as /api/jobs is: the panel renders from
        # whichever of the two answered last, so a flag on one and not the other
        # makes a button appear and vanish depending on how the screen was
        # reached. This is the endpoint the Results screen opens on.
        return {"jobs": [_card(j) for j in app.state.store.all(scope)
                         if not j.is_promo]}
