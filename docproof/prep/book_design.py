"""The interior design for the book output, read from a file.

Nothing about how the book sketch looks is written into the code — the page
geometry, the faces, the per-style formats, the running heads, the drop caps
and the subject-matter display faces all come from config/prep/book_design.yaml
(or a replacement dropped into the prep override directory). This module is the
only thing that knows what that file looks like; the book writer and the
subject detector ask it rather than carrying copies.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("docproof.prep.book_design")

BOOK_DESIGN_FILE = "book_design.yaml"


class BookDesignError(Exception):
    """A book design that cannot be used. The message names the file and the
    problem, because whoever hits this is editing YAML, not Python."""


@dataclass(frozen=True)
class BookMeta:
    """What the book output needs to know about THIS manuscript: the subject
    that picks the display face, and the strings on the running heads. Filled
    by the detector, overridden per job, defaulted from the file name."""
    subject: str = ""
    title: str = ""
    author: str = ""
    detected: bool = False


@dataclass(frozen=True)
class Margins:
    top: float
    bottom: float
    inside: float
    outside: float
    header: float
    footer: float


@dataclass(frozen=True)
class Page:
    width: float
    height: float
    margins: Margins
    mirror: bool = True


@dataclass(frozen=True)
class Font:
    """One face the design uses: the family name Word matches on, and the
    files to embed so matching succeeds on a machine without the font."""
    family: str
    files: tuple[Path, ...] = ()


@dataclass(frozen=True)
class Subject:
    key: str
    family: str
    file: Path | None
    describe: str


@dataclass(frozen=True)
class RunningHeads:
    enabled: bool = True         # false leaves the head band empty
    verso: str = "author"        # "author" | "title"
    recto: str = "title"
    size: float = 9
    caps: bool = True
    letterspace: float = 2


@dataclass(frozen=True)
class DropCaps:
    lines: int = 3
    after: tuple[str, ...] = ()


@dataclass(frozen=True)
class BookDesign:
    version: int
    name: str
    page: Page
    fonts: dict[str, Font]                  # "body" | "heading"
    styles: dict[str, dict]                 # house style name -> format
    display_styles: tuple[str, ...]
    subjects: dict[str, Subject]
    default_subject: str
    running_heads: RunningHeads
    folio_size: float
    folio: bool
    drop_caps: DropCaps
    path: str

    # -- lookups --------------------------------------------------------------

    @property
    def subject_choices(self) -> tuple[str, ...]:
        return tuple(self.subjects)

    @property
    def needs_meta(self) -> bool:
        """Whether this design reads the manuscript's subject, title and
        author. A plain design that sets nothing in the display face and hangs
        no running heads uses none of them, so the run can skip the detection
        call — and its cost — entirely."""
        return bool(self.display_styles) or self.running_heads.enabled

    def subject(self, key: str | None) -> Subject:
        """The subject to design for, falling back to the default for an
        unknown or absent key — a book sketch with the wrong title face is
        better than no book sketch."""
        if key and key in self.subjects:
            return self.subjects[key]
        if key:
            log.warning("No subject %r in %s; using %r.", key, self.path,
                        self.default_subject)
        return self.subjects[self.default_subject]

    def describe_subjects(self) -> str:
        """The subject choices as prompt text, straight from the file."""
        return "\n".join(f"- {s.key} — {' '.join(s.describe.split())}"
                         for s in self.subjects.values())

    def display_font(self, subject_key: str | None) -> Font:
        s = self.subject(subject_key)
        return Font(family=s.family, files=(s.file,) if s.file else ())

    def embed_files(self, subject_key: str | None) -> tuple[Path, ...]:
        """Every font file the book file needs, body and heading faces first,
        then the subject's display face. Deduplicated, order kept."""
        files: list[Path] = []
        for font in self.fonts.values():
            files.extend(font.files)
        files.extend(self.display_font(subject_key).files)
        seen: set[Path] = set()
        out = []
        for f in files:
            if f not in seen:
                seen.add(f)
                out.append(f)
        return tuple(out)


