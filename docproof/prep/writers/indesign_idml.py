"""The InDesign-ready .idml.

The sibling of the clean writer. Where `write_clean` produces a tagged .docx a
designer places into the house template, this writer produces the placed file
itself — an IDML, InDesign's open interchange format (a zip of XML), that the
designer opens directly. InDesign turns IDML into INDD on open, so there is no
Place step and no style-name-matching gamble: the styles are already the house
styles, by name.

Crucially this needs no Adobe software to *write* — it is `zipfile` and `lxml`
— so unlike the InDesign placement step it runs on the web build, on Fly. What
still needs a Mac is *opening* the result, which is the designer's job either
way.

The transform is not re-implemented here. This writer runs the verified clean
writer first, mutating the manuscript in memory exactly as the .docx path does —
same paragraphs dropped, same scene breaks inserted, same edges trimmed, same
house styles applied — and then transcodes that cleaned body into IDML. Parity
with the clean .docx is therefore structural: the two say the same thing because
one is read out of the other.
"""
from __future__ import annotations

import html
import logging
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

import lxml.etree as ET

from ...utils.xml_helpers import (R_TAG, RPR_TAG, T_TAG, VIRTUAL_CHARS,
                                  iter_content_elements, qn)
from ..model import PrepPlan, Structure
from ..styles import StyleSheet
from ..verify import Verification, VerificationFailed, source_stream, stream
from .clean import WriteStats, write_clean
from .common import element_map

log = logging.getLogger("docproof.prep.writers.indesign_idml")

IDML_NS = "http://ns.adobe.com/AdobeInDesign/idml/1.0/packaging"
IDML_MIMETYPE = b"application/vnd.adobe.indesign-idml-package"
NO_CHAR_STYLE = "CharacterStyle/$ID/[No character style]"
NORMAL_STYLE = "ParagraphStyle/$ID/NormalParagraphStyle"

PSTYLE_TAG = qn("w:pStyle")
PPR_TAG = qn("w:pPr")
I_TAG = qn("w:i")
VAL = qn("w:val")


@dataclass(frozen=True)
class _Run:
    text: str
    italic: bool


@dataclass(frozen=True)
class _Para:
    style: str                    # house style NAME, or "" for the template default
    runs: tuple[_Run, ...]

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


# ---------------------------------------------------------------------------
# The public writer.
# ---------------------------------------------------------------------------


def write_indesign_idml(pkg, structure: Structure, plan: PrepPlan,
                        sheet: StyleSheet, out_path: str | Path, *,
                        template_path: str | Path,
                        strip_formatting: bool = True) -> WriteStats:
    """Write the placed manuscript as IDML at `out_path`.

    `pkg` is mutated (the clean transform runs on it); the caller passes a fresh
    package, exactly as it does for the other writers. Nothing is written to
    `pkg`'s own path — the IDML is a new file built from a copy of the template.
    """
    # Capture the body elements before the clean transform mutates them: the
    # clean writer drops and rebuilds paragraphs in place, so these same element
    # objects carry the final content afterward. Dropped paragraphs are detached
    # (and skipped below); everything else is edited in place.
    elements = element_map(pkg)
    stats = write_clean(pkg, structure, plan, sheet,
                        strip_formatting=strip_formatting)
    paras = _read_cleaned_body(elements, plan, sheet)
    _emit_idml(paras, sheet, Path(template_path), Path(out_path))
    return stats


def verify_idml(structure: Structure, idml_path: str | Path, glyph: str,
                body_story: str) -> list[Verification]:
    """Prove the IDML still says what the manuscript said.

    The same contract as the .docx verifier: the author's words, as a bare token
    stream with scene-break glyphs dropped, must be identical. Raises
    VerificationFailed on a mismatch so the pipeline deletes the file rather than
    shipping it."""
    texts = [p.text for p in _read_idml_story(idml_path, body_story)]
    out = stream(texts, glyph)
    src = source_stream(structure, glyph)
    ok = out == src
    detail = "" if ok else _first_difference(src, out)
    check = Verification(view="clean", ok=ok, source_words=len(src),
                         output_words=len(out), detail=detail)
    if not ok:
        raise VerificationFailed(check.describe())
    return [check]


