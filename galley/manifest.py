"""Build approval manifests, report configured model routes, and certify
delivery evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA_VERSION = 1

# Lanes forbidden by a mechanical-only approval.
COPYEDIT_LANES = ("smoothing", "smoothing_edits", "rewrite")

# Model-id fields end in "model".
_MODEL_FIELD = re.compile(r"(^|_)(model)$")

# Artifact patterns, with literal periods escaped. An ellipsis plus one
# sentence-ending period is valid house style; two periods indicate an
# artifact.
_ARTIFACTS = tuple(re.compile(p) for p in
                   (",,", '" "', r"…\.{2,}", r"”\.", "”,"))
# Rebuild/import findings may cost zero because paid work is recorded
# elsewhere.
_ZERO_COST_JUDGES = ("external:import-judgments", "galley:rebuild")
# Preserve leading indentation. Flag repeated spaces or NBSP only after
# visible text, matching normalization.
_POST_TEXT_DOUBLE = re.compile(r"(?<=\S)[  ]{2,}")


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# Exclude output locations so approve and review --out share the same config
# hash.
_VOLATILE_CONFIG_FIELDS = ("output_dir",)


def config_hash(cfg: Any) -> str:
    """Hash the effective configuration with sorted keys, excluding volatile
    output-location fields.
    """
    data = cfg.model_dump(mode="json")
    for name in _VOLATILE_CONFIG_FIELDS:
        data.pop(name, None)
    blob = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ModelRoute:
    """One model the config would call: the dotted role path, the model id, the
    provider that serves it, and whether the owning lane is active."""
    role: str
    model: str
    provider: str
    active: bool

    def to_json(self) -> dict[str, Any]:
        return {"role": self.role, "model": self.model,
                "provider": self.provider, "active": self.active}


def model_routes(cfg: Any) -> list[ModelRoute]:
    """List configured models, providers, and lane activity. Traverse sections
    recursively; handle reviewer and ensemble activity explicitly.
    """
    from docproof.providers.catalog import provider_for

    routes: list[ModelRoute] = []
    seen: set[tuple[str, str]] = set()

    def add(role: str, model: Any, active: bool) -> None:
        if not isinstance(model, str) or not model:
            return
        key = (role, model)
        if key in seen:
            return
        seen.add(key)
        routes.append(ModelRoute(role, model, provider_for(model, "unknown"),
                                 active))

    # The reviewer is inactive when no typed passes are configured.
    add("api.model", getattr(getattr(cfg, "api", None), "model", None),
        bool(getattr(cfg, "error_type_keys", True)))

    # The ensemble: active detectors + the verifier when it actually verifies.
    ens = getattr(cfg, "ensemble", None)
    if ens is not None and getattr(ens, "enabled", False):
        for d in ens.detectors:
            add(f"ensemble.detector[{d.model}]", d.model, True)
        if getattr(ens, "verifies", False):
            add("ensemble.verifier", ens.verifier_model, True)
    elif ens is not None and ens.verifier_model:
        add("ensemble.verifier", ens.verifier_model, False)

    # Everything else: recurse, honouring each section's own `enabled` flag.
    from pydantic import BaseModel

    def walk(obj: Any, path: str, active: bool) -> None:
        if not isinstance(obj, BaseModel):
            return
        for name in type(obj).model_fields:
            if path == "" and name in ("api", "ensemble"):
                continue                      # handled explicitly above
            value = getattr(obj, name, None)
            child_path = f"{path}.{name}" if path else name
            if isinstance(value, BaseModel):
                child_active = active and bool(getattr(value, "enabled", True))
                # Candidate screening uses mode; the rounds judge runs only
                # between multiple rounds.
                mode = getattr(value, "mode", None)
                if isinstance(mode, str):
                    child_active = child_active and mode != "off"
                if name == "rounds":
                    child_active = (child_active
                                    and getattr(value, "count", 1) > 1)
                walk(value, child_path, child_active)
            elif _MODEL_FIELD.search(name) and isinstance(value, str) and value:
                add(child_path, value, active)

    walk(cfg, "", True)
    return routes


def providers_in_use(cfg: Any, *, active_only: bool = True) -> list[str]:
    """The sorted set of providers the config would send material to."""
    return sorted({r.provider for r in model_routes(cfg)
                   if r.active or not active_only})


def build_manifest(*, source: str | Path, config_path: str | Path, cfg: Any,
                   max_spend_usd: float, stage: str | None = None,
                   genre: str | None = None,
                   allowed_providers: list[str] | None = None,
                   mechanical_only: bool = False,
                   comment_budget: int | None = None,
                   note: str = "") -> dict[str, Any]:
    """Build an approval manifest. Default allowed_providers to active
    configured providers; reject mechanical_only when copyediting lanes are
    enabled.
    """
    if mechanical_only:
        live = [lane for lane in _enabled_lanes(cfg) if lane in COPYEDIT_LANES]
        if live:
            raise ValueError(
                f"a mechanical-only approval cannot be written over a config "
                f"that enables the copy-edit lane(s) {', '.join(live)} — "
                f"compose the config with `--stage mechanical-wave`, which "
                f"locks them shut")
    routes = model_routes(cfg)
    active_models = sorted({r.model for r in routes if r.active})
    providers = (sorted(set(allowed_providers)) if allowed_providers is not None
                 else providers_in_use(cfg, active_only=True))
    lanes = _enabled_lanes(cfg)
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "source": str(source),
        "source_sha256": sha256_file(source),
        "config_path": str(config_path),
        "config_sha256": config_hash(cfg),
        "stage": stage,
        "genre": genre,
        "max_spend_usd": float(max_spend_usd),
        "allowed_models": active_models,
        "allowed_providers": providers,
        "enabled_lanes": lanes,
        "mechanical_only": bool(mechanical_only),
        # The margin-comment ceiling the run promised (about one per thousand
        # words unless the plan says otherwise); certify fails a deliverable
        # that carries more. None = not frozen here; certify then reads the
        # workspace profile's figure.
        "comment_budget": int(comment_budget) if comment_budget else None,
        "routes": [r.to_json() for r in routes],
        "note": note,
    }


def _enabled_lanes(cfg: Any) -> list[str]:
    """The stylistic/recall lanes the config turns on — the lanes an approval
    freezes so a later run cannot quietly enable another."""
    lanes: list[str] = []
    def on(path: str) -> bool:
        obj = cfg
        for part in path.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                return False
        return bool(obj)
    for name, path in (("ensemble", None), ("smoothing", "smoothing.enabled"),
                       ("smoothing_edits", "smoothing.edits"),
                       ("rewrite", "rewrite.enabled"),
                       ("repair", "repair.enabled"),
                       ("continuity", "continuity.enabled"),
                       ("factcheck", "factcheck.enabled"),
                       ("sapling", "sapling.enabled"),
                       ("chapter_sweep", "chapter_sweep.enabled")):
        if name == "ensemble":
            if getattr(getattr(cfg, "ensemble", None), "enabled", False):
                lanes.append("ensemble")
        elif on(path):
            lanes.append(name)
    return lanes


@dataclass
class Deviation:
    kind: str
    detail: str

    def to_json(self) -> dict[str, str]:
        return {"kind": self.kind, "detail": self.detail}


def verify_plan(manifest: dict[str, Any], *, source: str | Path, cfg: Any,
                config_path: str | Path | None = None,
                budget_usd: float | None = None) -> list[Deviation]:
    """Return deviations from the approved source, config, model routes, and
    budget. Paid commands must reject a nonempty result.
    """
    devs: list[Deviation] = []

    try:
        src_hash = sha256_file(source)
    except OSError as e:
        return [Deviation("source_missing", str(e))]
    if src_hash != manifest.get("source_sha256"):
        devs.append(Deviation(
            "source_changed",
            f"{source} hashes {src_hash[:12]}…, approval has "
            f"{str(manifest.get('source_sha256'))[:12]}…"))

    cfg_hash = config_hash(cfg)
    if cfg_hash != manifest.get("config_sha256"):
        devs.append(Deviation(
            "config_changed",
            f"effective config hashes {cfg_hash[:12]}…, approval has "
            f"{str(manifest.get('config_sha256'))[:12]}…"))

    allowed_models = set(manifest.get("allowed_models") or [])
    allowed_providers = set(manifest.get("allowed_providers") or [])
    for r in model_routes(cfg):
        if not r.active:
            continue
        if allowed_models and r.model not in allowed_models:
            devs.append(Deviation(
                "model_not_approved",
                f"{r.role} routes to {r.model} ({r.provider}), not in the "
                f"approved set"))
        if allowed_providers and r.provider not in allowed_providers:
            devs.append(Deviation(
                "provider_not_approved",
                f"{r.role} would send material to {r.provider}, a provider "
                f"the approval does not allow"))

    if budget_usd is not None:
        cap = float(manifest.get("max_spend_usd", 0.0))
        if budget_usd > cap:
            devs.append(Deviation(
                "budget_over_cap",
                f"planned spend ${budget_usd:.2f} exceeds the approved "
                f"${cap:.2f}"))
    return devs



@dataclass
class Check:
    name: str
    status: str          # "pass" | "fail" | "skip" | "warn" (never blocks)
    detail: str = ""
    # Required checks must pass. Skipped mandatory checks mean missing
    # evidence.
    required: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status,
                "detail": self.detail, "required": self.required}


# Required delivery evidence.
MANDATORY_CHECKS = ("change verifier", "finished-text walk",
                    "residual settlement", "outcome", "run state")


def _required(check: Check) -> Check:
    return Check(check.name, check.status, check.detail, required=True)


@dataclass
class Certificate:
    checks: list[Check] = field(default_factory=list)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == "fail"]

    @property
    def missing(self) -> list[Check]:
        """Required checks that did not PASS (skipped, warned, or failed):
        what stands between this run and delivery."""
        return [c for c in self.checks if c.required and c.status != "pass"]

    @property
    def passed(self) -> bool:
        return not self.failed and not self.missing

    def to_json(self) -> dict[str, Any]:
        return {"passed": self.passed,
                "checks": [c.to_json() for c in self.checks],
                "failed": len(self.failed),
                "missing": [c.name for c in self.missing]}


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# Reject verification older than its findings. Prefer generated_at
# timestamps; fall back to file mtimes.
_STALE = "stale — regenerate with `galley verify`"


def _generated_at(payload: Any) -> str:
    if isinstance(payload, dict):
        value = payload.get("generated_at")
        if isinstance(value, str) and value:
            return value
    return ""


def _is_stale(run: Path, artifact: str, payload: Any) -> str:
    """Why this artifact is out of date with the run's findings, or ""."""
    findings = run / "findings.json"
    if not findings.exists():
        findings = run / "flights_findings.json"
    if not findings.exists():
        return ""
    made = _generated_at(payload)
    findings_made = _generated_at(_load_json(findings))
    if made and findings_made:
        # ISO-8601 UTC strings from the same writer compare lexicographically.
        return (f"{artifact} was written {made}, before the run's "
                f"{findings.name} ({findings_made})") if made < findings_made \
            else ""
    try:
        if (run / artifact).stat().st_mtime < findings.stat().st_mtime:
            return (f"{artifact} is older than the run's {findings.name}")
    except OSError:
        return ""
    return ""


