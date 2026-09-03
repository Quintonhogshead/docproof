"""Deterministic generator for the Galley "bad manuscript" fixture.

Builds `bad_novel.docx`: Chapter One of the tiny-novel fixture
(`make_tiny_novel.py` — one heading, twelve body paragraphs), with most of its
sentences carrying REWRITE-CLASS damage instead of the mechanical slips
`tiny_novel.docx` plants — subject-verb disagreement, a dropped word, a
run-on, or garbled word order. The catalog of what was broken and how a
proofread repairs it lives in `bad_novel.manifest.json` (the answer key
`galley/outcome.py`'s `needs_human` tests anchor their correction rows
against); this module is the single source of truth for both files.

Determinism: every damage is an exact, asserted substring replacement — no
randomness, no timestamps, no seeds — on the tiny novel's own literal prose,
so extracting the paragraph texts yields byte-identical strings on every run.
Core properties are pinned the same way `make_tiny_novel.py` pins them.

Why Chapter One only: this fixture exists to drive `galley/outcome.py`'s
verdict through a real run directory at $0, not to be a second novel — twelve
body paragraphs, ten of them damaged, is enough book for the rewrite-share
threshold to fire with a clear margin while staying small.

Run directly (`python make_bad_novel.py`) to (re)write the .docx and the
manifest next to this file, or import `paragraph_texts()` / `DAMAGE` /
`build()` for the text and the answer key without touching disk.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import docx

HERE = Path(__file__).parent
DOCX = HERE / "bad_novel.docx"
MANIFEST_PATH = HERE / "bad_novel.manifest.json"

sys.path.insert(0, str(HERE))
try:
    from make_tiny_novel import BODY_STYLE, CHAPTERS, HEADING_STYLE
finally:
    sys.path.remove(str(HERE))

# A fixed instant for the package's core properties — never "now", the same
# discipline make_tiny_novel.py follows so the build is reproducible.
_EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)

CHAPTER_ONE_HEADING, CHAPTER_ONE_BODY = CHAPTERS[0]
assert CHAPTER_ONE_HEADING == "Chapter One"


# --- The damage -------------------------------------------------------
#
# One entry per damaged paragraph (1-based position among Chapter One's
# twelve body paragraphs, in tiny_novel.docx reading order). `clean` is the
# exact sentence(s) as they read in tiny_novel.docx; `damaged` is what a
# genuinely broken manuscript would carry instead — the whole-sentence
# rewrite a proofread's correction row would anchor on. Paragraphs 1 and 7
# are left undamaged on purpose: a real bad book still has a few sound
# paragraphs, and it keeps this fixture's rewrite share short of 100%, which
# would be a suspiciously clean signal to test against.

DAMAGE: list[dict] = [
    {
        "id": "D1-subject-verb",
        "paragraph": 2,
        "damage_type": "subject_verb_disagreement",
        "clean": "The orchard beyond the yard was a smudge of dark "
                 "branches, patient and unmoving in the wet morning air.",
        "damaged": "The orchard beyond the yard were a smudge of dark "
                   "branches, patient and unmoving in the wet morning air.",
    },
    {
        "id": "D2-dropped-word",
        "paragraph": 3,
        "damage_type": "dropped_word",
        "clean": "Her father had built the house with his own hands, "
                 "board by board, in the years before she was born.",
        "damaged": "Her father had built house with his own hands, "
                   "board by board, in the years before she was born.",
    },
    {
        "id": "D3-garbled-order",
        "paragraph": 4,
        "damage_type": "garbled_word_order",
        "clean": "She had set the pages on the kitchen table three days "
                 "ago and had not found the nerve to open them since, as "
                 "though the ink itself might rise up and accuse her of "
                 "some small dishonesty.",
        "damaged": "She had set the pages on the kitchen table three days "
                   "ago and not had found the nerve to open them since, as "
                   "though the ink itself might rise up and accuse her of "
                   "some small dishonesty.",
    },
    {
        "id": "D4-dropped-word",
        "paragraph": 5,
        "damage_type": "dropped_word",
        "clean": "Kathryn pulled on her coat, laced her boots, and "
                 "stepped out into a world washed clean, determined to "
                 "walk until her thoughts settled into something she "
                 "could use.",
        "damaged": "Kathryn pulled on her coat, laced her boots, and "
                   "stepped out into a world washed clean, determined "
                   "walk until her thoughts settled into something she "
                   "could use.",
    },
    {
        "id": "D5-run-on",
        "paragraph": 6,
        "damage_type": "run_on",
        "clean": "The lane was soft underfoot and quiet, and she met no "
                 "one on the long walk to the crossroads. A single crow "
                 "watched her from the top of a fence post, unbothered, "
                 "turning its head as she passed as if it had been "
                 "expecting her all along.",
        "damaged": "The lane was soft underfoot and quiet, and she met no "
                   "one on the long walk to the crossroads, a single crow "
                   "watched her from the top of a fence post, unbothered, "
                   "turning its head as she passed as if it had been "
                   "expecting her all along.",
    },
    {
        "id": "D6-subject-verb",
        "paragraph": 8,
        "damage_type": "subject_verb_disagreement",
        "clean": "He noticed everything and said almost none of it aloud.",
        "damaged": "He notice everything and said almost none of it aloud.",
    },
    {
        "id": "D7-garbled-order",
        "paragraph": 9,
        "damage_type": "garbled_word_order",
        "clean": "The crossroads had not changed in her lifetime, four "
                 "rough lanes meeting at a leaning signpost whose paint "
                 "had long since weathered away.",
        "damaged": "The crossroads not had changed in her lifetime, four "
                   "rough lanes meeting at a leaning signpost whose paint "
                   "had long since weathered away.",
    },
    {
        "id": "D8-dropped-word",
        "paragraph": 10,
        "damage_type": "dropped_word",
        "clean": "In the end the decision made itself, the way such "
                 "decisions usually did, and she found her feet already "
                 "carrying her up the rising road before she had quite "
                 "chosen to go.",
        "damaged": "In the end decision made itself, the way such "
                   "decisions usually did, and she found her feet already "
                   "carrying her up the rising road before she had quite "
                   "chosen to go.",
    },
    {
        "id": "D9-run-on",
        "paragraph": 11,
        "damage_type": "run_on",
        "clean": "The years had taught her to distrust grand plans and to "
                 "trust small motions instead, and so she did not try to "
                 "picture the whole of the day ahead. She would walk to "
                 "the town, and see what the town had to show her, and "
                 "let the next thing follow from the last, and somewhere "
                 "in all that ordinary forward motion the answer she "
                 "needed might quietly present itself.",
        "damaged": "The years had taught her to distrust grand plans and "
                   "to trust small motions instead, and so she did not "
                   "try to picture the whole of the day ahead, she would "
                   "walk to the town, and see what the town had to show "
                   "her, and let the next thing follow from the last, and "
                   "somewhere in all that ordinary forward motion the "
                   "answer she needed might quietly present itself.",
    },
    {
        "id": "D10-subject-verb",
        "paragraph": 12,
        "damage_type": "subject_verb_disagreement",
        "clean": "But she did not turn.",
        "damaged": "But she do not turn.",
    },
]

_DAMAGE_BY_PARA = {d["paragraph"]: d for d in DAMAGE}
assert len(_DAMAGE_BY_PARA) == len(DAMAGE), "duplicate paragraph in DAMAGE"

# tiny_novel.docx's Chapter One carries its own planted mechanical error
# (E1, "the the" in paragraph 2) — inherited here because bad_novel.docx is
# built from that same source text. It is not part of THIS fixture's rewrite
# damage, and left in place it is a confound: the deterministic doubled-word
# sweep that runs even at $0 (see docproof.replay.zero_paid_passes — it only
# silences PAID stages) fixes it on its own before any correction row is
# ever considered, quietly changing the measured edit counts underneath
# whatever a test submits. Scrubbed here so bad_novel.docx's only defects are
# the ones DAMAGE describes.
_BASE_FIXUPS: dict[int, tuple[str, str]] = {
    2: ("the the window", "the window"),
}


def _apply(text: str, clean: str, damaged: str) -> str:
    """`text` with `clean` replaced by `damaged`, failing loudly (rather than
    silently no-op'ing) if `clean` is not exactly the substring it claims to
    be — the same discipline the fixture's manifest tests hold tiny_novel's
    planted errors to."""
    if text.count(clean) != 1:
        raise ValueError(f"expected exactly one occurrence of {clean!r} in "
                         f"{text!r}")
    return text.replace(clean, damaged, 1)


def paragraph_texts() -> list[tuple[str, str]]:
    """The bad-novel manuscript as (text, style) pairs — one heading and the
    twelve body paragraphs of tiny_novel.docx's Chapter One, ten of them
    damaged. Pure data: two calls are always equal."""
    out: list[tuple[str, str]] = [(CHAPTER_ONE_HEADING, HEADING_STYLE)]
    for i, para in enumerate(CHAPTER_ONE_BODY, start=1):
        if i in _BASE_FIXUPS:
            before, after = _BASE_FIXUPS[i]
            para = _apply(para, before, after)
        d = _DAMAGE_BY_PARA.get(i)
        text = _apply(para, d["clean"], d["damaged"]) if d else para
        out.append((text, BODY_STYLE))
    return out


def build() -> "docx.document.Document":
    """Build (in memory) the bad-novel Document with pinned core properties."""
    d = docx.Document()
    for text, style in paragraph_texts():
        d.add_paragraph(text, style=style)
    props = d.core_properties
    props.author = "DocProof Galley Fixtures"
    props.title = "A Bad Novel"
    props.created = _EPOCH
    props.modified = _EPOCH
    props.revision = 1
    return d


def manifest() -> list[dict]:
    """The answer key: what a proofread would find (`quote`, the damaged text
    as it reads in bad_novel.docx) and how it would fix it (`correction`, the
    clean text) — same shape as tiny_novel.manifest.json's entries."""
    return [{"id": d["id"], "damage_type": d["damage_type"],
             "paragraph": d["paragraph"], "quote": d["damaged"],
             "correction": d["clean"]} for d in DAMAGE]


def write() -> None:
    """Write the .docx and the manifest to disk next to this file."""
    build().save(DOCX)
    MANIFEST_PATH.write_text(
        json.dumps(manifest(), indent=2, ensure_ascii=True) + "\n",
        encoding="ascii",
    )


if __name__ == "__main__":
    write()
    words = sum(len(t.split()) for t, _ in paragraph_texts())
    print(f"wrote {DOCX.name} ({words} words, {len(CHAPTER_ONE_BODY)} body "
          f"paragraph(s), {len(DAMAGE)} damaged) and {MANIFEST_PATH.name}")
