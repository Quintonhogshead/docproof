"""Turning findings.json into something a writer can read.

The pipeline's own output is a record: every finding, every status, token
counts. This module answers a different question — "what did it change, and
why?" — so it groups by the kind of mistake, shows each fix as a before/after
with the altered words marked, and says everything in words rather than
scores. Nothing here re-derives anything: the reviewed document is already
written by the time this runs.
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from docproof.providers import estimate_cost

# Confidence is a three-point scale in the data. On screen it is a sentence
# about how much the reader should trust the change.
CONFIDENCE_WORDS = {
    "high": "Confident",
    "medium": "Fairly confident",
    "low": "Unsure — worth a look",
}

STATUS_WORDS = {
    # A query is not a failure to change something — it is the other half of
    # the deliverable. The reviewed file carries it as a margin comment, and
    # this is the word for that, so it stops reading as a change that fell
    # over. See docs/deliverables.md.
    "query": "Asked in the margin — nothing was changed",
    "skipped_low_confidence": "Left alone — below your care setting",
    "rejected_no_anchor": "Couldn't be placed in the document",
    "rejected_overlap": "Overlapped another change",
    "rejected_duplicate": "Already covered by another change",
    "rejected_noop": "Would not have changed anything",
    # The remaining two were missing, which left their cards with no reason at
    # all on them — and the panel they sit in now tells the reader that each
    # finding carries its own, so every status a finding can hold needs a word.
    "rejected_oversized": "A real catch, but too large a fix to make for you",
    "rejected_by_verifier": "Set aside by the second reader",
}

_WORDS = re.compile(r"\s+")


def diff_segments(before: str, after: str) -> tuple[list[dict], list[dict]]:
    """Word-level before/after, each split into kept and changed runs.

    Corrections are usually a character or two inside a whole sentence, so
    showing the sentence twice without marking the difference makes the reader
    hunt for it."""
    old, new = _WORDS.split(before.strip()), _WORDS.split(after.strip())
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    left: list[dict] = []
    right: list[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("equal", "delete", "replace") and i1 != i2:
            left.append({"text": " ".join(old[i1:i2]),
                         "changed": tag != "equal"})
        if tag in ("equal", "insert", "replace") and j1 != j2:
            right.append({"text": " ".join(new[j1:j2]),
                          "changed": tag != "equal"})
    return left, right


def _finding_view(f: dict, names: dict[str, str]) -> dict:
    before, after = diff_segments(f["original_text"], f["corrected_text"])
    return {
        "finding_id": f["finding_id"],
        "para_id": f["para_id"],
        "error_type": f["error_type"],
        "error_name": names.get(f["error_type"], f["error_type"]),
        "original_text": f["original_text"],
        "corrected_text": f["corrected_text"],
        "before": before,
        "after": after,
        "explanation": f.get("explanation", ""),
        "confidence": f.get("confidence", "medium"),
        "confidence_word": CONFIDENCE_WORDS.get(f.get("confidence", "medium"),
                                                "Fairly confident"),
        "status": f.get("status", ""),
        "status_word": _status_word(f),
        "applied": bool(f.get("applied")),
    }


def _status_word(f: dict) -> str:
    """Why this finding is where it is, in words.

    One case can't be read off the status alone: a finding that validated and
    still wasn't written. For plain text there is one cause — the writer
    re-checked the quoted words at the moment of editing, found they had moved,
    and refused rather than edit the wrong characters.

    A formatting mark has two, and findings.json cannot tell them apart: the
    text was already set that way (not a failure at all — the model reads plain
    text and cannot see the italics on a title it correctly reports), or the
    same anchor re-check refused it, which happens when a correction inside the
    title applied first and shifted the offsets underneath it. Both leave
    status "validated", `format` set and `applied` false, and only the run log
    distinguishes them — so say both rather than pick one and be wrong."""
    status = f.get("status", "")
    if status == "validated" and not f.get("applied"):
        return ("Already set that way, or the words around it had moved"
                if f.get("format")
                else "The words it quotes had moved before it was written")
    return STATUS_WORDS.get(status, "")


def build_report(findings_path: str | Path,
                 names: dict[str, str] | None = None) -> dict:
    """A findings.json, re-read for a human. `names` maps error-type keys to
    their display names; unknown keys fall back to the key itself."""
    data = json.loads(Path(findings_path).read_text(encoding="utf-8"))
    names = names or {}
    findings = [_finding_view(f, names) for f in data.get("findings", [])]

    # Four ways a finding can end up not being a tracked change, and only one
    # of them is a disappointment. A query is a deliverable — a question the
    # author is meant to answer — so it gets its own bucket instead of being
    # swept in with the rejections; and the one status that really does mean
    # "there was nowhere to put this" is separated from the ones that mean
    # "another change already covered it", which are working as intended.
    applied = [f for f in findings if f["applied"]]
    rest = [f for f in findings if not f["applied"]]
    queries = [f for f in rest if f["status"] == "query"]
    low = [f for f in rest if f["status"] == "skipped_low_confidence"]
    not_placed = [f for f in rest if f["status"] == "rejected_no_anchor"]
    other = [f for f in rest if f["status"] not in
             ("query", "skipped_low_confidence", "rejected_no_anchor")]

    groups: dict[str, dict] = {}
    for f in applied:
        g = groups.setdefault(f["error_type"], {
            "error_type": f["error_type"], "error_name": f["error_name"],
            "findings": []})
        g["findings"].append(f)
    ordered = sorted(groups.values(), key=lambda g: (-len(g["findings"]),
                                                     g["error_name"]))
    for g in ordered:
        g["count"] = len(g["findings"])

    usage = data.get("usage") or {}
    config = data.get("config") or {}
    cost = estimate_cost(
        config.get("model", ""),
        input_tokens=usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        batch=bool(data.get("batch")))
    # Sapling is billed per character and never in the model estimate, so fold
    # its charge in here too — otherwise the report headline understates what
    # the spending ledger and the completion email both show.
    sapling_cost = usage.get("sapling_cost", 0.0) or 0.0
    if sapling_cost:
        cost = (cost or 0.0) + sapling_cost

    return {
        "source": Path(data.get("source", "")).name,
        "generated_at": data.get("generated_at", ""),
        "model": config.get("model", ""),
        "batch": bool(data.get("batch")),
        "headline": {
            "applied": len(applied),
            "paragraphs": len({f["para_id"] for f in applied}),
            "top_name": ordered[0]["error_name"] if ordered else "",
            "top_count": ordered[0]["count"] if ordered else 0,
            "low_confidence": len(low),
            "queries": len(queries),
            "not_placed": len(not_placed),
            "not_applied": len(other),
        },
        "cost": cost,
        "groups": ordered,
        "queries": queries,
        "low_confidence": low,
        "not_placed": not_placed,
        "not_applied": other,
        "examination_graph": data.get("examination_graph"),
    }
