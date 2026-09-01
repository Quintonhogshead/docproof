"""Ensemble review: fan-out, agreement merge, and the overseer-verifier.

Single-detector mode (empty `ensemble.detectors`) is the default and must stay
byte-for-byte the old behaviour — that is guarded across the rest of the suite.
Here we exercise the machinery that only turns on when detectors are listed,
with mock providers so nothing touches the network.
"""
from __future__ import annotations

import re

import docx

from docproof.agreement import merge
from docproof.checkpoint import Checkpoint
from docproof.config import DetectorSpec, EnsembleConfig, load_config
from docproof.models import (CoverageLedger, DocumentModel, Finding,
                             ParagraphRef)
from docproof.pipeline import prepare, run_sync
from docproof.providers import NormalizedUsage, ProviderResult

U = NormalizedUsage(input_tokens=5, output_tokens=2)


class CountingProvider:
    """Records each call's model and returns a spelling fix for the paragraph
    under review, so a run produces one finding per (chunk, detector)."""

    def __init__(self, model: str, sink: list):
        self.model = model
        self.sink = sink

    def complete_structured(self, *, user, **kw) -> ProviderResult:
        self.sink.append(self.model)
        pid = re.findall(r'<paragraph id="([^"]+)"',
                         user.split("</context>")[-1])[0]
        return ProviderResult(parsed={"findings": [{
            "para_id": pid, "error_type": "spelling", "original_text": "teh",
            "corrected_text": "the", "confidence": "high", "explanation": ""}]},
            usage=U)


def _prepared(tmp_path, models, n=3):
    d = docx.Document()
    for i in range(n):
        d.add_paragraph(f"Paragraph {i} has teh typo in it.")
    src = tmp_path / "m.docx"
    d.save(src)
    cfg = load_config("config/default.yaml")
    cfg.error_types = [["spelling"]]
    cfg.chunking.token_budget = 1              # one paragraph per chunk
    # These tests isolate the detector fan-out; the post-loop model passes
    # (glossary, adjudication) would add their own calls and findings.
    cfg.glossary.enabled = False
    cfg.adjudicate.enabled = False
    cfg.ensemble = EnsembleConfig(
        detectors=[DetectorSpec(model=m) for m in models])
    return cfg, prepare(cfg, str(src), "config/error_types")


def _factory(sink):
    return lambda dcfg: CountingProvider(dcfg.api.model, sink)


def test_two_detectors_each_review_every_chunk(tmp_path):
    cfg, prepared = _prepared(tmp_path, ["gpt-5.6-luna", "claude-haiku-4-5"])
    assert len(prepared.chunks) == 3
    assert prepared.n_detectors == 2
    assert prepared.request_count == 3 * 2            # multiplier reflected

    calls = []
    cov = CoverageLedger()
    findings, _ = run_sync(cfg, prepared, provider_factory=_factory(calls),
                           coverage=cov)

    assert len(calls) == 6                            # 3 chunks x 2 detectors
    assert set(calls) == {"gpt-5.6-luna", "claude-haiku-4-5"}
    assert len(findings) == 6
    # Each finding is tagged with the detector that produced it.
    assert {f.detector for f in findings} == {0, 1}
    # Coverage is per (pass, chunk), not per call: 3 sections, all reviewed.
    assert cov.total == 3 and cov.complete


def test_detector_zero_keeps_the_legacy_checkpoint_key(tmp_path):
    cfg, prepared = _prepared(tmp_path, ["gpt-5.6-luna", "claude-haiku-4-5"])
    ckpt = Checkpoint(tmp_path / "ck.json", fingerprint={"t": 1})
    ckpt.load()
    run_sync(cfg, prepared, provider_factory=_factory([]), checkpoint=ckpt)

    keys = set(ckpt._entries)
    # Detector 0 uses the two-part key a single-detector run would have written…
    assert "p0-chunk-000" in keys
    # …and detector 1 gets the suffixed key.
    assert "p0-chunk-000-d1" in keys


