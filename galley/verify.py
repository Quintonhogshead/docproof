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
from typing import Any, Mapping, Sequence

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
shown edits that were APPLIED to the manuscript. Each paragraph an edit sits in
is given ONCE, whole, in both views — BEFORE (as the author wrote it) and NOW
READS (after every tracked change was accepted) — and each edit names its
paragraph and its own before -> after span. Judge the edit inside the whole
paragraph: a span that looks cut off, or a quotation mark that looks stray, is
only a problem if the PARAGRAPH reads that way. Your only job is to catch a
change that made the book WORSE. Judge each edit and report ONLY the ones that
are a real problem.

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

Standard: U.S. English, Chicago 17, Merriam-Webster — and the HOUSE STYLE
below, which overrides Chicago wherever the two differ: an edit that installs a
house form is correct, and an edit into a house form is never `wrong_rule`. The
bar is a REAL problem — do not relitigate a defensible call, a matter of taste,
or a change that is simply one of two acceptable forms. When unsure whether
something is the author's voice, it is voice: leave it alone.

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
Merriam-Webster — and the HOUSE STYLE below, which overrides Chicago wherever
the two differ. Text already in a house form (“4:00 AM”, “40 percent”, an
unspaced em dash) is correct: never flag it and never suggest the Chicago form.

