"""The approval manifest, model-route visibility, and delivery certificate.

Three related things a reproducible, model-agnostic Galley run needs, all built
on two primitives — a content hash and a route map:

* **model_routes(cfg)** walks a loaded Config and reports every model it would
  call, each with the provider that serves it and whether its lane is active.
  This is the single effective-config report the flight deck lacked: a plan
  could look Fable-free while the eventual command routed to Fable through a
  section's default. Nothing about routing is implicit once this is printed.

* **build_manifest / verify_plan** turn a human-approved plan into an immutable
  ``approval.json`` — source hash, config hash, the allowed model/provider set,
  the stage, the max spend, the enabled lanes — and check a proposed paid
  command against it. A deviation (a changed manuscript, a model not in the
  allowed set, a budget over the ceiling) is a refusal, not a warning.

* **certify_run** is the delivery gate: it re-checks a finished run against the
  manifest and against a set of structural invariants (checkpoint present, no
  zero-cost anomaly, budget reconciled, artifact scan clean) and returns a
  certificate. Delivery should require a passing certificate.

Model IDs are never hard-coded here. A role (reviewer, verifier, copyedit
judge, auditor) resolves to whatever model the config routes it to, so
substituting one model for another is a config change this module simply
reports — never an edit to code.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA_VERSION = 1

# A config field whose NAME ends in one of these carries a model id we route on.
_MODEL_FIELD = re.compile(r"(^|_)(model)$")

# The deterministic artifact patterns a clean deliverable must not contain —
# the merge desk's own scan list (doubled comma/space, orphaned punctuation).
# Compiled regexes, so the two that end in a period mean a LITERAL period
# (an ellipsis or a closing quote followed by one) rather than any character;
# the pattern text is what a failing check names.
_ARTIFACTS = tuple(re.compile(p) for p in (",,", '" "', r"…\.", r"”\.", "”,"))
# The zero-cost markers: an envelope whose findings legitimately cost $0 to
# produce because the paid work is recorded elsewhere — an import of external
# judgments, or a Galley rebuild of a case file's adjudicated findings (see
# galley.deliverable.REBUILD_MARKER; its paid spend lives in casefile.json).
_ZERO_COST_JUDGES = ("external:import-judgments", "galley:rebuild")
# Double spaces are an artifact only AFTER visible text: leading indentation
# is spacing normalization deliberately preserves (normalize._SPACE_RUN uses
# the same lookbehind), so a bare "  " substring test fails hand-centered
# subtitle/TOC lines that are perfectly clean. NBSP included, as in normalize.
_POST_TEXT_DOUBLE = re.compile(r"(?<=\S)[  ]{2,}")


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# Fields that say WHERE a run writes, not WHAT it does — excluded from the
# hash so `approve` (no --out) and `review --out runs/...` agree on the same
# material config. Leaving output_dir in is what made every gated run refuse
# itself: _configure stamps args.out onto the config before verify_plan runs.
_VOLATILE_CONFIG_FIELDS = ("output_dir",)


def config_hash(cfg: Any) -> str:
    """A stable hash of a loaded Config's effective content — the model_dump
    serialized with sorted keys, so two configs that differ only in key order
    or comments hash the same, and any material change hashes differently.
    Volatile run-location fields (see _VOLATILE_CONFIG_FIELDS) are excluded:
    where a run writes its output is not part of what a human approved."""
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
    """Every model-bearing field in the config, resolved to its provider and
    tagged active/inactive by whether its owning section is enabled. Walks the
    Config recursively so a new model-bearing section is covered without a code
    change here. The reviewer (``api.model``) and the ensemble detectors/verifier
    are handled explicitly because their activity is not a plain ``enabled``
    flag."""
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

    # The reviewer / single detector: live only when there are typed passes to
    # run it through — with `error_types: []` (final-replay, a deterministic
    # floor config) it is never called, and showing it active reads as spend.
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
                # Sections gated by something other than `enabled`:
                # candidate_screening runs only when mode != 'off'; the rounds
                # judge fires only between rounds, so count 1 never calls it.
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
                   note: str = "") -> dict[str, Any]:
    """Assemble the immutable approval manifest. ``allowed_providers`` defaults
    to exactly the providers the approved config actively uses — an approval is
    a promise that the run reaches no OTHER vendor."""
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
    """Check a proposed paid command against an approval manifest. Returns the
    list of deviations; an empty list means the command is within approval. A
    paid command should refuse (exit non-zero) on any deviation."""
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


# --- certify -----------------------------------------------------------------

@dataclass
class Check:
    name: str
    status: str          # "pass" | "fail" | "skip"
    detail: str = ""

    def to_json(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class Certificate:
    checks: list[Check] = field(default_factory=list)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == "fail"]

    @property
    def passed(self) -> bool:
        return not self.failed

    def to_json(self) -> dict[str, Any]:
        return {"passed": self.passed,
                "checks": [c.to_json() for c in self.checks],
                "failed": len(self.failed)}


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# A verify artifact that predates the findings it claims to have read is a
# PREVIOUS build's verdict. Redding Book 1 rebuilt its deliverable after
# `galley verify` had run, and certify reported the old run's clean read over
# the new run's edits — the one way this gate can lie. Both comparisons are
# made when they can be: the recorded `generated_at` timestamps (authoritative,
# and survive a copy) and, failing that, the files' own mtimes.
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
    """Re-check a finished run for delivery. Runs every check whose inputs are
    present; a check whose artifact is absent is recorded as ``skip`` (honest),
    never silently passed. Delivery should require zero ``fail`` checks."""
    run = Path(run_dir)
    cert = Certificate()

    # The envelope is read early because two checks below need to know whether
    # this run actually spent money: a $0 replay rebuild legitimately runs a
    # different (final-replay) config than the approved paid wave, so its
    # config-hash mismatch is informational, not a failure.
    early_envelope = _load_json(run / "findings.json")
    if early_envelope is None:
        early_envelope = _load_json(run / "flights_findings.json")
    _cost = ((early_envelope or {}).get("cost") or {}).get("total_usd")
    run_paid = bool(_cost) and float(_cost) > 0

    # 1. Source + config hashes match the approval. A comparison that cannot
    # be made — no approval, no --source, no --config — is recorded as a skip
    # that names what was not compared, never silently omitted: a certificate
    # that lists no "source hash" line reads as if the source were checked.
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

    # 2. Model routes stay within the approved set (route check only —
    # source/config hashes are checked separately above).
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

    # 7. Run state machine, if present — a delivery certificate expects the run
    # to have reached at least the audited state.
    cert.checks.append(_certify_run_state(run))

    # 8. Text hygiene over the delivered .docx itself: no double spaces, no
    # paragraph-trailing whitespace. The Johnson run shipped 84 double spaces
    # the head proofreader's own check caught — whatever path produced the
    # deliverable, this fails loud instead.
    cert.checks.append(_certify_text_hygiene(run))

    # 9. Finished-text SENSE gates. certify itself reads no text for meaning;
    # `galley verify` does and records its verdict here. A recorded problem fails
    # delivery; a clean record passes; no record skips loudly (run `galley
    # verify`) so an unread deliverable never reads as a certified-clean one.
    cert.checks.append(_certify_change_verify(run))
    cert.checks.append(_certify_finished_walk(run))
    return cert


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
        return Check("change verifier", "skip", f"{stale}: {_STALE}")
    # `galley verify` records whether this gate actually ran (`--walk-only`
    # writes the file with ran: false and no problems, which is NOT a clean
    # read). Absent `ran` is an older record that always ran both gates.
    if payload.get("ran") is False:
        why = payload.get("reason") or "verify ran with --walk-only"
        return Check("change verifier", "skip",
                     f"{why}; change verifier did not run — re-run `galley "
                     f"verify` without --walk-only before delivery")
    problems = payload.get("problems") or []
    if problems:
        from collections import Counter
        counts = Counter(p.get("verdict", "?") for p in problems
                         if isinstance(p, dict))
        return Check("change verifier", "fail",
                     f"{len(problems)} applied edit(s) flagged ("
                     + ", ".join(f"{v}={n}" for v, n in counts.most_common())
                     + ") — see change_verify.json")
    return Check("change verifier", "pass",
                 f"{payload.get('applied_edits', 0)} applied edit(s) re-read, "
                 f"no meaning/grammar/voice damage")


def _certify_finished_walk(run: Path) -> Check:
    """Read finished_walk.json (from `galley verify`): a residual-error read over
    the ACCEPTED text. A high-severity residual fails; lower ones warn but do not
    block (they are candidates for a next wave, not proof the build is broken)."""
    payload = _load_json(run / "finished_walk.json")
    if payload is None:
        return Check("finished-text walk", "skip",
                     "no finished_walk.json — run `galley verify` to proofread "
                     "the accepted text for residual errors")
    stale = _is_stale(run, "finished_walk.json", payload)
    if stale:
        return Check("finished-text walk", "skip", f"{stale}: {_STALE}")
    if payload.get("ran") is False:
        why = payload.get("reason") or "verify ran with --changes-only"
        return Check("finished-text walk", "skip",
                     f"{why}; finished-text walk did not run — re-run `galley "
                     f"verify` without --changes-only before delivery")
    residuals = [r for r in (payload.get("residuals") or []) if isinstance(r, dict)]
    highs = [r for r in residuals if r.get("severity") == "high"]
    if highs:
        return Check("finished-text walk", "fail",
                     f"{len(highs)} high-severity residual error(s) of "
                     f"{len(residuals)} — see finished_walk.json")
    if residuals:
        return Check("finished-text walk", "pass",
                     f"{len(residuals)} low/medium residual(s), none high — "
                     f"review finished_walk.json before the next wave")
    return Check("finished-text walk", "pass",
                 "accepted text read, no residual errors")


def _certify_text_hygiene(run: Path) -> Check:
    """Scan every delivered .docx in the run directory (accept-all view) for
    double spaces and paragraph-trailing whitespace — the two things
    normalization promises are gone. Skips honestly when there is no .docx or
    the OOXML tooling is unavailable."""
    # The change-log docx quotes ORIGINAL, pre-fix text (its trailing spaces
    # are what the run fixed), so hygiene applies only to the manuscript
    # deliverable — and to its ACCEPT view, the text the author ends up with.
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
    trailing = re.compile(r"[  ]+(?=\n|$)")
    problems: list[str] = []
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
            doubles += len(_POST_TEXT_DOUBLE.findall(text))
            trails += sum(1 for _ in trailing.finditer(text))
        if doubles or trails:
            problems.append(f"{path.name}: {doubles} double space(s), "
                            f"{trails} trailing-space run(s)")
    if problems:
        return Check("delivered text hygiene", "fail", "; ".join(problems))
    return Check("delivered text hygiene", "pass",
                 f"{len(docs)} document(s): no double spaces, no trailing "
                 f"whitespace")


def _certify_no_merged_duplicates(envelope: dict[str, Any]) -> Check:
    """Two findings that share a content key AND both merged would double-apply
    the same edit. A duplicate where one side dropped is expected (that is dedup
    working); only a duplicate that survived on both sides fails."""
    from galley.lifecycle import reconstruct_from_findings
    led = reconstruct_from_findings(envelope)
    # Anchor spans distinguish two same-content findings that landed at
    # DIFFERENT places — a paragraph-sized row split into per-touch tracked
    # changes shares (para, original, type) across its siblings, and that is
    # composition, not a double-apply. Only two merged findings at the SAME
    # anchor (or with no anchor to tell them apart) fail.
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
    """Whether a findings row reached the deliverable as a tracked change — the
    rows the artifact and collision scans must read, since what a REJECTED or
    SKIPPED row would have written never reaches the author. A row that
    carries the reporter's ``applied`` flag is believed; one without it (a
    flights envelope, a hand-built row) is judged by its ``status`` —
    ``rejected_*`` / ``skipped_*`` / ``query`` never applied — and a row with
    neither is scanned conservatively."""
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
    """Two edits at the same anchor (same para_id + original window + occurrence)
    with different corrections would compose into an artifact (,, or " "). The
    merge desk dedupes by insertion point; this is the certificate that it did."""
    seen: dict[tuple, str] = {}
    collisions: list[str] = []
    for row in envelope.get("findings", []) or []:
        if not isinstance(row, dict):
            continue
        # Only edits that actually change text and landed (not queries, not
        # the rejected side of an arbitration — that loser is the dedup
        # working, not a collision).
        corr = row.get("corrected_text", "")
        if not _row_applied(row) or not corr:
            continue
        # Key on the real anchor when the run recorded one: two rows may quote
        # the SAME sentence while editing different characters of it (a repair
        # cluster's members, a paragraph row split into per-touch changes) —
        # that is composition the validator already arbitrated, not a
        # collision. Only two edits at the same anchored span that disagree on
        # what to write there conflict. Rows with no anchor fall back to the
        # quote-level key, conservatively.
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


# The tracked-change author stamps a .docx carries: insertions, deletions,
# and formatting revisions (comments are the query channel, not an edit lane).
_REVISION_AUTHOR = re.compile(
    r'<w:(?:ins|del|rPrChange|pPrChange)\b[^>]*?\bw:author="([^"]*)"')


def _revision_authors(run: Path) -> set[str] | None:
    """The distinct tracked-change authors in the manuscript deliverable, or
    None when there is no deliverable to read. Read straight off the OOXML —
    the author is a plain attribute, so no lxml round trip is needed."""
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
    """When both lanes ran (a copyedit finding applied beside a mechanical
    one), the deliverable must carry two tracked-change authors so the author
    can filter a proofread change from a copy-edit one (Config.lane_authors,
    reassembler.py). The findings rows carry the lane; the author name each
    lane was written under is in the .docx alone, so that is what is read: two
    applied lanes and fewer than two distinct revision authors is a failure. A
    single-lane run needs no second author."""
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
    """The reject-all round trip: finish() rejects every tracked change it wrote
    and compares the result to the ingested text (docproof.audit), and records
    the verdict in the envelope as ``audit: {ran, passed, ...}``. A recorded
    failure means rejecting the changes does NOT return the author's text —
    delivery fails. A record that says it did not run (audit: off) skips loudly:
    the round trip is unproven, not proven."""
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
    """If the run recorded intent-zone collisions (edits downgraded to queries
    inside protected spans), surface the count — informational, since the
    downgrade already protected the zone; a high count may mean a mis-drawn
    zone."""
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
    # `galley state` writes state.json at the WORKSPACE root; a run's results
    # directory usually sits one level below it, so look in both places.
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
    """For a Galley rebuild envelope (galley.deliverable.REBUILD_MARKER), the
    paid spend the sibling case file recorded — ``budget.spent_usd`` when it
    is serialized, else the sum of ``budget.charges[].cost_usd`` — and a note
    saying where it came from. ``(None, why)`` when the case file is not there
    to reconcile against."""
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

    # A Galley rebuild (galley.deliverable) legitimately produces its findings
    # at $0: the paid waves are in the case file beside it, so that ledger —
    # not the envelope's own $0 — is what checkpoint and budget reconcile to.
    rebuild = (envelope.get("judge_model") == "galley:rebuild"
               or isinstance(envelope.get("rebuild"), dict))
    spend_note = ""
    if rebuild:
        # The envelope's own $0 is never the number to reconcile: with no
        # case file to read, the spend is unknown (None), not zero.
        cost, spend_note = _rebuild_spend(envelope, run)

    # Checkpoint completeness — a finished paid run should have left one. For
    # a rebuild the case file IS the checkpoint (it holds every finding and
    # verdict a resumed run rebuilds from).
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

    # Zero-cost anomaly: findings exist and detectors ran, but cost is $0.
    # Not for an envelope that names itself a $0 rebuild/import (the paid
    # work is recorded elsewhere).
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

    # Budget reconciliation against the approval ceiling — for a rebuild,
    # against the case file's paid spend, never the envelope's own $0.
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
    """Catch the truncated-stem spelling signature: the flagged token is a
    PREFIX of the suggested word and the suggestion already appears in the
    original text — which means the tokenizer split a word (an accent, once)
    and the 'fix' re-inserts characters that were never missing. Six such
    wrong edits ('clichéé', 'cruditéé', 'voilàà', 'entréée') shipped on the
    Purpura beta and passed every other gate."""
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


def _artifact_scan(run: Path) -> Check:
    """Scan what the author will actually READ after accepting the changes:
    every finding's corrected_text. Raw change-log / findings-file text is NOT
    scanned — those files faithfully quote the ORIGINAL, pre-fix text, whose
    artifacts are precisely what the findings fix, so a raw-text scan fails a
    clean deliverable for honestly reporting what it repaired."""
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
                texts.append(corr)
    if not texts:
        return Check("artifact scan", "skip",
                     "no corrected text in the run directory to scan")
    blob = "\n".join(texts)
    hits = [pat.pattern for pat in _ARTIFACTS if pat.search(blob)]
    if _POST_TEXT_DOUBLE.search(blob):
        hits.append("  ")
    if hits:
        return Check("artifact scan", "fail",
                     f"found merge artifact(s) in corrected text: "
                     f"{', '.join(map(repr, hits))}")
    return Check("artifact scan", "pass", "no doubled punctuation / merge "
                 "artifacts in any corrected text")


__all__ = [
    "MANIFEST_SCHEMA_VERSION", "ModelRoute", "Deviation", "Check",
    "Certificate", "sha256_file", "config_hash", "model_routes",
    "providers_in_use", "build_manifest", "verify_plan", "certify_run",
]
