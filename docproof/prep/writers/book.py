"""The book-styled .docx — a sketch of the finished interior.

This is the file for the author and the developmental editors, not the InDesign
team: the same clean, verified text as the placed file, dressed as an
Atmosphere paperback. It starts from the clean writer's decisions — the plan is
the same object — and then applies the interior design on top: real page
geometry at the house trim, the design's faces embedded so they travel, running
heads and folios, a chapter-opener drop cap, and the front matter set in the
display face the manuscript's subject picked.

Word is an approximation of a typeset page, and that is the point — the file
stays editable and commentable, which a rendered PDF would not be.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from lxml import etree

from ...utils.xml_helpers import DR_NS, W_NS, qn
from ..book_design import BookDesign, BookMeta
from ..ingest import BODY_PART
from ..model import PrepPlan, Structure
from ..styles import StyleSheet, install_style_sheet
from . import clean
from .clean import WriteStats
from .common import PPR_TAG, PSTYLE_TAG, SECTPR_TAG, element_map
from .fonts import EmbeddedFont, embed_fonts

log = logging.getLogger("docproof.prep.writers.book")

SETTINGS_PART = "word/settings.xml"
_CT_SETTINGS = ("application/vnd.openxmlformats-officedocument."
                "wordprocessingml.settings+xml")
_CT_HEADER = ("application/vnd.openxmlformats-officedocument."
              "wordprocessingml.header+xml")
_CT_FOOTER = ("application/vnd.openxmlformats-officedocument."
              "wordprocessingml.footer+xml")
_REL_SETTINGS = f"{DR_NS}/settings"
_REL_HEADER = f"{DR_NS}/header"
_REL_FOOTER = f"{DR_NS}/footer"

_HEADER_EVEN = "word/header_book_even.xml"
_HEADER_ODD = "word/header_book_odd.xml"
_FOOTER = "word/footer_book.xml"

# CT_SectPr child order (ECMA-376). The manuscript's own sectPr elements are
# patched, not rebuilt, so insertion has to land each element where the schema
# wants it.
_SECTPR_ORDER = ("headerReference", "footerReference", "footnotePr",
                 "endnotePr", "type", "pgSz", "pgMar", "paperSrc", "pgBorders",
                 "lnNumType", "pgNumType", "cols", "formProt", "vAlign",
                 "noEndnote", "titlePg", "textDirection", "bidi", "rtlGutter",
                 "docGrid", "printerSettings", "sectPrChange")

# EG_RPrBase child order — enough of it to keep a patched w:rPr valid.
_RPR_ORDER = ("rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps",
              "strike", "dstrike", "outline", "shadow", "emboss", "imprint",
              "noProof", "snapToGrid", "vanish", "webHidden", "color",
              "spacing", "w", "kern", "position", "sz", "szCs", "highlight",
              "u", "effect", "bdr", "shd", "fitText", "vertAlign", "rtl",
              "cs", "em", "lang", "eastAsianLayout", "specVanish")

# CT_Settings child order — the subset that matters for ranked insertion; an
# element not listed sorts after everything that is.
_SETTINGS_ORDER = (
    "writeProtection", "view", "zoom", "removePersonalInformation",
    "doNotDisplayPageBoundaries", "displayBackgroundShape",
    "printPostScriptOverText", "printFractionalCharacterWidth",
    "printFormsData", "embedTrueTypeFonts", "embedSystemFonts",
    "saveSubsetFonts", "saveFormsData", "mirrorMargins",
    "alignBordersAndEdges", "bordersDoNotSurroundHeader",
    "bordersDoNotSurroundFooter", "gutterAtTop", "hideSpellingErrors",
    "hideGrammaticalErrors", "activeWritingStyle", "proofState", "formsDesign",
    "attachedTemplate", "linkStyles", "stylePaneFormatFilter",
    "stylePaneSortMethod", "documentType", "mailMerge", "revisionView",
    "trackChanges", "doNotTrackMoves", "doNotTrackFormatting",
    "documentProtection", "autoFormatOverride", "styleLockTheme",
    "styleLockQFSet", "defaultTabStop", "autoHyphenation",
    "consecutiveHyphenLimit", "hyphenationZone", "doNotHyphenateCaps",
    "showEnvelope", "summaryLength", "clickAndTypeStyle", "defaultTableStyle",
    "evenAndOddHeaders", "bookFoldRevPrinting", "bookFoldPrinting",
    "bookFoldPrintingSheets", "drawingGridHorizontalSpacing",
    "drawingGridVerticalSpacing", "displayHorizontalDrawingGridEvery",
    "displayVerticalDrawingGridEvery", "characterSpacingControl",
    "savePreviewPicture", "updateFields", "hdrShapeDefaults", "footnotePr",
    "endnotePr", "compat", "rsids", "mathPr", "themeFontLang",
    "clrSchemeMapping", "shapeDefaults", "decimalSymbol", "listSeparator")


def write_book(pkg, structure: Structure, plan: PrepPlan, sheet: StyleSheet,
               design: BookDesign, meta: BookMeta, *,
               strip_formatting: bool = True) -> WriteStats:
    """Everything the clean writer does, then the interior design on top."""
    # Positional paragraph ids stop describing the tree once the clean pass
    # removes blank lines, so the id -> element map is taken first. The
    # elements a drop cap needs are never ones the clean pass removes.
    elements = element_map(pkg)
    # Through the module, not a from-import: the clean writer is the seam the
    # word-eating tests (and any future instrumentation) patch.
    stats = clean.write_clean(pkg, structure, plan, sheet,
                              strip_formatting=strip_formatting)

    subject = design.subject(meta.subject or None)
    designed = _designed_sheet(sheet, design, subject.key)
    install_style_sheet(pkg, designed, replace=True,
                        defaults=_defaults(design))
    _suppress_repeated_openers(elements, plan, designed)
    header_refs, footer_refs = _running_head_parts(pkg, design, meta)
    _apply_geometry(pkg, design, header_refs, footer_refs)
    _ensure_settings(pkg, ["evenAndOddHeaders", "embedTrueTypeFonts"]
                     + (["mirrorMargins"] if design.page.mirror else []))
    fonts = embed_fonts(pkg, _fonts_to_embed(design, subject.key))
    drops = _drop_caps(elements, plan, sheet, design)

    log.info("Book file: subject '%s' (display face %s), %d drop cap(s), "
             "%d font file(s) embedded.", subject.key,
             design.display_font(subject.key).family, drops, fonts)
    return replace(stats, drop_caps=drops, fonts=fonts)


# --- the designed style sheet -------------------------------------------------

def _defaults(design: BookDesign) -> dict:
    body = design.styles.get("body para") or {}
    return {"family": design.fonts["body"].family,
            "size": body.get("size", 11), "line": body.get("line", 1.35)}


def _designed_sheet(sheet: StyleSheet, design: BookDesign,
                    subject_key: str) -> StyleSheet:
    """The house style sheet with every style's light format replaced by the
    design's, and the font keys resolved to family names."""
    display = design.display_font(subject_key).family
    styled = []
    for style in sheet.styles:
        fmt = dict(style.format)
        override = design.styles.get(style.name)
        if override:
            fmt.update({k: v for k, v in override.items() if k != "font"})
        font_key = (override or {}).get("font")
        if style.name in design.display_styles or font_key == "display":
            fmt["family"] = display
        elif font_key:
            fmt["family"] = design.fonts[font_key].family
        styled.append(replace(style, format=fmt))
    unknown = [name for name in design.styles if not sheet.has(name)]
    if unknown:
        log.warning("The book design styles %s, which the style sheet does "
                    "not define; ignored.", ", ".join(map(repr, unknown)))
    return replace(sheet, styles=tuple(styled))


