"""Jev as the pre-screen over the fixed recipe's local rule candidates.

The deterministic generators and LanguageTool are deliberately low precision:
they name places to examine, and the paid Sonnet + Luna screen pays a window
for each one. Jev (TypeSafe's System One, see ``galley.jev``) answers a typed
question about each site for about a cent a book, so the obvious misfires can
be dropped before the screen buys a judgment on them.

Measured 2026-09-17 (``~/.docproof-private-eval/run_jev_candidates.py``) over
553 rule candidates on the Redding miss paragraphs: at P >= 0.30 Jev kept 232
of them and preserved 56 of the 81 located misses, where the lane's own
Luna-low judge preserved 30 to 35 — deterministically, and replayable from
receipts on resume.

Two questions, one per site:

* a **Choice** over ``{corrected, original, either}`` when the row carries a
  concrete replacement — Jev compares the sentence as written against the
  sentence with the rule's correction applied; and
* a **Noul** when the row is a query-only candidate with nothing to compare —
  "the text at this site has a genuine <category> error".

Nothing here edits, and nothing here is proof: a surviving row is still only a
proposal for the ordinary gates. The lane never drops a row it did not judge.
A row whose site cannot be located exactly, a diagnostic-only category, and
every row left unanswered when Jev becomes unavailable all pass through
untouched.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Callable

from galley.jev import JevUnavailable

STAGE = "typed_prescreen"
# One request carries one paragraph and up to this many of its sites.
MAX_SITES_PER_REQUEST = 30
# Requests are answered in chunks, so an outage mid-stage keeps the answers
# already paid for and passes only the rest through unjudged.
CHUNK = 20
# A paragraph at or below this length is quoted whole; longer ones are cut
# down to the sentence around the site (jev_comma.sentence_around).
WHOLE_PARAGRAPH_CHARS = 700
NOTE_CHARS = 300

_SENT_END = re.compile(r'(?:(?<=[.!?])|(?<=[.!?]["”’\')\]]))\s+(?=["“‘(\[]?[A-Z])')
_ABBREV = re.compile(r"\b(Mr|Mrs|Ms|Dr|St|Mt|Jr|Sr|vs|etc|No)\.$")


def sentence_window(text: str, pos: int) -> tuple[int, int]:
    """``[start, end)`` of the sentence holding ``pos``; the whole paragraph
    when it is short enough to read in full."""
    if len(text) <= WHOLE_PARAGRAPH_CHARS:
        return 0, len(text)
    bounds = [0]
    for m in _SENT_END.finditer(text):
        if _ABBREV.search(text[:m.start()]):
            continue
        bounds.append(m.end())
    bounds.append(len(text))
    for start, end in zip(bounds, bounds[1:]):
        if start <= pos < end:
            return start, end
    return 0, len(text)


def _label(category: Any) -> str:
    return str(category or "proofreading").replace("_", " ")


def site_for_row(row: Mapping, text: str) -> dict | None:
    """The exact span this local row is about, or None when it cannot be sited.

    ``quote``/``occurrence`` locate the row in its paragraph exactly as
    ``fixed_local._locate`` does; the changed span is then the minimal
    difference between the quote and its replacement, so a whole-paragraph
    comparison shrinks to the words the rule actually touched. A row whose
    replacement equals its quote is a query-only candidate: it has a site but
    nothing to compare.
    """
    quote, occurrence, replacement = row.get("quote"), row.get("occurrence"), row.get("replacement")
    if (not isinstance(quote, str) or not quote or type(occurrence) is not int or occurrence < 1
            or not isinstance(replacement, str) or not isinstance(text, str) or not text):
        return None
    at = -1
    for _ in range(occurrence):
        at = text.find(quote, at + 1)
        if at < 0:
            return None
    if replacement == quote:
        return {"start": at, "end": at + len(quote), "before": quote, "after": None}
    prefix = 0
    while prefix < len(quote) and prefix < len(replacement) and quote[prefix] == replacement[prefix]:
        prefix += 1
    suffix = 0
    while (suffix < len(quote) - prefix and suffix < len(replacement) - prefix
           and quote[len(quote) - 1 - suffix] == replacement[len(replacement) - 1 - suffix]):
        suffix += 1
    start, end = at + prefix, at + len(quote) - suffix
    return {"start": start, "end": end, "before": text[start:end],
            "after": replacement[prefix:len(replacement) - suffix]}


def choice_question(qid: str, category: Any) -> dict:
    return {"type": "choice",
            "instructions": (f"Compare `variants.{qid}.original` and `variants.{qid}.corrected`. A rule "
                             f"flagged a possible {_label(category)} error. Under `house_rule`, which is correct?"),
            "criteria": {"corrected": "The correction is required; the original has this error",
                         "original": "The original is correct; the rule misfired",
                         "either": "Both are acceptable"}}


def noul_question(qid: str, category: Any) -> dict:
    return {"type": "noul",
            "instructions": (f"In `variants.{qid}.sentence`, the text at `variants.{qid}.site` has a "
                             f"genuine {_label(category)} error that a proofreader must fix under `house_rule`."),
            "criteria": {"true": "A proofreader would correct this site",
                         "false": "The site is correct as written, or the change is a style preference"}}


def build_requests(rows, texts, *, house_rule: str, diagnostic_types=frozenset()) -> tuple[list, list, list]:
    """``(jobs, index, unjudged)`` for ``rows``, as plain data.

    ``jobs`` are ``(state, questions)`` pairs for ``JevLedger.ask_many``, one
    per paragraph chunk of at most ``MAX_SITES_PER_REQUEST`` sites. ``index``
    holds, per job, ``(question_id, row_position, site)``. ``unjudged`` lists
    the positions of rows that are passed through without a question: a
    diagnostic-only category, or a row that cannot be sited exactly.
    """
    by_para: dict[str, list[tuple[int, Mapping, dict]]] = {}
    unjudged: list[int] = []
    for position, row in enumerate(rows):
        text = texts.get(row.get("para_id")) if isinstance(texts, Mapping) else None
        site = site_for_row(row, text) if isinstance(text, str) else None
        if row.get("category") in diagnostic_types or site is None:
            unjudged.append(position)
            continue
        by_para.setdefault(row["para_id"], []).append((position, row, site))
    jobs, index = [], []
    for para_id in sorted(by_para):
        text = texts[para_id]
        group = by_para[para_id]
        for offset in range(0, len(group), MAX_SITES_PER_REQUEST):
            chunk = group[offset:offset + MAX_SITES_PER_REQUEST]
            variants, questions, entries = {}, {}, []
            for number, (position, row, site) in enumerate(chunk, start=1):
                qid = f"c{number:02d}"
                start, end = sentence_window(text, site["start"])
                if site["after"] is None:
                    variants[qid] = {"sentence": text[start:end],
                                     "site": site["before"] or text[max(0, site["start"] - 20):site["start"] + 20],
                                     "kind": row.get("category"),
                                     "note": json.dumps(row.get("reason", ""), ensure_ascii=False)[:NOTE_CHARS]}
                    questions[qid] = noul_question(qid, row.get("category"))
                else:
                    variants[qid] = {"original": text[start:end],
                                     "corrected": text[start:site["start"]] + site["after"] + text[site["end"]:end],
                                     "kind": row.get("category")}
                    questions[qid] = choice_question(qid, row.get("category"))
                entries.append((qid, position, site))
            jobs.append(({"house_rule": house_rule, "paragraph": text, "variants": variants}, questions))
            index.append(entries)
    return jobs, index, unjudged


def read_answer(answer: Mapping) -> dict:
    """One Jev answer as ``{"p", "confidence", "question"}``.

    The probability a proofreader would make this change: the Noul itself, or
    the Choice's probability of ``corrected``. An answer that carries neither
    is no judgment at all and scores None, so its row is passed through.
    """
    if not isinstance(answer, Mapping):
        return {"p": None, "confidence": None, "question": None}
    if "noul" in answer:
        return {"p": float(answer["noul"]), "confidence": None, "question": "noul"}
    probabilities = answer.get("probabilities")
    if isinstance(probabilities, Mapping) and "corrected" in probabilities:
        return {"p": float(probabilities["corrected"]),
                "confidence": float(answer["confidence"]) if answer.get("confidence") is not None else None,
                "question": "choice"}
    return {"p": None, "confidence": None, "question": None}


def _with_evidence(row: Mapping, judgment: Mapping) -> dict:
    kept = dict(row)
    evidence = dict(kept.get("local_evidence") or {})
    evidence["jev"] = {"p": judgment["p"], "confidence": judgment["confidence"],
                       "question": judgment["question"]}
    kept["local_evidence"] = evidence
    return kept


def readable_span(text: str, site: Mapping) -> tuple[str, str | None]:
    """The dropped site widened to whole words, for the record.

    The judged span is the minimal difference, so teh -> the narrows to
    eh -> he. That is the right comparison to send and the wrong thing to
    read back in a log: widened to its surrounding non-space characters it
    is the word the rule was actually about.
    """
    start, end = site["start"], site["end"]
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    before = text[start:end]
    if site["after"] is None:
        return before, None
    return before, text[start:site["start"]] + site["after"] + text[site["end"]:end]


def _dropped_entry(row: Mapping, site: Mapping, judgment: Mapping, text: str) -> dict:
    before, replacement = readable_span(text, site)
    return {"para_id": row.get("para_id"), "category": row.get("category"),
            "source": row.get("source"), "occurrence": row.get("occurrence"),
            "start": site["start"], "end": site["end"],
            "before": before, "replacement": replacement,
            "p": judgment["p"], "confidence": judgment["confidence"],
            "question": judgment["question"]}


def prescreen_local_candidates(rows, texts, *, ledger, house_rule, threshold,
                               should_cancel: Callable[[], bool] | None = None,
                               progress: Callable[[int, int], None] | None = None):
    """Drop the local rule candidates Jev judges to be misfires.

    Returns ``(kept_rows, dropped, evidence)``. ``kept_rows`` preserves the
    input order and carries ``row["local_evidence"]["jev"]`` on every row that
    was judged. ``dropped`` records each dropped site with its probability.
    ``evidence`` is a deterministic summary for the stage receipt: it holds no
    timings or call counts, so a resumed run records the same block.

    Every row that is not judged — a diagnostic-only category, a row that
    cannot be sited exactly, an answer that is not a judgment, and everything
    left over when Jev becomes unavailable mid-stage — is kept untouched.
    """
    from galley.fixed_policy import DIAGNOSTIC_ONLY_TYPES
    rows = list(rows)
    jobs, index, unjudged = build_requests(rows, texts, house_rule=house_rule,
                                           diagnostic_types=DIAGNOSTIC_ONLY_TYPES)
    judgments: dict[int, tuple[dict, dict]] = {}
    unavailable = None
    done = 0
    for offset in range(0, len(jobs), CHUNK):
        batch, entries = jobs[offset:offset + CHUNK], index[offset:offset + CHUNK]
        try:
            results = ledger.ask_many(STAGE, batch, should_cancel=should_cancel)
        except JevUnavailable as exc:
            unavailable = f"{type(exc).__name__}: {exc}"
            break
        for result, group in zip(results, entries):
            answers = result.get("answers") or {}
            for qid, position, site in group:
                judgments[position] = (read_answer(answers.get(qid, {})), site)
        done += len(batch)
        if progress is not None:
            progress(done, len(jobs))
    kept, dropped, judged = [], [], 0
    counts: dict[str, dict[str, int]] = {}
    for position, row in enumerate(rows):
        judgment, site = judgments.get(position, (None, None))
        if judgment is None or judgment["p"] is None:
            kept.append(dict(row))
            continue
        judged += 1
        bucket = counts.setdefault(str(row.get("category")), {"kept": 0, "dropped": 0})
        if judgment["p"] >= threshold:
            bucket["kept"] += 1
            kept.append(_with_evidence(row, judgment))
        else:
            bucket["dropped"] += 1
            dropped.append(_dropped_entry(row, site, judgment, texts[row["para_id"]]))
    evidence = {"kind": "jev_prescreen", "stage": STAGE, "model": getattr(ledger, "model", None),
                "threshold": threshold, "requests": len(jobs), "rows": len(rows),
                "judged": judged, "kept": judged - len(dropped), "dropped": len(dropped),
                "passed_through": len(rows) - judged, "unsited_or_diagnostic": len(unjudged),
                "by_category": {key: counts[key] for key in sorted(counts)}}
    if unavailable:
        evidence["unavailable"] = unavailable
    return kept, dropped, evidence


__all__ = ["prescreen_local_candidates", "build_requests", "read_answer", "site_for_row",
           "sentence_window", "choice_question", "noul_question", "readable_span", "STAGE",
           "MAX_SITES_PER_REQUEST", "WHOLE_PARAGRAPH_CHARS"]
