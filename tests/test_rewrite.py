"""Rewrite-then-diff: paragraphs diffed against their minimal-edit rewrite
become candidates, and a skeptical confirm routes each to an edit, a query, or
nothing — the adjudication valve, for span edits instead of word typos."""
from __future__ import annotations

import itertools

from docproof.models import Chunk, ParagraphRef, Usage
from docproof.providers.base import ProviderResult
from docproof.rewrite import (RewriteCandidate, _diff_candidates, confirm,
                              propose)

from .fakes import FakeProvider


def _para(pid: str, text: str) -> ParagraphRef:
    return ParagraphRef(para_id=pid, part="word/document.xml", location="body",
                        text=text, style="Normal")


def _chunk(*paras: ParagraphRef) -> Chunk:
    return Chunk(chunk_id="chunk-000", paragraphs=tuple(paras), est_tokens=10)


def _rewritten(**id_to_corrected) -> ProviderResult:
    return ProviderResult(parsed={"paragraphs": [
        {"id": k, "corrected": v} for k, v in id_to_corrected.items()]})


def _verdicts(*items) -> ProviderResult:
    return ProviderResult(parsed={"verdicts": list(items)})


# --- diffing ------------------------------------------------------------------

def test_diff_keeps_a_minimal_fix():
    p = _para("body-0", "He holds out hand for the bottle.")
    cands = _diff_candidates(p, "He holds out a hand for the bottle.",
                             max_add=24, max_span=48)
    assert len(cands) == 1
    assert cands[0].replacement.strip() == "a"


def test_diff_drops_a_pure_whitespace_change():
    p = _para("body-0", "The end. ")            # trailing space
    assert _diff_candidates(p, "The end.", max_add=24, max_span=48) == []


def test_diff_drops_a_large_single_diff():
    # The per-diff size guard catches a single oversized replacement (a wholesale
    # re-type); a paraphrase that splits into several small diffs is left for the
    # skeptical confirm step, which is the real over-correction filter.
    p = _para("body-0", "I agree.")
    big = "I agree completely and without the slightest possible reservation."
    assert _diff_candidates(p, big, max_add=24, max_span=48) == []


def test_diff_returns_nothing_when_unchanged():
    p = _para("body-0", "Nothing at all is wrong here.")
    assert _diff_candidates(p, p.text, max_add=24, max_span=48) == []


# --- propose ------------------------------------------------------------------

def test_propose_diffs_a_rewritten_chunk():
    p = _para("body-0", "He recieved the letter.")
    prov = FakeProvider([_rewritten(**{"body-0": "He received the letter."})])
    cands = propose([_chunk(p)], prov, model="m", max_tokens=100,
                    usage=Usage(), workers=1)
    assert len(cands) == 1 and cands[0].para_id == "body-0"


def test_propose_ignores_a_hallucinated_paragraph_id():
    p = _para("body-0", "All correct here.")
    prov = FakeProvider([_rewritten(**{"body-9": "Some other text entirely."})])
    assert propose([_chunk(p)], prov, model="m", max_tokens=100,
                   usage=Usage(), workers=1) == []


def test_propose_unions_independent_samples():
    # Two retypes of the same chunk catch different errors; the union carries
    # both (one candidate per distinct span), so recall is additive.
    p = _para("body-0", "He holds out hand for teh bottle.")
    prov = FakeProvider([
        _rewritten(**{"body-0": "He holds out a hand for teh bottle."}),  # +"a"
        _rewritten(**{"body-0": "He holds out hand for the bottle."}),    # teh->the
    ])
    cands = propose([_chunk(p)], prov, model="m", max_tokens=100,
                    usage=Usage(), workers=1, samples=2)
    assert len(cands) == 2
    assert len({(c.start, c.end) for c in cands}) == 2      # two different sites


def test_dedup_candidates_unions_by_span_and_replacement():
    from docproof.rewrite import dedup_candidates
    a = RewriteCandidate("body-0", 3, 11, "recieved", "received")
    a2 = RewriteCandidate("body-0", 3, 11, "recieved", "received")   # exact dup
    b = RewriteCandidate("body-0", 3, 11, "recieved", "receive")     # same span, other fix
    c = RewriteCandidate("body-0", 20, 23, "teh", "the")             # other span
    out = dedup_candidates([a, a2, b, c])
    assert len(out) == 3                             # a2 collapses into a
    assert a in out and b in out and c in out


# --- confirm (the valve) ------------------------------------------------------

def test_a_high_confidence_affirmation_becomes_an_edit():
    p = _para("body-0", "He holds out hand for the bottle.")
    cand = RewriteCandidate("body-0", 13, 13, "", "a ")
    prov = FakeProvider([_verdicts(
        {"index": 1, "is_error": True, "confidence": "high"})])
    out = confirm([cand], [p], prov, model="m", max_tokens=100,
                  usage=Usage(), ids=itertools.count(1))
    assert len(out) == 1
    assert out[0].error_type == "rewrite" and out[0].force_query is False
    assert out[0].corrected_text == "He holds out a hand for the bottle."