def _fonts_to_embed(design: BookDesign, subject_key: str) -> list[EmbeddedFont]:
    out: list[EmbeddedFont] = []
    seen = set()
    for font in design.fonts.values():
        for file in font.files:
            if file not in seen:
                seen.add(file)
                out.append(EmbeddedFont(family=font.family, file=file))
    display = design.display_font(subject_key)
    for file in display.files:
        if file not in seen:
            seen.add(file)
            out.append(EmbeddedFont(family=display.family, file=file))
    return out


# --- page geometry ------------------------------------------------------------

def _apply_geometry(pkg, design: BookDesign, header_refs: dict[str, str],
                    footer_refs: dict[str, str]) -> None:
    """The house trim on every section, running-head references on the first.

    Every sectPr is stripped of the manuscript's own page size, margins and
    header/footer references, so the sketch's geometry is the only one left;
    later sections then inherit the first section's references, which is how
    OOXML fills the gap."""
    root = pkg.tree(BODY_PART)
    pkg.mark_modified(BODY_PART)
    body = root.find(qn("w:body"))
    sects = list(root.iter(SECTPR_TAG))
    if not sects and body is not None:
        sects = [etree.SubElement(body, SECTPR_TAG)]

    m = design.page.margins
    for index, sect in enumerate(sects):
        for tag in ("w:headerReference", "w:footerReference", "w:pgSz",
                    "w:pgMar", "w:titlePg"):
            for el in sect.findall(qn(tag)):
                sect.remove(el)
        etree.SubElement(sect, qn("w:pgSz"), {
            qn("w:w"): _twips(design.page.width),
            qn("w:h"): _twips(design.page.height)})
        etree.SubElement(sect, qn("w:pgMar"), {
            qn("w:top"): _twips(m.top), qn("w:right"): _twips(m.outside),
            qn("w:bottom"): _twips(m.bottom), qn("w:left"): _twips(m.inside),
            qn("w:header"): _twips(m.header), qn("w:footer"): _twips(m.footer),
            qn("w:gutter"): "0"})
        if index == 0:
            for kind, rel_id in header_refs.items():
                etree.SubElement(sect, qn("w:headerReference"),
                                 {qn("w:type"): kind, f"{{{DR_NS}}}id": rel_id})
            for kind, rel_id in footer_refs.items():
                etree.SubElement(sect, qn("w:footerReference"),
                                 {qn("w:type"): kind, f"{{{DR_NS}}}id": rel_id})
            # No head or folio on the very first page — the title page.
            etree.SubElement(sect, qn("w:titlePg"))
        _order_children(sect, _SECTPR_ORDER)


