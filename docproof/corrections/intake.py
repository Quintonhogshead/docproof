"""Reading an author's corrections submission into the strings a corrections Job
takes — headless, without the FastAPI routes.

The corrections panel reads a submission in three shapes, each through its own
endpoint: a marked-up PDF proof (`read-pdf` / `extract-pdf`), a Word file that is
either a redline or a typed list (`extract-docx`), and pasted free text
(`extract-list`). The DocWatch automation has the same three shapes arriving in a
folder and no browser to drive, so this module is those endpoints' bodies with the
HTTP taken off: the same readers, the same rules-first-then-model split for a
proof, the same fallbacks, the same output shapes. Anything the routes do that
this does not — recording the spend to a ledger, checking a file's ownership — is
the caller's, not the read's.

The output is the three strings `Job.corrections`, `Job.corrections_comments` and
`Job.corrections_pages` hold, already in the exact shape `_create_corrections`
validates: a JSON list of edit rows, a JSON list of the reviewer's comments (so
the change log can account for every one, including those no edit was made for),
and a JSON list of the proof's page texts (what the page map narrows a page-cited
edit with). `parse_edits` is the validator throughout, so the `corrections`
string handed back re-parses to exactly `edits` entries.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..models import Usage
from .parse import ParseResult, parse_edits

log = logging.getLogger("docproof.corrections.intake")

# How many of a proof's comments go to the model per call — the same bound the
# panel uses (`app.routes.jobs.CORRECTIONS_PDF_BATCH_SIZE`). One call for hundreds
# of marks overruns the output ceiling and truncates to nothing.
DEFAULT_BATCH_SIZE = 40

SOURCE_PDF = "pdf"
SOURCE_DOCX_TRACKED = "docx-tracked"
SOURCE_DOCX_LIST = "docx-list"
SOURCE_TEXT = "text"

# What the caller's `progress(stage, done, total)` is told is happening.
STAGE_READ = "read"           # reading the file itself (deterministic)
STAGE_EXTRACT = "extract"     # one model call per batch


class IntakeError(RuntimeError):
    """The submission could not be turned into a corrections list at all — the
    wrong kind of file, a proof with no comment layer, a typed list with no
    model to read it, or a model call that failed. Per-entry problems are not
    errors: they come back as `IntakeResult.issues`."""


@dataclass(frozen=True)
class IntakeResult:
    """What one submission yields, shaped for a corrections Job."""
    corrections: str        # JSON list text, exactly what Job.corrections takes
    comments: str           # JSON list text for Job.corrections_comments ("" when none)
    pages: str              # JSON list of page texts for Job.corrections_pages ("" when none)
    edits: int              # number of edits in `corrections`
    issues: tuple[str, ...] # per-entry problems, human-readable
    source_kind: str        # "pdf" | "docx-tracked" | "docx-list" | "text"
    comments_total: int     # reviewer comments read from a PDF, else 0
    page_texts_from: str    # "proof" (the marked PDF) | "pdf" (a separate proof PDF) | ""


# --- the shared serializer ------------------------------------------------------

def edits_to_corrections_json(edits) -> str:
    """Serialize an extracted `Edit` list back into the corrections JSON the
    textarea holds (and a Job takes), so a person reviews and edits it before
    anything is applied. Fields at their default are left out so the result
    reads clean."""
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


def _rows_json(rows: list[dict]) -> str:
    return json.dumps(rows, indent=2, ensure_ascii=False)


def _issue_lines(result: ParseResult) -> list[str]:
    """A parse result's issues as the sentences a log or an email can carry."""
    return [f"entry {i.index + 1}: {i.reason}" for i in result.issues]


