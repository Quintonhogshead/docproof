"""Finished-text delivery gates: read the ACCEPTED manuscript for SENSE (P0-3).

`certify` proves integrity — source/config hashes, approved model routes, the
deterministic artifact regexes, the reject-all round-trip, spellfix sanity.
Nothing in it, or anywhere else in the pipeline, reads the finished manuscript
for MEANING. On the Purpura beta a LanguageTool lane spelled 35 coinages, brands,
and loanwords into unrelated real words (boop->book, crudité->erudite), applied
them as tracked edits, and passed every certify check; the corruption surfaced
only when an external model re-read the changes. These two gates are that
re-read, made a standard delivery stage:

  * ``verify_changes`` — the change verifier. Re-reads every APPLIED edit in its
    finished context and reports the ones that break meaning or grammar, damage
    voice, leave an artifact, or misapply a rule. (OPUS_VERIFY.md)
  * ``walk_finished_text`` — the finished-text walk. A residual-error proofread
    over the ACCEPTED text — the text the author ends up reading — not the source
    chunks the auditor samples, so it can see damage an applied edit did.
    (FABLE_QA.md)

Read-only over a finished run directory. The paid steps are the
``complete_structured`` calls; everything else — locating the deliverable, taking
its accept-all view, pairing each edit to its finished context, chunking,
parsing — is deterministic and unit-tested with a fake provider.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from docproof.models import Usage

log = logging.getLogger("galley.verify")

# A reply that truncates parses as empty and is recorded as a loss, so the
# ceilings are set well clear of the terse structured output they cap.
DEFAULT_MAX_TOKENS = 12000
# Applied edits per change-verify request, and the char budget of one
# finished-text-walk read. Kept modest so one truncation loses little.
DEFAULT_CHANGE_BATCH = 30
DEFAULT_WALK_CHARS = 6000
# Hard caps so a runaway reply can never blow a downstream budget.
MAX_PROBLEMS = 200
MAX_RESIDUALS = 200

_CHANGE_VERDICTS = (
    "breaks_meaning", "breaks_grammar", "voice_damage", "artifact", "wrong_rule")
_SEVERITIES = ("high", "medium", "low")


# ---- contracts ---------------------------------------------------------------

@dataclass(frozen=True)
class ChangeProblem:
    """One applied edit the change verifier judged a real problem."""

    para_id: str
    original_text: str
    corrected_text: str
    verdict: str
    detail: str
    fix: str

    def to_json(self) -> dict[str, Any]:
        return {"para_id": self.para_id, "original_text": self.original_text,
                "corrected_text": self.corrected_text, "verdict": self.verdict,
                "detail": self.detail, "fix": self.fix}


@dataclass(frozen=True)
class ResidualFinding:
    """One residual error the finished-text walk found in the accepted text."""

    para_id: str
    quote: str
    problem: str
    suggestion: str
    severity: str

    def to_json(self) -> dict[str, Any]:
        return {"para_id": self.para_id, "quote": self.quote,
                "problem": self.problem, "suggestion": self.suggestion,
                "severity": self.severity}


# ---- deterministic inputs (no model) -----------------------------------------

def deliverable_docx(run_dir: str | Path) -> Path | None:
    """The manuscript deliverable in a finished run dir, or None.

    The same selection `certify`'s text-hygiene check uses: a real `.docx` that
    is neither a Word lock file nor the change-log document (which quotes the
    pre-fix text on purpose)."""
    run = Path(run_dir)
    docs = sorted(p for p in run.glob("*.docx")
                  if not p.name.startswith("~$")
                  and "change log" not in p.name.lower())
    return docs[0] if docs else None


def accepted_text(run_dir: str | Path) -> dict[str, str]:
    """Every paragraph of the deliverable in its ACCEPT-all view, keyed by
    para_id — the text the author reads once the tracked changes are accepted.
    Empty when there is no deliverable or the OOXML tooling is unavailable (the
    caller reports that honestly rather than walking nothing as if it were
    clean)."""
    path = deliverable_docx(run_dir)
    if path is None:
        return {}
    try:
        from docproof.reassembler import paragraph_view_text
        from docproof.utils.xml_helpers import DocxPackage, walk_package
    except Exception as e:                          # pragma: no cover - lxml etc.
        log.warning("verify: OOXML tooling unavailable (%s); no accepted text", e)
        return {}
    pkg = DocxPackage(str(path))
    return {wp.para_id: paragraph_view_text(wp.element, "accept")
            for wp in walk_package(pkg)}


def applied_edits(run_dir: str | Path) -> list[dict[str, Any]]:
    """The tracked EDITS a finished run applied — the rows the change verifier
    re-reads. Reads findings.json and keeps rows that landed as a tracked change
    (validated / applied) and are not margin queries. A missing or unreadable
    findings.json yields an empty list."""
    path = Path(run_dir) / "findings.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = payload.get("findings", []) if isinstance(payload, dict) else []
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if r.get("force_query") or r.get("queried"):
            continue
        status = r.get("status")
        applied = r.get("applied")
        if status not in ("validated", "applied") and applied is not True:
            continue
        if not r.get("original_text") or not r.get("corrected_text"):
            continue
        if r.get("original_text") == r.get("corrected_text"):
            continue
        out.append(r)
    return out


def _sentence_around(text: str, needle: str) -> str:
    """The sentence of `text` that contains `needle` (its finished context), or
    the whole paragraph when the span cannot be located (an edit that a later
    edit further changed). Deterministic: first occurrence, sentence bounded by
    ., !, ? or the paragraph edges."""
    if not text:
        return needle
    idx = text.find(needle)
    if idx == -1:
        return text
    lo = 0
    for m in range(idx - 1, -1, -1):
        if text[m] in ".!?":
            lo = m + 1
            break
    hi = len(text)
    for m in range(idx + len(needle), len(text)):
        if text[m] in ".!?":
            hi = m + 1
            break
    return text[lo:hi].strip()


def _chunks(items: Sequence[Any], size: int) -> list[list[Any]]:
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def _walk_reads(accepted: dict[str, str], char_budget: int) -> list[list[tuple[str, str]]]:
    """Group (para_id, text) pairs into reads no larger than `char_budget`,
    keeping document order. A single paragraph over budget is its own read."""
    reads: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] = []
    used = 0
    for pid, text in accepted.items():
        if not text.strip():
            continue
        if cur and used + len(text) > char_budget:
            reads.append(cur)
            cur, used = [], 0
        cur.append((pid, text))
        used += len(text)
    if cur:
        reads.append(cur)
    return reads


# ---- schemas -----------------------------------------------------------------

def _change_schema() -> tuple[dict[str, Any], str]:
    from pydantic import BaseModel

    from docproof.providers import strict_json_schema

    class _Problem(BaseModel):
        index: int
        verdict: str
        detail: str
        fix: str

    class _Problems(BaseModel):
        problems: list[_Problem]

    return strict_json_schema(_Problems), "problems"


def _walk_schema() -> tuple[dict[str, Any], str]:
    from pydantic import BaseModel

    from docproof.providers import strict_json_schema

    class _Row(BaseModel):
        para_id: str
        quote: str
        problem: str
        suggestion: str
        severity: str

    class _Rows(BaseModel):
        findings: list[_Row]

    return strict_json_schema(_Rows), "findings"


# ---- prompts -----------------------------------------------------------------

_CHANGE_SYSTEM = """\
You are an adversarial change verifier on a finished book proofread. You are
shown edits that were APPLIED to the manuscript, each with the sentence AS IT
NOW READS after the change. Your only job is to catch a change that made the
book WORSE. Judge each edit and report ONLY the ones that are a real problem.