def certify_run(run_dir: str | Path, *, manifest: dict[str, Any] | None = None,
                cfg: Any | None = None, source: str | Path | None = None
                ) -> Certificate:
    """Check delivery evidence. Record absent artifacts as skipped;
    certification requires every mandatory check to pass and no failures.
    """
    run = Path(run_dir)
    cert = Certificate()

    # Zero-cost replay may use a different config from the approved paid
    # wave.
    early_envelope = _load_json(run / "findings.json")
    if early_envelope is None:
        early_envelope = _load_json(run / "flights_findings.json")
    _cost = ((early_envelope or {}).get("cost") or {}).get("total_usd")
    run_paid = bool(_cost) and float(_cost) > 0

    # 1. Check source and config hashes; record missing comparison inputs as
    # skips.
    if manifest is None:
        cert.checks.append(Check("source hash", "skip",
                                 "no approval manifest given — the manuscript "
                                 "was not compared against an approval"))
    elif source is None:
        cert.checks.append(Check("source hash", "skip",
                                 "no --source given — the manuscript hash was "
                                 "not compared against the approval"))
    else:
        cert.checks.append(_check_hash("source hash", sha256_file(source),
                                       manifest.get("source_sha256")))
    if manifest is None:
        cert.checks.append(Check("config hash", "skip",
                                 "no approval manifest given — the config was "
                                 "not compared against an approval"))
    elif cfg is None:
        cert.checks.append(Check("config hash", "skip",
                                 "no --config given — the effective config "
                                 "was not compared against the approval"))
    else:
        hash_check = _check_hash("config hash", config_hash(cfg),
                                 manifest.get("config_sha256"))
        if hash_check.status == "fail" and not run_paid:
            hash_check = Check(
                "config hash", "skip",
                f"{hash_check.detail} — a $0 replay rebuild runs its own "
                f"config; the approved hash governs the paid wave (routes "
                f"still checked below)")
        cert.checks.append(hash_check)

    # 2. Check model routes against the approved set.
    if manifest is None or cfg is None:
        cert.checks.append(Check("approved model routes", "skip",
                                 "no approval manifest and --config to compare "
                                 "the active routes against"))
    else:
        allowed_models = set(manifest.get("allowed_models") or [])
        allowed_providers = set(manifest.get("allowed_providers") or [])
        offenders: list[str] = []
        for r in model_routes(cfg):
            if not r.active:
                continue
            if allowed_models and r.model not in allowed_models:
                offenders.append(f"{r.role}->{r.model}")
            elif allowed_providers and r.provider not in allowed_providers:
                offenders.append(f"{r.role}->{r.provider}")
        cert.checks.append(Check(
            "approved model routes",
            "fail" if offenders else "pass",
            ("routes outside approval: " + ", ".join(offenders)) if offenders
            else "all active routes within the approved set"))

    # 3. Findings envelope: checkpoint present, no zero-cost anomaly, budget.
    envelope = early_envelope
    if envelope is None:
        cert.checks.append(Check("findings envelope", "skip",
                                 "no findings.json in the run directory"))
    else:
        cert.checks.extend(_certify_envelope(envelope, run, manifest))
        cert.checks.append(_certify_reject_all(envelope))

    # 4. Deterministic artifact scan over the change log / findings text.
    cert.checks.append(_artifact_scan(run))
    cert.checks.append(_certify_spellfix_sanity(run))

    # 5. Fuller structural checks over the findings themselves.
    if envelope is not None:
        cert.checks.append(_certify_no_merged_duplicates(envelope))
        cert.checks.append(_certify_no_insertion_collisions(envelope))
        cert.checks.append(_certify_two_author_attribution(envelope, run))

    # 6. Intent-zone collisions, if the run recorded them.
    cert.checks.append(_certify_intent_zone_collisions(run))

    # 7. Require the run to have reached audited.
    cert.checks.append(_required(_certify_run_state(run)))

    # 8. Scan accepted manuscript whitespace, exempting faults proven to
    # predate the run.
    cert.checks.append(_certify_text_hygiene(run, source))
    # 8b. Report unresolved fields, including skipped TOC paragraphs,
    # without failing.
    cert.checks.append(_certify_field_results(run))

    # 9. Require verification evidence and settlement records for raised
    # items.
    cert.checks.append(_required(_certify_change_verify(run)))
    cert.checks.append(_required(_certify_finished_walk(run)))

    # 10. Require terminal finding states and resolved verification items.
    if envelope is not None:
        cert.checks.append(_certify_terminal_states(envelope))
    cert.checks.append(_required(_certify_settlement(run)))
    cert.checks.append(_required(_certify_outcome(run)))

    # 11. Enforce mechanical-only scope when declared in the approval.
    if manifest is not None and manifest.get("mechanical_only"):
        cert.checks.append(_certify_mechanical_only(manifest, cfg, envelope,
                                                    run))

    # 12. The comment budget is a ceiling, not a suggestion (Georgis
    # head-to-head, 2026-09-04: a 61-comment plan delivered 153). Counted on
    # the delivered document itself when there is one.
    cert.checks.append(_certify_comment_budget(run, manifest, envelope))
    return cert


