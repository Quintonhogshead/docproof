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

import hashlib
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
# Hard caps so a runaway reply can never blow a downstream budget. The
# residual caps are PER READ (a 6k-char window with more than 80 findings is a
# hallucinating reply) plus a book-wide ceiling far above any real count —
# the Redding walk hit an earlier global cap of 200 at read 36 of 42 and
# silently left six windows unread.
MAX_PROBLEMS = 200
MAX_RESIDUALS_PER_READ = 80
MAX_RESIDUALS = 5000

_CHANGE_VERDICTS = (
    "breaks_meaning", "breaks_grammar", "voice_damage", "artifact", "wrong_rule")
_SEVERITIES = ("high", "medium", "low")


# ---- contracts ---------------------------------------------------------------

def residual_id(para_id: str, quote: str, problem: str = "") -> str:
    """A stable id for one residual: the paragraph and the verbatim quote (the
    problem text is NOT part of it — two walks describing one residual
    differently must still be one item to settle). The settle loop keys its
    records on this, and certify matches records to residuals by it; an older
    finished_walk.json without ids gets the same id recomputed."""
    norm = " ".join((quote or "").split())
    h = hashlib.sha1(f"{para_id}\x00{norm}".encode("utf-8")).hexdigest()
    return f"r-{h[:10]}"


def problem_id(para_id: str, original_text: str, corrected_text: str) -> str:
    """A stable id for one change-verifier problem: the applied edit it is
    about (paragraph, original, corrected)."""
    h = hashlib.sha1(
        f"{para_id}\x00{original_text}\x00{corrected_text}".encode("utf-8")
    ).hexdigest()
    return f"c-{h[:10]}"


