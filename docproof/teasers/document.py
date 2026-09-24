"""An editable author document: the five options and nothing internal."""
from pathlib import Path
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.oxml.ns import qn
from . import AUTHOR_WARNING


def write_document(path: Path, draft, *, book_label: str):
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.top_margin = section.bottom_margin = Inches(.75)
    section.left_margin = section.right_margin = Inches(.85)
    for name in ("Normal", "Body Text"):
        style = doc.styles[name]
        style.font.name = "Arial"
        style.font.size = Pt(11)
        style.paragraph_format.space_after = Pt(8)
        style.paragraph_format.line_spacing = 1.15
    for name, size in (("Title", 24), ("Subtitle", 12), ("Heading 1", 18), ("Heading 2", 13)):
        doc.styles[name].font.name = "Arial"
        doc.styles[name].font.size = Pt(size)
        doc.styles[name].font.color.rgb = RGBColor.from_string("000000")
        doc.styles[name].paragraph_format.keep_with_next = True
        for border in doc.styles[name].element.iter(qn("w:pBdr")):
            border.getparent().remove(border)
    doc.add_heading(draft.title or book_label, 0)
    if draft.author:
        doc.add_paragraph(draft.author, "Subtitle")
    doc.add_paragraph("Five back-cover teaser options", "Subtitle")
    doc.add_paragraph(AUTHOR_WARNING)
    doc.add_paragraph("Choose and adapt any of these five options. The accompanying two-page "
                      "dos and donts sheet offers guidance for revising your choice.")
    for index, teaser in enumerate(sorted(draft.teasers, key=lambda t: t.number)):
        if index:
            doc.add_page_break()
        doc.add_heading(f"Option {teaser.number}", 1)
        doc.add_paragraph(teaser.angle, "Subtitle")
        for paragraph in teaser.paragraphs:
            p = doc.add_paragraph(paragraph)
            p.paragraph_format.keep_together = True
    doc.core_properties.title = (draft.title or book_label) + " — Author teasers"
    doc.core_properties.author = "DocProof"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path
