"""Connect independent verification outputs and explicit author queries to settlement."""
from __future__ import annotations

import hashlib
import json
import os
import fcntl
from pathlib import Path

from docproof.utils.files import write_atomic

REGISTRY = "verification-sources.json"
SNAPSHOTS = ".verification-inputs"
CANDIDATES = "verification-candidates.json"
_ARTIFACTS = (("finished_walk.json", "residuals", "residual", "walk"),
              ("change_verify.json", "problems", "edit_damage", "changes"))


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False,
                                    sort_keys=True).encode()).hexdigest()


def _source_hash(run: Path) -> str:
    from galley.verify import paragraph_views, source_fingerprint
    original, _ = paragraph_views(run)
    if not original:
        raise ValueError(f"No source paragraphs for verification inputs: {run}")
    return source_fingerprint(original)


def verification_dirs(run: str | Path) -> list[Path]:
    """Read registered independent passes, keeping their original build bindings."""
    run = Path(run).resolve()
    path = run / REGISTRY
    if not path.is_file():
        return [run]
    data = json.loads(path.read_text("utf-8"))
    if (not isinstance(data, dict) or data.get("schema_version") not in (1, 2)
            or not isinstance(data.get("directories"), list)
            or any(not isinstance(p, str) or not p for p in data["directories"])):
        raise ValueError("Invalid verification input registry")
    if data.get("source_sha256") != _source_hash(run):
        # A prior clean read has no candidate to misapply to the replacement
        # source. Permit the normal fresh-read path (its proof reuse check will
        # fail); unresolved candidates from another source must stay a blocker.
        directories = [run, *((run / name).resolve() for name in data["directories"])]
        if all(not json.loads((directory / filename).read_text("utf-8")).get(key)
               for directory in directories for filename, key, *_ in _ARTIFACTS):
            return [run]
        raise ValueError("Registered verification inputs belong to a different source")
    dirs = [run]
    for name in data.get("directories", []):
        directory = (run / name).resolve()
        if any(not (directory / name).is_file()
               for name in ("finished_walk.json", "change_verify.json")):
            raise ValueError(f"Registered verification input is missing: {directory}")
        if directory not in dirs:
            dirs.append(directory)
        if data["schema_version"] == 2:
            try:
                directory.relative_to(run / SNAPSHOTS)
            except ValueError as exc:
                raise ValueError("Verification snapshot is outside the registry") from exc
            expected = data.get("artifacts_sha256", {}).get(name)
            if not isinstance(expected, dict) or any(
                    expected.get(filename) != hashlib.sha256((directory / filename).read_bytes()).hexdigest()
                    for filename, *_ in _ARTIFACTS):
                raise ValueError(f"Registered verification input changed: {directory}")
    return dirs


