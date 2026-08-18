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
                 deterministic: bool = True) -> tuple[Path, Path]:
    """Write `corrections.json` and `corrections_notes.md` into `out_dir`, and
    return their paths. `deterministic` is False when the opt-in sanity gate ran —
    a model call — so the report does not over-claim being model-free."""
    payload = _payload(source_path=source_path, after_path=after_path,
                       parse=parse, apply=apply, verify=verify, comments=comments,
                       deterministic=deterministic)
    json_path = out_dir / "corrections.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    md_path = out_dir / "corrections_notes.md"
    md_path.write_text(_markdown(payload), encoding="utf-8")
    log.info("Wrote %s and %s", md_path, json_path)
    return md_path, json_path


def _payload(*, source_path, after_path, parse, apply, verify, comments=(),
             deterministic=True) -> dict:
    needs_human = [c for c in comments if c.needs_human]
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
            "reconciliations": [
                {"id": r.edit.id, "status": r.status,
                 "find": r.edit.find, "replace": r.edit.replace,
                 "detail": r.detail}
                for r in verify.reconciliations],
            "discrepancies": [dataclasses.asdict(d) for d in verify.discrepancies],
        },
        # One row per reviewer comment — the ledger that makes sure none is lost.
        # `total` and `unresolved` are the honest headline: how many marks came in,
        # and how many a person still owns (flagged, or never turned into an edit).
        "comments": {
            "total": len(comments),
            "unresolved": len(needs_human),
            "items": [_comment(c) for c in comments],
        },
    }


def _outcome(o) -> dict:
    return {"id": o.edit.id, "status": o.status, "find": o.edit.find,
            "replace": o.edit.replace, "instruction": o.edit.instruction,
            "story_id": o.story_id, "paragraph": o.paragraph,
            "occurrences": o.occurrences, "detail": o.detail}


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
    if (verify["clean"] and (ap is None or not ap["flagged"])
            and not com["unresolved"]):
        L.append("**Clean.** Every correction landed exactly, nothing else in the "
                 "file changed, and every reviewer comment was accounted for.\n")
    else:
        headline = []
        if ap is not None:
            headline.append(f"{ap['applied']} applied")
            if ap["flagged"]:
                headline.append(f"{len(ap['flagged'])} need a human")
        if com["total"]:
            headline.append(f"{com['total']} comment(s)")
            if com["unresolved"]:
                headline.append(f"{com['unresolved']} unresolved")
        if verify["discrepancies"]:
            headline.append(f"{len(verify['discrepancies'])} unaccounted change(s)")
        if verify["structure_changed"]:
            headline.append("paragraph count changed")
        L.append("**" + ", ".join(headline) + ".** See below.\n")

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
        L.append(f"## Applied — {ap['applied']}\n")
        if ap["flagged"]:
            L.append(f"## For a human — {len(ap['flagged'])}\n")
            L.append("Each of these was refused rather than guessed at.\n")
            by_status: dict[str, list[dict]] = {}
            for o in ap["flagged"]:
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

    L.append("## Verification\n")
    L.append("The file after correcting is compared word for word against what a "
             "clean apply of the list *should* produce. Anything that differs is "
             "a change the corrections do not explain — there is nowhere for it "
             "to hide.\n")
    L.append(f"- Paragraphs: {verify['paragraphs_before']:,} before, "
             f"{verify['paragraphs_after']:,} after"
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
