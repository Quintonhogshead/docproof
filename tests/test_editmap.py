"""docproof/editmap.py — the accepted<->source translation the settle loop
relies on. Pure functions over findings.json-shaped rows; no document."""
from __future__ import annotations

import pytest

from docproof import editmap as em


def _row(fid, para, start, end, delete, insert, *, original=None,
         corrected=None, occurrence=1, et="spelling", applied=True):
    return {"finding_id": fid, "para_id": para, "error_type": et,
            "original_text": original or delete, "occurrence": occurrence,
            "corrected_text": corrected or insert, "status": "validated",
            "applied": applied,
            "anchor": {"start": start, "end": end, "delete_text": delete,
                       "insert_text": insert}}


SRC = "It was was late, and teh dog slept."
#      0123456789...


def test_paragraph_map_reproduces_the_accepted_text():
    rows = [_row("f-1", "p", 7, 11, "was ", ""),          # drop doubled word
            _row("f-2", "p", 21, 24, "teh", "the")]
    segs = em.paragraph_map(SRC, rows)
    assert em.accepted_of(segs) == "It was late, and the dog slept."
    owners = [s.owner for s in segs]
    assert owners == [None, "f-1", None, "f-2", None]


def test_paragraph_map_refuses_an_anchor_that_lies():
    with pytest.raises(em.EditMapError):
        em.paragraph_map(SRC, [_row("f-1", "p", 7, 11, "xxx ", "")])


def test_locate_untouched_maps_by_arithmetic():
    rows = [_row("f-1", "p", 7, 11, "was ", "")]
    segs = em.paragraph_map(SRC, rows)
    acc = em.accepted_of(segs)                  # "It was late, and teh dog slept."
    lo = acc.index("teh")
    res = em.locate(segs, lo, lo + 3)
    assert res.case == "untouched"
    assert res.source_span == (21, 24)
    assert SRC[21:24] == "teh"


def test_locate_inside_an_owned_replacement_names_the_owner():
    rows = [_row("f-2", "p", 21, 24, "teh", "thee")]       # a bad fix
    segs = em.paragraph_map(SRC, rows)
    acc = em.accepted_of(segs)
    lo = acc.index("thee")
    res = em.locate(segs, lo, lo + 4)
    assert res.case == "inside" and res.owners == ("f-2",)
    assert res.source_widened == (21, 24)


def test_locate_straddle_widens_to_every_segment_of_the_owner():
    # One decision split into two minimal regions (f-3 / f-3b): touching one
    # region must absorb both, or the other region's fix is lost.
    rows = [_row("f-3", "p", 3, 6, "was", "is",
                 original="was was late", corrected="is late"),
            _row("f-3b", "p", 7, 11, "was ", "",
                 original="was was late", corrected="is late")]
    segs = em.paragraph_map(SRC, rows, owner_of=em.owners_index(rows))
    acc = em.accepted_of(segs)
    assert acc == "It is late, and teh dog slept."
    # a residual straddling "is late" -> touches the first region and the gap
    lo = acc.index("is late")
    res = em.locate(segs, lo, lo + len("is la"))
    assert res.case == "straddle"
    assert res.owners == ("f-3",)
    assert set(res.absorbed_rows) == {"f-3", "f-3b"}
    # the residual's own range reaches two chars into the untouched tail
    assert res.source_widened == (3, 13) and SRC[3:13] == "was was la"
    c = em.compose(segs, lo, lo + len("is la"), "was la")
    assert c.text == "was la" and set(c.absorbed) == {"f-3", "f-3b"}


def test_compose_untouched_and_owned():
    rows = [_row("f-2", "p", 21, 24, "teh", "thee")]
    segs = em.paragraph_map(SRC, rows)
    acc = em.accepted_of(segs)
    # untouched: a new edit
    lo = acc.index("was was")
    c = em.compose(segs, lo, lo + len("was was"), "was")
    assert c.case == "untouched" and c.source_span == (3, 10)
    assert c.text == "was" and c.absorbed == ()
    # owned: revise the owner's replacement, absorbing it
    lo = acc.index("thee")
    c = em.compose(segs, lo, lo + 4, "the")
    assert c.case == "inside" and c.absorbed == ("f-2",)
    assert (c.src_start, c.src_end, c.text, c.before) == (21, 24, "the", "thee")


def test_as_row_quotes_the_sentence_and_the_validator_can_shrink_it():
    from docproof.validator import shrink
    rows = [_row("f-2", "p", 21, 24, "teh", "thee")]
    segs = em.paragraph_map(SRC, rows)
    acc = em.accepted_of(segs)
    lo = acc.index("thee")
    c = em.compose(segs, lo, lo + 4, "the")
    row = em.as_row(SRC, c, para_id="p", error_type="galley_settle")
    assert row["original_text"] == "teh" and row["occurrence"] == 1
    assert row["corrected_text"] == "the"
    pre, deleted, inserted = shrink(row["original_text"], row["corrected_text"])
    assert "teh"[:pre] + inserted + "teh"[pre + len(deleted):] == "the"
    # a pure insertion anchors on one character each side
    at = acc.index("dog")
    ins = em.compose(segs, at, at, "old ")
    row = em.as_row(SRC, ins, para_id="p", error_type="galley_settle")
    assert row["original_text"] == " d" and row["corrected_text"] == " old d"


