"""Everything mechanical about the tagging, decided in Python.

The model answers one question per paragraph — what is this. Which of the two
body styles it becomes, where a scene-break glyph goes, which blank lines
disappear, and every sanity check on an answer that cannot be right are all
decided here, deterministically, from the style sheet's own role table.

Keeping this out of the prompt is what makes prep reproducible: the same
labels always produce the same document.
"""
from __future__ import annotations

import logging

from .model import Flag, ParagraphPlan, PrepPlan, Structure, Tag
from .styles import BODY, SPACING, StyleSheet

log = logging.getLogger("docproof.prep.rules")

# A line the author typed as a scene break is short and mostly punctuation.
# Anything longer is prose that got mislabelled.
_GLYPH_MAX_CHARS = 12


def build_plan(structure: Structure, tags: list[Tag],
               sheet: StyleSheet) -> PrepPlan:
    by_id = {t.para_id: t for t in tags}
    body_first = sheet.role("body_first").name
    body_para = sheet.role("body").name
    scene_break = sheet.role("scene_break").name

    # Optional: a sheet that names no toc style sends contents lines to body,
    # which is wrong-looking but harmless. Being a chapter opener is not.
    toc_style = sheet.roles.get("toc")
    if toc_style and not sheet.has(toc_style):
        log.warning("The style sheet's toc role names %r, which is not one of "
                    "its styles; contents lines will be treated as body.",
                    toc_style)
        toc_style = None

    plans: list[ParagraphPlan] = []
    flags: list[Flag] = []
    # The first body paragraph of a manuscript opens its block: there is no
    # preceding paragraph for it to be a continuation of.
    opens = True
    last_was_break = False
    in_toc = False

    # `taggable` has already set aside what prep must not touch — everything
    # inside a table, and images on their own line — so a plan only ever
    # describes running text, and no writer will visit a preserved paragraph.
    for p in structure.taggable:
        tag = by_id.get(p.para_id) or Tag(p.para_id,
                                          SPACING if p.is_blank else BODY)
        role = tag.role
        if tag.flag:
            flags.append(Flag(p.para_id, "model", tag.flag,
                              _preview(p.text)))

        if p.is_blank:
            plan, opens, last_was_break = _blank(
                p, role, scene_break, plans, flags, opens, last_was_break)
            plans.append(plan)
            continue

        if p.is_toc:
            # The file itself says this line belongs to a contents list, and
            # the file is not guessing. A model reading only the words cannot
            # be right here: an entry reading "Chapter One" is the same eleven
            # characters as the heading it points at, and calling it a heading
            # puts a forced page break under every line of the contents.
            if not in_toc:
                flags.append(Flag(
                    p.para_id, "toc_detected",
                    "The manuscript's own table of contents. These lines were "
                    "styled as contents entries, not as chapter headings. "
                    "InDesign builds its own contents from the styles you "
                    "place, so decide whether to keep them or delete them.",
                    _preview(p.text)))
                in_toc = True
            if toc_style:
                style = toc_style
            else:
                style = body_first if opens else body_para
                opens = False
            plans.append(ParagraphPlan(
                para_id=p.para_id, role=toc_style or BODY, style=style,
                strip_leading=p.leading_ws, strip_trailing=p.trailing_ws))
            last_was_break = False
            continue
        in_toc = False

        role, extra = _sane_role(p, role, scene_break, sheet)
        if extra:
            flags.append(extra)
        if p.is_list:
            # List membership is kept in the file, but no house style describes
            # it, so the designer decides what a list becomes in the template.
            flags.append(Flag(p.para_id, "list_paragraph",
                              "A numbered or bulleted list item. The style set "
                              "has no list style, so its numbering was left as "
                              "it was for you to decide.", _preview(p.text)))

        if role == BODY:
            style = body_first if opens else body_para
            opens = False
        elif role == scene_break:
            style = scene_break                 # the author typed the glyph
            opens = True
        else:
            style = role
            # A style that doesn't open a block — an epigraph, a reference —
            # must not clear a heading's claim on the paragraph after it. Only
            # a body paragraph consumes that.
            opens = opens or sheet.opens_block(role)

        plans.append(ParagraphPlan(
            para_id=p.para_id, role=role, style=style,
            strip_leading=p.leading_ws, strip_trailing=p.trailing_ws))
        last_was_break = style == scene_break

    flags.extend(_trailing_break_flags(structure, plans, scene_break))
    plan = PrepPlan(plans=tuple(plans), flags=tuple(flags),
                    glyph=sheet.scene_break_glyph)
    log.info("Planned %d paragraph(s): %d scene break(s) inserted, %d blank "
             "line(s) removed, %d flag(s) for the designer.",
             len(plan.plans), plan.inserted_breaks, plan.dropped,
             len(plan.flags))
    return plan


def _blank(p, role: str, scene_break: str, plans: list[ParagraphPlan],
           flags: list[Flag], opens: bool,
           last_was_break: bool) -> tuple[ParagraphPlan, bool, bool]:
    """What happens to a blank line. Everything that is not a scene break is
    removed: InDesign takes its vertical spacing from the styles, so an empty
    paragraph is at best noise and at worst a stray line at the top of a page."""
    if role != scene_break:
        return ParagraphPlan(p.para_id, role, drop=True), opens, False

    if last_was_break:
        # Two blank lines at one break is a typing habit, not two breaks.
        return ParagraphPlan(p.para_id, role, drop=True), opens, True

    if opens:
        # A blank line straight after a heading is the gap under the heading.
        # Turning it into a break would put a glyph above a chapter's first
        # paragraph — so it goes, and the call is recorded.
        flags.append(Flag(p.para_id, "break_after_heading",
                          "A blank line right after a heading was read as the "
                          "gap under it, not a scene break."))
        return ParagraphPlan(p.para_id, role, drop=True), opens, False

    return (ParagraphPlan(p.para_id, role, style=scene_break,
                          insert_glyph=True), True, True)


def _sane_role(p, role: str, scene_break: str,
               sheet: StyleSheet) -> tuple[str, Flag | None]:
    """Answers that cannot be right for this paragraph, corrected in the safe
    direction. The schema guarantees the label exists; it cannot know that the
    paragraph it was attached to is a page of prose."""
    if role == SPACING:
        return BODY, Flag(p.para_id, "not_blank",
                          "Labelled a blank line but it has text in it; "
                          "treated as body.", _preview(p.text))

    if role == scene_break:
        text = p.text.strip()
        if len(text) > _GLYPH_MAX_CHARS or any(c.isalnum() for c in text):
            return BODY, Flag(p.para_id, "long_scene_break",
                              "Labelled a scene break but reads as text; "
                              "treated as body.", _preview(p.text))
    if role != BODY and not sheet.has(role):
        return BODY, Flag(p.para_id, "unknown_style",
                          f"Labelled {role!r}, which is not in the style "
                          f"sheet; treated as body.", _preview(p.text))
    return role, None


def _trailing_break_flags(structure: Structure, plans: list[ParagraphPlan],
                          scene_break: str) -> list[Flag]:
    """A scene break with nothing after it separates a scene from nothing."""
    for plan in reversed(plans):
        if plan.drop:
            continue
        if plan.style == scene_break:
            return [Flag(plan.para_id, "trailing_scene_break",
                         "The manuscript ends on a scene break. Check whether "
                         "something is missing after it.")]
        return []
    return []


def _preview(text: str, limit: int = 90) -> str:
    flat = " ".join(text.split())
    return flat[:limit] + ("…" if len(flat) > limit else "")
