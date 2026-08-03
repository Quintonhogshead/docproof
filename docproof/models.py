from __future__ import annotations

from dataclasses import dataclass, field

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

# Every value `status` can take. The design doc's five, plus one honest
# addition (rejected_noop — see Step 8).
STATUSES = (
    "pending",
    "validated",
    "rejected_no_anchor",
    "rejected_overlap",
    "rejected_duplicate",
    "rejected_noop",
    "skipped_low_confidence",
)


@dataclass(frozen=True)
class ParagraphRef:
    para_id: str      # "body-0042", "table-0-r2-c1-p0", "footnote-2-p0"
    part: str         # which XML part it lives in, e.g. "word/document.xml"
    location: str     # "body" | "table" | "header" | "footer" | "footnote" | "endnote"
    text: str         # canonical text — THE text Claude sees and anchors quote from
    style: str        # Word style ID, e.g. "Heading1", "Normal"


@dataclass(frozen=True)
class Chunk:
    chunk_id: str                                   # "chunk-007"
    paragraphs: tuple[ParagraphRef, ...]
    est_tokens: int
    context_paragraphs: tuple[ParagraphRef, ...] = ()   # reserved for future
                                                        # cross-paragraph error types


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
        