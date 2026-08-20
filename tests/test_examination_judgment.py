from docproof.adjudicate import Candidate
from docproof.checkpoint import Checkpoint
from docproof.config import Config
from docproof.examination import prepare_shadow
from docproof.examination_judgment import run_shadow_judgment
from docproof.models import Anchor, DocumentModel, Finding, ParagraphRef
from docproof.pipeline import Prepared, run_sync
from docproof.providers import ProviderResult
from docproof.site_models import LedgerState
from docproof.spellscan import SpellScan

from .fakes import FakeProvider, USAGE


def _prepared_run(*, max_cost=2.0):
    text = "I staired at the wall for a long and silent moment."
    para = ParagraphRef("body-0000", "word/document.xml", "body", text,
                        "Normal")
    doc = DocumentModel("book.docx", (para,))
    start = text.index("staired")
    candidate = Candidate(para.para_id, "staired", start, start + 7,
                          "stared", "typo")
    cfg = Config(
        error_types=["spelling"], sweeps=[],
        examination_graph={
            "enabled": True,
            "judgment": {
                "enabled": True, "sample_rate": 1.0, "max_sites": 10,
                "max_cost_usd": max_cost,
                "eligible_generator_prefixes": ["deterministic.adjudicate"],
            },
        })
    run = prepare_shadow(
        cfg, doc, paragraphs=[para], sweep_findings=[],
        consistency_findings=[], spell=SpellScan(available=False),
        adjudicate_candidates=[candidate])
    assert run is not None
    return cfg, run, para, candidate


def _error(site_id):
    return ProviderResult(parsed={
        "pass_ids": [],
        "errors": [{"site_id": site_id, "correction": "stared",
                    "explanation": "The context needs the verb stared.",
                    "confidence": "high"}],
        "uncertain": [], "defer_ids": [],
    }, usage=USAGE)


def _pass(site_id):
    return ProviderResult(parsed={
        "pass_ids": [site_id], "errors": [], "uncertain": [],
        "defer_ids": [],
    }, usage=USAGE)


def test_phase_1b_judges_a_precise_site_without_creating_a_finding():
    cfg, run, _para, _candidate = _prepared_run()
    site_id = next(iter(
        s.site_id for s in run.ledger.sites
        if s.generator.startswith("deterministic.adjudicate")))
    provider = FakeProvider([_error(site_id)])

    usage = run_shadow_judgment(
        run, cfg, provider_factory=lambda _cfg: provider)

    assert provider.calls
    assert usage.api_calls == 1
    assert run.judgment_verdicts[site_id].decision == "error"
    assert run.ledger.state(site_id) == LedgerState.MODEL_CONFIRMED
    assert run.judgment_report(cfg.examination_graph.judgment) \
        ["may_create_findings"] is False


def test_completed_packet_replays_from_checkpoint_without_a_second_call(tmp_path):
    cfg, first, _para, _candidate = _prepared_run()
    site_id = next(s.site_id for s in first.ledger.sites
                   if s.generator.startswith("deterministic.adjudicate"))
    checkpoint = Checkpoint(tmp_path / "checkpoint.json",
                            fingerprint={"test": "phase-1b"})
    checkpoint.load()
    provider = FakeProvider([_pass(site_id)])
    run_shadow_judgment(first, cfg, provider_factory=lambda _cfg: provider,
                        checkpoint=checkpoint)
    assert len(provider.calls) == 1

    _cfg, resumed, _p, _c = _prepared_run()
    replay = Checkpoint(tmp_path / "checkpoint.json",
                        fingerprint={"test": "phase-1b"})
    assert replay.load() == 1
    no_call_provider = FakeProvider([])
    usage = run_shadow_judgment(
        resumed, cfg, provider_factory=lambda _cfg: no_call_provider,
        checkpoint=replay)
    assert no_call_provider.calls == []
    assert usage.api_calls == 1
    assert resumed.ledger.state(site_id) == LedgerState.MODEL_PASSED


