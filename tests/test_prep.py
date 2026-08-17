"""Manuscript prep, from a styleless export to two finished files.

The load-bearing test in here is `test_author_text_survives_*`: prep is allowed
to restyle, remove blank lines and insert scene-break glyphs, and it is allowed
to do nothing else to the author's text. Everything else is detail.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from lxml import etree

from docproof import prep as preplib
from docproof.config import load_config
from docproof.ingest import IngestError
from docproof.models import Usage
from docproof.prep import rules, verify
from docproof.prep.chunker import preview, split, windows
from docproof.prep.ingest import BODY_PART, build_structure, preflight
from docproof.prep.styles import (BODY, SPACING, StyleSheetError,
                                  build_styles_xml, load_style_sheet)
from docproof.prep.tagger import (Tagger, load_tagging_prompt, render_window)
from docproof.prep.model import Tag
from docproof.providers import ProviderResult
from docproof.utils.xml_helpers import DocxPackage, paragraph_text, qn, walk_package

from .conftest import FIXTURES
from .fakes import FakeProvider, USAGE

CONFIG = FIXTURES.parent.parent / "config" / "default.yaml"
CONFIG_DIR = CONFIG.parent

# The fixture's paragraphs, by position. Written out because every rules test
# below is really a claim about one of these.
TITLE, COPYRIGHT, CHAPTER = "body-0000", "body-0002", "body-0004"
FIRST_BODY, SECOND_BODY = "body-0006", "body-0007"
BREAK, AFTER_BREAK, LINK = "body-0008", "body-0009", "body-0010"
ACKS = "body-0012"

REALISTIC = {TITLE: "title page", COPYRIGHT: "copyright",
             CHAPTER: "chapter # / title", BREAK: "scene break",
             ACKS: "front/backmatter title"}


@pytest.fixture
def cfg():
    return load_config(CONFIG)


@pytest.fixture
def sheet(cfg):
    return load_style_sheet(CONFIG_DIR / cfg.prep.style_sheet)


@pytest.fixture
def prepared(cfg):
    return preplib.prepare(cfg, FIXTURES / "googledoc.docx",
                           config_dir=CONFIG_DIR)


def run(cfg, prepared, tmp_path, canned=None, outputs=("indesign",)):
    tags, usage = preplib.run_mock(prepared, canned)
    return preplib.finish(prepared, tags, usage, cfg, out_dir=tmp_path,
                          source_path=FIXTURES / "googledoc.docx",
                          outputs=list(outputs))


def styles_in(path) -> dict[str, str]:
    """para_id → the style name applied, resolved through styles.xml."""
    pkg = DocxPackage(path)
    names = {s.get(qn("w:styleId")): s.find(qn("w:name")).get(qn("w:val"))
             for s in pkg.tree("word/styles.xml").findall(qn("w:style"))}
    out = {}
    for wp in walk_package(pkg):
        if wp.part != BODY_PART:
            continue
        el = wp.element.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
        out[wp.para_id] = names.get(el.get(qn("w:val"))) if el is not None else None
    return out


def idml_styles_in(path) -> list[str]:
    """Applied paragraph-style NAMES, in document order, read from the IDML the
    InDesign output now produces. The IDML carries no para_id, so this is a
    list, not a map — the order is the assertion."""
    import zipfile

    from docproof.prep.writers.indesign_idml import discover_body_story
    body_story = discover_body_story(path)
    with zipfile.ZipFile(path) as z:
        styles = etree.fromstring(z.read("Resources/Styles.xml"))
        self_to_name = {e.get("Self"): e.get("Name")
                        for e in styles.iter("ParagraphStyle")}
        story = etree.fromstring(z.read(f"Stories/Story_{body_story}.xml"))
    return [self_to_name.get(psr.get("AppliedParagraphStyle"),
                             psr.get("AppliedParagraphStyle"))
            for psr in story.iter("ParagraphStyleRange")]


def clean_docx(cfg, prepared, tmp_path, canned=None):
    """Run the clean transform straight and return the .docx it makes.

    The InDesign deliverable is IDML now, but that IDML is built by running this
    same clean transform first and transcoding it — so the docx-level cleanup
    (blanks removed, edges trimmed, italics and link styles applied) is tested
    here, on the transform itself, where those assertions are legible."""
    from docproof.prep.writers.clean import write_clean
    tags, _ = preplib.run_mock(prepared, canned)
    plan = rules.build_plan(prepared.structure, tags, prepared.sheet)
    pkg = DocxPackage(Path(prepared.structure.source_path))
    write_clean(pkg, prepared.structure, plan, prepared.sheet,
                strip_formatting=cfg.prep.strip_direct_formatting)
    path = tmp_path / "clean.docx"
    pkg.save(path)
    return path


# --- reading ------------------------------------------------------------------

def test_every_paragraph_is_read_including_the_blank_ones(prepared):
    """The review pipeline drops blanks, short lines and headings. Prep cannot:
    a blank line is how an author writes a scene break."""
    ids = [p.para_id for p in prepared.structure.paragraphs]
    assert len(ids) == 14
    assert ids == [f"body-{i:04d}" for i in range(14)]
    assert sum(1 for p in prepared.structure.paragraphs if p.is_blank) == 5
    # "Chapter One" is eleven characters — under the review pipeline's floor.
    chapter = prepared.structure.paragraphs[4]
    assert chapter.text == "Chapter One"


def test_ids_match_the_shared_walker(prepared):
    pkg = DocxPackage(FIXTURES / "googledoc.docx")
    walked = [wp.para_id for wp in walk_package(pkg) if wp.part == BODY_PART]
    assert [p.para_id for p in prepared.structure.paragraphs] == walked


def test_typed_whitespace_and_italics_are_noticed(prepared):
    body = prepared.structure.paragraphs[6]
    assert body.leading_ws == 3
    assert body.trailing_ws == 2
    assert body.has_italics
    assert prepared.structure.paragraphs[10].has_link


# A picture, a legacy VML shape and an embedded object, each alone on its line
# with no text, between two ordinary paragraphs and one genuinely blank line.
# An image run carries no text, so without image detection each of these reads
# as a blank line and prep deletes it — taking the image out of the file.
_IMAGE_DOC = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>A body paragraph before the image.</w:t></w:r></w:p>
    <w:p/>
    <w:p><w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"/></w:drawing></w:r></w:p>
    <w:p><w:r><w:pict/></w:r></w:p>
    <w:p><w:r><w:object/></w:r></w:p>
    <w:p><w:r><w:t>A body paragraph after the image.</w:t></w:r></w:p>
  </w:body>
</w:document>"""


