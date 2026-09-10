"""Connect independent verification outputs and explicit author queries to settlement."""
from __future__ import annotations

import hashlib
import json
import os
import fcntl
from pathlib import Path

from docproof.utils.files import write_atomic

REGISTRY = "verification-sources.json"


def _source_hash(run: Path) -> str:
    from galley.verify import paragraph_views
    original, _ = paragraph_views(run)
    if not original:
        raise ValueError(f"No source paragraphs for verification inputs: {run}")
    return hashlib.sha256(json.dumps(original, ensure_ascii=False,
                                    sort_keys=True).encode()).hexdigest()


def verification_dirs(run: str | Path) -> list[Path]:
    """Read registered independent passes, keeping their original build bindings."""
    run = Path(run).resolve()
    path = run / REGISTRY
    if not path.is_file():
        return [run]
    data = json.loads(path.read_text("utf-8"))
    if (not isinstance(data, dict) or data.get("schema_version") != 1
            or not isinstance(data.get("directories"), list)
            or any(not isinstance(p, str) or not p for p in data["directories"])):
        raise ValueError("Invalid verification input registry")
    if data.get("source_sha256") != _source_hash(run):
        raise ValueError("Registered verification inputs belong to a different source")
    dirs = [run]
    for name in data.get("directories", []):
        directory = (run / name).resolve()
        if any(not (directory / name).is_file()
               for name in ("finished_walk.json", "change_verify.json")):
            raise ValueError(f"Registered verification input is missing: {directory}")
        if directory not in dirs:
            dirs.append(directory)
    return dirs


def register_verification_source(run: str | Path, output: str | Path) -> None:
    run, output = Path(run).resolve(), Path(output).resolve()
    if run == output:
        return
    with (run / ".verification-sources.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        dirs = verification_dirs(run)
        if output not in dirs:
            dirs.append(output)
        data = {"schema_version": 1, "source_sha256": _source_hash(run),
                "directories": [os.path.relpath(d, run) for d in dirs if d != run]}
        write_atomic(run / REGISTRY, json.dumps(data, indent=2))


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
