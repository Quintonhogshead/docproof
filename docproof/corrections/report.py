"""The corrections run written up: what was applied, what a human still has to
look at, and — the part Nick asked for — every change in the file that no
correction accounts for.

`corrections.json` is the machine copy for the app; `corrections_notes.md` is the
one a person reads. Both are produced from the same three reports (parse, apply,
verify), so they never disagree.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from .model import (APPLIED_EXACTLY, ApplyReport, CommentDisposition,
                    DEVIATES, DISP_APPLIED, DISP_FLAGGED, DISP_NO_OP,
                    DISP_NOT_EXTRACTED, MISSING, VerifyReport)
from .parse import ParseResult

log = logging.getLogger("docproof.corrections.report")

# The mark a model pass appends to an edit's note when it made an editorial call the
# reviewer had left open — a settled either/or, a merged collision, a resolved query.
# A re-anchor is only a relocation, not a decision, so it is not gathered here.
_ANSWERED = re.compile(r"— (?:second look: (?:settled|merged)|resolved on the book):")

# The apply statuses that mean a human has to look, mapped to a plain phrase.
FLAG_TITLES = {
    "not_found": "The text to change was not found",
    "ambiguous": "The text appears more than once",
    "crosses_paragraph": "The change would span a paragraph break",
    "routed_to_design": "A layout request, not a text edit",
    "overlaps": "Two corrections land on the same words",
    "withheld": "Held back for a human",
    "unstyleable": "The formatting could not be applied here",
    "off_page": "The text is not on the page it was marked on",
}

VERIFY_TITLES = {
    APPLIED_EXACTLY: "applied exactly",
    DEVIATES: "differs from what was asked",
    MISSING: "not in the document",
}

# The reviewer-comment dispositions, mapped to a plain phrase for the report.
DISP_TITLES = {
    DISP_APPLIED: "applied",
    DISP_FLAGGED: "flagged for a human",
    DISP_NO_OP: "no change needed",
    DISP_NOT_EXTRACTED: "not turned into an edit",
}


def write_report(out_dir: Path, *, source_path, after_path, parse: ParseResult,
                 apply: ApplyReport | None, verify: VerifyReport,
                 comments: tuple[CommentDisposition, ...] = (),
                 deterministic: bool = True,
                 pages: tuple[int, int] = (0, 0), pages_cited: int = 0,
                 checks: tuple = (), merged_away: int = 0,
                 page_labels: dict[int, str] | None = None,
                 queue: list[dict] | None = None) -> tuple[Path, Path]:
    """Write `corrections.json` and `corrections_notes.md` into `out_dir`, and
    return their paths. `deterministic` is False when an opt-in model pass ran
    (the sanity gate, or the second look over the extractor's queries), so the
    report does not over-claim being model-free. `pages` is
    `(placed, total)` from the page map, reported so a run whose page narrowing
    silently did not happen says so. `pages_cited` is how many corrections cite
    a proof page — with a zero page total it means the marks lost their page
    narrowing entirely, which the report says in so many words rather than
    leaving a flood of ambiguous flags to imply it. `page_labels` maps a proof
    page to the
    InDesign folio it should be shown as (see `idml.page_label_map`), so every
    page a reviewer or designer reads here is the page InDesign shows. `queue` is
    the review screen's list of unresolved flags with their clickable resolutions
    (see `resolve`)."""
    payload = _payload(source_path=source_path, after_path=after_path,
                       parse=parse, apply=apply, verify=verify, comments=comments,
                       deterministic=deterministic, pages=pages,
                       pages_cited=pages_cited, checks=checks,
                       merged_away=merged_away, page_labels=page_labels or {},
                       queue=queue)
    json_path = out_dir / "corrections.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    md_path = out_dir / "corrections_notes.md"
    md_path.write_text(_markdown(payload), encoding="utf-8")
    log.info("Wrote %s and %s", md_path, json_path)
    return md_path, json_path


def _payload(*, source_path, after_path, parse, apply, verify, comments=(),
             deterministic=True, pages=(0, 0), pages_cited=0, checks=(),
             merged_away=0, page_labels=None, queue=None) -> dict:
    needs_human = [c for c in comments if c.needs_human]
    placed, total = pages
    page_labels = page_labels or {}
    return {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source_path),
        "source_name": Path(source_path).name,
        "after": (str(after_path) if after_path is not None else None),
        "mode": "apply" if apply is not None else "verify",
        # Applying corrections is deterministic — no model call, no cost. Said
        # plainly because it is a selling point of the engine, not an omission.
        # False only when the opt-in sanity gate ran, so the report stays honest.
        "deterministic": deterministic,
        "parse": {
            "edits": len(parse.edits),
            "issues": [dataclasses.asdict(i) for i in parse.issues],
        },
        "apply": (None if apply is None else {
            "applied": apply.applied,
            "flagged": [_outcome(o) for o in apply.flagged],
            "no_op": [_outcome(o) for o in apply.outcomes
                      if not o.applied and not o.needs_human],
            # Edits that several-into-one merges removed from the list. Reported so
            # the id count reconciles: parsed + emptied-line removals = applied +
            # flagged + no-op + merged-away. The comments behind them are accounted
            # for on the edit they merged into (its `source` carries theirs).
            "merged_away": merged_away,
            # Synthetic outcomes the apply added on its own: a paragraph a deletion
            # left empty is removed as its own change, so it is an extra applied
            # outcome with no parsed edit behind it. Counted so the equation above
            # accounts for it rather than reading it as an id that appeared from
            # nowhere. Identified by the `+…-para` id `_remove_emptied` gives them.
            "para_removals": sum(1 for o in apply.outcomes
                                 if o.edit.id.endswith("-para")),
            # The other synthetic outcome: a settled compound note ("remove the
            # quotes and italicize it") that the second look split into a text edit
            # and a companion format edit, the latter under a `…-fmt` id. It too is
            # an extra outcome with no parsed id behind it, so the equation counts
            # it. The reviewer comment it came from is accounted for on the text
            # edit; this is only the styling that rode along.
            "fmt_companions": sum(1 for o in apply.outcomes
                                  if o.edit.id.endswith("-fmt")),
            "stories_changed": list(apply.stories_changed),
        }),
        "verify": {
            "clean": verify.clean,
            "structure_changed": verify.structure_changed,
            "paragraphs_before": verify.paragraphs_before,
            "paragraphs_after": verify.paragraphs_after,
            "paragraphs_expected": verify.paragraphs_expected,
            "structure_intended": verify.structure_intended,
            "reconciliations": [
                {"id": r.edit.id, "status": r.status,
                 "find": r.edit.find, "replace": r.edit.replace,
                 "detail": r.detail}
                for r in verify.reconciliations],
            "discrepancies": [dataclasses.asdict(d) for d in verify.discrepancies],
        },
        # Every paragraph an applied edit changed, before and after — the list a
        # designer reads down to confirm each correction is right, not merely that
        # it landed. The pair is the machine truth; the display diffs it into a
        # redline. The verify discrepancy list is the complement: this is what did
        # change, that is proof nothing else did.
        "changes": [
            {"story_id": c.story_id, "paragraph": c.paragraph,
             "before": c.before, "after": c.after,
             "edit_ids": list(c.edit_ids), "instruction": c.instruction,
             "formatting": c.formatting, "layout": c.layout}
            for c in verify.changes],
        # One row per reviewer comment — the ledger that makes sure none is lost.
        # `total` and `unresolved` are the honest headline: how many marks came in,
        # and how many a person still owns (flagged, or never turned into an edit).
        "comments": {
            "total": len(comments),
            "unresolved": len(needs_human),
            "items": [_comment(c, page_labels) for c in comments],
        },
        # How much of the proof the page map placed in the book. A page it could
        # not place is one whose marks had the whole book to land in, which is the
        # difference between an edit that applies and one that is flagged — so it
        # is reported rather than left to be inferred from a flag count.
        "pages": {"placed": placed, "total": total,
                  # How many proof pages could be shown as the file's own folio.
                  # 0 with a nonzero total means every reported page is the
                  # proof's physical page — worth the report saying out loud.
                  "labeled": len(page_labels),
                  # How many corrections cite a proof page. Nonzero with a zero
                  # total means the marks lost their page narrowing entirely —
                  # the proof's pages never reached the run — and the report
                  # warns in so many words.
                  "cited": pages_cited},
        # The designer's list: what needs InDesign open, located. Composition is the
        # one thing this engine cannot do, so it is the one thing it does not claim.
        "checks": [_with_label(dataclasses.asdict(c), page_labels) for c in checks],
        # The review screen's queue: every flag a person still owns, each with the
        # concrete places its change could land, ready to click-apply — and, once
        # someone does, the record of what they chose (see `resolve`).
        "queue": list(queue or []),
    }


def _page_where(row: dict) -> str:
    """`page N` for a report row, using the InDesign folio when the row carries one
    and falling back to the physical proof page otherwise. `—` when neither is
    known (a typed list has no page)."""
    label = row.get("page_label")
    if label:
        return f"page {label}"
    return f"page {row['page']}" if row.get("page") else "—"


def _with_label(row: dict, page_labels: dict[int, str]) -> dict:
    """A row carrying a `page` gains the InDesign folio it should be shown as, so
    every page a designer reads points at the page InDesign shows. Absent when the
    page could not be aligned to a folio, and the physical page stands."""
    label = page_labels.get(row.get("page") or 0)
    if label is not None:
        row["page_label"] = label
    return row


def _outcome(o) -> dict:
    row = {"id": o.edit.id, "status": o.status, "find": o.edit.find,
           "replace": o.edit.replace, "instruction": o.edit.instruction,
           "story_id": o.story_id, "paragraph": o.paragraph,
           "occurrences": o.occurrences, "detail": o.detail}
    # A formatting edit leaves the text alone, so find and replace read as
    # identical; saying which formatting it applied is what makes the row legible.
    if o.edit.format:
        row["format"] = o.edit.format
    return row


def _comment(c: CommentDisposition, page_labels: dict[int, str]) -> dict:
    return _with_label(
        {"id": c.id, "page": c.page, "kind": c.kind,
         "instruction": c.instruction, "anchor": c.anchor,
         "disposition": c.disposition, "edit_ids": list(c.edit_ids),
         "detail": c.detail},
        page_labels)


def _markdown(d: dict) -> str:
    L: list[str] = [f"# Corrections — {d['source_name']}\n"]
    when = d["generated_at"][:16].replace("T", " ")
    mode = ("applied to the InDesign file" if d["mode"] == "apply"
            else "checked against a file corrected elsewhere")
    how = ("deterministic, no model calls" if d.get("deterministic", True)
           else "deterministic apply, with an opt-in model pass over the edits")
    L.append(f"*{when} UTC · {mode} · {how}*\n")

    verify = d["verify"]
    ap = d["apply"]
    com = d.get("comments") or {"total": 0, "unresolved": 0, "items": []}
    # A flagged edit read from a proof is the same problem as its reviewer comment,
    # so when comments are present the two would double-count. Fold the edits that a
    # needs-human comment already covers into that ledger, and surface only the
    # remainder (a hand-added or typed edit) as its own list.
    covered: set[str] = set()
    for c in com["items"]:
        if c["disposition"] in (DISP_FLAGGED, DISP_NOT_EXTRACTED):
            covered.update(c.get("edit_ids") or [])
    # A flag the designer resolved from the review screen is done — it leaves
    # the needs-a-human lists and counts with the applied, and the resolutions
    # get a section of their own below. A flag they deliberately set aside
    # ("ignore this") leaves the lists too, into its own bucket: neither
    # applied nor awaiting anyone, and said so rather than hidden.
    ap_flagged = ([o for o in ap["flagged"]
                   if not o.get("resolved") and not o.get("dismissed")]
                  if ap is not None else [])
    ap_dismissed = ([o for o in ap["flagged"] if o.get("dismissed")]
                    if ap is not None else [])
    flagged_uncovered = ([o for o in ap_flagged if o["id"] not in covered]
                         if com["total"] else ap_flagged)
    all_res = d.get("resolutions") or []
    resolutions = [r for r in all_res if r.get("kind") != "dismissed"]
    set_aside = [r for r in all_res if r.get("kind") == "dismissed"]
    if (verify["clean"] and (ap is None or not ap_flagged)
            and not com["unresolved"]):
        L.append("**Clean.** Every correction landed exactly, nothing else in the "
                 "file changed, and every reviewer comment was accounted for.\n")
    else:
        headline = []
        # Count in one unit, so the buckets sum to a total the reader can check.
        # A proof's unit is its comments — what the reviewer actually marked, and
        # what `com['total']` already names — so applied / no-change / need-a-human
        # are counted per comment, never per edit. The edit and line counts are the
        # mechanism underneath; they ride along as a parenthetical below, never as a
        # fourth addend that would break the sum (an edit count next to a comment
        # count is exactly what stopped the old headline adding up). A typed list has
        # no comments, so there the unit is the edit and the same three buckets are
        # read off the apply outcomes instead.
        if com["total"]:
            items = com.get("items") or []
            # A resolved comment counts with the applied — its change is in the
            # file now — so the buckets still sum to the reviewer's total.
            applied_c = sum(1 for c in items
                            if c["disposition"] == DISP_APPLIED
                            or c.get("resolved"))
            nochange_c = sum(1 for c in items if c["disposition"] == DISP_NO_OP)
            aside_c = sum(1 for c in items if c.get("dismissed"))
            headline.append(f"{com['total']} reviewer comment(s)")
            headline.append(f"{applied_c} applied")
            if nochange_c:
                headline.append(f"{nochange_c} no change needed")
            if aside_c:
                headline.append(f"{aside_c} set aside in review")
            if com["unresolved"]:
                headline.append(f"{com['unresolved']} need a human")
        elif ap is not None:
            headline.append(f"{ap['applied']} applied")
            if ap["no_op"]:
                headline.append(f"{len(ap['no_op'])} no change needed")
            if ap_dismissed:
                headline.append(f"{len(ap_dismissed)} set aside in review")
            if flagged_uncovered:
                headline.append(f"{len(flagged_uncovered)} need a human")
        if verify["discrepancies"]:
            headline.append(f"{len(verify['discrepancies'])} unaccounted change(s)")
        if verify["structure_changed"]:
            headline.append("paragraph count changed")
        L.append("**" + ", ".join(headline) + ".** See below.\n")
        # The edit/line mechanism behind a comment headline, said once so the
        # comment buckets above read as a clean sum. Only when a proof drove the run
        # and edits actually landed: a typed list is already counted in edits above,
        # so repeating it here would just say the same number twice.
        if com["total"] and ap is not None and ap["applied"]:
            L.append(f"*{ap['applied']} edit(s) applied, across "
                     f"{len(d.get('changes') or [])} line(s) — the comment counts "
                     f"above are the reviewer's marks, one to a comment.*\n")

    pages = d.get("pages") or {"placed": 0, "total": 0}
    if not pages["total"] and pages.get("cited"):
        # The run had page-citing corrections and no proof pages at all — the
        # sidecar a marked-PDF read attaches was lost (a reload discards it),
        # or a typed list carried a page column. Every mark then has the whole
        # book to match against, so a repeated find can only be flagged. Said
        # first and plainly, because the alternative is a designer reading a
        # flood of ambiguous flags one by one.
        L.append(f"**No proof pages accompanied this run.** "
                 f"{pages['cited']} correction(s) cite a proof page, but the "
                 f"proof's page texts were not attached, so every mark had the "
                 f"whole book to match against — repeated text could only be "
                 f"flagged, and page numbers here are the list's own. "
                 f"Re-reading the marked proof attaches its pages.\n")
    if pages["total"]:
        if pages["placed"] == pages["total"]:
            L.append(f"Every one of the {pages['total']} proof pages was located "
                     f"in the book, so each mark was matched against the text its "
                     f"own page set.\n")
        else:
            missed = pages["total"] - pages["placed"]
            L.append(f"**{pages['placed']} of {pages['total']} proof pages** were "
                     f"located in the book. Marks on the other {missed} had the "
                     f"whole book to match against, so a repeated word or a bare "
                     f"comma among them will have been flagged rather than "
                     f"applied.\n")
        # Which page numbers this report speaks in. Labels come from the file's
        # own spreads, or from the folios the proof itself prints; when neither
        # aligned, saying so beats a designer hunting for a page that InDesign
        # numbers differently.
        if not pages.get("labeled"):
            L.append("*Page numbers here are the proof PDF's physical pages — "
                     "the file's own folios could not be aligned to the proof "
                     "for this run.*\n")

    issues = d["parse"]["issues"]
    if issues:
        L.append(f"## Corrections that could not be read — {len(issues)}\n")
        L.append("These entries in the corrections list were skipped; nothing "
                 "was guessed.\n")
        for i in issues:
            L.append(f"- entry {i['index'] + 1}: {i['reason']}")
        L.append("")

    # The reviewer comments a person still owns, with the reviewer's own words and
    # the page — so an editor can act on each straight from here, whether it was
    # flagged during apply or never turned into an edit at all. This is the section
    # that keeps a comment from being swallowed.
    unresolved = [c for c in com["items"]
                  if c["disposition"] in (DISP_FLAGGED, DISP_NOT_EXTRACTED)
                  and not c.get("resolved") and not c.get("dismissed")]
    if unresolved:
        L.append(f"## Reviewer comments needing a human — {len(unresolved)}\n")
        L.append("Every one carries the reviewer's original note and the page it "
                 "was marked on, so it can be handled by hand.\n")
        for c in unresolved:
            where = _page_where(c)
            why = DISP_TITLES.get(c["disposition"], c["disposition"])
            note = c["instruction"] or "(no note)"
            L.append(f"- **{where}** ({why}): “{_preview(note)}”"
                     + (f" — on “{_preview(c['anchor'])}”" if c["anchor"] else "")
                     + (f" — {c['detail']}" if c["detail"] else ""))
        L.append("")

    # Period/capitalization queries the last tier read and confirmed correct as set
    # — a deliberate open line, an intended lowercase. Each is off the human's list
    # by a decision, not by omission, so the reasoning is shown for a person to
    # audit at a glance rather than left to be taken on trust.
    confirmed = [c for c in com["items"]
                 if c["disposition"] == DISP_NO_OP and c.get("detail")]
    if confirmed:
        L.append(f"## Confirmed as set — {len(confirmed)}\n")
        L.append("A model read each against the passage around it and found the "
                 "line correct as set, so no change was made. The reasoning is "
                 "given so the call can be checked.\n")
        for c in confirmed:
            where = _page_where(c)
            note = c["instruction"] or "(no note)"
            L.append(f"- **{where}**: “{_preview(note)}”"
                     + (f" — on “{_preview(c['anchor'])}”" if c["anchor"] else "")
                     + f" — {c['detail']}")
        L.append("")

    # What the designer settled from the review screen — a clicked placement, or
    # a typed answer a model transcribed. Each is in the file now; each is
    # listed so the log says who decided it. The queue item behind each carries
    # the page, so the log names it in the finished file's own folio — the same
    # number every other section speaks in.
    by_item = {q.get("id"): q for q in d.get("queue") or []}
    if resolutions:
        L.append(f"## Resolved in review — {len(resolutions)}\n")
        L.append("These flags were resolved on the review screen; the changes "
                 "are in the corrected file.\n")
        for r in resolutions:
            how = ("edited the line by hand" if r.get("kind") == "manual"
                   else "applied the model's suggestion"
                   if r.get("kind") == "suggestion"
                   else ("accepted the agent's change"
                         + (f" — {_preview(r['why'])}" if r.get("why") else ""))
                   if r.get("kind") == "agent"
                   else f"talked it through, then: “{_preview(r['text'])}”"
                   if r.get("kind") == "chat"
                   else f"typed: “{_preview(r['text'])}”" if r.get("text")
                   else "picked a placement")
            q = by_item.get(r.get("item_id")) or {}
            where = _page_where(q)
            if where != "—":
                how = f"**{where}** — {how}"
            L.append(f"- {how}:")
            if r.get("before"):
                L.append(f"  - was: “{_preview(r['before'], 300)}”")
            L.append(f"  - now: “{_preview(r.get('after') or '(line removed)', 300)}”")
            breaks = _break_notes(r.get("breaks") or {})
            if breaks:
                L.append(f"  - and {breaks}")
        L.append("")

    # What the run-level agent raised while working the designer's requests,
    # grouped by what a person actually has to do with each. Its accepted edits
    # are already in "Resolved in review" above (kind "agent"); this is the half
    # that stayed a person's — the layout work it cannot do in the text (tasks),
    # the parts blocked pending something (holds), and plain notes. A big mixed
    # request comes back as three legible lists rather than one flat pile.
    agent = d.get("agent") or {}
    agent_flags = agent.get("flags") or []
    if agent_flags:
        def _of(cat):
            return [f for f in agent_flags if (f.get("category") or "note")
                    == cat]

        def _line(f):
            note = _preview(f.get("note") or f.get("why") or "(no note)", 300)
            quote = _preview(f.get("quote") or "")
            return f"- {note}" + (f" — on “{quote}”" if quote else "")

        tasks, holds, notes = _of("task"), _of("hold"), _of("note")
        if tasks:
            L.append(f"## To do in InDesign — {len(tasks)}\n")
            L.append("Layout work the agent cannot do in the text — page order, "
                     "a cover, a placement that matches a design. Each is a step "
                     "to carry out by hand.\n")
            L.extend(_line(f) for f in tasks)
            L.append("")
        if holds:
            L.append(f"## Waiting on — {len(holds)}\n")
            L.append("Blocked pending something you have to supply or decide "
                     "before this part can be finished.\n")
            L.extend(_line(f) for f in holds)
            L.append("")
        if notes:
            L.append(f"## The agent flagged for you — {len(notes)}\n")
            L.append("Raised for a person rather than changed on its own.\n")
            L.extend(_line(f) for f in notes)
            L.append("")

    # The flags the designer deliberately left alone. Not applied and not
    # awaiting anyone — a decision, recorded so the printable log shows who
    # made it and what was left as set.
    if set_aside:
        L.append(f"## Set aside in review — {len(set_aside)}\n")
        L.append("The designer chose to leave each of these as set (or to "
                 "handle it in InDesign). Nothing was changed.\n")
        for r in set_aside:
            q = by_item.get(r.get("item_id")) or {}
            where = _page_where(q)
            note = q.get("instruction") or "(no note)"
            L.append(f"- **{where}**: “{_preview(note)}”"
                     + (f" — on “{_preview(q.get('anchor') or '')}”"
                        if q.get("anchor") else ""))
        L.append("")

    if ap is not None:
        L.append(f"## Applied — {ap['applied']} edit(s)\n")
        if flagged_uncovered:
            title = ("Other edits needing a human" if com["total"]
                     else "For a human")
            L.append(f"## {title} — {len(flagged_uncovered)}\n")
            L.append("Each of these was refused rather than guessed at.\n")
            by_status: dict[str, list[dict]] = {}
            for o in flagged_uncovered:
                by_status.setdefault(o["status"], []).append(o)
            for status, items in by_status.items():
                L.append(f"### {FLAG_TITLES.get(status, status)} — {len(items)}\n")
                for o in items:
                    L.append(f"- `{o['id']}` {_change(o)}"
                             + (f" — {o['detail']}" if o["detail"] else ""))
                L.append("")
        no_op = ap["no_op"]
        if no_op:
            L.append(f"## No change needed — {len(no_op)}\n")
            for o in no_op:
                L.append(f"- `{o['id']}` {_change(o)}")
            L.append("")

    # The edits a model decided for the reviewer — a delegated either/or it settled,
    # a colliding pair it merged, a query the last tier resolved on the book's own
    # evidence. Each applied like any other, but each was a call the reviewer left
    # open, so they are gathered here as the short confirm list: read these, the rest
    # are mechanical. Told from the mechanical edits by the note the pass appended.
    answered = [c for c in (d.get("changes") or [])
                if _ANSWERED.search(c.get("instruction") or "")]
    if answered:
        L.append(f"## Answered for you — {len(answered)}\n")
        L.append("A model made these calls where the reviewer left the choice open. "
                 "Each was applied; each is worth a glance to confirm the call.\n")
        for c in answered:
            where = (f"story `{c['story_id']}`"
                     + (f", ¶ {c['paragraph']}" if c.get("paragraph", -1) >= 0
                        else ""))
            L.append(f"- {where}:")
            L.append(f"  - now: “{_preview(c['after'], 300)}”"
                     + (f" — {_preview(c['instruction'], 200)}"
                        if c.get("instruction") else ""))
        L.append("")

    # The applied changes, each in the line it changed, for a person to read down
    # and confirm — the designer's quick check that the corrections are right, not
    # just that they anchored. The verification section below is the complement.
    # Split by where the designer acts: a text or formatting edit is done in the
    # file and only needs confirming ("change here"); a paragraph layout op the
    # engine applied reflows the page, which InDesign settles, so it is called out
    # separately as one to finish and check there.
    changes = d.get("changes") or []

    def _change_line(c):
        where = (f"story `{c['story_id']}`"
                 + (f", ¶ {c['paragraph']}" if c.get("paragraph", -1) >= 0
                    else ""))
        out = [f"- {where}:"]
        if c.get("formatting") and c["before"] == c["after"]:
            # Formatting rewrites no text, so a before/after pair would read as a
            # change that did not happen. Say what changed instead, and show the
            # line so the words it landed on can be confirmed.
            out.append(f"  - set {c['formatting']}: “{_preview(c['after'], 300)}”"
                       + (f" — reviewer: “{_preview(c['instruction'])}”"
                          if c.get("instruction") else ""))
            return out
        out.append(f"  - was: “{_preview(c['before'], 300)}”")
        out.append(f"  - now: “{_preview(c.get('after') or '(line removed)', 300)}”"
                   + (f" — reviewer: “{_preview(c['instruction'])}”"
                      if c.get("instruction") else "")
                   + (f" — also set {c['formatting']}"
                      if c.get("formatting") else ""))
        return out

    if changes:
        here = [c for c in changes if not c.get("layout")]
        indd = [c for c in changes if c.get("layout")]
        L.append(f"## Changes to review — {len(changes)} line(s)\n")
        total = (f"All {ap['applied']} applied corrections"
                 if ap is not None else "Every applied correction")
        L.append(f"{total}, shown in the line(s) they changed — a line several "
                 f"touched appears once. They are split by where you act: the "
                 f"first group is changed in the file and only needs confirming; "
                 f"the second reflows the page, which is InDesign's to settle.\n")
        if here:
            L.append(f"### Changed here — confirm it reads right — {len(here)}\n")
            for c in here:
                L.extend(_change_line(c))
            L.append("")
        if indd:
            L.append(f"### Needs to be done in InDesign — {len(indd)}\n")
            L.append("The engine set the paragraph, but where the text now falls "
                     "on the page is InDesign's — open the file and confirm each "
                     "reflowed as intended.\n")
            for c in indd:
                L.extend(_change_line(c))
            L.append("")

    checks = d.get("checks") or []
    if checks:
        L.append(f"## To check in InDesign — {len(checks)}\n")
        L.append("Whether a line breaks well, a heading is stranded or a page runs "
                 "long is decided when InDesign sets the text, so no comparison of "
                 "files can settle it. Each of these is located; none was guessed "
                 "at.\n")
        for c in checks:
            where = _page_where(c)
            if c.get("paragraph", -1) >= 0:
                where += f" (story `{c['story_id']}`, ¶ {c['paragraph']})"
            L.append(f"- **{where}**: {c['what']} — “{_preview(c['why'])}”")
        L.append("")

    L.append("## Verification\n")
    L.append("The file after correcting is compared word for word against what a "
             "clean apply of the list *should* produce. Anything that differs is "
             "a change the corrections do not explain — there is nowhere for it "
             "to hide.\n")
    # Every id parsed reaches exactly one outcome — applied, needing a human, a
    # no-op, or merged into another edit. Said as an equation so the count is
    # checkable rather than taken on faith, and so a merge (several ids becoming one)
    # is visibly accounted for rather than looking like ids that went missing. The
    # left side carries any emptied-line removal the apply added on its own, so the
    # two sides balance: those are extra outcomes with no parsed id behind them.
    if ap is not None:
        merged_away = ap.get("merged_away", 0)
        para_removals = ap.get("para_removals", 0)
        fmt_companions = ap.get("fmt_companions", 0)
        parsed_n = d["parse"]["edits"]
        # A resolution moves an edit from "for a human" to "applied", so the
        # equation counts the flags still standing, not the flags as raised —
        # and a deliberate set-aside is its own addend, neither applied nor
        # awaiting anyone.
        pieces = [f"{ap['applied']} applied", f"{len(ap_flagged)} for a human",
                  f"{len(ap['no_op'])} no-op"]
        if ap_dismissed:
            pieces.append(f"{len(ap_dismissed)} set aside")
        if merged_away:
            pieces.append(f"{merged_away} merged into others")
        added = para_removals + fmt_companions
        extra = []
        if para_removals:
            extra.append(f"{para_removals} emptied-line removal(s)")
        if fmt_companions:
            extra.append(f"{fmt_companions} companion format edit(s)")
        # A resolution of a flagged *edit* moves that edit across the equals
        # sign (one more applied, one fewer for a human), so the two sides
        # still balance. A resolution of a comment that never became an edit
        # applies a change no parsed id stands behind, so it joins the left
        # side like the other synthetic outcomes.
        # A touch-up answers no flag and applies no correction from the list,
        # so it stands outside the equation entirely.
        no_edit = sum(1 for r in resolutions
                      if not r.get("touchup")
                      and not (by_item.get(r.get("item_id")) or {}).get("edit_ids"))
        if no_edit:
            extra.append(f"{no_edit} resolution(s) of an unextracted comment")
        left = f"{parsed_n} parsed" + (" + " + " + ".join(extra) if extra else "")
        balances = (parsed_n + added + no_edit
                    == ap["applied"] + len(ap_flagged) + len(ap_dismissed)
                    + len(ap["no_op"]) + merged_away)
        note = "" if balances else " — counts do not reconcile"
        L.append(f"- Corrections: {left} = " + " + ".join(pieces) + f"{note}.")
    L.append(f"- Paragraphs: {verify['paragraphs_before']:,} before, "
             f"{verify['paragraphs_after']:,} after"
             + (f" ({verify.get('paragraphs_expected', 0):,} expected — the "
                f"corrections asked for the difference)"
                if verify.get("structure_intended") else "")
             + ("" if not verify["structure_changed"]
                else " — **a paragraph was added, removed or merged**") + ".")
    disc = verify["discrepancies"]
    if not disc:
        L.append("- No unaccounted changes.\n")
    else:
        L.append(f"\n### Unaccounted changes — {len(disc)}\n")
        L.append("Changes present in the file that no correction asked for — "
                 "collateral damage, or a correction carried out differently "
                 "than written.\n")
        for x in disc:
            where = (f"story `{x['story_id']}`"
                     + (f", paragraph {x['paragraph']}" if x["paragraph"] >= 0
                        else ""))
            L.append(f"- {where}:")
            L.append(f"  - was: “{_preview(x['before'])}”")
            L.append(f"  - now: “{_preview(x['after'])}”")
        L.append("")

    # Edits the after file does not carry as written — MISSING (never anchored)
    # or DEVIATES (changed, but not to the requested text). In verify mode this
    # is the only place either shows. In apply mode a MISSING is the same edit
    # the "for a human" list already explains, so it is dropped here; a DEVIATES,
    # though, means our own written file diverged from a clean apply — a bug in
    # the writer, worth surfacing loudly rather than hiding as a duplicate.
    off = [r for r in verify["reconciliations"] if r["status"] != APPLIED_EXACTLY]
    if ap is not None:
        off = [r for r in off if r["status"] == DEVIATES]
    if off:
        L.append(f"## Corrections not carried out as written — {len(off)}\n")
        for r in off:
            L.append(f"- `{r['id']}` ({VERIFY_TITLES.get(r['status'], r['status'])})"
                     f" — {_change(r)}")
        L.append("")

    return "\n".join(L)


def _change(o: dict) -> str:
    """A one-line 'find → replace' for the report, deletions and insertions read
    naturally. The reviewer's own note is appended when there is one, so a flagged
    row an editor has to act on still carries the words the mark was made with —
    not just the find/replace the model inferred from them."""
    find, replace = _preview(o.get("find", "")), _preview(o.get("replace", ""))
    note = o.get("instruction", "").strip()
    if find and not replace:
        core = f"delete “{find}”"
    elif not find:
        core = f"“{replace}”" if replace else ""
    else:
        core = f"“{find}” → “{replace}”"
    if note and note != o.get("replace", ""):
        return f"{core} — reviewer: “{_preview(note)}”" if core \
            else f"reviewer: “{_preview(note)}”"
    return core


def _break_notes(breaks: dict) -> str:
    """What a manual save did to the section breaks around its line, as one
    clause — empty when it touched none."""
    bits = []
    if breaks.get("added_after"):
        bits.append("added a section break after it")
    if breaks.get("removed_above"):
        bits.append("removed the break above it")
    if breaks.get("removed_below"):
        bits.append("removed the break below it")
    return " and ".join(bits)


def _preview(s: str, n: int = 90) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"
