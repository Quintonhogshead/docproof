"""Generate test fixtures. Run directly or let conftest.py do it."""
from pathlib import Path
import zipfile

import docx

HERE = Path(__file__).parent

SPLICE_A = "The manuscript was finished, nobody wanted to read it."
CLEAN_B  = "Although it was late, we kept driving toward the coast."
SPLICE_C = "She opened the letter, her hands would not stay still."


def simple() -> None:
    d = docx.Document()
    for text in (SPLICE_A, CLEAN_B, SPLICE_C):
        d.add_paragraph(text)
    d.save(HERE / "simple.docx")


def styled() -> None:
    """Bold span mid-sentence — exercises run splitting across formatting."""
    d = docx.Document()
    d.add_heading("Chapter One", level=1)          # must be skipped by style
    p = d.add_paragraph("It was late, ")
    p.add_run("we were tired").bold = True
    p.add_run(", the road went on.")
    d.save(HERE / "styled.docx")


def table() -> None:
    d = docx.Document()
    d.add_paragraph("An introductory paragraph long enough to be reviewed.")
    t = d.add_table(rows=1, cols=2)
    t.cell(0, 0).text = "The gate was open, the dogs were gone."
    t.cell(0, 1).text = "The gate was open, and the dogs were gone."
    d.save(HERE / "table.docx")


_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/>
</Relationships>"""

_DOCUMENT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:r><w:t xml:space="preserve">A body paragraph with a footnote reference.</w:t></w:r>
      <w:r><w:footnoteReference w:id="2"/></w:r>
    </w:p>
  </w:body>
</w:document>"""

_FOOTNOTES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:footnote w:type="separator" w:id="0">
    <w:p><w:r><w:separator/></w:r></w:p>
  </w:footnote>
  <w:footnote w:type="continuationSeparator" w:id="1">
    <w:p><w:r><w:continuationSeparator/></w:r></w:p>
  </w:footnote>
  <w:footnote w:id="2">
    <w:p><w:r><w:t>The note was short, the implications were not.</w:t></w:r></w:p>
  </w:footnote>
</w:footnotes>"""


def footnotes() -> None:
    with zipfile.ZipFile(HERE / "footnotes.docx", "w",
                         zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("word/document.xml", _DOCUMENT)
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        z.writestr("word/footnotes.xml", _FOOTNOTES)


if __name__ == "__main__":
    simple(); styled(); table(); footnotes()
    print("fixtures written to", HERE)