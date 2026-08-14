import json
from argparse import Namespace

from docproof.__main__ import _configure, main
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
    for name in ("simple - Atmosphere Press Proofreader.docx", "summary.md",
                 "findings.json", "run.log"):
        assert (tmp_path / name).exists(), name

    data = json.loads((tmp_path / "findings.json").read_text())
    by_status = {f["status"] for f in data["findings"]}
    assert {"validated", "rejected_no_anchor"} <= by_status

    pkg = DocxPackage(tmp_path / "simple - Atmosphere Press Proofreader.docx")
    p = {wp.para_id: wp.element for wp in walk_package(pkg)}["body-0000"]
    assert paragraph_view_text(p, "reject") == SPLICE_A      # fidelity
    assert ";" in paragraph_view_text(p, "accept")

def test_the_variant_and_dictionary_flags_reach_the_config():
    """Both doors take the variant per run — `--variant` is the CLI's half of
    the submission panel's picker, and `--dictionary` the escape hatch for when
    the variant's Hunspell pair isn't bundled."""
    cfg, _ = _configure(Namespace(
        config=str(FIXTURES.parent.parent / "config" / "default.yaml"),
        variant="uk", dictionary="/dicts/en_GB"))
    assert cfg.variant == "uk"
    assert cfg.spellcheck.dictionary == "/dicts/en_GB"


def test_no_variant_flag_leaves_the_configs_own():
    cfg, _ = _configure(Namespace(
        config=str(FIXTURES.parent.parent / "config" / "default.yaml")))
    assert cfg.variant == "us"                # what config/default.yaml ships


def test_status_for_a_job_that_never_existed_answers_in_words(tmp_path, capsys,
                                                              monkeypatch):
    """`docproof status <id>` after the job folder was deleted used to be an
    unhandled traceback. The listing path already shrugged at unreadable
    manifests; the by-id path has to answer in words too."""
    monkeypatch.chdir(tmp_path)
    rc = main(["status", "gone-123",
               "--config", str(FIXTURES.parent.parent / "config" /
                               "default.yaml"),
               "--workspace", str(tmp_path / "jobs")])
    assert rc == 2
    assert "gone-123" in capsys.readouterr().err


def test_counts_only_preflight_matches_full_prepare(cfg):
    """The drop-time preflight runs prepare(analyses=False) to skip the
    whole-document spell scan, sweeps and consistency pass — seconds of work
    per manuscript whose results the drop screen never shows. The counts it
    *does* report must be identical to the full run's, or the estimate a user
    sees before paying would not match the review they get."""
    from docproof.pipeline import prepare, chunk_outline

    error_dir = FIXTURES.parent.parent / "config" / "error_types"
    full = prepare(cfg, FIXTURES / "styled.docx", error_dir)
    fast = prepare(cfg, FIXTURES / "styled.docx", error_dir, analyses=False)

    def counts(p):
        return (len(p.chunks), len(p.doc.paragraphs), p.request_count,
                p.est_document_tokens, len(p.groups), chunk_outline(p))

    assert counts(full) == counts(fast)
    # And the skipped analyses really are skipped, not merely empty by chance.
    assert fast.sweep_findings == []
    assert fast.spell.candidates == () and fast.spell.lexicon == ()
    assert fast.consistency_findings == []
