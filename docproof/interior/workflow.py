"""A resumable local correction job. Only verified artifacts become deliverables."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
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
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def worker_lock():
    """One full book operation on this Mac, including between native calls."""
    path = Path(tempfile.gettempdir()) / f"docproof-interior-job-{os.getuid()}.lock"
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


def _report(work: Path, result: dict, plan: dict | None = None) -> Path:
    path = work / "correction-report.json"
    packet_path = work / "packet.json"
    packet = json.loads(packet_path.read_text()) if packet_path.exists() else {}
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
            packet = json.loads(packet_path.read_text())
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
              packet_builder=None, page_reviewer=None) -> dict:
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
                if json.loads(request_path.read_text()) != identity:
                    raise ValueError("This job's source or submission changed. Start a new job folder.")
            else:
                save_json(request_path, identity)
            receipt_path = work / "workflow.json"
            receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
            if receipt.get("stage") == "complete":
                for path, sha in receipt.get("artifact_hashes", {}).items():
                    if not Path(path).is_file() or digest(Path(path)) != sha:
                        raise ValueError("A completed job artifact changed or is missing; delivery is blocked.")
                return receipt["result"]
            if receipt.get("stage") == "applying":
                raise RuntimeError("An InDesign edit was interrupted. Inspect the saved job before recovery; edits were not repeated.")
            evidence_path = work / "evidence-manifest.json"
            if evidence_path.exists():
                for path, sha in json.loads(evidence_path.read_text()).items():
                    if not Path(path).is_file() or digest(Path(path)) != sha:
                        raise RuntimeError("Frozen correction evidence changed. This job requires recovery.")
            from .intake import build_packet
            from .astra import AstraReviewer
            from .native import InDesignWorker
            native = native or InDesignWorker()
            astra = astra or AstraReviewer()
            packet_builder = packet_builder or build_packet
            page_reviewer = page_reviewer or review_pages
            packet_path, baseline_path = work / "packet.json", work / "baseline.json"
            if packet_path.exists():
                packet = json.loads(packet_path.read_text())
            else:
                packet = packet_builder(attachments, text, work)
                save_json(packet_path, packet)
            if baseline_path.exists():
                baseline = json.loads(baseline_path.read_text())
            else:
                baseline = native.inspect(source, work)
                save_json(baseline_path, baseline)
            plan_path = work / "plan.json"
            if plan_path.exists():
                plan = json.loads(plan_path.read_text())
            else:
                plan = astra.plan(packet, baseline, work, rules=rules or {})
                validate_plan(plan, packet)
                prepare_edits(baseline, plan["edits"])
                save_json(plan_path, plan)
            validate_plan(plan, packet)
            prepare_edits(baseline, plan["edits"])
            if not evidence_path.exists():
                save_json(evidence_path, {str(p): digest(p) for p in (packet_path, baseline_path, plan_path)})
            output = work / next_name(source)
            if receipt.get("stage") not in {"applied", "reviewing"}:
                save_json(receipt_path, {"stage": "applying"})
                applied = native.apply(source, output, plan["edits"], work)
                save_json(work / "applied.json", applied)
                save_json(receipt_path, {"stage": "applied"})
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
            save_json(receipt_path, {"stage": "reviewing"})
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