def load_book_design(path: str | Path, *,
                     override_dir: str | Path | None = None) -> BookDesign:
    """Read the interior design, preferring a replacement where one exists.
    Shadowing is wholesale, for the same reason the style sheet's is: a design
    is one coherent look."""
    source = Path(path)
    if override_dir:
        replacement = Path(override_dir) / source.name
        if replacement.is_file():
            log.info("Using the book design at %s", replacement)
            source = replacement
    if not source.is_file():
        raise BookDesignError(f"No book design at {source.resolve()}")
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise BookDesignError(f"{source} is not readable YAML: {e}") from e
    if not isinstance(raw, dict):
        raise BookDesignError(f"{source}: the file must be a YAML mapping.")

    base = source.parent

    page_raw = raw.get("page") or {}
    margins_raw = page_raw.get("margins") or {}
    try:
        margins = Margins(
            top=float(margins_raw.get("top", 0.9)),
            bottom=float(margins_raw.get("bottom", 0.8)),
            inside=float(margins_raw.get("inside", 0.75)),
            outside=float(margins_raw.get("outside", 0.65)),
            header=float(margins_raw.get("header", 0.5)),
            footer=float(margins_raw.get("footer", 0.45)))
        page = Page(width=float(page_raw.get("width", 5.5)),
                    height=float(page_raw.get("height", 8.5)),
                    margins=margins,
                    mirror=bool(page_raw.get("mirror_margins", True)))
    except (TypeError, ValueError) as e:
        raise BookDesignError(f"{source}: page geometry must be numbers "
                              f"(inches): {e}") from e
    if page.width <= 0 or page.height <= 0:
        raise BookDesignError(f"{source}: page width and height must be "
                              f"positive.")

    fonts: dict[str, Font] = {}
    for key, entry in (raw.get("fonts") or {}).items():
        if not isinstance(entry, dict) or not entry.get("family"):
            raise BookDesignError(
                f"{source}: fonts.{key} needs at least a family name.")
        fonts[str(key)] = Font(
            family=str(entry["family"]),
            files=_files(source, base, entry.get("files") or [],
                         where=f"fonts.{key}"))
    for required in ("body", "heading"):
        if required not in fonts:
            raise BookDesignError(f"{source}: fonts must define '{required}'.")

    styles = {}
    for name, fmt in (raw.get("styles") or {}).items():
        if not isinstance(fmt, dict):
            raise BookDesignError(
                f"{source}: styles['{name}'] must be a mapping of format keys.")
        font = fmt.get("font")
        if font is not None and font not in ("display", *fonts):
            raise BookDesignError(
                f"{source}: styles['{name}'] wants font {font!r}, which is "
                f"neither 'display' nor one of: {', '.join(fonts)}.")
        styles[str(name)] = dict(fmt)

    subjects: dict[str, Subject] = {}
    for key, entry in (raw.get("subjects") or {}).items():
        if not isinstance(entry, dict) or not entry.get("family"):
            raise BookDesignError(
                f"{source}: subjects.{key} needs at least a display family.")
        file_raw = str(entry.get("file") or "")
        file = _files(source, base, [file_raw],
                      where=f"subjects.{key}")[0] if file_raw else None
        subjects[str(key)] = Subject(key=str(key),
                                     family=str(entry["family"]),
                                     file=file,
                                     describe=str(entry.get("describe", "")))
    if not subjects:
        raise BookDesignError(f"{source}: subjects must define at least one "
                              f"subject-matter entry.")
    default_subject = str(raw.get("default_subject") or next(iter(subjects)))
    if default_subject not in subjects:
        raise BookDesignError(
            f"{source}: default_subject '{default_subject}' is not one of the "
            f"subjects defined in this file.")

    # `running_heads: false` (or an empty mapping with enabled: false) leaves
    # the pages bare — no author/title band. Anything else is the mapping.
    heads_field = raw.get("running_heads")
    heads_enabled = heads_field is not False
    heads_raw = heads_field if isinstance(heads_field, dict) else {}
    heads_enabled = heads_enabled and bool(heads_raw.get("enabled", True))
    for side in ("verso", "recto"):
        value = heads_raw.get(side)
        if value is not None and value not in ("author", "title"):
            raise BookDesignError(
                f"{source}: running_heads.{side} must be 'author' or 'title'.")
    heads = RunningHeads(
        enabled=heads_enabled,
        verso=str(heads_raw.get("verso", "author")),
        recto=str(heads_raw.get("recto", "title")),
        size=float(heads_raw.get("size", 9)),
        caps=bool(heads_raw.get("caps", True)),
        letterspace=float(heads_raw.get("letterspace", 2)))

    drops_raw = raw.get("drop_caps") or {}
    drops = DropCaps(lines=int(drops_raw.get("lines", 3) or 0),
                     after=tuple(str(n) for n in drops_raw.get("after") or ()))

    # `folio: false` prints no page numbers; otherwise the mapping's size.
    folio_field = raw.get("folio")
    folio_enabled = folio_field is not False
    folio_raw = folio_field if isinstance(folio_field, dict) else {}

    design = BookDesign(
        version=int(raw.get("version", 1)),
        name=str(raw.get("name") or source.stem),
        page=page, fonts=fonts, styles=styles,
        display_styles=tuple(str(n) for n in raw.get("display_styles") or ()),
        subjects=subjects, default_subject=default_subject,
        running_heads=heads,
        folio_size=float(folio_raw.get("size", 10)),
        folio=folio_enabled,
        drop_caps=drops, path=str(source))
    log.info("Book design '%s' v%d: %d styled style(s), %d subject(s), "
             "default '%s'.", design.name, design.version, len(styles),
             len(subjects), default_subject)
    return design


def _files(source: Path, base: Path, entries, *, where: str) -> tuple[Path, ...]:
    out = []
    for entry in entries:
        p = Path(str(entry))
        if not p.is_absolute():
            p = base / p
        if not p.is_file():
            raise BookDesignError(
                f"{source}: {where} names {entry!r}, and there is no such "
                f"file at {p}.")
        out.append(p)
    return tuple(out)
