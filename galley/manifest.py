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
_ARTIFACTS = (",,", '" "', "…\\.", "”.", "”,", "  ")


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def config_hash(cfg: Any) -> str:
    """A stable hash of a loaded Config's effective content — the model_dump
    serialized with sorted keys, so two configs that differ only in key order
    or comments hash the same, and any material change hashes differently."""
    data = cfg.model_dump(mode="json")
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

    # The reviewer / single detector, always a live route.
    add("api.model", getattr(getattr(cfg, "api", None), "model", None), True)

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


def certify_run(run_dir: str | Path, *, manifest: dict[str, Any] | None = None,
                cfg: Any | None = None, source: str | Path | None = None
                ) -> Certificate:
    """Re-check a finished run for delivery. Runs every check whose inputs are
    present; a check whose artifact is absent is recorded as ``skip`` (honest),
    never silently passed. Delivery should require zero ``fail`` checks."""
    run = Path(run_dir)
    cert = Certificate()

    # 1. Source + config hashes match the approval (if one is given).
    if manifest is not None and source is not None:
        cert.checks.append(_check_hash("source hash", sha256_file(source),
                                       manifest.get("source_sha256")))
    if manifest is not None and cfg is not None:
        cert.checks.append(_check_hash("config hash", config_hash(cfg),
                                       manifest.get("config_sha256")))

    # 2. Model routes stay within the approved set (route check only —
    # source/config hashes are checked separately above).
    if manifest is not None and cfg is not None:
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
    envelope = _load_json(run / "findings.json")
    if envelope is None:
        envelope = _load_json(run / "flights_findings.json")
    if envelope is None:
        cert.checks.append(Check("findings envelope", "skip",
                                 "no findings.json in the run directory"))
    else:
        cert.checks.extend(_certify_envelope(envelope, run, manifest))

    # 4. Deterministic artifact scan over the change log / findings text.
    cert.checks.append(_artifact_scan(run))
    return cert


def _check_hash(name: str, actual: str, expected: Any) -> Check:
    if not expected:
        return Check(name, "skip", "approval carries no hash to compare")
    if actual == expected:
        return Check(name, "pass", f"{actual[:12]}…")
    return Check(name, "fail",
                 f"run {actual[:12]}… != approved {str(expected)[:12]}…")


def _certify_envelope(envelope: dict[str, Any], run: Path,
                      manifest: dict[str, Any] | None) -> list[Check]:
    checks: list[Check] = []
    cost = (envelope.get("cost") or {}).get("total_usd")

    # Checkpoint completeness — a finished paid run should have left one.
    has_ckpt = (run / "findings.checkpoint.json").is_file() \
        or envelope.get("checkpoint") is not None
    paid = bool(cost) and float(cost) > 0
    if paid:
        checks.append(Check("checkpoint present",
                            "pass" if has_ckpt else "fail",
                            "findings.checkpoint.json / envelope checkpoint "
                            "found" if has_ckpt else "a paid run left no "
                            "checkpoint — a crash could not resume"))
    else:
        checks.append(Check("checkpoint present", "skip",
                            "no paid spend recorded"))

    # Zero-cost anomaly: findings exist and detectors ran, but cost is $0.
    n_findings = len(envelope.get("findings") or [])
    if cost is not None and float(cost) == 0.0 and n_findings > 0 \
            and envelope.get("judge_model") not in ("external:import-judgments",):
        checks.append(Check("zero-cost anomaly", "fail",
                            f"{n_findings} finding(s) but $0.00 recorded — a "
                            f"detector that should bill likely did not run"))
    else:
        checks.append(Check("zero-cost anomaly", "pass",
                            f"cost ${float(cost):.2f} for {n_findings} "
                            f"finding(s)" if cost is not None
                            else "no cost field to reconcile"))

    # Budget reconciliation against the approval ceiling.
    if manifest is not None and cost is not None:
        cap = float(manifest.get("max_spend_usd", 0.0))
        checks.append(Check("budget within approval",
                            "pass" if float(cost) <= cap else "fail",
                            f"${float(cost):.2f} of ${cap:.2f} approved"))
    return checks


def _artifact_scan(run: Path) -> Check:
    texts: list[str] = []
    for name in ("change_log.md", "changes.md", "findings.json",
                 "flights_findings.json"):
        p = run / name
        if p.is_file():
            try:
                texts.append(p.read_text(encoding="utf-8"))
            except OSError:
                pass
    if not texts:
        return Check("artifact scan", "skip", "no change log to scan")
    blob = "\n".join(texts)
    hits = [pat for pat in _ARTIFACTS if pat in blob]
    if hits:
        return Check("artifact scan", "fail",
                     f"found merge artifact(s): {', '.join(map(repr, hits))}")
    return Check("artifact scan", "pass", "no doubled punctuation / merge "
                 "artifacts in the change log")


__all__ = [
    "MANIFEST_SCHEMA_VERSION", "ModelRoute", "Deviation", "Check",
    "Certificate", "sha256_file", "config_hash", "model_routes",
    "providers_in_use", "build_manifest", "verify_plan", "certify_run",
]
