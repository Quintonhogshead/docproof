"""The house style set, read from a file, and the Word style sheet built from it.

Nothing about Atmosphere Press is hard-coded anywhere in prep. This module is
the only thing that knows what a style sheet looks like, and everything
downstream — the tagger's allowed answers, both writers, the prep notes — asks
it rather than carrying its own copy of the list. Dropping in a different house
style set is editing YAML.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from lxml import etree

from ..utils.xml_helpers import CT_NS, PR_NS, W_NS, qn

log = logging.getLogger("docproof.prep.styles")

STYLES_PART = "word/styles.xml"
_CT_STYLES = ("application/vnd.openxmlformats-officedocument."
              "wordprocessingml.styles+xml")
_REL_STYLES = ("http://schemas.openxmlformats.org/officeDocument/2006/"
               "relationships/styles")

# The two answers that are not house styles. `rules` turns them into styles;
# the model never sees a style name for either.
BODY = "body"
SPACING = "spacing"

# Word styleIds are far more restricted than style names — no spaces, no "/",
# no "#" — which is exactly why the sheet carries both.
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")

_ROLES = ("body", "body_first", "scene_break", "table")


class StyleSheetError(Exception):
    """A style sheet that cannot be used. The message names the file and the
    problem, because whoever hits this is editing YAML, not Python."""


@dataclass(frozen=True)
class Style:
    name: str            # exactly as the InDesign template spells it
    id: str              # the Word styleId
    describe: str
    assign: str          # "model" | "derived"
    opens: bool          # a body paragraph after this one is "body first"
    format: dict

    @property
    def model_assigned(self) -> bool:
        return self.assign == "model"


@dataclass(frozen=True)
class StyleSheet:
    version: int
    name: str
    trim: str
    scene_break_glyph: str
    styles: tuple[Style, ...]
    character_styles: tuple[Style, ...]
    roles: dict[str, str]
    pseudo: tuple[tuple[str, str], ...]     # (key, describe)
    path: str

    # -- lookups --------------------------------------------------------------

    def by_name(self, name: str) -> Style:
        for s in self.styles:
            if s.name == name:
                return s
        raise KeyError(name)

    def has(self, name: str) -> bool:
        return any(s.name == name for s in self.styles)

    def role(self, key: str) -> Style:
        """The style playing a part in the rules — 'body_first', 'scene_break'."""
        return self.by_name(self.roles[key])

    @property
    def model_choices(self) -> tuple[str, ...]:
        """Everything the model may answer, in sheet order, with the two
        pseudo-roles last. This tuple becomes the schema's enum, so an answer
        outside the house style set is impossible on the wire."""
        return tuple([s.name for s in self.styles if s.model_assigned]
                     + [key for key, _ in self.pseudo])

    def describe_choices(self) -> str:
        """The allowed answers as prompt text, straight from the sheet."""
        lines = []
        for s in self.styles:
            if s.model_assigned:
                lines.append(f"- {s.name} — {_one_line(s.describe)}")
        for key, describe in self.pseudo:
            lines.append(f"- {key} — {_one_line(describe)}")
        return "\n".join(lines)

    def opens_block(self, name: str) -> bool:
        return self.has(name) and self.by_name(name).opens

    @property
    def link_style(self) -> Style | None:
        """The character style for URLs, if the sheet defines one. Matched by
        name so a sheet can call it whatever the template calls it."""
        for s in self.character_styles:
            if "link" in s.name.lower():
                return s
        return self.character_styles[0] if self.character_styles else None


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


# --- loading ------------------------------------------------------------------

def load_style_sheet(path: str | Path, *,
                     override_dir: str | Path | None = None) -> StyleSheet:
    """Read the house style set, preferring a replacement where one exists.

    `override_dir` is where the app looks for a sheet the publisher dropped in
    themselves. It shadows the shipped file wholesale rather than per key: a
    style set is one coherent list, and half of one house's names mixed with
    half of another's would tag a manuscript into a template that doesn't
    exist."""
    source = Path(path)
    if override_dir:
        replacement = Path(override_dir) / source.name
        if replacement.is_file():
            log.info("Using the style sheet at %s", replacement)
            source = replacement
    if not source.is_file():
        raise StyleSheetError(f"No style sheet at {source.resolve()}")
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise StyleSheetError(f"{source} is not readable YAML: {e}") from e
    if not isinstance(raw, dict):
        raise StyleSheetError(f"{source}: the file must be a YAML mapping.")

    styles = tuple(_style(source, entry) for entry in raw.get("styles") or [])
    chars = tuple(_style(source, entry, character=True)
                  for entry in raw.get("character_styles") or [])
    if not styles:
        raise StyleSheetError(f"{source}: no paragraph styles defined.")

    _check_unique(source, styles + chars)
    roles = dict(raw.get("roles") or {})
    missing = [r for r in _ROLES if r not in roles]
    if missing:
        raise StyleSheetError(
            f"{source}: roles is missing {', '.join(missing)}. Every role must "
            f"name one of the styles in this file, so the rules can talk about "
            f"parts rather than house names.")
    names = {s.name for s in styles}
    for role, name in roles.items():
        if name not in names:
            raise StyleSheetError(
                f"{source}: role '{role}' points at '{name}', which is not a "
                f"style in this file.")

    pseudo = tuple((str(p["key"]), str(p.get("describe", "")))
                   for p in raw.get("pseudo_roles") or []
                   if isinstance(p, dict) and p.get("key"))
    for required in (BODY, SPACING):
        if required not in {k for k, _ in pseudo}:
            raise StyleSheetError(
                f"{source}: pseudo_roles must define '{required}' — it is how "
                f"the model reports "
                + ("running text." if required == BODY else "a blank line."))

    sheet = StyleSheet(
        version=int(raw.get("version", 1)),
        name=str(raw.get("name") or source.stem),
        trim=str(raw.get("trim") or ""),
        scene_break_glyph=str(raw.get("scene_break_glyph") or "***"),
        styles=styles, character_styles=chars, roles=roles, pseudo=pseudo,
        path=str(source),
    )
    log.info("Style sheet '%s' v%d: %d paragraph styles (%d the model picks), "
             "%d character style(s)", sheet.name, sheet.version, len(styles),
             len([s for s in styles if s.model_assigned]), len(chars))
    return sheet


def _style(source: Path, entry, *, character: bool = False) -> Style:
    if not isinstance(entry, dict):
        raise StyleSheetError(f"{source}: every style must be a mapping.")
    name, style_id = entry.get("name"), entry.get("id")
    if not name or not style_id:
        raise StyleSheetError(
            f"{source}: every style needs a name and an id; got {entry!r}.")
    if not _ID_RE.match(str(style_id)):
        raise StyleSheetError(
            f"{source}: id '{style_id}' is not a usable Word style id — "
            f"letters and digits only, starting with a letter. The house name "
            f"stays in `name`; `id` is only Word's internal handle.")
    assign = str(entry.get("assign", "model" if not character else "derived"))
    if assign not in ("model", "derived"):
        raise StyleSheetError(
            f"{source}: style '{name}' has assign: {assign!r}; it must be "
            f"'model' (the model may pick it) or 'derived' (only the rules "
            f"apply it).")
    return Style(name=str(name), id=str(style_id),
                 describe=str(entry.get("describe", "")), assign=assign,
                 opens=bool(entry.get("opens", False)),
                 format=dict(entry.get("format") or {}))


def _check_unique(source: Path, styles) -> None:
    for field in ("name", "id"):
        seen: set[str] = set()
        for s in styles:
            value = getattr(s, field)
            if value in seen:
                raise StyleSheetError(
                    f"{source}: two styles share the {field} '{value}'. "
                    f"InDesign matches on the name and Word on the id, so both "
                    f"have to be unique.")
            seen.add(value)


# --- the Word style sheet -----------------------------------------------------

def _sub(parent, tag: str, **attrs):
    return etree.SubElement(parent, qn(tag),
                            {qn(f"w:{k}"): str(v) for k, v in attrs.items()})


def _ppr(style: Style):
    """A w:pPr for one style. Children are emitted in schema order — Word is
    forgiving about it, but the OOXML validator built into some environments
    is not."""
    fmt = style.format
    ppr = etree.Element(qn("w:pPr"))
    if fmt.get("keep_next"):
        _sub(ppr, "w:keepNext", val="1")
    if fmt.get("page_break_before"):
        _sub(ppr, "w:pageBreakBefore", val="1")
    before, after = fmt.get("space_before"), fmt.get("space_after")
    line = fmt.get("line")                   # a line-spacing multiple
    if before is not None or after is not None or line is not None:
        spacing = _sub(ppr, "w:spacing")
        if before is not None:
            spacing.set(qn("w:before"), str(int(float(before) * 20)))
        if after is not None:
            spacing.set(qn("w:after"), str(int(float(after) * 20)))
        if line is not None:
            spacing.set(qn("w:line"), str(int(float(line) * 240)))
            spacing.set(qn("w:lineRule"), "auto")
    indent = fmt.get("indent")
    if indent is not None:
        twips = int(float(indent) * 20)
        key = "firstLine" if twips >= 0 else "hanging"
        _sub(ppr, "w:ind", **{key: abs(twips)})
    if fmt.get("align"):
        value = str(fmt["align"])
        _sub(ppr, "w:jc", val="both" if value == "justify" else value)
    return ppr if len(ppr) else None


def _rpr(style: Style):
    fmt = style.format
    rpr = etree.Element(qn("w:rPr"))
    if fmt.get("family"):                    # the face, by internal family name
        family = str(fmt["family"])
        _sub(rpr, "w:rFonts", ascii=family, hAnsi=family, cs=family)
    if fmt.get("bold"):
        _sub(rpr, "w:b", val="1")
    if fmt.get("italic"):
        _sub(rpr, "w:i", val="1")
    if fmt.get("caps"):
        _sub(rpr, "w:caps", val="1")
    if fmt.get("letterspace") is not None:   # points, as w:spacing twentieths
        _sub(rpr, "w:spacing", val=int(float(fmt["letterspace"]) * 20))
    if fmt.get("size") is not None:
        half = int(float(fmt["size"]) * 2)
        _sub(rpr, "w:sz", val=half)
        _sub(rpr, "w:szCs", val=half)
    return rpr if len(rpr) else None


def build_styles_xml(sheet: StyleSheet, *,
                     defaults: dict | None = None) -> etree._Element:
    """One clean style sheet: Normal, every house paragraph style, and the
    character styles. For the placed file the formatting is deliberately light —
    InDesign overrides all of it on Place, and the point of the file is the
    NAMES. The book writer passes `defaults` (family, size, line) to move the
    document's base face off the readable-anywhere Georgia default."""
    base = {"family": "Georgia", "size": 12, "line": 1.15, **(defaults or {})}
    root = etree.Element(qn("w:styles"), nsmap={"w": W_NS})

    d = _sub(root, "w:docDefaults")
    rpr_default = _sub(d, "w:rPrDefault")
    rpr = _sub(rpr_default, "w:rPr")
    _sub(rpr, "w:rFonts", ascii=base["family"], hAnsi=base["family"],
         cs=base["family"])
    half = str(int(float(base["size"]) * 2))
    _sub(rpr, "w:sz", val=half)
    _sub(rpr, "w:szCs", val=half)
    ppr_default = _sub(d, "w:pPrDefault")
    ppr = _sub(ppr_default, "w:pPr")
    _sub(ppr, "w:spacing", after="0",
         line=str(int(float(base["line"]) * 240)), lineRule="auto")

    normal = _sub(root, "w:style", type="paragraph", default="1",
                  styleId="Normal")
    _sub(normal, "w:name", val="Normal")
    _sub(normal, "w:qFormat")

    for style in sheet.styles:
        el = _sub(root, "w:style", type="paragraph", styleId=style.id)
        _sub(el, "w:name", val=style.name)
        _sub(el, "w:basedOn", val="Normal")
        _sub(el, "w:qFormat")
        for part in (_ppr(style), _rpr(style)):
            if part is not None:
                el.append(part)

    for style in sheet.character_styles:
        el = _sub(root, "w:style", type="character", styleId=style.id)
        _sub(el, "w:name", val=style.name)
        _sub(el, "w:uiPriority", val="99")
        part = _rpr(style)
        if part is not None:
            el.append(part)
    return root


