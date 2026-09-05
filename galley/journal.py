"""Render DECISION_LOG.md from run artifacts, grouped by phase and paragraph.
Use recorded reasons and caller-supplied timestamps for deterministic
output; label missing phase evidence explicitly.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from galley.phases import ALL_PHASES as PHASE_ORDER, COPYEDIT_PHASES as _SCOPED_OUT

JOURNAL_NAME = "DECISION_LOG.md"
#: The hand-off suffix, beside " - letter.md" and " - style-sheet.md".
HANDOFF_SUFFIX = " - decision-log.md"

PHASE_TITLES: dict[str, str] = {
    "profile": "Profile — reading the book before touching it",
    "approve": "Plan gate — what was approved, and why",
    "sweeps": "Sweeps — the deterministic floor",
    "ladder": "Mechanical ladder — every edit, and the reason recorded for it",
    "flights": "Copy-edit flights",
    "audit": "Audit — where a miss would hide",
    "reread": "Wave-2 re-read",
    "verify": "Verify — reading the finished text for sense",
    "settle": "Settle — closing every open item",
    "certify": "Certify — the delivery gate",
    "deliver": "Deliver — the verdict and what shipped",
}

# Match individual G-items, excluding approval ranges such as G1-G8.
_GATE_ITEM_RE = re.compile(r"^\s*(G\d+)(?![\d\-–—])[.:)]?\s+(.*)$")
# Match both driver-written and human-written approval/decline lines.
_APPROVAL_RE = re.compile(r"^\s*(?:approved|declined)\b|\b(?:approved|declined)"
                          r"\s+by\b", re.IGNORECASE)
#: A certify.txt check line: "  [PASS] source hash — 2ba8bdd0cff8…"
_CHECK_RE = re.compile(r"^\s*\[(PASS|FAIL|skip)\]\s*([^—]+?)(?:\s*—\s*(.*))?$")

_MAX_QUOTE = 160


def _clip(text: Any, limit: int = _MAX_QUOTE) -> str:
    """One line of at most ``limit`` characters, safe inside a Markdown table
    cell and a bullet alike."""
    s = " ".join(str(text or "").split())
    s = s.replace("|", "\\|")
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _load(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _rows(envelope: Any) -> list[dict[str, Any]]:
    if not isinstance(envelope, dict):
        return []
    return [r for r in (envelope.get("findings") or []) if isinstance(r, dict)]


def _by_para(rows: Iterable[Mapping[str, Any]]
             ) -> list[tuple[str, list[Mapping[str, Any]]]]:
    """Group rows by paragraph id, sorting groups and their findings
    deterministically.
    """
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row.get("para_id") or "(unplaced)"),
                          []).append(row)
    out = []
    for pid in sorted(groups):
        out.append((pid, sorted(groups[pid],
                                key=lambda r: (str(r.get("finding_id") or ""),
                                               str(r.get("residual_id") or ""),
                                               _clip(r.get("original_text"))))))
    return out


# --- the sources -------------------------------------------------------------

@dataclass
class JournalSources:
    """Every artifact the log is rendered from, loaded once. A missing one is
    ``None``/empty — never an error, because a partial run must still render."""

    run_dir: Path
    workspace: Path | None = None
    envelope: dict[str, Any] | None = None
    driver: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    profile: dict[str, Any] | None = None
    plan: str = ""
    audit: Any | None = None
    change_verify: dict[str, Any] | None = None
    finished_walk: dict[str, Any] | None = None
    settlement: dict[str, Any] | None = None
    certificate: str = ""
    outcome: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, run_dir: str | Path, workspace: str | Path | None = None
             ) -> "JournalSources":
        run = Path(run_dir)
        ws = Path(workspace) if workspace else None
        env = _load(run / "findings.json")
        if env is None:
            env = _load(run / "flights_findings.json")
        certificate = ""
        if ws is not None:
            for name in ("certify.txt", "certify_final.txt"):
                certificate = _text(ws / "runs" / name)
                if certificate:
                    break
        return cls(
            run_dir=run, workspace=ws,
            envelope=env if isinstance(env, dict) else None,
            driver=_load(ws / "runs" / "driver" / "driver.json") if ws else None,
            approval=_load(ws / "approval.json") if ws else None,
            profile=_load(ws / "profile.json") if ws else None,
            plan=_text(ws / "PLAN.md") if ws else "",
            audit=_load(run / "audit.json")
            or (_load(ws / "runs" / "audit.json") if ws else None),
            change_verify=_load(run / "change_verify.json"),
            finished_walk=_load(run / "finished_walk.json"),
            settlement=_load(run / "settlement.json"),
            certificate=certificate,
            outcome=_load(run / "outcome.json")
            or (_load(ws / "deliverable" / "outcome.json") if ws else None),
        )


# --- rendering ---------------------------------------------------------------

class _Doc:
    """A tiny Markdown accumulator, so the section writers read as prose."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def line(self, text: str = "") -> None:
        self.lines.append(text)

    def para(self, text: str) -> None:
        self.lines.append(text)
        self.lines.append("")

    def heading(self, level: int, text: str) -> None:
        if self.lines and self.lines[-1] != "":
            self.lines.append("")
        self.lines.append(f"{'#' * level} {text}")
        self.lines.append("")

    def bullet(self, text: str) -> None:
        self.lines.append(f"- {text}")

    def end_list(self) -> None:
        if self.lines and self.lines[-1] != "":
            self.lines.append("")

    def render(self) -> str:
        out: list[str] = []
        for ln in self.lines:
            if ln == "" and out and out[-1] == "":
                continue
            out.append(ln)
        return "\n".join(out).rstrip() + "\n"