def _twips(inches: float) -> str:
    return str(int(round(float(inches) * 1440)))


def _order_children(el: etree._Element, order: tuple[str, ...]) -> None:
    rank = {qn(f"w:{name}"): i for i, name in enumerate(order)}
    children = sorted(el, key=lambda c: rank.get(c.tag, len(order)))
    for child in children:
        el.append(child)


# --- running heads and folio --------------------------------------------------

def _running_head_parts(pkg, design: BookDesign,
                        meta: BookMeta) -> tuple[dict[str, str], dict[str, str]]:
    """The two header parts and the footer part, registered and related.
    Returns ({header type -> rel id}, {footer type -> rel id})."""
    title = (meta.title or "").strip()
    author = (meta.author or "").strip()
    texts = {"title": title or author, "author": author or title}
    heads = design.running_heads

    def head_part(text: str) -> etree._Element:
        root = etree.Element(qn("w:hdr"), nsmap={"w": W_NS})
        root.append(_centered_line(
            text, family=design.fonts["heading"].family, size=heads.size,
            caps=heads.caps, letterspace=heads.letterspace))
        return root

    even = head_part(texts.get(heads.verso, title))
    odd = head_part(texts.get(heads.recto, title))
    footer = etree.Element(qn("w:ftr"), nsmap={"w": W_NS})
    footer.append(_folio_paragraph(design))

    header_refs: dict[str, str] = {}
    footer_refs: dict[str, str] = {}
    for name, root, content_type, rel_type, refs, kind in (
            (_HEADER_EVEN, even, _CT_HEADER, _REL_HEADER, header_refs, "even"),
            (_HEADER_ODD, odd, _CT_HEADER, _REL_HEADER, header_refs, "default"),
            (_FOOTER, footer, _CT_FOOTER, _REL_FOOTER, footer_refs, "default")):
        _set_part(pkg, name, root)
        _override_content_type(pkg, name, content_type)
        refs[kind] = _document_relationship(pkg, rel_type,
                                            name.removeprefix("word/"))
    # With evenAndOddHeaders on, even pages read the "even" reference — the
    # same centered folio belongs on both sides.
    footer_refs["even"] = footer_refs["default"]
    return header_refs, footer_refs


def _centered_line(text: str, *, family: str, size: float, caps: bool,
                   letterspace: float) -> etree._Element:
    p = etree.Element(qn("w:p"))
    ppr = etree.SubElement(p, PPR_TAG)
    etree.SubElement(ppr, qn("w:jc"), {qn("w:val"): "center"})
    run = etree.SubElement(p, qn("w:r"))
    rpr = etree.SubElement(run, qn("w:rPr"))
    etree.SubElement(rpr, qn("w:rFonts"),
                     {qn("w:ascii"): family, qn("w:hAnsi"): family})
    if caps:
        etree.SubElement(rpr, qn("w:caps"), {qn("w:val"): "1"})
    if letterspace:
        etree.SubElement(rpr, qn("w:spacing"),
                         {qn("w:val"): str(int(letterspace * 20))})
    half = str(int(size * 2))
    etree.SubElement(rpr, qn("w:sz"), {qn("w:val"): half})
    etree.SubElement(rpr, qn("w:szCs"), {qn("w:val"): half})
    t = etree.SubElement(run, qn("w:t"))
    t.text = text
    return p


