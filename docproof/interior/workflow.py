"""A resumable local correction job. Only verified artifacts become deliverables."""
from __future__ import annotations

from docproof import platform_io as fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from zipfile import ZIP_DEFLATED, ZipFile
from contextlib import contextmanager
from pathlib import Path

from .verify import VerificationError, check_saved, prepare_edits, validate_plan


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=True, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(10):
            try:
                os.replace(name, path)
                break
            except PermissionError:
                # A simultaneous desktop status reader can briefly hold a
                # Windows handle without delete sharing. Preserve the old
                # complete record while waiting for that reader to close.
                if os.name != 'nt' or attempt == 9:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def worker_lock():
    """One full book operation for this user, including between native calls."""
    path = Path(tempfile.gettempdir()) / f"docproof-interior-job-{fcntl.user_lock_suffix()}.lock"
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("The InDesign correction worker is processing another book.") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def next_name(source: Path) -> str:
    match = re.fullmatch(r"(.+?\s*[-–—]\s*Book\s*)(\d+)\.indd", source.name, re.I)
    if not match:
        raise ValueError("The source must be named 'Last Name - Book N.indd'.")
    return f"{match[1]}{int(match[2]) + 1}.indd"


def review_pages(before_pdf: Path, after_pdf: Path, work_dir: Path, *, render=True) -> dict:
    from pypdf import PdfReader
    before, after = PdfReader(before_pdf), PdfReader(after_pdf)
    all_pages = list(range(1, len(after.pages) + 1))
    changed = [n for n in all_pages if n > len(before.pages)
               or before.pages[n-1].extract_text() != after.pages[n-1].extract_text()]
    images = []
    if render:
        folder = work_dir / "review-pages"
        folder.mkdir(exist_ok=True)
        for label, path in (("before", before_pdf), ("after", after_pdf)):
            prefix = folder / label
            subprocess.run(["pdftoppm", "-scale-to", "1400", "-png", str(path), str(prefix)],
                           check=True, capture_output=True, timeout=600)
        for n in all_pages:
            def locate(label, page):
                matches = [p for p in folder.glob(f"{label}-*.png")
                           if int(p.stem.rsplit("-", 1)[1]) == page]
                if len(matches) != 1:
                    raise RuntimeError(f"Missing rendered {label} page {page}.")
                return str(matches[0])
            images.append({"page": n, "after": locate("after", n),
                           "before": locate("before", n) if n <= len(before.pages) else ""})
    # Compare every rendered page, then spend visual review on every changed
    # page plus its neighbors. This catches artwork-only and formatting changes.
    image_changed = [row["page"] for row in images if not row["before"]
                     or digest(Path(row["before"])) != digest(Path(row["after"]))]
    affected = set(changed + image_changed)
    if len(before.pages) != len(after.pages) and all_pages:
        affected.add(all_pages[-1])
    required = sorted({p for n in affected for p in (n-1, n, n+1) if p in all_pages})
    return {"required_review_pages": required, "text_changed_pages": changed,
            "image_changed_pages": image_changed, "pages_compared": len(all_pages),
            "review_images": [row for row in images if row["page"] in required],
            "removed_pages": max(0, len(before.pages) - len(after.pages))}


def _artifact(snapshot, key, fallback):
    return Path(snapshot.get(key) or fallback).resolve()


_REVIEW_CHECKPOINT_VERSION = 1


def _checkpoint_entry(work: Path, path: Path) -> dict[str, str]:
    """Record one immutable, work-contained checkpoint artifact."""
    path = Path(path).resolve()
    if not path.is_file() or not path.stat().st_size:
        raise RuntimeError(f"Review checkpoint artifact is missing or empty: {path.name}.")
    try:
        relative = path.relative_to(work)
    except ValueError as exc:
        raise RuntimeError("Review checkpoint artifact escaped the job directory.") from exc
    return {"path": relative.as_posix(), "sha256": digest(path)}