def render_journal(run_dir: str | Path, *, workspace: str | Path | None = None,
                   book: str = "", generated_at: str = "",
                   sources: JournalSources | None = None) -> str:
    """The whole decision log as Markdown."""
    src = sources or JournalSources.load(run_dir, workspace)
    doc = _Doc()
    title = book or _book_name(src)
    doc.line(f"# Decision log — {title}")
    doc.line("")
    doc.para("Every action Galley took on this book, in order, with the reason "
             "recorded at the time. Rendered from the run's own artifacts by "
             "`docproof galley journal`; nothing here is written by hand.")
    if generated_at:
        doc.para(f"*Run `{src.run_dir}` · generated {generated_at}*")
    else:
        doc.para(f"*Run `{src.run_dir}`*")

    _section_summary(doc, src)
    _section_driver(doc, src)
    for phase in PHASE_ORDER:
        if phase in _SCOPED_OUT and not _has_copyedit_evidence(src, phase):
            continue
        doc.heading(2, f"{PHASE_TITLES[phase]}")
        _SECTIONS[phase](doc, src)
    return doc.render()


def _book_name(src: JournalSources) -> str:
    if isinstance(src.approval, dict) and src.approval.get("source"):
        return Path(str(src.approval["source"])).name
    if isinstance(src.envelope, dict) and src.envelope.get("source"):
        return Path(str(src.envelope["source"])).name
    if src.workspace is not None:
        return src.workspace.name
    return src.run_dir.name


def _has_copyedit_evidence(src: JournalSources, phase: str) -> bool:
    if phase == "flights":
        return ((src.run_dir / "flights_findings.json").is_file()
                or any(str(r.get("lane")) == "copyedit" for r in
                       _rows(src.envelope)))
    return bool(src.workspace and (src.workspace / "runs" / "wave2").exists())


def _not_run(doc: _Doc, why: str) -> None:
    doc.para(f"*Not run — {why}.*")


# --- sections ----------------------------------------------------------------

def _section_summary(doc: _Doc, src: JournalSources) -> None:
    doc.heading(2, "At a glance")
    rows = _rows(src.envelope)
    applied = [r for r in rows if r.get("applied") is True]
    queried = [r for r in rows if r.get("queried") is True]
    held = [r for r in rows if r.get("applied") is not True
            and r.get("queried") is not True]
    counts = [
        ("Findings recorded", len(rows)),
        ("Applied as tracked edits", len(applied)),
        ("Author queries", len(queried)),
        ("Withheld / rejected / dropped", len(held)),
    ]
    if isinstance(src.change_verify, dict):
        counts.append(("Edits the change verifier flagged",
                       len(src.change_verify.get("problems") or [])))
    if isinstance(src.finished_walk, dict):
        counts.append(("Residuals the finished-text walk raised",
                       len(src.finished_walk.get("residuals") or [])))
    if isinstance(src.settlement, dict):
        counts.append(("Items settled",
                       len(src.settlement.get("records") or [])))
        counts.append(("Settle rounds", src.settlement.get("rounds", 0)))
    doc.line("| | |")
    doc.line("|---|---|")
    for label, value in counts:
        doc.line(f"| {label} | {value} |")
    doc.line("")
    if isinstance(src.outcome, dict) and src.outcome.get("outcome"):
        doc.para(f"**Outcome: {src.outcome['outcome']}** — "
                 f"{_clip(src.outcome.get('reason'), 400)}")