def _write_docx(path, document_xml: str, styles_xml: str | None = None):
    """A minimal well-formed .docx around a document.xml, the way the raw-XML
    fixtures are built. `styles_xml` adds a registered word/styles.xml."""
    import zipfile
    styles_override = (
        '\n  <Override PartName="/word/styles.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        if styles_xml else "")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>{styles_override}
</Types>""")
        z.writestr("_rels/.rels",
                   """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""")
        z.writestr("word/document.xml", document_xml)
        if styles_xml:
            z.writestr("word/_rels/document.xml.rels",
                       """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>""")
            z.writestr("word/styles.xml", styles_xml)
    return path


def test_an_image_on_its_own_line_is_read_as_content_not_blank(tmp_path):
    """A picture, a legacy VML shape and an embedded object each sit alone on a
    line with no text. Each is content, not a blank line, so none may be dropped
    — and all three image containers Word writes have to be recognised."""
    structure = build_structure(preflight(_write_docx(tmp_path / "img.docx",
                                                       _IMAGE_DOC)))
    drawing, pict, obj = structure.paragraphs[2:5]
    assert [p.has_image for p in (drawing, pict, obj)] == [True, True, True]
    assert not any(p.is_blank for p in (drawing, pict, obj))
    # The real blank line and the text paragraphs are unaffected.
    assert structure.paragraphs[1].is_blank
    assert not structure.paragraphs[1].has_image
    assert not structure.paragraphs[0].is_blank
    assert not structure.paragraphs[0].has_image


def test_an_image_on_its_own_line_survives_the_clean_file(cfg, tmp_path):
    """End to end: the drawing, the VML shape and the object are all still in a
    .docx output, not removed as blank lines. Checked on the book output because
    it keeps the manuscript's own objects; the InDesign (IDML) output is
    text-only by design — images stay the designer's job — so it is not the
    place to assert image survival."""
    prepared = preplib.prepare(cfg, _write_docx(tmp_path / "img.docx", _IMAGE_DOC),
                               config_dir=CONFIG_DIR)
    tags, usage = preplib.run_mock(prepared)
    outputs = preplib.finish(prepared, tags, usage, cfg, out_dir=tmp_path,
                             source_path=tmp_path / "img.docx",
                             outputs=["book"])
    body = DocxPackage(outputs.documents["book"]).tree("word/document.xml")
    for tag in ("w:drawing", "w:pict", "w:object"):
        assert body.find(f".//{qn(tag)}") is not None, f"{tag} was dropped"


# Prose around a table and an image: the table references its own table style,
# its first cell is centered Courier New 9pt, its second cell holds only an
# empty paragraph (which every w:tc must keep — dropping it corrupts the file),
# and the image sits centered on its own line.
_TABLE_DOC = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Prose before the table, long enough to be body text.</w:t></w:r></w:p>
    <w:tbl>
      <w:tblPr><w:tblStyle w:val="FancyGrid"/></w:tblPr>
      <w:tblGrid><w:gridCol w:w="4000"/><w:gridCol w:w="4000"/></w:tblGrid>
      <w:tr>
        <w:tc><w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:rPr><w:rFonts w:ascii="Courier New" w:hAnsi="Courier New"/><w:sz w:val="18"/></w:rPr><w:t>Cell one text</w:t></w:r></w:p></w:tc>
        <w:tc><w:p/></w:tc>
      </w:tr>
    </w:tbl>
    <w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"/></w:drawing></w:r></w:p>
    <w:p><w:r><w:t>Prose after the table, also body text.</w:t></w:r></w:p>
  </w:body>
</w:document>"""

_TABLE_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
  <w:style w:type="table" w:styleId="FancyGrid">
    <w:name w:val="Fancy Grid"/>
    <w:tblPr><w:tblBorders><w:top w:val="single" w:sz="8"/></w:tblBorders></w:tblPr>
  </w:style>
</w:styles>"""


def _prepared_table_doc(cfg, tmp_path):
    path = _write_docx(tmp_path / "tbl.docx", _TABLE_DOC,
                       styles_xml=_TABLE_STYLES)
    return path, preplib.prepare(cfg, path, config_dir=CONFIG_DIR)


def test_table_and_image_line_paragraphs_are_preserved(cfg, tmp_path):
    """Everything inside a table, and an image on its own line, is the
    author's layout: read — their words still count in the word-for-word
    check — but never sent to the model and never in the plan, so no writer
    can restyle, strip or drop them."""
    _, prepared = _prepared_table_doc(cfg, tmp_path)
    by_id = {p.para_id: p for p in prepared.structure.paragraphs}

    cell = by_id["table-0-r0-c0-p0"]
    empty_cell = by_id["table-0-r0-c1-p0"]
    assert cell.location == "table" and cell.preserved
    assert empty_cell.preserved and empty_cell.is_blank
    image = next(p for p in prepared.structure.paragraphs if p.has_image)
    assert image.preserved and image.location == "body"

    prose_ids = [p.para_id for p in prepared.structure.paragraphs
                 if not p.preserved]
    assert [p.para_id for p in prepared.structure.taggable] == prose_ids

    tags, _ = preplib.run_mock(prepared)
    plan = rules.build_plan(prepared.structure, tags, prepared.sheet)
    assert {item.para_id for item in plan.plans} == set(prose_ids)


def test_the_book_output_leaves_tables_and_images_alone(cfg, tmp_path):
    """Formatting the manuscript as the book (Times New Roman 12 by default)
    must not reach inside a table or move an image: cell alignment and the
    author's own cell face survive, the empty cell keeps its paragraph, the
    table's own style survives the sheet replacement, and the centered image
    stays centered — while the prose around them is restyled."""
    path, prepared = _prepared_table_doc(cfg, tmp_path)
    tags, usage = preplib.run_mock(prepared)
    outputs = preplib.finish(prepared, tags, usage, cfg, out_dir=tmp_path,
                             source_path=path, outputs=["book"])
    assert all(c.ok for c in outputs.verifications)

    pkg = DocxPackage(outputs.documents["book"])
    body = pkg.tree(BODY_PART)
    cells = body.findall(f".//{qn('w:tc')}")
    assert len(cells) == 2

    cell_p = cells[0].find(qn("w:p"))
    assert cell_p.find(f"{qn('w:pPr')}/{qn('w:jc')}").get(qn("w:val")) == "center"
    fonts = cell_p.find(f".//{qn('w:rFonts')}")
    assert fonts.get(qn("w:ascii")) == "Courier New"
    assert cell_p.find(f".//{qn('w:sz')}").get(qn("w:val")) == "18"
    assert cell_p.find(f"{qn('w:pPr')}/{qn('w:pStyle')}") is None

    # The empty cell keeps its blank paragraph: it is not a blank line to
    # remove, it is what makes the cell a valid cell.
    assert cells[1].find(qn("w:p")) is not None

    image_p = next(wp.element for wp in walk_package(pkg)
                   if wp.part == BODY_PART
                   and wp.element.find(f".//{qn('w:drawing')}") is not None)
    assert image_p.find(f"{qn('w:pPr')}/{qn('w:jc')}").get(qn("w:val")) == "center"
    assert image_p.find(f"{qn('w:pPr')}/{qn('w:pStyle')}") is None

    styles = pkg.tree("word/styles.xml")
    fancy = [s for s in styles.findall(qn("w:style"))
             if s.get(qn("w:styleId")) == "FancyGrid"]
    assert fancy and fancy[0].get(qn("w:type")) == "table"

    # And the formatting still happened around them: every prose paragraph
    # carries a house style.
    prose = [wp.element for wp in walk_package(pkg)
             if wp.part == BODY_PART and wp.location == "body"
             and wp.element.find(f".//{qn('w:t')}") is not None]
    assert prose
    assert all(p.find(f"{qn('w:pPr')}/{qn('w:pStyle')}") is not None
               for p in prose)


def test_table_words_still_flow_into_the_idml(cfg, tmp_path):
    """The IDML output is text-only, so a table cannot travel as a table — but
    its words must: the word-for-word check reads the IDML back against every
    paragraph of the manuscript, tables included."""
    path, prepared = _prepared_table_doc(cfg, tmp_path)
    tags, usage = preplib.run_mock(prepared)
    outputs = preplib.finish(prepared, tags, usage, cfg, out_dir=tmp_path,
                             source_path=path, outputs=["indesign"])
    assert all(c.ok for c in outputs.verifications)

    import zipfile
    from docproof.prep.writers.indesign_idml import discover_body_story
    idml = outputs.documents["indesign"]
    with zipfile.ZipFile(idml) as z:
        story = z.read(
            f"Stories/Story_{discover_body_story(idml)}.xml").decode("utf-8")
    assert "Cell one text" in story


def test_tracked_changes_are_refused_outright():
    """Prep restyles every paragraph and deletes blank lines. Doing that on top
    of somebody's unresolved edit would bury it."""
    with pytest.raises(IngestError, match="tracked changes"):
        preflight(FIXTURES / "tracked.docx")


def test_an_idml_is_refused_with_the_reason():
    with pytest.raises(IngestError, match="gets a manuscript INTO InDesign"):
        preflight(FIXTURES / "layout.idml")


# --- the style sheet ----------------------------------------------------------

def test_the_style_sheet_is_the_only_source_of_style_names(sheet):
    assert sheet.role("body_first").name == "body first"
    assert sheet.role("scene_break").name == "scene break"
    # The names the model may answer with are the sheet's, plus the two
    # pseudo-roles that are not styles at all.
    assert "chapter # / title" in sheet.model_choices
    assert BODY in sheet.model_choices and SPACING in sheet.model_choices
    assert "body first" not in sheet.model_choices    # only the rules apply it


def test_word_style_ids_carry_the_house_names_verbatim(sheet):
    """InDesign matches on the NAME when you place the file, and Word cannot
    put "chapter # / title" in a styleId. Both have to be right."""
    root = build_styles_xml(sheet)
    names = {s.get(qn("w:styleId")): s.find(qn("w:name")).get(qn("w:val"))
             for s in root.findall(qn("w:style"))}
    assert names["ChapterTitle"] == "chapter # / title"
    assert names["PartNumber"] == "part #"
    assert names["FrontBackmatterTitle"] == "front/backmatter title"
    assert len(set(names.values())) == len(names)     # no two styles collide


def test_a_broken_style_sheet_says_what_to_fix(tmp_path):
    bad = tmp_path / "house_styles.yaml"
    bad.write_text("styles:\n  - name: body para\n    id: 'body para'\n",
                   encoding="utf-8")
    with pytest.raises(StyleSheetError, match="not a usable Word style id"):
        load_style_sheet(bad)


def test_a_replacement_sheet_wins_over_the_shipped_one(cfg, tmp_path):
    """Dropping in another publisher's style set is a file, not a code change."""
    (tmp_path / "house_styles.yaml").write_text(
        (CONFIG_DIR / cfg.prep.style_sheet).read_text("utf-8")
        .replace("Atmosphere Press prose template", "Some Other Press"),
        encoding="utf-8")
    sheet = load_style_sheet(CONFIG_DIR / cfg.prep.style_sheet,
                             override_dir=tmp_path)
    assert sheet.name == "Some Other Press"


# --- the rules ----------------------------------------------------------------

def plan_for(structure, sheet, canned):
    tags = [Tag(p.para_id, canned.get(p.para_id,
                                      SPACING if p.is_blank else BODY))
            for p in structure.paragraphs]
    return rules.build_plan(structure, tags, sheet)


def test_the_first_paragraph_after_a_heading_is_body_first(prepared, sheet):
    plan = plan_for(prepared.structure, sheet, REALISTIC).by_id()
    assert plan[FIRST_BODY].style == "body first"
    assert plan[SECOND_BODY].style == "body para"
    assert plan[AFTER_BREAK].style == "body first"     # a break opens a block


def test_a_scene_break_replaces_the_blank_line_it_was(prepared, sheet):
    plan = plan_for(prepared.structure, sheet, REALISTIC).by_id()
    assert plan[BREAK].insert_glyph and plan[BREAK].style == "scene break"
    # Every other blank line is spacing, and spacing goes.
    assert plan["body-0001"].drop and plan["body-0011"].drop


def test_a_blank_under_a_heading_is_not_a_scene_break(prepared, sheet):
    """The gap under "Chapter One" is the gap under a heading. Putting a glyph
    there would open the chapter with a scene break."""
    plan = plan_for(prepared.structure, sheet,
                    {**REALISTIC, "body-0005": "scene break"})
    by_id = plan.by_id()
    assert by_id["body-0005"].drop and not by_id["body-0005"].insert_glyph
    assert any(f.kind == "break_after_heading" for f in plan.flags)


def test_a_paragraph_labelled_a_scene_break_but_full_of_prose_is_body(
        prepared, sheet):
    plan = plan_for(prepared.structure, sheet, {FIRST_BODY: "scene break"})
    by_id = plan.by_id()
    assert by_id[FIRST_BODY].style in ("body first", "body para")
    assert any(f.kind == "long_scene_break" for f in plan.flags)


def test_two_blank_lines_at_one_break_make_one_break(prepared, sheet):
    plan = plan_for(prepared.structure, sheet,
                    {**REALISTIC, "body-0011": "scene break"}).by_id()
    # body-0008 is the break; body-0011 follows a body paragraph, so it is a
    # real second break — the collapsing case is two blanks in a row, which
    # this fixture expresses through the pair at 0001/0003.
    assert plan[BREAK].insert_glyph
    assert plan["body-0011"].insert_glyph


# --- the manuscript's own table of contents -----------------------------------

# toc.docx: a Contents title, then four entries written the three ways Word
# writes them, then the real chapter the first entry points at.
TOC_TITLE = "body-0000"
TOC_STYLED, TOC_LINKED = "body-0001", "body-0002"
TOC_PAGEREF, TOC_LIBRE = "body-0003", "body-0004"
REAL_CHAPTER = "body-0006"


@pytest.fixture
def toc_structure():
    return build_structure(preflight(FIXTURES / "toc.docx"))


def test_a_contents_list_is_read_from_the_file_not_the_words(toc_structure):
    """Every way a contents entry is marked up, and nothing else. The title
    above the list is front matter, and the chapter heading forty pages down is
    character-for-character identical to the entry pointing at it."""
    toc = {p.para_id for p in toc_structure.paragraphs if p.is_toc}
    assert toc == {TOC_STYLED, TOC_LINKED, TOC_PAGEREF, TOC_LIBRE}
    assert TOC_TITLE not in toc          # "Contents" is a front-matter title
    assert REAL_CHAPTER not in toc       # the heading, not the pointer


def test_textbox_prose_is_left_untouched_by_prep():
    """Prep reflows the manuscript body — it restyles paragraphs and drops blank
    lines. A text box is layout the author placed by hand, not running text, so
    prep sets its paragraphs aside as untouched (as it does a header or a
    footnote) rather than restyling them, even though they live in the body
    part. The running text around the boxes is still read as normal body."""
    structure = build_structure(preflight(FIXTURES / "textbox.docx"))
    ids = {p.para_id for p in structure.paragraphs}
    untouched = {pid for pid, _loc in structure.untouched}

    assert {"body-0001-tb0-p0", "body-0002-tb0-p0"} <= untouched
    assert not {"body-0001-tb0-p0", "body-0002-tb0-p0"} & ids
    assert {"body-0000", "body-0003"} <= ids


def test_contents_entries_are_not_chapter_headings(toc_structure, sheet):
    """The bug this exists for: the model reads "Chapter One" in the contents
    and answers "chapter # / title", which carries a forced page break — so a
    twenty-line contents page became twenty chapter openers."""
    plan = plan_for(toc_structure, sheet,
                    {TOC_TITLE: "front/backmatter title",
                     TOC_STYLED: "chapter # / title",
                     TOC_LINKED: "chapter # / title",
                     TOC_PAGEREF: "chapter # / title",
                     TOC_LIBRE: "front/backmatter title",
                     REAL_CHAPTER: "chapter # / title"})
    by_id = plan.by_id()
    for para_id in (TOC_STYLED, TOC_LINKED, TOC_PAGEREF, TOC_LIBRE):
        assert by_id[para_id].style == "toc entry"
    # What sits either side of the list is untouched by any of this.
    assert by_id[TOC_TITLE].style == "front/backmatter title"
    assert by_id[REAL_CHAPTER].style == "chapter # / title"


def test_the_contents_is_flagged_once_for_the_designer(toc_structure, sheet):
    """InDesign generates its own contents from the placed styles, so whether
    to keep these lines is the designer's call — asked once, not per line."""
    plan = plan_for(toc_structure, sheet, {})
    toc_flags = [f for f in plan.flags if f.kind == "toc_detected"]
    assert len(toc_flags) == 1
    assert toc_flags[0].para_id == TOC_STYLED
    assert "InDesign" in toc_flags[0].message


def test_a_contents_entry_never_opens_a_block(toc_structure, sheet):
    """A contents entry is not a heading, so nothing after the list is "the
    first paragraph of a chapter" on its account."""
    plan = plan_for(toc_structure, sheet, {}).by_id()
    assert plan[REAL_CHAPTER].style == "body para"
    # A real chapter heading still opens one, of course.
    with_heading = plan_for(toc_structure, sheet,
                            {REAL_CHAPTER: "chapter # / title"}).by_id()
    assert with_heading["body-0008"].style == "body first"
    assert with_heading["body-0009"].style == "body para"


def test_a_style_sheet_with_no_contents_style_falls_back_to_body(
        toc_structure, sheet, tmp_path):
    """An older or bespoke sheet names no toc style. Those lines become body,
    which looks wrong and reads fine — the failure that matters is a page break
    under every one of them, and that does not happen."""
    bare = replace(sheet, roles={k: v for k, v in sheet.roles.items()
                                 if k != "toc"})
    plan = plan_for(toc_structure, bare, {TOC_STYLED: "chapter # / title"})
    by_id = plan.by_id()
    assert by_id[TOC_STYLED].style in ("body first", "body para")
    assert any(f.kind == "toc_detected" for f in plan.flags)


def test_the_manuscripts_own_style_is_shown_to_the_model(toc_structure):
    """The style Word gave a paragraph is the strongest signal in the file, and
    prep used to read it and throw it away before the model ever saw it."""
    window = windows(toc_structure.paragraphs, max_paragraphs=120,
                     token_budget=6000, context=8, preview_chars=400)[0]
    turn = render_window(window, {}, header="Already labelled:",
                         preview_chars=400)
    assert 'style="TOC1"' in turn
    assert 'style="TOCHeading"' in turn
    assert 'style="Normal"' not in turn      # true of nearly every paragraph


def test_a_tagged_contents_survives_verification(cfg, tmp_path):
    """Restyling is the only thing that happened to those lines: the page
    numbers and the entry text come through word for word, so the file still
    ships."""
    prepared = preplib.prepare(cfg, FIXTURES / "toc.docx",
                               config_dir=CONFIG_DIR)
    tags, usage = preplib.run_mock(prepared, {TOC_STYLED: "chapter # / title"})
    outputs = preplib.finish(prepared, tags, usage, cfg, out_dir=tmp_path,
                             source_path=FIXTURES / "toc.docx",
                             outputs=["indesign"])
    assert all(check.ok for check in outputs.verifications)
    assert "toc entry" in idml_styles_in(outputs.documents["indesign"])


def test_a_manuscript_with_a_textbox_survives_verification(cfg, tmp_path):
    """A pull quote or a cover title floats in the body as a textbox — layout
    the author placed by hand, which ingest sets aside as untouched and prep
    never restyles. The verifier has to set the same paragraphs aside on the
    written file: reading a box on the output side while the source stream drops
    it counted its words on one side only, and failed every manuscript with one
    for as many words as the box held."""
    prepared = preplib.prepare(cfg, FIXTURES / "textbox.docx",
                               config_dir=CONFIG_DIR)
    tags, usage = preplib.run_mock(prepared, {})
    # Clean (book/indesign) and accept+reject (tracked) — every view reads the
    # written file back, so every one has to agree the box is not running text.
    outputs = preplib.finish(prepared, tags, usage, cfg, out_dir=tmp_path,
                             source_path=FIXTURES / "textbox.docx",
                             outputs=["indesign", "tracked"])
    assert {c.view for c in outputs.verifications} == {"clean", "accept",
                                                       "reject"}
    assert all(check.ok for check in outputs.verifications)


# --- the InDesign-ready file --------------------------------------------------

def test_the_clean_file_is_tagged_end_to_end(cfg, prepared, tmp_path):
    outputs = run(cfg, prepared, tmp_path, REALISTIC)
    applied = idml_styles_in(outputs.documents["indesign"])
    assert applied == [
        "title page", "copyright", "chapter # / title", "body first",
        "body para", "scene break", "body first", "body para",
        "front/backmatter title", "body first"]
    approved = {s.name for s in prepared.sheet.styles}
    assert set(applied) <= approved                 # no stray styles at all


def test_the_indesign_output_is_a_valid_idml(cfg, prepared, tmp_path):
    """The InDesign deliverable is a well-formed IDML package: mimetype first
    and stored, every part well-formed XML, the house styles carry their
    formatting, italics survive, and an inserted scene break is in the story."""
    import zipfile

    from docproof.prep.writers.indesign_idml import discover_body_story
    path = run(cfg, prepared, tmp_path, REALISTIC).documents["indesign"]

    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        assert names[0] == "mimetype"
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/vnd.adobe.indesign-idml-package"
        for name in names:                          # every XML part parses
            if name.endswith(".xml"):
                etree.fromstring(z.read(name))
        styles = etree.fromstring(z.read("Resources/Styles.xml"))
        by_name = {e.get("Name"): e for e in styles.iter("ParagraphStyle")}
        story = z.read(f"Stories/Story_{discover_body_story(path)}.xml").decode()

    # Body carries its first-line indent; the chapter head is centred and bold.
    assert by_name["body para"].get("FirstLineIndent") == "18"
    assert by_name["chapter # / title"].get("Justification") == "CenterAlign"
    assert by_name["chapter # / title"].get("FontStyle") == "Bold"
    # The author's one italic word survives as a local italic run.
    assert 'FontStyle="Italic"' in story and "much" in story
    # The inserted scene break is really in the text.
    assert "***" in story


def test_the_clean_file_is_cleaned_up(cfg, prepared, tmp_path):
    pkg = DocxPackage(clean_docx(cfg, prepared, tmp_path, REALISTIC))
    body = pkg.tree(BODY_PART)
    texts = [paragraph_text(p) for p in body.iter(qn("w:p"))]
    assert not any(t.strip() == "" for t in texts)          # blanks gone
    assert texts[3].startswith("The road")                  # typed spaces gone
    assert texts[3].endswith("darker.")                     # and at the end
    assert not list(body.iter(qn("w:sdt")))                 # goog_rdk gone
    assert "***" in texts                                   # the break glyph


def test_italics_and_links_survive_the_cleanup(cfg, prepared, tmp_path):
    pkg = DocxPackage(clean_docx(cfg, prepared, tmp_path, REALISTIC))
    body = pkg.tree(BODY_PART)
    italics = [t.text for t in body.iter(qn("w:t"))
               if t.getparent().find(f"{qn('w:rPr')}/{qn('w:i')}") is not None]
    assert italics == ["much"]
    # The URL carries the hyperlink character style; nothing else does.
    linked = [t.text for t in body.iter(qn("w:t"))
              if t.getparent().find(f"{qn('w:rPr')}/{qn('w:rStyle')}") is not None]
    assert linked == ["www.example.com/wildflower"]
    # ...and the export's fonts and sizes are gone, so InDesign gets no
    # local overrides to clear.
    assert not list(body.iter(qn("w:rFonts")))
    assert not list(body.iter(qn("w:sz")))


def test_author_text_survives_the_clean_file(cfg, prepared, tmp_path):
    outputs = run(cfg, prepared, tmp_path, REALISTIC)
    check, = outputs.verifications
    assert check.ok and check.view == "clean"
    assert check.output_words == prepared.structure.word_count


# --- the tracked-changes file -------------------------------------------------

def test_the_tracked_file_says_both_things_at_once(cfg, prepared, tmp_path):
    """Accept every change and you have the clean file; reject every change and
    you have the manuscript back. Both are checked on the written file."""
    outputs = run(cfg, prepared, tmp_path, REALISTIC, outputs=("tracked",))
    views = {c.view: c for c in outputs.verifications}
    assert set(views) == {"accept", "reject"}
    assert all(c.ok for c in views.values())


def test_the_tracked_file_records_what_each_style_was(cfg, prepared, tmp_path):
    outputs = run(cfg, prepared, tmp_path, REALISTIC, outputs=("tracked",))
    pkg = DocxPackage(outputs.documents["tracked"])
    body = pkg.tree(BODY_PART)
    changes = list(body.iter(qn("w:pPrChange")))
    assert len(changes) == 10                       # every tagged paragraph
    # A rejected change has to land on a style that still exists, so the
    # tracked file keeps the document's own styles alongside the house set.
    assert all(c.find(qn("w:pPr")) is not None for c in changes)

    # A removed blank line is a deleted paragraph mark, not a vanished
    # paragraph: nothing disappears without someone accepting it.
    marks = [p for p in body.iter(qn("w:p"))
             if p.find(f"{qn('w:pPr')}/{qn('w:rPr')}/{qn('w:del')}") is not None]
    assert len(marks) == 4


def test_the_tracked_file_inserts_the_break_as_a_revision(cfg, prepared,
                                                          tmp_path):
    outputs = run(cfg, prepared, tmp_path, REALISTIC, outputs=("tracked",))
    pkg = DocxPackage(outputs.documents["tracked"])
    inserted = [t.text for ins in pkg.tree(BODY_PART).iter(qn("w:ins"))
                for t in ins.iter(qn("w:t"))]
    assert inserted == ["***"]


def test_the_tracked_file_keeps_the_authors_formatting(cfg, prepared, tmp_path):
    """Stripping a Google export's fonts here would be hundreds of extra
    revisions to click through. The placed file is the one that gets cleaned."""
    outputs = run(cfg, prepared, tmp_path, REALISTIC, outputs=("tracked",))
    pkg = DocxPackage(outputs.documents["tracked"])
    assert list(pkg.tree(BODY_PART).iter(qn("w:rFonts")))


# --- the gate -----------------------------------------------------------------

def test_a_file_whose_words_drifted_is_not_shipped(cfg, prepared, tmp_path,
                                                   monkeypatch):
    """The one failure mode that matters. A writer that eats a word must not
    produce a file, however plausible everything else looks."""
    from docproof.prep.writers import clean as clean_writer
    real = clean_writer.write_clean

    def eats_a_word(pkg, structure, plan, sheet, **kw):
        stats = real(pkg, structure, plan, sheet, **kw)
        for t in pkg.tree(BODY_PART).iter(qn("w:t")):
            if "remembered" in (t.text or ""):
                t.text = t.text.replace("remembered", "")
        return stats

    monkeypatch.setattr(clean_writer, "write_clean", eats_a_word)
    # The InDesign writer bound write_clean at import; patch it there too, since
    # that is the path the "indesign" output runs through.
    monkeypatch.setattr("docproof.prep.writers.indesign_idml.write_clean",
                        eats_a_word)

    with pytest.raises(preplib.VerificationFailed, match="word 2[0-9]"):
        run(cfg, prepared, tmp_path, REALISTIC)
    assert not (tmp_path / "tagged_googledoc.idml").exists()
    # The notes are still written: they are how anyone finds out what happened.
    assert "Nothing was shipped" in (tmp_path / "prep_notes.md").read_text("utf-8")


def test_the_glyph_is_not_counted_as_a_word_on_either_side():
    source = verify.stream(["A scene ends.", "Another begins."], "***")
    output = verify.stream(["A scene ends.", "***", "Another begins."], "***")
    assert verify.compare(source, output, view="clean").ok


def test_a_glyph_written_with_spaces_in_it_is_still_one_break():
    """"* * *" is three tokens to anything that counts words. A house set that
    writes its breaks that way must not fail verification for every break the
    manuscript has."""
    source = verify.stream(["A scene ends.", "Another begins."], "* * *")
    output = verify.stream(["A scene ends.", "* * *", "Another begins."],
                           "* * *")
    assert verify.compare(source, output, view="clean").ok


def test_a_swapped_word_is_reported_with_its_place():
    result = verify.compare(["the", "road", "was", "long"],
                            ["the", "street", "was", "long"], view="clean")
    assert not result.ok
    assert "word 2" in result.detail and "'road'" in result.detail.replace('"', "'")


# --- talking to the model -----------------------------------------------------

def test_blank_lines_are_offered_as_blank_lines(prepared):
    window = prepared.windows[0]
    rendered = render_window(window, {}, header="Already labelled:",
                             preview_chars=400)
    assert '<blank id="body-0001"/>' in rendered
    assert '<p id="body-0004" words="2">Chapter One</p>' in rendered


def test_long_paragraphs_are_truncated_before_they_are_billed(prepared):
    long_one = prepared.structure.paragraphs[6]
    assert preview(long_one, 20).endswith("…")
    assert len(preview(long_one, 20)) < len(long_one.text)


def test_windows_carry_the_tail_of_the_one_before(prepared):
    made = windows(prepared.structure.paragraphs, max_paragraphs=5,
                   token_budget=10_000, context=2, preview_chars=400)
    assert [len(w.paragraphs) for w in made] == [5, 5, 4]
    assert [p.para_id for p in made[1].context] == ["body-0003", "body-0004"]


def test_a_window_the_model_cannot_answer_is_retried_smaller(prepared, sheet,
                                                              cfg):
    """A failed window costs every paragraph in it, so it is halved and retried
    before anything is given up on."""
    prompt = load_tagging_prompt(CONFIG_DIR / cfg.prep.tagging_prompt)
    ok = ProviderResult(parsed={"paragraphs": [
        {"para_id": f"body-{i:04d}", "role": "body", "flag": ""}
        for i in range(7)]}, usage=USAGE)
    provider = FakeProvider([ProviderResult(stop_reason="max_tokens",
                                            error="too long"), ok, ok])
    tagger = Tagger(sheet, prompt, provider, model="test", max_paragraphs=14)
    usage = Usage()
    tags = tagger.tag(prepared.structure, usage)
    assert len(provider.calls) == 3                 # one failure, two halves
    assert len(tags) == 14


def test_paragraphs_the_model_skipped_are_flagged_not_guessed(prepared, sheet,
                                                              cfg):
    prompt = load_tagging_prompt(CONFIG_DIR / cfg.prep.tagging_prompt)
    partial = ProviderResult(parsed={"paragraphs": [
        {"para_id": "body-0000", "role": "title page", "flag": ""}]},
        usage=USAGE)
    tagger = Tagger(sheet, prompt, FakeProvider([partial]), model="test")
    tags = {t.para_id: t for t in tagger.tag(prepared.structure, Usage())}
    assert tags["body-0000"].source == "model"
    assert tags["body-0003"].source == "unanswered"
    assert "by hand" in tags["body-0003"].flag


def test_the_schema_only_admits_styles_the_template_has(sheet, cfg):
    prompt = load_tagging_prompt(CONFIG_DIR / cfg.prep.tagging_prompt)
    tagger = Tagger(sheet, prompt, FakeProvider(), model="test")
    role = tagger.schema["$defs"]["RawTag"]["properties"]["role"]
    assert set(role["enum"]) == set(sheet.model_choices)
    assert "Heading 1" not in role["enum"]


def test_the_prompt_names_the_style_set_it_was_built_from(sheet, cfg):
    prompt = load_tagging_prompt(CONFIG_DIR / cfg.prep.tagging_prompt)
    rendered = prompt.render(sheet)
    assert sheet.name in rendered
    assert "chapter # / title —" in rendered        # the sheet's own wording
    assert "{choices}" not in rendered


# --- the notes ----------------------------------------------------------------

def test_the_notes_say_what_happened_and_what_to_decide(cfg, prepared,
                                                        tmp_path):
    outputs = run(cfg, prepared, tmp_path, REALISTIC, outputs=("indesign",
                                                               "tracked"))
    notes = outputs.notes_md.read_text("utf-8")
    assert "chapter # / title" in notes            # the mapping, with counts
    assert "identical to the manuscript" in notes  # the word-for-word result
    assert "For the designer" in notes
    import json
    payload = json.loads(outputs.notes_json.read_text("utf-8"))
    assert payload["verified"] is True
    assert payload["counts"]["scene_breaks_inserted"] == 1
    assert payload["counts"]["blank_lines_removed"] == 4
    assert payload["style_sheet"]["name"] == prepared.sheet.name


# --- the cost estimate is priced at the effort actually used ------------------

def test_prep_json_cost_is_priced_at_the_run_effort(cfg, prepared, tmp_path,
                                                    monkeypatch):
    """The recorded cost is an estimate, but not an effort-blind one: the
    effort dial scales output tokens, so the prep-notes call must pass the
    effort the run used, not the baseline."""
    import docproof.prep.notes as notes
    seen: dict = {}
    real = notes.estimate_cost

    def spy(model_id, **kw):
        seen.update(kw)
        return real(model_id, **kw)

    monkeypatch.setattr(notes, "estimate_cost", spy)
    cfg.api.effort = "high"
    run(cfg, prepared, tmp_path)

    assert seen.get("effort") == "high"


def test_effort_lifts_the_estimate_for_a_model_that_honours_it():
    from docproof.providers.catalog import estimate_cost

    low = estimate_cost("claude-opus-5", input_tokens=1000,
                        output_tokens=1000, effort="low")
    high = estimate_cost("claude-opus-5", input_tokens=1000,
                         output_tokens=1000, effort="high")
    assert low is not None and high is not None and high > low