def test_ensemble_resumes_from_a_checkpoint_without_recalling(tmp_path):
    cfg, prepared = _prepared(tmp_path, ["gpt-5.6-luna", "claude-haiku-4-5"])
    fp = {"t": 1}

    first = []
    ck1 = Checkpoint(tmp_path / "ck.json", fingerprint=fp)
    ck1.load()
    f1, _ = run_sync(cfg, prepared, provider_factory=_factory(first),
                     checkpoint=ck1)
    assert len(first) == 6

    # A fresh run against the same checkpoint replays everything: no new calls,
    # identical findings and detector tags.
    second = []
    ck2 = Checkpoint(tmp_path / "ck.json", fingerprint=fp)
    ck2.load()
    f2, _ = run_sync(cfg, prepared, provider_factory=_factory(second),
                     checkpoint=ck2)
    assert second == []                               # all replayed
    assert [(f.para_id, f.detector) for f in f1] == \
           [(f.para_id, f.detector) for f in f2]


# --- agreement merge ---------------------------------------------------------

def _doc(text: str) -> DocumentModel:
    return DocumentModel(source_path="x.docx", paragraphs=(
        ParagraphRef("body-0000", "word/document.xml", "body", text, "Normal"),))


def _f(fid, original, corrected, detector, etype="comma_splice", conf="high"):
    return Finding(fid, "chunk-000", "body-0000", etype, original, 1, corrected,
                   "x", conf, detector=detector)


def test_same_edit_from_two_detectors_merges_with_agreement_two():
    """One detector quotes the whole sentence, the other a clause, but both make
    the same comma->semicolon fix. Merge collapses them to one finding."""
    doc = _doc("The gate was open, the dogs were gone.")
    out = merge([
        _f("f-0001", "The gate was open, the dogs were gone.",
           "The gate was open; the dogs were gone.", 0),
        _f("f-0002", "open, the dogs", "open; the dogs", 1),
    ], doc)
    assert len(out) == 1
    assert out[0].agreement == 2
    assert out[0].provenance == (0, 1)
    assert not out[0].disputed_fix


def test_different_edits_in_one_sentence_stay_distinct():
    """Two typos in one sentence, one detector fixing each: different spans, so
    two findings survive for the validator to arbitrate — not a false merge."""
    doc = _doc("He sat on teh mat and teh dog ran.")
    out = merge([
        _f("f-0001", "He sat on teh mat and teh dog ran.",
           "He sat on the mat and teh dog ran.", 0, etype="spelling"),
        _f("f-0002", "He sat on teh mat and teh dog ran.",
           "He sat on teh mat and the dog ran.", 1, etype="spelling"),
    ], doc)
    assert len(out) == 2
    assert all(f.agreement == 1 for f in out)


def test_same_span_different_fix_is_flagged_disputed():
    """Both detectors fix the same comma splice, one with a semicolon, one with
    a full stop. Merged as agreement=2 with disputed_fix set for the verifier."""
    doc = _doc("The manuscript was finished, nobody wanted it.")
    out = merge([
        _f("f-0001", "The manuscript was finished, nobody wanted it.",
           "The manuscript was finished; nobody wanted it.", 0),
        _f("f-0002", "The manuscript was finished, nobody wanted it.",
           "The manuscript was finished. Nobody wanted it.", 1),
    ], doc)
    assert len(out) == 1
    assert out[0].agreement == 2 and out[0].disputed_fix


def test_merge_crowns_the_whole_repair_over_one_of_its_halves():
    """A coupled repair (both numerals spelled out) and one detector's half of it
    land on overlapping spans and cluster together. The representative must be the
    WHOLE repair, not the half — a half would leave the merged finding correcting
    only part of the sentence. The half is given the lower id on purpose, so only
    span-coverage (not the id tiebreak) can pick the right one."""
    doc = _doc("There were 2 and 3 owls.")
    out = merge([
        _f("f-0001", "There were 2 and 3 owls.",
           "There were two and 3 owls.", 0, etype="number_style"),        # half
        _f("f-0002", "There were 2 and 3 owls.",
           "There were two and three owls.", 1, etype="number_style"),     # whole
    ], doc)
    assert len(out) == 1
    assert out[0].corrected_text == "There were two and three owls."
    assert out[0].agreement == 2