def _section_driver(doc: _Doc, src: JournalSources) -> None:
    doc.heading(2, "Driver events")
    if not isinstance(src.driver, dict):
        _not_run(doc, "no `runs/driver/driver.json`; this run was driven by "
                      "hand, one phase at a time")
        return
    gate = src.driver.get("gate") or {}
    for entry in src.driver.get("phases") or []:
        if not isinstance(entry, dict):
            continue
        phase = entry.get("phase", "?")
        rc = entry.get("returncode", 0)
        limit = entry.get("limit") or ""
        if limit:
            doc.bullet(f"**{phase}** — STOPPED at its {limit.replace('_', ' ')} "
                       f"cap (exit {rc}); the run ended here.")
        elif rc:
            doc.bullet(f"**{phase}** — FAILED, exit {rc}; the run ended here.")
        else:
            doc.bullet(f"**{phase}** — ran and finished.")
    doc.end_list()
    if gate:
        verdict = "approved" if gate.get("approved") else "refused"
        doc.para(f"**Plan gate ({gate.get('policy', '?')}): {verdict}** — "
                 f"{_clip(gate.get('reason'), 400)}"
                 + (f" Reply: {gate['reply']}." if gate.get("reply") else ""))
    stopped = src.driver.get("stopped_at")
    if stopped:
        doc.para(f"**The run stopped at `{stopped}`:** "
                 f"{_clip(src.driver.get('reason'), 600)}")


def _section_profile(doc: _Doc, src: JournalSources) -> None:
    prof = src.profile
    if not isinstance(prof, dict):
        _not_run(doc, "no `profile.json` in the workspace")
        return
    for key, label in (("genre", "Genre"), ("posture", "Posture"),
                       ("word_count", "Words"), ("words", "Words"),
                       ("chapters", "Chapters"),
                       ("comment_budget", "Comment budget")):
        value = prof.get(key)
        if value is None or isinstance(value, (list, dict)):
            continue
        doc.bullet(f"{label}: {_clip(value)} — as recorded by `/profile`.")
    for key, label, why in (
            ("tics", "Author tics recorded",
             "each becomes a sweep candidate or a protected habit"),
            ("intent_zones", "Intent zones recorded",
             "no-edit zones: nothing inside is corrected"),
            ("bespoke_sweeps", "Bespoke sweeps proposed",
             "taken to the plan gate with counts and samples")):
        value = prof.get(key)
        if isinstance(value, list) and value:
            doc.bullet(f"{label}: {len(value)} — {why}.")
    doc.end_list()
    zones = prof.get("intent_zones")
    if isinstance(zones, list) and zones:
        doc.heading(3, "Intent zones (protected from every lane)")
        for zone in zones:
            if isinstance(zone, dict):
                doc.bullet(f"`{_clip(zone.get('name') or zone.get('id'), 60)}` "
                           f"— {_clip(zone.get('reason') or zone.get('note') or zone.get('why'), 200)}")
            else:
                doc.bullet(_clip(zone, 200))
        doc.end_list()


