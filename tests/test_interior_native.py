"""The native adapter's contract, without requiring InDesign on CI."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from docproof.interior.native import InDesignWorker, NativeError, build_jsx


def _runner_for(work_dir: Path, *, output: Path | None = None,
                report: dict | None = None):
    report = report or {
        "page_count": 2,
        "stories": [{"id": "s1", "text": "A boat",
                      "style_ranges": [{"start": 0, "end": 1, "font_style": "Roman"}],
                      "pages": [1]}],
        "fonts": [{"name": "Minion Pro", "status": "FontStatus.INSTALLED"}],
        "links": [],
        "overset": [],
    }

    def run(command, **kwargs):
        script_path = Path(command[-1].split('POSIX file ')[1].split(') language')[0].strip()[1:-1])
        script = script_path.read_text(encoding="ascii")
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"copied-output")
        snapshot = work_dir / ("native-baseline.json" if '"inspect"' in script else "native-final.json")
        snapshot.write_text(json.dumps(report), encoding="utf-8")
        for name in ("baseline.pdf", "baseline.idml", "final.pdf", "final.idml"):
            (work_dir / name).write_bytes(b"artifact")
        return subprocess.CompletedProcess(command, 0, "OK", "")

    return run


def test_build_jsx_is_ascii_safe_and_targets_explicit_documents(tmp_path):
    jsx = build_jsx(
        "apply", tmp_path / "Bóôk.indd", tmp_path / "work",
        tmp_path / "out.indd",
        [{"id": "e1", "story_id": "s1", "find": "café", "replacement": "café\n",
          "expected_count": 1, "font_style": "Italic",
          "style_ranges": [{"start": 0, "end": 4, "font_style": "Italic"}]}],
    )
    assert jsx.isascii()
    assert "activeDocument" not in jsx
    assert "doc.fonts" in jsx
    assert "preflight(opened)" in jsx
    assert jsx.index("preflight(opened)") < jsx.index("applyOne(plan[pi])")
    assert "NEVER_INTERACT" in jsx
    assert "source or output is already open" in jsx
    assert "captureFormatting" in jsx
    assert "explicitLength" in jsx
    assert "High Quality Print" in jsx
    assert "PageRange.ALL_PAGES" in jsx
    assert "childLinks" in jsx
    assert "Document fonts" in jsx
    assert "output already exists; refusing to replace it" in jsx


def test_repeated_expected_count_applies_all_matches_and_occurrence_selects_one(tmp_path):
    jsx = build_jsx("apply", tmp_path / "source.indd", tmp_path / "work", tmp_path / "out.indd", [
        {"id": "all", "story_id": "s1", "find": "boat", "replacement": "ship",
         "expected_count": 2},
        {"id": "one", "story_id": "s2", "find": "a", "replacement": "an",
         "expected_count": 2, "occurrence": 2},
    ])
    assert "var selected = occurrence === undefined" in jsx
    assert "matches :" in jsx
    assert "matches[occurrence - 1]" in jsx


def test_inspect_writes_and_returns_baseline_audit(tmp_path):
    source = tmp_path / "source.indd"
    source.write_bytes(b"source")
    work = tmp_path / "work"
    result = InDesignWorker(runner=_runner_for(work)).inspect(source, work)
    assert result["page_count"] == 2
    assert result["stories"][0]["id"] == "s1"
    assert result["baseline_pdf"] == str(work / "baseline.pdf")
    assert result["baseline_idml"] == str(work / "baseline.idml")
    assert result["missing_fonts"] == []
    assert result["ok"] is True


def test_apply_uses_output_copy_and_reports_final_artifacts(tmp_path):
    source = tmp_path / "source.indd"
    output = tmp_path / "nested" / "final.indd"
    source.write_bytes(b"source")
    work = tmp_path / "work"
    runner = _runner_for(work, output=output)
    result = InDesignWorker(runner=runner).apply(
        source, output,
        [{"id": "e1", "story_id": "s1", "find": "boat", "replacement": "ship"}],
        work,
    )
    assert output.read_bytes() == b"copied-output"
    assert result["output_pdf"] == str(work / "final.pdf")
    assert result["output_idml"] == str(work / "final.idml")


def test_verify_reopens_saved_document_and_flags_unresolved_assets(tmp_path):
    document = tmp_path / "saved.indd"
    document.write_bytes(b"saved")
    work = tmp_path / "work"
    report = {"page_count": 1, "stories": [], "fonts": [
        {"name": "Missing Font", "status": "FontStatus.MISSING"}],
        "links": [{"name": "art.tif", "path": "/missing/art.tif", "status": "LINK_MISSING"}],
        "overset": []}
    result = InDesignWorker(runner=_runner_for(work, report=report)).verify(document, work)
    assert result["ok"] is False
    assert result["unresolved_assets"] is True
    assert len(result["missing_fonts"]) == 1
    assert len(result["missing_links"]) == 1


def test_audit_style_ranges_are_python_code_point_offsets(tmp_path):
    document = tmp_path / "saved.indd"
    document.write_bytes(b"saved")
    work = tmp_path / "work"
    report = {"page_count": 1, "stories": [{"id": "s1", "text": "A 🚀 boat",
        "style_ranges": [{"start": 2, "end": 8, "font_style": "Italic"}], "pages": [1]}],
        "fonts": [], "links": [], "overset": []}
    result = InDesignWorker(runner=_runner_for(work, report=report)).verify(document, work)
    # Native's end=8 counts the rocket as two UTF-16 units; Python sees it as
    # one code point, so the same span ends at code-point offset 7.
    assert result["stories"][0]["style_ranges"] == [
        {"start": 2, "end": 7, "font_style": "Italic"}]


def test_apply_rejects_same_source_and_output_before_invoking_runner(tmp_path):
    source = tmp_path / "source.indd"
    source.write_bytes(b"source")
    runner = lambda *args, **kwargs: pytest.fail("runner should not be called")
    with pytest.raises(NativeError, match="different path"):
        InDesignWorker(runner=runner).apply(source, source, [], tmp_path / "work")


def test_apply_refuses_to_replace_an_existing_output(tmp_path):
    source = tmp_path / "source.indd"
    output = tmp_path / "output.indd"
    source.write_bytes(b"source")
    output.write_bytes(b"existing")
    runner = lambda *args, **kwargs: pytest.fail("runner should not be called")
    with pytest.raises(NativeError, match="already exists"):
        InDesignWorker(runner=runner).apply(source, output, [], tmp_path / "work")


def test_runner_failure_is_reported(tmp_path):
    source = tmp_path / "source.indd"
    source.write_bytes(b"source")

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "Not authorized (-1743)")

    with pytest.raises(NativeError, match="authorized"):
        InDesignWorker(runner=runner).inspect(source, tmp_path / "work")
