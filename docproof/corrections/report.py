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
from datetime import datetime, timezone
from pathlib import Path

from .model import (APPLIED_EXACTLY, ApplyReport, CommentDisposition,
                    DEVIATES, DISP_APPLIED, DISP_FLAGGED, DISP_NO_OP,
                    DISP_NOT_EXTRACTED, MISSING, VerifyReport)
from .parse import ParseResult

log = logging.getLogger("docproof.corrections.report")

# The apply statuses that mean a human has to look, mapped to a plain phrase.
FLAG_TITLES = {
    "not_found": "The text to change was not found",
    "ambiguous": "The text appears more than once",
    "crosses_paragraph": "The change would span a paragraph break",
    "routed_to_design": "A layout request, not a text edit",
    "overlaps": "Two corrections land on the same words",
    "withheld": "Held back by the sanity check",
    "unstyleable": "The formatting could not be applied here",
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
                 pages: tuple[int, int] = (0, 0),
                 checks: tuple = ()) -> tuple[Path, Path]:
    """Write `corrections.json` and `corrections_notes.md` into `out_dir`, and
    return their paths. `deterministic` is False when the opt-in sanity gate ran —
    a model call — so the report does not over-claim being model-free. `pages` is
    `(placed, total)` from the page map, reported so a run whose page narrowing
    silently did not happen says so."""
    payload = _payload(source_path=source_path, after_path=after_path,
                       parse=parse, apply=apply, verify=verify, comments=comments,
                       deterministic=deterministic, pages=pages, checks=checks)
    json_path = out_dir / "corrections.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    md_path = out_dir / "corrections_notes.md"
    md_path.write_text(_markdown(payload), encoding="utf-8")
    log.info("Wrote %s and %s", md_path, json_path)
    return md_path, json_path


def _payload(*, source_path, after_path, parse, apply, verify, comments=(),
             deterministic=True, pages=(0, 0), checks=()) -> dict:
    needs_human = [c for c in comments if c.needs_human]
    placed, total = pages
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
             "formatting": c.formatting}
            for c in verify.changes],
        # One row per reviewer comment — the ledger that makes sure none is lost.
        # `total` and `unresolved` are the honest headline: how many marks came in,
        # and how many a person still owns (flagged, or never turned into an edit).
        "comments": {
            "total": len(comments),
            "unresolved": len(needs_human),
            "items": [_comment(c) for c in comments],
        },
        # How much of the proof the page map placed in the book. A page it could
        # not place is one whose marks had the whole book to land in, which is the
        # difference between an edit that applies and one that is flagged — so it
        # is reported rather than left to be inferred from a flag count.
        "pages": {"placed": placed, "total": total},
        # The designer's list: what needs InDesign open, located. Composition is the
        # one thing this engine cannot do, so it is the one thing it does not claim.
        "checks": [dataclasses.asdict(c) for c in checks],
    }


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


def _comment(c: CommentDisposition) -> dict:
    return {"id": c.id, "page": c.page, "kind": c.kind,
            "instruction": c.instruction, "anchor": c.anchor,
            "disposition": c.disposition, "edit_ids": list(c.edit_ids),
            "detail": c.detail}