An edit is a problem when, in its finished context, it:
  - breaks_meaning: changes what the sentence says, drops a negation, or leaves
    it nonsensical;
  - breaks_grammar: leaves the sentence ungrammatical or misspelled;
  - voice_damage: overwrites a deliberate coinage, slang, brand, dialect, or
    stylized spelling with a "correct" word the author did not intend (boop->book,
    crudité->erudite, UGGs->Eggs);
  - artifact: leaves a doubled word, doubled/missing space, stray punctuation,
    or a half-applied edit;
  - wrong_rule: applies a rule that does not hold here (de-accenting a word the
    dictionary spells with the accent; closing a compound the author keeps open).

Standard: U.S. English, Chicago 17, Merriam-Webster, the house style and voice
notes below. The bar is a REAL problem — do not relitigate a defensible call, a
matter of taste, or a change that is simply one of two acceptable forms. When
unsure whether something is the author's voice, it is voice: leave it alone.

Return JSON {"problems": [...]}. For each PROBLEM only, give the 1-based `index`
of the edit, a `verdict` from exactly {breaks_meaning, breaks_grammar,
voice_damage, artifact, wrong_rule}, a one-sentence `detail`, and a `fix` (the
wording that should stand instead). A clean edit is omitted entirely. If every
edit is clean, return {"problems": []}."""

_WALK_SYSTEM = """\
You are giving a finished book its last proofread. You are shown the manuscript
AS THE AUTHOR WILL READ IT — every tracked change already accepted. Find the
residual mechanical errors that survived: real typos, wrong or missing or
doubled words, homophones, subject/verb or pronoun agreement, punctuation and
capitalization errors, and malformed edits (a sentence that no longer parses, a
doubled space, stray punctuation, a numbering or heading inconsistency).

