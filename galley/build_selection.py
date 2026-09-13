"""Restore the selected corrected build from durable, source-bound evidence."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from docproof.utils.files import write_atomic


class BuildSelectionError(ValueError):
    pass


_FINAL_STATES = {"adjudicated", "settled", "astra_reviewed", "certified", "delivered"}


def _later_transaction(selected, history, source_sha256):
    """The CLI writes its validated pin before committing its state record."""
    from galley.state_machine import RUN_STATES
    selected_by = selected.get("selected_by")
    target_state = ({"adjudicate": "adjudicated", "settle": "settled"}.get(selected_by)
                    if isinstance(selected_by, str) else None)
    if not target_state or selected.get("source_sha256") != source_sha256 or not history:
        return False
    latest = history[-1]
    if not isinstance(latest, dict) or latest.get("state") not in RUN_STATES:
        return False
    if RUN_STATES.index(target_state) < RUN_STATES.index(latest["state"]):
        return False
    try:
        saved_at = datetime.fromisoformat(selected["at"])
        previous_at = datetime.fromisoformat(latest["at"])
        return (saved_at.tzinfo is not None and previous_at.tzinfo is not None
                and saved_at > previous_at)
    except (KeyError, TypeError, ValueError):
        return False


def _read(path):
    try:
        value = json.loads(path.read_text("utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def final_run(workspace: Path, source_sha256: str) -> Path | None:
    """A damaged pointer never silently selects a newer, unrelated run."""
    workspace = workspace.resolve()
    directory = workspace / "runs"
    pin = directory / "driver/final-run.json"

    def candidate(value, *, manuscript=False):
        if not isinstance(value, str) or not value:
            return None
        path = (workspace / value).resolve()
        if not path.is_relative_to(directory) or not (path / "findings.json").is_file():
            return None
        if manuscript:
            from galley.verify import deliverable_docx
            if deliverable_docx(path) is None:
                return None
        return path

    def restore(target, label):
        pin.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(pin, json.dumps({"run": str(target.relative_to(workspace)),
            "source_sha256": source_sha256, "recovered_from": label}, indent=2))
        return target

    selected = _read(pin)
    if selected.get("source_sha256") not in (None, "", source_sha256):
        raise BuildSelectionError("Selected manuscript belongs to a different source revision")
    state = _read(workspace / "state.json")
    if state.get("source_sha256") not in (None, "", source_sha256):
        raise BuildSelectionError("Run state belongs to a different source revision")
    history = state.get("history")
    history = history if isinstance(history, list) else []
    final_history = [r for r in history if isinstance(r, dict)
                     and isinstance(r.get("state"), str) and r["state"] in _FINAL_STATES]
    selections = [r for r in final_history if r.get("results_run")]
    if selections:
        # The latest explicit final state outranks an older pin or archive.
        # If its corrected document is missing, no earlier build substitutes.
        row = selections[-1]
        if row.get("source_sha256") not in (None, "", source_sha256):
            raise BuildSelectionError("Selected build state belongs to a different source")
        # A later validated selection may be the first half of a state
        # transaction interrupted after pin write. Preserve that forward
        # selection instead of silently returning to the previous manuscript.
        if _later_transaction(selected, history, source_sha256):
            pending = candidate(selected.get("run"), manuscript=True)
            findings = _read(pending / "findings.json") if pending else {}
            if pending is not None and isinstance(findings.get("findings"), list):
                return pending
            raise BuildSelectionError("Pending selected corrected manuscript is missing or invalid")
        target = candidate(row["results_run"], manuscript=True)
        if target is None:
            raise BuildSelectionError("Recorded corrected manuscript is missing; cannot substitute another build")
        if candidate(selected.get("run")) == target:
            return target
        return restore(target, "state")
    target = candidate(selected.get("run"))
    if target is not None:
        return target

    # A stale package or incomplete metadata write must not hide another valid
    # source-bound record. Collect corroborating candidates; conflicting valid
    # locations remain ambiguous rather than being chosen by modification time.
    proofs = []
    recorded = False
    package = _read(directory / "driver/package.json")
    if package.get("run"):
        recorded = True
        target = candidate(package["run"], manuscript=True)
        if target is not None and state.get("source_sha256") == source_sha256:
            from galley.verify import build_fingerprints
            try:
                fingerprints = build_fingerprints(target) or {}
            except (OSError, ValueError):
                fingerprints = {}
            if (package.get("build_sha256")
                    and fingerprints.get("build_sha256") == package["build_sha256"]):
                proofs.append((target, "package"))
    engine = directory / "driver/engine"
    loop = _read(engine / "review-loop.json").get("identity")
    loop = loop if isinstance(loop, dict) else {}
    coverage = _read(engine / "initial-coverage.json")
    for label, record in (("review-loop", loop), ("initial-coverage", coverage)):
        if record.get("run") and record.get("source") == source_sha256:
            recorded = True
            target = candidate(record["run"], manuscript=True)
            if target is not None:
                proofs.append((target, label))
    if proofs:
        if len({target for target, _label in proofs}) != 1:
            raise BuildSelectionError("Saved final evidence identifies conflicting corrected manuscripts")
        return restore(*proofs[0])
    if recorded:
        raise BuildSelectionError("Recorded corrected manuscript is missing or unproven; cannot substitute another build")
    if pin.exists():
        raise BuildSelectionError("Cannot establish the selected corrected build from saved evidence")
    runs = sorted(directory.glob("*/findings.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if final_history and len(runs) > 1:
        raise BuildSelectionError("Legacy completed run has multiple possible builds and no selection evidence")
    # Older workspaces did not stamp selected builds. A sole candidate is
    # unambiguous; before selection the ladder still uses newest-run discovery.
    return runs[0].parent if runs else None
