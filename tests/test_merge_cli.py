"""`docproof merge` — the merge desk's CLI door (docproof/__main__.py cmd_merge).

Deterministic and $0: LanguageTool is installed in this environment, so the
contested pair below is built to fail the deterministic sweep check first
(rewrite_is_clean short-circuits before ever calling LanguageTool), never
spinning up the real JVM server in a test.
"""

from __future__ import annotations

import json

from docproof.__main__ import main
from docproof.utils.xml_helpers import DocxPackage, walk_package
from .conftest import FIXTURES

SPLICE_A = "The manuscript was finished, nobody wanted to read it."
CLEAN_B = "Although it was late, we kept driving toward the coast."
SPLICE_C = "She opened the letter, her hands would not stay still."

_CONFIG = str(FIXTURES.parent.parent / "config" / "default.yaml")


def _write(path, rows):
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def _row(fid, para_id, original, corrected, **extra):
    """A findings.json-shaped row — every field `mergedesk.tag_lane` requires,
    the same contract a real findings.json or run-checkpoint file carries."""
    row = {"finding_id": fid, "chunk_id": "chunk-000", "para_id": para_id,
          "error_type": "test", "original_text": original, "occurrence": 1,
          "corrected_text": corrected, "explanation": "because",
          "confidence": "high"}
    row.update(extra)
    return row


def test_dry_run_prints_the_ledger_and_writes_nothing(tmp_path, capsys):
    mech = _write(tmp_path / "mech.json", [
        _row("m-1", "body-0000", SPLICE_A, SPLICE_A.replace(",", ";", 1)),
    ])
    # A rewrite that reintroduces stacked punctuation — rewrite_is_clean fails
    # it on the sweep check alone, before LanguageTool is ever consulted.
    ce = _write(tmp_path / "ce.json", [
        _row("c-1", "body-0000", SPLICE_A,
            "The manuscript was finished!! Nobody wanted to read it.",
            lane="copyedit"),
    ])

    rc = main(["merge", str(FIXTURES / "simple.docx"),
              "--mechanical", str(mech), "--copyedit", str(ce),
              "--out", str(tmp_path), "--dry-run", "--json",
              "--config", _CONFIG])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Merge desk:" in out
    assert "mechanical wins over" in out

    envelope = json.loads(out.strip().splitlines()[-1])
    assert envelope["cost"] == {"total": 0.0}
    assert envelope["checkpoint"] is None
    claims = envelope["ledger"]["claims"]
    assert len(claims) == 1
    assert claims[0]["rule"] == "mechanical_default"
    assert claims[0]["winner_lane"] == "mechanical"

    assert not list(tmp_path.glob("*.docx"))          # nothing written


def test_apply_writes_two_authors_for_non_overlapping_lane_edits(tmp_path):
    mech = _write(tmp_path / "mech.json", [
        _row("m-1", "body-0000", SPLICE_A, SPLICE_A.replace(",", ";", 1)),
    ])
    ce = _write(tmp_path / "ce.json", [
        _row("c-1", "body-0002", SPLICE_C,
            "She opened the letter; her hands trembled.", lane="copyedit"),
    ])

    rc = main(["merge", str(FIXTURES / "simple.docx"),
              "--mechanical", str(mech), "--copyedit", str(ce),
              "--out", str(tmp_path), "--json", "--config", _CONFIG])
    assert rc == 0

    docs = [p for p in tmp_path.glob("*.docx") if "Change Log" not in p.name]
    assert len(docs) == 1
    pkg = DocxPackage(docs[0])
    # Every w:ins/w:del in the body carries one of the two lane authors.
    body = pkg.tree("word/document.xml")
    rev_authors = {el.get("{http://schemas.openxmlformats.org/wordprocessingml/"
                          "2006/main}author")
                  for tag in ("ins", "del") for el in body.iter(
                      f"{{http://schemas.openxmlformats.org/wordprocessingml/"
                      f"2006/main}}{tag}")}
    assert rev_authors == {"Atmosphere Press Proofreader",
                           "Atmosphere Press Copy Editor"}


