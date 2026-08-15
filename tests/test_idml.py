"""IDML walker, ingest and reassembler.

The fixtures are real InDesign 2026 exports rather than hand-built XML, and
they are checked in because regenerating them needs InDesign on the machine —
tests/fixtures/make_idml.jsx is the script that produced them. docs/idml-notes.md
records the shapes they proved.
"""
from __future__ import annotations

import zipfile

import pytest

from docproof.formats import get_format
from docproof.formats.idml.ingest import build_document_model, preflight
from docproof.formats.idml.reassembler import (apply_tracked_changes,
                                               paragraph_view_text)
from docproof.formats.idml.walker import (IdmlPackage, paragraph_text,
                                          walk_package)
from docproof.ingest import IngestError
from docproof.models import Finding
from docproof.validator import validate_findings

from .conftest import FIXTURES

LAYOUT = FIXTURES / "layout.idml"
TRACKED = FIXTURES / "layout_tracked.idml"

# Text of the fixture, so the tests read as prose rather than as offsets.
SPLICE = "It was late, we were tired and the road went on forever."
DOOR = "She opened the door, the room was empty."
CELL = "First cell has a sentence in it, it runs on."


def finding(fid, para_id, original, corrected, error="comma_splice"):
    return Finding(fid, "chunk-000", para_id, error, original, 1, corrected,
                   "Because.", "high")


def texts(pkg):
    return {wp.para_id: paragraph_text(wp.contents) for wp in walk_package(pkg)}


# --- the walker ---------------------------------------------------------------

def test_walk_is_deterministic():
    pkg = IdmlPackage(LAYOUT)
    first = [(wp.para_id, paragraph_text(wp.contents)) for wp in walk_package(pkg)]
    second = [(wp.para_id, paragraph_text(wp.contents)) for wp in walk_package(pkg)]
    assert first == second
    assert first, "fixture should contain paragraphs"


def test_paragraph_spans_several_style_ranges():
    """The fixture's second paragraph is split across five CharacterStyleRanges
    by italic and point-size runs. Canonical text must stitch it back together
    — this is the property the whole anchoring scheme rests on."""
    assert texts(IdmlPackage(LAYOUT))["story-ue0-p0001"] == SPLICE


def test_paragraph_boundaries_come_from_br_not_elements():
    """One CharacterStyleRange holds three paragraphs in the fixture; a Br
    terminates a paragraph rather than separating two, so no phantom empty
    paragraph appears at the end of a style range."""
    found = texts(IdmlPackage(LAYOUT))
    assert found["story-ue0-p0002"] == DOOR
    assert found["story-ue0-p0004"] == "Their were several mistakes here to find."
    assert "story-ue0-p0005" not in found


def test_table_cells_walk_in_reading_order():
    found = texts(IdmlPackage(LAYOUT))
    assert found["story-uff-tbl0-r0-c0-p0000"] == CELL
    assert found["story-uff-tbl0-r0-c1-p0000"] == "Second cell"
    assert found["story-uff-tbl0-r1-c0-p0000"] == "Third cell"


def test_footnote_text_is_found_by_the_walker():
    assert texts(IdmlPackage(LAYOUT))["story-ue0-fn0-p0000"].startswith(
        "A footnote with its own sentence")


def test_style_name_is_stripped_of_its_prefix():
    """"ParagraphStyle/Chapter Head" -> "Chapter Head", so skip globs can be
    written against the name shown in InDesign's Styles panel, and "$ID/" —
    InDesign's marker for a built-in localized name — never leaks out."""
    styles = {wp.para_id: wp.style for wp in walk_package(IdmlPackage(LAYOUT))}
    assert styles["story-ue0-p0000"] == "Chapter Head"
    assert styles["story-ue0-p0001"] == "NormalParagraphStyle"


def test_skip_styles_match_indesign_style_names(cfg):
    cfg.skip.styles = ["Chapter*"]
    doc = build_document_model(IdmlPackage(LAYOUT), cfg)
    assert ("story-ue0-p0000", "style:Chapter Head") in doc.skipped


# --- ingest -------------------------------------------------------------------

def test_preflight_rejects_a_non_idml(tmp_path):
    bogus = tmp_path / "notreally.idml"
    bogus.write_text("this is not a zip", encoding="utf-8")
    with pytest.raises(IngestError, match="signature"):
        preflight(bogus, "abort")