def _finish(rows: list[dict], *, comments: str, pages: str, issues: list[str],
            source_kind: str, comments_total: int, page_texts_from: str
            ) -> IntakeResult:
    """Serialize the rows and prove they read back. `parse_edits` is the one
    validator the Job applies, so the count reported here is the count the Job
    will see; a row that would not survive it is reported as an issue rather
    than counted."""
    corrections = _rows_json(rows)
    parsed = parse_edits(corrections)
    extra = _issue_lines(parsed)
    return IntakeResult(
        corrections=corrections, comments=comments, pages=pages,
        edits=len(parsed.edits), issues=tuple(issues + extra),
        source_kind=source_kind, comments_total=comments_total,
        page_texts_from=page_texts_from)


# --- the proof's pages against the book -----------------------------------------

def book_pages_for(idml_path: str | Path | None, page_texts: list[str],
                   wanted: set[int]) -> dict[int, str]:
    """The book's own text for each page a comment sits on, read out of the
    designer's IDML — what the extractor should be quoting from, so its anchors
    match the file being edited rather than the PDF's rendering of it.

    Best-effort, exactly as the routes' `_book_pages_for`: no IDML, a file that
    is not one, an unreadable export, or a page the map cannot place means that
    page's comments are read the old way, never a failed read."""
    if not idml_path or not page_texts:
        return {}
    path = Path(idml_path)
    if not path.is_file() or path.suffix.lower() != ".idml":
        return {}
    try:
        from .idml import read_stories
        from .pagemap import build_page_map, page_book_text
        stories = read_stories(path)
        page_map = build_page_map(stories, list(page_texts))
        return {p: page_book_text(stories, page_map, p) for p in sorted(wanted)
                if page_map.knows(p)}
    except Exception:              # noqa: BLE001 - a nicety, never the job
        log.warning("Could not read the book text for the corrections read",
                    exc_info=True)
        return {}


def _proof_pages_json(proof_pdf: str | Path | None, issues: list[str]) -> str:
    """The page texts of a separate proof PDF, for a submission that is not
    itself a PDF, so its page-cited edits can be page-narrowed. Best-effort: the
    pages only narrow where an edit may land, so an unreadable proof costs
    precision, never a correction — it is noted and the read goes on."""
    if not proof_pdf:
        return ""
    try:
        from .from_pdf import read_pdf
        proof = read_pdf(proof_pdf)
    except Exception as e:         # noqa: BLE001 - an unreadable proof
        log.warning("Could not read the proof PDF for page texts", exc_info=True)
        issues.append(f"the proof PDF {Path(proof_pdf).name} could not be read "
                      f"for its page texts: {e}")
        return ""
    return json.dumps(list(proof.page_texts), ensure_ascii=False)


def _comment_items(comments) -> list[dict]:
    """The reviewer's comments as the Job carries them — the shape the routes
    hand the panel, so the finished change log can account for every one."""
    return [{"id": c.id, "page": c.page, "kind": c.kind,
             "instruction": c.instruction, "anchor": c.anchor,
             "offset": c.offset, "replies": list(c.replies)}
            for c in comments]


def _extract(text: str, provider, model: str, usage: Usage) -> ParseResult:
    from .extract import ExtractionError, extract_edits
    try:
        return extract_edits(text, provider, model=model, usage=usage)
    except ExtractionError as e:
        raise IntakeError(f"The model could not read that: {e}") from e


# --- the three readers ------------------------------------------------------------