Return JSON {"findings": [...]}. Each row: the `para_id` it is in (from the
labels below), a verbatim minimal `quote` of the problem span, a one-sentence
`problem`, a `suggestion`, and a `severity` from {high, medium, low}. An empty
list is a valid, good result — return {"findings": []} if the text is clean."""


def _paragraph_before(after: str, edits: Sequence[dict[str, Any]]) -> str:
    """The paragraph as it read before its edits, composed from the accepted
    view by undoing each edit's own span — the fallback for a caller that has
    no reject-all view. Approximate (first occurrence), but a whole paragraph
    either way."""
    text = after
    for e in edits:
        corr = str(e.get("corrected_text", "") or "")
        orig = str(e.get("original_text", "") or "")
        if corr and corr in text:
            text = text.replace(corr, orig, 1)
    return text


def _change_user(batch: list[dict[str, Any]], accepted: dict[str, str],
                 original: dict[str, str] | None = None) -> str:
    """The change-verify packet: every paragraph the batch touches, ONCE, in
    both views — as ingested and as it now reads — then the edits, each naming
    its paragraph and its own before -> after span.

    WHOLE paragraphs, never a sentence slice. An earlier packet cut the
    accepted text at the nearest `.`, `!`, or `?` around the edit, so an edit
    containing a period or a quote mark — "$0.05", "Slow down!”" — reached the
    verifier as a fragment ending mid-amount or opening on a closing quote,
    and was reported as "truncated" / "stray closing quotation mark" (Georgis,
    2026-09-04). The paragraph is the unit the author reads; it is the unit
    the verifier judges."""
    original = original or {}
    order: list[str] = []
    by_para: dict[str, list[dict[str, Any]]] = {}
    for e in batch:
        pid = str(e.get("para_id", ""))
        if pid not in by_para:
            order.append(pid)
            by_para[pid] = []
        by_para[pid].append(e)
    paras: list[str] = []
    for pid in order:
        after = accepted.get(pid, "")
        before = original.get(pid) or (_paragraph_before(after, by_para[pid])
                                       if after else "")
        paras.append(f"[{pid}] BEFORE: {before or '(not available)'}\n"
                     f"[{pid}] NOW READS: {after or '(not available)'}")
    lines: list[str] = []
    for n, e in enumerate(batch, 1):
        lines.append(
            f"{n}. in [{e.get('para_id', '')}] rule: {e.get('error_type', '?')}\n"
            f"   edit: {e.get('original_text', '')!r} -> "
            f"{e.get('corrected_text', '')!r}")
    return ("PARAGRAPHS (whole, both views):\n\n" + "\n\n".join(paras)
            + "\n\nEDITS:\n\n" + "\n\n".join(lines))


def _walk_user(read: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"[{pid}] {text}" for pid, text in read)


def _context_block(context: str, role: str = "reader") -> str:
    """The shared house-rule block (galley/house_style.py — one constant for
    the walk, the verifier, and the settle judge) followed by the run's own
    voice notes. The house block comes first and unconditionally: it is what
    keeps a Chicago-trained reader from flagging “4:00 AM”."""
    from galley.house_style import house_rules_block
    context = (context or "").strip()
    body = context if context else "No special voice notes were supplied."
    return (f"\n\n{house_rules_block(role)}"
            f"\n\nVOICE NOTES FOR THIS BOOK:\n{body}")


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
# The change-verifier batches the last run could not read after their retry:
# [{"index": n, "para_ids": [...], "edits": k}]. Written into
# change_verify.json as `unread_batches`; certify refuses to pass a record
# with any (GALLEY-002: an unread batch is not a clean read).
UNREAD_BATCHES: list[dict[str, Any]] = []


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
                   original: dict[str, str] | None = None,
                   concurrency: int = 1) -> list[ChangeProblem]:
    """Re-read every applied edit in its finished context and return the ones
    that are a real problem. One `complete_structured` call per `batch_size`
    edits; a reply that did not come back clean is a loss (no problems), never a
    parse of a half-answer. `original` is the reject-all view (see
    :func:`paragraph_views`); with it, each paragraph's BEFORE view is the
    text as ingested rather than one recomposed from the accepted view.

    `concurrency` batches are in flight at once (`docproof.fanout.fan_out`);
    replies are folded in BATCH ORDER on this thread, so the problems list,
    the usage ledger, and the loss record read exactly as the sequential
    loop's did. On Georgis (2026-09-04) ~95 verify calls took ~5 minutes one
    at a time under an `api.concurrency` of 8."""
    from docproof.fanout import fan_out, fold_usage
    schema, schema_name = _change_schema()
    system = _CHANGE_SYSTEM + _context_block(context, "verifier")
    problems: list[ChangeProblem] = []
    edits = list(edits)
    batches = _chunks(list(range(len(edits))), batch_size)
    UNREAD_BATCHES.clear()

    def fetch(numbered):
        n, batch_idx = numbered
        batch = [edits[i] for i in batch_idx]
        user = _change_user(batch, accepted, original)
        # Progress for a headless run: each call is a model turn (a whole
        # subprocess on the subagent lane), and a 2,000-edit book is ~80 of
        # them in silence otherwise.
        log.info("change verifier: batch %d/%d (%d edit(s)) on %s", n,
                 len(batches), len(batch), model)
        local = Usage()                 # folded on the calling thread
        result = _ask_with_retry(provider, model=model, system=system,
                                 user=user, schema=schema,
                                 schema_name=schema_name, max_tokens=max_tokens,
                                 usage=local,
                                 what=f"change batch {n}/{len(batches)}")
        return result, local

    for (n, batch_idx), (result, local) in fan_out(
            list(enumerate(batches, 1)), fetch, concurrency=concurrency):
        batch = [edits[i] for i in batch_idx]
        fold_usage(usage, local)
        log.info("change verifier: batch %d/%d done — %d problem(s) so far",
                 n, len(batches), len(problems))
        if result.stop_reason != "ok":
            log.warning("verify_changes: reply not ok (stop_reason=%s%s) — %d "
                        "edit(s) unread this batch", result.stop_reason,
                        f": {result.error}" if result.error else "", len(batch))
            _LOSSES.append(("changes", result.stop_reason, result.error or ""))
            UNREAD_BATCHES.append({
                "index": n, "edits": len(batch),
                "para_ids": sorted({str(e.get("para_id", "")) for e in batch})})
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
                       max_tokens: int = DEFAULT_MAX_TOKENS,
                       concurrency: int = 1) -> list[ResidualFinding]:
    """Proofread the accepted text for residual errors and return them. One
    `complete_structured` call per read (a `char_budget` slice of paragraphs);
    a non-clean reply is a loss for that read, not a parse of a truncation.
    `concurrency` reads are in flight at once, folded in READ ORDER (see
    :func:`verify_changes`)."""
    from docproof.fanout import fan_out, fold_usage
    schema, schema_name = _walk_schema()
    system = _WALK_SYSTEM + _context_block(context, "proofreader")
    valid_ids = set(accepted)
    found: list[ResidualFinding] = []
    reads = _walk_reads(accepted, char_budget)
    unread_paragraphs: list[str] = []
    UNREAD.clear()

    def fetch(numbered):
        n, read = numbered
        log.info("finished-text walk: read %d/%d (%d paragraph(s), %s..%s) "
                 "on %s", n, len(reads), len(read), read[0][0], read[-1][0],
                 model)
        local = Usage()                 # folded on the calling thread
        result = _ask_with_retry(provider, model=model, system=system,
                                 user=_walk_user(read), schema=schema,
                                 schema_name=schema_name, max_tokens=max_tokens,
                                 usage=local, what=f"walk read {n}/{len(reads)}")
        return result, local

    for (n, read), (result, local) in fan_out(
            list(enumerate(reads, 1)), fetch, concurrency=concurrency):
        fold_usage(usage, local)
        log.info("finished-text walk: read %d/%d done — %d residual(s) so far",
                 n, len(reads), len(found))
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
               concurrency: int = 1) -> VerifyRunResult:
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
                                  original=original, concurrency=concurrency)
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
                                       context=context, max_tokens=max_tokens,
                                       concurrency=concurrency)
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
                 char_budget: int = DEFAULT_WALK_CHARS,
                 concurrency: int = 1) -> VerifyRunResult:
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
                                      original=orig, concurrency=concurrency)
    if run_walk and acc:
        residuals = walk_finished_text(acc, provider, model, usage,
                                       context=context, max_tokens=max_tokens,
                                       char_budget=char_budget,
                                       concurrency=concurrency)
    return VerifyRunResult(problems, residuals, ran_changes=run_changes,
                           ran_walk=run_walk)


def accepted_fingerprint(accepted: Mapping[str, str]) -> str:
    """One hash over the ACCEPTED text, paragraph by paragraph — the identity
    of "the document version that was verified". Comments and margin queries
    do not change it; any tracked edit does. Certify compares a verify
    artifact's recorded fingerprint with the deliverable's current one
    (GALLEY-002: document changes invalidate verification evidence)."""
    h = hashlib.sha256()
    for pid in sorted(accepted):
        h.update(pid.encode("utf-8"))
        h.update(b"\x1f")
        h.update((accepted[pid] or "").encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()


def paragraph_fingerprints(accepted: Mapping[str, str]) -> dict[str, str]:
    """sha256 per accepted paragraph, for the per-paragraph coverage record."""
    return {pid: hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]
            for pid, text in accepted.items()}


def build_fingerprints(run_dir: str | Path) -> dict[str, Any]:
    """The build identity a verify artifact is bound to: the deliverable's
    file hash and its accepted-text fingerprint, plus the per-paragraph
    fingerprints. Empty when there is no deliverable."""
    path = deliverable_docx(run_dir)
    if path is None:
        return {}
    from galley.manifest import sha256_file
    _orig, accepted = paragraph_views(run_dir)
    return {"build_sha256": sha256_file(path),
            "accepted_sha256": accepted_fingerprint(accepted),
            "paragraph_sha256": paragraph_fingerprints(accepted)}


def _load_artifact(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _merge_rows(old_rows: list, new_rows: list, para_ids: set[str],
                key: str) -> list:
    """Rows for paragraphs OUTSIDE the re-read keep their previous verdicts;
    rows for the re-read paragraphs are replaced by the new read's."""
    kept = [r for r in old_rows
            if isinstance(r, dict) and str(r.get(key, "")) not in para_ids]
    return kept + list(new_rows)


