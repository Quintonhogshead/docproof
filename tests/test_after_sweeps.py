"""P2-11: re-anchor imported rows written against POST-sweep text.

A curated row whose quote was captured after the deterministic sweeps ran (an
en-dash, a lowered am, an added :00) is not in the pre-sweep manuscript, so it
lands as rejected_no_anchor and has to be hand-built as a micro-span. This
resolves it against the swept text and rewrites it to the pre-sweep equivalent —
but only when the row's own edit is disjoint from the sweep, so it can never
compose a garbled third version.
"""
from __future__ import annotations

from docproof.models import Anchor, Finding, ParagraphRef
from docproof.replay import (_minimal_diff, _swept_and_map,
                             reanchor_after_sweeps)


def _para(text: str) -> ParagraphRef:
    return ParagraphRef("body-0000", "word/document.xml", "body", text, "Normal")


def _sweep(start: int, end: int, insert: str, para="body-0000") -> Finding:
    return Finding(finding_id="sw-0001", chunk_id="sweep", para_id=para,
                   error_type="sweep_dash", original_text="x", occurrence=1,
                   corrected_text="y", explanation="", confidence="high",
                   status="validated",
                   anchor=Anchor(start=start, end=end, delete_text="", insert_text=insert))


def test_swept_and_map_tracks_pre_coordinates():
    # "5-6" (pre 6:9) replaced by "5–6"
    swept, sw2pre, inserted = _swept_and_map("I saw 5-6 cats", [(6, 9, "5–6")])
    assert swept == "I saw 5–6 cats"
    assert inserted[7] is True                     # the en-dash is an inserted char
    assert sw2pre[10] == 10                         # 'c' of cats maps back to itself


def test_minimal_diff_trims_shared_affixes():
    # shared prefix "saw 5–6 " AND the trailing "s" are both trimmed
    assert _minimal_diff("saw 5–6 cats", "saw 5–6 kittens") == (8, "cat", "kitten")


def test_row_disjoint_from_a_sweep_is_reanchored_to_pre_sweep_text():
    para = _para("I saw 5-6 cats")
    sweep = _sweep(6, 9, "5–6")            # the hyphen->en-dash sweep
    row = {"para_id": "body-0000",
           "original_text": "saw 5–6 cats",     # captured AFTER the sweep
           "corrected_text": "saw 5–6 kittens"}  # a disjoint word fix
    out, adjusted = reanchor_after_sweeps([row], [para], [sweep])
    assert adjusted == 1
    (r,) = out
    # rewritten to pre-sweep coordinates: anchors in "I saw 5-6 cats"
    assert r["original_text"] == "saw 5-6 cats"
    assert r["corrected_text"] == "saw 5-6 kittens"
    assert r["original_text"] in para.text        # it now anchors pre-sweep


def test_a_row_already_anchoring_pre_sweep_is_left_untouched():
    para = _para("I saw 5-6 cats")
    sweep = _sweep(6, 9, "5–6")
    row = {"para_id": "body-0000", "original_text": "cats",
           "corrected_text": "kittens"}
    out, adjusted = reanchor_after_sweeps([row], [para], [sweep])
    assert adjusted == 0
    assert out[0] is row                            # untouched, same object


def test_a_row_whose_edit_lands_on_the_sweep_is_not_rewritten():
    para = _para("I saw 5-6 cats")
    sweep = _sweep(6, 9, "5–6")
    # the human "fix" IS the en-dash the sweep made — overlaps the sweep, so it
    # must be left for the ordinary arbitration, never disjoint-remapped.
    row = {"para_id": "body-0000", "original_text": "5–6",
           "corrected_text": "5—6"}                 # em-dash, on the sweep's char
    out, adjusted = reanchor_after_sweeps([row], [para], [sweep])
    assert adjusted == 0
    assert out[0] is row
