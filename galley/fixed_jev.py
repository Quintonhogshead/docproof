"""Jev's brute-force single-character sites: a candidate SOURCE for the typed stage.

The recipe's readers propose what they happen to notice. Jev, TypeSafe's System
One judgment model, is cheap enough to be asked about every site of a kind, so
this lane enumerates them exhaustively and asks one typed question each:

* ``comma_insert`` — every word boundary, against the sentence with a comma added,
* ``comma_delete`` — every comma between words, against the sentence without it,
* ``spelling`` — every word in LanguageTool's confusion sets, against its alternates.

A site whose answer clears its threshold becomes an ordinary candidate row for
the Sonnet + Luna screen, carrying ``source="jev"`` and its probability. Jev
decides nothing: a row reaches the screen as a single-proposal site from one
model, which the screen can (and mostly does) drop.

Generation is pure and deterministic — ``*_sites``, ``build_requests`` and
``rows_from_answers`` need no network — so the recipe's arithmetic is testable
without a key. Only ``collect_jev_candidates`` speaks to the ledger, and it
answers with whatever it kept if the lane goes away mid-stage.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from galley.fixed_policy import (JEV_HOUSE_RULES, JEV_SENTENCE_LIMIT, JEV_THRESHOLDS,
                                 JEV_VARIANTS_PER_REQUEST)

VERSION = "fixed-jev-v1"
KINDS = ("comma_insert", "comma_delete", "spelling")
#: The house category each kind proposes under; both comma kinds are punctuation.
CATEGORIES = {"comma_insert": "punctuation", "comma_delete": "punctuation",
              "spelling": "spelling"}
#: Receipt stages under ``<run>/jev/``.
STAGES = {kind: "typed_" + kind for kind in KINDS}
#: The variant key each kind's question asks about, besides "original"/"either".
VARIANT = {"comma_insert": "with_comma", "comma_delete": "without_comma"}
CONFUSION_SETS = "org/languagetool/resource/en/confusion_sets.txt"

_SENT_END = re.compile(r'(?:(?<=[.!?])|(?<=[.!?]["”’\')\]]))\s+(?=["“‘(\[]?[A-Z])')
_ABBREV = re.compile(r"\b(Mr|Mrs|Ms|Dr|St|Mt|Jr|Sr|vs|etc|No)\.$")
_WORD = re.compile(r"[A-Za-z][A-Za-z’']*")


# ---------------------------------------------------------------- site generators

def comma_insert_sites(text: str) -> list[dict]:
    """Every offset at which a comma could be inserted: immediately before a
    space that follows a word character or a closing quote/bracket, where the
    preceding character is not already punctuation and a dash follows neither."""
    sites = []
    for i, ch in enumerate(text):
        if ch != " " or i == 0 or i + 1 >= len(text):
            continue
        previous, following = text[i - 1], text[i + 1]
        if previous in ",;:.!?—-(“‘\"'":
            continue
        if not (previous.isalnum() or previous in ")”’\"'"):
            continue
        if following in " —-":
            continue
        sites.append({"kind": "comma_insert", "pos": i, "end": i,
                      "variants": {"with_comma": ","}})
    return sites


def comma_delete_sites(text: str) -> list[dict]:
    """Every existing comma that sits between words, never one inside a number."""
    return [{"kind": "comma_delete", "pos": i, "end": i + 1, "variants": {"without_comma": ""}}
            for i, ch in enumerate(text)
            if ch == "," and 0 < i < len(text) - 1
            and not (text[i - 1].isdigit() and text[i + 1].isdigit())]


def spelling_sites(text: str, confusion: dict) -> list[dict]:
    """Every word whose lowercase form LanguageTool lists as confusable, with
    its alternates in the manuscript's own capitalization."""
    sites = []
    for match in _WORD.finditer(text):
        word = match.group()
        folded = word.lower().replace("’", "'")
        alternates = confusion.get(folded)
        if not alternates:
            continue
        variants = {}
        for alternate in sorted(alternates):
            written = alternate.capitalize() if word[0].isupper() else alternate
            if written != word:
                variants[written] = written
        if variants:
            sites.append({"kind": "spelling", "pos": match.start(), "end": match.end(),
                          "variants": variants})
    return sites


