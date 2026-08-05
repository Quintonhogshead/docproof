"""What the pipeline needs to know about a document format.

Everything between ingest and reassembly — the models, the chunker, the
analyzer, the validator, the providers, the report — is already format-neutral:
a ParagraphRef is an id, a part, a location, some text and a style name, and
none of those mean anything OOXML-specific. So a format is just the two ends
of the pipeline plus the words the UI uses to talk about it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class DocumentFormat:
    suffix: str                 # ".docx"
    name: str                   # "Word", for prose
    kind: str                   # "Word document", for lists and errors
    app: str                    # the application the user opens the result in
    # One line for summary.md, which is Markdown.
    review_instructions: str
    # The same thing for the app, which renders as plain text.
    where_to_look: str
    # Where the explanation for each change ends up, when comments are on.
    comment_noun: str

    preflight: Callable
    build_document_model: Callable
    apply_tracked_changes: Callable

    # Optional, because they are the only two things a format has to implement
    # in OOXML-shaped terms. A format that provides neither still reviews
    # documents; it just does not normalize quotes or audit itself, and both
    # facts are reported rather than assumed.
    #   normalize(pkg, quotes=, spaces=) -> NormalizationReport
    #   snapshot(pkg, mode) -> {para_id: text}, mode "current" | "reject"
    normalize: Callable | None = None
    snapshot: Callable | None = None

    # What the press appends to a manuscript it has proofread. The name is the
    # deliverable's identity — the team hands "<book> - Pre-Proofread.docx" to
    # an author — so it lives here rather than in whoever happens to save the
    # file. Anything that has to recognise docproof's own output matches on
    # these (see app/watch/stages.py).
    REVIEWED_SUFFIX = " - Pre-Proofread"
    CHANGE_LOG_SUFFIX = " - Pre-Proofread Change Log"

    def reviewed_name(self, source_path: str | Path) -> str:
        return f"{Path(source_path).stem}{self.REVIEWED_SUFFIX}{self.suffix}"

    def change_log_name(self, source_path: str | Path) -> str:
        """Always .docx: the change log is prose the press reads, whatever
        format the manuscript arrived in."""
        return f"{Path(source_path).stem}{self.CHANGE_LOG_SUFFIX}.docx"

    def to_api(self) -> dict:
        """The subset the frontend needs to label a file and tell the user
        where to look when it's done."""
        return {"suffix": self.suffix, "name": self.name, "kind": self.kind,
                "app": self.app, "where_to_look": self.where_to_look,
                "comment_noun": self.comment_noun}
