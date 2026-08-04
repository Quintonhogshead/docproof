"""The InDesign-ready .docx.

One clean style sheet whose style NAMES are the house template's, applied to
every paragraph, with the export artifacts gone and the author's words
untouched. This is the file a designer places.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ...utils.xml_helpers import qn
from ..ingest import BODY_PART
from ..model import PrepPlan, Structure
from ..styles import StyleSheet, install_style_sheet
from .common import (clean_content_controls, element_map, glyph_paragraph,
                     leading_tab_count, remove_leading_tabs,
                     set_paragraph_style, strip_direct_paragraph_formatting,
                     strip_direct_run_formatting, style_links, trim_text_edges)

log = logging.getLogger("docproof.prep.writers.clean")


@dataclass(frozen=True)
class WriteStats:
    styled: int = 0
    dropped: int = 0
    inserted: int = 0
    trimmed: int = 0
    links: int = 0
    controls: int = 0
    stripped: int = 0
    missing: tuple[str, ...] = ()


def write_clean(pkg, structure: Structure, plan: PrepPlan, sheet: StyleSheet, *,
                strip_formatting: bool = True) -> WriteStats:
    elements = element_map(pkg)
    pkg.mark_modified(BODY_PART)
    link_style = sheet.link_style
    styled = dropped = inserted = trimmed = links = stripped = 0
    missing: list[str] = []

    for item in plan.plans:
        p = elements.get(item.para_id)
        if p is None:
            # Ingest and writing walk the same tree in the same order, so this
            # cannot happen — which is exactly why it is reported rather than
            # assumed away.
            log.error("%s vanished between reading and writing.", item.para_id)
            missing.append(item.para_id)
            continue

        if item.drop:
            p.getparent().remove(p)
            dropped += 1
            continue

        if item.insert_glyph:
            _become_glyph(p, sheet.by_name(item.style).id, plan.glyph)
            inserted += 1
            styled += 1
            continue

        tabs = leading_tab_count(p)
        if tabs or item.strip_leading or item.strip_trailing:
            remove_leading_tabs(p, tabs)
            trim_text_edges(p, item.strip_leading, item.strip_trailing)
            trimmed += 1

        if strip_formatting:
            if strip_direct_paragraph_formatting(p) | strip_direct_run_formatting(p):
                stripped += 1

        if item.style:
            set_paragraph_style(p, sheet.by_name(item.style).id)
            styled += 1

        if link_style is not None:
            links += style_links(p, link_style.id)

    controls = clean_content_controls(pkg)
    install_style_sheet(pkg, sheet, replace=True)

    log.info("Clean file: %d paragraph(s) styled, %d blank line(s) removed, "
             "%d scene break(s) written, %d link(s) tagged.",
             styled, dropped, inserted, links)
    return WriteStats(styled=styled, dropped=dropped, inserted=inserted,
                      trimmed=trimmed, links=links, controls=controls,
                      stripped=stripped, missing=tuple(missing))


def _become_glyph(p, style_id: str, glyph: str) -> None:
    """Turn the author's blank line into the house scene-break paragraph.

    Rebuilding the existing element rather than inserting a new one keeps the
    break exactly where the author put it, with no index arithmetic to get
    wrong."""
    replacement = glyph_paragraph(style_id, glyph)
    keep = {k: v for k, v in p.attrib.items() if k != qn("w:rsidR")}
    p.clear()
    for key, value in keep.items():
        p.set(key, value)
    for child in replacement:
        p.append(child)
