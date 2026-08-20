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


def test_model_obligation_aggregates_multiple_outcomes_without_reopening_state(
        tmp_path):
    """One paragraph/category obligation can match more than one finding.

    The Lighthouse production run surfaced this exact ordering: a soft finding
    moved the shared obligation to ``uncertain``, then a validated finding for a
    different span tried the illegal ``uncertain -> edit`` transition. Precise
    sites retain each outcome; the broad obligation records the strongest
    aggregate outcome once, after every matching finding has been observed.
    """
    text = "Teh first word and teh second word."
    para = ParagraphRef("body-0000", "word/document.xml", "body", text,
                        "Normal")
    doc = DocumentModel("book.docx", (para,))
    cfg = Config(
        error_types=[["spelling"]], sweeps=[],
        examination_graph={"enabled": True})
    run = prepare_shadow(
        cfg, doc, paragraphs=[para], sweep_findings=[],
        consistency_findings=[], spell=SpellScan(available=False),
        adjudicate_candidates=[])
    assert run is not None

    first = text.index("Teh")
    second = text.index("teh", first + 1)
    soft = Finding(
        "f-soft", "chunk-000", para.para_id, "spelling", text, 1,
        text.replace("Teh", "The", 1), "Soft candidate.", "low",
        status="skipped_low_confidence",
        anchor=Anchor(first, first + 3, "Teh", "The"))
    applied = Finding(
        "f-applied", "chunk-000", para.para_id, "spelling", text, 1,
        text[:second] + "the" + text[second + 3:], "Validated candidate.",
        "high", status="validated",
        anchor=Anchor(second, second + 3, "teh", "the"))

    run.observe_findings([soft, applied], doc,
                         applied_ids=(applied.finding_id,))

    obligation_id = run.model_obligations[(para.para_id, "spelling")]
    assert run.ledger.state(obligation_id) == LedgerState.APPLIED
    assert run.ledger.state(site_from_finding(soft, doc).site_id) \
        == LedgerState.UNCERTAIN
    assert run.ledger.state(site_from_finding(applied, doc).site_id) \
        == LedgerState.APPLIED
    run.write(tmp_path, cfg.examination_graph, source=doc.source_path)
    assert (tmp_path / "examination-coverage.md").is_file()