def discover_body_story(template_path: str | Path) -> str:
    """The Self id of the story the book flows into: the story bound to a text
    frame on a document spread (the primary text frame on page one), not one on
    a master. Falls back to the single non-backing story if the template has
    exactly one."""
    with zipfile.ZipFile(Path(template_path)) as z:
        names = z.namelist()
        story_ids = {n[len("Stories/Story_"):-len(".xml")]
                     for n in names if n.startswith("Stories/Story_")}
        for name in names:
            if not name.startswith("Spreads/"):
                continue
            root = ET.fromstring(z.read(name))
            for frame in root.iter():
                if frame.tag.endswith("TextFrame"):
                    parent = frame.get("ParentStory")
                    if parent in story_ids:
                        return parent
        if len(story_ids) == 1:
            return next(iter(story_ids))
    raise ValueError(
        f"Could not find the body story in {template_path}: no text frame on a "
        f"spread is bound to a story part.")


# ---------------------------------------------------------------------------
# Reading the cleaned manuscript.
# ---------------------------------------------------------------------------


def _read_cleaned_body(elements: dict, plan: PrepPlan,
                       sheet: StyleSheet) -> list[_Para]:
    """One _Para per surviving plan paragraph, in document order: its house
    style name (mapped from the Word styleId the clean writer set) and its runs
    with italic preserved.

    Iterating the plan — not every body paragraph — is what keeps hand-placed
    layout the ingest set aside (textboxes, headers) out of the flowed book,
    exactly as the clean writer does. Dropped paragraphs are skipped; glyph and
    styled paragraphs were mutated in place, so their element carries the final
    content."""
    id_to_name = {s.id: s.name for s in sheet.styles}
    body_role = sheet.roles.get("body", "")
    out: list[_Para] = []
    for item in plan.plans:
        if item.drop:
            continue
        p = elements.get(item.para_id)
        if p is None:
            continue
        style_id = _paragraph_style_id(p)
        name = id_to_name.get(style_id, body_role)
        out.append(_Para(style=name, runs=_runs_of(p)))
    return out


def _paragraph_style_id(p) -> str | None:
    ppr = p.find(PPR_TAG)
    if ppr is None:
        return None
    pstyle = ppr.find(PSTYLE_TAG)
    return pstyle.get(VAL) if pstyle is not None else None


def _runs_of(p) -> tuple[_Run, ...]:
    """The paragraph's text as italic/non-italic runs, in document order.

    Built from the same content elements paragraph_text walks — w:t text plus
    the tab and break elements that each render as one virtual character — so
    the words come out identical to what verification reads on the source side.
    A tab lost here would glue a table-of-contents entry to its page number
    ("One" + "1" -> "One1") and fail every file with one. Adjacent runs of the
    same slant are merged, matching what InDesign itself exports."""
    runs: list[_Run] = []
    for el in iter_content_elements(p):
        italic = _element_italic(el)
        text = (el.text or "") if el.tag == T_TAG else VIRTUAL_CHARS[el.tag]
        if not text:
            continue
        if runs and runs[-1].italic == italic:
            runs[-1] = _Run(text=runs[-1].text + text, italic=italic)
        else:
            runs.append(_Run(text=text, italic=italic))
    return tuple(runs)


def _element_italic(el) -> bool:
    """Whether the run containing this content element is italic."""
    node = el.getparent()
    while node is not None:
        if node.tag == R_TAG:
            return _is_italic(node)
        node = node.getparent()
    return False


def _is_italic(r) -> bool:
    rpr = r.find(RPR_TAG)
    if rpr is None:
        return False
    i = rpr.find(I_TAG)
    if i is None:
        return False
    return (i.get(VAL) or "true").lower() not in ("0", "false", "off")


