"""Re-screen a completed fixed run's final-reader questions in the walk-through scope.

Wilder's first v5 run (2026-09-14) finished with every reading complete and
one author question, although Fable and Astra had raised eight more about
facts, logic, continuity and structure. The Sonnet/Luna screen that rules on
a reader's questions judged them under the plain proofreading contract and
dropped seven as "outside proofreading scope"; the walk-through rider that
told the readers to raise exactly such questions never reached it.

This extends the completed workspace under its own identity, with new
receipts: the dropped fact/logic, continuity and structure questions are
screened again with the rider (Opus on disagreement), Astra reviews every
surviving question, a `walkthrough_questions` stage is recorded, and the
result and checkpoint are rewritten so the driver can package and deliver
again. Nothing is re-read; no correction is added or removed.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from galley.fixed_workflow import ASTRA, FixedWorkflow, FixedWorkflowError, VERSION

STAGE = "walkthrough_questions"
REINSTATED_CATEGORIES = frozenset({"fact_logic", "continuity", "structure"})
SOURCE_STAGES = frozenset({"fable_screened", "astra_screened", "fable_disputes", "astra_disputes"})


class FixedReinstateError(ValueError):
    pass


def dropped_question_candidates(result, current):
    """The final readers' fact/logic, continuity and structure questions the
    screen dropped, re-anchored to the delivered text; rows that no longer
    anchor are returned separately."""
    candidates, unanchored, seen = [], [], set()
    for entry in result["history"]:
        if entry.get("stage") not in SOURCE_STAGES or entry.get("decision", {}).get("action") != "drop":
            continue
        for row in entry["site"]["proposals"]:
            if row.get("action") != "query" or row.get("category") not in REINSTATED_CATEGORIES or row["id"] in seen:
                continue
            seen.add(row["id"])
            text = current.get(row["para_id"], "")
            lo, hi = row["start"], row["end"]
            if text[lo:hi] != row["before"]:
                if text.count(row["before"]) == 1:
                    lo = text.find(row["before"]); hi = lo + len(row["before"])
                else:
                    unanchored.append(row)
                    continue
            candidates.append({**row, "start": lo, "end": hi})
    return candidates, unanchored


def reinstate_walkthrough_questions(book, workspace, *, progress=None, max_api_usd=10, calls=None):
    """Extend the completed run at <workspace>/runs/fixed; return the new result."""
    workspace = Path(workspace).resolve()
    directory = workspace / "runs" / "fixed"
    result_path = directory / "result.json"
    if not result_path.is_file():
        raise FixedReinstateError("This workspace holds no completed fixed proofread")
    result = json.loads(result_path.read_text("utf-8"))
    if result.get("status") != "completed" or result.get("execution_mode") != "fixed":
        raise FixedReinstateError("The fixed proofread has not completed")
    if any(s["stage"] == STAGE for s in result["stages"]):
        raise FixedReinstateError("This run's questions were already re-screened")
    if result.get("poetry_only"):
        raise FixedReinstateError("A spelling-only run has no walk-through questions")
    flow = FixedWorkflow(book, directory, calls=calls, progress=progress, max_api_usd=max_api_usd)
    if flow.identity != result["identity"] or flow.identity["version"] != VERSION:
        raise FixedWorkflowError("The completed run belongs to a different source or fixed recipe")
    # Restore the finished state exactly as the run left it.
    flow.original, flow.current = result["original"], dict(result["accepted"])
    flow.questions, flow.formats = list(result["questions"]), list(result["formats"])
    flow.history, flow.stages = list(result["history"]), list(result["stages"])
    flow.needs_human = result["editorial_verdict"] == "needs_human"
    poetry = json.loads((directory / "stages" / "poetry.json").read_text("utf-8"))
    flow.poetry_ids = set(poetry["evidence"]["poetry_ids"])
    sheet = json.loads((directory / "stages" / "story_sheet.json").read_text("utf-8"))["evidence"].get("sheet")
    if sheet:
        from docproof.storysheet import StorySheet, prompt_section
        flow.context = prompt_section(StorySheet.model_validate(sheet))
    flow._stage(STAGE)
    candidates, unanchored = dropped_question_candidates(result, flow.current)
    before_ids = {q["id"] for q in flow.questions}
    snapshot = dict(flow.current)
    accepted = flow._adjudicate(STAGE, candidates, ())
    # Only questions are reinstated. A screener may answer a question with an
    # edit; that edit was not checked by the run's meaning and correction
    # gates, so it is recorded and left unapplied.
    for row in accepted:
        flow.history.append({"stage": STAGE, "unapplied_edit": row,
                             "reason": "Reinstatement adds author questions only; edits need the run's checks"})
    if flow.current != snapshot:
        raise FixedReinstateError("Reinstatement must not change the delivered text")
    # Astra reviews every question, as it does at the end of its own read.
    flow._comments([], STAGE, before=snapshot, model=ASTRA)
    reinstated = [q for q in flow.questions if q["id"] not in before_ids]
    flow._record(STAGE, candidates=len(candidates), unanchored=unanchored,
                 reinstated=[q["id"] for q in reinstated], questions=len(flow.questions))
    for stale in ("runs/driver/package.json", "runs/driver/delivery.json"):
        (workspace / stale).unlink(missing_ok=True)
    for stale in ("runs/final", "handoff"):
        shutil.rmtree(workspace / stale, ignore_errors=True)
    new = flow._write_result(False)
    return {"result": new, "reinstated": reinstated, "candidates": len(candidates),
            "unanchored": len(unanchored), "workspace": str(workspace)}


__all__ = ["STAGE", "dropped_question_candidates", "reinstate_walkthrough_questions", "FixedReinstateError"]
