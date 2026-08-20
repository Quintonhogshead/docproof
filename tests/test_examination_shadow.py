import dataclasses
import json

from docproof.config import Config, load_config
from docproof.examination import prepare_shadow
from docproof.models import Anchor, DocumentModel, Finding, ParagraphRef
from docproof.site_generators import site_from_finding
from docproof.site_models import LedgerState
from docproof.spellscan import SpellScan


def _finding(fid, chunk, error_type, original, corrected, *, start, end):
    return Finding(
        fid, chunk, "body-0000", error_type, original, 1, corrected,
        "test finding", "high", status="validated",
        anchor=Anchor(start, end, original[start:end], corrected[start:end]))


def test_shipped_config_enables_shadow_but_bare_config_is_rollback_safe():
    assert Config().examination_graph.enabled is False
    cfg = load_config("config/default.yaml")
    assert cfg.examination_graph.enabled is True
    assert cfg.examination_graph.mode == "shadow"


def test_deployment_kill_switch_wins_over_shipped_and_per_run_config(
        monkeypatch):
    monkeypatch.setenv("DOCPROOF_EXAMINATION_GRAPH", "0")
    cfg = load_config("config/default.yaml")
    assert cfg.examination_graph.enabled is False

    # A stored job may still contain the old per-run "on" choice. The runtime
    # gate must win too, after feature overrides have been applied.
    cfg.examination_graph.enabled = True
    para = ParagraphRef("body-0000", "word/document.xml", "body",
                        "A clean paragraph.", "Normal")
    doc = DocumentModel("book.docx", (para,))
    assert prepare_shadow(
        cfg, doc, paragraphs=[para], sweep_findings=[],
        consistency_findings=[], spell=SpellScan(available=False),
        adjudicate_candidates=[]) is None


def test_paid_judgment_has_an_independent_deployment_kill_switch(monkeypatch):
    monkeypatch.setenv("DOCPROOF_EXAMINATION_JUDGMENT", "off")
    cfg = load_config("config/default.yaml")
    assert cfg.examination_graph.enabled is True
    assert cfg.examination_graph.judgment.enabled is False


def test_pure_insertion_finding_becomes_a_zero_width_site():
    text = "She went the shop."
    para = ParagraphRef("body-0000", "word/document.xml", "body", text,
                        "Normal")
    doc = DocumentModel("book.docx", (para,))
    finding = Finding(
        "f-insert", "chunk-000", para.para_id, "missing_word", text, 1,
        "She went to the shop.", "Missing preposition.", "high")
    site = site_from_finding(finding, doc)
    assert site is not None
    assert site.anchors[0].start_offset == site.anchors[0].end_offset


def test_shadow_observes_applied_findings_and_writes_separate_artifacts(tmp_path):
    text = "This is is teh draft."
    para = ParagraphRef("body-0000", "word/document.xml", "body", text, "Normal")
    doc = DocumentModel("book.docx", (para,))
    cfg = Config(
        error_types=["spelling"], sweeps=["sweep_doubled_word"],
        examination_graph={"enabled": True})

    sweep = Finding(
        "s-0001", "sweep", para.para_id, "sweep_doubled_word", text, 1,
        "This is teh draft.", "doubled word", "high")
    run = prepare_shadow(
        cfg, doc, paragraphs=[para], sweep_findings=[sweep],
        consistency_findings=[], spell=SpellScan(available=False),
        adjudicate_candidates=[])
    assert run is not None

    sweep_valid = dataclasses.replace(
        sweep, status="validated",
        anchor=Anchor(8, 11, "is ", ""))
    spelling = Finding(
        "f-0001", "chunk-000", para.para_id, "spelling", text, 1,
        "This is is the draft.", "teh should be the", "high",
        status="validated", anchor=Anchor(11, 14, "teh", "the"))
    run.observe_findings([sweep_valid, spelling], doc,
                         applied_ids=("s-0001", "f-0001"))

    states = run.ledger.state_counts()
    assert states["applied"] >= 3  # two precise sites + spelling obligation
    report, ledger_path, markdown_path = run.write(
        tmp_path, cfg.examination_graph, source=doc.source_path)
    assert ledger_path.is_file()
    assert markdown_path.is_file()
    assert (tmp_path / "examination-coverage.json").is_file()
    assert report["scope"]["shadow_only"] is True
    assert report["scope"]["may_create_edits"] is False
    assert report["accounting"]["all_sites_have_state"] is True
    assert json.loads((tmp_path / "examination-coverage.json").read_text()) \
        ["accounting"]["generated_sites"] == len(run.ledger)