# ---------------------------------------------------------------------------
# Writing the IDML.
# ---------------------------------------------------------------------------


def _emit_idml(paras: list[_Para], sheet: StyleSheet, template: Path,
               out: Path) -> None:
    body_story = discover_body_story(template)
    tmp = out.with_suffix(".build")
    if tmp.exists():
        shutil.rmtree(tmp)
    with zipfile.ZipFile(template) as z:
        z.extractall(tmp)
    try:
        style_self = _ensure_styles(tmp, sheet, paras)
        story_xml = _build_story_xml(paras, body_story, style_self)
        (tmp / "Stories" / f"Story_{body_story}.xml").write_text(
            story_xml, encoding="UTF-8")
        _repackage(tmp, out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _ensure_styles(root: Path, sheet: StyleSheet,
                   paras: list[_Para]) -> dict[str, str]:
    """Guarantee every house style the book uses is defined in the template's
    Styles.xml, and return name -> ParagraphStyle Self id.

    Styles already in the template (by name) keep their real Self; the rest are
    minted with a safe synthetic id and the house Name and formatting. The
    group container is NOT namespaced even though the file's root is — a style
    appended anywhere else is silently ignored by InDesign, dropping every
    paragraph to [Basic Paragraph]."""
    sx = root / "Resources" / "Styles.xml"
    tree = ET.parse(str(sx))
    rt = tree.getroot()
    pgroup = rt.find("RootParagraphStyleGroup")
    if pgroup is None:
        pgroup = ET.SubElement(rt, "RootParagraphStyleGroup", {"Self": "upsg"})

    style_self: dict[str, str] = {e.get("Name"): e.get("Self")
                                  for e in rt.iter("ParagraphStyle")}
    fmt_by_name = {s.name: s.format for s in sheet.styles}
    used = {p.style for p in paras if p.style}
    for i, name in enumerate(sorted(used)):
        if name in style_self:
            continue
        self_id = f"ParagraphStyle/hs{i}"
        style_self[name] = self_id
        attrs = {"Self": self_id, "Name": name, "Imported": "false",
                 "NextStyle": self_id, "KeyboardShortcut": "0 0"}
        font_style = _style_attrs(attrs, fmt_by_name.get(name, {}))
        if font_style:
            attrs["FontStyle"] = font_style
        ET.SubElement(pgroup, "ParagraphStyle", attrs)

    tree.write(str(sx), xml_declaration=True, encoding="UTF-8", standalone=True)
    return style_self


_ALIGN = {"left": "LeftAlign", "center": "CenterAlign", "right": "RightAlign",
          "justify": "LeftJustified"}


def _style_attrs(attrs: dict, fmt: dict) -> str:
    """Map a house_styles.yaml `format` block onto IDML ParagraphStyle
    attributes. Returns the font style ('Bold'/'Italic'/…) to set separately, or
    '' for regular. Only the keys the house set uses are mapped; InDesign owns
    everything else through the template."""
    if fmt.get("align"):
        attrs["Justification"] = _ALIGN.get(str(fmt["align"]), "LeftAlign")
    if fmt.get("size") is not None:
        attrs["PointSize"] = _num(fmt["size"])
    if fmt.get("indent") is not None:
        attrs["FirstLineIndent"] = _num(fmt["indent"])
    if fmt.get("space_before") is not None:
        attrs["SpaceBefore"] = _num(fmt["space_before"])
    if fmt.get("space_after") is not None:
        attrs["SpaceAfter"] = _num(fmt["space_after"])
    if fmt.get("page_break_before"):
        attrs["StartParagraph"] = "NextPage"
    if fmt.get("keep_next"):
        attrs["KeepWithNext"] = "2"
    bold, ital = fmt.get("bold"), fmt.get("italic")
    if bold and ital:
        return "Bold Italic"
    return "Bold" if bold else ("Italic" if ital else "")


def _build_story_xml(paras: list[_Para], body_story: str,
                     style_self: dict[str, str]) -> str:
    """One ParagraphStyleRange per paragraph, each terminated by a Br except the
    last — InDesign's own convention. Italic runs become their own
    CharacterStyleRange with a local FontStyle; everything else is plain."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f'<idPkg:Story xmlns:idPkg="{IDML_NS}" DOMVersion="21.4">',
        f'\t<Story Self="{_attr(body_story)}" UserText="true" '
        f'IsEndnoteStory="false" AppliedTOCStyle="n" TrackChanges="false" '
        f'StoryTitle="$ID/" AppliedNamedGrid="n">',
        '\t\t<StoryPreference OpticalMarginAlignment="false" '
        'OpticalMarginSize="12" FrameType="TextFrameType" '
        'StoryOrientation="Horizontal" StoryDirection="LeftToRightDirection" />',
        '\t\t<InCopyExportOption IncludeGraphicProxies="true" '
        'IncludeAllResources="false" />',
    ]
    n = len(paras)
    for i, para in enumerate(paras):
        applied = style_self.get(para.style, NORMAL_STYLE)
        lines.append(
            f'\t\t<ParagraphStyleRange AppliedParagraphStyle="{_attr(applied)}">')
        for run in para.runs or (_Run(text="", italic=False),):
            fs = ' FontStyle="Italic"' if run.italic else ""
            lines.append(
                f'\t\t\t<CharacterStyleRange '
                f'AppliedCharacterStyle="{NO_CHAR_STYLE}"{fs}>')
            lines.append(f'\t\t\t\t<Content>{_content(run.text)}</Content>')
            lines.append('\t\t\t</CharacterStyleRange>')
        if i != n - 1:                     # paragraph terminator, not on the last
            lines.append(
                f'\t\t\t<CharacterStyleRange AppliedCharacterStyle='
                f'"{NO_CHAR_STYLE}"><Br /></CharacterStyleRange>')
        lines.append('\t\t</ParagraphStyleRange>')
    lines.append('\t</Story>')
    lines.append('</idPkg:Story>')
    return "\n".join(lines) + "\n"


def _repackage(root: Path, out: Path) -> None:
    """Zip the tree into an .idml. mimetype must be the first entry and stored
    uncompressed — the OCF rule every IDML reader relies on."""
    if out.exists():
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(_stored_info("mimetype"), IDML_MIMETYPE)
        for path in sorted(root.rglob("*")):
            if path.is_dir() or path.name == "mimetype":
                continue
            z.write(path, str(path.relative_to(root)))


def _stored_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_STORED
    return info


# ---------------------------------------------------------------------------
# Reading an IDML story back (verification, and the corrections engine reuses it).
# ---------------------------------------------------------------------------


def _read_idml_story(idml_path: str | Path, body_story: str) -> list[_Para]:
    with zipfile.ZipFile(Path(idml_path)) as z:
        raw = z.read(f"Stories/Story_{body_story}.xml")
    rt = ET.fromstring(raw)
    out: list[_Para] = []
    for psr in rt.iter("ParagraphStyleRange"):
        text = "".join(c.text or "" for c in psr.iter("Content"))
        out.append(_Para(style="", runs=(_Run(text=text, italic=False),)))
    return out


def _first_difference(src: list[str], out: list[str]) -> str:
    for i, (a, b) in enumerate(zip(src, out)):
        if a != b:
            return f"first differs at word {i}: {a!r} vs {b!r}"
    if len(src) != len(out):
        longer, n = ("manuscript", len(src)) if len(src) > len(out) else (
            "output", len(out))
        return f"{longer} has {n} words, the other {min(len(src), len(out))}"
    return "streams differ"


def _num(v) -> str:
    """A number as InDesign writes it: an integral value has no trailing '.0'.
    The style editor stores sizes as floats (24.0), the shipped sheet as ints
    (18); both should land as '24' and '18'."""
    f = float(v)
    return str(int(f)) if f.is_integer() else str(f)


def _attr(s: str) -> str:
    return html.escape(s, quote=True)


def _content(s: str) -> str:
    return html.escape(s, quote=False)