def register_verification_source(run: str | Path, output: str | Path) -> None:
    """Snapshot evidence, never promote its build hashes or verification proof.

    Primary-output reads are registered too: the next independent reader may
    overwrite those two files, but must not erase the earlier reader's work.
    Legacy artifacts retain their missing proof/binding and are labelled as such
    in the candidate ledger. Registration itself proves no coverage.
    """
    run, output = Path(run).resolve(), Path(output).resolve()
    with (run / ".verification-sources.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        source_hash = _source_hash(run)
        # Upgrade a legacy registry by freezing its currently available evidence.
        # A later invocation never rewrites a snapshot that was already saved.
        prior = json.loads((run / REGISTRY).read_text("utf-8")) if (run / REGISTRY).exists() else {}
        if prior and prior.get("source_sha256") != source_hash:
            # A new source draft starts its own intake. Keep the earlier registry
            # as evidence, but never mix its candidates into the replacement book.
            payloads = [json.loads((output / name).read_text("utf-8")) for name, *_ in _ARTIFACTS]
            if not any(p.get("source_sha256") == source_hash for p in payloads):
                raise ValueError("Registered verification inputs belong to a different source")
            retired = run / SNAPSHOTS / ("registry-" + _digest(prior) + ".json")
            retired.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(retired, json.dumps(prior, indent=2))
            prior = {}
            dirs = []
        else:
            dirs = verification_dirs(run)[1:]
        hashes = dict(prior.get("artifacts_sha256") or {})
        origins = dict(prior.get("origins") or {})
        snapshots = []
        for directory in [*dirs, output]:
            if directory in dirs and prior.get("schema_version") == 2:
                snapshot = directory
            else:
                contents = {}
                pair_ids = []
                for filename, key, *_ in _ARTIFACTS:
                    try:
                        raw = (directory / filename).read_bytes()
                        payload = json.loads(raw)
                    except (OSError, ValueError) as exc:
                        raise ValueError(f"Unreadable verification input: {directory / filename}") from exc
                    if (not isinstance(payload, dict) or not isinstance(payload.get(key, []), list)
                            or any(not isinstance(row, dict) for row in payload.get(key, []))):
                        raise ValueError(f"Invalid verification input: {directory / filename}")
                    if payload.get("source_sha256") not in (None, source_hash):
                        raise ValueError(f"Verification input belongs to a different source: {directory / filename}")
                    pair_ids.append(payload.get("verification_pair_id"))
                    contents[filename] = raw
                if len(set(pair_ids)) != 1:
                    raise ValueError(f"Incomplete verification artifact pair: {directory}")
                artifact_hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in contents.items()}
                snapshot = run / SNAPSHOTS / _digest(artifact_hashes)
                snapshot.mkdir(parents=True, exist_ok=True)
                for name, raw in contents.items():
                    target = snapshot / name
                    if target.exists() and target.read_bytes() != raw:
                        raise ValueError(f"Verification snapshot changed: {target}")
                    if not target.exists():
                        write_atomic(target, raw.decode("utf-8"))
                relative = os.path.relpath(snapshot, run)
                hashes[relative] = artifact_hashes
                origins[relative] = os.path.relpath(directory, run)
            if snapshot not in snapshots:
                snapshots.append(snapshot)
        data = {"schema_version": 2, "source_sha256": source_hash,
                "directories": [os.path.relpath(d, run) for d in snapshots],
                "artifacts_sha256": hashes, "origins": origins}
        write_atomic(run / REGISTRY, json.dumps(data, indent=2))
    refresh_candidate_dispositions(run)


def _evidence_key(row: dict, kind: str) -> tuple:
    """Editorial content only; reader IDs, severity and bookkeeping may differ.

    Do not infer that two explanations or suggested corrections mean the same
    thing merely because the residual ID names the same quote.
    """
    if kind == "edit_damage":
        return (kind, str(row.get("para_id", "")), str(row.get("original_text", "")),
                str(row.get("corrected_text", "")), str(row.get("verdict", "")),
                str(row.get("detail", row.get("problem", ""))),
                str(row.get("fix", row.get("suggestion", ""))))
    return (kind, str(row.get("para_id", "")), str(row.get("quote", "")),
            str(row.get("problem", "")), str(row.get("suggestion", "")))