def generate_sites(kind: str, text: str, *, confusion: dict | None = None) -> list[dict]:
    if kind == "comma_insert":
        return comma_insert_sites(text)
    if kind == "comma_delete":
        return comma_delete_sites(text)
    if kind == "spelling":
        return spelling_sites(text, confusion or {})
    raise ValueError(f"Unknown Jev site kind {kind!r}")


def confusion_sets_path() -> Path:
    """LanguageTool's confusion list inside the pinned distribution."""
    from galley.local_runtime import DEFAULT_HOME
    home = os.environ.get("GALLEY_LANGUAGETOOL_HOME") or DEFAULT_HOME
    return Path(home) / CONFUSION_SETS


def confusion_map(path: Path | str | None = None) -> dict[str, set[str]]:
    """``{word: {alternates}}`` from LanguageTool's confusion_sets.txt, or {}
    when the pinned distribution is not installed."""
    path = Path(path) if path is not None else confusion_sets_path()
    if not path.is_file():
        return {}
    mapping: dict[str, set[str]] = {}
    for line in path.read_text("utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        words = [w.strip() for w in re.split(r";|->", line) if w.strip() and not w.strip().isdigit()]
        words = [w for w in words if re.fullmatch(r"[a-z']+", w)]
        for word in words:
            mapping.setdefault(word, set()).update(other for other in words if other != word)
    return mapping


# ---------------------------------------------------------------- windows and anchors

def sentence_around(text: str, pos: int) -> tuple[int, int]:
    """``[start, end)`` of the sentence holding ``pos`` — the whole paragraph
    while it is short enough to quote entire."""
    if len(text) <= JEV_SENTENCE_LIMIT:
        return 0, len(text)
    bounds = [0]
    for match in _SENT_END.finditer(text):
        if _ABBREV.search(text[:match.start()]):
            continue
        bounds.append(match.end())
    bounds.append(len(text))
    for start, end in zip(bounds, bounds[1:]):
        if start <= pos < end:
            return start, end
    return 0, len(text)


def anchor_span(text: str, pos: int, end: int) -> tuple[int, int]:
    """The word before the site through the word after it: a quotable anchor
    around a change that is often a single character."""
    low = pos
    while low > 0 and not text[low - 1].isspace():
        low -= 1
    high = end
    while high < len(text) and text[high].isspace():
        high += 1
    while high < len(text) and not text[high].isspace():
        high += 1
    return low, max(high, end)


def occurrence_of(text: str, quote: str, start: int) -> int:
    """Which occurrence of ``quote`` begins at ``start``, counted the way the
    workflow's anchoring counts them."""
    offset, count = -1, 0
    while True:
        offset = text.find(quote, offset + 1)
        if offset < 0 or offset > start:
            raise ValueError("A Jev site's quote does not occur at its own offset")
        count += 1
        if offset == start:
            return count


# ---------------------------------------------------------------- requests

def _question(kind: str, key: str, site: dict) -> dict:
    if kind in VARIANT:
        name = VARIANT[kind]
        criteria = ({"with_comma": "The comma is required; the original is missing it",
                     "original": "No comma belongs there; adding it would be an error",
                     "either": "The comma is optional; both readings are acceptable"}
                    if kind == "comma_insert" else
                    {"without_comma": "The comma is an error and must be removed",
                     "original": "The comma is correct and belongs there",
                     "either": "The comma is optional; both readings are acceptable"})
        return {"type": "choice",
                "instructions": f"Compare `variants.{key}.original` and `variants.{key}.{name}`, "
                                f"which differ by one comma. Under `house_rule`, which is "
                                f"correctly punctuated?",
                "criteria": criteria}
    names = ", ".join(f"`variants.{key}.{name}`" for name in site["variants"])
    criteria = {"original": "The original word is the one the writer intended"}
    criteria.update({name: f"The writer meant '{name}'" for name in site["variants"]})
    return {"type": "choice",
            "instructions": f"Compare `variants.{key}.original` with {names}; they differ only in "
                            f"one word. Under `house_rule`, which is correct?",
            "criteria": criteria}


def build_requests(kind: str, paragraphs, *, per_request: int = JEV_VARIANTS_PER_REQUEST):
    """``(jobs, index)`` for ``ledger.ask_many``: up to ``per_request`` variants
    of one paragraph share a request. ``paragraphs`` is ``[(para_id, text, sites)]``;
    ``index[i]`` names the ``(question_id, para_id, site)`` of request ``i``."""
    house_rule = JEV_HOUSE_RULES[kind]
    jobs, index = [], []
    for para_id, text, sites in paragraphs:
        for start in range(0, len(sites), per_request):
            chunk = sites[start:start + per_request]
            keys = [f"c{n + 1:02d}" for n in range(len(chunk))]
            variants, questions = {}, {}
            for key, site in zip(keys, chunk):
                low, high = sentence_around(text, site["pos"])
                variants[key] = {"original": text[low:high]}
                for name, replacement in site["variants"].items():
                    variants[key][name] = text[low:site["pos"]] + replacement + text[site["end"]:high]
                questions[key] = _question(kind, key, site)
            jobs.append(({"house_rule": house_rule, "paragraph": text, "variants": variants}, questions))
            index.append([(key, para_id, site) for key, site in zip(keys, chunk)])
    return jobs, index


# ---------------------------------------------------------------- answers to rows

def _winner(kind: str, site: dict, answer: dict, threshold: float):
    """``(variant name, probability)`` when the answer clears the threshold."""
    probabilities = answer.get("probabilities") or {}
    if kind in VARIANT:
        name = VARIANT[kind]
        probability = float(probabilities.get(name, 0.0))
        return (name, probability) if probability >= threshold else None
    alternates = {name: float(probability) for name, probability in probabilities.items()
                  if name in site["variants"]}
    if not alternates:
        return None
    best = max(alternates, key=lambda name: (alternates[name], name))
    return (best, alternates[best]) if alternates[best] >= threshold else None


_REASON = {
    "comma_insert": "Jev reads the house comma rule as requiring a comma at this boundary",
    "comma_delete": "Jev reads the house comma rule as forbidding the comma at this site",
    "spelling": "Jev reads the sentence as intending a different confusable word",
}


def rows_from_answers(kind: str, answers, index, texts, *, thresholds=None) -> list[dict]:
    """Candidate-input rows for the sites whose answers clear the threshold.

    ``answers[i]`` is the ``answers`` mapping of request ``i``; sites without an
    answer are simply not proposed."""
    threshold = (thresholds or JEV_THRESHOLDS)[kind]
    rows = []
    for answered, entries in zip(answers, index):
        for key, para_id, site in entries:
            answer = (answered or {}).get(key)
            if not answer or answer.get("type") != "choice":
                continue
            kept = _winner(kind, site, answer, threshold)
            if kept is None:
                continue
            name, probability = kept
            text = texts[para_id]
            low, high = anchor_span(text, site["pos"], site["end"])
            quote = text[low:high]
            replacement = text[low:site["pos"]] + site["variants"][name] + text[site["end"]:high]
            if replacement == quote:
                continue
            rows.append({
                "para_id": para_id, "quote": quote, "occurrence": occurrence_of(text, quote, low),
                "replacement": replacement, "category": CATEGORIES[kind],
                "reason": f"{_REASON[kind]} (Jev p={probability:.2f}).", "source": "jev",
                "jev": {"kind": kind, "variant": name, "probability": round(probability, 4),
                        "confidence": round(float(answer.get("confidence", 0.0)), 4),
                        "choice": answer.get("choice", ""), "threshold": threshold,
                        "start": site["pos"], "end": site["end"]}})
    return rows


# ---------------------------------------------------------------- the lane

def _rounded(usage: dict) -> dict:
    return {key: (round(value, 6) if isinstance(value, float) else value)
            for key, value in usage.items()}


def collect_jev_candidates(prepared, texts, directory, *, identity, poetry_ids, ledger,
                           should_cancel=None, progress=None):
    """Ask Jev about every site of every kind in the prose paragraphs.

    Returns ``(rows, evidence)``. ``rows`` are candidate inputs for the typed
    stage's ordinary ``_candidate`` conversion; nothing is applied here. The
    lane degrades instead of failing: a ``JevUnavailable`` mid-stage ends the
    asking and the rows already kept are returned with the reason in the
    evidence. ``prepared`` is accepted for symmetry with the other collectors —
    Jev reads the frozen paragraph texts and nothing else.

    Recorded usage is summed from the receipts under ``directory`` rather than
    from the live ledger counters, so a resumed run that replays its receipts
    records the same stage evidence as the run that paid for them.
    """
    from galley.jev import JevUnavailable, usage_from_receipts
    # The ledger owns where its receipts land; `directory` is the caller's
    # answer for a ledger that does not say.
    directory = Path(getattr(ledger, "directory", None) or directory)
    excluded = set(poetry_ids or ())
    prose = [(para_id, text) for para_id, text in texts.items()
             if para_id not in excluded and text.strip()]
    evidence = {"version": VERSION, "model": getattr(ledger, "model", ""),
                "identity_sha256": _identity_sha(identity), "paragraphs": len(prose),
                "thresholds": dict(JEV_THRESHOLDS), "generated": {}, "kept": {},
                "requests": {}, "notes": []}
    confusion = confusion_map()
    if not confusion:
        evidence["notes"].append(
            f"spelling sites skipped: no confusion sets at {confusion_sets_path()}")

    plans = {}
    for kind in KINDS:
        if kind == "spelling" and not confusion:
            evidence["generated"][kind] = 0
            evidence["requests"][kind] = 0
            evidence["kept"][kind] = 0
            continue
        paragraphs = []
        for para_id, text in prose:
            sites = generate_sites(kind, text, confusion=confusion)
            if sites:
                paragraphs.append((para_id, text, sites))
        jobs, index = build_requests(kind, paragraphs)
        plans[kind] = (jobs, index)
        evidence["generated"][kind] = sum(len(sites) for _, _, sites in paragraphs)
        evidence["requests"][kind] = len(jobs)
        evidence["kept"][kind] = 0

    total = sum(len(jobs) for jobs, _ in plans.values())
    completed = 0
    rows = []
    for kind in KINDS:
        jobs, index = plans.get(kind, ([], []))
        if not jobs:
            continue
        done_before = completed

        def step(done, _of, _before=done_before):
            if progress is not None:
                progress(_before + done, total)

        try:
            results = ledger.ask_many(STAGES[kind], jobs, should_cancel=should_cancel, progress=step)
        except JevUnavailable as exc:
            evidence["unavailable"] = str(exc)
            break
        completed += len(jobs)
        kept = rows_from_answers(kind, [result["answers"] for result in results], index, texts)
        evidence["kept"][kind] = len(kept)
        rows.extend(kept)
    receipts = usage_from_receipts(directory)
    evidence["usage"] = {"model": ledger.usage_summary().get("model", ""),
                         **_rounded(receipts["total"]),
                         "stages": {stage: _rounded(usage)
                                    for stage, usage in receipts["stages"].items()}}
    return rows, evidence


def _identity_sha(identity) -> str:
    from galley.fixed_calls import _hash
    return _hash(identity) if identity is not None else ""


__all__ = ["CATEGORIES", "KINDS", "STAGES", "VERSION", "anchor_span", "build_requests",
           "collect_jev_candidates", "comma_delete_sites", "comma_insert_sites",
           "confusion_map", "confusion_sets_path", "generate_sites", "occurrence_of",
           "rows_from_answers", "sentence_around", "spelling_sites"]