def write_artifacts(run_dir: str | Path, changes: VerifyRunResult,
                    walk: VerifyRunResult, *, model: str, engine: str,
                    usage_changes: Usage, usage_walk: Usage,
                    applied: int, paragraphs: int,
                    para_ids: Sequence[str] | None = None,
                    merge: bool = False) -> tuple[Path, Path]:
    """Write change_verify.json and finished_walk.json in the shape the CLI
    writes and certify reads (generated_at, ran/reason, cost, coverage, and
    the BUILD BINDING: `build_sha256` / `accepted_sha256` /
    `paragraph_sha256` of the deliverable the read was made against).

    `merge=True` is a partial re-read (`--paragraphs`, or settle's delta): the
    existing artifact's rows for paragraphs outside `para_ids` are kept, the
    re-read paragraphs' rows are replaced, `unread_*` shrink by what was read
    successfully and grow by what was lost, and `ran`/`reason` are NEVER
    upgraded by a read that read nothing (GALLEY-002: settlement cannot turn
    a failed read into a successful one). The binding is stamped to the
    current build only when nothing is left unread or unverified; otherwise
    the previous binding stands and certify says what is missing.

    Shared by the `galley verify` verb and the settle loop."""
    from datetime import datetime, timezone

    from docproof.contract import build_envelope
    now = datetime.now(timezone.utc).isoformat()
    run = Path(run_dir)
    run.mkdir(parents=True, exist_ok=True)
    cv_path = run / "change_verify.json"
    fw_path = run / "finished_walk.json"
    ids = {str(p) for p in (para_ids or [])}
    fp = build_fingerprints(run)
    old_cv = _load_artifact(cv_path) if merge else {}
    old_fw = _load_artifact(fw_path) if merge else {}

    # -- change verifier ------------------------------------------------------
    problems = [p.to_json() for p in changes.problems]
    unread_batches = list(UNREAD_BATCHES)
    if merge and old_cv:
        problems = _merge_rows(old_cv.get("problems") or [], problems, ids,
                               "para_id")
        # a previously unread batch whose paragraphs were all re-read is
        # covered now; a batch lost this time is added
        prior = [b for b in (old_cv.get("unread_batches") or [])
                 if isinstance(b, dict)
                 and not set(b.get("para_ids") or []) <= ids]
        unread_batches = prior + unread_batches
    ran_changes = changes.ran_changes or bool(merge and old_cv.get("ran"))
    reason_cv = changes.reason if changes.ran_changes else \
        (old_cv.get("reason") or changes.reason)
    if merge and old_cv and not changes.ran_changes and old_cv.get("ran"):
        # a delta that read nothing keeps the previous verdicts whole
        problems = old_cv.get("problems") or []
    cv = {**old_cv, "generated_at": now, "results_dir": str(run),
          "model": model, "engine": engine,
          "paragraphs_verified": sorted(ids) or None,
          "ran": ran_changes, "reason": reason_cv,
          "applied_edits": applied, "problems": problems,
          "unread_batches": unread_batches,
          "cost": build_envelope(findings=(), usage=usage_changes,
                                 fallback_model=model)["cost"]}
    cv.pop("settled", None)

    # -- finished-text walk ---------------------------------------------------
    residuals = [r.to_json() for r in walk.residuals]
    unread = list(UNREAD)
    if merge and old_fw:
        residuals = _merge_rows(old_fw.get("residuals") or [], residuals, ids,
                                "para_id")
        read_ok = ids - set(unread)
        prior_unread = [p for p in (old_fw.get("unread_paragraphs") or [])
                        if p not in read_ok]
        unread = sorted(set(prior_unread) | set(unread))
    ran_walk = walk.ran_walk or bool(merge and old_fw.get("ran"))
    reason_fw = walk.reason if walk.ran_walk else \
        (old_fw.get("reason") or walk.reason)
    if merge and old_fw and not walk.ran_walk and old_fw.get("ran"):
        residuals = old_fw.get("residuals") or []
    fw = {**old_fw, "generated_at": now, "results_dir": str(run),
          "model": model, "engine": engine,
          "paragraphs_verified": sorted(ids) or None,
          "ran": ran_walk, "reason": reason_fw,
          "paragraphs": paragraphs, "residuals": residuals,
          "unread_paragraphs": unread,
          "cost": build_envelope(findings=(), usage=usage_walk,
                                 fallback_model=model)["cost"]}
    fw.pop("settled", None)

    # -- the binding ----------------------------------------------------------
    for payload, complete in ((cv, ran_changes and not unread_batches),
                              (fw, ran_walk and not unread)):
        unverified = [p for p in (payload.get("unverified_paragraphs") or [])
                      if p not in ids] if merge else []
        payload["unverified_paragraphs"] = unverified
        if fp and complete and not unverified and (not merge or ids or
                                                   payload.get("ran")):
            # A full read, or a partial one that leaves nothing unread and
            # nothing unverified: the artifact now describes THIS build.
            if not merge or not payload.get("accepted_sha256") or ids:
                payload["build_sha256"] = fp["build_sha256"]
                payload["accepted_sha256"] = fp["accepted_sha256"]
        if fp:
            per = dict(payload.get("paragraph_sha256") or {}) if merge else {}
            read = ids if merge else set(fp["paragraph_sha256"])
            for pid in read:
                if pid in fp["paragraph_sha256"] and pid not in unread:
                    per[pid] = fp["paragraph_sha256"][pid]
            payload["paragraph_sha256"] = per
    cv_path.write_text(json.dumps(cv, indent=2, ensure_ascii=False),
                       encoding="utf-8")
    fw_path.write_text(json.dumps(fw, indent=2, ensure_ascii=False),
                       encoding="utf-8")
    return cv_path, fw_path


def mark_unverified(run_dir: str | Path, para_ids: Sequence[str]) -> None:
    """Record that `para_ids` changed AFTER their last read and were not read
    again — what a settle round leaves behind when it has no engine to
    re-verify with. Certify refuses to pass an artifact carrying any."""
    run = Path(run_dir)
    ids = sorted({str(p) for p in para_ids})
    if not ids:
        return
    for name in ("change_verify.json", "finished_walk.json"):
        path = run / name
        payload = _load_artifact(path)
        if not payload:
            continue
        have = set(payload.get("unverified_paragraphs") or [])
        payload["unverified_paragraphs"] = sorted(have | set(ids))
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                        encoding="utf-8")


__all__ = [
    "ChangeProblem", "ResidualFinding", "VerifyRunResult", "accepted_text",
    "write_artifacts",
    "problem_id", "residual_id", "verify_delta", "MAX_RESIDUALS_PER_READ",
    "UNREAD", "UNREAD_BATCHES", "accepted_fingerprint", "build_fingerprints",
    "paragraph_fingerprints", "mark_unverified",
    "applied_edits", "deliverable_docx", "paragraph_views", "verify_changes",
    "walk_finished_text", "verify_run", "MAX_PROBLEMS", "MAX_RESIDUALS",
]