def _section_approve(doc: _Doc, src: JournalSources) -> None:
    if not src.plan and not isinstance(src.approval, dict):
        _not_run(doc, "no `PLAN.md` and no `approval.json` in the workspace")
        return
    items = []
    approvals = []
    for line in src.plan.splitlines():
        match = _GATE_ITEM_RE.match(line)
        if match and match.group(2):
            items.append((match.group(1), match.group(2)))
        elif _APPROVAL_RE.search(line):
            approvals.append(line.strip())
    if items:
        doc.heading(3, "Gate items put to a person")
        doc.line("| Item | The call asked for |")
        doc.line("|---|---|")
        for key, body in items:
            doc.line(f"| {key} | {_clip(body, 240)} |")
        doc.line("")
    if approvals:
        doc.heading(3, "How the gate was resolved")
        for line in approvals:
            doc.bullet(_clip(line, 400))
        doc.end_list()
    elif items:
        doc.para("*No approval line was recorded in PLAN.md — the gate items "
                 "above were resolved outside it.*")
    if isinstance(src.approval, dict):
        doc.heading(3, "What `approval.json` froze")
        appr = src.approval
        doc.bullet(f"API ceiling: **${float(appr.get('max_spend_usd', 0)):.2f}** "
                   f"— every paid verb refuses past this figure.")
        doc.bullet(f"Stage: `{appr.get('stage') or 'not recorded'}`, genre: "
                   f"`{appr.get('genre') or 'not recorded'}` — which lanes may "
                   f"run at all.")
        doc.bullet(f"Lanes enabled: "
                   f"{', '.join(appr.get('enabled_lanes') or []) or 'none'}"
                   + ("  ·  **mechanical proofreading only**"
                      if appr.get("mechanical_only") else "") + ".")
        doc.bullet(f"Models allowed: "
                   f"{', '.join(appr.get('allowed_models') or []) or 'none'} "
                   f"(providers: "
                   f"{', '.join(appr.get('allowed_providers') or []) or 'none'})"
                   f" — a route outside this set is a refusal, not a warning.")
        doc.end_list()


def _section_sweeps(doc: _Doc, src: JournalSources) -> None:
    env = src.envelope or {}
    checks = [c for c in (env.get("scripted_checks") or [])
              if isinstance(c, dict)]
    norm = env.get("normalization") if isinstance(env.get("normalization"),
                                                  dict) else None
    consistency = env.get("consistency") if isinstance(
        env.get("consistency"), dict) else None
    if not (checks or norm or consistency):
        _not_run(doc, "the run's findings envelope records no sweeps")
        return
    fired = [c for c in checks if (c.get("applied") or 0) or (c.get("flagged") or 0)]
    if fired:
        doc.line("| Sweep | Flagged | Applied | Left as-is |")
        doc.line("|---|---|---|---|")
        for check in sorted(fired, key=lambda c: str(c.get("key"))):
            doc.line(f"| `{check.get('key')}` ({_clip(check.get('name'), 40)}) "
                     f"| {check.get('flagged', 0)} | {check.get('applied', 0)} "
                     f"| {check.get('remaining', 0)} |")
        doc.line("")
    quiet = [str(c.get("key")) for c in checks if c not in fired]
    if quiet:
        doc.para(f"Ran and found nothing: {', '.join(f'`{k}`' for k in sorted(quiet))}.")
    if norm and norm.get("ran"):
        doc.para(f"Normalization: {norm.get('quotes', 0)} quote(s) curled and "
                 f"{norm.get('spaces', 0)} space run(s) collapsed over "
                 f"{norm.get('paragraphs', 0)} paragraph(s) — house typography, "
                 f"applied before any lane read the text.")
    terms = (consistency or {}).get("terms")
    if isinstance(terms, list) and terms:
        doc.heading(3, "Consistency scan — the pairs it found")
        doc.line("| Term | House form chosen | Forms seen | Outliers |")
        doc.line("|---|---|---|---|")
        for term in sorted(terms, key=lambda t: str(t.get("key"))):
            if not isinstance(term, dict):
                continue
            forms = term.get("forms") or {}
            spelled = ", ".join(f"{k} ×{v}" for k, v in
                                sorted(forms.items())) if isinstance(forms, dict) \
                else _clip(forms)
            doc.line(f"| `{term.get('key')}` | {_clip(term.get('dominant'), 40)} "
                     f"| {_clip(spelled, 80)} | {term.get('outliers', 0)} |")
        doc.line("")
    spell = env.get("spell_scan")
    if isinstance(spell, dict) and spell.get("available"):
        doc.para(f"Spell scan: {spell.get('unknown', 0)} unknown token(s) out of "
                 f"{spell.get('unique', 0)} unique — each triaged as an edit, a "
                 f"coined term to protect, or voice to leave alone.")