def test_pure_insertion_at_a_replacement_edge_composes_with_the_owner():
    rows = [_row("f-2", "p", 21, 24, "teh", "the")]
    segs = em.paragraph_map(SRC, rows)
    acc = em.accepted_of(segs)
    at = acc.index("the") + 3                    # right after the replacement
    res = em.locate(segs, at, at)
    assert res.owners == ("f-2",)
    c = em.compose(segs, at, at, "n")
    assert c.text == "then" and c.absorbed == ("f-2",)


def test_build_editmap_records_unmappable_paragraphs_instead_of_lying():
    rows = [_row("f-1", "p1", 7, 11, "was ", ""),
            _row("f-9", "p2", 0, 3, "zzz", "")]
    m = em.build_editmap({"p1": SRC, "p2": "Not that."}, rows)
    assert m.accepted("p1") == "It was late, and teh dog slept."
    assert "p2" in m.unmapped and "f-9" in m.unmapped["p2"]


def test_editmap_round_trips_through_json(tmp_path):
    rows = [_row("f-1", "p1", 7, 11, "was ", "")]
    m = em.build_editmap({"p1": SRC}, rows)
    path = m.save(tmp_path / "editmap.json")
    back = em.EditMap.load(path)
    assert back.accepted("p1") == m.accepted("p1")
    assert back.paragraphs["p1"][1].row_ids == ("f-1",)
    # load_or_build trusts the file when it describes these rows...
    assert em.load_or_build(tmp_path, {"p1": SRC}, rows).accepted("p1") \
        == m.accepted("p1")
    # ...and rebuilds when the rows moved on
    fresh = em.load_or_build(tmp_path, {"p1": SRC},
                             [_row("f-7", "p1", 21, 24, "teh", "the")])
    assert fresh.accepted("p1") == "It was was late, and the dog slept."


def test_edit_rows_skips_queries_marks_and_rejections():
    rows = [_row("f-1", "p", 7, 11, "was ", ""),
            {**_row("f-2", "p", 21, 24, "teh", "the"), "queried": True,
             "applied": False},
            {**_row("f-3", "p", 21, 24, "teh", "teh"), "format": "italic"},
            {**_row("f-4", "p", 21, 24, "teh", "the"), "applied": False,
             "status": "rejected_overlap"}]
    assert [r["finding_id"] for r in em.edit_rows(rows)] == ["f-1"]


def test_base_id_and_row_key():
    assert em.base_id("f-0031b") == "f-0031"
    assert em.base_id("f-0031") == "f-0031"
    assert em.base_id("sw-12") == "sw-12"
    a = _row("f-1", "p", 0, 1, "a", "b", original="a cat", corrected="b cat")
    b = _row("f-1b", "p", 0, 1, "a", "b", original="a cat", corrected="b cat")
    assert em.row_key(a) == em.row_key(b)


def test_widening_covers_an_owners_trailing_deletion():
    # One owner, two regions: a replacement and, after untouched " you", a
    # pure deletion of "can begin to " (Redding body-0290-tb0-p0).
    src = " A vision board is a venue where you can begin to identify"
    rows = [_row("fl-1", "p", 16, 32, "is a venue where", "lets",
                 original=src, corrected=" A vision board lets you identify"),
            _row("fl-1b", "p", 37, 50, "can begin to ", "",
                 original=src, corrected=" A vision board lets you identify")]
    segs = em.paragraph_map(src, rows, owner_of=em.owners_index(rows))
    acc = em.accepted_of(segs)
    assert acc == " A vision board lets you identify"
    res = em.locate(segs, 0, len(" A vision board lets you"))
    assert res.owners == ("fl-1",)
    assert res.source_widened == (0, 50)          # includes the deletion
    c = em.compose(segs, 0, len(" A vision board lets you"), "A vision board lets you")
    row = em.as_row(src, c, para_id="p", error_type="galley_settle")
    assert row["original_text"] == src[:50]
    # the composite runs through the deletion, so it keeps the space that
    # precedes "identify"
    assert row["corrected_text"] == "A vision board lets you "
    # applying the row reproduces the intended accepted text exactly
    assert row["corrected_text"] + src[50:] == "A vision board lets you identify"


def test_build_editmap_tolerates_an_overlapping_sub_row_pair():
    # Redding f-0233 / f-0233b: the base row is region 1 (a comma inserted at
    # 1072); the "b" row carries the WHOLE diff from the same start. The
    # reassembler applied one; the tolerant map keeps the first and records
    # the other as skipped, and the caller's drift check decides.
    src = "Bodhi you know what I am talking about my love."
    rows = [_row("f-1", "p", 5, 5, "", ",",
                 original=src, corrected="Bodhi, you know what I am talking about, my love."),
            _row("f-1b", "p", 5, 38, " you know what I am talking about",
                 ", you know what I am talking about,",
                 original=src, corrected="Bodhi, you know what I am talking about, my love.")]
    m = em.build_editmap({"p": src}, rows)
    assert "p" not in m.unmapped
    assert m.skipped["p"] == ["f-1b"]
    assert m.accepted("p") == "Bodhi, you know what I am talking about my love."

