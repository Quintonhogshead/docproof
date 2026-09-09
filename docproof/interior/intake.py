"""Build complete, visual-friendly evidence packets for interior review.

The interior workflow accepts a mixture of marked PDFs, Word files, images and
plain text.  This reader is deliberately conservative: extraction failures are
recorded beside the source and never turn a flattened PDF into an empty source.
Rendered pages are retained so the Astra worker can inspect the visual proof
even when a PDF has no text layer or annotation objects.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import mimetypes
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


class IntakeError(RuntimeError):
    """Raised only for invalid invocation, never for a per-source read error."""


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS = {"w": _W}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_name(path: Path) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in path.name)


def _evidence_id(source_id: str, kind: str, ordinal: int = 0) -> str:
    raw = f"{source_id}|{kind}|{ordinal}".encode("utf-8")
    return "evidence-" + hashlib.sha256(raw).hexdigest()[:20]


def _error(source_id: str, kind: str, message: str, *, actionable: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {"source_id": source_id, "kind": kind, "message": message}
    if actionable:
        row["actionable"] = actionable
    return row


def _run_render(pdf: Path, destination: Path) -> tuple[list[Path], list[str]]:
    """Render every PDF page with pdftoppm, then pdftocairo when available."""
    destination.mkdir(parents=True, exist_ok=True)
    prefix = destination / "page"
    errors: list[str] = []
    commands = [
        ["pdftoppm", "-png", "-r", "150", str(pdf), str(prefix)],
        ["pdftocairo", "-png", "-r", "150", str(pdf), str(prefix)],
    ]
    for command in commands:
        for previous in destination.glob("page-*.png"):
            previous.unlink(missing_ok=True)
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            errors.append(f"{command[0]} is not installed")
            continue
        except OSError as exc:
            errors.append(f"{command[0]} could not start: {exc}")
            continue
        if result.returncode == 0:
            files = sorted(destination.glob("page-*.png"), key=lambda p: _page_number(p.name))
            if files:
                # A fallback renderer completed the visual evidence.  Its
                # predecessor's failure is an implementation detail, not a
                # missing page that should block an otherwise complete packet.
                return files, []
            errors.append(f"{command[0]} completed without producing PNG pages")
        else:
            detail = (result.stderr or result.stdout or "no diagnostic").strip()
            errors.append(f"{command[0]} failed ({result.returncode}): {detail[-500:]}")
    errors.append("Install Poppler (pdftoppm or pdftocairo) and retry so Astra can inspect every page visually.")
    return [], errors


def _page_number(name: str) -> int:
    try:
        return int(name.rsplit("-", 1)[1].split(".", 1)[0])
    except (ValueError, IndexError):
        return 0


def _pdf_annotations(page: Any) -> list[dict[str, Any]]:
    annotations: list[dict[str, Any]] = []
    for ordinal, annotation in enumerate(page.get("/Annots", []) or []):
        try:
            obj = annotation.get_object() if hasattr(annotation, "get_object") else annotation
            if not isinstance(obj, dict):
                continue
            row: dict[str, Any] = {"ordinal": ordinal}
            for key, output in (("/Subtype", "subtype"), ("/Contents", "contents"),
                                ("/T", "author"), ("/NM", "name"), ("/Rect", "rect"),
                                ("/QuadPoints", "quad_points")):
                value = obj.get(key)
                if value is not None:
                    try:
                        value = value.get_object() if hasattr(value, "get_object") else value
                    except Exception:  # pragma: no cover - malformed optional PDF object
                        pass
                    if isinstance(value, (str, int, float, bool)) or value is None:
                        row[output] = value
                    else:
                        row[output] = list(value) if hasattr(value, "__iter__") else str(value)
            action = obj.get("/A")
            if action is not None:
                try:
                    action = action.get_object() if hasattr(action, "get_object") else action
                except Exception:
                    action = str(action)
                if isinstance(action, dict):
                    row["action"] = {str(k): str(v) for k, v in action.items()}
                else:
                    row["action"] = str(action)
            annotations.append(row)
        except Exception as exc:  # retain a trace for malformed annotation objects
            annotations.append({"ordinal": ordinal, "error": str(exc)})
    return annotations


def _copy_artifact(path: Path, work_dir: Path, source_id: str) -> str:
    destination = work_dir / "evidence" / f"{source_id}-{_safe_name(path)}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copyfile(path, destination)
    return str(destination.resolve())


def _pdf_source(path: Path, source_id: str, work_dir: Path, digest: str) -> tuple[dict, list[dict]]:
    source: dict[str, Any] = {"id": source_id, "kind": "pdf", "name": path.name,
                             "path": str(path.resolve()), "sha256": digest, "pages": [],
                             "annotations": [], "rendered_pages": [], "errors": []}
    source["artifact_path"] = _copy_artifact(path, work_dir, source_id)
    evidence: list[dict[str, Any]] = []
    rendered, render_errors = _run_render(path, work_dir / "renders" / source_id)
    for message in render_errors:
        source["errors"].append(_error(
            source_id, "render", message,
            actionable="Install Poppler and ensure pdftoppm or pdftocairo is on PATH; retry the intake."))
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
    except Exception as exc:
        source["errors"].append(_error(source_id, "pdf_read", f"Could not open PDF: {exc}"))
        reader = None
    page_count = len(reader.pages) if reader is not None else len(rendered)
    source["page_count"] = page_count
    for index in range(page_count):
        page_text = ""
        annotations: list[dict[str, Any]] = []
        if reader is not None and index < len(reader.pages):
            page = reader.pages[index]
            try:
                page_text = page.extract_text() or ""
            except Exception as exc:
                source["errors"].append(_error(source_id, "pdf_text", f"Page {index}: {exc}"))
            try:
                annotations = _pdf_annotations(page)
            except Exception as exc:
                source["errors"].append(_error(source_id, "pdf_annotations", f"Page {index}: {exc}"))
        image_path = rendered[index] if index < len(rendered) else None
        image = str(image_path.resolve()) if image_path else None
        if image:
            source["rendered_pages"].append({"page_index": index, "path": image})
        source["annotations"].extend([{**item, "page_index": index} for item in annotations])
        page_row = {"page_index": index, "text": page_text, "annotations": annotations,
                    "image": image, "note": "Complete page evidence; an empty text layer may be a flattened scan."}
        source["pages"].append(page_row)
        evidence.append({"id": _evidence_id(source_id, "pdf-page", index), "source_id": source_id,
                         "kind": "pdf_page", "page_index": index, "text": page_text,
                         "annotations": annotations, "image": image, "note": page_row["note"]})
        for annotation_index, annotation in enumerate(annotations):
            evidence.append({"id": _evidence_id(source_id, "pdf-annotation", index * 100000 + annotation_index),
                             "source_id": source_id, "kind": "pdf_annotation", "page_index": index,
                             "annotation": annotation, "note": "Complete PDF annotation evidence."})
    if not page_count and not rendered:
        source["errors"].append(_error(source_id, "pdf_pages", "PDF has no readable or rendered pages."))
    return source, evidence


def _xml_text(element: ET.Element, *, include_deleted: bool = True) -> str:
    pieces: list[str] = []
    for node in element.iter():
        if node.tag in {f"{{{_W}}}t", f"{{{_W}}}delText"}:
            if include_deleted or node.tag.endswith("t"):
                pieces.append(node.text or "")
        elif node.tag == f"{{{_W}}}tab":
            pieces.append("\t")
        elif node.tag in {f"{{{_W}}}br", f"{{{_W}}}cr"}:
            pieces.append("\n")
    return "".join(pieces)


def _revision_kind(element: ET.Element) -> str | None:
    for parent in element.iterancestors() if hasattr(element, "iterancestors") else ():
        local = parent.tag.rsplit("}", 1)[-1]
        if local in {"ins", "del", "moveFrom", "moveTo"}:
            return local
    return None


def _docx_source(path: Path, source_id: str, work_dir: Path, digest: str) -> tuple[dict, list[dict]]:
    source: dict[str, Any] = {"id": source_id, "kind": "docx", "name": path.name,
                             "path": str(path.resolve()), "sha256": digest, "paragraphs": [],
                             "tables": [], "tracked_changes": [], "comments": [], "images": [], "errors": []}
    source["artifact_path"] = _copy_artifact(path, work_dir, source_id)
    evidence: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            document = ET.fromstring(archive.read("word/document.xml"))
            comments_root = None
            if "word/comments.xml" in names:
                comments_root = ET.fromstring(archive.read("word/comments.xml"))
            comments_by_id: dict[str, dict[str, Any]] = {}
            if comments_root is not None:
                for comment in comments_root.findall("w:comment", _NS):
                    cid = comment.get(f"{{{_W}}}id", "")
                    row = {"id": cid, "author": comment.get(f"{{{_W}}}author"),
                           "date": comment.get(f"{{{_W}}}date"), "text": _xml_text(comment)}
                    comments_by_id[cid] = row
                    source["comments"].append(row)
            for image_name in sorted(n for n in names if n.startswith("word/media/")):
                raw = archive.read(image_name)
                destination = work_dir / "evidence" / source_id / Path(image_name).name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
                source["images"].append({"name": image_name, "path": str(destination.resolve()),
                                         "sha256": _sha256_bytes(raw), "mime": mimetypes.guess_type(image_name)[0]})
            # Paragraphs in table cells are part of the author evidence too.
            # The previous direct-child XPath silently lost revision rows from
            # common interior revision forms.
            paragraphs = document.findall(".//w:body//w:p", _NS)
            for index, paragraph in enumerate(paragraphs):
                paragraph_id = f"{source_id}-paragraph-{index:05d}"
                runs: list[dict[str, Any]] = []
                for node in paragraph.iter():
                    if node.tag not in {f"{{{_W}}}r", f"{{{_W}}}ins", f"{{{_W}}}del",
                                        f"{{{_W}}}moveFrom", f"{{{_W}}}moveTo"}:
                        continue
                    if node.tag != f"{{{_W}}}r":
                        continue
                    text_value = _xml_text(node)
                    if not text_value:
                        continue
                    revision = None
                    parent = paragraph
                    # ElementTree has no parent pointers; inspect enclosing revision
                    # containers by matching the run object while walking paragraph.
                    for container in paragraph.iter():
                        if container.tag.rsplit("}", 1)[-1] in {"ins", "del", "moveFrom", "moveTo"} and node in list(container.iter()):
                            revision = container.tag.rsplit("}", 1)[-1]
                            break
                    runs.append({"text": text_value, "revision": revision})
                    if revision:
                        source["tracked_changes"].append({"id": f"{paragraph_id}-revision-{len(source['tracked_changes']):05d}",
                                                          "paragraph_id": paragraph_id, "kind": revision,
                                                          "text": text_value})
                text_value = _xml_text(paragraph)
                paragraph_row = {"id": paragraph_id, "index": index, "text": text_value,
                                 "runs": runs, "comments": []}
                for marker in paragraph.iter():
                    if marker.tag in {f"{{{_W}}}commentRangeStart", f"{{{_W}}}commentRangeEnd",
                                      f"{{{_W}}}commentReference"}:
                        cid = marker.get(f"{{{_W}}}id", "")
                        if cid in comments_by_id:
                            if cid not in paragraph_row["comments"]:
                                paragraph_row["comments"].append(cid)
                            comments_by_id[cid].setdefault("anchors", []).append({
                                "paragraph_id": paragraph_id,
                                "kind": marker.tag.rsplit("}", 1)[-1],
                            })
                source["paragraphs"].append(paragraph_row)
                evidence.append({"id": _evidence_id(source_id, "docx-paragraph", index),
                                 "source_id": source_id, "kind": "docx_paragraph", "page_index": None,
                                 "paragraph_id": paragraph_id, "text": text_value, "runs": runs,
                                 "comments": paragraph_row["comments"],
                                 "note": "Complete paragraph text including tracked revision runs."})
            for table_index, table in enumerate(document.findall(".//w:body//w:tbl", _NS)):
                table_rows: list[list[str]] = []
                for row in table.findall("./w:tr", _NS):
                    cells: list[str] = []
                    for cell in row.findall("./w:tc", _NS):
                        cells.append(_xml_text(cell))
                    table_rows.append(cells)
                table_row = {"id": f"{source_id}-table-{table_index:05d}", "index": table_index,
                             "rows": table_rows}
                source["tables"].append(table_row)
                evidence.append({"id": _evidence_id(source_id, "docx-table", table_index),
                                 "source_id": source_id, "kind": "docx_table", "page_index": None,
                                 "table": table_row,
                                 "note": "Complete table evidence, including every cell and table-cell paragraph."})
            for index, comment in enumerate(source["comments"]):
                evidence.append({"id": _evidence_id(source_id, "docx-comment", index), "source_id": source_id,
                                 "kind": "docx_comment", "page_index": None, "comment": comment,
                                 "note": "Complete DOCX comment content; paragraph anchors are listed where present."})
    except Exception as exc:
        source["errors"].append(_error(source_id, "docx_read", f"Could not read DOCX package: {exc}"))
    return source, evidence


def _image_source(path: Path, source_id: str, work_dir: Path, digest: str) -> tuple[dict, list[dict]]:
    copied = _copy_artifact(path, work_dir, source_id)
    source: dict[str, Any] = {"id": source_id, "kind": "image", "name": path.name,
                             "path": str(path.resolve()), "sha256": digest, "images": [{"path": copied}],
                             "errors": []}
    evidence = [{"id": _evidence_id(source_id, "image", 0), "source_id": source_id,
                 "kind": "image", "page_index": 0, "image": copied,
                 "note": "Complete image evidence for visual inspection."}]
    return source, evidence


def _other_source(path: Path, source_id: str, work_dir: Path, digest: str) -> tuple[dict, list[dict]]:
    source: dict[str, Any] = {"id": source_id, "kind": "file", "name": path.name,
                             "path": str(path.resolve()), "sha256": digest, "errors": []}
    copied = _copy_artifact(path, work_dir, source_id)
    source["artifact_path"] = copied
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    except OSError as exc:
        source["errors"].append(_error(source_id, "file_read", str(exc)))
        text = None
    if text is not None:
        source["text"] = text
        content = text
    else:
        source["content_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
        content = ""
    evidence = [{"id": _evidence_id(source_id, "file", 0), "source_id": source_id,
                 "kind": "file", "page_index": None, "text": content, "artifact_path": copied,
                 "note": "Complete source content; binary files are retained at artifact_path."}]
    return source, evidence


def build_packet(attachments: list[Path], text: str, work_dir: Path) -> dict:
    """Return a deterministic, complete evidence packet for interior review."""
    if not isinstance(attachments, list):
        raise IntakeError("attachments must be a list of paths")
    if not isinstance(text, str):
        raise IntakeError("text must be a string")
    root = Path(work_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    sources: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for raw_path in attachments:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raw_id = "source-" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
            row = {"id": raw_id, "kind": "missing", "name": path.name, "path": str(path),
                   "sha256": None, "errors": [_error(raw_id, "missing", f"Attachment does not exist: {path}")]}
            sources.append(row)
            errors.extend(row["errors"])
            evidence.append({"id": _evidence_id(raw_id, "missing", 0), "source_id": raw_id,
                             "kind": "missing", "page_index": None, "text": "",
                             "note": "Missing attachment was retained as explicit evidence."})
            continue
        digest = _sha256_file(path)
        occurrence = seen.get(digest, 0)
        seen[digest] = occurrence + 1
        source_id = f"source-{digest[:16]}" + (f"-{occurrence}" if occurrence else "")
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            source, rows = _pdf_source(path, source_id, root, digest)
        elif suffix == ".docx":
            source, rows = _docx_source(path, source_id, root, digest)
        elif suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp"}:
            source, rows = _image_source(path, source_id, root, digest)
        else:
            source, rows = _other_source(path, source_id, root, digest)
        sources.append(source)
        evidence.extend(rows)
        errors.extend(source.get("errors", []))
    normalized_text = html.unescape(text)
    text_id = "source-text-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    text_evidence = {"id": _evidence_id(text_id, "text", 0), "source_id": text_id,
                     "kind": "text", "page_index": None, "text": normalized_text,
                     "raw_text": text,
                     "note": "Complete caller-supplied text evidence."}
    text_source = {"id": text_id, "kind": "text", "name": "supplied text", "path": None,
                   "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                   "text": normalized_text, "raw_text": text,
                   "errors": []}
    sources.append(text_source)
    evidence.append(text_evidence)
    packet: dict[str, Any] = {
        "schema_version": 1,
        "text": normalized_text,
        "text_raw": text,
        "sources": sources,
        "attachments": sources[:-1],
        "text_source_id": text_id,
        "evidence": evidence,
        "errors": errors,
        "coverage": {"source_count": len(sources), "evidence_count": len(evidence),
                      "error_count": len(errors), "complete": not errors,
                      "all_sources_preserved": True,
                      "visual_complete": not any(row.get("kind") == "render" for row in errors)},
        "work_dir": str(root),
    }
    packet["source_ids"] = [source["id"] for source in sources]
    packet["evidence_ids"] = [row["id"] for row in evidence]
    required_evidence: list[str] = []
    for row in evidence:
        kind = row.get("kind")
        if kind == "pdf_annotation":
            annotation = row.get("annotation")
            if isinstance(annotation, dict) and any(value not in (None, "", [], {})
                                                   for key, value in annotation.items() if key != "ordinal"):
                required_evidence.append(row["id"])
        elif kind == "docx_paragraph":
            if str(row.get("text", "")).strip() or row.get("comments") or any(
                    run.get("revision") for run in row.get("runs", []) if isinstance(run, dict)):
                required_evidence.append(row["id"])
        elif kind == "docx_comment":
            comment = row.get("comment", {})
            if isinstance(comment, dict) and str(comment.get("text", "")).strip():
                required_evidence.append(row["id"])
        elif kind in {"text", "file"}:
            if str(row.get("text", "")).strip():
                required_evidence.append(row["id"])
        elif kind == "image":
            required_evidence.append(row["id"])
    packet["required_evidence_ids"] = required_evidence
    evidence_by_source: dict[str, list[str]] = {}
    for row in evidence:
        evidence_by_source.setdefault(row["source_id"], []).append(row["id"])
    for source in sources:
        source["evidence_ids"] = evidence_by_source.get(source["id"], [])
        source.setdefault("notes", "All extracted content and visual artifacts are retained in evidence rows.")
        source["required_evidence_ids"] = [evidence_id for evidence_id in required_evidence
                                            if evidence_id in source["evidence_ids"]]
    return packet