def comment_budget_for(run: Path, manifest: dict[str, Any] | None
                       ) -> tuple[int | None, str]:
    """The comment ceiling this run promised and where it came from: the
    approval's ``comment_budget``, else the workspace profile's
    ``comment_budget``, else one per thousand words of the profile's word
    count. ``(None, why)`` when nothing states it."""
    if manifest and manifest.get("comment_budget"):
        return int(manifest["comment_budget"]), "approval.json"
    ws = run.parent.parent if run.parent.name == "runs" else run.parent
    prof = _load_json(ws / "profile.json")
    if isinstance(prof, dict):
        if prof.get("comment_budget"):
            try:
                return int(prof["comment_budget"]), "profile.json"
            except (TypeError, ValueError):
                pass
        words = prof.get("word_count") or prof.get("words")
        try:
            if words and float(words) > 0:
                return max(1, math.ceil(float(words) / 1000.0)), \
                    "profile.json word count (1 per 1,000 words)"
        except (TypeError, ValueError):
            pass
    return None, ("no comment_budget in the approval or the workspace profile, "
                  "and no word count to derive one from")


def delivered_comment_count(run: Path) -> int | None:
    """How many margin comments the delivered .docx carries, or None when
    there is no deliverable (or no OOXML tooling) to count on."""
    from galley.verify import deliverable_docx
    path = deliverable_docx(run)
    if path is None:
        return None
    try:
        with zipfile.ZipFile(path) as z:
            if "word/comments.xml" not in z.namelist():
                return 0
            xml = z.read("word/comments.xml")
    except (OSError, zipfile.BadZipFile, KeyError):
        return None
    return len(re.findall(rb"<w:comment\b", xml))


def _certify_comment_budget(run: Path, manifest: dict[str, Any] | None,
                            envelope: dict[str, Any] | None) -> Check:
    budget, origin = comment_budget_for(run, manifest)
    if budget is None:
        return Check("comment budget", "skip", origin)
    count = delivered_comment_count(run)
    where = "on the delivered document"
    if count is None:
        if envelope is None:
            return Check("comment budget", "skip",
                         f"budget {budget} ({origin}) but no deliverable or "
                         f"findings.json to count comments on")
        from galley.settle import terminal_state
        count = 0
        for r in (envelope.get("findings") or []):
            if not isinstance(r, dict):
                continue
            state, reason = terminal_state(r)
            if state == "query" and reason != "withheld":
                count += 1
        where = "query rows in findings.json (no deliverable to count on)"
    if count > budget:
        return Check("comment budget", "fail",
                     f"{count} comment(s) {where} over the ceiling of {budget} "
                     f"({origin}) — collapse same-rule families to one "
                     f"comment, decide what the book itself answers (a "
                     f"verbatim repeat, a dictionary compound, a pronoun the "
                     f"sentence disambiguates), or record a raised budget in "
                     f"the approval with the reason")
    return Check("comment budget", "pass",
                 f"{count} comment(s) {where}, ceiling {budget} ({origin})")


