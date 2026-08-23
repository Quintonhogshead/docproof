"""Regenerate the double-comma regression fixture (P0-02).

Run from the repo root:

    .venv/bin/python tests/fixtures/candidate_double_comma/build_fixture.py

The generated ``source.docx`` reproduces the reported candidate-screening
failure: correctly punctuated introductory clauses were turned into
double-comma insertions, and a genuinely missing comma was mis-located to the
position immediately after the conjunction. See ``README.md`` for the full
description and the expected post-fix behaviour.
"""
from pathlib import Path

from docx import Document

HERE = Path(__file__).parent

# Each paragraph is a sentence whose introductory clause the generator inspects.
# The first three are already correct (a second comma must never be inserted);
# the fourth is genuinely missing its comma (a fix must place it at the real
# clause boundary, never right after the conjunction).
PARAGRAPHS = [
    "After the war, the men returned home.",           # correct — must pass
    "Although she was tired, she kept walking.",        # correct — must pass
    "Because it rained, the match was cancelled.",      # correct — must pass
    "When the sun rose the birds sang.",                # missing comma
    "However he never arrived.",                        # strong intro, missing
    "Meanwhile, the kettle boiled.",                    # strong intro, correct
]


def build() -> Path:
    document = Document()
    for text in PARAGRAPHS:
        document.add_paragraph(text)
    out = HERE / "source.docx"
    document.save(out)
    return out


if __name__ == "__main__":
    print("wrote", build())
