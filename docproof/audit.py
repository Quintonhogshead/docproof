"""The reject-all audit: proof that docproof changed nothing it did not track.

The house brief makes this mandatory, and phrases it as reproducing the
original character-for-character with two allowed exceptions. Stated that way
the check needs to know about quote curling and space collapsing, which would
tie it to the normalizer forever.

There is a cleaner formulation that means the same thing. Normalization runs
before ingest, so the text docproof *ingested* already has both exceptions
applied. The invariant is therefore simply:

    rejecting every tracked change reproduces the document as ingested

with no exceptions at all. The two silent edits are accounted for separately,
by count, in the normalization report. Anything else that differs is a bug —
an edit that reached the manuscript without a revision mark around it, which
is precisely the failure no one would otherwise notice, because the accepted
view looks perfectly correct.

The audit runs before the file is written. A run that fails it produces no
output document at all, which is the only honest meaning of "refuse to ship".

One thing it does not cover, stated here because a check whose limits are
unwritten gets trusted past them: this compares TEXT. A tracked formatting
revision (`title_italics` marking a book title italic) changes no characters,
so it passes the audit trivially — correctly, since rejecting it restores the
old run properties and the text was never in question. But formatting applied
*without* a revision around it would also pass unnoticed. Nothing does that
today; if something ever should, the baseline needs to carry run properties
too, not just text.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("docproof.audit")


class AuditError(Exception):
    """Raised when the reject-all view does not reproduce the ingested text.
    Message is user-facing."""


@dataclass(frozen=True)
class Mismatch:
    para_id: str
    expected: str
    got: str

    def describe(self, width: int = 90) -> str:
        """Where the two first diverge, which is what a person needs to see —
        two long paragraphs printed in full would bury it."""
        i = 0
        while (i < len(self.expected) and i < len(self.got)
               and self.expected[i] == self.got[i]):
            i += 1
        lo = max(0, i - width // 2)
        return (f"{self.para_id} at character {i}:\n"
                f"    ingested: …{self.expected[lo:lo + width]!r}\n"
                f"    rejected: …{self.got[lo:lo + width]!r}")


@dataclass(frozen=True)
class AuditReport:
    checked: int = 0
    mismatches: tuple[Mismatch, ...] = ()
    missing: tuple[str, ...] = ()      # paragraphs that vanished entirely
    ran: bool = False

    @property
    def passed(self) -> bool:
        return not self.mismatches and not self.missing

    def summary(self) -> str:
        if not self.ran:
            return "not run"
        if self.passed:
            return (f"passed — rejecting every tracked change reproduces all "
                    f"{self.checked} ingested paragraph(s) exactly")
        n = len(self.mismatches) + len(self.missing)
        return (f"FAILED — {n} of {self.checked} paragraph(s) do not return to "
                f"their ingested text when every change is rejected")


def run_audit(baseline: dict[str, str], reject_view: dict[str, str]
              ) -> AuditReport:
    """Compare the reject-all view against the text as ingested."""
    mismatches: list[Mismatch] = []
    missing: list[str] = []
    for para_id, expected in baseline.items():
        if para_id not in reject_view:
            missing.append(para_id)
        elif reject_view[para_id] != expected:
            mismatches.append(Mismatch(para_id, expected, reject_view[para_id]))

    report = AuditReport(checked=len(baseline), mismatches=tuple(mismatches),
                         missing=tuple(missing), ran=True)
    if report.passed:
        log.info("Reject-all audit passed: %d paragraph(s) return to their "
                 "ingested text exactly.", report.checked)
    else:
        log.error("Reject-all audit FAILED. %s", report.summary())
        for m in mismatches[:5]:
            log.error("  %s", m.describe())
        for para_id in missing[:5]:
            log.error("  %s is missing from the output entirely", para_id)
    return report


def enforce(report: AuditReport, policy: str) -> None:
    """Turn a failed audit into a refusal, unless told otherwise."""
    if report.passed or not report.ran or policy != "strict":
        return
    detail = "\n".join(m.describe() for m in report.mismatches[:5])
    if report.missing:
        detail += "\nMissing paragraphs: " + ", ".join(report.missing[:5])
    raise AuditError(
        f"{report.summary()}.\n{detail}\n\n"
        "No file was written. Something changed the manuscript without "
        "wrapping it in a tracked change, so accepting or rejecting the "
        "revisions would not return the author's text. Rerun with "
        "audit: warn to write the file anyway and inspect it.")
