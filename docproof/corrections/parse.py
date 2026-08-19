"""Turning a structured corrections list into an `Edit` list.

This is the machine-readable half of parsing. A corrections source that already
arrives in a find/replace shape — a list a person typed, or JSON a model emitted
after reading a corrections document — becomes the `Edit` objects `apply`
consumes. The tracked-changes-PDF parser is a separate, future module; both land
here, in the same `Edit` list, so the deterministic core never learns where a
correction came from.

Nothing is guessed and nothing is dropped silently, the same contract the apply
and verify stages keep: an entry that cannot become an `Edit` comes back as a
`ParseIssue` with the reason and the raw entry, not as a missing correction. A
source that is malformed as a *whole* (not valid JSON, not a list of entries) is
a caller error and raises; only per-entry problems become issues.

Accepted inputs, all resolving to a list of entries:

  * a list of entries already in memory;
  * a JSON `str` or `bytes` — an array, or an object with an ``"edits"`` key;
  * a `pathlib.Path` to a ``.json`` file of the same;
  * a lone dict, treated as a one-entry list.

An entry is either a mapping (its fields named — see `_ALIASES` for the spellings
accepted) or a short sequence ``[find, replace]`` / ``[find, replace,
instruction]`` for a terse typed list.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import DESIGN, FORMATS, JUDGMENT, MECHANICAL, PARA_OPS, Edit
from .overgrab import repair_pair
from .textmatch import normalize

KINDS = (MECHANICAL, JUDGMENT, DESIGN)

# The shortest `find` that can stand as an address on its own, in normalized
# characters. Below this a find is not a location but a character class: a bare ","
# has about seven thousand homes in a novel and a bare "." rather more, and an edit
# carrying one can only ever be counted and flagged.
#
# Such a find is an extraction failure, not a correction, and it is refused here so
# it reads as one. The alternative — which is what used to happen — is that it
# travels all the way to `apply`, matches thousands of places, and is reported as
# "the text appears 6947 times": true, useless, and indistinguishable from a
# genuinely ambiguous correction. An entry refused here still reaches a person: its
# reviewer comment is accounted for in the change log as one no edit was made for.
#
# A short find WITH an anchor is fine and common — "Replace single quote with
# double" is a one-character find, and its page or its marked line is what places
# it — so the guard only fires when there is nothing to place it by.
MIN_ANCHORABLE_FIND = 3

# A PDF comment's id, `p{page}-{n}` (see `from_pdf.PdfComment`). An edit echoes
# that id in `source`, so the page a correction was marked on is already on the
# edit and does not have to be asked of the model — which is the point: a page read
# off an id is deterministic, and one a model retypes is a thing it can get wrong.
# The first comment id in `source` — which may name several, when one edit
# answers for more than one mark. They are all on the same page by
# construction (a mark belongs to the page it was made on), so the first
# one settles it.
_SOURCE_PAGE = re.compile(r"^p(\d+)-\d+(?:\s|$)")


def page_from_source(source: str) -> int:
    """The proof page a comment id names, or 0 if it names none.

    >>> page_from_source("p49-2")
    49
    """
    m = _SOURCE_PAGE.match((source or "").strip())
    return int(m.group(1)) if m else 0

# The canonical Edit field -> the spellings a person or a model naturally reaches
# for. `find`/`replace` are the model's own names; the rest are what a corrections
# note tends to say. When more than one alias is present, the first listed wins.
_ALIASES: dict[str, tuple[str, ...]] = {
    "find": ("find", "original", "original_text", "from", "old", "before",
             "search", "text"),
    "replace": ("replace", "replacement", "corrected", "correction", "to",
                "new", "after"),
    "instruction": ("instruction", "note", "comment", "reason", "message"),
    "kind": ("kind", "type", "category"),
    "occurrence": ("occurrence", "occ", "nth", "instance"),
    "id": ("id", "ref", "identifier"),
    "context": ("context", "near", "within", "around"),
    "source": ("source", "comment_id", "src", "from_comment"),
    "page": ("page", "page_number", "pageno", "p"),
    "format": ("format", "formatting", "style", "character_style"),
    "paragraph": ("paragraph", "paragraph_op", "layout", "para"),
    "paragraph_style": ("paragraph_style", "para_style", "applied_style"),
}


@dataclass(frozen=True)
class ParseIssue:
    """An entry that could not become an `Edit`, kept with its reason and the raw
    entry so a person can find and fix it — never silently dropped."""
    index: int                         # position in the input list, 0-based
    reason: str
    raw: Any = None


@dataclass(frozen=True)
class ParseResult:
    """The edits parsed cleanly, and the entries that could not be."""
    edits: tuple[Edit, ...]
    issues: tuple[ParseIssue, ...]

    @property
    def ok(self) -> bool:
        """Every entry became an edit."""
        return not self.issues

    def summary(self) -> str:
        return (f"{len(self.edits)} edit(s) parsed, "
                f"{len(self.issues)} entr(y/ies) could not be read")


def parse_edits(source: Any, *, id_prefix: str = "c") -> ParseResult:
    """Parse a structured corrections source into a `ParseResult`.

    `id_prefix` names the auto-generated ids (``c1``, ``c2``, …) given to entries
    that carry none; a generated id never collides with an explicit one. Raises
    `ValueError` if the whole source cannot be read as a list of entries."""
    entries = _load(source)

    # Explicit ids are gathered first so a generated id never lands on one, and
    # so a duplicate explicit id can be told apart from a mere collision.
    explicit: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict):
            raw_id = _field(entry, "id")
            if raw_id not in (None, ""):
                explicit.add(str(raw_id).strip())

    edits: list[Edit] = []
    issues: list[ParseIssue] = []
    used: set[str] = set()
    auto = 0

    for i, entry in enumerate(entries):
        parsed = _parse_entry(entry, i)
        if isinstance(parsed, ParseIssue):
            issues.append(parsed)
            continue
        fields, explicit_id = parsed
        if explicit_id is not None:
            if explicit_id in used:
                issues.append(ParseIssue(
                    i, f"duplicate id {explicit_id!r}", entry))
                continue
            eid = explicit_id
        else:
            auto += 1
            eid = f"{id_prefix}{auto}"
            while eid in explicit or eid in used:
                auto += 1
                eid = f"{id_prefix}{auto}"
        used.add(eid)
        edits.append(Edit(id=eid, **fields))

    return ParseResult(edits=tuple(edits), issues=tuple(issues))


# --- loading the source -------------------------------------------------------

def _load(source: Any) -> list:
    """Normalize any accepted input to a list of entries, or raise `ValueError`
    if the source as a whole is not one."""
    if isinstance(source, Path):
        try:
            source = source.read_text(encoding="utf-8")
        except OSError as e:
            raise ValueError(f"could not read {source}: {e}") from e
    if isinstance(source, (str, bytes)):
        try:
            source = json.loads(source)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"not valid JSON: {e}") from e
    # A wrapper object — {"edits": [...]} — is what a model tends to emit; unwrap
    # it. A lone edit dict (no "edits" key) is treated as a one-entry list.
    if isinstance(source, dict):
        if "edits" in source:
            source = source["edits"]
        else:
            source = [source]
    if not isinstance(source, list):
        raise ValueError("expected a list of corrections (or an object with an "
                         f"'edits' list), got {type(source).__name__}")
    return source


# --- one entry ----------------------------------------------------------------

def _parse_entry(entry: Any, index: int):
    """Turn one entry into (fields-for-Edit, explicit-id-or-None), or a
    `ParseIssue`. `fields` never contains `id`; the caller assigns it."""
    if isinstance(entry, (list, tuple)):
        return _parse_sequence(entry, index)
    if not isinstance(entry, dict):
        return ParseIssue(index, f"entry is a {type(entry).__name__}, not an "
                          "object or a [find, replace] pair", entry)

    find = _field(entry, "find")
    replace = _field(entry, "replace")
    instruction = _field(entry, "instruction")
    context = _field(entry, "context")
    source = _field(entry, "source")
    raw_kind = _field(entry, "kind")
    raw_occ = _field(entry, "occurrence")
    raw_id = _field(entry, "id")
    raw_page = _field(entry, "page")
    raw_format = _field(entry, "format")
    raw_para = _field(entry, "paragraph")
    raw_para_style = _field(entry, "paragraph_style")

    kind = _norm_kind(raw_kind)
    if kind is None:
        return ParseIssue(index, f"unknown kind {raw_kind!r} (expected one of "
                          f"{', '.join(KINDS)})", entry)

    occ = _norm_occurrence(raw_occ)
    if occ is None:
        return ParseIssue(index, f"occurrence {raw_occ!r} is not a whole number "
                          "≥ 0", entry)

    page = _norm_occurrence(raw_page)
    if page is None:
        return ParseIssue(index, f"page {raw_page!r} is not a whole number ≥ 0",
                          entry)
    if not page and isinstance(source, str):
        page = page_from_source(source)      # free, from the comment id it cites

    fmt = _norm_format(raw_format)
    if fmt is None:
        return ParseIssue(index, f"unknown format {raw_format!r} (expected one of "
                          f"{', '.join(FORMATS)})", entry)

    para_op = "" if raw_para in (None, "") else str(raw_para).strip().lower()
    if para_op and para_op not in PARA_OPS:
        return ParseIssue(index, f"unknown paragraph operation {raw_para!r} "
                          f"(expected one of {', '.join(PARA_OPS)})", entry)
    para_style = "" if raw_para_style in (None, "") else str(raw_para_style).strip()

    fields = _assemble(find, replace, instruction, kind, occ, index, entry,
                       context=context, source=source, page=page, fmt=fmt,
                       para_op=para_op, para_style=para_style)
    if isinstance(fields, ParseIssue):
        return fields
    explicit_id = str(raw_id).strip() if raw_id not in (None, "") else None
    if explicit_id == "":
        explicit_id = None
    return fields, explicit_id


def _parse_sequence(entry, index: int):
    """A terse ``[find, replace]`` or ``[find, replace, instruction]`` entry."""
    if not 2 <= len(entry) <= 3:
        return ParseIssue(index, "a list entry must be [find, replace] or "
                          f"[find, replace, instruction], got {len(entry)} "
                          "item(s)", list(entry))
    find, replace = entry[0], entry[1]
    instruction = entry[2] if len(entry) == 3 else None
    fields = _assemble(find, replace, instruction, MECHANICAL, 0, index,
                       list(entry))
    if isinstance(fields, ParseIssue):
        return fields
    return fields, None


def _assemble(find, replace, instruction, kind, occ, index, raw, *, context=None,
              source=None, page=0, fmt="", para_op="", para_style=""):
    """Validate the text fields common to both entry shapes and build the kwargs
    for `Edit` (no id). Returns a dict or a `ParseIssue`."""
    if find is not None and not isinstance(find, str):
        return ParseIssue(index, "find is not text", raw)
    if replace is not None and not isinstance(replace, str):
        return ParseIssue(index, "replace is not text", raw)
    if context is not None and not isinstance(context, str):
        return ParseIssue(index, "context is not text", raw)
    find = find or ""
    replace = replace or ""
    context = context or ""
    source = source if isinstance(source, str) else ""
    instruction = "" if instruction is None else str(instruction)

    if kind == DESIGN:
        # A layout request carries its meaning in the instruction; it need not
        # anchor to text at all, but it must say *something*.
        if not find and not instruction.strip():
            return ParseIssue(index, "a design entry needs a find anchor or an "
                              "instruction describing the request", raw)
    else:
        # An over-grabbing pair pulled back to the change its note describes —
        # see `overgrab`. It runs here, on the one funnel both a typed list and
        # an extracted one come through, because the failure it repairs is
        # invisible everywhere downstream: `apply` writes the pair it is given,
        # and `verify` then confirms the file matches the pair, so a find that
        # swallowed a closing quotation mark is reported as applied exactly. A
        # repair only ever shortens the run written over, and never one a
        # formatting or paragraph request named (those change no words, and
        # their find is copied into replace unchanged).
        if not fmt and not para_op:
            find, replace = repair_pair(find, replace, instruction)
            # A judgment is a question, not a change to carry out. One the extractor
            # left with an empty replacement — it could not commit to a rewrite —
            # would read as a deletion, and a deletion is the one thing a
            # mis-handled query must never do. It never applies (apply treats every
            # judgment as a person's call), but normalizing it to a no-op shape here
            # keeps it clear of the deletion path and lets the second look re-read it
            # as the query it is.
            if kind == JUDGMENT and find and not replace:
                replace = find
        # A text edit anchors to the exact run it changes. An empty find is a
        # pure insertion, which the anchor cannot place (there is nothing to
        # locate) — the model documents this and parsers must refuse it.
        if not find:
            return ParseIssue(index, "no find text to anchor the edit "
                              "(an insertion must be expressed as a replace of "
                              "the surrounding text)", raw)
        anchored = bool(context.strip()) or bool(page) or bool(occ)
        if not anchored and len(normalize(find)) < MIN_ANCHORABLE_FIND:
            return ParseIssue(
                index,
                f"{find!r} is too short to locate on its own — a find this small "
                "matches thousands of places in a book. It needs the line it was "
                "marked on (context), the page it was marked on, or an occurrence "
                "number", raw)

    return {"find": find, "replace": replace, "instruction": instruction,
            "kind": kind, "occurrence": occ, "context": context, "source": source,
            "page": page, "format": fmt, "paragraph": para_op,
            "paragraph_style": para_style}


def _field(entry: dict, canonical: str):
    """The value for a canonical field, trying its aliases in order; None if
    none are present. A present-but-null alias is treated as absent."""
    for name in _ALIASES[canonical]:
        if name in entry and entry[name] is not None:
            return entry[name]
    return None


def _norm_format(raw):
    """The canonical character format for a raw value, "" for absent, or None if it
    names no format this engine can apply."""
    if raw in (None, ""):
        return ""
    if not isinstance(raw, str):
        return None
    f = raw.strip().lower()
    if f in ("", "regular", "roman", "de-italic", "unitalic"):
        return "" if f == "" else "roman"
    return f if f in FORMATS else None


def _norm_kind(raw):
    """The canonical kind for a raw value, or None if it names no known kind.
    Absent (None) defaults to mechanical."""
    if raw is None:
        return MECHANICAL
    if not isinstance(raw, str):
        return None
    k = raw.strip().lower()
    return k if k in KINDS else None


def _norm_occurrence(raw):
    """A non-negative int occurrence, or None if the value is not one. Absent
    defaults to 0 (require a unique match)."""
    if raw is None:
        return 0
    if isinstance(raw, bool):            # bool is an int subclass; not meant here
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, str):
        s = raw.strip()
        if s.isdigit():
            return int(s)
    return None