def test_unknown_mechanical_field_shape_errors_cleanly(tmp_path, capsys):
    bad = _write(tmp_path / "bad.json", [{"para_id": "body-0000"}])
    rc = main(["merge", str(FIXTURES / "simple.docx"),
              "--mechanical", str(bad), "--out", str(tmp_path),
              "--dry-run", "--config", _CONFIG])
    assert rc == 2
    assert "missing required" in capsys.readouterr().err


def test_mechanical_only_run_is_a_normal_single_author_merge(tmp_path):
    mech = _write(tmp_path / "mech.json", [
        _row("m-1", "body-0000", SPLICE_A, SPLICE_A.replace(",", ";", 1)),
    ])
    rc = main(["merge", str(FIXTURES / "simple.docx"),
              "--mechanical", str(mech), "--out", str(tmp_path),
              "--json", "--config", _CONFIG])
    assert rc == 0
    docs = [p for p in tmp_path.glob("*.docx") if "Change Log" not in p.name]
    assert len(docs) == 1
    pkg = DocxPackage(docs[0])
    body = pkg.tree("word/document.xml")
    authors = {el.get("{http://schemas.openxmlformats.org/wordprocessingml/"
                      "2006/main}author")
              for el in body.iter(
                  "{http://schemas.openxmlformats.org/wordprocessingml/"
                  "2006/main}ins")}
    assert authors == {"Atmosphere Press Proofreader"}


def test_a_findings_json_carrying_a_split_pair_rebuilds_to_disjoint_anchors(tmp_path):
    """Replay a prior build's report through `docproof merge` on the fixture
    novel: the pre-split pair (f-0077 / f-0077b, identical text) must come
    out as two disjoint minimal regions under unique ids, and the edit map
    finish() writes beside findings.json must map the paragraph with nothing
    skipped (the Redding final build had 16 pairs the map had to tolerate)."""
    from docproof.config import Config
    from docproof.editmap import EDITMAP_NAME, EditMap
    from docproof.ingest import build_document_model, preflight

    doc = build_document_model(preflight(FIXTURES / "tiny_novel.docx", "abort"),
                               Config())
    para = next(p for p in doc.paragraphs if p.text.startswith("Kathryn had lived"))
    original = para.text
    corrected = (original.replace("lived", "LIVED", 1)
                 .replace("her whole", "HER whole", 1))
    mech = _write(tmp_path / "mech.json", {"findings": [
        _row("f-0077", para.para_id, original, corrected,
             error_type="capitalization", status="validated"),
        _row("f-0077b", para.para_id, original, corrected,
             error_type="capitalization", status="validated"),
    ]})
    out = tmp_path / "out"
    rc = main(["merge", str(FIXTURES / "tiny_novel.docx"),
               "--mechanical", str(mech), "--out", str(out),
               "--config", _CONFIG])
    assert rc == 0

    rows = json.loads((out / "findings.json").read_text(encoding="utf-8"))["findings"]
    ids = [r["finding_id"] for r in rows]
    assert len(ids) == len(set(ids))
    mine = [r for r in rows if r["finding_id"].startswith("f-0077")]
    assert [(r["finding_id"], r["status"], r["applied"]) for r in mine] == \
        [("f-0077", "validated", True), ("f-0077b", "validated", True)]
    (s1, e1), (s2, e2) = [(r["anchor"]["start"], r["anchor"]["end"]) for r in mine]
    assert s1 < e1 <= s2 < e2
    assert [r["anchor"]["insert_text"] for r in mine] == ["LIVED", "HER"]

    em = EditMap.load(out / EDITMAP_NAME)
    assert em.skipped == {}
    assert para.para_id in em.paragraphs and para.para_id not in em.unmapped