def _folio_paragraph(design: BookDesign) -> etree._Element:
    p = etree.Element(qn("w:p"))
    ppr = etree.SubElement(p, PPR_TAG)
    etree.SubElement(ppr, qn("w:jc"), {qn("w:val"): "center"})
    half = str(int(design.folio_size * 2))

    def run() -> etree._Element:
        r = etree.SubElement(p, qn("w:r"))
        rpr = etree.SubElement(r, qn("w:rPr"))
        etree.SubElement(rpr, qn("w:rFonts"),
                         {qn("w:ascii"): design.fonts["body"].family,
                          qn("w:hAnsi"): design.fonts["body"].family})
        etree.SubElement(rpr, qn("w:sz"), {qn("w:val"): half})
        etree.SubElement(rpr, qn("w:szCs"), {qn("w:val"): half})
        return r

    begin = run()
    etree.SubElement(begin, qn("w:fldChar"), {qn("w:fldCharType"): "begin"})
    instr = run()
    text = etree.SubElement(instr, qn("w:instrText"))
    text.set(qn("xml:space"), "preserve")
    text.text = " PAGE "
    sep = run()
    etree.SubElement(sep, qn("w:fldChar"), {qn("w:fldCharType"): "separate"})
    number = run()
    t = etree.SubElement(number, qn("w:t"))
    t.text = "1"
    end = run()
    etree.SubElement(end, qn("w:fldChar"), {qn("w:fldCharType"): "end"})
    return p


# CT_PPr child order — the subset the suppression patch can meet.
_PPR_ORDER = ("pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr",
              "widowControl", "numPr", "spacing", "ind", "contextualSpacing",
              "jc", "textAlignment", "rPr", "sectPr")


def _suppress_repeated_openers(elements, plan: PrepPlan,
                               designed: StyleSheet) -> None:
    """Page breaks and sinks belong to the first paragraph of a run, not to
    the style. A two-line chapter opener, a multi-paragraph copyright page, a
    two-line title: with `page_break_before` and `space_before` on the style,
    every following line of the run would start its own page and fall its own
    sink. Each continuation paragraph gets a direct override saying 'no break,
    no sink' — attribute-level, so the style's space_after and line survive."""
    from .common import ensure_ppr

    formats = {s.name: s.format for s in designed.styles}
    previous: str | None = None
    for item in plan.plans:
        if item.drop:
            continue
        style = item.style or None
        if style and style == previous:
            fmt = formats.get(style) or {}
            needs_break = bool(fmt.get("page_break_before"))
            needs_sink = fmt.get("space_before") is not None
            p = elements.get(item.para_id)
            if p is not None and (needs_break or needs_sink):
                ppr = ensure_ppr(p)
                if needs_break:
                    for el in ppr.findall(qn("w:pageBreakBefore")):
                        ppr.remove(el)
                    etree.SubElement(ppr, qn("w:pageBreakBefore"),
                                     {qn("w:val"): "0"})
                if needs_sink:
                    spacing = ppr.find(qn("w:spacing"))
                    if spacing is None:
                        spacing = etree.SubElement(ppr, qn("w:spacing"))
                    spacing.set(qn("w:before"), "0")
                _order_children(ppr, _PPR_ORDER)
        previous = style


def _drop_caps(elements, plan: PrepPlan, sheet: StyleSheet,
               design: BookDesign) -> int:
    """A dropped initial on the opening paragraph of each chapter.

    The first letter moves into its own frame paragraph (`w:framePr
    w:dropCap`), which is exactly how Word records a drop cap it made itself.
    The verifier knows to read the letter and the rest as one word again.
    Only paragraphs that start with a plain letter are dropped — an opening
    quotation mark makes a poor initial and a worse sketch."""
    if design.drop_caps.lines < 2 or not design.drop_caps.after:
        return 0
    from ...utils.xml_helpers import paragraph_text

    body_first = sheet.role("body_first").name
    count = 0
    previous: str | None = None
    for item in plan.plans:
        if item.drop:
            continue
        style = item.style or None
        if (style == body_first and previous in design.drop_caps.after):
            p = elements.get(item.para_id)
            if p is not None and _drop_first_letter(p, sheet, design,
                                                    paragraph_text(p)):
                count += 1
        previous = style if style else previous
    return count


