"""Compare the InDesign formatting on two .docx files.

The sibling of `trackdiff`. Where that module diffs the tracked *changes* two
editors made to the same manuscript, this one diffs the *paragraph styling* two
formatters applied to it — a human tagging a book for layout by hand versus
DocProof's prep pass tagging it into the same house style set. What InDesign
matches on when it places a Word file is the paragraph style NAME, so that is
what is compared here, not Word's internal styleId (which the two files spell
however they like) and not the visual formatting (which the template overrides
on Place anyway).

Paragraphs are lined up by their reject-all *content*, exactly as in
`trackdiff` and for the same reason: an inserted or deleted paragraph shifts
every positional id after it, so aligning on position throws the tail of the
document out of sync. Matching on the base text pairs identical prose up
regardless of structural drift, and — because the base is the reject-all text —
it works even if either file still carries tracked changes.

Each aligned paragraph buckets as:

  * agree       — both files gave it the same style name
  * different   — both styled it, to different names
  * only in A / B — the paragraph exists in one file but not the other

Point it at (human-formatted doc) vs (DocProof's prep output on the same raw
file) and "different" is where the model and the human disagreed on a style,
"only in B" is a paragraph DocProof kept that the human dropped (a blank line
turned into a scene break, say), and the agreement rate is the headline.

Like `trackdiff`, the core here has no CLI or app dependency: the `compare
--formatting` command and the app's Compare tab are thin wrappers over
`compare_files`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from .trackdiff import TrackDiffError, _fold, extract_paragraph_edits, open_docx
from .utils.xml_helpers import DocxPackage, qn, walk_package

STYLES_PART = "word/styles.xml"

# The name shown for a paragraph that carries no explicit style and no default
# could be resolved — Word treats such a paragraph as the default paragraph
# style, conventionally "Normal".
_FALLBACK_STYLE = "Normal"


# --- reading the style each paragraph carries --------------------------------

def _style_index(pkg: DocxPackage) -> tuple[dict[str, str], str | None]:
    """(styleId -> style name, default paragraph-style name) from styles.xml.

    A paragraph references a style by its id; InDesign and this comparison care
    about the name, so the id is resolved here once per document. The default
    paragraph style is the one a paragraph with no `w:pStyle` inherits."""
    id_to_name: dict[str, str] = {}
    default: str | None = None
    if not pkg.has(STYLES_PART):
        return id_to_name, default
    for st in pkg.tree(STYLES_PART).findall(qn("w:style")):
        style_type = st.get(qn("w:type"))
        if style_type not in (None, "paragraph"):
            continue
        sid = st.get(qn("w:styleId"))
        name_el = st.find(qn("w:name"))
        name = name_el.get(qn("w:val")) if name_el is not None else sid
        if sid and name:
            id_to_name[sid] = name
        if st.get(qn("w:default")) == "1" and name:
            default = name
    return id_to_name, default


def _para_style_name(p, id_to_name: dict[str, str],
                     default: str | None) -> str:
    """The paragraph's style NAME: its `w:pStyle` resolved through the style
    index, or the document's default paragraph style when it has none."""
    ppr = p.find(qn("w:pPr"))
    if ppr is not None:
        pstyle = ppr.find(qn("w:pStyle"))
        if pstyle is not None:
            sid = pstyle.get(qn("w:val"))
            return id_to_name.get(sid, sid or default or _FALLBACK_STYLE)
    return default or _FALLBACK_STYLE


@dataclass(frozen=True)
class DocStyles:
    base: dict[str, str]        # para_id -> reject-all text
    style: dict[str, str]       # para_id -> paragraph style NAME

    @property
    def styled_count(self) -> int:
        return sum(1 for pid, text in self.base.items() if text.strip())


def extract_styles(pkg: DocxPackage) -> DocStyles:
    """Every paragraph's reject-all text and the style name applied to it."""
    id_to_name, default = _style_index(pkg)
    base: dict[str, str] = {}
    style: dict[str, str] = {}
    for wp in walk_package(pkg):
        text, _ = extract_paragraph_edits(wp.element)
        base[wp.para_id] = text
        style[wp.para_id] = _para_style_name(wp.element, id_to_name, default)
    return DocStyles(base=base, style=style)


# --- comparison --------------------------------------------------------------

@dataclass(frozen=True)
class ParaStyle:
    para_id: str
    text: str                   # the paragraph's reject-all text (from A)
    style_a: str
    style_b: str

    @property
    def agree(self) -> bool:
        return self.style_a == self.style_b