def test_a_soft_affirmation_is_a_query_not_an_edit():
    p = _para("body-0", "advantages to being a bastard")
    cand = RewriteCandidate("body-0", 11, 13, "to", "of")
    prov = FakeProvider([_verdicts(
        {"index": 1, "is_error": True, "confidence": "medium"})])
    out = confirm([cand], [p], prov, model="m", max_tokens=100,
                  usage=Usage(), ids=itertools.count(1))
    assert len(out) == 1 and out[0].force_query is True


def test_a_kept_original_produces_nothing():
    # An in-voice misspelling in a ransom note the rewriter tried to "fix".
    p = _para("body-0", "Leeve the muny by the oek tree.")
    cand = RewriteCandidate("body-0", 0, 5, "Leeve", "Leave")
    prov = FakeProvider([_verdicts(
        {"index": 1, "is_error": False, "confidence": "high"})])
    assert confirm([cand], [p], prov, model="m", max_tokens=100,
                   usage=Usage(), ids=itertools.count(1)) == []


def test_edit_confidence_floor_demands_the_surest_edits():
    p = _para("body-0", "He holds out hand for the bottle.")
    cand = RewriteCandidate("body-0", 13, 13, "", "a ")
    prov = FakeProvider([_verdicts(
        {"index": 1, "is_error": True, "confidence": "medium"})])
    out = confirm([cand], [p], prov, model="m", max_tokens=100, usage=Usage(),
                  ids=itertools.count(1), edit_confidence="high")
    assert out[0].force_query is True         # medium < high floor -> a query


# --- batching: propose rides the batch when its model matches the review's ----

def test_candidates_from_result_diffs_a_parsed_payload():
    from docproof.rewrite import candidates_from_result
    ch = _chunk(_para("body-0", "He recieved it."))
    cands = candidates_from_result(
        ch, {"paragraphs": [{"id": "body-0", "corrected": "He received it."}]},
        max_add=24, max_span=48)
    assert len(cands) == 1 and cands[0].para_id == "body-0"
    assert candidates_from_result(ch, {"nope": True}) == []   # bad payload
    assert candidates_from_result(ch, None) == []             # no result


def _prepared(texts, tmp_path):
    import docx
    from docproof.config import load_config
    from docproof.pipeline import prepare
    d = docx.Document()
    for t in texts:
        d.add_paragraph(t)
    path = tmp_path / "doc.docx"
    d.save(str(path))
    cfg = load_config("config/default.yaml")
    cfg.error_types = ["comma_splice"]
    return cfg, prepare(cfg, str(path), "config/error_types")


def test_rewrite_batched_predicate(tmp_path):
    from docproof.batch import rewrite_batched
    cfg, _ = _prepared(["An ordinary reviewable sentence goes here."], tmp_path)
    cfg.rewrite.enabled = True
    cfg.rewrite.model = None                       # = api.model
    assert rewrite_batched(cfg) is True
    cfg.rewrite.model = cfg.api.model
    assert rewrite_batched(cfg) is True
    cfg.rewrite.model = "claude-opus-5"            # a different (valid) model
    assert rewrite_batched(cfg) is False           # cannot share one batch
    cfg.rewrite.enabled = False
    assert rewrite_batched(cfg) is False


def test_build_requests_adds_a_rewrite_request_per_chunk(tmp_path):
    from docproof.batch import REWRITE_PREFIX, build_requests
    cfg, prep = _prepared(["An ordinary reviewable sentence goes here."],
                          tmp_path)
    n_chunks = len(prep.chunks)

    cfg.rewrite.enabled = False
    assert not [r for r in build_requests(cfg, prep)
                if r.custom_id.startswith(REWRITE_PREFIX)]

    cfg.rewrite.enabled = True
    cfg.rewrite.model = None                       # batchable (= api.model)
    rw = [r for r in build_requests(cfg, prep)
          if r.custom_id.startswith(REWRITE_PREFIX)]
    assert len(rw) == n_chunks and rw[0].schema_name == "rewritten"

    cfg.rewrite.model = "claude-opus-5"            # not batchable -> stays sync
    assert not [r for r in build_requests(cfg, prep)
                if r.custom_id.startswith(REWRITE_PREFIX)]


def test_build_requests_multiplies_rewrite_requests_by_samples(tmp_path):
    from docproof.batch import REWRITE_PREFIX, build_requests
    cfg, prep = _prepared(["An ordinary reviewable sentence goes here."],
                          tmp_path)
    n_chunks = len(prep.chunks)
    cfg.rewrite.enabled = True
    cfg.rewrite.model = None
    cfg.rewrite.samples = 3
    rw = [r for r in build_requests(cfg, prep)
          if r.custom_id.startswith(REWRITE_PREFIX)]
    assert len(rw) == 3 * n_chunks
    assert len({r.custom_id for r in rw}) == 3 * n_chunks    # distinct ids per sample
