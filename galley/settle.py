"""Residual settlement — Galley finishes with ZERO open candidates.

Every finding a lane ever raised ends in exactly one terminal state: an
APPLIED tracked edit, a DROPPED row with a recorded reason, or an author QUERY.
No "candidate slips" list, no human hour, no certify check left failing for
open items. This module is the loop that gets a finished run there.

Where the open items come from. `galley verify` reads the ACCEPTED manuscript
— the text the author will read — and raises two kinds: RESIDUALS the
finished-text walk still finds (a typo that survived, an artifact an edit
left) and EDIT DAMAGE the change verifier proved (an applied edit that broke
meaning, grammar, or voice). On the Redding run (2026-09-01) 79 residuals
could not be settled by hand because they sat inside spans other tracked
edits already owned; settling them meant hand-editing the owning rows, which
leaked editor notes and overran the edit guard. So this loop settles them
THROUGH THE ENGINE:

  A1  the accepted view + EDIT MAP come from the build itself (docproof/
      editmap.py) — the offsets finish() edited at, not a text-matching
      reconstruction;
  A2  each residual is located in the accepted paragraph and translated back
      to a SOURCE span: untouched text (a new row), inside one applied edit's
      replacement (revise that owner), or straddling a boundary (widen to
      absorb every owner touched, whole — a row split into minimal regions is
      one decision);
  A3  deterministic decisions first — duplicate, intent zone, editorial note,
      fact change (I4: a number/name/title/date/quoted line changes -> QUERY,
      never an edit), pure space deletion, unanchorable — and only then a
      NARROW model call (absorb | add | drop | query) that never edits text
      outside the owner's span;
  A4  absorb = the owner's replacement is REVISED as a composite (I2: one
      owner per span; the composite rides the same tracked-change machinery,
      so reject-all stays byte-identical, I6); add = a new row on an unowned
      span; the deliverable is rebuilt at $0 through the same replay path
      import-findings uses;
  A5  the change verifier and the walk re-read ONLY the paragraphs the round
      touched; a verifier flag on a composite REVERTS it to the owner's
      previous replacement and turns the residual into a query;
  A6  rounds are bounded (I5); anything still open after the last becomes a
      query carrying the walker's suggestion — the book ships with QUESTIONS,
      not a to-do list — and settlement.json records every decision;
  A7  certify then requires every residual and every problem to carry a
      settlement record (galley/manifest.py) before the run can ship.

Model doctrine holds here as everywhere: the narrow judge and the delta
verifier default to the $0 subscription lane (docproof/providers/subagent.py)
when this machine can run it, a configured API model otherwise, and a
deterministic-only mode (`engine="none"`) that queries whatever it cannot
decide. A headless Galley (DocWatch-triggered, nobody at a keyboard) gets the
same loop, with the same power, as a practitioner session.
"""
from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from docproof import editmap as emap
from docproof.models import Usage

log = logging.getLogger("galley.settle")

SETTLEMENT_NAME = "settlement.json"
SETTLEMENT_SCHEMA_VERSION = 1
SETTLE_TYPE = "galley_settle"
DEFAULT_ROUNDS = 3
# A composite may not grow the region it revises past this: the settlement
# changes a word, not the sentence around it. Beyond it the model rewrote.
MAX_GROWTH = 1.5
MAX_GROWTH_SLACK = 20

ACTIONS = ("absorb", "add", "drop", "query", "revise", "revert")
TERMINAL_STATES = ("applied", "dropped", "query")
NON_TERMINAL = ("pending", "held", "residual", "flagged", "proposed",
                "screened")

REASON_PREFIXES = (
    "duplicate", "overlap_loser", "voice", "intent_zone", "style_only", "fact",
    "unanchorable", "walker_wrong", "unresolved_after_", "ambiguous_anchor",
    "editorial_note", "verifier_reverted", "oversize", "unplaced",
    "space_deletion", "rejected_", "no_suggestion", "unmapped", "edit_damage")

_SPACE_ONLY = re.compile(r"\s+")
_PUNCT_ONLY = re.compile(r"[^\w\s]")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


SETTLE_CHUNK_PREFIX = "settle:"


def settle_residual_of(row: Mapping[str, Any]) -> str | None:
    """The residual id a settlement row carries. It rides in `chunk_id`
    (`settle:<residual_id>`) because that is the one free-form field a
    Finding keeps through replay; an extra key on a row is dropped at
    build_findings."""
    cid = str(row.get("chunk_id") or "")
    if cid.startswith(SETTLE_CHUNK_PREFIX):
        return cid[len(SETTLE_CHUNK_PREFIX):]
    return None


# ---- contracts ---------------------------------------------------------------

@dataclass
class Residual:
    """One open item: a walk residual (`kind="residual"`) or a change-verifier
    problem (`kind="edit_damage"`). Resolution fields are filled by
    `resolve`."""

    id: str
    kind: str
    para_id: str
    quote: str
    problem: str
    suggestion: str
    severity: str = "medium"
    verdict: str = ""                       # edit_damage: the verifier's verdict
    owner_original: str = ""                # edit_damage: the flagged edit
    owner_corrected: str = ""
    round_seen: int = 0
    # resolution
    source_span: tuple[int, int] | None = None
    owner_finding_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_walk(cls, row: Mapping[str, Any], round_seen: int = 0) -> "Residual":
        from galley.verify import residual_id
        pid = str(row.get("para_id", ""))
        quote = str(row.get("quote", ""))
        rid = str(row.get("residual_id") or residual_id(pid, quote))
        sev = str(row.get("severity") or "medium")
        return cls(id=rid, kind="residual", para_id=pid, quote=quote,
                   problem=str(row.get("problem", "")),
                   suggestion=str(row.get("suggestion", "")), severity=sev,
                   round_seen=round_seen, raw=dict(row))

    @classmethod
    def from_problem(cls, row: Mapping[str, Any], round_seen: int = 0
                     ) -> "Residual":
        from galley.verify import problem_id
        pid = str(row.get("para_id", ""))
        orig = str(row.get("original_text", ""))
        corr = str(row.get("corrected_text", ""))
        rid = str(row.get("problem_id") or problem_id(pid, orig, corr))
        return cls(id=rid, kind="edit_damage", para_id=pid, quote=corr,
                   problem=str(row.get("detail", "")),
                   suggestion=str(row.get("fix", "")), severity="high",
                   verdict=str(row.get("verdict", "")), owner_original=orig,
                   owner_corrected=corr, round_seen=round_seen, raw=dict(row))

    def to_json(self) -> dict[str, Any]:
        return {"residual_id": self.id, "kind": self.kind,
                "para_id": self.para_id, "quote": self.quote,
                "problem": self.problem, "suggestion": self.suggestion,
                "severity": self.severity, "verdict": self.verdict,
                "source_span": list(self.source_span) if self.source_span
                else None, "owner_finding_id": self.owner_finding_id,
                "round_seen": self.round_seen}


@dataclass
class SettlementRecord:
    residual_id: str
    round: int
    action: str
    owner_finding_id: str | None
    before_replacement: str
    after_replacement: str
    reason: str
    verified_by: str
    para_id: str = ""
    question: str = ""
    kind: str = "residual"

    def to_json(self) -> dict[str, Any]:
        return {"residual_id": self.residual_id, "round": self.round,
                "action": self.action,
                "owner_finding_id": self.owner_finding_id,
                "before_replacement": self.before_replacement,
                "after_replacement": self.after_replacement,
                "reason": self.reason, "verified_by": self.verified_by,
                "para_id": self.para_id, "question": self.question,
                "kind": self.kind}

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "SettlementRecord":
        return cls(residual_id=str(d.get("residual_id", "")),
                   round=int(d.get("round", 0) or 0),
                   action=str(d.get("action", "")),
                   owner_finding_id=d.get("owner_finding_id"),
                   before_replacement=str(d.get("before_replacement", "")),
                   after_replacement=str(d.get("after_replacement", "")),
                   reason=str(d.get("reason", "")),
                   verified_by=str(d.get("verified_by", "")),
                   para_id=str(d.get("para_id", "")),
                   question=str(d.get("question", "")),
                   kind=str(d.get("kind", "residual")))


