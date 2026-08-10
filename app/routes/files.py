"""Staging uploads, and what the drop zone may be handed."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, UploadFile

from docproof import prep as preplib
from docproof import promo as promolib
from docproof.config import load_config
from docproof.formats import SUFFIXES, describe, get_format
from docproof.ingest import IngestError
from docproof.pipeline import chunk_outline, prepare
from docproof.prep import convert as prep_convert
from docproof.prep.styles import StyleSheetError

from ..auth import owner_for
from ..settings import CONFIG_PATH, ERROR_DIR, Paths


def register(app: FastAPI) -> None:

    @app.post("/api/files")
    async def upload(files: list[UploadFile],
                     owner: str = Depends(owner_for)) -> dict:
        """Stage documents and preflight them immediately, so a file docproof
        refuses to touch says so at drop time rather than at 11pm.

        Both pipelines are preflighted, because the user has not chosen yet:
        an .idml can be reviewed but never prepped, a manuscript with tracked
        changes in it is refused by both, and a .doc can be prepped only once
        LibreOffice has turned it into a .docx. All of that is local parsing,
        so answering both questions costs one extra read of the file.

        Staging lives under uploads/<owner>, so the id handed back is only ever
        resolvable by the user who uploaded it — one local owner on the desktop,
        the signed-in user on the web."""
        paths: Paths = app.state.paths
        cfg = load_config(CONFIG_PATH)
        staged = []
        for upload_file in files:
            name = Path(upload_file.filename or "document.docx").name
            # Each upload gets its own folder so the document keeps its real
            # name — that name ends up on the file the user opens.
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
            folder = paths.uploads / owner / stamp
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / name
            with dest.open("wb") as fh:
                shutil.copyfileobj(upload_file.file, fh)

            entry = _stage(cfg, paths, stamp, dest)
            if not entry["ok"]:
                shutil.rmtree(folder, ignore_errors=True)
            staged.append(entry)
        return {"files": staged}

    def _stage(cfg, paths: Paths, stamp: str, dest: Path) -> dict:
        name = dest.name
        note = None
        if prep_convert.needs_conversion(dest):
            # .doc/.rtf/.odt/.txt are prep inputs, not DocProof formats. Convert
            # at drop time so everything after this point is a .docx.
            try:
                converted, note = prep_convert.ensure_docx(dest, dest.parent)
            except prep_convert.ConversionError as e:
                return {"filename": name, "ok": False, "error": str(e)}
            dest = converted

        entry: dict = {"id": f"{stamp}/{dest.name}", "filename": dest.name,
                       "original_filename": name, "converted": dest.name != name,
                       "note": note}
        try:
            entry["format"] = get_format(dest.name).to_api()
        except IngestError as e:
            return {**entry, "ok": False, "error": str(e)}

        review, review_error = _review_preflight(cfg, dest)
        prep, prep_error = _prep_preflight(cfg, paths, dest)
        promo, promo_error = _promo_preflight(cfg, paths, dest)
        entry.update(review or {}, review_error=review_error,
                     prep=prep, prep_error=prep_error,
                     promo=promo, promo_error=promo_error,
                     can_review=review is not None, can_prep=prep is not None,
                     can_promo=promo is not None)
        entry["ok"] = (review is not None or prep is not None
                       or promo is not None)
        if not entry["ok"]:
            entry["error"] = review_error or prep_error or promo_error
        return entry

    def _review_preflight(cfg, path: Path) -> tuple[dict | None, str | None]:
        try:
            # Counts only: this runs once per file at drop time and reports
            # section and token counts. The spell scan, sweeps and consistency
            # pass are seconds of work per manuscript whose results the drop
            # screen never shows — the real review recomputes them.
            prepared = prepare(cfg, path, ERROR_DIR, analyses=False)
        except (IngestError, ValueError) as e:
            return None, str(e)
        return {
            "sections": len(prepared.chunks),
            "paragraphs": len(prepared.doc.paragraphs),
            "requests": prepared.request_count,
            "input_tokens": prepared.est_document_tokens,
            "passes": len(prepared.groups),
            # The section list is what the picker is built from: the user
            # chooses by reading their own prose, not by chunk id.
            "chunks": chunk_outline(prepared),
        }, None

    def _prep_preflight(cfg, paths: Paths,
                        path: Path) -> tuple[dict | None, str | None]:
        try:
            prepared = preplib.prepare(cfg, path, config_dir=CONFIG_PATH.parent,
                                       override_dir=paths.prep)
        except (IngestError, StyleSheetError, ValueError) as e:
            return None, str(e)
        structure = prepared.structure
        return {
            "paragraphs": prepared.paragraph_count,
            "blank_lines": sum(1 for p in structure.paragraphs if p.is_blank),
            "words": structure.word_count,
            "requests": prepared.request_count,
            "input_tokens": prepared.est_document_tokens,
            "output_tokens": prepared.est_output_tokens,
            "style_sheet": prepared.sheet.name,
        }, None

    def _promo_preflight(cfg, paths: Paths,
                         path: Path) -> tuple[dict | None, str | None]:
        # Promo only reads, so it opens what prep would turn away (a manuscript
        # with tracked changes in it, say). A word count is enough for the drop
        # card; the run reads it in full.
        try:
            book = promolib.read_manuscript(path)
        except (IngestError, ValueError) as e:
            return None, str(e)
        return {"words": book.word_count}, None

    @app.get("/api/formats")
    def formats() -> dict:
        """What docproof can read, so the drop zone and the file picker are
        driven by the format registry rather than a hardcoded list that drifts
        the next time one is added."""
        return {"formats": describe(), "suffixes": list(SUFFIXES),
                # Not formats docproof reads: manuscripts LibreOffice turns
                # into a .docx at drop time. They belong with Word in the
                # picker, and the list belongs here rather than in the page.
                "prep_extra_suffixes": list(prep_convert.CONVERTIBLE)}
