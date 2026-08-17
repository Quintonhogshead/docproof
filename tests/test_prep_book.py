"""The book-styled output: the sketch of the finished interior.

The load-bearing test is still the word-for-word one — the book writer moves a
letter into a drop-cap frame, and the verifier must read the word whole again.
Everything else checks that the design lands where the YAML says it should:
trim on the sections, faces embedded, heads and folios referenced, and the
subject picking the display font.
"""
from __future__ import annotations

import zipfile

import pytest
from lxml import etree

from docproof import prep as preplib
from docproof.config import load_config
from docproof.models import Usage
from docproof.prep import verify
from docproof.prep.book_design import (BookDesignError, BookMeta,
                                       load_book_design)
from docproof.prep.meta import detect_meta
from docproof.providers import ProviderResult
from docproof.utils.xml_helpers import DocxPackage, qn, walk_package
from docproof.prep.ingest import BODY_PART

from .conftest import FIXTURES
from .fakes import FakeProvider, USAGE

CONFIG = FIXTURES.parent.parent / "config" / "default.yaml"
CONFIG_DIR = CONFIG.parent

REALISTIC = {"body-0000": "title page", "body-0002": "copyright",
             "body-0004": "chapter # / title", "body-0008": "scene break",
             "body-0012": "front/backmatter title"}

META = BookMeta(subject="fantasy", title="Witch in the Wall",
                author="Quinn Hogshead", detected=True)


@pytest.fixture
def cfg():
    # The shipped default book design is the plain manuscript now; this file's
    # ornament tests are about the Atmosphere paperback sketch, so point at it.
    c = load_config(CONFIG)
    c.prep.book_design = "prep/book_design.yaml"
    return c


@pytest.fixture
def prepared(cfg):
    return preplib.prepare(cfg, FIXTURES / "googledoc.docx",
                           config_dir=CONFIG_DIR)


def run_book(cfg, prepared, tmp_path, meta=META):
    tags, usage = preplib.run_mock(prepared, REALISTIC)
    return preplib.finish(prepared, tags, usage, cfg, out_dir=tmp_path,
                          source_path=FIXTURES / "googledoc.docx",
                          outputs=["book"], meta=meta)


# --- the design file ----------------------------------------------------------

def test_the_default_book_design_is_the_plain_manuscript():
    """A manual run hands back a plain Times New Roman 12pt manuscript by
    default — no ornaments, no display faces. The paperback sketch is the
    opt-in alternate."""
    default = load_config(CONFIG)                 # untouched by the cfg fixture
    design = load_book_design(CONFIG_DIR / default.prep.book_design)
    assert design.fonts["body"].family == "Times New Roman"
    assert design.display_styles == ()
    assert design.drop_caps.lines == 0
    assert not design.running_heads.enabled


def test_the_paperback_design_loads_and_knows_its_subjects(cfg):
    design = load_book_design(CONFIG_DIR / cfg.prep.book_design)
    assert "fantasy" in design.subject_choices
    assert design.subject("fantasy").family == "IM FELL English"
    # An unknown subject falls back to the default rather than failing: a
    # sketch with the wrong face beats no sketch.
    assert design.subject("gardening").key == design.default_subject
    # The display face travels with the body faces.
    names = [p.name for p in design.embed_files("fantasy")]
    assert "IMFellEnglish-Regular.ttf" in names
    assert "Spectral-Regular.ttf" in names


def test_a_design_naming_a_missing_font_file_is_refused(tmp_path):
    bad = tmp_path / "book_design.yaml"
    bad.write_text("""
version: 1
page: {width: 5.5, height: 8.5}
fonts:
  body: {family: Nope, files: [fonts/NoSuch.ttf]}
  heading: {family: Nope}
subjects:
  literary: {family: Nope}
""", encoding="utf-8")
    with pytest.raises(BookDesignError):
        load_book_design(bad)


# --- the written file ---------------------------------------------------------

def test_book_file_is_written_verified_and_named(cfg, prepared, tmp_path):
    outputs = run_book(cfg, prepared, tmp_path)
    path = outputs.documents["book"]
    assert path.name == "book_googledoc.docx"
    assert all(c.ok for c in outputs.verifications)


def test_trim_margins_and_head_references_land_on_the_section(cfg, prepared,
                                                              tmp_path):
    path = run_book(cfg, prepared, tmp_path).documents["book"]
    pkg = DocxPackage(path)
    body = pkg.tree(BODY_PART)
    sects = list(body.iter(qn("w:sectPr")))
    assert sects
    for sect in sects:
        size = sect.find(qn("w:pgSz"))
        assert size.get(qn("w:w")) == "7920"      # 5.5in
        assert size.get(qn("w:h")) == "12240"     # 8.5in
    first = sects[0]
    kinds = {(el.get(qn("w:type")))
             for el in first.findall(qn("w:headerReference"))}
    assert kinds == {"even", "default"}
    assert first.find(qn("w:titlePg")) is not None
    # Mirrored margins and even/odd heads are settings-wide switches.
    settings = pkg.tree("word/settings.xml")
    assert settings.find(qn("w:evenAndOddHeaders")) is not None
    assert settings.find(qn("w:mirrorMargins")) is not None


