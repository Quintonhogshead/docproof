"""The repair channel: the error-density trigger, per-sentence repair, the
whole-sentence judge, the guard exemption, and — the load-bearing invariant —
that a cluster is never shipped by halves."""
import dataclasses
from itertools import count

from docproof.config import EditGuardConfig
from docproof.eval.repair_shadow import _norm
from docproof.models import Anchor, DocumentModel, Finding, ParagraphRef, Usage
from docproof.providers import ProviderResult
from docproof.repair import (BrokenSite, RepairCluster, _members, confirm,
                             enforce_cluster_atomicity, repair_sites,
                             triggered_sentences)
from docproof.validator import validate_findings


def _para(pid, text, reviewable=True):
    return ParagraphRef(pid, "word/document.xml", "body", text, "Normal",
                        reviewable)


def _doc(paras):
    return DocumentModel("x.docx", tuple(paras))


def _edit(para_id, original, corrected, error_type="grammar",
          force_query=False, occurrence=1):
    return Finding(finding_id="f", chunk_id="c", para_id=para_id,
                   error_type=error_type, original_text=original,
                   corrected_text=corrected, occurrence=occurrence,
                   explanation="", confidence="high", force_query=force_query)


class _Provider:
    """Replays canned ProviderResults, one per complete_structured call."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete_structured(self, *, model, system, user, schema, schema_name,
                            max_tokens):
        self.calls.append(user)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            return ProviderResult(stop_reason="error", error=str(reply))
        return ProviderResult(parsed=reply)


# --- the trigger: error density per sentence (component 1) --------------------

def test_threshold_routes_only_error_dense_sentences():
    para = _para("p1", "He run to teh stor. All is well here.")
    s1 = "He run to teh stor."
    findings = [
        _edit("p1", s1, "He ran to teh stor."),      # run->ran
        _edit("p1", s1, "He run to the stor."),       # teh->the
        _edit("p1", s1, "He run to teh store."),      # stor->store
        _edit("p1", "All is well here.", "All is well, here."),  # 1 edit, sentence 2
    ]
    sites = triggered_sentences(findings, [para], threshold=3)
    assert len(sites) == 1
    site = sites[0]
    assert site.sentence == s1 and site.error_count == 3 and site.occurrence == 1
    # Below threshold: nothing routed.
    assert triggered_sentences(findings, [para], threshold=4) == []


def test_queries_and_repair_findings_do_not_count():
    para = _para("p1", "He run to teh stor now.")
    s = "He run to teh stor now."
    findings = [
        _edit("p1", s, "He ran to teh stor now."),
        _edit("p1", s, "He run to the stor now."),
        _edit("p1", s, "He run to teh stor now.", force_query=True),  # a question
        _edit("p1", s, "He run to teh store now.", error_type="repair"),  # own kind
    ]
    # Only the two real corrections count, so the 3-threshold is not met.
    assert triggered_sentences(findings, [para], threshold=3) == []
    assert len(triggered_sentences(findings, [para], threshold=2)) == 1


def test_paragraph_quoting_finding_maps_to_its_sentence():
    para = _para("p1", "First is fine. He run to teh stor now.")
    s2 = "He run to teh stor now."
    # A rewrite-style finding quotes the WHOLE paragraph; it must still be
    # attributed to the sentence its edit falls in.
    whole = para.text
    findings = [
        _edit("p1", whole, whole.replace("run", "ran")),
        _edit("p1", s2, "He run to the stor now."),
        _edit("p1", s2, "He run to teh store now."),
    ]
    sites = triggered_sentences(findings, [para], threshold=3)
    assert len(sites) == 1 and sites[0].sentence == s2


# --- per-sentence repair ------------------------------------------------------

def _site(para_id="p1", sentence="he run to school.", reasons=("grammar",)):
    return BrokenSite(para_id, sentence, 1, len(reasons), tuple(reasons))


def test_repair_sites_builds_a_cluster_from_the_model_repair():
    sites = [_site(sentence="he run to school.")]
    provider = _Provider([{"repairs": [
        {"index": 1, "repaired": "He ran to school."}]}])
    clusters = repair_sites(sites, provider, model="m", max_output_tokens=1000,
                            usage=Usage())
    assert len(clusters) == 1
    c = clusters[0]
    assert c.cluster_id == "rp-c-0001" and c.sentence == "he run to school."
    rebuilt = c.sentence
    for m in sorted(c.members, key=lambda a: a.start, reverse=True):
        rebuilt = rebuilt[:m.start] + m.insert_text + rebuilt[m.end:]
    assert rebuilt == "He ran to school."


def test_repair_sites_drops_unchanged_and_oversize():
    sites = [_site(sentence="A clean sentence."),
             _site(sentence="Another clean one.")]
    provider = _Provider([{"repairs": [
        {"index": 1, "repaired": "A clean sentence."},          # unchanged -> noop
        {"index": 2, "repaired": "Another clean one " + "x" * 200 + "."}]}])  # too large
    stats = {}
    clusters = repair_sites(sites, provider, model="m", max_output_tokens=1000,
                            usage=Usage(), stats=stats)
    assert clusters == []
    assert stats["noop"] == 1 and stats["too_large"] == 1


# --- the whole-sentence judge -------------------------------------------------

def _cluster(cid="rp-c-0001", sentence="he run to school.",
             repaired="He ran to school."):
    return RepairCluster(cid, "p1", sentence, 1, repaired, "grammar",
                         _members(sentence, repaired))


def test_confirm_high_affirmation_emits_atomic_member_findings():
    provider = _Provider([{"verdicts": [
        {"index": 1, "broken": True, "fixes": True,
         "meaning_preserved": True, "confidence": "high"}]}])
    findings = confirm([_cluster()], provider, model="m", max_tokens=1000,
                       usage=Usage(), ids=count(1), edit_confidence="high")
    assert findings and all(f.cluster_id == "rp-c-0001" for f in findings)
    assert all(f.error_type == "repair" and not f.force_query for f in findings)
    assert all(f.original_text == "he run to school." for f in findings)


def test_confirm_soft_affirmation_becomes_one_margin_query():
    provider = _Provider([{"verdicts": [
        {"index": 1, "broken": True, "fixes": True,
         "meaning_preserved": True, "confidence": "medium"}]}])
    findings = confirm([_cluster()], provider, model="m", max_tokens=1000,
                       usage=Usage(), ids=count(1), edit_confidence="high")
    assert len(findings) == 1
    assert findings[0].force_query and findings[0].cluster_id == ""
    assert findings[0].corrected_text == "He ran to school."


def test_confirm_rejects_when_not_broken_or_meaning_changed():
    clusters = [_cluster(), _cluster("rp-c-0002")]
    provider = _Provider([{"verdicts": [
        {"index": 1, "broken": False, "fixes": True,
         "meaning_preserved": True, "confidence": "high"},
        {"index": 2, "broken": True, "fixes": True,
         "meaning_preserved": False, "confidence": "high"}]}])
    rejected = []
    findings = confirm(clusters, provider, model="m", max_tokens=1000,
                       usage=Usage(), ids=count(1), edit_confidence="high",
                       reject_sink=rejected)
    assert findings == [] and len(rejected) == 2


# --- the guard exemption ------------------------------------------------------

def test_repair_member_bypasses_the_edit_guard():
    para = _para("p1", "She said nothing.")
    guard = EditGuardConfig(enabled=True, max_edit_chars=64, max_added_chars=16)
    f = Finding(
        finding_id="rp-1", chunk_id="repair", para_id="p1", error_type="repair",
        original_text="She said nothing.",
        corrected_text="She said nothing, and turned away.",
        occurrence=1, explanation="", confidence="high", cluster_id="rp-c-0001")
    exempt = validate_findings([f], _doc([para]), "low", edit_guard=guard,
                               guard_exempt=frozenset({"repair"}))
    assert exempt[0].status == "validated"
    plain = validate_findings([dataclasses.replace(f, error_type="grammar")],
                              _doc([para]), "low", edit_guard=guard)
    assert plain[0].status == "rejected_oversized"


# --- atomicity: the load-bearing invariant ------------------------------------

def _validated(pid, para_id, original, corrected, cid, status="validated",
               force_query=False):
    return Finding(
        finding_id=pid, chunk_id="repair", para_id=para_id, error_type="repair",
        original_text=original, corrected_text=corrected, occurrence=1,
        explanation="", confidence="high", status=status,
        force_query=force_query, cluster_id=cid,
        anchor=Anchor(0, len(original), original, ""))


def test_intact_cluster_is_left_alone():
    para = _para("p1", "He run to the store fast.")
    validated = [
        _validated("m1", "p1", "He run to the store fast.",
                   "He ran to the store fast.", "rp-c-0001"),
        _validated("m2", "p1", "He run to the store fast.",
                   "He run to the store quickly.", "rp-c-0001"),
    ]
    assert enforce_cluster_atomicity(validated, _doc([para])) == 0
    assert all(f.status == "validated" for f in validated)


def test_partial_cluster_is_fully_withdrawn():
    para = _para("p1", "He run to the store fast.")
    validated = [
        _validated("m1", "p1", "He run to the store fast.",
                   "He ran to the store fast.", "rp-c-0001"),
        _validated("m2", "p1", "He run to the store fast.",
                   "He run to the store quickly.", "rp-c-0001",
                   status="rejected_overlap"),
    ]
    assert enforce_cluster_atomicity(validated, _doc([para])) == 1
    m1 = next(f for f in validated if f.finding_id == "m1")
    assert m1.status == "query" and m1.withheld and m1.force_query
    m2 = next(f for f in validated if f.finding_id == "m2")
    assert m2.status == "rejected_overlap"


def test_gate_withdrawal_of_one_member_pulls_the_rest():
    para = _para("p1", "He run to the store fast.")
    validated = [
        _validated("m1", "p1", "He run to the store fast.",
                   "He ran to the store fast.", "rp-c-0001"),
        _validated("m2", "p1", "He run to the store fast.",
                   "He run to the store quickly.", "rp-c-0001",
                   status="query", force_query=True),
    ]
    assert enforce_cluster_atomicity(validated, _doc([para])) == 1
    assert all(f.force_query for f in validated)


def test_non_cluster_findings_are_never_touched():
    para = _para("p1", "He run to the store fast.")
    lone = _validated("x1", "p1", "He run to the store fast.",
                      "He ran to the store fast.", "")   # no cluster_id
    validated = [lone]
    assert enforce_cluster_atomicity(validated, _doc([para])) == 0
    assert validated[0].status == "validated"


# --- integration: real validator arbitration + atomicity ----------------------

def _repair_members(para_text, repaired):
    """Member findings the production way: cluster -> confirm at high confidence."""
    cluster = RepairCluster("rp-c-0001", "p1", para_text, 1, repaired, "grammar",
                            _members(para_text, repaired))
    provider = _Provider([{"verdicts": [
        {"index": 1, "broken": True, "fixes": True,
         "meaning_preserved": True, "confidence": "high"}]}])
    return confirm([cluster], provider, model="m", max_tokens=1000,
                   usage=Usage(), ids=count(1), edit_confidence="high")


def test_members_validate_to_disjoint_anchors_and_ship_whole():
    text = "he saw the dog run away."
    para = _para("p1", text)
    members = _repair_members(text, "He saw the dog ran away.")
    guard = EditGuardConfig(enabled=True)
    validated = validate_findings(members, _doc([para]), "low", edit_guard=guard,
                                  guard_exempt=frozenset({"repair"}))
    assert all(f.status == "validated" for f in validated)
    spans = sorted((f.anchor.start, f.anchor.end) for f in validated)
    assert spans[0][1] <= spans[1][0]
    assert enforce_cluster_atomicity(validated, _doc([para])) == 0


def test_a_surer_edit_inside_the_sentence_withdraws_the_whole_repair():
    text = "he saw the dog run away."
    para = _para("p1", text)
    sweep = Finding(
        finding_id="sw-1", chunk_id="sweep", para_id="p1",
        error_type="terminal_mark", original_text=text,
        corrected_text="he saw the dog RAN away.", occurrence=1,
        explanation="", confidence="high")
    members = _repair_members(text, "He saw the dog ran away.")
    guard = EditGuardConfig(enabled=True)
    validated = validate_findings([sweep, *members], _doc([para]), "low",
                                  edit_guard=guard,
                                  guard_exempt=frozenset({"repair"}))
    enforce_cluster_atomicity(validated, _doc([para]))
    sw = next(f for f in validated if f.finding_id == "sw-1")
    assert sw.status == "validated"
    reps = [f for f in validated if f.error_type == "repair"]
    assert not any(f.status == "validated" and not f.force_query for f in reps)
    assert any(f.withheld for f in reps)


# --- pipeline wiring (finish()-resident, fail-open) ---------------------------

class _Prepared:
    whole_document = True
    vocabulary = ""
    conventions = ""
    story_sheet = ""

    def __init__(self, paras):
        self.doc = _doc(paras)


def test_pipeline_repair_off_returns_empty():
    from docproof.config import Config
    from docproof.pipeline import _repair_findings
    cfg = Config()
    assert _repair_findings(cfg, _Prepared([_para("p1", "x")]), Usage(),
                            trigger_findings=[]) == []


def test_pipeline_repair_skips_without_key(monkeypatch):
    from docproof import providers
    from docproof.config import Config
    from docproof.pipeline import _repair_findings

    def boom(cfg):
        raise RuntimeError("No Claude API key found.")
    monkeypatch.setattr(providers, "build_provider", boom)

    class _Coverage:
        def __init__(self): self.notes = []
        def note(self, *a): self.notes.append(a)

    para = _para("p1", "He run to teh stor.")
    s = para.text
    trig = [_edit("p1", s, "He ran to teh stor."),
            _edit("p1", s, "He run to the stor."),
            _edit("p1", s, "He run to teh store.")]
    cfg = Config(repair={"enabled": True, "error_threshold": 3})
    coverage = _Coverage()
    out = _repair_findings(cfg, _Prepared([para]), Usage(), coverage=coverage,
                           trigger_findings=trig)
    assert out == []
    assert any("Repair" in n[0] for n in coverage.notes)


def test_pipeline_repair_happy_path_emits_cluster_findings(monkeypatch):
    from docproof import providers
    from docproof.config import Config
    from docproof.pipeline import _repair_findings

    provider = _Provider([
        {"repairs": [{"index": 1, "repaired": "He ran to the store."}]},
        {"verdicts": [{"index": 1, "broken": True, "fixes": True,
                       "meaning_preserved": True, "confidence": "high"}]}])
    monkeypatch.setattr(providers, "build_provider", lambda cfg: provider)

    para = _para("p1", "he run to teh stor.")
    s = para.text
    trig = [_edit("p1", s, "He run to teh stor."),   # cap
            _edit("p1", s, "he run to the stor."),    # teh->the
            _edit("p1", s, "he run to teh store.")]   # stor->store
    cfg = Config(repair={"enabled": True, "error_threshold": 3})
    out = _repair_findings(cfg, _Prepared([para]), Usage(), trigger_findings=trig)
    assert out and all(f.error_type == "repair" for f in out)
    assert all(f.cluster_id for f in out) and all(not f.force_query for f in out)


# --- shadow-mode scorer (component 8) -----------------------------------------

def test_shadow_scores_edits_against_a_human_reference():
    from docproof.eval.repair_shadow import run_shadow

    sites = [_site("p1", "He run to the store."),
             _site("p2", "She were happy.")]
    human = {("p1", _norm("He run to the store.")): "He ran to the store.",
             ("p2", _norm("She were happy.")): "She was very happy."}
    provider = _Provider([
        {"repairs": [{"index": 1, "repaired": "He ran to the store."},
                     {"index": 2, "repaired": "She was happy."}]},
        {"verdicts": [{"index": 1, "broken": True, "fixes": True,
                       "meaning_preserved": True, "confidence": "high"},
                      {"index": 2, "broken": True, "fixes": True,
                       "meaning_preserved": True, "confidence": "high"}]}])
    report = run_shadow(sites, provider, model="m", human_repairs=human)
    assert report.triggered == 2 and report.edits == 2
    assert report.same == 1 and report.different == 1 and report.machine_only == 0
    assert report.human_total == 2 and report.missed == 0


def test_shadow_counts_a_missed_human_repair():
    from docproof.eval.repair_shadow import run_shadow

    sites = [_site("p1", "He run to the store.")]
    human = {("p1", _norm("He run to the store.")): "He ran to the store.",
             ("p9", _norm("Some other broken sentence here.")):
                 "Some other, broken sentence here."}
    provider = _Provider([
        {"repairs": [{"index": 1, "repaired": "He ran to the store."}]},
        {"verdicts": [{"index": 1, "broken": True, "fixes": True,
                       "meaning_preserved": True, "confidence": "high"}]}])
    report = run_shadow(sites, provider, model="m", human_repairs=human)
    assert report.same == 1 and report.missed == 1


def test_human_repairs_from_keeps_clusters_drops_lone_tokens():
    from docproof.eval.repair_shadow import human_repairs_from

    base = [_para("p1", "He run to the store. The cat sat still."),
            _para("p2", "It was a quiet, still night.")]
    repaired = [_para("p1", "He ran to the store, quickly. The cat sat still."),
                _para("p2", "It was a quiet still night.")]
    ref = human_repairs_from(base, repaired)
    assert ("p1", _norm("He run to the store.")) in ref
    assert ref[("p1", _norm("He run to the store."))] == "He ran to the store, quickly."
    assert all(pid != "p2" for pid, _ in ref)