def _certify_mechanical_only(manifest: dict[str, Any], cfg: Any | None,
                             envelope: dict[str, Any] | None, run: Path
                             ) -> Check:
    """The go-live scope gate: no copy-edit lane in the config, no copy-edit
    finding in the deliverable, no flight-deck artifact in the run."""
    problems: list[str] = []
    approved = [lane for lane in (manifest.get("enabled_lanes") or [])
                if lane in COPYEDIT_LANES]
    if approved:
        problems.append(f"the approval itself lists {', '.join(approved)}")
    if cfg is not None:
        live = [lane for lane in _enabled_lanes(cfg) if lane in COPYEDIT_LANES]
        if live:
            problems.append(f"the config enables {', '.join(live)}")
    rows = [r for r in ((envelope or {}).get("findings") or [])
            if isinstance(r, dict)]
    shipped = [r for r in rows
               if (r.get("lane") == "copyedit"
                   or r.get("error_type") == "copyedit")
               and (_row_applied(r) or r.get("queried") is True)]
    if shipped:
        problems.append(f"{len(shipped)} copy-edit finding(s) shipped "
                        f"(e.g. {shipped[0].get('finding_id', '?')})")
    if (run / "flights_findings.json").is_file():
        problems.append("a flight-deck run (flights_findings.json) is in the "
                        "run directory")
    if problems:
        return Check("mechanical only", "fail",
                     "; ".join(problems) + " — this run was approved as "
                     "mechanical proofreading only")
    return Check("mechanical only", "pass",
                 "no copy-edit lane in the config, findings, or artifacts")


def _certify_terminal_states(envelope: dict[str, Any]) -> Check:
    """I1 — terminal states only. Every findings row must read as applied,
    dropped, or query; a `pending` row (never validated) or an explicitly
    non-terminal `state` fails."""
    from galley.settle import TERMINAL_STATES, terminal_state
    rows = [r for r in (envelope.get("findings") or []) if isinstance(r, dict)]
    counts: dict[str, int] = {}
    bad: list[str] = []
    for r in rows:
        state, _reason = terminal_state(r)
        counts[state] = counts.get(state, 0) + 1
        # Rows with no status are legacy/unknown; reject only explicit
        # nonterminal states.
        if state not in TERMINAL_STATES and state != "unknown":
            bad.append(f"{r.get('finding_id', '?')}:{state}")
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    if bad:
        return Check("terminal states", "fail",
                     f"{len(bad)} finding(s) not in a terminal state ("
                     + ", ".join(bad[:6]) + ("…" if len(bad) > 6 else "")
                     + f") — {summary}")
    return Check("terminal states", "pass",
                 f"every finding applied/dropped/query ({summary or 'no rows'})")


