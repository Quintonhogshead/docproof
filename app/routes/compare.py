"""Diffing two versions of the same manuscript."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, UploadFile

from docproof import formatdiff
from docproof.trackdiff import (TrackDiffError, compare_edits, extract_edits,
                                open_docx, report_json)

from ..auth import owner_for
from ..settings import Paths


def register(app: FastAPI) -> None:

    @app.post("/api/compare")
    async def compare(doc_a: UploadFile, doc_b: UploadFile,
                      mode: str = Form("changes"),
                      owner: str = Depends(owner_for)) -> dict:
        """Diff two uploaded .docx files, one of two ways.

        `mode="changes"` (the default) diffs their tracked changes — a human
        proofread against DocProof's review of the same manuscript.
        `mode="formatting"` diffs their InDesign paragraph styling — a human's
        hand-tagging against DocProof's prep pass. Either way the two files are
        the same manuscript formatted or edited two ways.

        Unlike /api/files, this does NOT reject a document that carries tracked
        changes — comparing them is the whole point — so each file is opened
        with trackdiff.open_docx rather than the review preflight. Nothing is
        staged or queued: parsing two documents locally is fast, so the answer
        comes back in the same request. The uploads are written under the
        owner's space and removed before the function returns."""
        if mode not in ("changes", "formatting"):
            raise HTTPException(400, f"Unknown compare mode: {mode!r}.")
        paths: Paths = app.state.paths
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        folder = paths.uploads / owner / f"compare-{stamp}"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            saved = []
            for i, up in enumerate((doc_a, doc_b)):
                name = Path(up.filename or f"doc{i}.docx").name
                dest = folder / f"{i}_{name}"
                with dest.open("wb") as fh:
                    shutil.copyfileobj(up.file, fh)
                saved.append(dest)
            try:
                if mode == "formatting":
                    report = formatdiff.compare_files(
                        saved[0], saved[1],
                        label_a="human", label_b="docproof")
                    return formatdiff.report_json(report)
                edits = [extract_edits(open_docx(p)) for p in saved]
            except TrackDiffError as e:
                raise HTTPException(400, str(e))
            report = compare_edits(edits[0], edits[1],
                                   label_a="human", label_b="docproof")
            return {**report_json(report, edits[0].base), "mode": "changes"}
        finally:
            shutil.rmtree(folder, ignore_errors=True)