def test_preflight_names_idml_export_for_a_native_indd(tmp_path):
    indd = tmp_path / "book.idml"
    indd.write_bytes(b"\x06\x06\xed\xf5\xd8\x1d\x46\xe5DOCUMENT" + b"\x00" * 64)
    with pytest.raises(IngestError, match="Export"):
        preflight(indd, "abort")


def test_preflight_rejects_a_zip_that_is_not_a_package(tmp_path):
    path = tmp_path / "empty.idml"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("hello.txt", "nope")
    with pytest.raises(IngestError, match="designmap"):
        preflight(path, "abort")


def test_existing_tracked_changes_abort_by_default():
    with pytest.raises(IngestError, match="already contains tracked changes"):
        preflight(TRACKED, "abort")


def test_accept_all_first_resolves_existing_changes(cfg):
    """The fixture was exported with an insertion, a deletion and a
    replacement pending. Accepting them must leave the insertions in and take
    the deletions out."""
    pkg = preflight(TRACKED, "accept_all_first")
    found = texts(pkg)
    assert "VERYNEW" in found["story-ue0-p0003"]          # insertion kept
    assert not found["story-ue0-p0004"].startswith("Their")  # deletion dropped
    doc = build_document_model(pkg, cfg)
    assert doc.paragraphs


def test_ignore_policy_reads_the_accepted_view(cfg):
    pkg = preflight(TRACKED, "ignore")
    assert "VERYNEW" in texts(pkg)["story-ue0-p0003"]


def test_footnotes_are_skipped_with_a_reason(cfg):
    """InDesign cannot carry a tracked change inside a footnote, so docproof
    must not spend tokens reviewing one."""
    doc = build_document_model(IdmlPackage(LAYOUT), cfg)
    reasons = dict(doc.skipped)
    assert reasons["story-ue0-fn0-p0000"] == "footnote:tracked-changes-unsupported"
    assert all(p.location != "footnote" for p in doc.paragraphs)


# --- the reassembler ----------------------------------------------------------

def _apply(cfg, findings, tmp_path, comments=True, query_types=frozenset()):
    cfg.comments = comments
    cfg.revision_author = "docproof"
    pkg = preflight(LAYOUT, "abort")
    doc = build_document_model(pkg, cfg)
    validated = validate_findings(list(findings), doc, cfg.min_confidence,
                                  query_types=query_types)
    stats = apply_tracked_changes(pkg, doc, validated, cfg)
    out = tmp_path / "reviewed.idml"
    pkg.save(out)
    return stats, doc, IdmlPackage(out)


def test_accept_and_reject_views_are_both_exact(cfg, tmp_path):
    """The invariant the whole reassembler exists to keep: accepting every
    change yields the corrected text, rejecting every change yields byte-exact
    original text."""
    findings = [
        finding("f-1", "story-ue0-p0001", SPLICE, SPLICE.replace("late,", "late;")),
        finding("f-2", "story-ue0-p0002", DOOR, DOOR.replace("door,", "door;")),
        finding("f-3", "story-uff-tbl0-r0-c0-p0000", CELL,
                CELL.replace("it,", "it;")),
    ]
    stats, doc, after = _apply(cfg, findings, tmp_path)
    assert len(stats.applied) == 3 and not stats.skipped

    before = {p.para_id: p.text for p in doc.paragraphs}
    corrected = {f.para_id: f.corrected_text for f in findings}
    for wp in walk_package(after):
        if wp.para_id not in before:
            continue
        # The walker reads the accepted view, so this is the accept invariant.
        assert paragraph_text(wp.contents) == corrected.get(
            wp.para_id, before[wp.para_id])

    # And rejecting must give the original wording back.
    for wp in walk_package(after):
        if wp.para_id in corrected:
            rejected = paragraph_view_text(wp.psr, "reject")
            assert before[wp.para_id] in rejected


