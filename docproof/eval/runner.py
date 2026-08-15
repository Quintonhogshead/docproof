"""Run the corpus through the real pipeline and hand back the findings.

The runner deliberately does NOT call `finish()` — it never applies tracked
changes, runs the audit, or writes a manuscript. It stops at
`validate_findings`, which is where a finding gets its status and anchor, and
that is all the scorer needs.

Two config choices make the score honest rather than an artifact of how the
corpus is assembled:

  * min_confidence is forced to "low" so nothing is dropped by the gate. The
    scorer re-applies the gate in post, which is how one run yields a
    precision/recall curve across all three thresholds.
  * the whole-document scans (consistency, spellcheck) are turned OFF. The
    corpus is unrelated one-paragraph cases concatenated into one file; a
    term-consistency or spell signal that spans paragraphs is an artifact of
    that packing, not something a real manuscript would show. The per-paragraph
    sweeps stay on — they see one paragraph at a time, exactly as in production.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path

from ..analyzer import MockAnalyzer
from ..config import Config
from ..models import DocumentModel, Finding, Usage
from ..pipeline import Prepared, prepare, run_sync
from ..providers import Provider
from ..validator import validate_findings
from .corpus import Case
from .docbuilder import BuiltCorpus, build_docx


@dataclass(frozen=True)
class EvalRun:
    cases: list[Case]
    built: BuiltCorpus
    doc: DocumentModel
    findings: list[Finding]      # validated, at the low gate — every one visible
    usage: Usage
    model: str


def _eval_config(cfg: Config) -> Config:
    """A copy of cfg with the eval-safe overrides applied. `validate_assignment`
    is on, so each set is checked."""
    cfg = cfg.model_copy(deep=True)
    cfg.min_confidence = "low"
    cfg.consistency.enabled = False
    cfg.spellcheck.enabled = False
    # Belt and braces: the runner never calls finish() (see the module
    # docstring), so a finish()-resident pass cannot fire here anyway. The line
    # is here so that if the eval ever grows a finish() path, the scorecard does
    # not silently start counting taste against a mechanical trap set.
    cfg.smoothing.enabled = False
    cfg.chapter_continuity.enabled = False   # finish()-resident too; same reason
    cfg.audit = "off"
    cfg.change_log = False
    return cfg


def _run_mock(cfg: Config, prepared: Prepared, canned: list[dict]
              ) -> tuple[list[Finding], Usage]:
    ids = itertools.count(1)
    usage = Usage()
    findings: list[Finding] = []
    for group in prepared.groups:
        analyzer = MockAnalyzer(group, canned, ids)
        for chunk in prepared.chunks:
            found, _ = analyzer.analyze_chunk(chunk, usage)
            findings.extend(found)
    return findings, usage


def run_eval(cfg: Config, cases: list[Case], error_dir: str | Path, work_dir: Path,
             *, provider: Provider | None = None,
             mock_findings: list[dict] | None = None,
             progress=None) -> EvalRun:
    """Build the corpus doc, review it, and return validated findings.

    Exactly one of `provider` (a real API run) or `mock_findings` (canned, free)
    must be given.
    """
    if (provider is None) == (mock_findings is None):
        raise ValueError("pass exactly one of provider or mock_findings")

    cfg = _eval_config(cfg)
    work_dir = Path(work_dir)
    built = build_docx(cases, work_dir / "corpus.docx",
                       min_paragraph_chars=cfg.chunking.min_paragraph_chars)

    prepared = prepare(cfg, str(built.docx_path), error_dir)

    if mock_findings is not None:
        model_findings, usage = _run_mock(cfg, prepared, mock_findings)
    else:
        model_findings, usage = run_sync(cfg, prepared, provider,
                                         progress=progress)

    # The ensemble's merge and verifier live in finish(), which the eval skips —
    # so mirror them here, or a 2x-detector run would score its raw union
    # (double-counted, unverified) instead of the pipeline the app would ship.
    if cfg.ensemble.enabled and mock_findings is None:
        from ..agreement import merge
        model_findings = merge(list(model_findings), prepared.doc)
        ens = cfg.ensemble
        if ens.verifier_model and ens.verify_policy != "none":
            from ..providers import build_provider
            from ..verifier import verify_findings
            vcfg = cfg.model_copy(deep=True)
            vcfg.api.model = ens.verifier_model
            vcfg.api.effort = ens.verifier_effort
            model_findings, _ = verify_findings(
                cfg, prepared, model_findings, build_provider(vcfg), usage)

    # Same order finish() uses: sweeps, then consistency, then model — earliest
    # to claim a span wins. (Consistency is empty here; it is disabled.)
    validated = validate_findings(
        list(prepared.sweep_findings)
        + list(prepared.consistency_findings)
        + list(model_findings),
        prepared.doc, cfg.min_confidence,
        query_types=prepared.query_types,
        format_types=prepared.format_types,
        edit_guard=cfg.edit_guard)

    return EvalRun(cases=cases, built=built, doc=prepared.doc,
                   findings=validated, usage=usage, model=cfg.api.model)
