"""Comparing an IDML before and after, and reconciling every change against the
edit list.

The point Nick asked for — "arm the AI to find 100% of the discrepancies" — done
as subtraction, not as a second reading. We compute what a clean apply of the
edit list *would* produce from the before file, and compare that expectation to
what the after file *actually* says. Every paragraph where the two disagree is a
change the edits do not explain: collateral damage, or an edit carried out
differently than asked. There is nowhere for such a change to hide, because the
comparison is exhaustive by construction.

This works whether the after file came from this engine (then it agrees exactly,
and the report is clean) or from a hand-run script (then every stray space, merged
paragraph and deleted line the script produced turns up here).
"""
from __future__ import annotations

from pathlib import Path

from .apply import apply_to_stories
from .idml import Story, read_stories
from .model import (APPLIED_EXACTLY, DEVIATES, Discrepancy, Edit, MISSING,
                    Reconciliation, ReviewChange, VerifyReport)


def verify(before_idml: str | Path, after_idml: str | Path,
           edits: list[Edit], *,
           withheld: dict[str, str] | None = None,
           scope=None) -> VerifyReport:
    """Reconcile the after file against the before file and the edit list.

    `withheld` must match what `apply` used: an edit a sanity gate held back was
    never written, so the expectation has to withhold it too, or the self-check
    would read its absence as an unaccounted change. `scope` must match for the
    same reason — a page map narrows where an edit lands, so an expectation
    computed without it would disagree with the file that was written."""
    expected = read_stories(before_idml)         # a fresh copy to mutate
    outcomes, _ = apply_to_stories(expected, edits, withheld=withheld,
                                   scope=scope)
    actual = read_stories(after_idml)

    expected_by_id = {s.story_id: s for s in expected}
    actual_by_id = {s.story_id: s for s in actual}

    discrepancies: list[Discrepancy] = []
    before = read_stories(before_idml)           # untouched, for the diff report
    before_by_id = {s.story_id: s for s in before}

    p_before = sum(len(s.paragraphs) for s in before)
    p_after = sum(len(s.paragraphs) for s in actual)
    p_expected = sum(len(s.paragraphs) for s in expected)

    for sid, exp in expected_by_id.items():
        act = actual_by_id.get(sid)
        bef = before_by_id.get(sid)
        if act is None:
            discrepancies.append(Discrepancy(sid, -1, "(story present)",
                                            "(story missing from the after file)"))
            continue
        discrepancies.extend(_story_discrepancies(sid, bef, exp, act))

    reconciliations = _reconcile(outcomes, expected_by_id, actual_by_id)
    changes = _review_changes(outcomes, before_by_id, actual_by_id)
    return VerifyReport(reconciliations=tuple(reconciliations),
                        discrepancies=tuple(discrepancies),
                        paragraphs_before=p_before, paragraphs_after=p_after,
                        paragraphs_expected=p_expected,
                        changes=tuple(changes))


def _story_discrepancies(sid: str, before: Story | None, expected: Story,
                         actual: Story) -> list[Discrepancy]:
    """Paragraphs where the after file disagrees with a clean apply.

    Aligned by index; if the counts differ (a merge, a split, a deleted line)
    the tail cannot align and the difference in count is itself the alarm, so we
    report the paragraphs that no longer line up and stop rather than flooding."""
    out: list[Discrepancy] = []
    exp_paras = expected.paragraphs
    act_paras = actual.paragraphs
    bef_paras = before.paragraphs if before else exp_paras
    n = min(len(exp_paras), len(act_paras))
    for i in range(n):
        if exp_paras[i].text != act_paras[i].text:
            b = bef_paras[i].text if i < len(bef_paras) else ""
            out.append(Discrepancy(sid, i, b, act_paras[i].text))
    if len(exp_paras) != len(act_paras):
        out.append(Discrepancy(
            sid, n,
            f"({len(exp_paras)} paragraphs expected)",
            f"({len(act_paras)} in the after file — a paragraph was "
            f"added, removed or merged that no correction asked for)"))
    return out


def _review_changes(outcomes, before_by_id, actual_by_id) -> list[ReviewChange]:
    """One `ReviewChange` per paragraph an applied edit changed — the whole line
    before (untouched source) and after (the corrected file) — so a person can
    read each correction in context. Grouped by paragraph in document order, so a
    line two edits touched is shown once, fully corrected, carrying both ids and
    notes. A paragraph whose net text did not change (an edit that cancelled out)
    is dropped: there is nothing to look at — unless what changed was the
    *formatting*, which rewrites no text and would otherwise disappear from the one
    section a designer uses to confirm the corrections read right."""
    order: list[tuple[str, int]] = []
    acc: dict[tuple[str, int], dict] = {}
    for o in outcomes:
        if not o.applied:
            continue
        key = (o.story_id, o.paragraph)
        if key not in acc:
            bef = before_by_id.get(o.story_id)
            act = actual_by_id.get(o.story_id)
            acc[key] = {
                "before": (bef.paragraphs[o.paragraph].text
                           if bef and 0 <= o.paragraph < len(bef.paragraphs)
                           else ""),
                "after": (act.paragraphs[o.paragraph].text
                          if act and 0 <= o.paragraph < len(act.paragraphs)
                          else ""),
                "ids": [], "notes": [], "formats": []}
            order.append(key)
        acc[key]["ids"].append(o.edit.id)
        if o.edit.format and o.edit.format not in acc[key]["formats"]:
            acc[key]["formats"].append(o.edit.format)
        note = (o.edit.instruction or "").strip()
        if note and note not in acc[key]["notes"]:
            acc[key]["notes"].append(note)
    changes: list[ReviewChange] = []
    for sid, para in order:
        d = acc[(sid, para)]
        if d["before"] == d["after"] and not d["formats"]:
            continue
        changes.append(ReviewChange(
            story_id=sid, paragraph=para, before=d["before"], after=d["after"],
            edit_ids=tuple(d["ids"]), instruction=" · ".join(d["notes"]),
            formatting=", ".join(d["formats"])))
    return changes


def _reconcile(outcomes, expected_by_id, actual_by_id) -> list[Reconciliation]:
    """Per edit: did the after file carry it out exactly, differently, or not at
    all? An edit that a clean apply placed is APPLIED_EXACTLY when the after
    paragraph matches the expected one, DEVIATES when it does not; an edit a
    clean apply could not place at all is MISSING."""
    recs: list[Reconciliation] = []
    for o in outcomes:
        if not o.applied:
            recs.append(Reconciliation(o.edit, MISSING, detail=o.status))
            continue
        exp = expected_by_id.get(o.story_id)
        act = actual_by_id.get(o.story_id)
        exp_text = (exp.paragraphs[o.paragraph].text
                    if exp and o.paragraph < len(exp.paragraphs) else None)
        act_text = (act.paragraphs[o.paragraph].text
                    if act and o.paragraph < len(act.paragraphs) else None)
        if exp_text is not None and exp_text == act_text:
            recs.append(Reconciliation(o.edit, APPLIED_EXACTLY))
        else:
            recs.append(Reconciliation(
                o.edit, DEVIATES,
                detail="the after file differs from the requested edit here"))
    return recs