@dataclass
class Settlement:
    """settlement.json: every SettlementRecord, the items still open (empty
    when the loop converged or was closed out as queries), and the loop's own
    accounting."""

    run_dir: str = ""
    rounds: int = 0
    max_rounds: int = DEFAULT_ROUNDS
    engine: str = "none"
    model: str = ""
    records: list[SettlementRecord] = field(default_factory=list)
    open: list[dict[str, Any]] = field(default_factory=list)
    residuals_seen: list[dict[str, Any]] = field(default_factory=list)
    cost: dict[str, Any] = field(default_factory=dict)
    generated_at: str = ""
    notes: list[str] = field(default_factory=list)
    # How the loop ended: {"stopped": clean|quiet|turn_budget|round_cap|
    # rounds, "last_new_items": n, "last_reread": m, "rounds": r}. The
    # outcome reads it: a sweep that was still finding a pile of new errors
    # when it had to stop is a book for a human proofreader.
    convergence: dict[str, Any] = field(default_factory=dict)

    def record_ids(self) -> set[str]:
        return {r.residual_id for r in self.records}

    def latest(self) -> dict[str, SettlementRecord]:
        """The last record per residual (a revert supersedes the absorb)."""
        out: dict[str, SettlementRecord] = {}
        for r in self.records:
            out[r.residual_id] = r
        return out

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for r in self.latest().values():
            c[r.action] = c.get(r.action, 0) + 1
        return c

    def to_json(self) -> dict[str, Any]:
        return {"schema_version": SETTLEMENT_SCHEMA_VERSION,
                "generated_at": self.generated_at or _now(),
                "run_dir": self.run_dir, "rounds": self.rounds,
                "max_rounds": self.max_rounds, "engine": self.engine,
                "model": self.model, "counts": self.counts(),
                "records": [r.to_json() for r in self.records],
                "open": list(self.open),
                "residuals_seen": list(self.residuals_seen),
                "cost": dict(self.cost), "notes": list(self.notes),
                "convergence": dict(self.convergence)}

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "Settlement":
        s = cls(run_dir=str(d.get("run_dir", "")),
                rounds=int(d.get("rounds", 0) or 0),
                max_rounds=int(d.get("max_rounds", DEFAULT_ROUNDS) or 0),
                engine=str(d.get("engine", "none")),
                model=str(d.get("model", "")),
                records=[SettlementRecord.from_json(r)
                         for r in d.get("records") or []],
                open=list(d.get("open") or []),
                residuals_seen=list(d.get("residuals_seen") or []),
                cost=dict(d.get("cost") or {}),
                generated_at=str(d.get("generated_at", "")),
                notes=list(d.get("notes") or []))
        s.convergence = dict(d.get("convergence") or {})
        return s

    def save(self, run_dir: str | Path) -> Path:
        self.generated_at = _now()
        p = Path(run_dir) / SETTLEMENT_NAME
        p.write_text(json.dumps(self.to_json(), indent=1, ensure_ascii=False),
                     encoding="utf-8")
        return p

    @classmethod
    def load(cls, run_dir: str | Path) -> "Settlement | None":
        p = Path(run_dir) / SETTLEMENT_NAME
        if not p.is_file():
            return None
        try:
            return cls.from_json(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None


# ---- the working set: rows that reproduce the deliverable -----------------------

def load_envelope(run_dir: str | Path) -> dict[str, Any]:
    p = Path(run_dir) / "findings.json"
    return json.loads(p.read_text(encoding="utf-8"))


def kept_rows(rows: Sequence[Mapping[str, Any]]
              ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """The rows that REPRODUCE a build's deliverable — applied edits, applied
    format marks, and queries that reached the margin — one per decision
    (minimal-region siblings collapse onto their first row), keyed by that
    row's finding_id. Also returns finding_id -> key id for every row, so an
    absorbed sub-row resolves to the working row it belongs to."""
    owner_of = emap.owners_index(rows)
    working: dict[str, dict[str, Any]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        fid = str(r.get("finding_id") or "")
        key = owner_of.get(fid, fid)
        if key in working:
            continue
        status = str(r.get("status") or "")
        keep = False
        if r.get("applied") is True or (r.get("applied") is None
                                        and status == "validated"):
            keep = True
        elif status == "query" and (r.get("queried") is True
                                    or r.get("queried") is None):
            keep = True
        if not keep:
            continue
        row = {k: v for k, v in r.items()
               if k not in ("status", "anchor", "applied", "queried",
                            "unplaced", "state", "disposition_reason",
                            "absorbed", "owner_of")}
        if status == "query":
            row["force_query"] = True
            row["queried"] = True
        working[key] = row
    return working, owner_of


def terminal_state(row: Mapping[str, Any]) -> tuple[str, str]:
    """(state, reason) for a findings.json row: `applied`, `dropped`, or
    `query` — the only terminal states — derived from the reporter's flags,
    or the row's own `state` when the settle loop stamped one. `pending`
    (never validated) is the one non-terminal answer and means the build never
    ruled on the row."""
    state = str(row.get("state") or "")
    if state in TERMINAL_STATES:
        return state, str(row.get("disposition_reason") or "")
    if state in NON_TERMINAL:
        return state, str(row.get("disposition_reason") or "")
    status = str(row.get("status") or "")
    if row.get("applied") is True:
        return "applied", ""
    if status == "query" or row.get("force_query") or row.get("queried"):
        if row.get("queried") is False and row.get("applied") is False \
                and status == "query":
            return "dropped", "unplaced"
        return "query", "withheld" if row.get("withheld") else ""
    if status == "validated":
        # validated but never written: the reassembler could not place it
        return ("applied", "") if row.get("applied") is None \
            else ("dropped", "unplaced")
    if status.startswith(("rejected_", "skipped_")):
        return "dropped", status
    if status == "pending":
        return "pending", "never validated"
    if not status:
        # Not a lifecycle state at all: a row that carries no status was never
        # put through the validator (a hand-built or foreign envelope).
        return "unknown", "no status recorded"
    return "dropped", status


def open_items(run_dir: str | Path) -> list[Residual]:
    """Every residual and problem the run's verify artifacts hold that has no
    settlement record — what `galley settle` will face, what `galley
    residuals` lists, and what certify counts as unsettled."""
    run = Path(run_dir)
    settled = Settlement.load(run)
    have = settled.record_ids() if settled else set()
    out: list[Residual] = []
    seen: set[str] = set()
    for name, kind in (("finished_walk.json", "residual"),
                       ("change_verify.json", "edit_damage")):
        try:
            payload = json.loads((run / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows = payload.get("residuals" if kind == "residual" else "problems") \
            or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = Residual.from_walk(row) if kind == "residual" \
                else Residual.from_problem(row)
            if item.id in have or item.id in seen:
                continue
            seen.add(item.id)
            out.append(item)
    return out


# ---- deterministic decisions (A3, first half) ----------------------------------

@dataclass
class Decision:
    action: str                      # absorb | add | drop | query | revise | revert | judge
    reason: str = ""
    replacement: str = ""            # for absorb/add/revise: the text to apply
    question: str = ""               # for query
    composite: emap.Composite | None = None
    owner_key: str | None = None


# A parenthetical or dash-led tail that TALKS ABOUT typography instead of
# being text: "(with Rocky in italics)", "— set in small caps". The flight
# deck's filter keys on a few leading words; a walker's asides are freer.
_TYPO_NOTE_RE = re.compile(
    r"\s*\((?=[^)]*\b(?:italic|italics|roman|bold|small caps|capitali[sz]e|"
    r"lowercase|uppercase|hyphenat|spell(?:ed|ing)? out|delete|remove|insert|"
    r"comma|period|semicolon|colon|quotation|en dash|em dash|ellipsis|"
    r"should be|needs?|sic)\b)[^)]*\)", re.IGNORECASE)


def strip_note(text: str, quote: str = "") -> tuple[str, bool]:
    """Remove an editorial aside from a suggestion: the flight deck's filter
    first, then any parenthetical about typography that the quoted text
    itself did not contain (an author's own parenthetical survives because it
    is in the quote too)."""
    from docproof.flights import strip_editorial_note
    cleaned, had = strip_editorial_note(text)
    for m in list(_TYPO_NOTE_RE.finditer(cleaned)):
        if m.group(0).strip() and m.group(0).strip() not in quote:
            cleaned = cleaned.replace(m.group(0), "", 1)
            had = True
    return (cleaned.strip() if had else cleaned), had


_INSTRUCTION_RE = re.compile(
    r"^\s*(?:delete|remove|insert|add|change|replace|set|use|capitalize|"
    r"lowercase|hyphenate|spell|close|drop|make|move|put|rewrite|consider|"
    r"should|needs?|keep|restore|fix|correct|omit|italicize|un-?italicize)\b",
    re.IGNORECASE)


def looks_like_instruction(suggestion: str, quote: str) -> bool:
    """A "suggestion" that is an instruction to an editor ("Delete the
    trailing whitespace.", "Use a semicolon: ...") rather than replacement
    text. Applied verbatim it becomes the manuscript's text — the Redding
    trial wrote "Mindset Number 1: Delete the trailing whitespace." That
    shape routes to the judge (or to a query), never to compose."""
    if not suggestion.strip():
        return False
    if _INSTRUCTION_RE.match(suggestion) and not _INSTRUCTION_RE.match(quote):
        return True
    return False


def align_to_sentence(acc: str, lo: int, hi: int, suggestion: str
                      ) -> tuple[int, int]:
    """When the walker quoted a fragment but suggested a rewrite of the WHOLE
    sentence (its suggestion shares most of the sentence's words and reaches
    well past the quote), widen [lo, hi) to the sentence so the composite
    replaces the sentence rather than splicing a second copy of it into the
    fragment. Otherwise the range is returned unchanged."""
    if len(suggestion) <= 1.5 * max(len(acc[lo:hi]), 1) + 10:
        return lo, hi
    slo, shi = emap.sentence_bounds(acc, lo, hi)
    while slo < shi and acc[slo].isspace():
        slo += 1
    sentence = acc[slo:shi]
    if not sentence or (slo, shi) == (lo, hi):
        return lo, hi
    from docproof.flights import word_retention
    if (word_retention(sentence, suggestion) >= 0.6
            and len(suggestion) >= 0.6 * len(sentence)):
        return slo, shi
    return lo, hi


def artifact_in(text: str) -> str | None:
    """The first house-style artifact a settlement's text would ship — the
    same patterns certify's artifact scan fails a build on (`”.` and `”,`
    are British/logical punctuation, which the walker's suggestions used 16
    times on the Redding trial; `,,`, a doubled period after an ellipsis, a
    doubled space are composition damage). None when clean."""
    from galley.manifest import _ARTIFACTS
    for pat in _ARTIFACTS:
        if pat.search(text):
            return pat.pattern
    if "  " in text:
        return "  "
    return None


def is_space_deletion(before: str, after: str) -> bool:
    """`before` and `after` differ only by removing whitespace between two
    letters — closing an open compound, the author's call (validator policy)."""
    if not before or after == before:
        return False
    if _SPACE_ONLY.sub("", before) != after:
        return False
    return len(after) < len(before)


def edit_kind(before: str, after: str) -> str:
    """"punctuation" when the two differ only in punctuation/whitespace/case,
    else "wording" — what an intent zone's permission is checked against."""
    b = _PUNCT_ONLY.sub("", before).replace(" ", "").lower()
    a = _PUNCT_ONLY.sub("", after).replace(" ", "").lower()
    return "punctuation" if a == b else "wording"


def _norm_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:['’][a-z]+)?", text.lower())


# Words that betray an editor's aside living inside the manuscript text — a
# parenthetical a copy-edit lane leaked ("(parallel with X; breaks tense
# agreement as written)"). Deleting such a parenthetical is a repair, never
# a fact change, however many numbers or quoted strings it contains.
_ASIDE_WORDS_RE = re.compile(
    r"\b(?:editorial|editor|as written|breaks|parallel with|agreement|"
    r"tense|revision note|query|should be|needs? (?:a|to)|missing|stray|"
    r"consider|reword|rephrase|clarif|ambiguous|antecedent|dangling|"
    r"comma splice|run-on|fragment|subject and verb)\b", re.IGNORECASE)


def deletes_an_aside(before: str, after: str) -> bool:
    """`after` is `before` with one parenthetical or bracketed span removed,
    and that span reads as an editor's aside rather than the author's."""
    if len(after) >= len(before):
        return False
    for opener, closer in (("(", ")"), ("[", "]")):
        start = 0
        while True:
            i = before.find(opener, start)
            if i == -1:
                break
            j = before.find(closer, i + 1)
            if j == -1:
                break
            inner = before[i + 1:j]
            candidate = (before[:i].rstrip() + before[j + 1:]) \
                if before[:i].endswith(" ") and before[j + 1:j + 2] in (" ", "") \
                else before[:i] + before[j + 1:]
            if _ASIDE_WORDS_RE.search(inner) and (
                    " ".join(candidate.split()) == " ".join(after.split())):
                return True
            start = i + 1
    return False


def _fact(before: str, after: str) -> str | None:
    """flights.fact_change, minus two false alarms a settlement raises: a
    case/punctuation-only change inside quoted speech reads to that check as
    a rewritten title, and a re-cased word ("just" -> "Just") as a new proper
    noun. Only a change in the WORDS of a quoted passage, a number, or a
    genuinely new capitalized word counts here. Removing an editor's aside
    that leaked into the text is never a fact change."""
    from docproof.flights import _titles, fact_change
    if deletes_an_aside(before, after):
        return None
    try:
        why = fact_change(before, after)
    except Exception:                                    # noqa: BLE001
        return None
    if why is None:
        return None
    if why.startswith("title"):
        tb = sorted(" ".join(_norm_words(t)) for t in _titles(before))
        ta = sorted(" ".join(_norm_words(t)) for t in _titles(after))
        if tb == ta:
            return None
        # a quote that the settlement merely opened/closed (marks added)
        if set(_norm_words(before)) == set(_norm_words(after)):
            return None
    if why.startswith("proper noun"):
        if set(_norm_words(before)) >= set(_norm_words(after)):
            return None                                  # re-casing only
    return why


def resolve(res: Residual, em: emap.EditMap, accepted: Mapping[str, str],
            working: Mapping[str, Mapping[str, Any]]) -> str | None:
    """Fill the residual's `source_span` / `owner_finding_id` from the edit
    map, or return why it cannot be placed (a reason prefix)."""
    segs = em.paragraphs.get(res.para_id)
    if segs is None:
        return "unmapped" if res.para_id in em.unmapped else "unanchorable"
    # The map is the coordinate system, but the docx is the truth: when the
    # two disagree (overlapping anchors composed differently than the
    # arithmetic predicts), nothing here may be settled by offsets.
    view = accepted.get(res.para_id)
    if view is not None and view != emap.accepted_of(segs):
        return "unmapped"
    if res.kind == "edit_damage":
        key = _owner_of_edit(res, working)
        if key is None:
            return "unanchorable"
        res.owner_finding_id = key
        owned = [s for s in segs if s.owner == key]
        if owned:
            res.source_span = (min(s.src_start for s in owned),
                               max(s.src_end for s in owned))
        return None
    acc = emap.accepted_of(segs)
    if not res.quote:
        return "unanchorable"
    n = acc.count(res.quote)
    if n == 0:
        # The docx view may carry text the map cannot (a paragraph outside
        # the mapped set); the map is the coordinate system, so a quote it
        # does not hold cannot be settled by arithmetic.
        return "unanchorable"
    if n > 1:
        # The same word-shaped surface more than once in the paragraph: a
        # misspelling repeated. Settle the first site; propagation applies
        # the identical fix to the others (the walker flags one and ignores
        # its twin). Anything else that repeats is genuinely ambiguous.
        if not (_WORD_SHAPED.search(res.quote) and res.suggestion.strip()
                and " " not in res.quote.strip()):
            return "ambiguous_anchor"
        res.raw["multi"] = n
    lo = acc.index(res.quote)
    r = emap.locate(segs, lo, lo + len(res.quote))
    res.source_span = r.source_span or r.source_widened
    res.owner_finding_id = r.owners[0] if r.owners else None
    return None


def _owner_of_edit(res: Residual, working: Mapping[str, Mapping[str, Any]]
                   ) -> str | None:
    for key, row in working.items():
        if (str(row.get("para_id")) == res.para_id
                and str(row.get("original_text")) == res.owner_original
                and str(row.get("corrected_text")) == res.owner_corrected):
            return key
    return None


def decide(res: Residual, em: emap.EditMap, accepted: Mapping[str, str],
           source: Mapping[str, str], working: Mapping[str, Mapping[str, Any]],
           zones: Any = None, *, replacement: str | None = None) -> Decision:
    """The deterministic half of A3. Returns a Decision whose action is one of
    absorb/add/drop/query/revise/revert, or `judge` when only a model can
    settle it (no usable suggestion, or the composite grew past the guard).
    `replacement` overrides the residual's own suggestion (the judge's
    answer on a second pass)."""
    why = resolve(res, em, accepted, working)
    if why is not None:
        if why in ("ambiguous_anchor", "unmapped"):
            # Real text the engine cannot place by arithmetic: still a
            # residual, so it goes to the author with the suggestion, never
            # silently away.
            return Decision("query", why, question=_question(res))
        return Decision("drop", why)

    # --- edit damage: the flagged edit itself is the item -----------------
    if res.kind == "edit_damage":
        key = res.owner_finding_id or ""
        owner = working.get(key, {})
        fix = replacement if replacement is not None else res.suggestion
        fix, had_note = strip_note(fix, res.owner_corrected)
        if not fix.strip() or fix == res.owner_original:
            return Decision("revert", f"edit_damage:{res.verdict or 'flagged'}",
                            owner_key=key)
        if fix == res.owner_corrected:
            # the verifier disliked the edit but offered the same words
            return Decision("revert", f"edit_damage:{res.verdict or 'flagged'}",
                            owner_key=key)
        if had_note and fix == res.owner_corrected:
            return Decision("revert", "editorial_note", owner_key=key)
        fact = _fact(res.owner_corrected, fix)
        if fact:
            return Decision("query", f"fact:{fact}", owner_key=key,
                            question=_question(res, fix))
        bad = artifact_in(fix)
        if bad and not artifact_in(res.owner_corrected):
            if replacement is None:
                return Decision("judge", f"artifact:{bad}", owner_key=key)
            return Decision("query", f"artifact:{bad}", owner_key=key,
                            question=_question(res, fix))
        if len(fix) > MAX_GROWTH * max(len(res.owner_original), 1) \
                + MAX_GROWTH_SLACK and replacement is None:
            return Decision("judge", "oversize", owner_key=key)
        return Decision("revise", f"edit_damage:{res.verdict or 'flagged'}",
                        replacement=fix, owner_key=key)

    # --- a walk residual ---------------------------------------------------
    segs = em.paragraphs[res.para_id]
    acc = emap.accepted_of(segs)
    lo = acc.index(res.quote)
    hi = lo + len(res.quote)
    suggestion = replacement if replacement is not None else res.suggestion
    suggestion, had_note = strip_note(suggestion, res.quote)
    if had_note and not suggestion.strip():
        return Decision("query", "editorial_note", question=_question(res))
    if not suggestion.strip() and res.quote.strip():
        if replacement is None:
            return Decision("judge", "no_suggestion")
        return Decision("query", "no_suggestion", question=_question(res))
    if looks_like_instruction(suggestion, res.quote):
        if replacement is None:
            return Decision("judge", "instruction")
        return Decision("query", "instruction", question=_question(res))
    if suggestion == res.quote:
        return Decision("drop", "walker_wrong")
    if is_space_deletion(res.quote, suggestion):
        return Decision("query", "space_deletion",
                        question=_question(res, suggestion))
    lo, hi = align_to_sentence(acc, lo, hi, suggestion)
    if suggestion == acc[lo:hi]:
        return Decision("drop", "walker_wrong")

    comp = emap.compose(segs, lo, hi, suggestion)
    src_text = source.get(res.para_id, "")
    # intent zones: the SOURCE span the settlement would touch
    if zones is not None and getattr(zones, "any", False):
        zone = zones.zone_at(res.para_id, comp.src_start, comp.src_end)
        if zone is not None:
            from docproof.intent_zones import permits
            if not permits(zone, edit_kind(res.quote, suggestion)):
                label = getattr(zone, "label", "") or getattr(zone, "category",
                                                              "")
                return Decision("drop", f"intent_zone:{label}")
    fact = _fact(comp.before, comp.text)
    if fact:
        return Decision("query", f"fact:{fact}", question=_question(res,
                                                                    suggestion))
    # Scan the settlement WITH its neighbours: `”.` is the boundary between a
    # replacement ending in a quote mark and the period that follows it.
    ctx_before = acc[max(0, comp.acc_start - 3):comp.acc_start]
    ctx_after = acc[comp.acc_end:comp.acc_end + 3]
    bad = artifact_in(ctx_before + comp.text + ctx_after)
    if bad and not artifact_in(ctx_before + comp.before + ctx_after):
        if replacement is None:
            return Decision("judge", f"artifact:{bad}")
        return Decision("query", f"artifact:{bad}",
                        question=_question(res, suggestion))
    if len(comp.text) > MAX_GROWTH * max(len(comp.before), 1) + MAX_GROWTH_SLACK:
        if replacement is None:
            return Decision("judge", "oversize")
        return Decision("query", "oversize", question=_question(res, suggestion))
    # duplicate: a kept row already does exactly this
    for key, row in working.items():
        if (str(row.get("para_id")) == res.para_id and not comp.absorbed
                and str(row.get("corrected_text")) == comp.text
                and str(row.get("original_text")) == src_text[comp.src_start:
                                                              comp.src_end]):
            return Decision("drop", "duplicate")
    if comp.absorbed:
        return Decision("absorb", "", replacement=comp.text, composite=comp,
                        owner_key=comp.owners[0])
    return Decision("add", "", replacement=comp.text, composite=comp)


def _question(res: Residual, suggestion: str = "") -> str:
    sug = suggestion or res.suggestion
    what = res.problem.strip() or "possible error"
    if sug and sug != res.quote:
        return f"{what} — suggested: {sug!r}"
    return what


# ---- the narrow judge (A3, second half) ------------------------------------------

_JUDGE_SYSTEM = """\
You settle ONE residual at a time on a finished book proofread. You are shown a
sentence as the author wrote it (SOURCE), the sentence as it now reads after
tracked edits (CURRENT), the applied edit that owns the span if any, and what a
proofreader flagged in CURRENT with their suggestion. Decide the narrowest
correct settlement:
  absorb  — the flagged span sits inside/across an applied edit; give the exact
            text that should replace the FLAGGED SPAN ONLY (not the sentence);
  add     — the flagged span is untouched text; give the exact replacement for
            the flagged span only;
  drop    — the flag is wrong or is a matter of taste/voice; say why in one line;
  query   — only the author can answer (a fact, an intent, an identity, or two
            plausible repairs); write the one-line question.
Never rewrite beyond the flagged span. Never change a number, name, title,
date, or quoted line — that is a query. Never write a note to the editor into
the replacement. Standard: U.S. English, Chicago 17, Merriam-Webster.
Return JSON {"action": ..., "replacement": ..., "reason": ..., "question": ...}
with every key present (empty string when unused)."""


def _judge_schema() -> tuple[dict[str, Any], str]:
    from pydantic import BaseModel

    from docproof.providers import strict_json_schema

    class _Decision(BaseModel):
        action: str
        replacement: str
        reason: str
        question: str

    return strict_json_schema(_Decision), "decision"


def judge(res: Residual, em: emap.EditMap, source: Mapping[str, str],
          working: Mapping[str, Mapping[str, Any]], provider, model: str,
          usage: Usage, *, context: str = "", max_tokens: int = 2000
          ) -> Decision:
    """One model call for one residual the deterministic pass could not
    settle. The reply is re-run through `decide` with the judge's replacement
    so every guard (note, fact, growth, intent zone) still applies to what the
    model proposed."""
    segs = em.paragraphs.get(res.para_id) or []
    acc = emap.accepted_of(segs)
    src = source.get(res.para_id, "")
    if res.kind == "edit_damage":
        span_lo, span_hi = res.source_span or (0, 0)
        lo, hi = emap.sentence_bounds(src, span_lo, span_hi)
        source_sentence = src[lo:hi]
        current_sentence = res.owner_corrected
        flagged = res.owner_corrected
    else:
        a0 = acc.find(res.quote)
        alo, ahi = emap.sentence_bounds(acc, a0, a0 + len(res.quote))
        current_sentence = acc[alo:ahi]
        s0, s1 = res.source_span or (0, 0)
        lo, hi = emap.sentence_bounds(src, s0, s1)
        source_sentence = src[lo:hi]
        flagged = res.quote
    owner = working.get(res.owner_finding_id or "", {})
    owner_line = (f"OWNING EDIT: {owner.get('original_text', '')!r} -> "
                  f"{owner.get('corrected_text', '')!r}\n") if owner else \
        "OWNING EDIT: none (untouched text)\n"
    user = (f"SOURCE: {source_sentence}\nCURRENT: {current_sentence}\n"
            f"{owner_line}FLAGGED SPAN: {flagged!r}\n"
            f"PROBLEM: {res.problem}\nSUGGESTION: {res.suggestion!r}")
    schema, name = _judge_schema()
    system = _JUDGE_SYSTEM + ("\n\nHOUSE STYLE AND VOICE NOTES:\n" + context
                              if context.strip() else "")
    result = provider.complete_structured(model=model, system=system, user=user,
                                          schema=schema, schema_name=name,
                                          max_tokens=max_tokens)
    if result.usage is not None:
        usage.add(result.usage, model=model)
    if result.stop_reason != "ok" or not isinstance(result.parsed, dict):
        return Decision("query", "no_suggestion", question=_question(res))
    action = str(result.parsed.get("action", "")).strip().lower()
    replacement = str(result.parsed.get("replacement", "") or "")
    reason = str(result.parsed.get("reason", "") or "").strip()[:160]
    question = str(result.parsed.get("question", "") or "").strip()[:300]
    if action == "drop":
        return Decision("drop", f"voice:{reason}" if reason else "voice")
    if action == "query":
        return Decision("query", "judge", question=question or _question(res))
    if action in ("absorb", "add") and replacement.strip():
        return Decision("judge_replacement", reason, replacement=replacement)
    return Decision("query", "no_suggestion", question=_question(res))


# ---- applying decisions (A4) ---------------------------------------------------

def _query_row(res: Residual, source: Mapping[str, str], question: str,
               *, lane: str = "") -> dict[str, Any] | None:
    """An author-question row (O == C, force_query) anchored on the sentence
    around the residual's source span, or None when it has no source span."""
    src = source.get(res.para_id, "")
    if not src:
        return None
    s0, s1 = res.source_span or (0, 0)
    lo, hi = emap.sentence_bounds(src, s0, s1)
    if lo == hi:
        lo, hi = 0, len(src)
    while lo < hi and src[lo].isspace():
        lo += 1
    sentence = src[lo:hi]
    if not sentence.strip():
        return None
    row: dict[str, Any] = {
        "para_id": res.para_id, "original_text": sentence,
        "occurrence": src[:lo].count(sentence) + 1 if sentence else 1,
        "corrected_text": sentence, "error_type": SETTLE_TYPE,
        "explanation": question, "confidence": "medium",
        "force_query": True, "queried": True,
        "chunk_id": f"{SETTLE_CHUNK_PREFIX}{res.id}",
    }
    if lane:
        row["lane"] = lane
    return row


def restore_rows(working: dict[str, dict[str, Any]], rid: str,
                 rows: Any) -> int:
    """Put previously removed rows back into the working set under keys that
    cannot collide with any current finding id. Returns how many."""
    n = 0
    for row in rows:
        n += 1
        working[f"restore-{rid}-{n}"] = row
    return n


def apply_decision(res: Residual, dec: Decision,
                   working: dict[str, dict[str, Any]],
                   source: Mapping[str, str], round_no: int, *,
                   verified_by: str) -> tuple[SettlementRecord,
                                              list[dict[str, Any]],
                                              dict[str, dict[str, Any]]]:
    """Mutate the working set per the decision. Returns the record, the new
    rows to append, and the rows removed (so a failed settlement can be
    reverted)."""
    removed: dict[str, dict[str, Any]] = {}
    new_rows: list[dict[str, Any]] = []
    owner_key = dec.owner_key
    before = working.get(owner_key or "", {}).get("corrected_text", "") \
        if owner_key else ""

    if dec.action in ("absorb", "add") and dec.composite is not None:
        comp = dec.composite
        lane = ""
        cluster = ""
        for key in comp.owners:
            row = working.pop(key, None)
            if row is not None:
                removed[key] = row
                lane = lane or str(row.get("lane") or "")
                cluster = cluster or str(row.get("cluster_id") or "")
        extra = {"chunk_id": f"{SETTLE_CHUNK_PREFIX}{res.id}",
                 "lane": lane, "cluster_id": cluster}
        row = emap.as_row(source[res.para_id], comp, para_id=res.para_id,
                          error_type=SETTLE_TYPE, explanation=res.problem,
                          extra=extra)
        new_rows.append(row)
        rec = SettlementRecord(res.id, round_no, dec.action, owner_key,
                               comp.before, comp.text, dec.reason, verified_by,
                               para_id=res.para_id, kind=res.kind)
        return rec, new_rows, removed

    if dec.action == "revise" and owner_key:
        row = working.get(owner_key)
        if row is not None:
            removed[owner_key] = dict(row)
            row["corrected_text"] = dec.replacement
            row["chunk_id"] = f"{SETTLE_CHUNK_PREFIX}{res.id}"
        rec = SettlementRecord(res.id, round_no, "revise", owner_key, before,
                               dec.replacement, dec.reason, verified_by,
                               para_id=res.para_id, kind=res.kind)
        return rec, new_rows, removed

    if dec.action == "revert" and owner_key:
        row = working.pop(owner_key, None)
        if row is not None:
            removed[owner_key] = row
        rec = SettlementRecord(res.id, round_no, "drop", owner_key, before, "",
                               dec.reason, verified_by, para_id=res.para_id,
                               kind=res.kind)
        return rec, new_rows, removed

    if dec.action == "query":
        lane = str(working.get(owner_key or "", {}).get("lane") or "")
        qrow = _query_row(res, source, dec.question or _question(res),
                          lane=lane)
        if qrow is not None:
            new_rows.append(qrow)
        rec = SettlementRecord(res.id, round_no, "query", owner_key, before,
                               "", dec.reason, verified_by, para_id=res.para_id,
                               question=dec.question or _question(res),
                               kind=res.kind)
        return rec, new_rows, removed

    # drop
    rec = SettlementRecord(res.id, round_no, "drop", owner_key, before, "",
                           dec.reason, verified_by, para_id=res.para_id,
                           kind=res.kind)
    return rec, new_rows, removed


# ---- import-findings --anchor accepted (rows in hand, no loop) ------------------

@dataclass
class Folded:
    rows: list[dict[str, Any]]
    absorbed: int = 0
    added: int = 0
    queried: int = 0
    dropped: int = 0
    notes: list[str] = field(default_factory=list)
    records: list[SettlementRecord] = field(default_factory=list)


def _residual_from_row(row: Mapping[str, Any]) -> Residual:
    pid = str(row.get("para_id", ""))
    quote = str(row.get("quote") if row.get("quote") is not None
                else row.get("original_text", ""))
    repl = row.get("replacement") if row.get("replacement") is not None \
        else row.get("corrected_text", "")
    from galley.verify import residual_id
    return Residual(id=str(row.get("residual_id") or residual_id(pid, quote)),
                    kind="residual", para_id=pid, quote=quote,
                    problem=str(row.get("explanation") or row.get("problem")
                                or row.get("rationale") or ""),
                    suggestion=str(repl or ""),
                    severity=str(row.get("severity") or "medium"),
                    raw=dict(row))


def _source_paragraphs(cfg, manuscript: str | Path, error_dir: str | Path
                       ) -> tuple[dict[str, str], Any]:
    """The canonical (post-normalization) paragraphs the replay anchors
    against, plus the resolved intent zones when the config names a file."""
    from docproof.pipeline import prepare
    c = copy.deepcopy(cfg)
    from docproof.replay import zero_paid_passes
    zero_paid_passes(c)
    prepared = prepare(c, str(manuscript), error_dir, analyses=False,
                       dry_run=True)
    paras = {p.para_id: p.text for p in prepared.doc.paragraphs}
    zones = None
    if getattr(cfg, "intent_zones_file", None):
        from docproof.intent_zones import load_intent_zones, resolve as _res
        zones = _res(load_intent_zones(cfg.intent_zones_file),
                     list(prepared.doc.paragraphs))
    return paras, zones


def fold_accepted_rows(run_dir: Path, rows: Sequence[Mapping[str, Any]], *,
                       cfg, manuscript: str | Path, error_dir: str | Path
                       ) -> Folded:
    """Rows quoted from a build's ACCEPTED text, folded into that build's kept
    rows through its edit map — the `import-findings --anchor accepted` path.
    Deterministic only (a row the guards cannot settle becomes a query)."""
    env = load_envelope(run_dir)
    working, _owner_of = kept_rows(env.get("findings") or [])
    source, zones = _source_paragraphs(cfg, manuscript, error_dir)
    em = emap.load_or_build(run_dir, source, env.get("findings") or [])
    from galley.verify import paragraph_views
    _orig, accepted = paragraph_views(run_dir)
    folded = Folded(rows=[])
    new_rows: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        res = _residual_from_row(raw)
        dec = decide(res, em, accepted, source, working, zones)
        if dec.action == "judge":
            dec = Decision("query", dec.reason, question=_question(res))
        rec, added, _removed = apply_decision(res, dec, working, source, 0,
                                              verified_by="import")
        new_rows.extend(added)
        folded.records.append(rec)
        if rec.action in ("absorb", "revise"):
            folded.absorbed += 1
        elif rec.action == "add":
            folded.added += 1
        elif rec.action == "query":
            folded.queried += 1
        else:
            folded.dropped += 1
            folded.notes.append(f"{res.para_id} {res.quote[:40]!r}: dropped "
                                f"({rec.reason})")
    folded.rows = list(working.values()) + new_rows
    return folded


# ---- the loop (A1–A7) ---------------------------------------------------------

# --until-clean: keep sweeping while a round is still finding real work, stop
# when a round comes back quiet. "Quiet" is judged against how much the round
# re-read, never by a fixed count alone: 100 new items after re-reading 2,000
# edits is a signal to keep looking; 3 after re-reading 150 is noise (the
# verifier flags ~5% of anything it re-reads, and would churn forever).
DEFAULT_QUIET_FLOOR = 3          # new items at or below this are always quiet
DEFAULT_QUIET_SHARE = 0.02       # ...or at or below this share of items re-read
DEFAULT_MAX_TURNS = 400          # subagent/API calls per settle invocation
HARD_MAX_ROUNDS = 12             # the ceiling --until-clean can never pass
# Propagation: a settled fix on untouched text is applied to the SAME surface
# elsewhere in the paragraph and its neighbours (the walker flags one site
# and ignores the twin two lines down). Word-shaped quotes only.
PROPAGATE_REACH = 1              # paragraphs either side
PROPAGATE_MAX = 40               # sites per residual
_WORD_SHAPED = re.compile(r"[A-Za-z]{3,}")


@dataclass
class SettleOptions:
    rounds: int = DEFAULT_ROUNDS
    engine: str = "none"                # none | provider | subagent
    model: str = ""
    context: str = ""
    max_tokens: int = 12000
    verify_delta: bool = True           # A5 (needs an engine)
    keep_snapshots: bool = True
    until_clean: bool = False
    quiet_floor: int = DEFAULT_QUIET_FLOOR
    quiet_share: float = DEFAULT_QUIET_SHARE
    max_turns: int = DEFAULT_MAX_TURNS
    propagate: bool = True


@dataclass
class SettleResult:
    settlement: Settlement
    outputs: Any = None
    usage: Usage = field(default_factory=Usage)
    touched: set[str] = field(default_factory=set)

    @property
    def open(self) -> int:
        return len(self.settlement.open)


class Settler:
    """One run's settlement loop. Construct, then `run()`."""

    def __init__(self, run_dir: str | Path, *, cfg, manuscript: str | Path,
                 error_dir: str | Path, provider=None,
                 options: SettleOptions | None = None):
        self.run_dir = Path(run_dir)
        self.cfg = cfg
        self.manuscript = Path(manuscript)
        self.error_dir = Path(error_dir)
        self.provider = provider
        self.opt = options or SettleOptions()
        self.usage = Usage()
        self.settlement = Settlement.load(self.run_dir) or Settlement()
        self.settlement.run_dir = str(self.run_dir)
        self.settlement.max_rounds = self.opt.rounds
        self.settlement.engine = self.opt.engine
        self.settlement.model = self.opt.model
        self.verified_by = (f"{self.opt.engine}:{self.opt.model}"
                            if self.provider is not None else "deterministic")
        self.touched: set[str] = set()
        self._source: dict[str, str] = {}
        self._zones: Any = None

    # -- one round ----------------------------------------------------------

    def _rebuild(self, rows: list[dict[str, Any]], *, snapshot: str) -> Any:
        from docproof.replay import rebuild_from_rows
        cfg = copy.deepcopy(self.cfg)
        cfg.output_dir = str(self.run_dir)
        if self.opt.keep_snapshots:
            snap = self.run_dir / "settle" / snapshot
            snap.mkdir(parents=True, exist_ok=True)
            for name in ("findings.json", "finished_walk.json",
                         "change_verify.json", emap.EDITMAP_NAME):
                p = self.run_dir / name
                if p.is_file():
                    (snap / name).write_bytes(p.read_bytes())
        result = rebuild_from_rows(cfg, manuscript=self.manuscript, rows=rows,
                                   error_dir=self.error_dir,
                                   remap_unchanneled=False, id_prefix="settle")
        return result

    def _load_state(self) -> tuple[dict[str, dict[str, Any]], emap.EditMap,
                                   dict[str, str]]:
        env = load_envelope(self.run_dir)
        rows = env.get("findings") or []
        working, _ = kept_rows(rows)
        if not self._source:
            self._source, self._zones = _source_paragraphs(
                self.cfg, self.manuscript, self.error_dir)
        em = emap.load_or_build(self.run_dir, self._source, rows)
        from galley.verify import paragraph_views
        _orig, accepted = paragraph_views(self.run_dir)
        return working, em, accepted

    def _settled_ok(self, rows_after: Sequence[Mapping[str, Any]],
                    residual_ids: set[str]) -> dict[str, str]:
        """After a rebuild: for each settlement row, whether it landed.
        Returns residual_id -> failure status for the ones that did not."""
        failed: dict[str, str] = {}
        for r in rows_after:
            if not isinstance(r, dict):
                continue
            rid = settle_residual_of(r)
            if not rid or rid not in residual_ids:
                continue
            state, reason = terminal_state(r)
            if state == "applied":
                continue
            if state == "query" and (r.get("force_query") or r.get("queried")):
                continue
            failed[str(rid)] = reason or state
        return failed

    def round(self, round_no: int, items: list[Residual]) -> list[Residual]:
        """Settle `items`; return the NEW open items the delta verify raised
        (empty when the engine cannot verify or nothing new surfaced)."""
        working, em, accepted = self._load_state()
        source = self._source
        new_rows: list[dict[str, Any]] = []
        removed_by: dict[str, dict[str, dict[str, Any]]] = {}
        added_by: dict[str, list[dict[str, Any]]] = {}
        records: list[SettlementRecord] = []
        judged = 0
        for res in items:
            dec = decide(res, em, accepted, source, working, self._zones)
            if dec.action == "judge":
                if self.provider is None:
                    dec = Decision("query", dec.reason,
                                   question=_question(res))
                else:
                    judged += 1
                    jd = judge(res, em, source, working, self.provider,
                               self.opt.model, self.usage,
                               context=self.opt.context)
                    if jd.action == "judge_replacement":
                        dec = decide(res, em, accepted, source, working,
                                     self._zones, replacement=jd.replacement)
                        if dec.action == "judge":
                            dec = Decision("query", dec.reason,
                                           question=_question(res))
                    else:
                        dec = jd
            rec, added, removed = apply_decision(
                res, dec, working, source, round_no,
                verified_by=self.verified_by)
            records.append(rec)
            new_rows.extend(added)
            removed_by[res.id] = removed
            added_by[res.id] = added
            if rec.action in ("absorb", "add", "revise", "drop") \
                    and rec.owner_finding_id or rec.action in ("absorb", "add",
                                                               "revise"):
                self.touched.add(res.para_id)
            elif rec.action == "query" and added:
                self.touched.add(res.para_id)
        # A revert/drop that removed an owner also changes the paragraph.
        for res in items:
            if removed_by.get(res.id):
                self.touched.add(res.para_id)

        # Propagate: the same surface, untouched, in the paragraph and its
        # neighbours gets the same fix — recorded, never silent.
        if self.opt.propagate:
            propagated = self._propagate(items, records, em, source, working,
                                         new_rows, round_no)
            new_rows.extend(propagated[0])
            records.extend(propagated[1])

        rows = list(working.values()) + new_rows
        result = self._rebuild(rows, snapshot=f"round{round_no}")
        env = load_envelope(self.run_dir)
        failed = self._settled_ok(env.get("findings") or [],
                                  {r.id for r in items})
        if failed:
            # The composite did not land (the validator refused it). Put the
            # owner rows back, take the composite out, and hand the residual
            # to the author instead — never lose the owner's fix silently.
            for rid, status in failed.items():
                res = next(r for r in items if r.id == rid)
                for key, row in removed_by.get(rid, {}).items():
                    working[key] = row
                for row in added_by.get(rid, []):
                    if row in new_rows:
                        new_rows.remove(row)
                q = _query_row(res, source, _question(res))
                if q is not None:
                    new_rows.append(q)
                records.append(SettlementRecord(
                    rid, round_no, "query", res.owner_finding_id, "", "",
                    f"rejected_{status}" if not status.startswith("rejected")
                    else status, self.verified_by, para_id=res.para_id,
                    question=_question(res), kind=res.kind))
            rows = list(working.values()) + new_rows
            self._rebuild(rows, snapshot=f"round{round_no}-revert")
        self.settlement.records.extend(records)
        for res in items:
            self.settlement.residuals_seen.append(res.to_json())
        self.settlement.notes.append(
            f"round {round_no}: {len(items)} item(s), {judged} judged, "
            f"{len(failed)} reverted")

        # A5 — delta verify over the touched paragraphs
        self.last_reread = 0
        if self.provider is None or not self.opt.verify_delta \
                or not self.touched:
            return []
        from galley.verify import applied_edits, verify_delta
        touched = sorted(self.touched)
        self.last_reread = (
            sum(1 for e in applied_edits(self.run_dir)
                if str(e.get("para_id", "")) in self.touched)
            + len(touched))
        vr = verify_delta(self.run_dir, touched, self.provider, self.opt.model,
                          self.usage, context=self.opt.context,
                          max_tokens=self.opt.max_tokens)
        have = self.settlement.record_ids()
        fresh: list[Residual] = []
        # a verifier flag on a composite this round -> revert it, query
        composites = {rec.residual_id: rec for rec in records
                      if rec.action in ("absorb", "add", "revise")}
        revert_ids: list[str] = []
        for p in vr.problems:
            item = Residual.from_problem(p.to_json(), round_no)
            # Is the flagged edit one of this round's settlement rows?
            hit = None
            for rid, rec in composites.items():
                if rec.para_id == item.para_id and \
                        rec.after_replacement and \
                        rec.after_replacement in item.owner_corrected:
                    hit = rid
                    break
            if hit is not None:
                revert_ids.append(hit)
                continue
            if item.id not in have:
                fresh.append(item)
        for r in vr.residuals:
            item = Residual.from_walk(r.to_json(), round_no)
            if item.id not in have:
                fresh.append(item)
        if revert_ids:
            working, em, accepted = self._load_state()
            for rid in revert_ids:
                res = next(r for r in items if r.id == rid)
                # remove the settlement row(s) for this residual
                for key in [k for k, row in working.items()
                            if settle_residual_of(row) == rid]:
                    working.pop(key)
                # Restore what the composite absorbed — under FRESH keys.
                # `removed_by` is keyed by finding ids from BEFORE the
                # rebuild, and every rebuild re-mints ids, so writing them
                # back by their old key overwrote whatever unrelated row now
                # held that id (Redding lost a number_style row and two
                # galley_read rows that way).
                restore_rows(working, rid, removed_by.get(rid, {}).values())
                q = _query_row(res, source, _question(res))
                extra_rows = [q] if q is not None else []
                self.settlement.records.append(SettlementRecord(
                    rid, round_no, "query", res.owner_finding_id,
                    composites[rid].after_replacement, "",
                    "verifier_reverted", self.verified_by,
                    para_id=res.para_id, question=_question(res),
                    kind=res.kind))
                working[f"settle-q-{rid}"] = extra_rows[0] if extra_rows \
                    else working.get(f"settle-q-{rid}", {})
                if not extra_rows:
                    working.pop(f"settle-q-{rid}", None)
            self._rebuild(list(working.values()),
                          snapshot=f"round{round_no}-verifier-revert")
        self.touched = set()
        return fresh

    def _propagate(self, items, records, em, source, working, new_rows,
                   round_no):
        """For every `add` this round on a word-shaped quote, find the same
        surface in untouched text of the paragraph and its neighbours and
        add the same fix there. Returns (rows, records)."""
        order = list(source.keys())
        index = {pid: i for i, pid in enumerate(order)}
        by_id = {res.id: res for res in items}
        out_rows: list[dict[str, Any]] = []
        out_recs: list[SettlementRecord] = []
        claimed: set[tuple[str, int]] = set()
        for rec in list(records):
            if rec.action != "add" or rec.kind != "residual":
                continue
            res = by_id.get(rec.residual_id)
            if res is None or not _WORD_SHAPED.search(res.quote):
                continue
            quote, repl = res.quote, rec.after_replacement
            if not repl or repl == quote or " " in quote.strip() and \
                    len(quote) > 40:
                continue
            i = index.get(res.para_id)
            if i is None:
                continue
            n = 0
            for j in range(max(0, i - PROPAGATE_REACH),
                           min(len(order), i + PROPAGATE_REACH + 1)):
                pid = order[j]
                segs = em.paragraphs.get(pid)
                if not segs:
                    continue
                acc = emap.accepted_of(segs)
                start = 0
                while n < PROPAGATE_MAX:
                    lo = acc.find(quote, start)
                    if lo == -1:
                        break
                    start = lo + 1
                    hi = lo + len(quote)
                    if pid == res.para_id and res.source_span is not None:
                        # skip the site the residual itself settled
                        try:
                            r0 = emap.locate(segs, lo, hi)
                        except ValueError:
                            continue
                        if r0.source_span == res.source_span:
                            continue
                    try:
                        r = emap.locate(segs, lo, hi)
                    except ValueError:
                        continue
                    if r.case != "untouched" or r.source_span is None:
                        continue
                    key = (pid, r.source_span[0])
                    if key in claimed:
                        continue
                    # a word boundary on both sides, so "form" never edits
                    # "formal"
                    if (lo > 0 and acc[lo - 1].isalnum()) or \
                            (hi < len(acc) and acc[hi].isalnum()):
                        continue
                    comp = emap.compose(segs, lo, hi, repl)
                    row = emap.as_row(source[pid], comp, para_id=pid,
                                      error_type=SETTLE_TYPE,
                                      explanation=f"same as {res.para_id}: "
                                                  f"{res.problem}",
                                      extra={"chunk_id": f"{SETTLE_CHUNK_PREFIX}"
                                             f"{res.id}+{n + 1}"})
                    out_rows.append(row)
                    claimed.add(key)
                    n += 1
                    out_recs.append(SettlementRecord(
                        f"{res.id}+{n}", round_no, "add", None, quote, repl,
                        f"propagated:{res.id}", self.verified_by,
                        para_id=pid, kind="residual"))
                    self.touched.add(pid)
        if out_rows:
            log.info("settle: propagated %d fix(es) to identical untouched "
                     "sites nearby", len(out_rows))
        return out_rows, out_recs

    # -- the whole loop -----------------------------------------------------

    def _quiet(self, new_items: int) -> bool:
        """--until-clean's stop rule: a round is quiet when the new items it
        raised are within the floor OR within the share of what it re-read."""
        if new_items <= self.opt.quiet_floor:
            return True
        return new_items <= self.opt.quiet_share * max(self.last_reread, 1)

    def fresh_sweep(self) -> list[Residual]:
        """A FULL verify (both gates) over the current build, written as the
        run's artifacts, and the open items it raises. This is what
        `--until-clean` does when it starts with nothing open: a sweep must
        read the book as it is now, not restamp a read of an earlier build.
        Counts toward the turn budget like every other call."""
        from galley.verify import (accepted_text, applied_edits, verify_run,
                                   write_artifacts)
        uc, uw = Usage(), Usage()
        changes = verify_run(self.run_dir, self.provider, self.opt.model, uc,
                             context=self.opt.context, run_changes=True,
                             run_walk=False, max_tokens=self.opt.max_tokens)
        walk = verify_run(self.run_dir, self.provider, self.opt.model, uw,
                          context=self.opt.context, run_changes=False,
                          run_walk=True, max_tokens=self.opt.max_tokens)
        for u in (uc, uw):
            for f in ("input_tokens", "output_tokens",
                      "cache_creation_input_tokens", "cache_read_input_tokens",
                      "api_calls"):
                setattr(self.usage, f, getattr(self.usage, f) + getattr(u, f))
            for mdl, bucket in u.by_model.items():
                dst = self.usage.by_model.setdefault(mdl, {"api_calls": 0})
                for k, v in bucket.items():
                    dst[k] = dst.get(k, 0) + v
        acc = accepted_text(self.run_dir)
        write_artifacts(self.run_dir, changes, walk, model=self.opt.model,
                        engine=self.opt.engine, usage_changes=uc,
                        usage_walk=uw, applied=len(applied_edits(self.run_dir)),
                        paragraphs=sum(1 for t in acc.values() if t.strip()))
        self.settlement.notes.append(
            f"fresh sweep: {len(changes.problems)} flagged edit(s), "
            f"{len(walk.residuals)} residual(s) over the whole book")
        # Every earlier record stays; the new artifacts carry only what this
        # sweep raised, so completeness is judged against the current read.
        return open_items(self.run_dir)

    def run(self) -> SettleResult:
        items = open_items(self.run_dir)
        round_no = self.settlement.rounds
        self.last_reread = 0
        if not items and self.opt.until_clean and self.provider is not None:
            log.info("settle: nothing open — --until-clean starts with a "
                     "fresh sweep of the whole book")
            items = self.fresh_sweep()
            self.last_reread = (len(items) or 1) * 50   # a whole-book read
        limit = HARD_MAX_ROUNDS if self.opt.until_clean else self.opt.rounds
        stopped = "clean"
        while items and round_no < limit:
            round_no += 1
            log.info("settle: round %d over %d item(s)", round_no, len(items))
            items = self.round(round_no, items)
            self.settlement.rounds = round_no
            self.settlement.save(self.run_dir)
            if not items:
                stopped = "clean"
                break
            stopped = "rounds"
            if not self.opt.until_clean:
                continue
            if self.usage.api_calls >= self.opt.max_turns:
                stopped = "turn_budget"
                note = (f"round {round_no}: turn budget reached "
                        f"({self.usage.api_calls} of {self.opt.max_turns}); "
                        f"{len(items)} item(s) ship as questions")
                log.warning("settle: %s", note)
                self.settlement.notes.append(note)
                break
            if self._quiet(len(items)):
                stopped = "quiet"
                note = (f"round {round_no}: quiet — {len(items)} new item(s) "
                        f"after re-reading {self.last_reread}; converged, "
                        f"leftovers ship as questions")
                log.info("settle: %s", note)
                self.settlement.notes.append(note)
                break
            self.settlement.notes.append(
                f"round {round_no}: {len(items)} new item(s) after "
                f"re-reading {self.last_reread} — still finding work, "
                f"another round")
            if round_no >= limit:
                stopped = "round_cap"
        self.settlement.convergence = {
            "stopped": stopped, "last_new_items": len(items),
            "last_reread": self.last_reread, "rounds": round_no,
            "quiet": self._quiet(len(items)) if items else True,
            "turns": self.usage.api_calls}
        if items:
            # I5 — bounded and honest: leftovers ship as questions.
            round_no += 1
            working, em, accepted = self._load_state()
            source = self._source
            new_rows: list[dict[str, Any]] = []
            for res in items:
                resolve(res, em, accepted, working)
                q = _query_row(res, source, _question(res))
                if q is not None:
                    new_rows.append(q)
                    self.touched.add(res.para_id)
                self.settlement.records.append(SettlementRecord(
                    res.id, round_no, "query" if q is not None else "drop",
                    res.owner_finding_id, "", "",
                    f"unresolved_after_{self.settlement.rounds}" if q is not None
                    else "unanchorable", self.verified_by,
                    para_id=res.para_id, question=_question(res),
                    kind=res.kind))
                self.settlement.residuals_seen.append(res.to_json())
            self._rebuild(list(working.values()) + new_rows,
                          snapshot=f"round{round_no}-closeout")
            self.settlement.rounds = round_no
            items = []
        self.settlement.open = [r.to_json() for r in items]
        self._finalize()
        return SettleResult(self.settlement, usage=self.usage,
                            touched=set(self.touched))

    def _finalize(self) -> None:
        """Write the artifacts certify reads: settlement.json, the two verify
        artifacts re-stamped for THIS build with every item carrying a record,
        and the terminal state on every findings.json row."""
        from docproof.providers import cost_of_usage
        cost = cost_of_usage(self.usage, fallback_model=self.opt.model or None) \
            or 0.0
        self.settlement.cost = {"total_usd": float(cost),
                                "api_calls": self.usage.api_calls,
                                "engine": self.opt.engine,
                                "model": self.opt.model}
        stamp_states(self.run_dir, self.settlement)
        rewrite_verify_artifacts(self.run_dir, self.settlement)
        self.settlement.save(self.run_dir)


def stamp_states(run_dir: str | Path, settlement: Settlement) -> int:
    """Add `state` / `disposition_reason` (and `absorbed` / `owner_of` on the
    settlement composites) to every findings.json row, preserving everything
    else. Returns the count of non-terminal rows found (should be 0)."""
    run = Path(run_dir)
    path = run / "findings.json"
    env = json.loads(path.read_text(encoding="utf-8"))
    latest = settlement.latest()
    by_residual = {rec.residual_id: rec for rec in latest.values()}
    non_terminal = 0
    for row in env.get("findings") or []:
        if not isinstance(row, dict):
            continue
        state, reason = terminal_state(row)
        row["state"] = state
        row["disposition_reason"] = reason
        if state not in TERMINAL_STATES:
            non_terminal += 1
        rid = settle_residual_of(row)
        rec = by_residual.get(str(rid)) if rid else None
        if rec is not None and rec.action in ("absorb", "revise"):
            row["absorbed"] = [rec.owner_finding_id] if rec.owner_finding_id \
                else []
            a = row.get("anchor")
            if isinstance(a, dict):
                row["owner_of"] = [a.get("start"), a.get("end")]
    env["settlement"] = {"rounds": settlement.rounds,
                         "counts": settlement.counts(),
                         "open": len(settlement.open)}
    path.write_text(json.dumps(env, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return non_terminal


def rewrite_verify_artifacts(run_dir: str | Path, settlement: Settlement
                             ) -> None:
    """Re-stamp finished_walk.json and change_verify.json for the FINAL build:
    every residual/problem the loop ever saw, each with its settlement action,
    so certify's completeness check reads one file per gate and the record
    beside it. `generated_at` is fresh so the stale check passes for this
    build."""
    run = Path(run_dir)
    latest = settlement.latest()
    seen_res: dict[str, dict[str, Any]] = {}
    seen_prob: dict[str, dict[str, Any]] = {}
    for item in settlement.residuals_seen:
        rid = str(item.get("residual_id", ""))
        rec = latest.get(rid)
        d = dict(item)
        d["settled"] = rec.action if rec else None
        d["settlement_reason"] = rec.reason if rec else ""
        if item.get("kind") == "edit_damage":
            d.setdefault("problem_id", rid)
            d.setdefault("original_text", "")
            seen_prob[rid] = d
        else:
            d.setdefault("residual_id", rid)
            seen_res[rid] = d
    for name, key, rows in (("finished_walk.json", "residuals", seen_res),
                            ("change_verify.json", "problems", seen_prob)):
        p = run / name
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload["generated_at"] = _now()
        payload["ran"] = True
        payload["settled"] = True
        payload["settlement_rounds"] = settlement.rounds
        payload[key] = list(rows.values())
        payload["unsettled"] = [r.get("residual_id") or r.get("problem_id")
                                for r in rows.values() if not r.get("settled")]
        p.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                     encoding="utf-8")


def unsettled(run_dir: str | Path) -> tuple[list[str], list[str]]:
    """(residual ids, problem ids) present in the verify artifacts with NO
    settlement record — certify's definition of "open"."""
    run = Path(run_dir)
    settled = Settlement.load(run)
    have = settled.record_ids() if settled else set()
    open_res: list[str] = []
    open_prob: list[str] = []
    for name, kind, out in (("finished_walk.json", "residual", open_res),
                            ("change_verify.json", "edit_damage", open_prob)):
        try:
            payload = json.loads((run / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows = payload.get("residuals" if kind == "residual" else "problems") \
            or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = Residual.from_walk(row) if kind == "residual" \
                else Residual.from_problem(row)
            if item.id not in have and not row.get("settled"):
                out.append(item.id)
    return open_res, open_prob


__all__ = [
    "ACTIONS", "DEFAULT_ROUNDS", "DEFAULT_QUIET_FLOOR", "DEFAULT_QUIET_SHARE",
    "DEFAULT_MAX_TURNS", "HARD_MAX_ROUNDS", "Decision", "Folded", "NON_TERMINAL",
    "Residual", "SETTLEMENT_NAME", "SETTLE_TYPE", "SettleOptions",
    "SettleResult", "Settlement", "SettlementRecord", "Settler",
    "TERMINAL_STATES", "apply_decision", "decide", "fold_accepted_rows",
    "artifact_in", "deletes_an_aside", "judge", "kept_rows",
    "looks_like_instruction",
    "align_to_sentence",
    "open_items", "resolve", "restore_rows", "rewrite_verify_artifacts",
    "stamp_states", "terminal_state", "unsettled",
]