This is mechanics only. Style, tense inside a flashback, sentence rhythm, and
VOICE are NOT errors — deliberate coinages, slang, brand names, profanity, and
intentional repetition are the author's and must never be flagged. When unsure
whether something is voice, it is voice. Standard: U.S. English, Chicago 17,
Merriam-Webster, the house style and voice notes below.

Return JSON {"findings": [...]}. Each row: the `para_id` it is in (from the
labels below), a verbatim minimal `quote` of the problem span, a one-sentence
`problem`, a `suggestion`, and a `severity` from {high, medium, low}. An empty
list is a valid, good result — return {"findings": []} if the text is clean."""


def _change_user(batch: list[dict[str, Any]], accepted: dict[str, str]) -> str:
    lines = []
    for n, e in enumerate(batch, 1):
        ctx = _sentence_around(accepted.get(e["para_id"], ""), e["corrected_text"])
        lines.append(
            f"{n}. rule: {e.get('error_type', '?')}\n"
            f"   was: {e['original_text']}\n"
            f"   now reads: {ctx}")
    return "\n\n".join(lines)


def _walk_user(read: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"[{pid}] {text}" for pid, text in read)


def _context_block(context: str) -> str:
    context = (context or "").strip()
    body = context if context else "No special voice notes were supplied."
    return f"\n\nHOUSE STYLE AND VOICE NOTES:\n{body}"


# ---- the gates ---------------------------------------------------------------

def verify_changes(edits: Sequence[dict[str, Any]], accepted: dict[str, str],
                   provider, model: str, usage: Usage, *,
                   context: str = "", batch_size: int = DEFAULT_CHANGE_BATCH,
                   max_tokens: int = DEFAULT_MAX_TOKENS) -> list[ChangeProblem]:
    """Re-read every applied edit in its finished context and return the ones
    that are a real problem. One `complete_structured` call per `batch_size`
    edits; a reply that did not come back clean is a loss (no problems), never a
    parse of a half-answer."""
    schema, schema_name = _change_schema()
    system = _CHANGE_SYSTEM + _context_block(context)
    problems: list[ChangeProblem] = []
    for batch in _chunks(list(edits), batch_size):
        result = provider.complete_structured(
            model=model, system=system, user=_change_user(batch, accepted),
            schema=schema, schema_name=schema_name, max_tokens=max_tokens)
        if result.usage is not None:
            usage.add(result.usage, model=model)
        if result.stop_reason != "ok":
            log.warning("verify_changes: reply not ok (stop_reason=%s) — %d "
                        "edit(s) unread this batch", result.stop_reason, len(batch))
            continue
        for row in (result.parsed or {}).get("problems", []):
            if not isinstance(row, dict):
                continue
            i = row.get("index")
            if not isinstance(i, int) or not (1 <= i <= len(batch)):
                continue
            verdict = row.get("verdict")
            if verdict not in _CHANGE_VERDICTS:
                verdict = "wrong_rule"
            e = batch[i - 1]
            problems.append(ChangeProblem(
                para_id=e["para_id"], original_text=e["original_text"],
                corrected_text=e["corrected_text"], verdict=verdict,
                detail=str(row.get("detail", "")), fix=str(row.get("fix", ""))))
            if len(problems) >= MAX_PROBLEMS:
                return problems
    return problems


def walk_finished_text(accepted: dict[str, str], provider, model: str,
                       usage: Usage, *, context: str = "",
                       char_budget: int = DEFAULT_WALK_CHARS,
                       max_tokens: int = DEFAULT_MAX_TOKENS) -> list[ResidualFinding]:
    """Proofread the accepted text for residual errors and return them. One
    `complete_structured` call per read (a `char_budget` slice of paragraphs);
    a non-clean reply is a loss for that read, not a parse of a truncation."""
    schema, schema_name = _walk_schema()
    system = _WALK_SYSTEM + _context_block(context)
    valid_ids = set(accepted)
    found: list[ResidualFinding] = []
    for read in _walk_reads(accepted, char_budget):
        result = provider.complete_structured(
            model=model, system=system, user=_walk_user(read),
            schema=schema, schema_name=schema_name, max_tokens=max_tokens)
        if result.usage is not None:
            usage.add(result.usage, model=model)
        if result.stop_reason != "ok":
            log.warning("walk_finished_text: reply not ok (stop_reason=%s) — a "
                        "read of %d paragraph(s) went unread", result.stop_reason,
                        len(read))
            continue
        for row in (result.parsed or {}).get("findings", []):
            if not isinstance(row, dict):
                continue
            pid = row.get("para_id")
            if pid not in valid_ids:       # a hallucinated id is not a finding
                continue
            sev = row.get("severity")
            if sev not in _SEVERITIES:
                sev = "medium"
            found.append(ResidualFinding(
                para_id=pid, quote=str(row.get("quote", "")),
                problem=str(row.get("problem", "")),
                suggestion=str(row.get("suggestion", "")), severity=sev))
            if len(found) >= MAX_RESIDUALS:
                return found
    return found


def verify_run(run_dir: str | Path, provider, model: str, usage: Usage, *,
               context: str = "", run_changes: bool = True,
               run_walk: bool = True, max_tokens: int = DEFAULT_MAX_TOKENS,
               ) -> tuple[list[ChangeProblem], list[ResidualFinding]]:
    """Both gates over a finished run dir. Returns (change problems, residuals).
    Reads the deliverable's accepted text and findings.json deterministically,
    then spends one model call per batch/read."""
    accepted = accepted_text(run_dir)
    problems: list[ChangeProblem] = []
    residuals: list[ResidualFinding] = []
    if run_changes:
        problems = verify_changes(applied_edits(run_dir), accepted, provider,
                                  model, usage, context=context,
                                  max_tokens=max_tokens)
    if run_walk:
        residuals = walk_finished_text(accepted, provider, model, usage,
                                       context=context, max_tokens=max_tokens)
    return problems, residuals


__all__ = [
    "ChangeProblem", "ResidualFinding", "accepted_text", "applied_edits",
    "deliverable_docx", "verify_changes", "walk_finished_text", "verify_run",
    "MAX_PROBLEMS", "MAX_RESIDUALS",
]
