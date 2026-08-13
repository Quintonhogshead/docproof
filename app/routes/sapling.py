"""Sapling.ai grammar check — a standalone test surface, not part of review.

Wired in as its own panel so Sapling's suggestions can be eyeballed against
DocProof's own on the same text, without touching the review pipeline or a bill.
One route: paste text, get Sapling's edits back. The key is the admin-set
Sapling key (see settings.KEY_PROVIDERS); a missing or rejected key comes back
as a plain message the panel shows.
"""
from __future__ import annotations

import base64
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, UploadFile
from pydantic import BaseModel

from docproof.config import load_config
from docproof.ingest import IngestError, build_document_model, preflight
from docproof.models import Anchor, Finding, index_paragraphs
from docproof.reassembler import apply_tracked_changes
from docproof.sapling import SaplingError, check, check_paragraphs, describe

from ..auth import owner_for
from ..settings import CONFIG_PATH, SAPLING, Paths, get_api_key

# The regional varieties the panel offers, mapped to Sapling's own codes. An
# empty label means "send no preference"; anything else must be a key here.
VARIETIES = {"": None, "us": "us-variety", "gb": "gb-variety",
             "au": "au-variety", "ca": "ca-variety"}

# A generous ceiling for the paste box — enough for a chapter, not a whole book
# (that is what a real review is for), and a guard against an accidental paste of
# something enormous.
MAX_CHARS = 50_000

# The most manuscript text the docx pass will send Sapling in one go. Larger than
# the paste box — a document is the point here — but still a ceiling: this is a
# test surface returning the whole file inline, not the production review.
MAX_DOC_CHARS = 300_000

# What the tracked changes are attributed to, and the tag appended to the
# downloaded file — deliberately NOT the review's " - Atmosphere Press
# Proofreader" suffix, so a Sapling test output is never mistaken for a real
# proofread (nor picked up by the watcher, which recognises the review suffix).
SAPLING_AUTHOR = "Sapling"
SAPLING_SUFFIX = " - Sapling"


class SaplingCheck(BaseModel):
    text: str
    variety: str = ""


def _key_or_400() -> str:
    key = get_api_key(SAPLING)
    if not key:
        raise HTTPException(
            400, "No Sapling API key is set. An administrator can add one "
                 "under Admin → Provider API keys.")
    return key


def register(app: FastAPI) -> None:

    @app.post("/api/sapling/check")
    def sapling_check(body: SaplingCheck) -> dict:
        key = _key_or_400()
        if body.variety not in VARIETIES:
            raise HTTPException(400, f"Unknown variety: {body.variety!r}.")
        text = body.text or ""
        if len(text) > MAX_CHARS:
            raise HTTPException(
                400, f"That's a lot of text for a test — keep it under "
                     f"{MAX_CHARS:,} characters.")
        try:
            edits = check(text, key, variety=VARIETIES[body.variety])
        except SaplingError as e:
            # 502: the failure is Sapling's or the network's, not the caller's.
            raise HTTPException(502, str(e))
        return {"count": len(edits), "edits": [e.as_dict() for e in edits]}

    @app.post("/api/sapling/docx")
    async def sapling_docx(file: UploadFile, variety: str = Form(""),
                           owner: str = Depends(owner_for)) -> dict:
        """Run a whole .docx through Sapling and hand back a tracked-changes copy.

        The same OOXML reassembler a DocProof review writes with turns Sapling's
        edits into real Word revisions, so the result opens with each suggestion
        as an accept/reject change attributed to Sapling. The file comes back
        inline (base64) alongside the list of changes; nothing is queued, saved
        as a job, or billed through DocProof. Not the production path — a size
        ceiling keeps this a test surface, not a stand-in for a real review."""
        key = _key_or_400()
        if variety not in VARIETIES:
            raise HTTPException(400, f"Unknown variety: {variety!r}.")
        name = Path(file.filename or "document.docx").name
        if not name.lower().endswith(".docx"):
            raise HTTPException(400, "Sapling's document pass takes a .docx file.")

        paths: Paths = app.state.paths
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        folder = paths.uploads / owner / f"sapling-{stamp}"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            src = folder / name
            with src.open("wb") as fh:
                shutil.copyfileobj(file.file, fh)

            cfg = load_config(CONFIG_PATH)
            cfg.revision_author = SAPLING_AUTHOR
            try:
                pkg = preflight(src, cfg.tracked_changes_policy)
                doc = build_document_model(pkg, cfg)
            except IngestError as e:
                raise HTTPException(400, str(e))

            total = sum(len(p.text) for p in doc.paragraphs)
            if total > MAX_DOC_CHARS:
                raise HTTPException(
                    400, f"That manuscript is {total:,} characters — over this "
                         f"test panel's {MAX_DOC_CHARS:,} limit. Run a real "
                         f"review for a whole book, or try a shorter excerpt.")

            try:
                para_edits = check_paragraphs(
                    [(p.para_id, p.text) for p in doc.paragraphs], key,
                    variety=VARIETIES[variety])
            except SaplingError as e:
                raise HTTPException(502, str(e))

            findings, records = _findings_from(
                para_edits, doc, silent=not cfg.sapling.comments)
            stats = apply_tracked_changes(pkg, doc, findings, cfg)
            applied = set(stats.applied)

            out_name = f"{Path(name).stem}{SAPLING_SUFFIX}.docx"
            out_path = folder / out_name
            pkg.save(out_path)
            data = base64.b64encode(out_path.read_bytes()).decode("ascii")

            edits_out = [{
                "para_id": fid_pe[1].para_id,
                "original": fid_pe[1].original,
                "replacement": fid_pe[1].replacement,
                "error_type": fid_pe[1].error_type,
                "general_error_type": fid_pe[1].general_error_type,
                "applied": fid_pe[0] in applied,
            } for fid_pe in records]

            return {"filename": out_name, "docx_base64": data,
                    "paragraphs": len(doc.paragraphs),
                    "found": len(records), "applied": len(applied),
                    "edits": edits_out}
        finally:
            shutil.rmtree(folder, ignore_errors=True)


def _findings_from(para_edits, doc, *, silent=False):
    """Turn Sapling's per-paragraph edits into validated Findings the reassembler
    can apply, dropping no-ops and any edit overlapping one already kept in the
    same paragraph (the reassembler assumes disjoint edits, as the validator
    guarantees on the review path). Returns (findings, records) where records
    pairs each finding_id with its ParaEdit for the response list. `silent`
    applies each change with no margin comment (sapling.comments off)."""
    paras = index_paragraphs(doc)
    by_para: dict[str, list] = {}
    for pe in para_edits:
        by_para.setdefault(pe.para_id, []).append(pe)

    findings: list[Finding] = []
    records: list[tuple[str, object]] = []
    n = 0
    for para_id, edits in by_para.items():
        if para_id not in paras:
            continue
        last_end = -1
        for pe in sorted(edits, key=lambda e: (e.start, e.end)):
            if pe.start < last_end:                # overlaps a kept edit
                continue
            if pe.original == pe.replacement:      # nothing to change
                continue
            if pe.start == pe.end and not pe.replacement:
                continue
            last_end = pe.end
            n += 1
            fid = f"sap-{n:04d}"
            findings.append(Finding(
                finding_id=fid, chunk_id="sapling", para_id=para_id,
                error_type=(pe.error_type or "sapling"),
                original_text=pe.original, occurrence=1,
                corrected_text=pe.replacement, explanation=describe(pe),
                confidence="high", status="validated", silent=silent,
                anchor=Anchor(pe.start, pe.end, pe.original, pe.replacement)))
            records.append((fid, pe))
    return findings, records