def test_an_unanchorable_finding_passes_through_as_a_singleton():
    doc = _doc("A perfectly clean sentence sits here.")
    out = merge([_f("f-0001", "text that is simply not present",
                    "a correction", 0)], doc)
    assert len(out) == 1 and out[0].agreement == 1


def test_self_ensemble_run_merges_duplicate_findings(tmp_path):
    """End to end through run_sync: two detectors each find the same typo in
    every chunk, and the merge folds each pair into one agreement=2 finding."""
    cfg, prepared = _prepared(tmp_path, ["gpt-5.6-luna", "claude-haiku-4-5"])
    findings, _ = run_sync(cfg, prepared, provider_factory=_factory([]))
    assert len(findings) == 6                         # 3 chunks x 2 detectors
    merged = merge(findings, prepared.doc)
    assert len(merged) == 3                           # one per chunk
    assert all(f.agreement == 2 and f.provenance == (0, 1) for f in merged)


# --- overseer-verifier -------------------------------------------------------

import json                                                          # noqa: E402

from docproof.models import Usage                                    # noqa: E402
from docproof.pipeline import finish                                 # noqa: E402
from docproof.verifier import verify_findings                        # noqa: E402


class VerdictProvider:
    """Returns a canned verdict for each finding_id the verifier asks about;
    anything unlisted defaults to keep."""

    def __init__(self, verdicts: dict):
        self.verdicts = verdicts
        self.seen: list = []

    def complete_structured(self, *, user, **kw) -> ProviderResult:
        fids = re.findall(r"finding_id=(\S+)", user)
        self.seen.append(fids)
        out = []
        for fid in fids:
            verdict, fix, reason = self.verdicts.get(fid, ("keep", "", ""))
            out.append({"finding_id": fid, "verdict": verdict,
                        "chosen_fix": fix, "reason": reason})
        return ProviderResult(parsed={"verdicts": out}, usage=U)


def _ensemble(tmp_path, *, verifier="claude-opus-5", policy="disputed",
              bump=False):
    cfg, prepared = _prepared(tmp_path, ["gpt-5.6-luna", "claude-haiku-4-5"])
    cfg.ensemble = EnsembleConfig(
        detectors=[DetectorSpec(model="gpt-5.6-luna"),
                   DetectorSpec(model="claude-haiku-4-5")],
        verifier_model=verifier, verify_policy=policy,
        consensus_confidence_bump=bump)
    return cfg, prepared


def _fp(fid, original, corrected, *, agreement=2, disputed=False, conf="high"):
    # A finding on body-0000 ("Paragraph 0 has teh typo in it.").
    return Finding(fid, "chunk-000", "body-0000", "spelling", original, 1,
                   corrected, "x", conf, detector=0, agreement=agreement,
                   disputed_fix=disputed, provenance=tuple(range(agreement)))


def test_verifier_keep_reject_and_downgrade_route_correctly(tmp_path):
    cfg, prepared = _ensemble(tmp_path, policy="all")
    provider = VerdictProvider({
        "f-0001": ("keep", "", ""),
        "f-0002": ("reject", "", "not an error"),
        "f-0003": ("downgrade", "", "worth a look"),
    })
    findings = [_fp("f-0001", "teh", "the"),
                _fp("f-0002", "typo", "typo!"),
                _fp("f-0003", "has", "had")]
    survivors, rejected = verify_findings(cfg, prepared, findings, provider,
                                          Usage())

    assert [f.finding_id for f in rejected] == ["f-0002"]
    assert rejected[0].status == "rejected_by_verifier"
    byid = {f.finding_id: f for f in survivors}
    assert not byid["f-0001"].force_query            # kept as a change
    assert byid["f-0003"].force_query                # downgraded to a query
    assert byid["f-0003"].explanation == "worth a look"