@dataclass
class FormatReport:
    label_a: str
    label_b: str
    matched: list[ParaStyle] = field(default_factory=list)
    only_a: list[str] = field(default_factory=list)   # para_ids, one side only
    only_b: list[str] = field(default_factory=list)

    @property
    def aligned_paras(self) -> int:
        return len(self.matched)

    @property
    def agree(self) -> int:
        return sum(1 for p in self.matched if p.agree)

    @property
    def different(self) -> int:
        return sum(1 for p in self.matched if not p.agree)

    @property
    def agreement(self) -> float | None:
        """Share of aligned paragraphs the two files styled identically."""
        d = self.aligned_paras
        return self.agree / d if d else None


def _ordered(doc: DocStyles) -> list[tuple[str, str]]:
    """(para_id, reject-all base) in document order, for paragraphs that carry
    text. A blank spacer paragraph has no content to style and no InDesign
    meaning — the prep pass removes them outright — so comparing their styling
    would only add noise."""
    return [(pid, base) for pid, base in doc.base.items() if base.strip()]


def compare_styles(a: DocStyles, b: DocStyles, *, label_a: str = "A",
                   label_b: str = "B") -> FormatReport:
    report = FormatReport(label_a=label_a, label_b=label_b)
    a_items = _ordered(a)
    b_items = _ordered(b)
    # Align paragraphs by content — same approach as trackdiff. difflib's runs
    # of equal base texts are the pairs whose styling is comparable; anything
    # left over exists in one file and not the other.
    a_keys = [_fold(base) for _, base in a_items]
    b_keys = [_fold(base) for _, base in b_items]
    sm = SequenceMatcher(a=a_keys, b=b_keys, autojunk=False)
    matched_a: set[int] = set()
    matched_b: set[int] = set()
    for i, j, n in sm.get_matching_blocks():
        for k in range(n):
            ia, ib = i + k, j + k
            matched_a.add(ia)
            matched_b.add(ib)
            pa_id, base = a_items[ia]
            pb_id, _ = b_items[ib]
            report.matched.append(ParaStyle(
                para_id=pa_id, text=base,
                style_a=a.style.get(pa_id, _FALLBACK_STYLE),
                style_b=b.style.get(pb_id, _FALLBACK_STYLE)))
    report.only_a = [pid for idx, (pid, _) in enumerate(a_items)
                     if idx not in matched_a]
    report.only_b = [pid for idx, (pid, _) in enumerate(b_items)
                     if idx not in matched_b]
    return report


def compare_files(path_a: str | Path, path_b: str | Path, *,
                  label_a: str = "A", label_b: str = "B") -> FormatReport:
    return compare_styles(extract_styles(open_docx(path_a)),
                          extract_styles(open_docx(path_b)),
                          label_a=label_a, label_b=label_b)


# --- rendering ---------------------------------------------------------------

def _snippet(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def report_json(report: FormatReport) -> dict:
    return {
        "schema_version": 1,
        "mode": "formatting",
        "label_a": report.label_a,
        "label_b": report.label_b,
        "aligned_paragraphs": report.aligned_paras,
        "totals": {"agree": report.agree, "different": report.different,
                   "only_a": len(report.only_a), "only_b": len(report.only_b)},
        "agreement": _round(report.agreement),
        "differences": [
            {"para_id": p.para_id, "text": _snippet(p.text),
             "style_a": p.style_a, "style_b": p.style_b}
            for p in report.matched if not p.agree],
    }


def _round(x: float | None) -> float | None:
    return round(x, 4) if x is not None else None


def render_markdown(report: FormatReport) -> str:
    def pct(x): return f"{x * 100:.0f}%" if x is not None else "—"
    a, b = report.label_a, report.label_b
    L: list[str] = []
    L.append(f"# InDesign-formatting comparison — {a} vs {b}\n")
    L.append(f"{report.aligned_paras} paragraph(s) compared"
             + (f" · {len(report.only_a)} only in {a}"
                if report.only_a else "")
             + (f" · {len(report.only_b)} only in {b}"
                if report.only_b else "") + "\n")

    L.append("## Totals\n")
    L.append(f"- **Same style**: {report.agree}")
    L.append(f"- **Different style**: {report.different}")
    L.append(f"- **Only in {a}**: {len(report.only_a)}")
    L.append(f"- **Only in {b}**: {len(report.only_b)}\n")
    L.append(f"**Style agreement: {pct(report.agreement)}**\n")

    diffs = [p for p in report.matched if not p.agree]
    if diffs:
        L.append("## Styled differently\n")
        for p in diffs:
            L.append(f"- {_snippet(p.text)}")
            L.append(f"  - {a}: {p.style_a}  ·  {b}: {p.style_b}")
        L.append("")
    return "\n".join(L)


__all__ = [
    "TrackDiffError", "DocStyles", "FormatReport", "ParaStyle",
    "extract_styles", "compare_styles", "compare_files",
    "report_json", "render_markdown",
]
