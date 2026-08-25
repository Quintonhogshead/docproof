from lxml import etree

from docproof.config import Config
from docproof.ingest import build_document_model, preflight
from docproof.models import Anchor, Finding
from docproof.reassembler import (annotate_excluded_words,
                                  apply_tracked_changes, paragraph_view_text)
from docproof.utils.xml_helpers import (MC_NS, DocxPackage, iter_text_elements,
                                        paragraph_text, walk_package)
from .conftest import FIXTURES

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _w(t):
    return f"{{{_W}}}{t}"


def _mc(t):
    return f"{{{MC_NS}}}{t}"


def test_reject_view_ignores_textbox_and_fallback_like_the_baseline():
    """A cover title in an mc:AlternateContent — a text box in the Choice, a
    duplicate in the Fallback — is not this paragraph's canonical text. The
    accept/reject views must agree with iter_text_elements (which skips both),
    or the reject-all audit fails a title page nothing touched: the baseline
    reads empty while the view reads the title twice."""
    p = etree.Element(_w("p"))
    alt = etree.SubElement(p, _mc("AlternateContent"))
    txbx = etree.SubElement(etree.SubElement(alt, _mc("Choice")),
                            _w("txbxContent"))
    r1 = etree.SubElement(txbx, _w("r"))
    etree.SubElement(r1, _w("t")).text = "Witch in the Wall"
    fb = etree.SubElement(alt, _mc("Fallback"))
    r2 = etree.SubElement(fb, _w("r"))
    etree.SubElement(r2, _w("t")).text = "Witch in the Wall "

    assert list(iter_text_elements(p)) == []          # canonical text is empty
    assert paragraph_text(p) == ""
    assert paragraph_view_text(p, "reject") == ""      # was the doubled title
    assert paragraph_view_text(p, "accept") == ""


_BOX = "Beware the dog, it bites without warning."


def test_edit_inside_a_textbox_roundtrips_without_a_shape_comment(tmp_path, cfg):
    """A comma splice inside a floating text box is corrected like any other:
    the fix lands as a tracked change, reject restores the original exactly (the
    audit's parity), and — because a comment anchored inside a shape is
    unreliable in Word — no margin note is written even with comments on, so no
    comments part is created at all."""
    cfg = cfg.model_copy(update={"comments": True})
    pkg = preflight(FIXTURES / "textbox.docx", "abort")
    doc = build_document_model(pkg, cfg)

    pid = "body-0001-tb0-p0"
    ref = next(p for p in doc.paragraphs if p.para_id == pid)
    assert ref.location == "textbox" and ref.reviewable and ref.text == _BOX

    assert _BOX[14:18] == ", it"                    # the splice, to ". It"
    f = Finding("f-1", "chunk-000", pid, "comma_splice", _BOX, 1,
                "A comma splice; a period separates the clauses.", "test",
                "high", status="validated", anchor=Anchor(14, 18, ", it", ". It"))
    stats = apply_tracked_changes(pkg, doc, [f], cfg)
    assert stats.applied == ("f-1",) and not stats.skipped

    out = tmp_path / "textbox_reviewed.docx"
    pkg.save(out)
    reloaded = DocxPackage(out)
    p = _para(reloaded, pid)
    assert paragraph_view_text(p, "accept") == "Beware the dog. It bites without warning."
    assert paragraph_view_text(p, "reject") == _BOX
    assert not reloaded.has("word/comments.xml")     # no comment inside the shape


ORIG = "It was late, we were tired, the road went on."


def _para(pkg, pid):
    return {wp.para_id: wp.element for wp in walk_package(pkg)}[pid]


def _body(doc):
    """The body paragraph in styled.docx. It opens with a chapter heading,
    which is now ingested (swept, not modelled), so it is no longer the first
    paragraph in the model."""
    return next(p.para_id for p in doc.paragraphs if p.text == ORIG)


def _validated(pid, start, end, deleted, inserted, i=1):
    return Finding(f"f-{i}", "chunk-000", pid, "comma_splice",
                   ORIG, 1, "", "test", "high", status="validated",
                   anchor=Anchor(start, end, deleted, inserted))