def test_failed_packet_spend_survives_resume_and_counts_toward_the_cap(tmp_path):
    cfg, first, _para, _candidate = _prepared_run()
    checkpoint = Checkpoint(tmp_path / "checkpoint.json",
                            fingerprint={"test": "phase-1b-failure"})
    checkpoint.load()
    burned = FakeProvider([ProviderResult(parsed=None, usage=USAGE)])
    first_usage = run_shadow_judgment(
        first, cfg, provider_factory=lambda _cfg: burned,
        checkpoint=checkpoint)
    assert first_usage.api_calls == 1

    _cfg, resumed, _p, _c = _prepared_run()
    replay = Checkpoint(tmp_path / "checkpoint.json",
                        fingerprint={"test": "phase-1b-failure"})
    assert replay.load() == 0  # failed calls retry; their usage still replays
    site_id = next(s.site_id for s in resumed.ledger.sites
                   if s.generator.startswith("deterministic.adjudicate"))
    retry = FakeProvider([_pass(site_id)])
    total = run_shadow_judgment(
        resumed, cfg, provider_factory=lambda _cfg: retry,
        checkpoint=replay)
    assert total.api_calls == 2
    assert resumed.ledger.state(site_id) == LedgerState.MODEL_PASSED


def test_cost_ceiling_leaves_site_explicitly_pending_without_calling():
    cfg, run, _para, _candidate = _prepared_run(max_cost=0.000001)
    provider = FakeProvider([])
    run_shadow_judgment(run, cfg, provider_factory=lambda _cfg: provider)
    site_id = next(s.site_id for s in run.ledger.sites
                   if s.generator.startswith("deterministic.adjudicate"))
    assert provider.calls == []
    assert site_id in run.judgment_budget_omissions
    assert run.ledger.state(site_id) == LedgerState.NEEDS_JUDGMENT


def test_production_observation_compares_without_overwriting_blind_verdict():
    cfg, run, para, candidate = _prepared_run()
    site_id = next(s.site_id for s in run.ledger.sites
                   if s.generator.startswith("deterministic.adjudicate"))
    run_shadow_judgment(
        run, cfg, provider_factory=lambda _cfg: FakeProvider([_error(site_id)]))
    finding = Finding(
        "f-0001", "chunk-000", para.para_id, "spelling", para.text, 1,
        para.text.replace("staired", "stared"), "Wrong verb.", "high",
        status="validated",
        anchor=Anchor(candidate.start, candidate.end, "staired", "stared"))
    run.observe_findings([finding], run.doc, applied_ids=(finding.finding_id,))

    assert run.ledger.state(site_id) == LedgerState.MODEL_CONFIRMED
    assert run.comparison()["both_found_error"] == 1
    observation = [e for e in run.ledger.events
                   if e.site_id == site_id and e.event_kind == "observation"]
    assert observation


def test_evaluation_queue_is_blinded_and_answer_key_is_separate():
    cfg, run, _para, _candidate = _prepared_run()
    site_id = next(s.site_id for s in run.ledger.sites
                   if s.generator.startswith("deterministic.adjudicate"))
    run_shadow_judgment(
        run, cfg, provider_factory=lambda _cfg: FakeProvider([_error(site_id)]))
    queue, key = run.evaluation_queue()
    assert len(queue) == len(key) == 1
    assert "source" not in str(queue[0]).lower()
    assert {key[0]["candidate_a_source"], key[0]["candidate_b_source"]} == {
        "examination", "production"}


def test_run_sync_bills_shadow_usage_but_returns_no_shadow_findings():
    cfg, run, _para, _candidate = _prepared_run()
    cfg.adjudicate.enabled = False
    site_id = next(s.site_id for s in run.ledger.sites
                   if s.generator.startswith("deterministic.adjudicate"))
    provider = FakeProvider([_error(site_id)])
    prepared = Prepared(
        pkg=None, doc=run.doc, chunks=[], groups=[], fmt=None,
        whole_document=False, examination=run)

    findings, usage = run_sync(
        cfg, prepared, provider,
        provider_factory=lambda _cfg: provider)

    assert findings == []
    assert usage.api_calls == 1
    assert run.judgment_verdicts[site_id].decision == "error"


def test_defer_can_escalate_to_a_separate_stronger_judge():
    cfg, run, _para, _candidate = _prepared_run()
    cfg.examination_graph.judgment.escalation_model = "claude-opus-5"
    site_id = next(s.site_id for s in run.ledger.sites
                   if s.generator.startswith("deterministic.adjudicate"))
    primary = FakeProvider([ProviderResult(parsed={
        "pass_ids": [], "errors": [], "uncertain": [],
        "defer_ids": [site_id],
    }, usage=USAGE)])
    strong = FakeProvider([_pass(site_id)])

    def factory(config):
        return strong if config.api.model == "claude-opus-5" else primary

    usage = run_shadow_judgment(run, cfg, provider_factory=factory)
    assert len(primary.calls) == len(strong.calls) == 1
    assert usage.api_calls == 2
    assert run.ledger.state(site_id) == LedgerState.MODEL_PASSED
    assert run.judgment_verdicts[site_id].judge == "claude-opus-5"
