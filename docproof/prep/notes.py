"""Prep Notes: what was tagged, what was cleaned up, and what a human still has
to decide.

The house prompt asks for this write-up by name, and it is the part a designer
actually reads: the mapping with counts, how scene breaks were handled, what was
preserved, the word-for-word result, and the flags. `prep.json` is the same
thing for the app.
"""
from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..providers import estimate_cost
from .model import PrepPlan, Structure, Tag
from .styles import StyleSheet

log = logging.getLogger("docproof.prep.notes")

FLAG_TITLES = {
    "model": "Judgment calls",
    "not_blank": "Labelled a blank line but has text",
    "long_scene_break": "Labelled a scene break but reads as text",
    "unknown_style": "Labelled with a style the sheet does not have",
    "break_after_heading": "Blank line under a heading",
    "trailing_scene_break": "Ends on a scene break",
    "list_paragraph": "Lists, which the style set does not cover",
}

KIND_TITLES = {"indesign": "InDesign-ready Word file",
               "tracked": "Tracked-changes Word file"}


def write_notes(out_dir: Path, *, structure: Structure, plan: PrepPlan,
                sheet: StyleSheet, stats: dict, checks: list, usage,
                cfg, written: dict, failures: list[str],
                source_path: str | Path, prompt, tags: list[Tag]
                ) -> tuple[Path, Path]:
    payload = _payload(structure=structure, plan=plan, sheet=sheet,
                       stats=stats, checks=checks, usage=usage, cfg=cfg,
                       written=written, failures=failures,
                       source_path=source_path, prompt=prompt, tags=tags)
    json_path = out_dir / "prep.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    md_path = out_dir / "prep_notes.md"
    md_path.write_text(_markdown(payload), encoding="utf-8")
    log.info("Wrote %s and %s", md_path, json_path)
    return md_path, json_path


def _payload(*, structure, plan, sheet, stats, checks, usage, cfg, written,
             failures, source_path, prompt, tags) -> dict:
    unanswered = [t.para_id for t in tags if t.source == "unanswered"]
    author_breaks = sum(1 for p in plan.plans
                        if p.style == sheet.role("scene_break").name
                        and not p.insert_glyph)
    # Priced at the effort the run actually used, not the baseline: the effort
    # dial scales only output tokens (see catalog.EFFORT_MULTIPLIER), and a
    # high-effort run whose estimate ignored it reads too low. Still an estimate
    # — list rates on real token counts — but no longer an effort-blind one.
    cost = estimate_cost(cfg.api.model,
                         input_tokens=usage.input_tokens
                         + usage.cache_creation_input_tokens,
                         output_tokens=usage.output_tokens,
                         effort=cfg.api.effort)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source_path),
        "source_name": Path(source_path).name,
        "style_sheet": {"name": sheet.name, "version": sheet.version,
                        "trim": sheet.trim, "path": sheet.path,
                        "styles": [s.name for s in sheet.styles]},
        "tagging_prompt": {"name": prompt.name, "version": prompt.version,
                           "path": prompt.path},
        "config": {"model": cfg.api.model, "glyph": plan.glyph,
                   "strip_direct_formatting": cfg.prep.strip_direct_formatting,
                   "revision_author": cfg.revision_author},
        "counts": {
            "paragraphs": len(structure.paragraphs),
            "blank_lines": sum(1 for p in structure.paragraphs if p.is_blank),
            "words": structure.word_count,
            "tagged": len([p for p in plan.plans if p.style]),
            "scene_breaks_inserted": plan.inserted_breaks,
            "scene_breaks_from_author": author_breaks,
            "blank_lines_removed": plan.dropped,
            "paragraphs_trimmed": plan.trimmed,
            "italic_paragraphs": sum(1 for p in structure.paragraphs
                                     if p.has_italics),
            "link_paragraphs": sum(1 for p in structure.paragraphs
                                   if p.has_link),
            "unanswered": len(unanswered),
            "untouched_outside_body": len(structure.untouched),
        },
        "styles": plan.style_counts,
        "written": {kind: str(path) for kind, path in written.items()},
        "write_stats": {kind: dataclasses.asdict(s) for kind, s in stats.items()},
        "verification": [dataclasses.asdict(c) for c in checks],
        "verified": bool(checks) and all(c.ok for c in checks),
        "failures": failures,
        "flags": [dataclasses.asdict(f) for f in plan.flags],
        "unanswered_paragraphs": unanswered,
        "usage": dataclasses.asdict(usage),
        "cost": cost,
    }


