"""Exact, conservative adaptation of proposal-ID dispute decisions.

No aliases are guessed from prose. Every nested proposal must be explicitly
covered, and accepted text must be the exact correction for its named span.
"""
from __future__ import annotations

import json


def grouped_decisions(parsed, request, rule):
    """Return a working view and audit, or None for an unrelated/incomplete read."""
    if not request["stage"].endswith("_disputes") or rule["id_key"] != "id":
        return None
    try:
        sites = json.loads(request["user"])["sites"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(sites, list) or not sites or any(not isinstance(s, dict) or not s.get("proposals") for s in sites):
        return None
    if [s.get("id") for s in sites] != rule["ids"]:
        return None
    proposals = [p for s in sites for p in s["proposals"]]
    if any(not isinstance(p, dict) or not isinstance(p.get("id"), str) for p in proposals):
        return None
    ids = [p["id"] for p in proposals]
    rows = parsed.get("decisions", [])
    actual = [d.get("id") for d in rows]
    if (len(ids) != len(set(ids)) or set(ids) & set(rule["ids"])
            or len(actual) != len(ids) or set(actual) != set(ids)):
        return None
    # Validate all coordinates before interpreting any decision. A group-span
    # replacement must never be substituted into a shorter proposal span.
    for site in sites:
        text, lo, hi = site.get("paragraph"), site.get("start"), site.get("end")
        if (not isinstance(text, str) or type(lo) is not int or type(hi) is not int
                or not 0 <= lo <= hi <= len(text) or text[lo:hi] != site.get("before")):
            return None
        for p in site["proposals"]:
            a, b = p.get("start"), p.get("end")
            if (p.get("para_id") != site.get("para_id") or type(a) is not int or type(b) is not int
                    or not lo <= a <= b <= hi or text[a:b] != p.get("before")):
                return None
    by_id = {d["id"]: d for d in rows}
    normalized, audit = [], []
    for site in sites:
        members = site["proposals"]
        applied = []
        unusable = False
        for p in members:
            d = by_id[p["id"]]
            if d["action"] == "drop":
                continue
            # A query or novel replacement attached to the wrong ID has an
            # ambiguous scope. Discard that site, never invent a correction or
            # promote an operational mismatch into an author comment.
            if (d["action"] != "apply" or p.get("action") != "edit"
                    or d["replacement"] != p.get("replacement")
                    or (p.get("format") and (len(members) != 1 or d["replacement"] != p["before"]))):
                unusable = True
                break
            applied.append(p)
        for i, a in enumerate(applied):
            for b in applied[i + 1:]:
                overlap = (max(a["start"], b["start"]) <= min(a["end"], b["end"])
                           if a["start"] == a["end"] or b["start"] == b["end"] else
                           max(a["start"], b["start"]) < min(a["end"], b["end"]))
                if overlap:
                    unusable = True
        replacement = site["before"]
        if not unusable:
            for p in sorted(applied, key=lambda p: (p["start"], p["end"]), reverse=True):
                a, b = p["start"] - site["start"], p["end"] - site["start"]
                replacement = replacement[:a] + p["replacement"] + replacement[b:]
        action = "apply" if applied and not unusable else "drop"
        reason = ("Discarded ambiguous or conflicting proposal-ID decisions." if unusable else
                  "Exact proposal-ID decisions: " + " ".join(by_id[p["id"]]["reason"] for p in members))
        normalized.append({"id": site["id"], "action": action,
                           "replacement": replacement if action == "apply" else "",
                           "reason": reason, "missing_knowledge": "", "question": ""})
        audit.append({"site_id": site["id"], "proposal_ids": [p["id"] for p in members],
                      "action": action, "rejected_ambiguous": unusable})
    return {**parsed, "decisions": normalized}, {"version": 1, "kind": "complete_proposal_decisions", "sites": audit}
