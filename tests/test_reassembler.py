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