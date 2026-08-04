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

    def reviewed_name(self, source_path: str | Path) -> str:
        return f"reviewed_{Path(source_path).stem}{self.suffix}"

    def to_api(self) -> dict:
        """The subset the frontend needs to label a file and tell the user
        where to look when it's done."""
        return {"suffix": self.suffix, "name": self.name, "kind": self.kind,
                "app": self.app, "where_to_look": self.where_to_look,
                "comment_noun": self.comment_noun}