def test_roundtrip_across_formatting_boundary(tmp_path, cfg):
    pkg = preflight(FIXTURES / "styled.docx", "abort")
    doc = build_document_model(pkg, cfg)
    pid = _body(doc)
    # ", w" -> ". W": deletion straddles the roman/bold run boundary
    f = _validated(pid, 11, 14, ", w", ". W")
    stats = apply_tracked_changes(pkg, doc, [f], cfg)
    assert stats.applied == ("f-1",) and not stats.skipped

    out = tmp_path / "styled_reviewed.docx"
    pkg.save(out)
    p = _para(DocxPackage(out), pid)           # reload from disk — the real test
    assert paragraph_view_text(p, "reject") == ORIG
    assert paragraph_view_text(p, "accept") == \
        "It was late. We were tired, the road went on."


def test_an_insertion_and_a_deletion_at_the_same_offset_both_apply(tmp_path,
                                                                   cfg):
    """The validator lets a pure insertion coexist with a deletion starting at
    the same offset. Applied insertion-first — which a start-only sort did
    whenever the insertion came first in the findings list — the inserted text
    sat under the deletion's slice re-check and a validated edit was silently
    dropped from the document."""
    pkg = preflight(FIXTURES / "styled.docx", "abort")
    doc = build_document_model(pkg, cfg)
    pid = _body(doc)
    edits = [_validated(pid, 13, 13, "", "so ", 1),   # the losing order
             _validated(pid, 13, 16, "we ", "", 2)]
    stats = apply_tracked_changes(pkg, doc, edits, cfg)
    assert set(stats.applied) == {"f-1", "f-2"} and not stats.skipped
    p = _para(pkg, pid)
    assert paragraph_view_text(p, "reject") == ORIG
    assert paragraph_view_text(p, "accept") == \
        "It was late, so were tired, the road went on."


def test_two_edits_one_paragraph_descending(tmp_path, cfg):
    pkg = preflight(FIXTURES / "styled.docx", "abort")
    doc = build_document_model(pkg, cfg)
    pid = _body(doc)
    edits = [_validated(pid, 11, 12, ",", ";", 1),
             _validated(pid, 26, 27, ",", ";", 2)]
    apply_tracked_changes(pkg, doc, edits, cfg)
    p = _para(pkg, pid)
    assert paragraph_view_text(p, "reject") == ORIG
    assert paragraph_view_text(p, "accept") == \
        "It was late; we were tired; the road went on."


# --- the top-of-document "excluded from spell-check" note ---------------------

def test_excluded_words_note_is_a_comment_at_the_top(tmp_path, cfg):
    pkg = preflight(FIXTURES / "styled.docx", "abort")
    doc = build_document_model(pkg, cfg)
    placed = annotate_excluded_words(
        pkg, doc, ["Vorrenth", "Kaelith", "accross"],
        "Atmosphere Press Proofreader")
    assert placed is True

    out = tmp_path / "annotated.docx"
    pkg.save(out)
    re_pkg = DocxPackage(out)
    # The comment part exists and carries the note, every word named...
    body = "".join(re_pkg.tree("word/comments.xml").itertext())
    assert "spell-check" in body
    for w in ("Vorrenth", "Kaelith", "accross"):
        assert w in body
    # ...listed alphabetically, case-insensitively, whatever order they came in.
    assert body.index("accross") < body.index("Kaelith") < body.index("Vorrenth")
    # ...and it is anchored in the body exactly once.
    doc_xml = re_pkg.tree("word/document.xml")
    assert len(list(doc_xml.iter(_w("commentReference")))) == 1


def test_excluded_words_note_is_skipped_when_there_is_nothing_to_say(tmp_path,
                                                                     cfg):
    pkg = preflight(FIXTURES / "styled.docx", "abort")
    doc = build_document_model(pkg, cfg)
    assert annotate_excluded_words(pkg, doc, [], "Proofreader") is False
    assert not pkg.has("word/comments.xml")


# --- two-author output (the merge desk's Finding.lane / Config.lane_authors) --

_ORIG2 = "It was late, we were tired, the road went on."


def _body2(doc):
    return next(p.para_id for p in doc.paragraphs if p.text == _ORIG2)