def _section_ladder(doc: _Doc, src: JournalSources) -> None:
    rows = [r for r in _rows(src.envelope) if str(r.get("lane")) != "copyedit"]
    if not rows:
        _not_run(doc, "no `findings.json` with mechanical rows in the run")
        return
    applied = [r for r in rows if r.get("applied") is True]
    queried = [r for r in rows if r.get("queried") is True]
    held = [r for r in rows if r.get("applied") is not True
            and r.get("queried") is not True]

    doc.heading(3, f"Applied as tracked edits ({len(applied)})")
    if applied:
        doc.para("Every one is rejectable by the author. The *why* is the "
                 "explanation the detector recorded at the time.")
        for pid, group in _by_para(applied):
            doc.line(f"**{pid}**")
            doc.line("")
            for row in group:
                doc.bullet(_edit_bullet(row))
            doc.end_list()
    else:
        doc.para("*None.*")

    doc.heading(3, f"Author queries ({len(queried)})")
    if queried:
        doc.para("A query is a last resort: it is asked only when the answer "
                 "needs knowledge the loop cannot have.")
        for pid, group in _by_para(queried):
            doc.line(f"**{pid}**")
            doc.line("")
            for row in group:
                doc.bullet(
                    f"`{row.get('finding_id', '?')}` "
                    f"({row.get('error_type', '?')}) on "
                    f"\"{_clip(row.get('original_text'))}\" — "
                    f"{_clip(row.get('explanation'), 400) or 'no reason recorded'}")
            doc.end_list()
    else:
        doc.para("*None.*")

    doc.heading(3, f"Raised and NOT applied ({len(held)})")
    if held:
        doc.para("The status is the reason: a duplicate of an edit already "
                 "made, an overlap with another edit's span, a row the "
                 "validator dropped, or one a gate withheld.")
        by_status: dict[str, list[Mapping[str, Any]]] = {}
        for row in held:
            key = str(row.get("state") or row.get("status") or "unknown")
            by_status.setdefault(key, []).append(row)
        for status in sorted(by_status):
            group = by_status[status]
            doc.line(f"**{status}** — {len(group)} row(s)")
            doc.line("")
            for row in sorted(group, key=lambda r: (
                    str(r.get("para_id")), str(r.get("finding_id")))):
                reason = (row.get("disposition_reason")
                          or row.get("explanation") or "")
                doc.bullet(
                    f"`{row.get('finding_id', '?')}` {row.get('para_id', '?')} "
                    f"({row.get('error_type', '?')}): "
                    f"\"{_clip(row.get('original_text'), 80)}\" → "
                    f"\"{_clip(row.get('corrected_text'), 80)}\""
                    + (f" — {_clip(reason, 240)}" if reason else ""))
            doc.end_list()
    else:
        doc.para("*None — every finding either shipped or was asked.*")


def _edit_bullet(row: Mapping[str, Any]) -> str:
    why = _clip(row.get("explanation"), 400) or "no reason recorded"
    return (f"`{row.get('finding_id', '?')}` ({row.get('error_type', '?')}): "
            f"\"{_clip(row.get('original_text'), 120)}\" → "
            f"\"{_clip(row.get('corrected_text'), 120)}\" — {why}")


def _section_flights(doc: _Doc, src: JournalSources) -> None:
    rows = [r for r in _rows(src.envelope) if str(r.get("lane")) == "copyedit"]
    if not rows:
        _not_run(doc, "no copy-edit findings in the run")
        return
    doc.para(f"{len(rows)} copy-edit finding(s). Go-live scope is mechanical "
             f"proofreading only, so these are here because this run was "
             f"explicitly opened to the lane.")
    for pid, group in _by_para(rows):
        doc.line(f"**{pid}**")
        doc.line("")
        for row in group:
            doc.bullet(_edit_bullet(row))
        doc.end_list()


def _section_audit(doc: _Doc, src: JournalSources) -> None:
    audit = src.audit
    if not audit:
        _not_run(doc, "no `audit.json` for this run")
        return
    payload = audit if isinstance(audit, dict) else {"hypotheses": audit}
    hypotheses = payload.get("hypotheses") or payload.get("findings") or []
    doc.para("The audit reads the quietest chapters, where a miss hides, and "
             "records what it suspects. On a mechanical run the hypotheses are "
             "recorded and no paid re-read follows.")
    if isinstance(hypotheses, list) and hypotheses:
        for item in hypotheses:
            if isinstance(item, dict):
                what = (item.get("hypothesis") or item.get("claim")
                        or item.get("quote") or item.get("summary"))
                why = (item.get("reason") or item.get("evidence")
                       or item.get("explanation") or "")
                where = item.get("chapter") or item.get("para_id") or ""
                doc.bullet(f"{f'{where}: ' if where else ''}{_clip(what, 240)}"
                           + (f" — {_clip(why, 240)}" if why else ""))
            else:
                doc.bullet(_clip(item, 240))
        doc.end_list()
    else:
        doc.para("*The audit recorded no hypotheses — a quiet audit converges "
                 "the loop.*")