def test_changes_land_where_indesign_expects_them(cfg, tmp_path):
    _, _, after = _apply(cfg, [finding("f-1", "story-ue0-p0002", DOOR,
                                       DOOR.replace("door,", "door;"))],
                         tmp_path, comments=False)
    story_part = [p for p in after.story_parts() if p.endswith("ue0.xml")][0]
    changes = list(after.tree(story_part).iter("Change"))
    kinds = [c.get("ChangeType") for c in changes]
    assert kinds == ["InsertedText", "DeletedText"]
    # An insertion holds Content directly; a deletion carries its own
    # ParagraphStyleRange/CharacterStyleRange. Getting this backwards produces
    # a file InDesign refuses to open.
    assert changes[0].find("Content") is not None
    assert changes[1].find("ParagraphStyleRange/CharacterStyleRange/Content") is not None
    assert all(c.get("UserName") == "docproof" for c in changes)
    assert after.story(story_part).get("TrackChanges") == "true"


def test_explanations_become_notes(cfg, tmp_path):
    _, _, after = _apply(cfg, [finding("f-1", "story-ue0-p0002", DOOR,
                                       DOOR.replace("door,", "door;"))],
                         tmp_path, comments=True)
    story_part = [p for p in after.story_parts() if p.endswith("ue0.xml")][0]
    notes = list(after.tree(story_part).iter("Note"))
    assert len(notes) == 1
    assert notes[0].findtext("ParagraphStyleRange/CharacterStyleRange/Content") \
        == "Because."


def test_notes_do_not_disturb_canonical_text(cfg, tmp_path):
    """A Note sits inline beside the change. If the walker counted it as text
    every anchor after it would shift."""
    _, doc, after = _apply(cfg, [finding("f-1", "story-ue0-p0002", DOOR,
                                         DOOR.replace("door,", "door;"))],
                           tmp_path, comments=True)
    assert texts(after)["story-ue0-p0004"] == \
        "Their were several mistakes here to find."


# --- the query channel --------------------------------------------------------
#
# The other half of the deliverable (docs/deliverables.md): a question that
# edits nothing. It reaches an .idml as an inline Note, the same element an
# explanation uses — for a long time it reached one as nothing at all, while
# every surface still counted it.

SPEAKER = frozenset({"speaker_change"})
FRAGMENT = "the room was empty."          # a sentence inside DOOR, not all of it


def query(fid, para_id, sentence, explanation="Who is speaking here?"):
    """A query-only type: it repeats its sentence rather than correcting it."""
    return Finding(fid, "chunk-000", para_id, "speaker_change", sentence, 1,
                   sentence, explanation, "high")


def notes_in(pkg):
    part = [p for p in pkg.story_parts() if p.endswith("ue0.xml")][0]
    return [n.findtext("ParagraphStyleRange/CharacterStyleRange/Content")
            for n in pkg.tree(part).iter("Note")]


def note_before(pkg, para_id, offset):
    """The Note anchored immediately before the character at `offset`.

    An InDesign Note is a point, not a range, so where the marker sits is the
    whole of what the author is shown — worth asserting on rather than trusting.
    """
    wp = next(w for w in walk_package(pkg) if w.para_id == para_id)
    off = 0
    for node in wp.contents:
        if off == offset:
            prev = node.getprevious()
            if prev is not None and prev.tag == "Note":
                return prev.findtext(
                    "ParagraphStyleRange/CharacterStyleRange/Content")
            return None
        off += len(node.text or "")
    return None


def test_a_query_becomes_a_note(cfg, tmp_path):
    """The headline: an .idml carries its questions. Reporting "3 questions in
    the margins" over a file that holds none is the bug this exists to stop."""
    stats, _, after = _apply(cfg, [query("q-1", "story-ue0-p0002", DOOR)],
                             tmp_path, query_types=SPEAKER)
    assert stats.queried == ("q-1",)
    assert stats.applied == () and not stats.unplaced
    assert notes_in(after) == ["Who is speaking here?"]


def test_a_query_changes_nothing(cfg, tmp_path):
    """A query's anchor spans its whole sentence with nothing to insert. Let it
    reach the change loop and it deletes that sentence — so the accepted and
    rejected views must both still read exactly as the author wrote it."""
    _, doc, after = _apply(cfg, [query("q-1", "story-ue0-p0002", DOOR)],
                           tmp_path, query_types=SPEAKER)
    before = {p.para_id: p.text for p in doc.paragraphs}
    wp = next(w for w in walk_package(after) if w.para_id == "story-ue0-p0002")
    assert paragraph_text(wp.contents) == before["story-ue0-p0002"]
    assert paragraph_view_text(wp.psr, "reject").count(DOOR) == 1
    assert not list(after.tree(
        [p for p in after.story_parts() if p.endswith("ue0.xml")][0]
    ).iter("Change"))


