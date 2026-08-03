"""The pipeline as three reusable steps.

`prepare` and `finish` bookend every route into docproof — the CLI's `review`,
the batch collector, and the app's job runner. Only the middle differs: one
sends chunks to a provider now, the other picks results up hours later.
"""
from __future__ import annotations

import hashlib
import itertools
import logging
from dataclasses import dataclass
from pathlib import Path

from .analyzer import Analyzer
from .chunker import chunk_document
from .config import Config
from .error_registry import ErrorType, load_error_types
from .ingest import build_document_model, preflight
from .models import Chunk, DocumentModel, Usage
from .providers import Provider
from .reassembler import apply_tracked_changes
from .reporting import write_findings_json, write_summary_md
from .utils.xml_helpers import DocxPackage
from .validator import validate_findings

log = logging.getLogger("docproof.pipeline")


@dataclass
class Prepared:
    pkg: DocxPackage
    doc: DocumentModel
    chunks: list[Chunk]
    groups: list[list[ErrorType]]

    @property
    def content_hash(self) -> str:
        return content_hash(self.doc)

    @property
    def request_count(self) -> int:
        return len(self.chunks) * len(self.groups)

    @property
    def est_document_tokens(self) -> int:
        return sum(c.est_tokens for c in self.chunks) * len(self.groups)


@dataclass(frozen=True)
class Outputs:
    reviewed_docx: Path
    summary_md: Path
    findings_json: Path
    applied: int
    findings: int


def content_hash(doc: DocumentModel) -> str:
    """Fingerprint of the reviewable text, in walk order.

    A batch is collected in a different process — possibly days later — so the
    document is re-ingested from disk at that point. If the author edited it
    meanwhile, paragraph offsets have moved and every anchor would land in the
    wrong place. Comparing this hash turns that into a clean failure."""
    h = hashlib.sha256()
    for p in doc.paragraphs:
        h.update(p.para_id.encode("utf-8"))
        h.update(b"\0")
        h.update(p.text.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def prepare(cfg: Config, input_path: str | Path, error_dir: str | Path, *,
            max_chunks: int | None = None) -> Prepared:
    """Ingest, chunk, and resolve error types. Raises IngestError on a document
    docproof refuses to touch (tracked changes, corruption)."""
    pkg = preflight(str(input_path), cfg.tracked_changes_policy)
    doc = build_document_model(pkg, cfg)
    chunks = list(chunk_document(doc, cfg))
    if max_chunks:
        chunks = chunks[:max_chunks]
        log.info("Reviewing only the first %d chunk(s)", len(chunks))
    registry = load_error_types(error_dir, cfg.error_type_keys)
    groups = [[registry[k] for k in group] for group in cfg.error_type_groups]
    log.info("%d error type(s) in %d pass(es): %s", len(cfg.error_type_keys),
             len(groups), "; ".join("+".join(g) for g in cfg.error_type_groups))
    return Prepared(pkg=pkg, doc=doc, chunks=chunks, groups=groups)


def build_analyzers(cfg: Config, groups: list[list[ErrorType]],
                    provider: Provider | None,
                    finding_ids: itertools.count) -> list[Analyzer]:
    return [Analyzer(cfg, group, provider, finding_ids) for group in groups]


def run_sync(cfg: Config, prepared: Prepared, provider: Provider, *,
             progress=None) -> tuple[list, Usage]:
    """Review every chunk now, one API call per (pass, chunk).

    `progress` is called with (done, total) after each call so a UI can show
    movement; it is the only reason this loop isn't a comprehension."""
    ids = itertools.count(1)
    usage = Usage()
    findings: list = []
    total = prepared.request_count
    done = 0
    for analyzer in build_analyzers(cfg, prepared.groups, provider, ids):
        for chunk in prepared.chunks:
            findings.extend(analyzer.analyze_chunk(chunk, usage))
            done += 1
            if progress:
                progress(done, total)
    return findings, usage


def finish(prepared: Prepared, findings: list, usage: Usage, cfg: Config, *,
           out_dir: str | Path, source_path: str | Path,
           batch: bool = False) -> Outputs:
    """Validate, write tracked changes, save, and report."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    validated = validate_findings(findings, prepared.doc, cfg.min_confidence)
    stats = apply_tracked_changes(prepared.pkg, prepared.doc, validated, cfg)

    reviewed = out / f"reviewed_{Path(source_path).stem}.docx"
    prepared.pkg.save(reviewed)
    write_findings_json(out / "findings.json", doc=prepared.doc,
                        findings=validated, usage=usage, cfg=cfg,
                        applied_ids=stats.applied, batch=batch)
    write_summary_md(out / "summary.md", doc=prepared.doc, findings=validated,
                     usage=usage, cfg=cfg, applied_ids=stats.applied,
                     batch=batch)
    return Outputs(reviewed_docx=reviewed, summary_md=out / "summary.md",
                   findings_json=out / "findings.json",
                   applied=len(stats.applied), findings=len(validated))