def test_disputed_policy_verifies_only_non_consensus(tmp_path):
    cfg, prepared = _ensemble(tmp_path, policy="disputed")   # n_detectors == 2
    provider = VerdictProvider({"f-0002": ("reject", "", "x")})
    findings = [_fp("f-0001", "teh", "the", agreement=2),        # consensus: skip
                _fp("f-0002", "typo", "typo!", agreement=1)]     # verify
    survivors, rejected = verify_findings(cfg, prepared, findings, provider,
                                          Usage())
    sent = [fid for batch in provider.seen for fid in batch]
    assert sent == ["f-0002"]                          # consensus never sent
    assert [f.finding_id for f in rejected] == ["f-0002"]


def test_verify_policy_none_never_calls_the_verifier(tmp_path):
    cfg, prepared = _ensemble(tmp_path, policy="none")

    class Boom:
        def complete_structured(self, **kw):
            raise AssertionError("verifier must not be called")

    survivors, rejected = verify_findings(
        cfg, prepared, [_fp("f-0001", "teh", "the")], Boom(), Usage())
    assert rejected == [] and [f.finding_id for f in survivors] == ["f-0001"]


def test_a_verifier_hiccup_keeps_the_findings(tmp_path):
    cfg, prepared = _ensemble(tmp_path, policy="all")

    class Failing:
        def complete_structured(self, **kw):
            return ProviderResult(stop_reason="error", error="503", usage=U)

    survivors, rejected = verify_findings(
        cfg, prepared, [_fp("f-0001", "teh", "the")], Failing(), Usage())
    assert rejected == [] and survivors[0].finding_id == "f-0001"


def test_disputed_fix_takes_the_verifiers_choice(tmp_path):
    cfg, prepared = _ensemble(tmp_path, policy="all")
    provider = VerdictProvider({"f-0001": ("keep", "the", "")})
    survivors, _ = verify_findings(
        cfg, prepared, [_fp("f-0001", "teh", "teh", disputed=True)],
        provider, Usage())
    assert survivors[0].corrected_text == "the"      # verifier's chosen fix


def test_consensus_confidence_bump_lifts_agreed_findings(tmp_path):
    cfg, prepared = _prepared(tmp_path, ["gpt-5.6-luna", "claude-haiku-4-5"])
    cfg.ensemble = EnsembleConfig(
        detectors=[DetectorSpec(model="gpt-5.6-luna"),
                   DetectorSpec(model="claude-haiku-4-5")],
        consensus_confidence_bump=True)              # no verifier: bump only
    survivors, _ = verify_findings(cfg, prepared, [
        _fp("f-0001", "teh", "the", agreement=2, conf="low"),
        _fp("f-0002", "typo", "typo!", agreement=1, conf="low"),
    ], None, Usage())
    byid = {f.finding_id: f for f in survivors}
    assert byid["f-0001"].confidence == "medium"     # agreed -> bumped
    assert byid["f-0002"].confidence == "low"        # single -> unchanged


class _RejectAll:
    def complete_structured(self, *, user, **kw) -> ProviderResult:
        fids = re.findall(r"finding_id=(\S+)", user)
        return ProviderResult(parsed={"verdicts": [
            {"finding_id": fid, "verdict": "reject", "chosen_fix": "",
             "reason": "overseer says no"} for fid in fids]}, usage=U)


def test_verifier_reject_blocks_the_tracked_change_end_to_end(tmp_path):
    cfg, prepared = _ensemble(tmp_path, policy="all")
    findings, usage = run_sync(cfg, prepared, provider_factory=_factory([]))
    out = finish(prepared, findings, usage, cfg, out_dir=tmp_path / "out",
                 source_path=tmp_path / "m.docx", verify_provider=_RejectAll())
    assert out.applied == 0                          # nothing survived the overseer
    stats = json.loads(out.findings_json.read_text())["stats"]
    assert stats.get("rejected_by_verifier", 0) >= 1