def _checkpoint_path(work: Path, entry: dict, label: str) -> Path:
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
        raise RuntimeError(f"Review checkpoint is missing its {label} path.")
    relative = Path(entry["path"])
    if relative.is_absolute():
        raise RuntimeError(f"Review checkpoint {label} path is not job-relative.")
    path = (work / relative).resolve()
    try:
        path.relative_to(work)
    except ValueError as exc:
        raise RuntimeError(f"Review checkpoint {label} path escaped the job directory.") from exc
    if not path.is_file() or digest(path) != entry.get("sha256"):
        raise RuntimeError(f"Review checkpoint artifact changed or is missing: {label}.")
    return path


def _make_review_checkpoint(work: Path, source: Path, output: Path,
                            before_pdf: Path, after_pdf: Path, idml: Path,
                            final: dict, baseline: dict) -> dict:
    images = []
    required = set(final.get("required_review_pages", []))
    rows = {row.get("page"): row for row in final.get("review_images", [])
            if isinstance(row, dict) and isinstance(row.get("page"), int)}
    baseline_pages = int(baseline.get("page_count", baseline.get("pages", 0)) or 0)
    for page in sorted(required):
        row = rows.get(page)
        if not row or not row.get("after"):
            raise RuntimeError(f"Review checkpoint is missing the after image for page {page}.")
        before = row.get("before")
        if page <= baseline_pages and not before:
            raise RuntimeError(f"Review checkpoint is missing the before image for page {page}.")
        images.append({
            "page": page,
            "before": _checkpoint_entry(work, Path(before)) if before else None,
            "after": _checkpoint_entry(work, Path(row["after"])),
        })
    return {
        "version": _REVIEW_CHECKPOINT_VERSION,
        "source": {"path": str(Path(source).resolve()), "sha256": digest(source)},
        "artifacts": {
            "output_indd": _checkpoint_entry(work, output),
            "baseline_pdf": _checkpoint_entry(work, before_pdf),
            "output_pdf": _checkpoint_entry(work, after_pdf),
            "output_idml": _checkpoint_entry(work, idml),
            "final_json": _checkpoint_entry(work, work / "final.json"),
            "verification_json": _checkpoint_entry(work, work / "verification.json"),
        },
        "review_images": images,
        "required_review_pages": sorted(required),
    }


def _restore_review_checkpoint(work: Path, source: Path, checkpoint: dict,
                               baseline: dict, plan: dict) -> tuple[dict, dict, Path, Path, Path]:
    if checkpoint.get("version") != _REVIEW_CHECKPOINT_VERSION:
        raise RuntimeError("Unsupported or incomplete review checkpoint.")
    source_info = checkpoint.get("source")
    if (not isinstance(source_info, dict)
            or Path(source_info.get("path", "")).resolve() != source.resolve()
            or source_info.get("sha256") != digest(source)):
        raise RuntimeError("Review checkpoint source changed.")
    artifacts = checkpoint.get("artifacts")
    if not isinstance(artifacts, dict):
        raise RuntimeError("Review checkpoint has no artifact manifest.")
    paths = {key: _checkpoint_path(work, artifacts.get(key), key)
             for key in ("output_indd", "baseline_pdf", "output_pdf", "output_idml",
                         "final_json", "verification_json")}
    expected_output = (work / next_name(source)).resolve()
    if paths["output_indd"] != expected_output:
        raise RuntimeError("Review checkpoint output INDD path changed.")
    expected_baseline_pdf = _artifact(baseline, "baseline_pdf", work / "baseline.pdf")
    if paths["baseline_pdf"] != expected_baseline_pdf:
        raise RuntimeError("Review checkpoint baseline PDF path changed.")
    final = json.loads(paths["final_json"].read_text(encoding="utf-8"))
    verification = json.loads(paths["verification_json"].read_text(encoding="utf-8"))
    if final.get("verification") != verification:
        raise RuntimeError("Review checkpoint final and verification JSON disagree.")
    required = checkpoint.get("required_review_pages")
    if (not isinstance(required, list) or any(type(page) is not int for page in required)
            or len(required) != len(set(required))):
        raise RuntimeError("Review checkpoint required-page coverage is invalid.")
    if final.get("required_review_pages") != required:
        raise RuntimeError("Review checkpoint required-page coverage changed.")
    checkpoint_images = checkpoint.get("review_images")
    if not isinstance(checkpoint_images, list):
        raise RuntimeError("Review checkpoint final JSON has no review images.")
    final_images = final.get("review_images")
    if not isinstance(final_images, list):
        raise RuntimeError("Review checkpoint final JSON has no review images.")
    checkpoint_pages = [row.get("page") if isinstance(row, dict) else None for row in checkpoint_images]
    final_pages = [row.get("page") if isinstance(row, dict) else None for row in final_images]
    if (any(type(page) is not int for page in checkpoint_pages + final_pages)
            or len(checkpoint_pages) != len(set(checkpoint_pages))
            or set(checkpoint_pages) != set(required)
            or len(final_pages) != len(set(final_pages))
            or set(final_pages) != set(required)):
        raise RuntimeError("Review checkpoint image coverage changed.")
    image_rows = {row["page"]: row for row in final_images}
    for image in checkpoint_images:
        page = image.get("page") if isinstance(image, dict) else None
        row = image_rows.get(page)
        if not row:
            raise RuntimeError(f"Review checkpoint lost review images for page {page}.")
        for side in ("before", "after"):
            entry = image.get(side)
            if entry is None:
                if row.get(side):
                    raise RuntimeError(f"Review checkpoint image set changed for page {page}.")
                continue
            path = _checkpoint_path(work, entry, f"page {page} {side}")
            if Path(row.get(side, "")).resolve() != path:
                raise RuntimeError(f"Review checkpoint image path changed for page {page}.")
    for field, key, label in (("output_pdf", "output_pdf", "PDF"),
                               ("output_idml", "output_idml", "IDML")):
        declared = final.get(field)
        if declared and Path(declared).resolve() != paths[key]:
            raise RuntimeError(f"Review checkpoint final {label} path changed.")
    if not verification.get("integrity_passed"):
        raise VerificationError("Saved document failed exact text verification: "
                                + "; ".join(verification.get("failures", [])))
    # Re-run the deterministic text/style check from the frozen JSON. This
    # keeps a model-review retry subject to the same integrity gate.
    checked = check_saved(baseline, final, plan["edits"])
    if checked != verification:
        raise RuntimeError("Review checkpoint deterministic verification changed.")
    return final, verification, paths["baseline_pdf"], paths["output_pdf"], paths["output_idml"]


