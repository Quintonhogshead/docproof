"""Native InDesign access for DocProof interior corrections.

The server cannot parse an ``.indd`` file.  This module therefore keeps the
boundary deliberately small: Python writes an ASCII-only ExtendScript file,
InDesign opens and audits a document, and Python reads the JSON/artifacts that
the script leaves in ``work_dir``.  The script never relies on
``app.activeDocument``; every operation is performed through the document it
opened itself.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


BUNDLE_ID = "com.adobe.InDesign"
TIMEOUT_SECONDS = 600
_LOCK_PATH = Path(tempfile.gettempdir()) / "docproof-indesign.lock"


class NativeError(RuntimeError):
    """A native InDesign operation could not be completed safely."""


def _as_path(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


@contextlib.contextmanager
def _indesign_lock() -> Iterator[None]:
    """Serialize all Apple Events sent to InDesign across processes."""
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK_PATH.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _normalise_edits(edits: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Make edit payloads JSON-safe and use InDesign's paragraph separator."""
    out: list[dict[str, Any]] = []
    for index, incoming in enumerate(edits):
        edit = dict(incoming)
        if not edit.get("id"):
            edit["id"] = f"native-{index + 1}"
        if "story_id" not in edit or "find" not in edit:
            raise NativeError(f"edit {edit['id']!r} needs story_id and find")
        if "replacement" not in edit:
            edit["replacement"] = ""
        replacement = str(edit["replacement"])
        edit["replacement"] = replacement.replace("\r\n", "\r").replace("\n", "\r")
        if "expected_count" not in edit:
            edit["expected_count"] = 1
        try:
            expected = int(edit["expected_count"])
        except (TypeError, ValueError) as exc:
            raise NativeError(f"edit {edit['id']!r} has a bad expected_count") from exc
        if expected < 0:
            raise NativeError(f"edit {edit['id']!r} has a negative expected_count")
        edit["expected_count"] = expected
        if "occurrence" in edit and edit["occurrence"] is not None:
            try:
                edit["occurrence"] = int(edit["occurrence"])
            except (TypeError, ValueError) as exc:
                raise NativeError(f"edit {edit['id']!r} has a bad occurrence") from exc
        ranges = edit.get("style_ranges")
        if ranges is not None:
            if not isinstance(ranges, Sequence) or isinstance(ranges, (str, bytes)):
                raise NativeError(f"edit {edit['id']!r} has bad style_ranges")
            clean_ranges: list[dict[str, Any]] = []
            replacement_len = len(edit["replacement"])
            for span in ranges:
                if not isinstance(span, Mapping):
                    raise NativeError(f"edit {edit['id']!r} has a bad style range")
                try:
                    start, end = int(span["start"]), int(span["end"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise NativeError(f"edit {edit['id']!r} has a bad style range") from exc
                if start < 0 or end < start or end > replacement_len:
                    raise NativeError(f"edit {edit['id']!r} style range is outside replacement")
                if "font_style" not in span:
                    raise NativeError(f"edit {edit['id']!r} style range needs font_style")
                # ExtendScript/InDesign addresses characters in UTF-16 code
                # units.  The public Python contract uses Unicode code-point
                # offsets, so convert only at the native boundary.
                clean_ranges.append({"start": _python_to_utf16(edit["replacement"], start),
                                     "end": _python_to_utf16(edit["replacement"], end),
                                     "font_style": str(span["font_style"])})
            edit["style_ranges"] = clean_ranges
        if str(edit.get("kind", "replace")) == "style" and not edit.get("font_style"):
            raise NativeError(f"edit {edit['id']!r} style kind needs font_style")
        out.append(edit)
    return out


def _python_to_utf16(value: str, offset: int) -> int:
    """Convert a Python code-point offset to an ExtendScript UTF-16 offset."""
    return len(value[:offset].encode("utf-16-le")) // 2


def _utf16_to_python(value: str, offset: int) -> int:
    """Convert an ExtendScript UTF-16 offset to a Python code-point offset."""
    if offset <= 0:
        return 0
    units = 0
    for index, char in enumerate(value):
        units += 2 if ord(char) > 0xFFFF else 1
        if units >= offset:
            return index + (1 if units == offset else 0)
    return len(value)


def _utf16_boundaries(value: str) -> dict[int, int]:
    """Build one native-offset lookup per story instead of rescanning it."""
    boundaries = {0: 0}
    units = 0
    for index, char in enumerate(value):
        units += 2 if ord(char) > 0xFFFF else 1
        boundaries[units] = index + 1
    return boundaries


def _artifact_paths(work_dir: Path, action: str) -> dict[str, Path]:
    if action == "inspect":
        return {
            "snapshot": work_dir / "native-baseline.json",
            "pdf": work_dir / "baseline.pdf",
            "idml": work_dir / "baseline.idml",
        }
    return {
        "snapshot": work_dir / "native-final.json",
        "pdf": work_dir / "final.pdf",
        "idml": work_dir / "final.idml",
    }


def _clear_artifacts(paths: Mapping[str, Path]) -> None:
    for path in paths.values():
        path.unlink(missing_ok=True)


def _read_report(snapshot: Path, stdout: str) -> dict[str, Any]:
    if snapshot.is_file():
        try:
            value = json.loads(snapshot.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise NativeError(f"InDesign wrote an invalid audit: {snapshot}") from exc
    else:
        # Useful for a test runner and for older InDesign builds that return
        # the JSON value through Apple Events rather than writing the file.
        candidate = stdout.strip()
        if candidate.startswith("OK:"):
            candidate = candidate[3:].strip()
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError) as exc:
            raise NativeError("InDesign did not write a native audit") from exc
    if not isinstance(value, dict):
        raise NativeError("InDesign native audit was not an object")
    return value


def _decorate_report(report: dict[str, Any], paths: Mapping[str, Path],
                     document: Path, action: str) -> dict[str, Any]:
    report = dict(report)
    report.setdefault("stories", [])
    report.setdefault("fonts", [])
    report.setdefault("links", [])
    report.setdefault("overset", [])
    report.setdefault("page_count", report.get("pages", 0))
    report.setdefault("pages", report["page_count"])
    report.setdefault("style_inventory_errors", [])
    report.setdefault("style_inventory_complete", not report["style_inventory_errors"])
    # InDesign reports UTF-16 character offsets.  Normalize the audit to the
    # Python-facing Unicode code-point contract before returning it.
    for story in report["stories"]:
        story = story if isinstance(story, dict) else None
        if not story:
            continue
        story_text = str(story.get("text", ""))
        boundaries = _utf16_boundaries(story_text)
        for span in story.get("style_ranges", []) or []:
            if isinstance(span, dict):
                try:
                    start, end = int(span["start"]), int(span["end"])
                    span["start"] = (boundaries[start] if start in boundaries
                                      else _utf16_to_python(story_text, start))
                    span["end"] = (boundaries[end] if end in boundaries
                                    else _utf16_to_python(story_text, end))
                except (KeyError, TypeError, ValueError):
                    pass
    missing_fonts = list(report.get("missing_fonts") or [])
    missing_links = list(report.get("missing_links") or [])
    if not missing_fonts:
        missing_fonts = [f for f in report["fonts"]
                         if "missing" in str(f.get("status", "")).lower()
                         or "notinstalled" in str(f.get("status", "")).lower()
                         or "error" in str(f.get("status", "")).lower()]
    if not missing_links:
        missing_links = [l for l in report["links"]
                         if "missing" in str(l.get("status", "")).lower()
                         or "inaccessible" in str(l.get("status", "")).lower()
                         or "error" in str(l.get("status", "")).lower()]
    report["missing_fonts"] = missing_fonts
    report["missing_links"] = missing_links
    report["unresolved_assets"] = bool(missing_fonts or missing_links)
    report["ok"] = not report["unresolved_assets"] and bool(report["style_inventory_complete"])
    report["document"] = str(document)
    report["snapshot_path"] = str(paths["snapshot"])
    report["pdf_path"] = str(paths["pdf"])
    report["idml_path"] = str(paths["idml"])
    report["artifact_paths"] = {
        "snapshot": str(paths["snapshot"]),
        "pdf": str(paths["pdf"]),
        "idml": str(paths["idml"]),
    }
    # Stable aliases used by callers that distinguish baseline and final work.
    if action == "inspect":
        report["baseline_json"] = str(paths["snapshot"])
        report["baseline_pdf"] = str(paths["pdf"])
        report["baseline_idml"] = str(paths["idml"])
    else:
        report["final_json"] = str(paths["snapshot"])
        report["output_pdf"] = str(paths["pdf"])
        report["output_idml"] = str(paths["idml"])
    return report


def build_jsx(action: str, source: str | Path, work_dir: str | Path,
              output: str | Path | None = None,
              edits: Sequence[Mapping[str, Any]] = ()) -> str:
    """Build the ExtendScript sent to InDesign.

    Every interpolated value is JSON-encoded with non-ASCII characters escaped,
    so paths and edit text cannot break the AppleScript or JSX string literal.
    """
    if action not in {"inspect", "apply", "verify"}:
        raise ValueError(f"unknown native action: {action}")
    source_path = _as_path(source)
    work_path = _as_path(work_dir)
    output_path = _as_path(output) if output is not None else source_path
    payload = _normalise_edits(edits) if action == "apply" else []
    paths = _artifact_paths(work_path, action)

    replacements = {
        "__ACTION__": json.dumps(action, ensure_ascii=True),
        "__SOURCE__": json.dumps(str(source_path), ensure_ascii=True),
        "__OUTPUT__": json.dumps(str(output_path), ensure_ascii=True),
        "__SNAPSHOT__": json.dumps(str(paths["snapshot"]), ensure_ascii=True),
        "__PDF__": json.dumps(str(paths["pdf"]), ensure_ascii=True),
        "__IDML__": json.dumps(str(paths["idml"]), ensure_ascii=True),
        "__EDITS__": json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
    }
    script = _JSX
    for token, value in replacements.items():
        script = script.replace(token, value)
    if not script.isascii():
        raise NativeError("native ExtendScript was not ASCII-safe")
    return script


class InDesignWorker:
    """Run read-only audits and anchored edits through native InDesign."""

    def __init__(self, *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 timeout: int = TIMEOUT_SECONDS,
                 bundle_id: str = BUNDLE_ID) -> None:
        self.runner = runner
        self.timeout = timeout
        self.bundle_id = bundle_id

    def inspect(self, source: Path, work_dir: Path) -> dict[str, Any]:
        source, work_dir = _as_path(source), _as_path(work_dir)
        if not source.is_file():
            raise NativeError(f"InDesign source does not exist: {source}")
        work_dir.mkdir(parents=True, exist_ok=True)
        paths = _artifact_paths(work_dir, "inspect")
        return self._execute("inspect", source, source, work_dir, paths, ())

    def apply(self, source: Path, output: Path, edits: list[dict],
              work_dir: Path) -> dict[str, Any]:
        source, output, work_dir = _as_path(source), _as_path(output), _as_path(work_dir)
        if not source.is_file():
            raise NativeError(f"InDesign source does not exist: {source}")
        if source == output:
            raise NativeError("InDesign output must be a different path from source")
        if output.exists():
            raise NativeError(f"InDesign output already exists: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        paths = _artifact_paths(work_dir, "apply")
        return self._execute("apply", source, output, work_dir, paths, edits)

    def verify(self, document: Path, work_dir: Path) -> dict[str, Any]:
        document, work_dir = _as_path(document), _as_path(work_dir)
        if not document.is_file():
            raise NativeError(f"InDesign document does not exist: {document}")
        work_dir.mkdir(parents=True, exist_ok=True)
        paths = _artifact_paths(work_dir, "verify")
        return self._execute("verify", document, document, work_dir, paths, ())

    def _execute(self, action: str, source: Path, output: Path, work_dir: Path,
                 paths: Mapping[str, Path], edits: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        script: Path | None = None
        with _indesign_lock():
            _clear_artifacts(paths)
            try:
                text = build_jsx(action, source, work_dir, output, edits)
                with tempfile.NamedTemporaryFile("w", suffix=".jsx", dir=work_dir,
                                                 encoding="ascii", delete=False) as handle:
                    handle.write(text)
                    script = Path(handle.name)
                tell = (f'tell application id {json.dumps(self.bundle_id)} to do script '
                        f'(POSIX file {json.dumps(str(script), ensure_ascii=True)}) '
                        "language javascript")
                try:
                    done = self.runner(["osascript", "-e", tell], capture_output=True,
                                       text=True, timeout=self.timeout)
                except subprocess.TimeoutExpired as exc:
                    raise NativeError(
                        f"InDesign took longer than {self.timeout // 60} minutes") from exc
                except OSError as exc:
                    raise NativeError(f"DocProof could not start InDesign: {exc}") from exc
            finally:
                if script is not None:
                    script.unlink(missing_ok=True)
        stdout = str(getattr(done, "stdout", "") or "")
        stderr = str(getattr(done, "stderr", "") or "")
        if getattr(done, "returncode", 1) != 0 or stdout.strip().startswith("ERR:"):
            message = stdout.strip()[4:].strip() if stdout.strip().startswith("ERR:") else stderr.strip()
            if "-1743" in message or "not authorized" in message.lower():
                message = "macOS has not authorized DocProof to control InDesign"
            raise NativeError(message or "InDesign gave no reply")
        report = _read_report(paths["snapshot"], stdout)
        report = _decorate_report(report, paths, output, action)
        if action == "apply" and not output.is_file():
            raise NativeError("InDesign did not save the requested output document")
        for kind in ("pdf", "idml"):
            if not paths[kind].is_file():
                raise NativeError(f"InDesign did not export {paths[kind].name}")
        missing = report["missing_fonts"] or report["missing_links"]
        if missing:
            # Keep the audit available to callers while making the unresolved
            # condition impossible to miss in logs and exception based callers.
            report["warning"] = "unresolved missing fonts or links"
        return report


_JSX = r'''// DocProof native interior worker.  This file is deliberately ASCII-only.
var ACTION = __ACTION__;
var SOURCE = __SOURCE__;
var OUTPUT = __OUTPUT__;
var SNAPSHOT = __SNAPSHOT__;
var PDF_OUT = __PDF__;
var IDML_OUT = __IDML__;
var EDITS = __EDITS__;

function jsonString(value) {
    if (value === null || value === undefined) return "null";
    var s = String(value), out = '"';
    for (var i = 0; i < s.length; i++) {
        var c = s.charCodeAt(i);
        if (c === 8) out += "\\b";
        else if (c === 9) out += "\\t";
        else if (c === 10) out += "\\n";
        else if (c === 12) out += "\\f";
        else if (c === 13) out += "\\r";
        else if (c === 34) out += '\\"';
        else if (c === 92) out += "\\\\";
        else if (c < 32 || c > 126) {
            var h = c.toString(16);
            while (h.length < 4) h = "0" + h;
            out += "\\u" + h;
        } else out += String.fromCharCode(c);
    }
    return out + '"';
}
function jsonValue(v) {
    if (v === null || v === undefined) return "null";
    if (typeof v === "string") return jsonString(v);
    if (typeof v === "number") return isFinite(v) ? String(v) : "null";
    if (typeof v === "boolean") return v ? "true" : "false";
    var a = [], k;
    if (v instanceof Array) {
        for (k = 0; k < v.length; k++) a.push(jsonValue(v[k]));
        return "[" + a.join(",") + "]";
    }
    for (k in v) if (v.hasOwnProperty(k)) a.push(jsonString(k) + ":" + jsonValue(v[k]));
    return "{" + a.join(",") + "}";
}
function writeJson(path, value) {
    var f = new File(path);
    f.encoding = "UTF-8";
    if (!f.open("w")) throw new Error("could not write native audit");
    f.write(jsonValue(value));
    f.close();
}
function text(v) { try { return String(v); } catch (e) { return ""; } }
function pageNumber(page) {
    try { return Number(page.documentOffset) + 1; } catch (e) { return null; }
}
function openPath(doc) {
    try { return String(doc.fullName.fsName); } catch (e) { return ""; }
}
function samePath(a, b) {
    return String(a).toLowerCase() === String(b).toLowerCase();
}
function isOpen(path) {
    var wanted = String(path);
    for (var i = 0; i < app.documents.length; i++) {
        if (samePath(openPath(app.documents[i]), wanted)) return true;
    }
    return false;
}
function uniquePush(values, value) {
    if (value === null || value === undefined) return;
    for (var i = 0; i < values.length; i++) if (values[i] === value) return;
    values.push(value);
}
function fontStyleOf(item, issues) {
    try { return String(item.fontStyle); }
    catch (e) {
        if (issues) issues.push("font_style: " + text(e));
        return "";
    }
}
function appliedFontOf(item, issues) {
    var got = safeRead(item, "appliedFont");
    if (!got.ok) {
        if (issues) issues.push("applied_font: " + got.error);
        return "";
    }
    try { return got.value && got.value.name ? String(got.value.name) : String(got.value); }
    catch (e) { if (issues) issues.push("applied_font: " + text(e)); return ""; }
}
function scalarProperty(item, property, issues) {
    var got = safeRead(item, property);
    if (!got.ok) {
        if (issues) issues.push(property + ": " + got.error);
        return null;
    }
    if (got.value === null || got.value === undefined) return null;
    try { return typeof got.value === "number" || typeof got.value === "boolean" ? got.value : String(got.value); }
    catch (e) { if (issues) issues.push(property + ": " + text(e)); return null; }
}
function paragraphStyleOf(item, issues) {
    var got = safeRead(item, "appliedParagraphStyle");
    if (!got.ok) {
        if (issues) issues.push("paragraph_style: " + got.error);
        return "";
    }
    try { return got.value && got.value.name ? String(got.value.name) : String(got.value); }
    catch (e) { if (issues) issues.push("paragraph_style: " + text(e)); return ""; }
}
function styleRanges(story, issues) {
    var ranges = [], rs;
    try { rs = story.textStyleRanges; }
    catch (e) { issues.push("textStyleRanges: " + text(e)); return ranges; }
    for (var i = 0; i < rs.length; i++) {
        try {
            var start = Number(rs[i].index), length = Number(rs[i].characters.length);
            if (isFinite(start) && isFinite(length) && length > 0)
                ranges.push({start: start, end: start + length,
                             font_style: fontStyleOf(rs[i], issues),
                             applied_font: appliedFontOf(rs[i], issues),
                             point_size: scalarProperty(rs[i], "pointSize", issues),
                             tracking: scalarProperty(rs[i], "tracking", issues)});
            else issues.push("invalid style range " + i);
        } catch (e2) { issues.push("style range " + i + ": " + text(e2)); }
    }
    return ranges;
}
function paragraphInventory(story, issues) {
    var rows = [], paragraphs;
    try { paragraphs = story.paragraphs; }
    catch (e) { issues.push("paragraphs: " + text(e)); return rows; }
    for (var i = 0; i < paragraphs.length; i++) {
        var p = paragraphs[i], start, length;
        try { start = Number(p.index); length = Number(p.characters.length); }
        catch (e2) { issues.push("paragraph " + i + ": " + text(e2)); continue; }
        rows.push({start: start, end: start + length,
                   paragraph_style: paragraphStyleOf(p, issues)});
    }
    return rows;
}
function attachParagraphStyles(ranges, paragraphs) {
    for (var i = 0; i < ranges.length; i++) {
        var style = "";
        for (var j = 0; j < paragraphs.length; j++) {
            if (ranges[i].start >= paragraphs[j].start && ranges[i].start < paragraphs[j].end) {
                style = paragraphs[j].paragraph_style; break;
            }
        }
        ranges[i].paragraph_style = style;
    }
    return ranges;
}
function storyPages(story) {
    var pages = [], frames;
    try { frames = story.textContainers; } catch (e) { return pages; }
    for (var i = 0; i < frames.length; i++) {
        try { uniquePush(pages, pageNumber(frames[i].parentPage)); } catch (e2) {}
    }
    return pages;
}
function pageTexts(doc) {
    var rows = [];
    for (var i = 0; i < doc.pages.length; i++) {
        var chunks = [], frames = doc.pages[i].textFrames;
        for (var j = 0; j < frames.length; j++) {
            try { chunks.push(text(frames[j].contents)); } catch (e) {}
        }
        rows.push({page: i + 1, text: chunks.join("\r")});
    }
    return rows;
}
function missingFont(status) {
    var s = String(status).toLowerCase();
    return s.indexOf("missing") >= 0 || s.indexOf("notinstalled") >= 0 || s.indexOf("error") >= 0;
}
function missingLink(status) {
    var s = String(status).toLowerCase();
    return s.indexOf("missing") >= 0 || s.indexOf("inaccessible") >= 0 || s.indexOf("error") >= 0;
}
function audit(doc) {
    var out = {name: text(doc.name), page_count: doc.pages.length,
               pages: doc.pages.length, fonts: [], links: [], stories: [],
               overset: [], page_texts: pageTexts(doc), missing_fonts: [], missing_links: [],
               style_inventory_errors: [], style_inventory_complete: true};
    // Use the document's collection.  app.fonts includes unrelated fonts from
    // other documents and cannot tell a document audit what is unresolved.
    for (var fi = 0; fi < doc.fonts.length; fi++) {
        var font = doc.fonts[fi], fs = text(font.status);
        var fr = {name: text(font.name), status: fs};
        out.fonts.push(fr);
        if (missingFont(fs)) out.missing_fonts.push(fr);
    }
    for (var li = 0; li < doc.links.length; li++) {
        var link = doc.links[li], ls = text(link.status), path = "";
        try { path = text(link.filePath); } catch (e3) {}
        var lr = {name: text(link.name), status: ls, path: path};
        out.links.push(lr);
        if (missingLink(ls)) out.missing_links.push(lr);
    }
    for (var si = 0; si < doc.stories.length; si++) {
        var story = doc.stories[si], over = false;
        try { over = Boolean(story.overflows); } catch (e4) {}
        var paragraphs = paragraphInventory(story, out.style_inventory_errors);
        var sr = {id: text(story.id), text: text(story.contents),
                  style_ranges: attachParagraphStyles(styleRanges(story, out.style_inventory_errors), paragraphs),
                  paragraphs: paragraphs, pages: storyPages(story)};
        out.stories.push(sr);
        if (over) out.overset.push(sr.id);
    }
    out.style_inventory_complete = out.style_inventory_errors.length === 0;
    return out;
}
function allMatches(haystack, needle) {
    var found = [], at = 0;
    if (needle === "") return found;
    while ((at = haystack.indexOf(needle, at)) >= 0) {
        found.push(at); at += needle.length;
    }
    return found;
}
function storyById(doc, wanted) {
    for (var i = 0; i < doc.stories.length; i++)
        if (text(doc.stories[i].id) === text(wanted)) return doc.stories[i];
    return null;
}
function checkStyleRanges(edit) {
    var spans = edit.style_ranges || [], length = text(edit.replacement).length;
    for (var i = 0; i < spans.length; i++) {
        if (Number(spans[i].start) < 0 || Number(spans[i].end) < Number(spans[i].start) ||
            Number(spans[i].end) > length) throw new Error("style range outside replacement");
    }
}
function preflight(doc) {
    var plan = [], occupied = {};
    for (var ei = 0; ei < EDITS.length; ei++) {
        var edit = EDITS[ei], story = storyById(doc, edit.story_id);
        if (!story) throw new Error("story not found: " + edit.story_id);
        var find = text(edit.find), contents = text(story.contents), matches = allMatches(contents, find);
        var expected = Number(edit.expected_count);
        if (matches.length !== expected)
            throw new Error("edit " + edit.id + " expected " + expected + " matches, found " + matches.length);
        var occurrence = edit.occurrence;
        if (occurrence !== undefined && occurrence !== null) {
            occurrence = Number(occurrence);
            if (occurrence < 1 || occurrence > matches.length)
                throw new Error("edit " + edit.id + " occurrence is outside matches");
        }
        checkStyleRanges(edit);
        var selected = occurrence === undefined || occurrence === null ? matches :
            [matches[occurrence - 1]];
        var sid = text(edit.story_id);
        if (!occupied[sid]) occupied[sid] = [];
        for (var mi = 0; mi < selected.length; mi++) {
            var start = selected[mi], end = start + find.length;
            for (var oi = 0; oi < occupied[sid].length; oi++) {
                var old = occupied[sid][oi];
                if (start < old.end && old.start < end || start === end && start === old.start)
                    throw new Error("edits overlap in story " + sid);
            }
            occupied[sid].push({start: start, end: end});
            plan.push({edit: edit, story: story, start: start, end: end});
        }
    }
    // Applying right-to-left keeps every preflight offset anchored to the
    // original story, even where edits change paragraph lengths.
    plan.sort(function(a, b) { return text(a.story.id) === text(b.story.id) ?
        b.start - a.start : text(a.story.id) < text(b.story.id) ? -1 : 1; });
    return plan;
}
function applyFontStyle(target, start, end, style) {
    if (!style || end <= start) return;
    try { target.characters.itemByRange(start, end - 1).fontStyle = style; }
    catch (e) { throw new Error("font style could not be applied: " + style); }
}
function safeRead(object, property) {
    try { return {ok: true, value: object[property]}; }
    catch (e) { return {ok: false, error: text(e)}; }
}
function optionalProperties(object, names) {
    var values = {};
    for (var i = 0; i < names.length; i++) {
        var got = safeRead(object, names[i]);
        // InDesign's DOM differs slightly between document versions.  A
        // property absent on this object cannot be preserved, but it must not
        // prevent the remaining supported formatting from being copied.
        if (got.ok) values[names[i]] = got.value;
    }
    return values;
}
function captureFormatting(story, start, end) {
    var result = {runs: [], paragraphs: []}, range;
    if (end <= start) return result;
    try { range = story.characters.itemByRange(start, end - 1); }
    catch (e) { throw new Error("could not inspect replacement formatting: " + e); }
    var styles;
    // Limit the inventory to the original replacement range.  Reading every
    // style range in a 400-page story for each anchored edit is prohibitively
    // expensive and is unnecessary for preserving the range being replaced.
    try { styles = range.textStyleRanges; }
    catch (e2) { throw new Error("could not inspect replacement style runs: " + e2); }
    var styleProps = ["appliedFont", "fontStyle", "pointSize", "tracking",
                      "capitalization", "position", "underline", "strikeThru",
                      "ligatures", "noBreak"];
    for (var i = 0; i < styles.length; i++) {
        var rs = styles[i], rsStart, rsLength;
        try { rsStart = Number(rs.index); rsLength = Number(rs.characters.length); }
        catch (styleError) { throw new Error("could not inspect style run " + i + ": " + styleError); }
        var rsEnd = rsStart + rsLength, overlapStart = Math.max(start, rsStart), overlapEnd = Math.min(end, rsEnd);
        if (overlapEnd <= overlapStart) continue;
        var run = {start: overlapStart - start, end: overlapEnd - start,
                   text: text(rs.contents).substr(overlapStart - rsStart, overlapEnd - overlapStart),
                   props: {}};
        run.props = optionalProperties(rs, styleProps);
        result.runs.push(run);
    }
    var paragraphs;
    try { paragraphs = range.paragraphs; }
    catch (paragraphError) { throw new Error("could not inspect paragraph formatting: " + paragraphError); }
    var paragraphProps = ["appliedParagraphStyle", "justification", "leading",
                          "spaceBefore", "spaceAfter", "firstLineIndent", "leftIndent",
                          "rightIndent", "keepWithNext", "keepWithPrevious",
                          "keepAllLinesTogether", "keepFirstLines", "keepLastLines",
                          "startParagraph", "hyphenation"];
    for (var pi = 0; pi < paragraphs.length; pi++) {
        result.paragraphs.push({props: optionalProperties(paragraphs[pi], paragraphProps)});
    }
    return result;
}
function closestOccurrence(haystack, needle, wanted) {
    if (!needle) return -1;
    var found = -1, at = 0, distance = 2147483647;
    while ((at = haystack.indexOf(needle, at)) >= 0) {
        var d = Math.abs(at - wanted);
        if (d < distance) { found = at; distance = d; }
        at += needle.length;
    }
    return found;
}
function applyCapturedFormatting(story, start, replacement, formatting) {
    var i, run, mapped, mappedEnd, object, prop;
    for (i = 0; i < formatting.runs.length; i++) {
        run = formatting.runs[i];
        mapped = closestOccurrence(replacement, run.text, run.start);
        if (mapped < 0) mapped = Math.min(run.start, replacement.length);
        mappedEnd = Math.min(replacement.length, mapped + Math.max(1, run.end - run.start));
        if (mappedEnd <= mapped) continue;
        object = story.characters.itemByRange(start + mapped, start + mappedEnd - 1);
        for (prop in run.props) if (run.props.hasOwnProperty(prop)) {
            try { object[prop] = run.props[prop]; } catch (formatError) {}
        }
    }
    if (replacement.length <= 0 || formatting.paragraphs.length <= 0) return;
    var newRange = story.characters.itemByRange(start, start + replacement.length - 1), newParagraphs;
    try { newParagraphs = newRange.paragraphs; }
    catch (newParagraphError) { throw new Error("could not restore paragraph formatting: " + newParagraphError); }
    for (i = 0; i < newParagraphs.length; i++) {
        var saved = formatting.paragraphs[Math.min(i, formatting.paragraphs.length - 1)];
        for (prop in saved.props) if (saved.props.hasOwnProperty(prop)) {
            try { newParagraphs[i][prop] = saved.props[prop]; } catch (paragraphFormatError) {}
        }
    }
}
function applyOne(item) {
    var edit = item.edit, story = item.story, start = item.start, end = item.end;
    var kind = text(edit.kind || "replace"), find = text(edit.find), replacement = text(edit.replacement);
    if (kind === "insert") {
        if (text(edit.position || "after").toLowerCase() === "before") replacement = replacement + find;
        else replacement = find + replacement;
    } else if (kind !== "replace" && kind !== "style") {
        throw new Error("unknown edit kind: " + kind);
    }
    var target = story.characters.itemByRange(start, end - 1);
    if (kind === "style") {
        applyFontStyle(story, start, end, text(edit.font_style));
        return;
    }
    // Setting contents preserves the existing character formatting in
    // InDesign.  Explicit style ranges below are applied after the write and
    // therefore cover the replacement, including roman articles and italic
    // boat names.
    var formatting = captureFormatting(story, start, end);
    target.contents = replacement;
    applyCapturedFormatting(story, start, replacement, formatting);
    var styleBase = start;
    if (kind === "insert" && text(edit.position || "after").toLowerCase() !== "before")
        styleBase = start + find.length;
    var explicitLength = kind === "insert" ? replacement.length - find.length : replacement.length;
    if (edit.font_style) applyFontStyle(story, styleBase, styleBase + explicitLength,
                                        text(edit.font_style));
    var spans = edit.style_ranges || [];
    for (var i = 0; i < spans.length; i++)
        applyFontStyle(story, styleBase + Number(spans[i].start), styleBase + Number(spans[i].end),
                       text(spans[i].font_style));
}
function exportArtifacts(doc) {
    var pdf = new File(PDF_OUT), idml = new File(IDML_OUT);
    try { if (pdf.exists) pdf.remove(); } catch (e) {}
    try { if (idml.exists) idml.remove(); } catch (e2) {}
    var prefs = app.pdfExportPreferences, hadRange = false, oldRange = null;
    try {
        try { oldRange = prefs.pageRange; hadRange = true; } catch (e3) {}
        prefs.pageRange = PageRange.ALL_PAGES;
        var highQuality = null;
        try { highQuality = app.pdfExportPresets.itemByName("High Quality Print"); } catch (e4) {}
        try {
            if (highQuality !== null) doc.exportFile(ExportFormat.PDF_TYPE, pdf, false, highQuality);
            else doc.exportFile(ExportFormat.PDF_TYPE, pdf, false);
        } catch (e5) {
            // The preset is localized or absent; all-pages remains explicit.
            doc.exportFile(ExportFormat.PDF_TYPE, pdf, false);
        }
        doc.exportFile(ExportFormat.INDESIGN_MARKUP, idml, false);
    } finally {
        if (hadRange) try { prefs.pageRange = oldRange; } catch (restorePdfError) {}
    }
}
function fileIsFont(file) {
    var n = text(file.name).toLowerCase();
    return /\.(otf|ttf|ttc|dfont|pfb)$/.test(n);
}
function collectFiles(folder, predicate, out) {
    var entries;
    try { entries = folder.getFiles(); } catch (e) { return; }
    for (var i = 0; i < entries.length; i++) {
        if (entries[i] instanceof Folder) collectFiles(entries[i], predicate, out);
        else if (predicate(entries[i])) out.push(entries[i]);
    }
}
function collectNamed(folder, wanted, out) {
    collectFiles(folder, function(file) { return text(file.name) === text(wanted); }, out);
}
function copyDocumentFonts(sourceFolder, outputFolder) {
    var candidates = [], documentFonts = new Folder(sourceFolder.fsName + "/Document fonts");
    if (documentFonts.exists) collectFiles(documentFonts, fileIsFont, candidates);
    // A few older production folders kept the document fonts beside the INDD.
    var rootFonts = [];
    collectFiles(sourceFolder, function(file) {
        var parent = file.parent ? text(file.parent.name).toLowerCase() : "";
        return parent === text(sourceFolder.name).toLowerCase() && fileIsFont(file);
    }, rootFonts);
    for (var i = 0; i < rootFonts.length; i++) candidates.push(rootFonts[i]);
    if (candidates.length === 0) return;
    var destination = new Folder(outputFolder.fsName + "/Document fonts");
    if (!destination.exists && !destination.create()) throw new Error("could not create Document fonts folder");
    for (var j = 0; j < candidates.length; j++) {
        var target = new File(destination.fsName + "/" + candidates[j].name);
        if (!target.exists && !candidates[j].copy(target.fsName))
            throw new Error("could not copy document font " + candidates[j].name);
    }
}
function collectLinks(item, out, seen) {
    var key = "";
    try { key = text(item.id); } catch (e) { key = text(item.name) + ":" + text(item.filePath); }
    if (seen[key]) return;
    seen[key] = true;
    out.push(item);
    var children;
    try { children = item.childLinks; } catch (childError) { children = []; }
    for (var i = 0; i < children.length; i++) collectLinks(children[i], out, seen);
}
function documentLinks(doc) {
    var out = [], seen = {};
    for (var i = 0; i < doc.links.length; i++) collectLinks(doc.links[i], out, seen);
    return out;
}
function prepareLinkedAssets(doc, sourceFolder, outputFolder) {
    var destination = new Folder(outputFolder.fsName + "/Links");
    if (!destination.exists && !destination.create()) throw new Error("could not create Links folder");
    var links = documentLinks(doc), copied = {};
    for (var i = 0; i < links.length; i++) {
        var link = links[i], original = null, candidates = [];
        try { original = new File(link.filePath); } catch (e) {}
        if (original !== null && original.exists) candidates = [original];
        else collectNamed(sourceFolder, link.name, candidates);
        if (candidates.length !== 1) continue; // never guess through duplicates
        var sourceAsset = candidates[0], name = text(sourceAsset.name);
        if (copied[name] && copied[name] !== text(sourceAsset.fsName)) continue;
        var target = new File(destination.fsName + "/" + name);
        if (!target.exists && !sourceAsset.copy(target.fsName))
            throw new Error("could not copy linked asset " + name);
        try { link.relink(target); link.update(); copied[name] = text(sourceAsset.fsName); }
        catch (relinkError) { throw new Error("could not relink " + name + ": " + relinkError); }
    }
}
function main() {
    if (ACTION === "apply") {
        if (isOpen(SOURCE) || isOpen(OUTPUT)) throw new Error("source or output is already open");
        var src = new File(SOURCE), out = new File(OUTPUT);
        if (!src.exists) throw new Error("source does not exist");
        if (out.exists) throw new Error("output already exists; refusing to replace it");
        copyDocumentFonts(src.parent, out.parent);
        if (!src.copy(out.fsName)) throw new Error("could not copy source to output");
    } else {
        if (isOpen(SOURCE)) throw new Error("document is already open");
    }
    var opened = null, oldInteraction = null, oldRedraw = null;
    try {
        oldInteraction = app.scriptPreferences.userInteractionLevel;
        try { oldRedraw = app.scriptPreferences.enableRedraw; } catch (e3) {}
        app.scriptPreferences.userInteractionLevel = UserInteractionLevels.NEVER_INTERACT;
        try { app.scriptPreferences.enableRedraw = false; } catch (e4) {}
        opened = app.open(new File(ACTION === "apply" ? OUTPUT : SOURCE), false);
        if (ACTION === "apply") {
            prepareLinkedAssets(opened, new File(SOURCE).parent, new File(OUTPUT).parent);
            var plan = preflight(opened);
            for (var pi = 0; pi < plan.length; pi++) applyOne(plan[pi]);
        }
        try { opened.recompose(); } catch (recomposeError) { throw new Error("recompose failed: " + recomposeError); }
        if (ACTION === "apply") opened.save();
        try { opened.recompose(); } catch (recomposeAfterSaveError) { throw new Error("recompose failed: " + recomposeAfterSaveError); }
        var result = audit(opened);
        exportArtifacts(opened);
        writeJson(SNAPSHOT, result);
        return "OK";
    } finally {
        if (opened !== null) try { opened.close(SaveOptions.NO); } catch (closeError) {}
        if (oldInteraction !== null) try { app.scriptPreferences.userInteractionLevel = oldInteraction; } catch (restoreError) {}
        if (oldRedraw !== null) try { app.scriptPreferences.enableRedraw = oldRedraw; } catch (restoreRedrawError) {}
    }
}
var result;
try { result = main(); } catch (error) { result = "ERR:" + error.message; }
result;
'''


__all__ = ["BUNDLE_ID", "InDesignWorker", "NativeError", "build_jsx"]
