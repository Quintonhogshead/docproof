"""Compact, source-bound screening packets and explicit pair disagreements."""
from __future__ import annotations

import json

PAIR = ("claude-sonnet-5", "gpt-6-luna")


def decision_key(row):
    """Reasons are explanations, not distinct editorial decisions."""
    if "verdict" in row:
        return (row["verdict"],)
    action = row["action"]
    if action == "apply":
        return action, row["replacement"]
    if action == "query":
        return action, row["question"], row["missing_knowledge"]
    return (action,)


def is_pair_disagreement(site):
    reviews = site.get("screening", {})
    if not isinstance(reviews, dict) or set(reviews) != set(PAIR):
        return False
    try:
        rows = [reviews[m] for m in PAIR]
        if any(not isinstance(row, dict) or row.get("id") != site["id"] or row.get("origin") for row in rows):
            return False
        if ("verdict" in rows[0]) != ("verdict" in rows[1]):
            return False
        if any((row["verdict"] not in {"approve", "reject"}) if "verdict" in row
               else (row["action"] not in {"apply", "drop", "query"}) for row in rows):
            return False
        return decision_key(rows[0]) != decision_key(rows[1])
    except (KeyError, TypeError):
        return False


def label(index):
    """The short per-request name a site is screened under: s01, s02, ...

    A site's durable id is a 22-character hash. Asked to copy 25 of them back,
    a reader sometimes returns one with a character added or dropped, and the
    coverage check then rightly refuses the whole window; on the first two
    production books that was the only cause of an incomplete review. The
    reader answers with the label; code maps it back to the hash. Nothing is
    matched approximately."""
    return f"s{index + 1:02d}"


def aliases(sites):
    """label -> durable site id, in request order."""
    return {label(i): site["id"] for i, site in enumerate(sites)}


def packet(sites):
    """Share each paragraph once, retaining every anchor and explanation.

    Generator internals remain in the local evidence, not repeated in every
    review prompt. Relevant neighbouring paragraphs are retained as context.
    Nested proposal IDs are omitted: only the containing site needs a ruling.
    Sites are presented under their per-request labels (see `label`).
    """
    paragraphs, related, compact = {}, {}, []
    for index, site in enumerate(sites):
        pid = site["para_id"]
        paragraphs[pid] = {"text": site["paragraph"]}
        if site["source"] != site["paragraph"]:
            paragraphs[pid]["source"] = site["source"]
        row = {"id": label(index), **{k: site[k] for k in ("para_id", "start", "end", "before")}}
        row["proposals"] = []
        for proposal in site["proposals"]:
            row["proposals"].append({k: proposal[k] for k in (
                "start", "end", "before", "replacement", "category", "action", "format",
                "reason", "missing_knowledge", "models") if k in proposal})
            related.update(proposal.get("related_paragraphs", {}))
        compact.append(row)
    return {"sites": compact, "paragraphs": paragraphs,
            "context": {pid: text for pid, text in related.items() if pid not in paragraphs}}


# A screening decision is written per site, and the reader thinks per site: at
# 43-50 sites per window Sonnet's output ran to ~11k tokens against a 12k
# ceiling and Luna dropped IDs from its coverage. Both readers stayed complete
# on shorter windows.
MAX_SITES = 25


def windows(sites, limit=20000, max_sites=MAX_SITES):
    """Size actual compact packets; never truncate a long singleton site."""
    if max_sites < 1:
        raise ValueError("a screening window holds at least one site")
    batch = []
    for site in sites:
        proposed = batch + [site]
        if batch and (len(proposed) > max_sites or
                      len(json.dumps(packet(proposed), ensure_ascii=False, separators=(",", ":"))) > limit):
            yield batch
            batch = []
        batch.append(site)
    if batch:
        yield batch
