from docproof.utils.xml_helpers import DocxPackage, walk_package, paragraph_text
from .conftest import FIXTURES


def test_ids_deterministic():
    pkg = DocxPackage(FIXTURES / "table.docx")
    first = [(wp.para_id, paragraph_text(wp.element)) for wp in walk_package(pkg)]
    second = [(wp.para_id, paragraph_text(wp.element)) for wp in walk_package(pkg)]
    assert first == second


def test_textbox_stories_are_walked_choice_only():
    """A text box is a run-anchored story of its own; the walker reads it right
    after the paragraph that anchors it, with a nested id and location
    "textbox" — but only once. The modern box's mc:Fallback repeats its line
    verbatim, and reading that duplicate would edit and count the sentence
    twice, so the walk takes the mc:Choice side and skips the Fallback."""
    wps = {wp.para_id: wp for wp in
           walk_package(DocxPackage(FIXTURES / "textbox.docx"))}

    # The modern (AlternateContent) box, anchored in body-0001.
    tb = wps["body-0001-tb0-p0"]
    assert tb.location == "textbox"
    assert paragraph_text(tb.element) == "Beware the dog, it bites without warning."
    # The anchoring paragraph keeps its own text, free of the box's.
    assert paragraph_text(wps["body-0001"].element) == "The sign by the road read as follows."

    # The plain legacy VML box, anchored in body-0002.
    assert (paragraph_text(wps["body-0002-tb0-p0"].element)
            == "The gate stood open, the yard was empty.")

    # The Fallback duplicate of the first box is never yielded as its own line:
    # exactly one textbox paragraph carries the dog sentence.
    dog = [pid for pid, wp in wps.items()
           if "Beware the dog" in paragraph_text(wp.element)]
    assert dog == ["body-0001-tb0-p0"]


def test_textbox_ids_deterministic():
    pkg = DocxPackage(FIXTURES / "textbox.docx")
    first = [(wp.para_id, wp.location, paragraph_text(wp.element))
             for wp in walk_package(pkg)]
    second = [(wp.para_id, wp.location, paragraph_text(wp.element))
              for wp in walk_package(pkg)]
    assert first == second
    assert ("body-0001-tb0-p0", "textbox",
            "Beware the dog, it bites without warning.") in first


def test_table_and_footnote_ids():
    ids = {wp.para_id for wp in walk_package(DocxPackage(FIXTURES / "table.docx"))}
    assert "body-0000" in ids and "table-0-r0-c0-p0" in ids

    wps = {wp.para_id: wp for wp in
           walk_package(DocxPackage(FIXTURES / "footnotes.docx"))}
    assert wps["footnote-2-p0"].location == "footnote"
    assert "implications" in paragraph_text(wps["footnote-2-p0"].element)