def _drop_first_letter(p, sheet: StyleSheet, design: BookDesign,
                       text: str) -> bool:
    if not text or not text[0].isalpha():
        return False
    from ...reassembler import _ensure_boundary, _text_spans

    _ensure_boundary(p, 1)
    letter_runs = [t.getparent() for t, s, e in _text_spans(p)
                   if s == 0 and e == 1]
    if not letter_runs:
        return False

    body = design.styles.get("body para") or {}
    size = float(body.get("size", 11))
    line = float(body.get("line", 1.35))
    lines = design.drop_caps.lines
    # Word's own recipe, read off a drop cap Word inserted itself: the frame
    # paragraph's line box is the WHOLE frame — all N lines — as an exact
    # height with baseline alignment, and the glyph's em is sized just inside
    # it. A single auto line here is what lands the letter one line low.
    line_twips = int(round(size * line * 20))
    frame_twips = lines * line_twips
    letter_pt = int(round(lines * size * line * 0.98))

    frame = etree.Element(qn("w:p"))
    ppr = etree.SubElement(frame, PPR_TAG)
    etree.SubElement(ppr, PSTYLE_TAG,
                     {qn("w:val"): sheet.role("body_first").id})
    etree.SubElement(ppr, qn("w:framePr"), {
        qn("w:dropCap"): "drop", qn("w:lines"): str(lines),
        qn("w:wrap"): "around", qn("w:vAnchor"): "text",
        qn("w:hAnchor"): "text"})
    etree.SubElement(ppr, qn("w:spacing"),
                     {qn("w:after"): "0", qn("w:line"): str(frame_twips),
                      qn("w:lineRule"): "exact"})
    etree.SubElement(ppr, qn("w:textAlignment"), {qn("w:val"): "baseline"})
    for run in letter_runs:
        rpr = run.find(qn("w:rPr"))
        if rpr is None:
            rpr = etree.Element(qn("w:rPr"))
            run.insert(0, rpr)
        for tag in ("w:rFonts", "w:sz", "w:szCs", "w:position"):
            for el in rpr.findall(qn(tag)):
                rpr.remove(el)
        family = design.fonts["body"].family
        etree.SubElement(rpr, qn("w:rFonts"), {
            qn("w:ascii"): family, qn("w:hAnsi"): family})
        half = str(letter_pt * 2)
        etree.SubElement(rpr, qn("w:sz"), {qn("w:val"): half})
        etree.SubElement(rpr, qn("w:szCs"), {qn("w:val"): half})
        _order_children(rpr, _RPR_ORDER)
        frame.append(run)
    p.addprevious(frame)
    return True


# --- settings and part plumbing -----------------------------------------------

def _ensure_settings(pkg, tags: list[str]) -> None:
    if pkg.has(SETTINGS_PART):
        settings = pkg.tree(SETTINGS_PART)
        pkg.mark_modified(SETTINGS_PART)
    else:
        settings = etree.Element(qn("w:settings"), nsmap={"w": W_NS})
        pkg.add_part(SETTINGS_PART, settings)
        _override_content_type(pkg, SETTINGS_PART, _CT_SETTINGS)
        _document_relationship(pkg, _REL_SETTINGS, "settings.xml")
    changed = False
    for tag in tags:
        if settings.find(qn(f"w:{tag}")) is None:
            etree.SubElement(settings, qn(f"w:{tag}"))
            changed = True
    if changed:
        _order_children(settings, _SETTINGS_ORDER)


def _set_part(pkg, name: str, root: etree._Element) -> None:
    if pkg.has(name):
        existing = pkg.tree(name)
        pkg.mark_modified(name)
        existing.clear()
        existing.tag = root.tag
        for key, value in root.attrib.items():
            existing.set(key, value)
        for child in root:
            existing.append(child)
    else:
        pkg.add_part(name, root)


def _override_content_type(pkg, part: str, content_type: str) -> None:
    from .fonts import _override_content_type as override

    override(pkg, part, content_type)


def _document_relationship(pkg, rel_type: str, target: str) -> str:
    """The rel id for this type+target, adding the relationship if new."""
    from ...utils.xml_helpers import PR_NS
    from .fonts import _add_relationship

    rels_name = "word/_rels/document.xml.rels"
    if pkg.has(rels_name):
        rels = pkg.tree(rels_name)
        pkg.mark_modified(rels_name)
    else:
        rels = etree.Element(f"{{{PR_NS}}}Relationships", nsmap={None: PR_NS})
        pkg.add_part(rels_name, rels)
    for r in rels:
        if r.get("Type") == rel_type and r.get("Target") == target:
            return r.get("Id")
    return _add_relationship(rels, rel_type, target)
