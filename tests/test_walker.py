from docproof.utils.xml_helpers import DocxPackage, walk_package, paragraph_text
from .conftest import FIXTURES


def test_ids_deterministic():
    pkg = DocxPackage(FIXTURES / "table.docx")
    first = [(wp.para_id, paragraph_text(wp.element)) for wp in walk_package(pkg)]
    second = [(wp.para_id, paragraph_text(wp.element)) for wp in walk_package(pkg)]
    assert first == second


def test_table_and_footnote_ids():
    ids = {wp.para_id for wp in walk_package(DocxPackage(FIXTURES / "table.docx"))}
    assert "body-0000" in ids and "table-0-r0-c0-p0" in ids

    wps = {wp.para_id: wp for wp in
           walk_package(DocxPackage(FIXTURES / "footnotes.docx"))}
    assert wps["footnote-2-p0"].location == "footnote"
    assert "implications" in paragraph_text(wps["footnote-2-p0"].element)