def install_style_sheet(pkg, sheet: StyleSheet, *, replace: bool = True,
                        defaults: dict | None = None) -> None:
    """Put the house styles into the document, registering the part if it never
    had one (a bare Google Docs export sometimes doesn't).

    `replace` writes one clean style sheet and nothing else — what the file
    placed into InDesign should be. The tracked-changes file merges instead:
    every retagged paragraph carries its previous style inside a `w:pPrChange`,
    and rejecting that change has to land back on a style that still exists."""
    built = build_styles_xml(sheet, defaults=defaults)
    if not pkg.has(STYLES_PART):
        pkg.add_part(STYLES_PART, built)
        _register_part(pkg)
        return

    root = pkg.tree(STYLES_PART)
    pkg.mark_modified(STYLES_PART)
    if replace:
        root.clear()
        for key, value in built.attrib.items():
            root.set(key, value)
        for child in built:
            root.append(child)
        log.info("Installed the '%s' style sheet: %d paragraph styles.",
                 sheet.name, len(sheet.styles))
        return

    ours_ids = {s.id for s in sheet.styles} | {s.id for s in sheet.character_styles}
    ours_names = {s.name for s in sheet.styles} | {s.name
                                                  for s in sheet.character_styles}
    for existing in root.findall(qn("w:style")):
        name_el = existing.find(qn("w:name"))
        name = name_el.get(qn("w:val")) if name_el is not None else None
        if existing.get(qn("w:styleId")) in ours_ids or name in ours_names:
            root.remove(existing)
    for style_el in built.findall(qn("w:style")):
        if style_el.get(qn("w:styleId")) == "Normal":
            continue                      # the document has its own
        root.append(style_el)
    log.info("Added %d house style(s) alongside the document's own.",
             len(sheet.styles) + len(sheet.character_styles))


def _register_part(pkg) -> None:
    """Content-type override + document relationship for a styles part we just
    created. (reassembler._Comments does the same dance for comments.xml; it
    predates this helper and is left alone deliberately.)"""
    ct = pkg.tree("[Content_Types].xml")
    pkg.mark_modified("[Content_Types].xml")
    etree.SubElement(ct, f"{{{CT_NS}}}Override",
                     {"PartName": f"/{STYLES_PART}", "ContentType": _CT_STYLES})

    rels_name = "word/_rels/document.xml.rels"
    if pkg.has(rels_name):
        rels = pkg.tree(rels_name)
        pkg.mark_modified(rels_name)
    else:
        rels = etree.Element(f"{{{PR_NS}}}Relationships", nsmap={None: PR_NS})
        pkg.add_part(rels_name, rels)
    used = {r.get("Id") for r in rels}
    n = 1
    while f"rId{n}" in used:
        n += 1
    etree.SubElement(rels, f"{{{PR_NS}}}Relationship",
                     {"Id": f"rId{n}", "Type": _REL_STYLES,
                      "Target": "styles.xml"})