def refresh_candidate_dispositions(run: str | Path, settlement=None) -> dict:
    """Project immutable reader evidence onto the latest settlement records.

    This is an accounting artifact, not authority for verification or delivery.
    Candidate IDs retain every reader's contribution even when settlement uses
    one stable residual ID for repeat detections of the same passage.
    """
    from galley.settle import Residual, Settlement
    run = Path(run).resolve()
    if not (run / REGISTRY).is_file():
        return {"schema_version": 1, "reads": [], "candidates": []}
    if settlement is None:
        settlement = Settlement.load(run)
    records = settlement.latest() if settlement else {}
    reads, candidates = {}, {}
    for directory in verification_dirs(run)[1:]:
        for filename, key, kind, gate in _ARTIFACTS:
            payload = json.loads((directory / filename).read_text("utf-8"))
            proof = payload.get("verification_provenance") or {}
            identity = proof.get("identity") or {}
            # Only actual proof fields identify a resumable reader pass. An
            # unbound legacy artifact gets an artifact identity, not a new proof.
            read_id = _digest({"gate": gate, "proof": proof}) if proof else _digest({"gate": gate, "artifact": payload})
            reads[read_id] = {
                "read_id": read_id, "gate": gate, "model": payload.get("model"),
                "engine": payload.get("engine"), "policy": identity.get("policy"),
                "snapshot": os.path.relpath(directory / filename, run),
                "binding": {k: payload.get(k) for k in ("source_sha256", "build_sha256", "accepted_sha256", "paragraph_sha256")},
                "coverage_status": ("recorded_complete" if proof.get("complete") is True else "incomplete") if proof else "unbound",
                "coverage_authority": False,
                "candidate_ids": []}
            for row in payload.get(key, []):
                res = Residual.from_walk(row) if kind == "residual" else Residual.from_problem(row)
                cid = "vc-" + _digest({"read_id": read_id, "kind": kind, "row": row})[:24]
                rec = records.get(res.id)
                disposition = rec.action if rec and rec.action != "internal_repair" else "open"
                candidates[cid] = {"candidate_id": cid, "read_id": read_id,
                    "residual_id": res.id, "kind": kind, "para_id": res.para_id,
                    "evidence": row, "disposition": disposition,
                    "reason": rec.reason if rec else "awaiting settlement",
                    "settlement_record": rec.to_json() if rec else None}
                reads[read_id]["candidate_ids"].append(cid)
    variants, seen = {}, {}
    for row in candidates.values():
        variants.setdefault(row["residual_id"], set()).add(_evidence_key(row["evidence"], row["kind"]))
    for row in settlement.residuals_seen if settlement else []:
        if isinstance(row, dict):
            rid = str(row.get("residual_id") or row.get("problem_id") or "")
            kind = str(row.get("kind") or ("edit_damage" if row.get("problem_id") else "residual"))
            seen.setdefault(rid, set()).add(_evidence_key(row, kind))
    for row in candidates.values():
        rec = records.get(row["residual_id"])
        if rec is None or row["disposition"] == "open":
            continue
        key = _evidence_key(row["evidence"], row["kind"])
        prior = seen.get(row["residual_id"], set())
        if rec.input_evidence is not None:
            supported = key == _evidence_key(rec.input_evidence, rec.kind)
        elif prior:
            supported = prior == {key}
        else:
            # Keep unambiguous legacy terminal records valid. With conflicting
            # evidence and no decision input, neither variant inherits closure.
            supported = variants[row["residual_id"]] == {key}
        if not supported:
            row["disposition"] = "open"
            row["reason"] = "conflicting_reader_evidence_requires_final_review"
    data = {"schema_version": 1,
        "source_sha256": _source_hash(run), "reads": list(reads.values()),
        "candidates": list(candidates.values())}
    write_atomic(run / CANDIDATES, json.dumps(data, ensure_ascii=False, indent=2))
    return data


def unresolved_candidates(run: str | Path) -> list[dict]:
    """Exact per-reader evidence still requiring settlement or final review."""
    return [row for row in refresh_candidate_dispositions(run)["candidates"]
            if row["disposition"] == "open"]


def load_queries(path: str | Path | None, run: str | Path, items) -> dict[str, dict]:
    """Only exact, current residuals can receive a practitioner author query."""
    if not path:
        return {}
    from galley.verify import deliverable_docx
    doc = deliverable_docx(run)
    data = json.loads(Path(path).read_text("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("--queries must be a JSON object")
    if doc is None or data.get("build_sha256") != hashlib.sha256(doc.read_bytes()).hexdigest():
        raise ValueError("--queries must name the current deliverable build_sha256")
    rows = data.get("queries")
    if not isinstance(rows, list) or not rows:
        raise ValueError("--queries must contain a nonempty queries array")
    by_id = {r.id: r for r in items}
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each query must be an object")
        rid = row.get("residual_id")
        if not isinstance(rid, str):
            raise ValueError("Query residual_id must be a string")
        res = by_id.get(rid)
        if res is None or res.kind != "residual" or rid in out:
            raise ValueError(f"Query must identify a unique open residual: {rid}")
        if row.get("para_id") != res.para_id or row.get("quote") != res.quote:
            raise ValueError(f"Query evidence does not match residual {rid}")
        if any(not isinstance(row.get(k), str) or not row[k].strip()
               for k in ("question", "missing_knowledge")):
            raise ValueError(f"Query {rid} requires question and missing_knowledge")
        out[rid] = row
    return out