def _section_reread(doc: _Doc, src: JournalSources) -> None:
    # Copyediting evidence is required before rendering this section.
    wave2 = src.workspace / "runs" / "wave2" if src.workspace else None
    if wave2 is None or not wave2.exists():
        _not_run(doc, "the gated wave-2 re-read is copy-edit scope and out of "
                      "scope for a mechanical run")
        return
    doc.para(f"A targeted wave-2 re-read ran to `{wave2}`. Its findings are "
             f"folded into the ladder section above (they are the same "
             f"envelope); the hypotheses it was scoped from are in the audit "
             f"section.")


def _section_verify(doc: _Doc, src: JournalSources) -> None:
    cv, walk = src.change_verify, src.finished_walk
    if not isinstance(cv, dict) and not isinstance(walk, dict):
        _not_run(doc, "no `change_verify.json` or `finished_walk.json` in the "
                      "run — the finished text was never read for sense")
        return
    if isinstance(cv, dict):
        problems = [p for p in (cv.get("problems") or []) if isinstance(p, dict)]
        doc.heading(3, f"Change verifier — {len(problems)} edit(s) flagged")
        doc.para(f"Re-read every one of {cv.get('applied_edits', '?')} applied "
                 f"edit(s) in place, on `{cv.get('engine', '?')}` "
                 f"(`{cv.get('model') or 'no model'}`), asking only whether the "
                 f"edit damaged the sentence.")
        _residual_bullets(doc, problems)
    if isinstance(walk, dict):
        residuals = [r for r in (walk.get("residuals") or [])
                     if isinstance(r, dict)]
        doc.heading(3, f"Finished-text walk — {len(residuals)} residual(s)")
        doc.para(f"Proofread the accepted text as the author will read it, over "
                 f"{walk.get('paragraphs_verified', '?')} paragraph(s). A "
                 f"residual is an error still standing after every lane.")
        unread = walk.get("unread_paragraphs") or []
        if unread:
            doc.para(f"**{len(unread)} paragraph(s) were not read** — they are "
                     f"listed in `finished_walk.json` and were re-run "
                     f"separately.")
        _residual_bullets(doc, residuals)


def _residual_bullets(doc: _Doc, items: Sequence[Mapping[str, Any]]) -> None:
    if not items:
        doc.para("*None.*")
        return
    for pid, group in _by_para(items):
        doc.line(f"**{pid}**")
        doc.line("")
        for item in group:
            settled = item.get("settled")
            why = _clip(item.get("settlement_reason"), 160)
            outcome = ""
            if settled:
                outcome = f"  ·  settled: **{settled}**"
                if why:
                    outcome += f" ({why})"
            doc.bullet(
                f"`{item.get('residual_id', '?')}` "
                f"[{item.get('severity', '?')}] "
                f"\"{_clip(item.get('quote'), 120)}\" — "
                f"{_clip(item.get('problem'), 300) or 'no reason recorded'}"
                + outcome)
        doc.end_list()


def _section_settle(doc: _Doc, src: JournalSources) -> None:
    settlement = src.settlement
    if not isinstance(settlement, dict):
        _not_run(doc, "no `settlement.json` in the run — nothing was settled")
        return
    records = [r for r in (settlement.get("records") or [])
               if isinstance(r, dict)]
    conv = settlement.get("convergence") or {}
    doc.para(f"{len(records)} item(s) closed over "
             f"{settlement.get('rounds', 0)} round(s) on "
             f"`{settlement.get('engine', '?')}`. Every open item ends as an "
             f"ABSORB into the owning edit, an ADD of a new edit, a DROP with a "
             f"reason, a REVISE of the owner, or a QUERY to the author — never "
             f"a note for later.")
    if conv:
        doc.para(f"The sweep stopped because: **{conv.get('stopped', '?')}** "
                 f"(last round found {conv.get('last_new_items', '?')} new "
                 f"item(s) while re-reading {conv.get('last_reread', '?')}).")
    still_open = settlement.get("open") or []
    if still_open:
        doc.para(f"**{len(still_open)} item(s) were still open when the loop "
                 f"stopped** — they ship as author questions.")
    by_round: dict[int, list[Mapping[str, Any]]] = {}
    for rec in records:
        by_round.setdefault(int(rec.get("round", 0) or 0), []).append(rec)
    for rnd in sorted(by_round):
        group = by_round[rnd]
        actions: dict[str, int] = {}
        for rec in group:
            actions[str(rec.get("action"))] = actions.get(
                str(rec.get("action")), 0) + 1
        doc.heading(3, f"Round {rnd} — {len(group)} item(s) ("
                       + ", ".join(f"{k}={v}" for k, v in sorted(actions.items()))
                       + ")")
        for pid, items in _by_para(group):
            doc.line(f"**{pid}**")
            doc.line("")
            for rec in items:
                owner = rec.get("owner_finding_id") or "-"
                doc.bullet(
                    f"`{rec.get('residual_id', '?')}` → **{rec.get('action')}** "
                    f"(owner `{owner}`) — "
                    f"{_clip(rec.get('reason'), 300) or 'no reason recorded'}"
                    + (f"  ·  asked: \"{_clip(rec.get('question'), 200)}\""
                       if rec.get("question") else "")
                    + (f"  ·  \"{_clip(rec.get('before_replacement'), 80)}\" → "
                       f"\"{_clip(rec.get('after_replacement'), 80)}\""
                       if rec.get("after_replacement") else ""))
            doc.end_list()


