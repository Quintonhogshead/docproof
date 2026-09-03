"""The terminal verdict: a Galley run ends at exactly one of two stopping
points, and says which in a file DocWatch can read to flip the HubSpot
toggle.

  done         — this book has no more errors in it that the loop can find or
                 decide; every finding is applied, dropped, or an author
                 question; the deliverable certified.
  needs_human  — this book needs a human proofreader, and here is why. Reserved
                 for the case where the book itself has major grammatical
                 problems and most of its sentences must be rewritten — a job
                 no mechanical proofread should pretend to have finished. The
                 evidence is the run's own numbers, and the reason names them.

`assess` reads a finished (ideally settled) run and applies fixed thresholds;
`galley outcome --set` lets a practitioner or a human overrule with a stated
reason. Either way outcome.json is written beside findings.json with the
HubSpot property/value the watch can PATCH verbatim (app/watch/tick.py owns
the write; this module never calls HubSpot).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

OUTCOME_NAME = "outcome.json"
OUTCOMES = ("done", "needs_human")

# HubSpot: the DocProof gate targets the Projects object (0-970), property
# `docproof`; option values equal their labels verbatim. The two values below
# are the defaults for the proofing stage — configurable per call.
HUBSPOT_OBJECT = "0-970"
HUBSPOT_PROPERTY = "docproof"
DEFAULT_DONE_VALUE = "Proofing Complete"
DEFAULT_NEEDS_HUMAN_VALUE = "Needs Human Proofreader"


@dataclass
class Thresholds:
    """When a book is beyond a mechanical proofread. Each is a share or a
    rate; the FIRST one crossed names the reason."""

    # Share of reviewable paragraphs that needed a rewrite-class touch: a
    # repair cluster, three or more WORDING edits, or two or more real
    # residuals after every lane. "Most sentences must be rewritten."
    rewrite_share: float = 0.50
    # WORDING edits per 1,000 words — edits that change words, not the
    # punctuation/spacing/case mechanics house style generates by the
    # thousand on a sound book. Redding Book 1 carried 61 edits per 1,000
    # words of which 46% were mechanics; counting those as "rewriting"
    # flagged a proofread-shaped book as needing a human. 60 wording edits
    # per 1,000 is roughly one reworded phrase every 17 words.
    edit_density_per_kword: float = 60.0
    # Questions the settle loop had to leave for the author because it could
    # not decide, as a share of reviewable paragraphs.
    unresolved_share: float = 0.25
    # Verifier problems (edit damage) as a share of applied edits — the lanes
    # themselves cannot hold the book.
    damage_share: float = 0.20


@dataclass
class Outcome:
    outcome: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
    hubspot: dict[str, str] = field(default_factory=dict)
    set_by: str = "assess"
    generated_at: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"schema_version": 1,
                "generated_at": self.generated_at or _now(),
                "outcome": self.outcome, "reason": self.reason,
                "evidence": dict(self.evidence), "hubspot": dict(self.hubspot),
                "set_by": self.set_by}

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "Outcome":
        return cls(outcome=str(d.get("outcome", "")),
                   reason=str(d.get("reason", "")),
                   evidence=dict(d.get("evidence") or {}),
                   hubspot=dict(d.get("hubspot") or {}),
                   set_by=str(d.get("set_by", "")),
                   generated_at=str(d.get("generated_at", "")))

    def save(self, run_dir: str | Path) -> Path:
        self.generated_at = _now()
        p = Path(run_dir) / OUTCOME_NAME
        p.write_text(json.dumps(self.to_json(), indent=2, ensure_ascii=False),
                     encoding="utf-8")
        return p

    @classmethod
    def load(cls, run_dir: str | Path) -> "Outcome | None":
        p = Path(run_dir) / OUTCOME_NAME
        if not p.is_file():
            return None
        try:
            return cls.from_json(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")


def hubspot_fields(outcome: str, *, done_value: str = DEFAULT_DONE_VALUE,
                   needs_human_value: str = DEFAULT_NEEDS_HUMAN_VALUE,
                   prop: str = HUBSPOT_PROPERTY, obj: str = HUBSPOT_OBJECT
                   ) -> dict[str, str]:
    return {"object": obj, "property": prop,
            "value": done_value if outcome == "done" else needs_human_value}


def non_reviewable_ids(run_dir: str | Path, cfg: Any = None, *,
                       texts: Mapping[str, str] | None = None) -> set[str]:
    """The deliverable's paragraphs the review never READS: what
    ``docproof.ingest.build_document_model`` leaves out of the model (a fully
    skipped style — title, subtitle, TOC, caption) or keeps with
    ``reviewable=False`` (a sweep-only heading; a line under the configured
    character floor). The same predicates, evaluated over the deliverable's
    styles, so every per-paragraph share in :func:`evidence_of` is taken over
    the reviewable prose, as :class:`Thresholds` documents — a book's title
    page, contents and chapter heads must not pad the denominator.

    `cfg` is the run's Config (its ``skip`` and ``chunking`` sections decide);
    without one the shipped defaults apply. `texts` are the paragraphs' texts
    (the original view) for the character floor; only consulted when a floor
    is set. Empty when there is no deliverable."""
    from galley.verify import paragraph_styles
    try:
        styles = paragraph_styles(run_dir)
    except Exception:                                      # noqa: BLE001
        return set()
    if cfg is not None:
        skip = cfg.skip
        floor = int(getattr(cfg.chunking, "min_paragraph_chars", 0) or 0)
    else:
        from docproof.config import SkipConfig
        skip, floor = SkipConfig(), 0
    out: set[str] = set()
    for pid, style in styles.items():
        if skip.fully_skipped(style) or skip.is_sweep_only(style):
            out.add(pid)
        elif floor and texts is not None \
                and len(texts.get(pid, "")) < floor:
            out.add(pid)
    return out


def evidence_of(run_dir: str | Path, source_paras: Mapping[str, str] | None
                = None, *, cfg: Any = None) -> dict[str, Any]:
    """The numbers the verdict is made from, all read from the run dir:
    findings.json (applied edits, clusters, queries), settlement.json (what the
    loop could not decide), change_verify.json (edit damage), and the accepted
    text (words, paragraphs) — from `source_paras` when given, else from the
    deliverable. Either way the paragraphs counted are the REVIEWABLE ones
    (:func:`non_reviewable_ids`, under `cfg`'s skip rules): a heading, a
    title page or a contents entry is never the model's to rewrite, so it
    neither pads a share's denominator nor counts toward its numerator."""
    run = Path(run_dir)
    try:
        env = json.loads((run / "findings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        env = {}
    rows = [r for r in (env.get("findings") or []) if isinstance(r, dict)]
    try:
        from galley.verify import paragraph_views
        orig_view, _acc_view = paragraph_views(run)
    except Exception:                                      # noqa: BLE001
        orig_view = {}
    if source_paras is None:
        source_paras = orig_view
    paras = {pid: t for pid, t in (source_paras or {}).items() if t.strip()}
    excluded = non_reviewable_ids(run, cfg, texts=orig_view)
    n_excluded = sum(1 for pid in paras if pid in excluded)
    paras = {pid: t for pid, t in paras.items() if pid not in excluded}
    n_paras = len(paras)
    words = sum(len(_WORD_RE.findall(t)) for t in paras.values())

    applied = [r for r in rows if r.get("applied") is True
               and not r.get("format")
               and r.get("original_text") != r.get("corrected_text")]
    queries = [r for r in rows if r.get("queried") is True]
    edits_per_para: dict[str, int] = {}
    cluster_paras: set[str] = set()
    from galley.settle import edit_kind
    wording = 0
    for r in applied:
        pid = str(r.get("para_id", ""))
        a = r.get("anchor") or {}
        kind = edit_kind(str(a.get("delete_text", "")),
                         str(a.get("insert_text", ""))) if a else "wording"
        if kind == "wording":
            wording += 1
            edits_per_para[pid] = edits_per_para.get(pid, 0) + 1
        if r.get("cluster_id"):
            cluster_paras.add(pid)

    from galley.settle import Settlement
    settlement = Settlement.load(run)
    unresolved = 0
    # Residuals per paragraph — only the ones that were REAL (absorbed, added,
    # revised, or left as a question); a dropped residual was a false alarm.
    residuals_per_para: dict[str, int] = {}
    damage = 0
    if settlement is not None:
        for rec in settlement.latest().values():
            if rec.action == "query" and rec.reason.startswith(
                    "unresolved_after_"):
                unresolved += 1
            if rec.kind == "edit_damage":
                damage += 1
            if rec.para_id and rec.action != "drop":
                residuals_per_para[rec.para_id] = \
                    residuals_per_para.get(rec.para_id, 0) + 1
    else:
        try:
            fw = json.loads((run / "finished_walk.json").read_text("utf-8"))
            for r in fw.get("residuals") or []:
                if isinstance(r, dict):
                    pid = str(r.get("para_id", ""))
                    residuals_per_para[pid] = residuals_per_para.get(pid, 0) + 1
        except (OSError, ValueError):
            pass
        try:
            cv = json.loads((run / "change_verify.json").read_text("utf-8"))
            damage = len(cv.get("problems") or [])
        except (OSError, ValueError):
            pass

    # A paragraph needed rewrite-class work when a repair cluster touched it,
    # it took three or more edits, or the walk still found two or more real
    # residuals in it after every lane. One residual is a slip, not a rewrite.
    residual_paras = {pid for pid, n in residuals_per_para.items() if n >= 2}
    # Missed vs introduced: a residual whose quoted text reads the same in
    # the author's original was MISSED by every lane; one whose text exists
    # only after the edits was introduced or altered by an edit. The second
    # class is what the change verifier exists for, so its size is a lane
    # health signal as much as a book signal.
    missed = introduced = 0
    if settlement is not None:
        seen = settlement.residuals_seen
    else:
        try:
            seen = json.loads((run / "finished_walk.json").read_text("utf-8")
                              ).get("residuals") or []
        except (OSError, ValueError):
            seen = []
    unanchorable = set()
    if settlement is not None:
        unanchorable = {rid for rid, rec in settlement.latest().items()
                        if rec.reason == "unanchorable"}
    for item in seen:
        if not isinstance(item, dict) or item.get("kind") == "edit_damage":
            continue
        if str(item.get("residual_id", "")) in unanchorable:
            continue
        pid, q = str(item.get("para_id", "")), str(item.get("quote", ""))
        if not q:
            continue
        # Classified against the author's ORIGINAL only (the reject view is
        # the same before and after settlement); the accepted view is not
        # consulted because a settled residual no longer reads there.
        if q in orig_view.get(pid, ""):
            missed += 1
        else:
            introduced += 1

    rewrite_paras = (cluster_paras | residual_paras
                     | {pid for pid, n in edits_per_para.items() if n >= 3})
    rewrite_paras = {p for p in rewrite_paras if p in paras} if paras \
        else rewrite_paras
    return {
        "paragraphs": n_paras, "paragraphs_not_reviewable": n_excluded,
        "words": words,
        "applied_edits": len(applied), "wording_edits": wording,
        "mechanical_edits": len(applied) - wording, "queries": len(queries),
        "edit_density_per_kword": (wording / words * 1000.0) if words
        else 0.0,
        "all_edits_per_kword": (len(applied) / words * 1000.0) if words
        else 0.0,
        "rewrite_paragraphs": len(rewrite_paras),
        "rewrite_share": (len(rewrite_paras) / n_paras) if n_paras else 0.0,
        "repair_cluster_paragraphs": len(cluster_paras),
        "unresolved_queries": unresolved,
        "unresolved_share": (unresolved / n_paras) if n_paras else 0.0,
        "edit_damage": damage,
        "damage_share": (damage / len(applied)) if applied else 0.0,
        "residuals_missed": missed,
        "residuals_introduced": introduced,
        "settled": settlement is not None,
        "settle_rounds": settlement.rounds if settlement else 0,
        "open_items": len(settlement.open) if settlement else None,
        "convergence": dict(settlement.convergence) if settlement else {},
    }


def assess(run_dir: str | Path, *, thresholds: Thresholds | None = None,
           source_paras: Mapping[str, str] | None = None, cfg: Any = None,
           done_value: str = DEFAULT_DONE_VALUE,
           needs_human_value: str = DEFAULT_NEEDS_HUMAN_VALUE) -> Outcome:
    """The verdict from the run's own numbers. `needs_human` only when the
    book is beyond a mechanical proofread by one of the thresholds; the reason
    names the number that crossed. `cfg` (the run's Config) decides which
    paragraphs are reviewable — see :func:`evidence_of`."""
    th = thresholds or Thresholds()
    ev = evidence_of(run_dir, source_paras, cfg=cfg)
    reasons: list[str] = []
    if ev["paragraphs"] and ev["rewrite_share"] >= th.rewrite_share:
        reasons.append(
            f"{ev['rewrite_paragraphs']} of {ev['paragraphs']} reviewable "
            f"paragraphs ({ev['rewrite_share']:.0%}) needed rewrite-class "
            f"work (a repair "
            f"cluster, 3+ edits, or 2+ residuals after every lane) — most "
            f"sentences must be rewritten, which is a human proofreader's job")
    if ev["words"] and ev["edit_density_per_kword"] >= th.edit_density_per_kword:
        reasons.append(
            f"{ev['wording_edits']} wording edits over {ev['words']} words "
            f"({ev['edit_density_per_kword']:.0f} per 1,000, mechanics "
            f"excluded) — the manuscript has major grammatical problems "
            f"throughout")
    if ev["paragraphs"] and ev["unresolved_share"] >= th.unresolved_share:
        reasons.append(
            f"{ev['unresolved_queries']} residuals could not be decided after "
            f"{ev['settle_rounds']} settle round(s) and ship as author "
            f"questions ({ev['unresolved_share']:.0%} of reviewable "
            f"paragraphs)")
    conv = ev.get("convergence") or {}
    if conv and conv.get("stopped") in ("turn_budget", "round_cap", "rounds") \
            and not conv.get("quiet", True) and conv.get("rounds", 0) >= 2:
        reasons.append(
            f"the settle sweep was still finding {conv.get('last_new_items')} "
            f"new error(s) per round after {conv.get('rounds')} round(s) "
            f"(re-reading {conv.get('last_reread')} edits and paragraphs) "
            f"when it had to stop — the book is not converging on clean and "
            f"needs a human proofreader")
    if ev["applied_edits"] and ev["damage_share"] >= th.damage_share:
        reasons.append(
            f"the change verifier flagged {ev['edit_damage']} of "
            f"{ev['applied_edits']} applied edits ({ev['damage_share']:.0%}) — "
            f"the lanes cannot hold this text")
    if reasons:
        outcome = "needs_human"
        reason = "; ".join(reasons)
    else:
        outcome = "done"
        reason = (f"no open items: {ev['applied_edits']} tracked edits and "
                  f"{ev['queries']} author questions over {ev['words']} words"
                  + (f"; the finished-text walk raised "
                     f"{ev['residuals_missed']} residual(s) the lanes missed "
                     f"and {ev['residuals_introduced']} an edit introduced or "
                     f"altered, all settled"
                     if (ev["residuals_missed"] or ev["residuals_introduced"])
                     else "")
                  + ("" if ev["settled"] else
                     " (not settled — run `galley settle` before delivery)"))
    return Outcome(outcome=outcome, reason=reason, evidence=ev,
                   hubspot=hubspot_fields(outcome, done_value=done_value,
                                          needs_human_value=needs_human_value),
                   set_by="assess")


__all__ = ["DEFAULT_DONE_VALUE", "DEFAULT_NEEDS_HUMAN_VALUE",
           "HUBSPOT_OBJECT", "HUBSPOT_PROPERTY", "OUTCOMES", "OUTCOME_NAME",
           "Outcome", "Thresholds", "assess", "evidence_of", "hubspot_fields"]