def test_a_query_hangs_at_the_start_of_the_sentence_it_asks_about(cfg, tmp_path):
    """A Word comment highlights the range; a Note cannot, so it goes on the
    first character of the sentence rather than wherever the anchor happened to
    land."""
    _, _, after = _apply(cfg, [query("q-1", "story-ue0-p0002", FRAGMENT)],
                         tmp_path, query_types=SPEAKER)
    assert note_before(after, "story-ue0-p0002", DOOR.index(FRAGMENT)) \
        == "Who is speaking here?"


def test_a_query_is_written_even_with_comments_off(cfg, tmp_path):
    """`comments` governs the explanation hung off a correction. A query type
    has no other channel — asking is its entire output — so switching comments
    off must not silently discard the question."""
    stats, _, after = _apply(cfg, [query("q-1", "story-ue0-p0002", DOOR)],
                             tmp_path, comments=False, query_types=SPEAKER)
    assert stats.queried == ("q-1",)
    assert notes_in(after) == ["Who is speaking here?"]


def test_a_query_and_a_change_in_one_paragraph_both_land(cfg, tmp_path):
    """Notes go on first because they insert no text. Applied the other way
    round, the deletion moves Content out of the paragraph and the note lands
    against a stale offset."""
    stats, _, after = _apply(
        cfg,
        [query("q-1", "story-ue0-p0002", FRAGMENT),
         finding("f-1", "story-ue0-p0002", DOOR, DOOR.replace("door,", "door;"))],
        tmp_path, query_types=SPEAKER)
    assert stats.queried == ("q-1",) and stats.applied == ("f-1",)
    wp = next(w for w in walk_package(after) if w.para_id == "story-ue0-p0002")
    assert paragraph_text(wp.contents) == DOOR.replace("door,", "door;")
    assert paragraph_view_text(wp.psr, "reject").count(DOOR) == 1
    assert note_before(after, "story-ue0-p0002", DOOR.index(FRAGMENT)) \
        == "Who is speaking here?"


def test_a_below_gate_finding_becomes_a_note_carrying_its_suggestion(cfg, tmp_path):
    """With query_comments on, a change the model was not confident enough to
    make is a question instead — and says what it would have done."""
    cfg.query_comments = True
    low = Finding("l-1", "chunk-000", "story-ue0-p0002", "comma_splice", DOOR,
                  1, DOOR.replace("door,", "door;"), "Two clauses.", "low")
    stats, _, after = _apply(cfg, [low], tmp_path)
    assert stats.queried == ("l-1",) and stats.applied == ()
    note = notes_in(after)[0]
    assert note.startswith("Possibly comma splice — left as written")
    assert "Suggested: " + DOOR.replace("door,", "door;") in note


def test_query_comments_off_keeps_a_below_gate_finding_out_of_the_file(cfg, tmp_path):
    """The switch still means what it says: these stay in summary.md, where
    only whoever ran the review sees them. A query *type* is unaffected."""
    cfg.query_comments = False
    low = Finding("l-1", "chunk-000", "story-ue0-p0002", "comma_splice", DOOR,
                  1, DOOR.replace("door,", "door;"), "Two clauses.", "low")
    stats, _, after = _apply(cfg, [low], tmp_path)
    assert stats.queried == () and notes_in(after) == []


def test_a_query_in_a_footnote_is_refused(cfg, tmp_path):
    """The same defense in depth the changes get. A Note is not the element
    InDesign crashes on, but footnote markup is unverified past that crash and
    a question is not worth being the thing that tests it."""
    cfg.comments = False
    pkg = preflight(LAYOUT, "abort")
    doc = build_document_model(pkg, cfg)
    from docproof.models import Anchor, ParagraphRef
    fn_text = "A footnote with its own sentence, it also runs on."
    doc = type(doc)(source_path=doc.source_path,
                    paragraphs=doc.paragraphs + (ParagraphRef(
                        "story-ue0-fn0-p0000", "Stories/Story_ue0.xml",
                        "footnote", fn_text, "NormalParagraphStyle"),),
                    skipped=doc.skipped)
    q = query("q-x", "story-ue0-fn0-p0000", fn_text)
    q = type(q)(**{**q.__dict__, "status": "query",
                   "anchor": Anchor(0, len(fn_text), fn_text, "")})
    stats = apply_tracked_changes(pkg, doc, [q], cfg)
    # A question that never reached the file is unplaced, not skipped: no
    # correction was withheld.
    assert stats.queried == () and stats.unplaced == ("q-x",)
    assert stats.skipped == ()
    assert not list(pkg.tree("Stories/Story_ue0.xml").iter("Note"))