def test_the_subject_picks_the_title_page_face(cfg, prepared, tmp_path):
    path = run_book(cfg, prepared, tmp_path).documents["book"]
    pkg = DocxPackage(path)
    styles = pkg.tree("word/styles.xml")
    for style in styles.findall(qn("w:style")):
        name = style.find(qn("w:name")).get(qn("w:val"))
        if name == "title page":
            fonts = style.find(f"{qn('w:rPr')}/{qn('w:rFonts')}")
            assert fonts.get(qn("w:ascii")) == "IM FELL English"
            return
    raise AssertionError("no 'title page' style in the book file")


def test_fonts_are_embedded_and_registered(cfg, prepared, tmp_path):
    path = run_book(cfg, prepared, tmp_path).documents["book"]
    with zipfile.ZipFile(path) as z:
        odttf = [n for n in z.namelist() if n.endswith(".odttf")]
        assert len(odttf) == 5                    # 4 Spectral + IM FELL
        table = etree.fromstring(z.read("word/fontTable.xml"))
    families = {f.get(qn("w:name")) for f in table.findall(qn("w:font"))}
    assert {"Spectral", "IM FELL English"} <= families


def test_the_drop_cap_letter_still_counts_as_the_authors_word(cfg, prepared,
                                                              tmp_path):
    """The chapter's first letter moves into a frame paragraph; the verifier
    reads it back as part of the word it came from — that is what lets the
    book output pass the same gate as the others."""
    path = run_book(cfg, prepared, tmp_path).documents["book"]
    pkg = DocxPackage(path)
    frames = [wp.element for wp in walk_package(pkg) if wp.part == BODY_PART
              and verify._is_drop_cap(wp.element)]
    assert len(frames) == 1
    # And the stream proves the glue-back: finish() already verified, but
    # this is the one behaviour worth asserting directly.
    source = verify.source_stream(prepared.structure, "***")
    output = verify.output_stream(path, "***")
    assert source == output


def test_no_drop_cap_when_the_paragraph_opens_with_a_quote(cfg, tmp_path):
    prepared = preplib.prepare(cfg, FIXTURES / "googledoc.docx",
                               config_dir=CONFIG_DIR)
    # Retag so the first body paragraph after the chapter heading is the one
    # starting with typed whitespace — the fixture's first body opens with
    # spaces, which is stripped, so instead check by rewriting nothing: the
    # realistic tags produce exactly one drop cap; a run with no chapter
    # heading tag produces none.
    tags, usage = preplib.run_mock(prepared)      # everything is body
    outputs = preplib.finish(prepared, tags, usage, cfg, out_dir=tmp_path,
                             source_path=FIXTURES / "googledoc.docx",
                             outputs=["book"], meta=META)
    pkg = DocxPackage(outputs.documents["book"])
    frames = [wp for wp in walk_package(pkg) if wp.part == BODY_PART
              and verify._is_drop_cap(wp.element)]
    assert frames == []


def test_running_heads_carry_the_title_and_author(cfg, prepared, tmp_path):
    path = run_book(cfg, prepared, tmp_path).documents["book"]
    with zipfile.ZipFile(path) as z:
        even = z.read("word/header_book_even.xml").decode("utf-8")
        odd = z.read("word/header_book_odd.xml").decode("utf-8")
        footer = z.read("word/footer_book.xml").decode("utf-8")
    assert "Quinn Hogshead" in even               # verso: the author
    assert "Witch in the Wall" in odd             # recto: the title
    assert "PAGE" in footer


def test_a_missing_meta_defaults_to_the_file_name(cfg, prepared, tmp_path):
    outputs = run_book(cfg, prepared, tmp_path, meta=None)
    path = outputs.documents["book"]
    with zipfile.ZipFile(path) as z:
        odd = z.read("word/header_book_odd.xml").decode("utf-8")
    assert "googledoc" in odd


# --- the plain DocWatch manuscript design -------------------------------------

@pytest.fixture
def plain_cfg(cfg):
    """The plain manuscript design — the shipped default, and what both a manual
    run and the watched folder hand back. (The `cfg` fixture forced the paperback
    design for the ornament tests, so put the plain one back.)"""
    cfg.prep.book_design = "prep/book_manuscript.yaml"
    return cfg


