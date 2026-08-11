from __future__ import annotations

from dataclasses import dataclass, field

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

# Every value `status` can take. The design doc's five, plus the honest
# additions the pipeline actually emits (rejected_noop — see Step 8; and
# rejected_oversized, the edit-guard's refusal of an over-large re-type).
STATUSES = (
    "pending",
    "validated",
    # A question rather than a correction: anchored, and written into the
    # document as a margin comment with no revision around it.
    "query",
    "rejected_no_anchor",
    "rejected_overlap",
    "rejected_duplicate",
    "rejected_noop",
    # The overreach guard refused a fix that re-types too large a span or adds
    # too much new text — a rewrite, not a minimal proofreading edit.
    "rejected_oversized",
    # The overseer-verifier judged this finding a false positive; kept for the
    # report with the verifier's reason, never applied.
    "rejected_by_verifier",
    "skipped_low_confidence",
)


@dataclass(frozen=True)
class ParagraphRef:
    para_id: str      # "body-0042", "table-0-r2-c1-p0", "footnote-2-p0"
    part: str         # which XML part it lives in, e.g. "word/document.xml"
    location: str     # "body" | "table" | "header" | "footer" | "footnote" | "endnote"
    text: str         # canonical text — THE text Claude sees and anchors quote from
    style: str        # Word style ID, e.g. "Heading1", "Normal"
    # Whether this paragraph is worth spending a model pass on. Headings are
    # not — a chapter title is the sweeps' to fix, not the model's to rewrite —
    # and short lines are excluded only when a press sets a min_paragraph_chars
    # floor (the default is 0, so dialogue lines like "“Who?” he asked." are
    # reviewed: that is exactly where a missing word or homophone slip hides).
    # The scripted sweeps read every paragraph regardless; only the model
    # passes honour this flag.
    reviewable: bool = True


@dataclass(frozen=True)
class Chunk:
    chunk_id: str                                   # "chunk-007"
    paragraphs: tuple[ParagraphRef, ...]
    est_tokens: int
    # The trailing paragraphs of the previous chunk, sent read-only ahead of
    # this chunk's own so an error here can be judged with its antecedent in
    # view — a pronoun's referent, a name's spelling, the speaker of a quote
    # that reaches back across the chunk boundary. The model is told never to
    # report on them, and any finding that lands on one is dropped by the
    # para_id guard in the analyzer. Populated by the chunker; empty when
    # context is disabled or there is no previous chunk.
    context_paragraphs: tuple[ParagraphRef, ...] = ()


@dataclass(frozen=True)
class Anchor:
    """A validated, shrunk edit. Offsets index into the paragraph's canonical
    text. start == end means a pure insertion; insert_text == "" means a pure
    deletion."""
    start: int
    end: int
    delete_text: str   # must equal paragraph.text[start:end] — re-asserted before surgery
    insert_text: str


@dataclass(frozen=True)
class Finding:
    finding_id: str            # "f-0031"
    chunk_id: str
    para_id: str
    error_type: str            # registry key, e.g. "comma_splice"
    original_text: str         # verbatim sentence quoted by the model
    occurrence: int            # 1-based, disambiguates repeated sentences
    corrected_text: str        # the model's corrected sentence
    explanation: str
    confidence: str            # "high" | "medium" | "low"
    status: str = "pending"
    anchor: Anchor | None = None   # set by the validator when status == "validated"
    # Set by the validator, never by the model: the run formatting this
    # finding applies ("italic"), for a finding that changes how text is set
    # rather than what it says.
    format: str = ""
    # Ensemble bookkeeping, all inert in single-detector mode. `detector` tags
    # which detector produced a raw finding during fan-out (and is the one field
    # that rides the resumable checkpoint); the rest are set by the agreement
    # merge — how many detectors found this edit, whether they proposed
    # different fixes for the same span, and which detectors those were.
    detector: int = 0
    agreement: int = 1
    disputed_fix: bool = False
    provenance: tuple[int, ...] = ()
    # Set by the overseer-verifier to route a finding it wasn't sure enough to
    # keep as a change down the query channel instead — a margin comment, not a
    # tracked edit. Inert unless the verifier ran.
    force_query: bool = False


@dataclass(frozen=True)
class DocumentModel:
    source_path: str
    paragraphs: tuple[ParagraphRef, ...]            # reviewable paragraphs only
    skipped: tuple[tuple[str, str], ...] = ()       # (para_id, reason) for the report


def index_paragraphs(doc: DocumentModel) -> dict[str, ParagraphRef]:
    return {p.para_id: p for p in doc.paragraphs}


@dataclass
class Usage:
    """Token accounting, accumulated across every API call (mutable on purpose
    — it's a counter, not a contract)."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    api_calls: int = 0

    def add(self, resp_usage) -> None:
        self.api_calls += 1
        for f in ("input_tokens", "output_tokens",
                  "cache_creation_input_tokens", "cache_read_input_tokens"):
            setattr(self, f, getattr(self, f) + (getattr(resp_usage, f, 0) or 0))


@dataclass(frozen=True)
class CoverageGap:
    """A (pass, chunk) unit the model never answered — a refusal, a truncation,
    or a dropped batch result — that a retry could not recover. Kept so the
    report can name the paragraphs that went unreviewed instead of letting the
    gap read as 'nothing found here'."""
    pass_label: str                    # the pass's error types, e.g. "spelling"
    chunk_id: str
    para_ids: tuple[str, ...]


@dataclass
class CoverageLedger:
    """Which (pass, chunk) units a run actually reviewed. `total` counts every
    unit attempted; `gaps` are the ones no answer (or retry) could cover. A run
    with gaps under-reports by definition, so the ledger is what turns that from
    a silent hole into a line in the report."""
    total: int = 0
    gaps: list[CoverageGap] = field(default_factory=list)

    def record(self, pass_label: str, chunk, ok: bool) -> None:
        self.total += 1
        if not ok:
            self.gaps.append(CoverageGap(
                pass_label, chunk.chunk_id,
                tuple(p.para_id for p in chunk.paragraphs)))

    @property
    def complete(self) -> bool:
        return not self.gaps

    @property
    def reviewed(self) -> int:
        return self.total - len(self.gaps)
        