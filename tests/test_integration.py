import json

from docproof.__main__ import main
from docproof.reassembler import paragraph_view_text
from docproof.utils.xml_helpers import DocxPackage, walk_package
from .conftest import FIXTURES

SPLICE_A = "The manuscript was finished, nobody wanted to read it."


def test_end_to_end_mock(tmp_path):
    mocks = tmp_path / "mocks.json"
    mocks.write_text(json.dumps([
        {"para_id": "body-0000", "original_text": SPLICE_A,
         "corrected_text": SPLICE_A.replace(",", ";", 1),
         "explanation": "Splice.", "confidence": "high"},
        {"para_id": "body-0002", "original_text": "Never appeared.",
         "corrected_text": "Nope.", "confidence": "high"},
    ]))

    rc = main(["review", str(FIXTURES / "simple.docx"),
               "--mock-findings", str(mocks), "--out", str(tmp_path)])
    assert rc == 0
    for name in ("simple - Pre-Proofread.docx", "summary.md",
                 "findings.json", "run.log"):
        assert (tmp_path / name).exists(), name

    data = json.loads((tmp_path / "findings.json").read_text())
    by_status = {f["status"] for f in data["findings"]}
    assert {"validated", "rejected_no_anchor"} <= by_status

    pkg = DocxPackage(tmp_path / "simple - Pre-Proofread.docx")
    p = {wp.para_id: wp.element for wp in walk_package(pkg)}["body-0000"]
    assert paragraph_view_text(p, "reject") == SPLICE_A      # fidelity
    assert ";" in paragraph_view_text(p, "accept")