def test_a_query_with_no_anchor_is_counted_as_unplaced_not_dropped(cfg,
                                                                   tmp_path):
    """A Note is placed from the anchor, so a query without one cannot be
    written — the same loss the .docx side takes, counted the same way rather
    than filtered out where nothing would ever say the question went missing."""
    cfg.comments = False
    pkg = preflight(LAYOUT, "abort")
    doc = build_document_model(pkg, cfg)
    pid = next(p.para_id for p in doc.paragraphs if p.text == DOOR)
    q = query("q-x", pid, DOOR)
    q = type(q)(**{**q.__dict__, "status": "query", "anchor": None})
    stats = apply_tracked_changes(pkg, doc, [q], cfg)
    assert stats.queried == () and stats.unplaced == ("q-x",)
    assert stats.applied == () and stats.skipped == ()
    assert notes_in(pkg) == []


def test_a_finding_in_a_footnote_is_refused(cfg, tmp_path):
    """Defense in depth: ingest already skips footnotes, but a stale document
    model must not be able to write a Change InDesign would crash on."""
    cfg.comments = False
    pkg = preflight(LAYOUT, "abort")
    doc = build_document_model(pkg, cfg)
    from docproof.models import Anchor, ParagraphRef
    fn_text = "A footnote with its own sentence, it also runs on."
    doc = type(doc)(source_path=doc.source_path,
                    paragraphs=doc.paragraphs + (ParagraphRef(
                        "story-ue0-fn0-p0000", "Stories/Story_ue0.xml",
                        "footnote", fn_text, "NormalParagraphStyle"),),
                    skipped=doc.skipped)
    f = finding("f-x", "story-ue0-fn0-p0000", fn_text, fn_text)
    f = type(f)(**{**f.__dict__, "status": "validated",
                   "anchor": Anchor(32, 33, ",", ";")})
    stats = apply_tracked_changes(pkg, doc, [f], cfg)
    assert stats.applied == () and stats.skipped == ("f-x",)


# --- packaging ----------------------------------------------------------------

def test_save_preserves_mimetype_and_processing_instruction(cfg, tmp_path):
    """Two things InDesign will not forgive: a compressed or relocated
    mimetype entry, and a designmap that lost its <?aid?> instruction."""
    _, _, _ = _apply(cfg, [finding("f-1", "story-ue0-p0002", DOOR,
                                   DOOR.replace("door,", "door;"))], tmp_path)
    with zipfile.ZipFile(tmp_path / "reviewed.idml") as z:
        first = z.infolist()[0]
        assert first.filename == "mimetype"
        assert first.compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/vnd.adobe.indesign-idml-package"
        assert z.read("designmap.xml").startswith(
            b'<?xml version=\'1.0\' encoding=\'UTF-8\' standalone=\'yes\'?>\n<?aid ')


def test_untouched_parts_are_copied_byte_for_byte(cfg, tmp_path):
    _apply(cfg, [finding("f-1", "story-ue0-p0002", DOOR,
                         DOOR.replace("door,", "door;"))], tmp_path)
    with zipfile.ZipFile(LAYOUT) as before, \
            zipfile.ZipFile(tmp_path / "reviewed.idml") as after:
        untouched = "Resources/Styles.xml"
        assert before.read(untouched) == after.read(untouched)


# --- dispatch -----------------------------------------------------------------

def test_get_format_dispatches_on_extension():
    assert get_format("book.idml").name == "InDesign"
    assert get_format("book.docx").name == "Word"
    assert get_format("BOOK.IDML").name == "InDesign"


def test_get_format_names_what_it_reads():
    with pytest.raises(IngestError, match=r"\.idml"):
        get_format("notes.pages")


def test_reviewed_name_keeps_the_extension():
    assert get_format("x.idml").reviewed_name("/a/Book Three.idml") \
        == "Book Three - Atmosphere Press Proofreader.idml"