def _report(work: Path, result: dict, plan: dict | None = None) -> Path:
    path = work / "correction-report.json"
    packet_path = work / "packet.json"
    packet = json.loads(packet_path.read_text(encoding='utf-8')) if packet_path.exists() else {}
    save_json(path, {**result, "instructions": (plan or {}).get("instructions", []),
                     "edits": (plan or {}).get("edits", []), "source_evidence": packet.get("evidence", [])})
    lines = ["InDesign corrections", "", f"Outcome: {result['status']}",
             f"Designer needed: {result.get('needs_designer')}", ""]
    lines.extend(f"- {reason}" for reason in result.get("reasons", []))
    for item in (plan or {}).get("instructions", []):
        lines.extend(["", f"{item['id']}: {item['disposition']}", item.get("reason", "")])
    (work / "correction-report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def package_result(work: Path, output: Path, pdf: Path, idml: Path) -> Path:
    """Keep linked artwork and document fonts portable beside the native file."""
    package = work / (output.stem + " - package.zip")
    with ZipFile(package, "w", ZIP_DEFLATED) as archive:
        for path, name in ((output, output.name), (pdf, output.with_suffix(".pdf").name),
                           (idml, output.with_suffix(".idml").name),
                           (work / "correction-report.json", "correction-report.json"),
                           (work / "correction-report.txt", "correction-report.txt")):
            archive.write(path, output.stem + "/" + name)
        for folder in ("Links", "Document fonts"):
            for path in sorted((output.parent / folder).rglob("*")):
                if path.is_file() and not path.name.startswith("."):
                    archive.write(path, output.stem + "/" + str(path.relative_to(output.parent)))
        packet_path = work / "packet.json"
        if packet_path.exists():
            packet = json.loads(packet_path.read_text(encoding='utf-8'))
            for index, source in enumerate(packet.get("sources", []), 1):
                artifact = source.get("artifact_path")
                if artifact and Path(artifact).is_file():
                    path = Path(artifact)
                    archive.write(path, f"{output.stem}/Corrections submitted/{index}-{path.name}")
    with ZipFile(package) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("The portable InDesign package failed its integrity check.")
    return package


def run_local(source: Path, attachments: list[Path], text: str, work_dir: Path,
              rules: dict | None = None, *, native=None, astra=None,
              packet_builder=None, page_reviewer=None, plan_only=False) -> dict:
    """Run/resume a frozen submission; all external boundaries are injectable."""
    work = Path(work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    source = Path(source).resolve()
    attachments = [Path(p).resolve() for p in attachments]
    plan = None
    result = {"status": "technical_block", "needs_designer": None, "reasons": [],
              "job_dir": str(work), "output_indd": "", "output_pdf": "", "output_idml": "",
              "report": str(work / "correction-report.json")}
    try:
        with worker_lock():
            if not attachments and not text.strip():
                raise ValueError("No correction instructions were supplied.")
            identity = {"source": str(source), "source_sha256": digest(source),
                        "attachments": [{"path": str(p), "sha256": digest(p)} for p in attachments],
                        "text": text, "rules": rules or {}}
            request_path = work / "submission.json"
            if request_path.exists():
                if json.loads(request_path.read_text(encoding='utf-8')) != identity:
                    raise ValueError("This job's source or submission changed. Start a new job folder.")
            else:
                save_json(request_path, identity)
            receipt_path = work / "workflow.json"
            receipt = json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.exists() else {}
            if receipt.get("stage") == "complete":
                for path, sha in receipt.get("artifact_hashes", {}).items():
                    if not Path(path).is_file() or digest(Path(path)) != sha:
                        raise ValueError("A completed job artifact changed or is missing; delivery is blocked.")
                return receipt["result"]
            if receipt.get("stage") == "applying":
                raise RuntimeError("An InDesign edit was interrupted. Inspect the saved job before recovery; edits were not repeated.")
            evidence_path = work / "evidence-manifest.json"
            if evidence_path.exists():
                for path, sha in json.loads(evidence_path.read_text(encoding='utf-8')).items():
                    if not Path(path).is_file() or digest(Path(path)) != sha:
                        raise RuntimeError("Frozen correction evidence changed. This job requires recovery.")
            from .intake import build_packet
            from .routing import LunaFirstReviewer
            from .native import InDesignWorker
            native = native or InDesignWorker()
            astra = astra or LunaFirstReviewer()
            packet_builder = packet_builder or build_packet
            page_reviewer = page_reviewer or review_pages
            packet_path, baseline_path = work / "packet.json", work / "baseline.json"
            if packet_path.exists():
                packet = json.loads(packet_path.read_text(encoding='utf-8'))
            else:
                packet = packet_builder(attachments, text, work)
                save_json(packet_path, packet)
            if (rules or {}).get('book_identity') and packet.get('errors'):
                raise ValueError('Some correction evidence could not be read completely; no book edits were started.')
            if baseline_path.exists():
                baseline = json.loads(baseline_path.read_text(encoding='utf-8'))
            else:
                baseline = native.inspect(source, work)
                save_json(baseline_path, baseline)
            if (rules or {}).get('book_identity'):
                from .book_identity import verify_identity
                verify_identity(baseline, rules['book_identity'])
            plan_path = work / "plan.json"
            if plan_path.exists():
                plan = json.loads(plan_path.read_text(encoding='utf-8'))
            else:
                plan = astra.plan(packet, baseline, work, rules=rules or {})
            validate_plan(plan, packet)
            prepare_edits(baseline, plan["edits"])
            if not plan_path.exists():
                save_json(plan_path, plan)
            if plan_only:
                return {**result, 'status': 'planned', 'counts': {
                    'instructions': len(plan['instructions']), 'edits': len(plan['edits']),
                    'unresolved': sum(row['disposition'] in {'designer', 'clarification'} for row in plan['instructions'])}}
            if not plan['edits'] and any(row.get('disposition') in {'clarification', 'designer'} for row in plan['instructions']):
                raise RuntimeError('No actionable corrections were established. InDesign was not asked to create an unchanged book: '+
                                   '; '.join(plan.get('questions', []) + plan.get('designer_reasons', [])))
            if not evidence_path.exists():
                save_json(evidence_path, {str(p): digest(p) for p in (packet_path, baseline_path, plan_path)})
            output = work / next_name(source)
            if receipt.get("stage") not in {"applied", "reviewing"}:
                save_json(receipt_path, {"stage": "applying"})
                applied = native.apply(source, output, plan["edits"], work)
                save_json(work / "applied.json", applied)
                save_json(receipt_path, {"stage": "applied"})
            checkpoint = receipt.get("review_checkpoint") if receipt.get("stage") == "reviewing" else None
            if checkpoint is not None:
                final, verification, before_pdf, after_pdf, idml = _restore_review_checkpoint(
                    work, source, checkpoint, baseline, plan)
            else:
                # Always inspect the actual saved INDD rather than trust the edit script.
                final = native.verify(output, work)
                if final.get("style_inventory_complete") is False or baseline.get("style_inventory_complete") is False:
                    raise VerificationError("InDesign did not return a complete formatting inventory.")
                verification = check_saved(baseline, final, plan["edits"])
                save_json(work / "verification.json", verification)
                if not verification["integrity_passed"]:
                    raise VerificationError("Saved document failed exact text verification: " + "; ".join(verification["failures"]))
                before_pdf = _artifact(baseline, "baseline_pdf", work / "baseline.pdf")
                after_pdf = _artifact(final, "output_pdf", work / "final.pdf")
                idml = _artifact(final, "output_idml", work / "final.idml")
                for path in (output, before_pdf, after_pdf, idml):
                    if not path.is_file() or not path.stat().st_size:
                        raise RuntimeError(f"InDesign did not produce {path.name}.")
                final.update(page_reviewer(before_pdf, after_pdf, work))
                final["verification"] = verification
                final["instructions"] = plan["instructions"]
                save_json(work / "final.json", final)
                checkpoint = _make_review_checkpoint(work, source, output, before_pdf, after_pdf, idml, final, baseline)
                save_json(receipt_path, {"stage": "reviewing", "review_checkpoint": checkpoint})
            review = astra.review(packet, baseline, final, plan["edits"], work)
            expected_ids = {r["id"] for r in plan["instructions"]}
            reviewed_ids = review.get("instruction_ids", [])
            if len(reviewed_ids) != len(set(reviewed_ids)) or set(reviewed_ids) != expected_ids:
                raise VerificationError("The final review did not cover every instruction.")
            reviewed_pages = review.get("reviewed_pages", [])
            if set(reviewed_pages) != set(final["required_review_pages"]):
                raise VerificationError("The final visual review did not cover every required page.")
            if review.get("status") not in {"verified", "designer_needed", "clarification_needed"}:
                raise VerificationError("Astra returned an unsupported final outcome.")
            save_json(work / "review.json", review)
            dispositions = {r["disposition"] for r in plan["instructions"]}
            reasons = verification["failures"] + plan.get("designer_reasons", []) + plan.get("questions", []) + review.get("reasons", [])
            if not verification["passed"] or "designer" in dispositions or review["status"] == "designer_needed":
                outcome = "designer_needed"
            elif "clarification" in dispositions or review["status"] == "clarification_needed":
                outcome = "clarification_needed"
            else:
                outcome = "verified"
            result.update(status=outcome, needs_designer=(True if outcome == "designer_needed" else False if outcome == "verified" else None),
                          reasons=list(dict.fromkeys(reasons)), output_indd=str(output), output_pdf=str(after_pdf),
                          output_idml=str(idml), counts={"instructions": len(plan["instructions"]),
                          "edits": verification["edits_checked"], "unresolved": sum(r["disposition"] in {"designer", "clarification"} for r in plan["instructions"])},
                          verification=verification)
            _report(work, result, plan)
            package = package_result(work, output, after_pdf, idml)
            result["output_package"] = str(package)
            hashes = {str(p): digest(p) for p in (output, after_pdf, idml, package, work / "correction-report.json")}
            save_json(receipt_path, {"stage": "complete", "result": result, "artifact_hashes": hashes})
            return result
    except Exception as exc:
        result["reasons"] = [f"{type(exc).__name__}: {exc}"]
        # A failed resume must not overwrite the report protected by a completed receipt.
        report = work / "correction-report.json"
        if not report.exists():
            _report(work, result, plan)
        save_json(work / "technical-block.json", result)
        return result
