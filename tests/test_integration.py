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

def _cli_config(*argv):
    """What `docproof review …` would run with, without running it."""
    import argparse

    from docproof.__main__ import _common, _configure
    parser = argparse.ArgumentParser()
    _common(parser)
    config = FIXTURES.parent.parent / "config" / "default.yaml"
    args = parser.parse_args([str(FIXTURES / "simple.docx"),
                              "--config", str(config), *argv])
    return _configure(args)[0]


def test_the_dictionary_scan_can_be_turned_off_for_one_run():
    """The dictionary belongs to the manuscript, not the install: a book dense
    enough in invented vocabulary is better read without one."""
    assert _cli_config().spellcheck.enabled is True
    assert _cli_config("--no-spellcheck").spellcheck.enabled is False


def test_one_run_can_name_its_own_dictionary():
    """spylls ships en_US only, so a British manuscript needs an .aff/.dic
    pair of its own — for that book, not for every book after it."""
    cfg = _cli_config("--dictionary", "dicts/en_GB")
    assert cfg.spellcheck.dictionary == "dicts/en_GB"
    assert cfg.spellcheck.enabled is True