def test_untagged_findings_write_the_single_revision_author_unchanged(tmp_path,
                                                                       cfg):
    """No lane on a finding (every finding outside a merged run) must write
    exactly what it always did: one author, `revision_author`. This is the
    byte-identical guarantee the merge desk's addition to Finding/Config must
    not disturb."""
    pkg = preflight(FIXTURES / "styled.docx", "abort")
    doc = build_document_model(pkg, cfg)
    pid = _body2(doc)
    f = Finding("f-1", "chunk-000", pid, "comma_splice", _ORIG2, 1, "",
               "test", "high", status="validated",
               anchor=Anchor(11, 14, ", w", ". W"))
    assert f.lane == ""
    stats = apply_tracked_changes(pkg, doc, [f], cfg)
    assert stats.applied == ("f-1",)

    out = tmp_path / "single_author.docx"
    pkg.save(out)
    body = DocxPackage(out).tree("word/document.xml")
    authors = {el.get(_w("author")) for el in body.iter(_w("ins"))}
    assert authors == {cfg.revision_author}


def test_two_lane_findings_write_two_tracked_change_authors(tmp_path, cfg):
    pkg = preflight(FIXTURES / "styled.docx", "abort")
    doc = build_document_model(pkg, cfg)
    pid = _body2(doc)
    mech = Finding("f-1", "chunk-000", pid, "comma_splice", _ORIG2, 1, "",
                   "A mechanical fix.", "high", status="validated",
                   anchor=Anchor(11, 14, ", w", ". W"), lane="mechanical")
    ce = Finding("f-2", "chunk-000", pid, "rewrite", _ORIG2, 1, "",
                "A copy-edit rewrite.", "high", status="validated",
                anchor=Anchor(26, 27, ",", ";"), lane="copyedit")
    stats = apply_tracked_changes(pkg, doc, [mech, ce], cfg)
    assert set(stats.applied) == {"f-1", "f-2"}

    out = tmp_path / "two_author.docx"
    pkg.save(out)
    body = DocxPackage(out).tree("word/document.xml")
    ins_by_author: dict[str, int] = {}
    for el in body.iter(_w("ins")):
        a = el.get(_w("author"))
        ins_by_author[a] = ins_by_author.get(a, 0) + 1
    assert set(ins_by_author) == {"Atmosphere Press Proofreader",
                                  "Atmosphere Press Copy Editor"}
    # One author per span — Word cannot stack pending revisions from two
    # authors on the same characters, so each w:ins carries exactly one.
    assert all(el.get(_w("author")) is not None for el in body.iter(_w("ins")))


def test_two_lane_findings_write_two_comment_authors(tmp_path, cfg):
    cfg = cfg.model_copy(update={"comments": True})
    pkg = preflight(FIXTURES / "styled.docx", "abort")
    doc = build_document_model(pkg, cfg)
    pid = _body2(doc)
    mech = Finding("f-1", "chunk-000", pid, "comma_splice", _ORIG2, 1, "",
                   "A mechanical fix.", "high", status="validated",
                   anchor=Anchor(11, 14, ", w", ". W"), lane="mechanical")
    ce = Finding("f-2", "chunk-000", pid, "rewrite", _ORIG2, 1, "",
                "A copy-edit rewrite.", "high", status="validated",
                anchor=Anchor(26, 27, ",", ";"), lane="copyedit")
    apply_tracked_changes(pkg, doc, [mech, ce], cfg)

    out = tmp_path / "two_author_comments.docx"
    pkg.save(out)
    comments = DocxPackage(out).tree("word/comments.xml")
    by_author = {c.get(_w("author")): "".join(c.itertext()) for c in comments}
    assert set(by_author) == {"Atmosphere Press Proofreader",
                              "Atmosphere Press Copy Editor"}
    assert "mechanical fix" in by_author["Atmosphere Press Proofreader"]
    assert "copy-edit rewrite" in by_author["Atmosphere Press Copy Editor"]


def test_lane_authors_config_is_the_only_thing_that_changes_attribution(cfg):
    """A lane with no entry in `lane_authors` (every lane but "copyedit" by
    default) falls back to `revision_author` — including a lane spelled
    "mechanical", which is not itself a key in the default map."""
    assert cfg.lane_authors.get("mechanical", cfg.revision_author) == \
        cfg.revision_author
    assert cfg.lane_authors.get("", cfg.revision_author) == cfg.revision_author
    assert cfg.lane_authors.get("copyedit", cfg.revision_author) == \
        "Atmosphere Press Copy Editor"