def read_pdf_submission(pdf: str | Path, *, idml_path: str | Path | None = None,
                        provider=None, model: str = "", usage: Usage | None = None,
                        batch_size: int = DEFAULT_BATCH_SIZE,
                        progress: Callable[[str, int, int], None] | None = None
                        ) -> IntakeResult:
    """A marked-up PDF proof, read the way `read-pdf` + `extract-list` (or the
    one-shot `extract-pdf`) read it.

    pypdf pulls every comment and the line it points at; the rules resolve the
    marks that are a function of their span ("Lowercase", "Replace comma with
    period") for nothing; the rest go to the model in bounded batches, shown the
    book's own text for their pages when `idml_path` is given. The rule-resolved
    rows lead the list and the model's follow. With no `provider` only the
    rule-resolved rows come back — the unresolved comments still ride in
    `comments`, so the report accounts for them as not extracted.

    A proof with no comment layer (flattened, or scanned) raises `IntakeError`."""
    from .from_pdf import comments_source_batches, read_pdf
    from .instructions import edits_from_comments
    usage = usage if usage is not None else Usage()
    path = Path(pdf)
    name = path.name
    if path.suffix.lower() != ".pdf":
        raise IntakeError("Upload a PDF proof with comments.")
    try:
        proof = read_pdf(path)
    except Exception as e:         # noqa: BLE001 - an unreadable file
        raise IntakeError(f"Could not read {name}: {e}") from e
    comments = list(proof.comments)
    if not comments:
        raise IntakeError(
            f"No comments found in {name}. The corrections have to be PDF "
            "comments or highlights — a flattened or scanned proof has no "
            "comment layer to read.")
    if progress is not None:
        progress(STAGE_READ, 1, 1)

    page_texts = list(proof.page_texts)
    book = book_pages_for(idml_path, page_texts, {c.page for c in comments})
    resolved, unresolved = edits_from_comments(
        comments, pages=book, pdf_pages=page_texts)
    batches = comments_source_batches(unresolved, batch_size, pages=book)

    issues: list[str] = []
    rows = list(resolved)
    if batches:
        if provider is None:
            issues.append(
                f"{len(unresolved)} comment(s) need a model to read and none "
                "was given; they are carried as comments no edit was made for")
        else:
            from .extract import ExtractionError, extract_edits_batched

            def _tick(done: int, total: int, _cumulative: ParseResult) -> None:
                if progress is not None:
                    progress(STAGE_EXTRACT, done, total)

            try:
                result = extract_edits_batched(
                    batches, provider, model=model, usage=usage, progress=_tick)
            except ExtractionError as e:
                raise IntakeError(f"The model could not read that: {e}") from e
            issues += _issue_lines(result)
            # The same merge the route makes: both halves are the same row shape,
            # rules first, the model's after.
            rows += json.loads(edits_to_corrections_json(result.edits))
    return _finish(
        rows,
        comments=json.dumps(_comment_items(comments), ensure_ascii=False),
        pages=json.dumps(page_texts, ensure_ascii=False),
        issues=issues, source_kind=SOURCE_PDF, comments_total=len(comments),
        page_texts_from="proof")


def read_docx_submission(docx: str | Path, *, provider=None, model: str = "",
                         usage: Usage | None = None,
                         proof_pdf: str | Path | None = None) -> IntakeResult:
    """A corrections Word file, read the way `extract-docx` reads it.

    A redline is deterministic — the tracked changes ARE the before/after, so no
    model and no cost. A file that is only a typed list carries no redline; its
    text goes to the same model that reads a pasted list. With no `provider`, a
    list raises `IntakeError` rather than guessing."""
    from .from_word import edits_from_docx, text_from_docx
    usage = usage if usage is not None else Usage()
    path = Path(docx)
    name = path.name
    if path.suffix.lower() != ".docx":
        raise IntakeError(
            "Upload a Word (.docx) file — either tracked changes or a typed "
            "list of corrections.")
    try:
        result = edits_from_docx(path)
        text = "" if (result.edits or result.issues) else text_from_docx(path)
    except Exception as e:         # noqa: BLE001 - a bad/unreadable file
        raise IntakeError(f"Could not read {name}: {e}") from e

    issues: list[str] = []
    if result.edits or result.issues:
        issues += _issue_lines(result)
        pages = _proof_pages_json(proof_pdf, issues)
        return _finish(json.loads(edits_to_corrections_json(result.edits)),
                       comments="", pages=pages, issues=issues,
                       source_kind=SOURCE_DOCX_TRACKED, comments_total=0,
                       page_texts_from="pdf" if pages else "")
    if not text.strip():
        raise IntakeError(
            f"{name} has no tracked changes and no text to read. Either mark "
            "the corrections with Track Changes on, or type them out as a list, "
            "and upload it again.")
    if provider is None:
        raise IntakeError(
            f"{name} has no tracked changes, so it is a typed list — reading it "
            "needs a model to read a typed list, and none was given.")
    extracted = _extract(text, provider, model, usage)
    issues += _issue_lines(extracted)
    pages = _proof_pages_json(proof_pdf, issues)
    return _finish(json.loads(edits_to_corrections_json(extracted.edits)),
                   comments="", pages=pages, issues=issues,
                   source_kind=SOURCE_DOCX_LIST, comments_total=0,
                   page_texts_from="pdf" if pages else "")