@dataclass(frozen=True)
class ChangeProblem:
    """One applied edit the change verifier judged a real problem."""

    para_id: str
    original_text: str
    corrected_text: str
    verdict: str
    detail: str
    fix: str

    @property
    def id(self) -> str:
        return problem_id(self.para_id, self.original_text, self.corrected_text)

    def to_json(self) -> dict[str, Any]:
        return {"problem_id": self.id, "para_id": self.para_id,
                "original_text": self.original_text,
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

    @property
    def id(self) -> str:
        return residual_id(self.para_id, self.quote)

    def to_json(self) -> dict[str, Any]:
        return {"residual_id": self.id, "para_id": self.para_id,
                "quote": self.quote, "problem": self.problem,
                "suggestion": self.suggestion, "severity": self.severity}


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


def paragraph_views(run_dir: str | Path) -> tuple[dict[str, str], dict[str, str]]:
    """Every paragraph of the deliverable in BOTH views, keyed by para_id:
    ``(original, accepted)`` — the REJECT-all view (the text as ingested, the
    space every finding's anchor offsets index into) and the ACCEPT-all view
    (the text the author reads once the tracked changes are accepted). Both
    empty when there is no deliverable or the OOXML tooling is unavailable (the
    caller reports that honestly rather than walking nothing as if it were
    clean)."""
    path = deliverable_docx(run_dir)
    if path is None:
        return {}, {}
    try:
        from docproof.reassembler import paragraph_view_text
        from docproof.utils.xml_helpers import DocxPackage, walk_package
    except Exception as e:                          # pragma: no cover - lxml etc.
        log.warning("verify: OOXML tooling unavailable (%s); no accepted text", e)
        return {}, {}
    pkg = DocxPackage(str(path))
    original: dict[str, str] = {}
    accepted: dict[str, str] = {}
    for wp in walk_package(pkg):
        original[wp.para_id] = paragraph_view_text(wp.element, "reject")
        accepted[wp.para_id] = paragraph_view_text(wp.element, "accept")
    return original, accepted


def accepted_text(run_dir: str | Path) -> dict[str, str]:
    """The ACCEPT-all view alone — see :func:`paragraph_views`."""
    return paragraph_views(run_dir)[1]


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
        # A pure deletion (corrected_text "") is an applied edit like any
        # other and is re-read too; only a row that changes nothing (a format
        # mark, or no text on either side) has no finished context to judge.
        if r.get("original_text", "") == r.get("corrected_text", ""):
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


def _sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """The ``[lo, hi)`` bounds of the sentence of `text` that spans the offsets
    ``start..end`` — bounded by ., !, ? or the paragraph edges, the same rule
    :func:`_sentence_around` applies, but located by OFFSET rather than by
    searching for a needle (a minimal-diff row whose corrected_text is "," or
    "" finds the wrong sentence, or none, by text)."""
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    lo = 0
    for m in range(start - 1, -1, -1):
        if text[m] in ".!?":
            lo = m + 1
            break
    hi = len(text)
    for m in range(end, len(text)):
        if text[m] in ".!?":
            hi = m + 1
            break
    return lo, hi


def _anchor_of(edit: dict[str, Any]) -> dict[str, Any] | None:
    """The row's validator anchor when it carries a usable one, else None."""
    a = edit.get("anchor")
    if (isinstance(a, dict) and isinstance(a.get("start"), int)
            and isinstance(a.get("end"), int) and a["start"] <= a["end"]):
        return a
    return None


def _accepted_starts(edits: Sequence[dict[str, Any]]) -> list[int | None]:
    """For each anchored edit, where its inserted text begins in the ACCEPTED
    paragraph: the anchor's start (an offset into the ORIGINAL text) shifted by
    the net length change of every anchored edit earlier in the same paragraph.
    Exact when the applied rows are the paragraph's every tracked change (they
    are — that is what :func:`applied_edits` selects); `_finished_context`
    re-checks the inserted text sits there and hunts nearby when it does not.
    None for an edit with no anchor."""
    out: list[int | None] = [None] * len(edits)
    by_para: dict[str, list[int]] = {}
    for i, e in enumerate(edits):
        if _anchor_of(e) is not None:
            by_para.setdefault(str(e.get("para_id", "")), []).append(i)
    for idxs in by_para.values():
        idxs.sort(key=lambda i: (edits[i]["anchor"]["start"],
                                 edits[i]["anchor"]["end"]))
        shift = 0
        for i in idxs:
            a = edits[i]["anchor"]
            out[i] = a["start"] + shift
            shift += len(str(a.get("insert_text") or "")) - (a["end"] - a["start"])
    return out


def _nearest(text: str, needle: str, pos: int) -> int | None:
    """The occurrence of `needle` in `text` closest to `pos`, or None."""
    best: int | None = None
    idx = text.find(needle)
    while idx != -1:
        if best is None or abs(idx - pos) < abs(best - pos):
            best = idx
        idx = text.find(needle, idx + 1)
    return best


def _finished_context(edit: dict[str, Any], acc_start: int | None,
                      original: dict[str, str], accepted: dict[str, str]
                      ) -> tuple[str, str]:
    """The ``(before, after)`` sentence pair for one applied edit: the sentence
    as it was ingested and the sentence as it now reads. Located by the row's
    anchor offsets — into the original text for `before`, into the accepted
    text (via `acc_start`) for `after` — so a minimal-diff row ("," or a pure
    deletion) lands on ITS sentence, not the first sentence that happens to
    contain a comma. Falls back to the needle search only for a row with no
    anchor, and to composing the after-sentence from the before-sentence when
    the accepted view cannot be read."""
    pid = str(edit.get("para_id", ""))
    orig_para = original.get(pid, "")
    acc_para = accepted.get(pid, "")
    a = _anchor_of(edit)
    if a is None or not orig_para:
        before = edit.get("original_text", "")
        after = _sentence_around(acc_para, edit.get("corrected_text", ""))
        return before, after
    lo, hi = _sentence_bounds(orig_para, a["start"], a["end"])
    before = orig_para[lo:hi].strip()
    ins = str(a.get("insert_text") or "")
    if acc_para and acc_start is not None:
        s: int | None = acc_start
        if ins and acc_para[acc_start:acc_start + len(ins)] != ins:
            # An untracked change shifted the paragraph; the inserted text is
            # still there, just not where the arithmetic put it.
            s = _nearest(acc_para, ins, acc_start)
        if s is not None:
            alo, ahi = _sentence_bounds(acc_para, s, s + len(ins))
            return before, acc_para[alo:ahi].strip()
    composed = (orig_para[lo:a["start"]] + ins + orig_para[a["end"]:hi]).strip()
    return before, composed


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
shown edits that were APPLIED to the manuscript, each with the sentence as it
was BEFORE the change and the sentence AS IT NOW READS after it. Your only job
is to catch a change that made the book WORSE. Judge each edit and report ONLY
the ones that are a real problem.

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


def _change_user(batch: list[dict[str, Any]], accepted: dict[str, str],
                 original: dict[str, str] | None = None,
                 acc_starts: Sequence[int | None] | None = None) -> str:
    lines = []
    for n, e in enumerate(batch, 1):
        acc_start = acc_starts[n - 1] if acc_starts is not None else None
        before, after = _finished_context(e, acc_start, original or {}, accepted)
        lines.append(
            f"{n}. rule: {e.get('error_type', '?')}\n"
            f"   edit: {e.get('original_text', '')!r} -> "
            f"{e.get('corrected_text', '')!r}\n"
            f"   before: {before}\n"
            f"   now reads: {after}")
    return "\n\n".join(lines)


def _walk_user(read: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"[{pid}] {text}" for pid, text in read)


def _context_block(context: str) -> str:
    context = (context or "").strip()
    body = context if context else "No special voice notes were supplied."
    return f"\n\nHOUSE STYLE AND VOICE NOTES:\n{body}"


# ---- the gates ---------------------------------------------------------------

# Every reply a gate could not use, in order: (gate, stop_reason, error).
# verify_run reads it to tell a gate that read NOTHING (every reply lost — a
# lane that cannot answer at all) from a gate that lost one batch: the first
# must never write a clean-looking artifact, which is exactly what a run of
# failed subagent turns would otherwise produce (0 residuals, ran: true).
_LOSSES: list[tuple[str, str, str]] = []


# Paragraph ids the last walk could not read (a lost reply after its retry,
# or the reads past the book-wide ceiling). The CLI writes them into
# finished_walk.json as `unread_paragraphs` so the next verb (a --paragraphs
# re-read, or settle) can see the hole instead of taking silence for clean.
UNREAD: list[str] = []


def _ask_with_retry(provider, *, model: str, system: str, user: str,
                    schema: dict[str, Any], schema_name: str, max_tokens: int,
                    usage: Usage, what: str):
    """One structured call, retried ONCE when the reply did not come back
    clean. A lost reply is usually transient (a truncated or malformed answer
    from the subagent lane, a dropped connection); the Redding walk lost six
    of 42 windows that way, and every one read fine on a second try."""
    result = provider.complete_structured(
        model=model, system=system, user=user, schema=schema,
        schema_name=schema_name, max_tokens=max_tokens)
    if result.usage is not None:
        usage.add(result.usage, model=model)
    if result.stop_reason == "ok":
        return result
    log.warning("%s: reply not ok (%s%s) — retrying once", what,
                result.stop_reason,
                f": {str(result.error)[:120]}" if result.error else "")
    retry = provider.complete_structured(
        model=model, system=system, user=user, schema=schema,
        schema_name=schema_name, max_tokens=max_tokens)
    if retry.usage is not None:
        usage.add(retry.usage, model=model)
    return retry


def _take_losses(gate: str) -> list[tuple[str, str, str]]:
    mine = [l for l in _LOSSES if l[0] == gate]
    _LOSSES[:] = [l for l in _LOSSES if l[0] != gate]
    return mine

def verify_changes(edits: Sequence[dict[str, Any]], accepted: dict[str, str],
                   provider, model: str, usage: Usage, *,
                   context: str = "", batch_size: int = DEFAULT_CHANGE_BATCH,
                   max_tokens: int = DEFAULT_MAX_TOKENS,
                   original: dict[str, str] | None = None) -> list[ChangeProblem]:
    """Re-read every applied edit in its finished context and return the ones
    that are a real problem. One `complete_structured` call per `batch_size`
    edits; a reply that did not come back clean is a loss (no problems), never a
    parse of a half-answer. `original` is the reject-all view (see
    :func:`paragraph_views`); with it, each edit's before/after sentence pair is
    located by its anchor offsets rather than by searching for its text."""
    schema, schema_name = _change_schema()
    system = _CHANGE_SYSTEM + _context_block(context)
    problems: list[ChangeProblem] = []
    edits = list(edits)
    acc_starts = _accepted_starts(edits)
    batches = _chunks(list(range(len(edits))), batch_size)
    for n, batch_idx in enumerate(batches, 1):
        batch = [edits[i] for i in batch_idx]
        user = _change_user(batch, accepted, original,
                            [acc_starts[i] for i in batch_idx])
        # Progress for a headless run: each call is a model turn (a whole
        # subprocess on the subagent lane), and a 2,000-edit book is ~80 of
        # them in silence otherwise.
        log.info("change verifier: batch %d/%d (%d edit(s)) on %s — %d "
                 "problem(s) so far", n, len(batches), len(batch), model,
                 len(problems))
        result = _ask_with_retry(provider, model=model, system=system,
                                 user=user, schema=schema,
                                 schema_name=schema_name, max_tokens=max_tokens,
                                 usage=usage,
                                 what=f"change batch {n}/{len(batches)}")
        if result.stop_reason != "ok":
            log.warning("verify_changes: reply not ok (stop_reason=%s%s) — %d "
                        "edit(s) unread this batch", result.stop_reason,
                        f": {result.error}" if result.error else "", len(batch))
            _LOSSES.append(("changes", result.stop_reason, result.error or ""))
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
    reads = _walk_reads(accepted, char_budget)
    unread_paragraphs: list[str] = []
    UNREAD.clear()
    for n, read in enumerate(reads, 1):
        log.info("finished-text walk: read %d/%d (%d paragraph(s), %s..%s) "
                 "on %s — %d residual(s) so far", n, len(reads), len(read),
                 read[0][0], read[-1][0], model, len(found))
        result = _ask_with_retry(provider, model=model, system=system,
                                 user=_walk_user(read), schema=schema,
                                 schema_name=schema_name, max_tokens=max_tokens,
                                 usage=usage, what=f"walk read {n}/{len(reads)}")
        if result.stop_reason != "ok":
            log.warning("walk_finished_text: reply not ok (stop_reason=%s%s) — a "
                        "read of %d paragraph(s) went unread", result.stop_reason,
                        f": {result.error}" if result.error else "", len(read))
            _LOSSES.append(("walk", result.stop_reason, result.error or ""))
            unread_paragraphs.extend(pid for pid, _t in read)
            continue
        rows = [r for r in (result.parsed or {}).get("findings", [])
                if isinstance(r, dict)]
        if len(rows) > MAX_RESIDUALS_PER_READ:
            log.warning("walk_finished_text: read %d/%d returned %d rows; "
                        "keeping the first %d (a runaway reply)", n,
                        len(reads), len(rows), MAX_RESIDUALS_PER_READ)
            rows = rows[:MAX_RESIDUALS_PER_READ]
        for row in rows:
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
                log.error("walk_finished_text: book-wide residual ceiling "
                          "(%d) reached at read %d/%d — the remaining reads "
                          "were NOT made", MAX_RESIDUALS, n, len(reads))
                unread_paragraphs.extend(
                    pid for rest in reads[n:] for pid, _t in rest)
                UNREAD.extend(unread_paragraphs)
                return found
    UNREAD.extend(unread_paragraphs)
    if unread_paragraphs:
        log.error("walk_finished_text: %d paragraph(s) in %d read(s) stayed "
                  "unread after a retry — re-run with --paragraphs on them",
                  len(unread_paragraphs), len(_LOSSES))
    return found


@dataclass
class VerifyRunResult:
    """What :func:`verify_run` did. ``ran_changes`` / ``ran_walk`` say whether
    each gate actually read anything — False when the caller switched it off
    AND when there was nothing to read (``reason`` says which); the CLI writes
    them into change_verify.json / finished_walk.json as ``ran`` so certify can
    tell a clean read from a gate that never ran. Unpacks as the
    ``(problems, residuals)`` pair it used to be, for callers that predate it.
    """

    problems: list[ChangeProblem]
    residuals: list[ResidualFinding]
    ran_changes: bool
    ran_walk: bool
    reason: str = ""

    def __iter__(self):
        yield self.problems
        yield self.residuals


def verify_run(run_dir: str | Path, provider, model: str, usage: Usage, *,
               context: str = "", run_changes: bool = True,
               run_walk: bool = True, max_tokens: int = DEFAULT_MAX_TOKENS,
               ) -> VerifyRunResult:
    """Both gates over a finished run dir. Reads the deliverable's two views and
    findings.json deterministically, then spends one model call per batch/read.
    With NO accepted text — no deliverable, or the OOXML tooling is missing —
    neither gate runs: the result says so (``ran_* = False`` plus a reason)
    rather than walking nothing and reporting it clean."""
    original, accepted = paragraph_views(run_dir)
    problems: list[ChangeProblem] = []
    residuals: list[ResidualFinding] = []
    if not accepted:
        reason = ("no accepted text could be read from the deliverable (no "
                  "manuscript .docx, or the OOXML tooling is unavailable) — "
                  "neither gate ran")
        log.warning("verify_run: %s", reason)
        return VerifyRunResult(problems, residuals, ran_changes=False,
                               ran_walk=False, reason=reason)
    ran_changes, ran_walk, reason = run_changes, run_walk, ""
    if run_changes:
        edits = applied_edits(run_dir)
        _take_losses("changes")
        problems = verify_changes(edits, accepted, provider, model, usage,
                                  context=context, max_tokens=max_tokens,
                                  original=original)
        lost = _take_losses("changes")
        n_calls = -(-len(edits) // DEFAULT_CHANGE_BATCH) if edits else 0
        if n_calls and len(lost) >= n_calls:
            ran_changes = False
            reason = (f"change verifier: every one of {n_calls} read(s) "
                      f"failed ({lost[0][1]}: {lost[0][2][:200]})")
            log.error("verify_run: %s", reason)
    if run_walk:
        _take_losses("walk")
        residuals = walk_finished_text(accepted, provider, model, usage,
                                       context=context, max_tokens=max_tokens)
        lost = _take_losses("walk")
        n_calls = len(_walk_reads(accepted, DEFAULT_WALK_CHARS))
        if n_calls and len(lost) >= n_calls:
            ran_walk = False
            reason = (reason + "; " if reason else "") + (
                f"finished-text walk: every one of {n_calls} read(s) failed "
                f"({lost[0][1]}: {lost[0][2][:200]})")
            log.error("verify_run: %s", reason)
    return VerifyRunResult(problems, residuals, ran_changes=ran_changes,
                           ran_walk=ran_walk, reason=reason)


def verify_delta(run_dir: str | Path, para_ids: Sequence[str], provider,
                 model: str, usage: Usage, *, context: str = "",
                 run_changes: bool = True, run_walk: bool = True,
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 char_budget: int = DEFAULT_WALK_CHARS) -> VerifyRunResult:
    """Both gates over ONLY the named paragraphs of a run — what the settle
    loop re-reads after a round touched them. Same packets and prompts as the
    full run (an applied edit in its finished context; the accepted text of
    the paragraphs), so a delta verdict is comparable to a full one. Reads
    nothing when the paragraph set is empty."""
    wanted = {str(p) for p in para_ids}
    original, accepted = paragraph_views(run_dir)
    if not accepted:
        reason = ("no accepted text could be read from the deliverable — "
                  "neither gate ran")
        return VerifyRunResult([], [], ran_changes=False, ran_walk=False,
                               reason=reason)
    acc = {pid: t for pid, t in accepted.items() if pid in wanted}
    orig = {pid: t for pid, t in original.items() if pid in wanted}
    problems: list[ChangeProblem] = []
    residuals: list[ResidualFinding] = []
    if run_changes:
        edits = [e for e in applied_edits(run_dir)
                 if str(e.get("para_id", "")) in wanted]
        if edits:
            problems = verify_changes(edits, acc, provider, model, usage,
                                      context=context, max_tokens=max_tokens,
                                      original=orig)
    if run_walk and acc:
        residuals = walk_finished_text(acc, provider, model, usage,
                                       context=context, max_tokens=max_tokens,
                                       char_budget=char_budget)
    return VerifyRunResult(problems, residuals, ran_changes=run_changes,
                           ran_walk=run_walk)


def write_artifacts(run_dir: str | Path, changes: VerifyRunResult,
                    walk: VerifyRunResult, *, model: str, engine: str,
                    usage_changes: Usage, usage_walk: Usage,
                    applied: int, paragraphs: int,
                    para_ids: Sequence[str] | None = None) -> tuple[Path, Path]:
    """Write change_verify.json and finished_walk.json in the shape the CLI
    writes and certify reads (generated_at, ran/reason, cost). Shared by the
    `galley verify` verb and the settle loop's fresh sweep."""
    from datetime import datetime, timezone

    from docproof.contract import build_envelope
    now = datetime.now(timezone.utc).isoformat()
    run = Path(run_dir)
    run.mkdir(parents=True, exist_ok=True)
    cv = {"generated_at": now, "results_dir": str(run), "model": model,
          "engine": engine, "paragraphs_verified": list(para_ids or []) or None,
          "ran": changes.ran_changes, "reason": changes.reason,
          "applied_edits": applied,
          "problems": [p.to_json() for p in changes.problems],
          "cost": build_envelope(findings=(), usage=usage_changes,
                                 fallback_model=model)["cost"]}
    fw = {"generated_at": now, "results_dir": str(run), "model": model,
          "engine": engine, "paragraphs_verified": list(para_ids or []) or None,
          "ran": walk.ran_walk, "reason": walk.reason,
          "paragraphs": paragraphs,
          "residuals": [r.to_json() for r in walk.residuals],
          "unread_paragraphs": list(UNREAD),
          "cost": build_envelope(findings=(), usage=usage_walk,
                                 fallback_model=model)["cost"]}
    cv_path = run / "change_verify.json"
    fw_path = run / "finished_walk.json"
    cv_path.write_text(json.dumps(cv, indent=2, ensure_ascii=False),
                       encoding="utf-8")
    fw_path.write_text(json.dumps(fw, indent=2, ensure_ascii=False),
                       encoding="utf-8")
    return cv_path, fw_path


__all__ = [
    "ChangeProblem", "ResidualFinding", "VerifyRunResult", "accepted_text",
    "write_artifacts",
    "problem_id", "residual_id", "verify_delta", "MAX_RESIDUALS_PER_READ",
    "UNREAD",
    "applied_edits", "deliverable_docx", "paragraph_views", "verify_changes",
    "walk_finished_text", "verify_run", "MAX_PROBLEMS", "MAX_RESIDUALS",
]