# --- recall-tuned prompt hook (inert until a YAML fills it in) ----------------

from docproof.analyzer import build_system_prompt                    # noqa: E402
from docproof.error_registry import ErrorType, load_error_types      # noqa: E402

from .test_error_types import ERROR_DIR                              # noqa: E402


def test_ensemble_prompt_hook_is_inert_until_a_type_sets_it():
    """No shipped type carries an ensemble_detection_prompt, so the system
    prompt is byte-identical whether or not the ensemble+verifier is active —
    the hook changes nothing until a human fills one in."""
    types = list(load_error_types(ERROR_DIR, ["comma_splice", "spelling"]).values())
    assert (build_system_prompt(types, ensemble=False)
            == build_system_prompt(types, ensemble=True))


def test_ensemble_prompt_used_only_in_ensemble_mode_when_set():
    et = ErrorType(key="x", name="X", version=1,
                   detection_prompt="STANDARD RULE", fix_guidance="fix",
                   confidence_guidance="", examples=(),
                   ensemble_detection_prompt="RELAXED RULE")
    assert et.detection(False) == "STANDARD RULE"
    assert et.detection(True) == "RELAXED RULE"
    assert "RELAXED RULE" in build_system_prompt([et], ensemble=True)
    assert "RELAXED RULE" not in build_system_prompt([et], ensemble=False)


def test_verifies_property_gates_the_hook():
    from docproof.config import EnsembleConfig, DetectorSpec
    two = [DetectorSpec(model="gpt-5.6-luna"), DetectorSpec(model="claude-haiku-4-5")]
    assert not EnsembleConfig().verifies                      # single-detector
    assert not EnsembleConfig(detectors=two).verifies         # no verifier
    assert not EnsembleConfig(detectors=two, verifier_model="claude-opus-5",
                              verify_policy="none").verifies   # verifier off
    assert EnsembleConfig(detectors=two, verifier_model="claude-opus-5").verifies


# --- the explicit off switch (Redding Book 1, 2026-09-01) --------------------
#
# A materialized genre pack writes `ensemble: {enabled: false, detectors: [...]}`
# and the run fanned out to every detector anyway: the key was accepted by the
# model and then never read. `enabled: false` now wins over a populated list.

def test_enabled_false_in_yaml_turns_a_populated_ensemble_off():
    import yaml
    cfg = EnsembleConfig(**yaml.safe_load(
        """
        enabled: false
        detectors:
          - model: gpt-5.6-luna
          - model: claude-haiku-4-5
        verifier_model: claude-opus-5
        """))
    assert cfg.detectors and not cfg.enabled
    assert not cfg.verifies


def test_enabled_defaults_to_the_detector_list():
    two = [DetectorSpec(model="gpt-5.6-luna"), DetectorSpec(model="claude-haiku-4-5")]
    assert EnsembleConfig(detectors=two).enabled
    assert not EnsembleConfig().enabled
    # An explicit true is honoured, and still needs detectors to fan out to.
    assert EnsembleConfig(**{"enabled": True, "detectors": two}).enabled
    assert not EnsembleConfig(**{"enabled": True}).enabled


def test_a_disabled_ensemble_reports_one_detector_to_the_cost_estimate(tmp_path):
    cfg = load_config("config/default.yaml")
    cfg.ensemble = EnsembleConfig(**{
        "enabled": False,
        "detectors": [{"model": "gpt-5.6-luna"},
                      {"model": "claude-haiku-4-5"}]})
    cfg.output_dir = str(tmp_path)
    prepared = prepare(cfg, "tests/fixtures/simple.docx", "config/error_types",
                       dry_run=True)
    assert prepared.n_detectors == 1