def _markdown(d: dict) -> str:
    counts = d["counts"]
    L: list[str] = [f"# Prep notes — {d['source_name']}\n"]
    L.append(f"*{d['generated_at'][:16].replace('T', ' ')} UTC · model "
             f"`{d['config']['model']}` · style sheet "
             f"**{d['style_sheet']['name']}** v{d['style_sheet']['version']}"
             + (f" · trim {d['style_sheet']['trim']}"
                if d["style_sheet"]["trim"] else "") + "*\n")

    if d["failures"]:
        L.append("## Nothing was shipped\n")
        for failure in d["failures"]:
            L.append(f"- {failure}")
        L.append("\nThe author's text and the output did not match, so the "
                 "file was deleted rather than handed on. Everything below "
                 "describes what prep intended to do.\n")

    L.append(f"**{counts['tagged']:,} paragraph(s) tagged** across "
             f"{counts['words']:,} words. "
             f"{counts['scene_breaks_inserted']} scene break(s) written from "
             f"blank lines, {counts['scene_breaks_from_author']} the author "
             f"had typed. {counts['blank_lines_removed']} blank line(s) "
             f"removed.\n")

    if d["written"]:
        L.append("## Files\n")
        for kind, path in d["written"].items():
            L.append(f"- **{KIND_TITLES.get(kind, kind)}** — `{Path(path).name}`")
        L.append("")

    L.append("## Style mapping\n")
    L.append("| InDesign paragraph style | Paragraphs |")
    L.append("| --- | ---: |")
    for name, count in d["styles"].items():
        L.append(f"| {name} | {count} |")
    L.append("")
    unused = [s for s in d["style_sheet"]["styles"] if s not in d["styles"]]
    if unused:
        L.append(f"Not used in this manuscript: {', '.join(unused)}.\n")

    L.append("## Cleanup\n")
    L.append(f"- {counts['blank_lines_removed']} blank paragraph(s) removed — "
             f"InDesign takes vertical spacing from the styles.")
    L.append(f"- {counts['paragraphs_trimmed']} paragraph(s) had typed leading "
             f"or trailing whitespace stripped. First-line indents were not "
             f"touched: those come from the style.")
    # Per file, not summed: each writer works on its own copy of the document,
    # so adding the two up would report the same artifact twice.
    controls = max([s.get("controls", 0)
                    for s in d["write_stats"].values()] or [0])
    L.append(f"- {controls} content control(s) / export artifact(s) removed.")
    links = max([s.get("links", 0) for s in d["write_stats"].values()] or [0])
    L.append(f"- {links} link(s) tagged with the hyperlink character style.")
    if d["config"]["strip_direct_formatting"]:
        L.append("- Direct paragraph formatting was cleared in the "
                 "InDesign-ready file; italics, bold, small caps and case "
                 "marks were kept. The tracked file keeps the author's "
                 "formatting as it was.")
    L.append("")

    L.append("## What was preserved\n")
    L.append(f"- Every word and punctuation character of the author's text.")
    L.append(f"- Inline italics, exactly where they were: "
             f"{counts['italic_paragraphs']} paragraph(s) carry them.")
    L.append(f"- {counts['link_paragraphs']} paragraph(s) contain a link.")
    if counts["untouched_outside_body"]:
        L.append(f"- {counts['untouched_outside_body']} paragraph(s) in "
                 f"headers, footers or notes were left exactly as they were; "
                 f"the template supplies its own running heads.")
    L.append("")

    L.append("## Word-for-word check\n")
    if d["verification"]:
        for check in d["verification"]:
            mark = "✓" if check["ok"] else "✗"
            L.append(f"- {mark} {_check_line(check)}")
    else:
        L.append("- Not run (verification is switched off in the config).")
    L.append("")

    flags = d["flags"]
    L.append(f"## For the designer — {len(flags)} flag(s)\n")
    if not flags:
        L.append("Nothing ambiguous came up.\n")
    else:
        L.append("These are raised, not fixed. Each one is a judgment a human "
                 "should make.\n")
        by_kind: dict[str, list[dict]] = {}
        for flag in flags:
            by_kind.setdefault(flag["kind"], []).append(flag)
        for kind, items in by_kind.items():
            L.append(f"### {FLAG_TITLES.get(kind, kind)} — {len(items)}\n")
            for flag in items:
                where = f"`{flag['para_id']}`"
                preview = f" — “{flag['preview']}”" if flag.get("preview") else ""
                L.append(f"- {where} {flag['message']}{preview}")
            L.append("")

    if d["unanswered_paragraphs"]:
        L.append("## Not labelled\n")
        L.append(f"{len(d['unanswered_paragraphs'])} paragraph(s) came back "
                 f"without a label and were treated as running text: "
                 f"{', '.join(d['unanswered_paragraphs'][:20])}"
                 f"{'…' if len(d['unanswered_paragraphs']) > 20 else ''}.\n")

    usage = d["usage"]
    L.append("## Usage\n")
    L.append(f"API calls: {usage['api_calls']} · input tokens: "
             f"{usage['input_tokens']:,} · output tokens: "
             f"{usage['output_tokens']:,}")
    if d["cost"] is not None:
        L.append(f" · estimated cost: **${d['cost']:.4f}**")
    L.append("")

    L.append("---\n")
    L.append("Place the tagged .docx in InDesign — the paragraph style names "
             "in it are the template's own, so they map on Place. The "
             "tracked-changes file is for reading the same decisions in Word "
             "with Accept and Reject.\n")
    return "\n".join(L)


def _check_line(check: dict) -> str:
    views = {"clean": "the tagged file",
             "accept": "the tracked file with every change accepted",
             "reject": "the tracked file with every change rejected"}
    where = views.get(check["view"], check["view"])
    if check["ok"]:
        return (f"{where}: {check['output_words']:,} words, identical to the "
                f"manuscript.")
    return f"{where}: {check['detail']}"
