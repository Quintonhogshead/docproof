"""The corrections run from end to end: a source list becomes edits, the edits
are applied to the designer's IDML, and the result is verified against the list.

This is the corrections counterpart to `prep.pipeline` — the one function the app
calls per job. It owns no policy of its own; it wires the three stages together
and writes the report, so the app runner stays thin and every decision lives in
`parse`, `apply` and `verify`.

Two entry points, sharing the report:

  * `apply_corrections` — the usual case. The designer exports the finished book
    to IDML; we apply the corrections and hand back a corrected IDML plus the
    report. We verify our own output as a self-check (it should always be clean;
    a discrepancy here is a bug in `apply`, surfaced rather than shipped).
  * `verify_corrections` — the audit case from Nick's thread. The book was
    corrected by someone else's script; given the before file, that after file
    and the list, we report every change the list does not explain. We write no
    IDML — the after file is already the deliverable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .apply import apply_edits
from .model import ApplyReport, VerifyReport
from .parse import ParseResult, parse_edits
from .report import write_report
from .verify import verify

log = logging.getLogger("docproof.corrections.run")


@dataclass(frozen=True)
class CorrectionsOutputs:
    """Everything a corrections run produced: the file (in apply mode), the two
    report artifacts, and the three stage reports the app reads for its card."""
    report_md: Path
    report_json: Path
    parse: ParseResult
    verify: VerifyReport
    corrected_idml: Path | None = None      # None in verify-only mode
    apply: ApplyReport | None = None        # None in verify-only mode

    @property
    def applied(self) -> int:
        return self.apply.applied if self.apply is not None else 0

    @property
    def flagged(self) -> int:
        """Corrections refused rather than guessed at — the ones a human owns."""
        if self.apply is not None:
            return len(self.apply.flagged)
        # Verify-only: a correction the after file does not carry is the same
        # idea, read off the reconciliations.
        from .model import APPLIED_EXACTLY
        return sum(1 for r in self.verify.reconciliations
                   if r.status != APPLIED_EXACTLY)

    @property
    def discrepancies(self) -> int:
        return len(self.verify.discrepancies)

    @property
    def clean(self) -> bool:
        """Every correction landed exactly and nothing else changed — and every
        entry in the source parsed."""
        return self.verify.clean and self.parse.ok and self.flagged == 0


def corrected_name(src_idml: str | Path) -> str:
    """The default name for the corrected file: the source stem with a suffix, so
    a designer can tell it from the export they sent."""
    return f"{Path(src_idml).stem}_corrected.idml"


def apply_corrections(src_idml: str | Path, corrections, out_dir: str | Path, *,
                      id_prefix: str = "c",
                      dest_name: str | None = None) -> CorrectionsOutputs:
    """Apply a corrections source to `src_idml`, writing the corrected IDML and
    the report into `out_dir`.

    `corrections` is anything `parse.parse_edits` accepts (a list, JSON text, a
    `.json` path). `src_idml` is never modified. Entries that cannot be parsed
    and edits that cannot be anchored are reported, never guessed."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    parsed = parse_edits(corrections, id_prefix=id_prefix)
    edits = list(parsed.edits)

    corrected = out / (dest_name or corrected_name(src_idml))
    apply_report = apply_edits(src_idml, corrected, edits)
    # Self-check: our own output, verified against the same list. Clean by
    # construction unless `apply` has a bug — in which case the report says so
    # instead of the file going out unflagged.
    verify_report = verify(src_idml, corrected, edits)

    report_md, report_json = write_report(
        out, source_path=src_idml, after_path=corrected, parse=parsed,
        apply=apply_report, verify=verify_report)
    log.info("Corrections applied to %s: %s; verify %s",
             Path(src_idml).name, apply_report.summary(),
             "clean" if verify_report.clean else "has discrepancies")
    return CorrectionsOutputs(
        report_md=report_md, report_json=report_json, parse=parsed,
        verify=verify_report, corrected_idml=corrected, apply=apply_report)


def verify_corrections(before_idml: str | Path, after_idml: str | Path,
                       corrections, out_dir: str | Path, *,
                       id_prefix: str = "c") -> CorrectionsOutputs:
    """Audit an already-corrected IDML against the corrections list.

    No IDML is written — `after_idml` is the deliverable, produced elsewhere (a
    hand-run script, InDesign's own find/change). The report names every change
    in it that the list does not account for."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    parsed = parse_edits(corrections, id_prefix=id_prefix)
    edits = list(parsed.edits)
    verify_report = verify(before_idml, after_idml, edits)
    report_md, report_json = write_report(
        out, source_path=before_idml, after_path=after_idml, parse=parsed,
        apply=None, verify=verify_report)
    log.info("Corrections verified for %s: %s",
             Path(after_idml).name,
             "clean" if verify_report.clean else
             f"{len(verify_report.discrepancies)} discrepancy(ies)")
    return CorrectionsOutputs(
        report_md=report_md, report_json=report_json, parse=parsed,
        verify=verify_report, corrected_idml=None, apply=None)