def _section_certify(doc: _Doc, src: JournalSources) -> None:
    text = src.certificate
    if not text:
        _not_run(doc, "no `runs/certify.txt` in the workspace")
        return
    checks: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        match = _CHECK_RE.match(line)
        if match:
            checks.append((match.group(1), match.group(2).strip(),
                           (match.group(3) or "").strip()))
    if not checks:
        doc.para("*`certify.txt` recorded no checks in the expected form; the "
                 "file is in the workspace.*")
        return
    doc.para("The delivery gate. A FAIL blocks delivery; a skip names what "
             "could not be compared, so a certificate never reads as more than "
             "it checked.")
    doc.line("| Check | Status | What it found |")
    doc.line("|---|---|---|")
    for status, name, detail in checks:
        doc.line(f"| {_clip(name, 60)} | **{status}** | {_clip(detail, 240)} |")
    doc.line("")
    failed = [c for c in checks if c[0] == "FAIL"]
    if failed:
        doc.para(f"**{len(failed)} check(s) failed** — delivery was blocked "
                 f"until each was fixed or the run was handed to a person.")


def _section_deliver(doc: _Doc, src: JournalSources) -> None:
    outcome = src.outcome
    if not isinstance(outcome, dict):
        _not_run(doc, "no `outcome.json` — the run never reached a verdict")
        return
    doc.para(f"**{outcome.get('outcome', '?')}** — "
             f"{_clip(outcome.get('reason'), 800)}")
    doc.para(f"Set by `{outcome.get('set_by', '?')}`. HubSpot: "
             f"{(outcome.get('hubspot') or {}).get('property', '?')} = "
             f"{(outcome.get('hubspot') or {}).get('value', '?')}.")
    evidence = outcome.get("evidence")
    if isinstance(evidence, dict) and evidence:
        doc.heading(3, "The numbers the verdict was made from")
        doc.line("| | |")
        doc.line("|---|---|")
        for key in sorted(evidence):
            value = evidence[key]
            if isinstance(value, dict):
                value = ", ".join(f"{k}={v}" for k, v in sorted(value.items()))
            if isinstance(value, float):
                value = f"{value:.4g}"
            doc.line(f"| {key} | {_clip(value, 200)} |")
        doc.line("")


_SECTIONS = {
    "profile": _section_profile,
    "approve": _section_approve,
    "sweeps": _section_sweeps,
    "ladder": _section_ladder,
    "flights": _section_flights,
    "audit": _section_audit,
    "reread": _section_reread,
    "verify": _section_verify,
    "settle": _section_settle,
    "certify": _section_certify,
    "deliver": _section_deliver,
}


def write_journal(run_dir: str | Path, out_path: str | Path, *,
                  workspace: str | Path | None = None, book: str = "",
                  generated_at: str = "") -> Path:
    """Render the decision log and write it. Returns the path."""
    text = render_journal(run_dir, workspace=workspace, book=book,
                          generated_at=generated_at)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


__all__ = ["HANDOFF_SUFFIX", "JOURNAL_NAME", "PHASE_ORDER", "PHASE_TITLES",
           "JournalSources", "render_journal", "write_journal"]
