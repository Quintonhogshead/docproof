"""An editable author document; internal evidence and review notes stay private."""
from pathlib import Path
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.oxml.ns import qn
from .models import word_count


def write_document(path: Path, story, draft, review, *, book_label: str):
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
    doc.add_heading(story.title or book_label, 0)
    if story.author:
        doc.add_paragraph(story.author, "Subtitle")
    doc.add_paragraph("Five back-cover teaser options", "Subtitle")
    doc.add_paragraph(f"Recommended starting point: Option {review.recommended_option}. "
                      "Each option offers a different approach to the same book. "
                      "Use the guide at the end to adapt your preferred version.")
    ordered = sorted(draft.teasers, key=lambda t: (t.number != review.recommended_option, t.number))
    for index, teaser in enumerate(ordered):
        suffix = " — Recommended" if teaser.number == review.recommended_option else ""
        doc.add_heading(f"Option {teaser.number}{suffix}", 1)
        doc.add_paragraph(teaser.angle, "Subtitle")
        count = doc.add_paragraph(f"{word_count(' '.join(teaser.paragraphs))} words")
        count.paragraph_format.keep_with_next = True
        for number, paragraph in enumerate(teaser.paragraphs):
            p = doc.add_paragraph(paragraph)
            p.paragraph_format.keep_together = True
            p.paragraph_format.keep_with_next = number < len(teaser.paragraphs) - 1
    doc.add_heading("Optional opening hooks", 1)
    doc.add_paragraph("Choose one if it suits your preferred teaser; these are alternatives.")
    for hook in draft.opening_hooks:
        doc.add_paragraph(hook, "List Bullet")
    doc.add_heading("Editorial note", 1)
    doc.add_paragraph(draft.editorial_note)
    doc.add_page_break()
    doc.add_heading("Teaser elements & best practices", 1)
    doc.add_paragraph("Keep the promise of the book clear while protecting what makes it worth finishing.")
    for element in draft.elements:
        doc.add_heading(element.name, 2)
        doc.add_paragraph(element.purpose)
        doc.add_paragraph(element.book_specific_guidance)
    doc.add_heading("Best practices", 2)
    for item in draft.best_practices:
        doc.add_paragraph(item, "List Bullet")
    doc.add_heading("Before you use your revised teaser", 2)
    for item in draft.modification_checklist:
        doc.add_paragraph(item, "List Bullet")
    doc.core_properties.title = (story.title or book_label) + " — Author teasers"
    doc.core_properties.author = "DocProof"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path