def read_text(text: str, *, provider=None, model: str = "",
              usage: Usage | None = None,
              proof_pdf: str | Path | None = None) -> IntakeResult:
    """A free-form corrections list (prose, a pasted table), read the way
    `extract-list` reads it — with the model, which is the one place a model
    touches the corrections flow. With no `provider` this raises `IntakeError`:
    a typed list cannot be read deterministically."""
    usage = usage if usage is not None else Usage()
    if not text.strip():
        raise IntakeError("Paste the corrections list to read.")
    if provider is None:
        raise IntakeError("A free-text corrections list needs a model to read "
                          "a typed list, and none was given.")
    extracted = _extract(text, provider, model, usage)
    issues = _issue_lines(extracted)
    pages = _proof_pages_json(proof_pdf, issues)
    return _finish(json.loads(edits_to_corrections_json(extracted.edits)),
                   comments="", pages=pages, issues=issues,
                   source_kind=SOURCE_TEXT, comments_total=0,
                   page_texts_from="pdf" if pages else "")


# --- the front door ---------------------------------------------------------------

def _as_existing_path(source: str | Path) -> Path | None:
    """`source` as a file on disk, or None when it is the text itself. A body of
    free text is not a path — and may be too long to even ask the filesystem
    about — so the probe is guarded rather than trusted."""
    if isinstance(source, Path):
        return source if source.is_file() else None
    if not source or "\n" in source or len(source) > 1024:
        return None
    try:
        path = Path(source)
        return path if path.is_file() else None
    except (OSError, ValueError):
        return None


def read_submission(source: str | Path, *, idml_path: str | Path | None,
                    provider, model: str, usage,
                    proof_pdf: str | Path | None = None,
                    batch_size: int = DEFAULT_BATCH_SIZE,
                    progress: Callable[[str, int, int], None] | None = None
                    ) -> IntakeResult:
    """Read one corrections submission of whatever kind into a Job's strings.

    `source` is a path to a .pdf or .docx (or a text file), or — when it is not
    an existing path — the free text of the list itself. `idml_path` is the
    designer's export the corrections will be applied to; for a PDF it lets the
    model quote anchors from the book's own text. `proof_pdf` is a separate proof
    PDF whose page texts supply `pages` for a .docx or text submission. `usage`
    accrues the model's tokens (a fresh `Usage` when None); `progress(stage,
    done, total)` is told about each batch of a PDF read."""
    usage = usage if usage is not None else Usage()
    path = _as_existing_path(source)
    if path is None:
        return read_text(str(source), provider=provider, model=model, usage=usage,
                         proof_pdf=proof_pdf)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return read_pdf_submission(path, idml_path=idml_path, provider=provider,
                                   model=model, usage=usage,
                                   batch_size=batch_size, progress=progress)
    if suffix == ".docx":
        return read_docx_submission(path, provider=provider, model=model,
                                    usage=usage, proof_pdf=proof_pdf)
    if suffix in (".txt", ".md", ".text", ""):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise IntakeError(f"Could not read {path.name}: {e}") from e
        return read_text(text, provider=provider, model=model, usage=usage,
                         proof_pdf=proof_pdf)
    raise IntakeError(
        f"{path.name} is not a corrections source this reads — send a marked-up "
        "PDF proof, a Word (.docx) file, or the list as text.")