def _markdown(d: dict) -> str:
    L: list[str] = [f"# Corrections — {d['source_name']}\n"]
    when = d["generated_at"][:16].replace("T", " ")
    mode = ("applied to the InDesign file" if d["mode"] == "apply"
            else "checked against a file corrected elsewhere")
    how = ("deterministic, no model calls" if d.get("deterministic", True)
           else "deterministic apply, with a model sanity check over the edits")
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
    ap_flagged = ap["flagged"] if ap is not None else []
    flagged_uncovered = ([o for o in ap_flagged if o["id"] not in covered]
                         if com["total"] else ap_flagged)
    if (verify["clean"] and (ap is None or not ap["flagged"])
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
            applied_c = sum(1 for c in items if c["disposition"] == DISP_APPLIED)
            nochange_c = sum(1 for c in items if c["disposition"] == DISP_NO_OP)
            headline.append(f"{com['total']} reviewer comment(s)")
            headline.append(f"{applied_c} applied")
            if nochange_c:
                headline.append(f"{nochange_c} no change needed")
            if com["unresolved"]:
                headline.append(f"{com['unresolved']} need a human")
        elif ap is not None:
            headline.append(f"{ap['applied']} applied")
            if ap["no_op"]:
                headline.append(f"{len(ap['no_op'])} no change needed")
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
                  if c["disposition"] in (DISP_FLAGGED, DISP_NOT_EXTRACTED)]
    if unresolved:
        L.append(f"## Reviewer comments needing a human — {len(unresolved)}\n")
        L.append("Every one carries the reviewer's original note and the page it "
                 "was marked on, so it can be handled by hand.\n")
        for c in unresolved:
            where = f"page {c['page']}" if c["page"] else "—"
            why = DISP_TITLES.get(c["disposition"], c["disposition"])
            note = c["instruction"] or "(no note)"
            L.append(f"- **{where}** ({why}): “{_preview(note)}”"
                     + (f" — on “{_preview(c['anchor'])}”" if c["anchor"] else "")
                     + (f" — {c['detail']}" if c["detail"] else ""))
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

    # The applied changes, each in the line it changed, for a person to read down
    # and confirm — the designer's quick check that the corrections are right, not
    # just that they anchored. The verification section below is the complement.
    changes = d.get("changes") or []
    if changes:
        L.append(f"## Changes to review — {len(changes)} line(s)\n")
        lead = (f"All {ap['applied']} applied corrections, shown in the "
                f"{len(changes)} line(s) they changed — a line several corrections "
                f"touched appears once"
                if ap is not None else
                "Every applied correction, shown in the line it changed")
        L.append(f"{lead}. Read down and confirm each reads right. “was” is the "
                 f"original line, “now” the corrected one.\n")
        for c in changes:
            where = (f"story `{c['story_id']}`"
                     + (f", ¶ {c['paragraph']}" if c.get("paragraph", -1) >= 0
                        else ""))
            L.append(f"- {where}:")
            if c.get("formatting") and c["before"] == c["after"]:
                # Formatting rewrites no text, so a before/after pair would read as
                # a change that did not happen. Say what changed instead, and show
                # the line so the words it landed on can be confirmed.
                L.append(f"  - set {c['formatting']}: "
                         f"“{_preview(c['after'], 300)}”"
                         + (f" — reviewer: “{_preview(c['instruction'])}”"
                            if c.get("instruction") else ""))
                continue
            L.append(f"  - was: “{_preview(c['before'], 300)}”")
            L.append(f"  - now: “{_preview(c['after'], 300)}”"
                     + (f" — reviewer: “{_preview(c['instruction'])}”"
                        if c.get("instruction") else "")
                     + (f" — also set {c['formatting']}"
                        if c.get("formatting") else ""))
        L.append("")

    checks = d.get("checks") or []
    if checks:
        L.append(f"## To check in InDesign — {len(checks)}\n")
        L.append("Whether a line breaks well, a heading is stranded or a page runs "
                 "long is decided when InDesign sets the text, so no comparison of "
                 "files can settle it. Each of these is located; none was guessed "
                 "at.\n")
        for c in checks:
            where = (f"page {c['page']}" if c.get("page") else "—")
            if c.get("paragraph", -1) >= 0:
                where += f" (story `{c['story_id']}`, ¶ {c['paragraph']})"
            L.append(f"- **{where}**: {c['what']} — “{_preview(c['why'])}”")
        L.append("")

    L.append("## Verification\n")
    L.append("The file after correcting is compared word for word against what a "
             "clean apply of the list *should* produce. Anything that differs is "
             "a change the corrections do not explain — there is nowhere for it "
             "to hide.\n")
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


def _preview(s: str, n: int = 90) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"
