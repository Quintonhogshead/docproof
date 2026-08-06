from docproof.config import Config
from docproof.ingest import build_document_model, preflight
from docproof.models import Anchor, Finding
from docproof.reassembler import apply_tracked_changes, paragraph_view_text
from docproof.utils.xml_helpers import DocxPackage, walk_package
from .conftest import FIXTURES

ORIG = "It was late, we were tired, the road went on."


def _para(pkg, pid):
    return {wp.para_id: wp.element for wp in walk_package(pkg)}[pid]


def _validated(pid, start, end, deleted, inserted, i=1):
    return Finding(f"f-{i}", "chunk-000", pid, "comma_splice",
                   ORIG, 1, "", "test", "high", status="validated",
                   anchor=Anchor(start, end, deleted, inserted))


def test_roundtrip_across_formatting_boundary(tmp_path, cfg):
    pkg = preflight(FIXTURES / "styled.docx", "abort")
    doc = build_document_model(pkg, cfg)
    pid = doc.paragraphs[0].para_id            # the heading was skipped
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
    pid = doc.paragraphs[0].para_id
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
    pid = doc.paragraphs[0].para_id
    edits = [_validated(pid, 11, 12, ",", ";", 1),
             _validated(pid, 26, 27, ",", ";", 2)]
    apply_tracked_changes(pkg, doc, edits, cfg)
    p = _para(pkg, pid)
    assert paragraph_view_text(p, "reject") == ORIG
    assert paragraph_view_text(p, "accept") == \
        "It was late; we were tired; the road went on."