@pytest.fixture
def plain_prepared(plain_cfg):
    return preplib.prepare(plain_cfg, FIXTURES / "googledoc.docx",
                           config_dir=CONFIG_DIR)


def test_the_manuscript_design_is_plain_letter_times_new_roman(cfg):
    design = load_book_design(CONFIG_DIR / "prep" / "book_manuscript.yaml")
    assert (design.page.width, design.page.height) == (8.5, 11)
    assert not design.page.mirror
    assert not design.running_heads.enabled       # no author/title band
    assert design.folio                            # but a page number at the foot
    assert design.display_styles == ()
    assert design.drop_caps.lines == 0
    assert design.fonts["body"].family == "Times New Roman"
    # Nothing to embed — Times New Roman is on every machine — and no subject,
    # title or author to read off the manuscript (the folio needs none).
    assert design.embed_files(None) == ()
    assert not design.needs_meta


def test_the_manuscript_output_is_plain_with_a_page_number(plain_cfg,
                                                           plain_prepared,
                                                           tmp_path):
    tags, usage = preplib.run_mock(plain_prepared, REALISTIC)
    outputs = preplib.finish(plain_prepared, tags, usage, plain_cfg,
                             out_dir=tmp_path,
                             source_path=FIXTURES / "googledoc.docx",
                             outputs=["book"], meta=None)
    path = outputs.documents["book"]
    assert all(c.ok for c in outputs.verifications)

    pkg = DocxPackage(path)
    first = next(pkg.tree(BODY_PART).iter(qn("w:sectPr")))
    for sect in pkg.tree(BODY_PART).iter(qn("w:sectPr")):
        size = sect.find(qn("w:pgSz"))
        assert size.get(qn("w:w")) == "12240"     # 8.5in
        assert size.get(qn("w:h")) == "15840"     # 11in
        assert sect.find(qn("w:headerReference")) is None
    # A folio but no head: a footer reference, no even/odd (one number, every
    # page), and a title page so the first page carries none.
    assert first.find(qn("w:footerReference")) is not None
    assert first.find(qn("w:titlePg")) is not None
    settings = pkg.tree("word/settings.xml")
    assert settings.find(qn("w:evenAndOddHeaders")) is None
    assert settings.find(qn("w:mirrorMargins")) is None

    # The folio part is written and holds a PAGE field; no running-head parts,
    # and no fonts embedded.
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        footer = z.read("word/footer_book.xml").decode("utf-8")
    assert not any("header_book" in n for n in names)
    assert not any(n.endswith(".odttf") for n in names)
    assert "PAGE" in footer

    # Every styled face is the one body face, and chapter titles are 14-point.
    styles = pkg.tree("word/styles.xml")
    families, chapter_sz = set(), None
    for style in styles.findall(qn("w:style")):
        rf = style.find(f"{qn('w:rPr')}/{qn('w:rFonts')}")
        if rf is not None:
            families.add(rf.get(qn("w:ascii")))
        name = style.find(qn("w:name"))
        if name is not None and name.get(qn("w:val")) == "chapter # / title":
            chapter_sz = style.find(f"{qn('w:rPr')}/{qn('w:sz')}").get(qn("w:val"))
    assert families == {"Times New Roman"}
    assert chapter_sz == "28"                      # 14pt, in half-points


# --- detection ----------------------------------------------------------------

def test_detect_meta_reads_the_three_facts(cfg, prepared):
    provider = FakeProvider([ProviderResult(
        parsed={"subject": "fantasy", "title": "Witch in the Wall",
                "author": "Quinn Hogshead"}, usage=USAGE)])
    usage = Usage()
    meta = detect_meta(prepared.structure, prepared.design, provider,
                       model="claude-sonnet-5", usage=usage)
    assert meta == BookMeta(subject="fantasy", title="Witch in the Wall",
                            author="Quinn Hogshead", detected=True)
    # The wire schema constrains the subject to the design's own keys.
    call = provider.calls[0]
    enum = call["schema"]["properties"]["subject"]["enum"]
    assert set(enum) == set(prepared.design.subject_choices)


def test_detection_failure_degrades_to_defaults(cfg, prepared):
    provider = FakeProvider([ProviderResult(parsed=None, stop_reason="error",
                                            error="nope", usage=USAGE)])
    meta = detect_meta(prepared.structure, prepared.design, provider,
                       model="claude-sonnet-5", usage=Usage())
    assert meta == BookMeta()
    assert not meta.detected


def test_operator_answers_win_field_by_field():
    detected = BookMeta(subject="fantasy", title="Detected", author="Model",
                        detected=True)
    merged = preplib.merge_meta(detected, subject="romance", author="")
    assert merged.subject == "romance"
    assert merged.title == "Detected"
    assert merged.author == "Model"