def _certify_settlement(run: Path) -> Check:
    """Check that settlement records resolve every verification item. Skip when
    verification evidence is absent.
    """
    from galley.settle import Settlement, unsettled
    have_walk = (run / "finished_walk.json").is_file()
    have_cv = (run / "change_verify.json").is_file()
    settlement = Settlement.load(run)
    open_res, open_prob = unsettled(run)
    n_open = len(open_res) + len(open_prob)
    if settlement is None:
        if not (have_walk or have_cv):
            return Check("residual settlement", "skip",
                         "no verify artifacts and no settlement.json — run "
                         "`galley verify` then `galley settle`")
        if n_open:
            return Check("residual settlement", "fail",
                         f"{n_open} item(s) raised by verify have no settlement "
                         f"record ({len(open_res)} residual(s), "
                         f"{len(open_prob)} flagged edit(s)) — run `galley "
                         f"settle`")
        return Check("residual settlement", "pass",
                     "verify raised nothing to settle")
    if settlement.open:
        return Check("residual settlement", "fail",
                     f"settlement.json leaves {len(settlement.open)} item(s) "
                     f"open after {settlement.rounds} round(s)")
    if n_open:
        return Check("residual settlement", "fail",
                     f"{n_open} item(s) in the verify artifacts have no "
                     f"settlement record — re-run `galley settle`")
    counts = settlement.counts()
    return Check("residual settlement", "pass",
                 f"{len(settlement.latest())} item(s) settled in "
                 f"{settlement.rounds} round(s): "
                 + (", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                    or "none raised"))


def _certify_outcome(run: Path) -> Check:
    """The terminal verdict (galley/outcome.py). Informational: both `done`
    and `needs_human` are legitimate ends; absence is a skip that names the
    verb."""
    from galley.outcome import Outcome
    oc = Outcome.load(run)
    if oc is None:
        return Check("outcome", "skip",
                     "no outcome.json — run `galley outcome` (or `galley "
                     "settle`, which writes it) to record done / needs_human")
    if oc.outcome not in ("done", "needs_human"):
        return Check("outcome", "fail", f"unknown outcome {oc.outcome!r}")
    return Check("outcome", "pass", f"{oc.outcome}: {oc.reason[:160]}")


def _binding_problem(run: Path, artifact: str, payload: dict[str, Any]
                     ) -> str:
    """Return why verification does not cover the current build, or an empty
    string. Reject failed or incomplete reads, changed paragraphs, and
    missing or mismatched build hashes.
    """
    unread = [p for p in (payload.get("unread_paragraphs") or []) if p]
    if unread:
        return (f"{len(unread)} paragraph(s) stayed unread ({', '.join(unread[:5])}"
                f"{'…' if len(unread) > 5 else ''}) — re-run `galley verify "
                f"--paragraphs` on them")
    batches = [b for b in (payload.get("unread_batches") or [])
               if isinstance(b, dict)]
    if batches:
        return (f"{len(batches)} change-verifier batch(es) stayed unread "
                f"({sum(int(b.get('edits', 0) or 0) for b in batches)} "
                f"edit(s)) — re-run `galley verify`")
    dirty = [p for p in (payload.get("unverified_paragraphs") or []) if p]
    if dirty:
        return (f"{len(dirty)} paragraph(s) changed after their last read "
                f"and were not re-read ({', '.join(dirty[:5])}"
                f"{'…' if len(dirty) > 5 else ''}) — re-run `galley verify "
                f"--paragraphs` (or settle with an engine)")
    recorded = str(payload.get("accepted_sha256") or "")
    if not recorded:
        return (f"{artifact} carries no build binding (accepted_sha256) — "
                f"re-run `galley verify` on this build")
    try:
        from galley.verify import build_fingerprints
        fp = build_fingerprints(run)
    except Exception as e:                               # noqa: BLE001
        return f"the deliverable could not be fingerprinted ({e})"
    if not fp:
        return "no deliverable .docx to bind the verification to"
    if fp["accepted_sha256"] != recorded:
        return (f"{artifact} verified a different accepted text "
                f"({recorded[:12]}… vs this build's "
                f"{fp['accepted_sha256'][:12]}…) — the deliverable changed "
                f"after verify; re-run `galley verify`")
    return ""


def _pass_if_bound(run: Path, artifact: str, name: str,
                   payload: dict[str, Any], detail: str) -> Check:
    """Pass only complete verification bound to the current build. Call after
    checking for unresolved findings.
    """
    bound = _binding_problem(run, artifact, payload)
    if bound:
        return Check(name, "fail", bound)
    return Check(name, "pass", detail)


def _certify_change_verify(run: Path) -> Check:
    """Read change_verify.json (from `galley verify`): the change verifier's
    verdict on every applied edit. Any recorded problem fails delivery."""
    payload = _load_json(run / "change_verify.json")
    if payload is None:
        return Check("change verifier", "skip",
                     "no change_verify.json — run `galley verify` to re-read "
                     "every applied edit for meaning/grammar/voice damage")
    stale = _is_stale(run, "change_verify.json", payload)
    if stale:
        return Check("change verifier", "fail", f"{stale}: {_STALE}")
    # ran:false is incomplete verification. Missing ran is a legacy record
    # that ran both gates.
    if payload.get("ran") is False:
        why = payload.get("reason") or "verify ran with --walk-only"
        return Check("change verifier", "fail",
                     f"{why}; change verifier did not run — re-run `galley "
                     f"verify` without --walk-only before delivery")
    problems = [p for p in (payload.get("problems") or []) if isinstance(p, dict)]
    if problems:
        from galley.settle import unsettled
        _res, open_prob = unsettled(run)
        if open_prob:
            from collections import Counter
            counts = Counter(p.get("verdict", "?") for p in problems)
            return Check("change verifier", "fail",
                         f"{len(open_prob)} of {len(problems)} flagged edit(s) "
                         f"unsettled ("
                         + ", ".join(f"{v}={n}" for v, n in counts.most_common())
                         + ") — run `galley settle`; see change_verify.json")
        return _pass_if_bound(run, "change_verify.json", "change verifier",
                              payload, f"{len(problems)} flagged edit(s), "
                              f"every one settled (see settlement.json)")
    return _pass_if_bound(run, "change_verify.json", "change verifier", payload,
                          f"{payload.get('applied_edits', 0)} applied edit(s) "
                          f"re-read, no meaning/grammar/voice damage")


def _certify_finished_walk(run: Path) -> Check:
    """Require a completed, bound finished-text read with every residual
    settled, regardless of severity.
    """
    payload = _load_json(run / "finished_walk.json")
    if payload is None:
        return Check("finished-text walk", "skip",
                     "no finished_walk.json — run `galley verify` to proofread "
                     "the accepted text for residual errors")
    stale = _is_stale(run, "finished_walk.json", payload)
    if stale:
        return Check("finished-text walk", "fail", f"{stale}: {_STALE}")
    if payload.get("ran") is False:
        why = payload.get("reason") or "verify ran with --changes-only"
        return Check("finished-text walk", "fail",
                     f"{why}; finished-text walk did not run — re-run `galley "
                     f"verify` without --changes-only before delivery")
    residuals = [r for r in (payload.get("residuals") or []) if isinstance(r, dict)]
    if residuals:
        from galley.settle import unsettled
        open_res, _prob = unsettled(run)
        if open_res:
            highs = sum(1 for r in residuals if r.get("severity") == "high")
            return Check("finished-text walk", "fail",
                         f"{len(open_res)} unsettled residual(s) of "
                         f"{len(residuals)} ({highs} high) — run `galley "
                         f"settle`; see finished_walk.json")
        return _pass_if_bound(run, "finished_walk.json", "finished-text walk",
                              payload, f"{len(residuals)} residual(s), every "
                              f"one settled (see settlement.json)")
    return _pass_if_bound(run, "finished_walk.json", "finished-text walk",
                          payload, "accepted text read, no residual errors")


# Context length used to match a whitespace hit against its source
# occurrence.
_HYGIENE_CTX = 15


def _hygiene_pre_existing(text: str, m: re.Match, src_text: str | None) -> bool:
    """Check whether the source paragraph contains the same whitespace hit and
    surrounding text. Missing source evidence counts as an introduced fault.
    """
    if src_text is None:
        return False
    window = text[max(0, m.start() - _HYGIENE_CTX):
                  min(len(text), m.end() + _HYGIENE_CTX)]
    return window in src_text


def _source_paragraph_texts(source: str | Path, walk_package, DocxPackage,
                            paragraph_view_text) -> dict[str, str] | None:
    """Read accepted source paragraphs by id, or return None if the manuscript
    cannot be read.
    """
    try:
        pkg = DocxPackage(str(source))
    except Exception:
        return None
    try:
        return {wp.para_id: paragraph_view_text(wp.element, "accept")
                for wp in walk_package(pkg)}
    except Exception:
        return None


def _certify_text_hygiene(run: Path, source: str | Path | None = None) -> Check:
    """Scan accepted deliverables for double and trailing spaces. Exempt hits
    matching the source paragraph; without readable source evidence, all
    hits fail. Skip if no deliverable or OOXML tooling is available.
    """
    # Scan the accepted manuscript; the change log quotes pre-fix source
    # text.
    docs = sorted(p for p in run.glob("*.docx")
                  if not p.name.startswith("~$")
                  and "change log" not in p.name.lower())
    if not docs:
        return Check("delivered text hygiene", "skip",
                     "no manuscript .docx in the run directory")
    try:
        from docproof.reassembler import paragraph_view_text
        from docproof.utils.xml_helpers import (DocxPackage,
                                                walk_package)
    except Exception as e:                        # lxml missing, etc.
        return Check("delivered text hygiene", "skip",
                     f"OOXML tooling unavailable: {e}")

    src_paragraphs = (_source_paragraph_texts(source, walk_package, DocxPackage,
                                              paragraph_view_text)
                      if source is not None else None)

    trailing = re.compile(r"[  ]+(?=\n|$)")
    problems: list[str] = []
    pre_existing: list[str] = []
    for path in docs:
        try:
            pkg = DocxPackage(str(path))
        except Exception as e:
            problems.append(f"{path.name}: unreadable ({e})")
            continue
        doubles = trails = 0
        for wp in walk_package(pkg):
            text = paragraph_view_text(wp.element, "accept")
            if not any(c.isalnum() for c in text):
                continue                          # scene dividers space freely
            src_text = (src_paragraphs.get(wp.para_id)
                       if src_paragraphs is not None else None)
            for m in _POST_TEXT_DOUBLE.finditer(text):
                if _hygiene_pre_existing(text, m, src_text):
                    pre_existing.append(f"{wp.para_id}: double space "
                                        f"pre-existing in the source")
                else:
                    doubles += 1
            for m in trailing.finditer(text):
                if _hygiene_pre_existing(text, m, src_text):
                    pre_existing.append(f"{wp.para_id}: trailing space "
                                        f"pre-existing in the source")
                else:
                    trails += 1
        if doubles or trails:
            problems.append(f"{path.name}: {doubles} double space(s), "
                            f"{trails} trailing-space run(s)")
    if problems:
        detail = "; ".join(problems)
        if pre_existing:
            detail += (f" (also {len(pre_existing)} pre-existing hit(s) not "
                       f"counted against this run)")
        return Check("delivered text hygiene", "fail", detail)
    if pre_existing:
        return Check("delivered text hygiene", "pass",
                     f"{len(docs)} document(s): no introduced double spaces "
                     f"or trailing whitespace; {len(pre_existing)} hit(s) "
                     f"pre-existing in the source and not this run's fault: "
                     + "; ".join(pre_existing[:6])
                     + ("…" if len(pre_existing) > 6 else ""))
    return Check("delivered text hygiene", "pass",
                 f"{len(docs)} document(s): no double spaces, no trailing "
                 f"whitespace")


def _certify_no_merged_duplicates(envelope: dict[str, Any]) -> Check:
    """Fail when multiple applied findings share a content key; ignore
    duplicates that were dropped.
    """
    from galley.lifecycle import reconstruct_from_findings
    led = reconstruct_from_findings(envelope)
    # Distinct anchors distinguish separate edits sharing the same quoted
    # text.
    anchors: dict[str, tuple] = {}
    for row in envelope.get("findings", []) or []:
        if not isinstance(row, dict):
            continue
        a = row.get("anchor") or {}
        fid = str(row.get("finding_id", ""))
        if fid and isinstance(a, dict) and "start" in a:
            anchors[fid] = (a.get("start"), a.get("end"))
    bad = []
    for key, ids in led.duplicates().items():
        merged = [i for i in ids if led.state_of(i) == "merged"]
        if len(merged) <= 1:
            continue
        spans = [anchors.get(i) for i in merged]
        if all(sp is not None for sp in spans) and len(set(spans)) == len(spans):
            continue                    # distinct anchors: split, not doubled
        bad.append(f"{key}: {', '.join(merged)}")
    if bad:
        return Check("no duplicate merged edits", "fail",
                     "; ".join(bad))
    return Check("no duplicate merged edits", "pass",
                 "no content-duplicate edit applied twice")


def _row_applied(row: dict[str, Any]) -> bool:
    """Use the explicit applied flag, then status, to identify delivered edits.
    Treat rows lacking both fields as applied for conservative scanning.
    """
    if row.get("force_query") or row.get("queried"):
        return False
    status = str(row.get("status") or "")
    if status.startswith(("rejected_", "skipped_")) or status == "query":
        return False
    applied = row.get("applied")
    if isinstance(applied, bool):
        return applied
    return True


def _certify_no_insertion_collisions(envelope: dict[str, Any]) -> Check:
    """Reject different corrections sharing a paragraph, original window, and
    occurrence.
    """
    seen: dict[tuple, str] = {}
    collisions: list[str] = []
    for row in envelope.get("findings", []) or []:
        if not isinstance(row, dict):
            continue
        # Check only applied text changes.
        corr = row.get("corrected_text", "")
        if not _row_applied(row) or not corr:
            continue
        # Use exact anchors when recorded; otherwise compare quoted windows
        # conservatively.
        a = row.get("anchor") or {}
        if isinstance(a, dict) and "start" in a:
            key = (row.get("para_id", ""), a.get("start"), a.get("end"))
            what = str(a.get("insert_text", corr))
        else:
            key = (row.get("para_id", ""), row.get("original_text", ""),
                   row.get("occurrence", 1))
            what = corr
        if key in seen and seen[key] != what:
            collisions.append(str(row.get("para_id", "")))
        else:
            seen[key] = what
    if collisions:
        return Check("no insertion collisions", "fail",
                     f"conflicting edits at the same anchor in: "
                     f"{', '.join(sorted(set(collisions)))}")
    return Check("no insertion collisions", "pass",
                 "no two edits claim the same anchor")


# Revision authors from insertions, deletions, and formatting changes;
# exclude query comments.
_REVISION_AUTHOR = re.compile(
    r'<w:(?:ins|del|rPrChange|pPrChange)\b[^>]*?\bw:author="([^"]*)"')


def _revision_authors(run: Path) -> set[str] | None:
    """Read distinct tracked-change authors from the deliverable OOXML, or
    return None if no deliverable exists.
    """
    import zipfile
    from xml.sax.saxutils import unescape
    docs = sorted(p for p in run.glob("*.docx")
                  if not p.name.startswith("~$")
                  and "change log" not in p.name.lower())
    if not docs:
        return None
    authors: set[str] = set()
    try:
        with zipfile.ZipFile(docs[0]) as z:
            for name in z.namelist():
                if name.startswith("word/") and name.endswith(".xml"):
                    xml = z.read(name).decode("utf-8", "replace")
                    authors.update(unescape(m) for m in
                                   _REVISION_AUTHOR.findall(xml))
    except (OSError, zipfile.BadZipFile):
        return None
    return authors


def _certify_two_author_attribution(envelope: dict[str, Any], run: Path
                                    ) -> Check:
    """Require distinct revision authors when both mechanical and copyediting
    findings were applied. Single-lane runs need only one author.
    """
    lanes = set()
    for row in envelope.get("findings", []) or []:
        if not isinstance(row, dict) or not _row_applied(row):
            continue
        lane = row.get("lane") or ("copyedit" if row.get("error_type") ==
                                   "copyedit" else "mechanical")
        lanes.add("copyedit" if lane == "copyedit" else "mechanical")
    if not {"copyedit", "mechanical"} <= lanes:
        return Check("two-author attribution", "skip",
                     f"single lane ({', '.join(sorted(lanes)) or 'none'}) — "
                     f"one author")
    authors = _revision_authors(run)
    if authors is None:
        return Check("two-author attribution", "skip",
                     "both lanes applied but no manuscript .docx in the run "
                     "directory to read the revision authors from")
    if len(authors) < 2:
        named = ", ".join(sorted(authors)) or "none"
        return Check("two-author attribution", "fail",
                     f"both lanes applied but the deliverable's tracked changes "
                     f"carry {len(authors)} author ({named}) — the copy-edit "
                     f"lane is indistinguishable from the proofread "
                     f"(check Config.lane_authors)")
    return Check("two-author attribution", "pass",
                 f"both lanes applied; tracked changes carry "
                 f"{len(authors)} authors ({', '.join(sorted(authors))})")


def _certify_reject_all(envelope: dict[str, Any]) -> Check:
    """Check the recorded reject-all round trip. A mismatch fails; an absent or
    unrun audit skips.
    """
    audit = envelope.get("audit")
    if not isinstance(audit, dict):
        return Check("reject-all round trip", "skip",
                     "findings.json carries no audit record — the reject-all "
                     "round trip was never recorded for this run")
    if not audit.get("ran"):
        return Check("reject-all round trip", "skip",
                     "the reject-all audit did not run (audit: off?) — that "
                     "rejecting every change restores the source is unproven")
    if not audit.get("passed"):
        mism = audit.get("mismatches") or []
        missing = audit.get("missing") or []
        return Check("reject-all round trip", "fail",
                     f"rejecting every change does not restore the source: "
                     f"{len(mism)} paragraph(s) differ, {len(missing)} missing"
                     + (f" (first: {', '.join(map(str, mism[:3]))})" if mism
                        else ""))
    return Check("reject-all round trip", "pass",
                 f"{audit.get('checked', 0)} paragraph(s) checked; rejecting "
                 f"every change restores the source")


def _certify_intent_zone_collisions(run: Path) -> Check:
    """Report edits downgraded to queries inside protected spans without
    failing certification.
    """
    data = _load_json(run / "intent_collisions.json")
    if data is None:
        return Check("intent-zone collisions", "skip",
                     "no intent-zone record for this run")
    rows = data if isinstance(data, list) else data.get("collisions", [])
    n = len(rows or [])
    return Check("intent-zone collisions", "pass",
                 f"{n} edit(s) downgraded to queries inside protected spans")


def _certify_run_state(run: Path) -> Check:
    """If a run state machine is present, a delivery certificate expects the run
    to have reached at least the audited state."""
    from galley.state_machine import RunStateMachine
    # Look for state.json in both the results directory and workspace root.
    path = run / "state.json"
    if not path.is_file():
        path = run.parent / "state.json"
    if not path.is_file():
        return Check("run state", "skip", "no state.json for this run")
    try:
        machine = RunStateMachine.load(path)
    except (OSError, ValueError) as e:
        return Check("run state", "fail", f"state.json unreadable: {e}")
    if machine.reached("audited"):
        return Check("run state", "pass",
                     f"reached {machine.current!r}")
    return Check("run state", "fail",
                 f"only reached {machine.current!r}; delivery expects at least "
                 f"'audited'")


def _check_hash(name: str, actual: str, expected: Any) -> Check:
    if not expected:
        return Check(name, "skip", "approval carries no hash to compare")
    if actual == expected:
        return Check(name, "pass", f"{actual[:12]}…")
    return Check(name, "fail",
                 f"run {actual[:12]}… != approved {str(expected)[:12]}…")


def _rebuild_spend(envelope: dict[str, Any], run: Path
                   ) -> tuple[float | None, str]:
    """Read paid spend from the rebuild's sibling case file, using spent_usd or
    summed charges. Return (None, reason) when unavailable.
    """
    marker = envelope.get("rebuild")
    name = (marker.get("paid_spend_recorded_in") if isinstance(marker, dict)
            else None) or "casefile.json"
    cf = _load_json(run / str(name))
    if not isinstance(cf, dict):
        return None, f"no {name} beside findings.json to reconcile spend against"
    budget = cf.get("budget") or {}
    if not isinstance(budget, dict):
        return None, f"{name} carries no budget ledger"
    if isinstance(budget.get("spent_usd"), (int, float)):
        return float(budget["spent_usd"]), f"{name} budget.spent_usd"
    charges = budget.get("charges") or []
    spent = sum(float(c.get("cost_usd", 0.0) or 0.0)
                for c in charges if isinstance(c, dict))
    return spent, f"{name} budget ledger ({len(charges)} charge(s))"


def _certify_envelope(envelope: dict[str, Any], run: Path,
                      manifest: dict[str, Any] | None) -> list[Check]:
    checks: list[Check] = []
    cost = (envelope.get("cost") or {}).get("total_usd")

    # Rebuild spend comes from the case file, not the zero-cost output
    # envelope.
    rebuild = (envelope.get("judge_model") == "galley:rebuild"
               or isinstance(envelope.get("rebuild"), dict))
    spend_note = ""
    if rebuild:
        # Without the case file, rebuild spend is unknown.
        cost, spend_note = _rebuild_spend(envelope, run)

    # Paid runs require a checkpoint; a rebuild uses its case file as the
    # checkpoint.
    has_ckpt = (run / "findings.checkpoint.json").is_file() \
        or envelope.get("checkpoint") is not None
    paid = bool(cost) and float(cost) > 0
    if rebuild:
        checks.append(Check("checkpoint present",
                            "pass" if cost is not None else "skip",
                            f"Galley rebuild: the case file is the checkpoint "
                            f"({spend_note})"))
    elif paid:
        checks.append(Check("checkpoint present",
                            "pass" if has_ckpt else "fail",
                            "findings.checkpoint.json / envelope checkpoint "
                            "found" if has_ckpt else "a paid run left no "
                            "checkpoint — a crash could not resume"))
    else:
        checks.append(Check("checkpoint present", "skip",
                            "no paid spend recorded"))

    # Flag zero-cost detector findings unless identified as a rebuild or
    # import.
    n_findings = len(envelope.get("findings") or [])
    if cost is not None and float(cost) == 0.0 and n_findings > 0 \
            and not rebuild \
            and envelope.get("judge_model") not in _ZERO_COST_JUDGES:
        checks.append(Check("zero-cost anomaly", "fail",
                            f"{n_findings} finding(s) but $0.00 recorded — a "
                            f"detector that should bill likely did not run"))
    elif rebuild:
        checks.append(Check("zero-cost anomaly", "pass",
                            f"Galley rebuild of {n_findings} finding(s) at $0; "
                            + (f"paid spend ${float(cost):.2f} per {spend_note}"
                               if cost is not None else spend_note)))
    else:
        checks.append(Check("zero-cost anomaly", "pass",
                            f"cost ${float(cost):.2f} for {n_findings} "
                            f"finding(s)" if cost is not None
                            else "no cost field to reconcile"))

    # Compare the paid spend with the approval ceiling.
    if manifest is not None and cost is not None:
        cap = float(manifest.get("max_spend_usd", 0.0))
        checks.append(Check("budget within approval",
                            "pass" if float(cost) <= cap else "fail",
                            f"${float(cost):.2f} of ${cap:.2f} approved"
                            + (f" ({spend_note})" if spend_note else "")))
    elif manifest is not None and rebuild:
        checks.append(Check("budget within approval", "skip", spend_note))
    return checks


_SPELLFIX_EXPL = re.compile(r'"(\w+)" appears to be a misspelling of "(.+?)"')


def _certify_spellfix_sanity(run: Path) -> Check:
    """Reject spelling edits whose flagged token is a prefix of a suggested
    word already present in the original. These can arise from incorrectly
    split accented words.
    """
    data = _load_json(run / "findings.json")
    if data is None:
        return Check("spellfix sanity", "skip", "no findings.json to scan")
    rows = data.get("findings", data) if isinstance(data, dict) else data
    bad: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("applied"):
            continue
        m = _SPELLFIX_EXPL.match(str(row.get("explanation") or ""))
        if not m:
            continue
        token, suggestion = m.group(1), m.group(2)
        if (suggestion != token and suggestion.startswith(token)
                and suggestion in str(row.get("original_text") or "")):
            bad.append(f"{row.get('para_id')}: \"{token}\" -> "
                       f"\"{suggestion}\" (already present)")
    if bad:
        return Check("spellfix sanity", "fail",
                     "truncated-stem spelling fix(es): " + "; ".join(bad[:6]))
    return Check("spellfix sanity", "pass",
                 "no truncated-stem spelling corrections")


def _duplicated_fragment_hits(run: Path) -> list[str]:
    """Find repeated five-word fragments introduced within an accepted
    paragraph, comparing against its reject-all view. Return no hits when
    the deliverable cannot be read.
    """
    try:
        from galley.settle import introduced_fragments
        from galley.verify import paragraph_views
        original, accepted = paragraph_views(run)
    except Exception:                                    # noqa: BLE001
        return []
    hits: list[str] = []
    for pid, text in accepted.items():
        for frag in introduced_fragments(original.get(pid, ""), text):
            hits.append(f"duplicated fragment in {pid}: {frag!r}")
            if len(hits) >= 12:
                return hits
    return hits


# Unresolved Word field results require regenerating fields or the TOC.
_UNRESOLVED_FIELD_RE = re.compile(
    r"Error! (?:Bookmark not defined|Reference source not found)\.?")


def unresolved_field_results(docx_path: str | Path) -> list[tuple[str, str]]:
    """Return (para_id, text) for unresolved field results in all accepted
    paragraphs, including skipped styles. Return an empty list when
    unreadable.
    """
    try:
        from docproof.reassembler import paragraph_view_text
        from docproof.utils.xml_helpers import DocxPackage, walk_package
        pkg = DocxPackage(str(docx_path))
    except Exception:                                    # noqa: BLE001
        return []
    out: list[tuple[str, str]] = []
    for wp in walk_package(pkg):
        text = paragraph_view_text(wp.element, "accept")
        if _UNRESOLVED_FIELD_RE.search(text):
            out.append((wp.para_id, " ".join(text.split())[:120]))
    return out


def _certify_field_results(run: Path) -> Check:
    """Report unresolved fields without blocking delivery; they require
    regenerating the TOC or other fields.
    """
    docs = sorted(p for p in run.glob("*.docx")
                  if not p.name.startswith("~$")
                  and "change log" not in p.name.lower())
    if not docs:
        return Check("unresolved fields", "skip",
                     "no manuscript .docx in the run directory")
    hits = unresolved_field_results(docs[0])
    if hits:
        sample = "; ".join(f"{pid}: {text[:60]}" for pid, text in hits[:4])
        return Check("unresolved fields", "warn",
                     f"{len(hits)} paragraph(s) carry an unresolved field "
                     f"result (\"Error! Bookmark not defined.\" / \"Reference "
                     f"source not found.\") — the designer regenerates the "
                     f"TOC/fields; no lane can edit them: {sample}"
                     + ("…" if len(hits) > 4 else ""))
    return Check("unresolved fields", "pass",
                 "no unresolved field results in the delivered text")


def _minus_preexisting(corrected: str, original: str) -> str:
    """Blank artifact matches already present in the original span, occurrence
    for occurrence, so only introduced artifacts remain.
    """
    if not original:
        return corrected
    out = corrected
    for pat in _ARTIFACTS:
        had = len(pat.findall(original))
        if not had:
            continue
        for _ in range(had):
            out, n = pat.subn("", out, count=1)
            if not n:
                break
    return out


def _artifact_scan(run: Path) -> Check:
    """Scan applied corrections for introduced artifacts and accepted
    deliverable paragraphs for duplicated fragments. Exclude pre-existing
    faults quoted from the source.
    """
    texts: list[str] = []
    for name in ("findings.json", "flights_findings.json"):
        data = _load_json(run / name)
        if data is None:
            continue
        rows = data.get("findings", data) if isinstance(data, dict) else data
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if not _row_applied(row):
                continue           # a question or a rejected row changes nothing
            corr = row.get("corrected_text")
            if isinstance(corr, str) and corr:
                texts.append(_minus_preexisting(
                    corr, str(row.get("original_text") or "")))
    repeats = _duplicated_fragment_hits(run)
    if not texts and not repeats:
        return Check("artifact scan", "skip",
                     "no corrected text in the run directory to scan")
    blob = "\n".join(texts)
    hits = [pat.pattern for pat in _ARTIFACTS if pat.search(blob)]
    if _POST_TEXT_DOUBLE.search(blob):
        hits.append("  ")
    hits.extend(repeats)
    if hits:
        return Check("artifact scan", "fail",
                     f"found merge artifact(s) in corrected text: "
                     f"{', '.join(map(repr, hits))}")
    return Check("artifact scan", "pass", "no doubled punctuation / merge "
                 "artifacts in any corrected text")


__all__ = [
    "MANIFEST_SCHEMA_VERSION", "COPYEDIT_LANES", "MANDATORY_CHECKS",
    "ModelRoute", "Deviation",
    "Check", "Certificate", "sha256_file", "config_hash", "model_routes",
    "providers_in_use", "build_manifest", "verify_plan", "certify_run",
    "unresolved_field_results",
]
