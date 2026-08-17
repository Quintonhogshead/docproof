"""Per-category passes and token budgets.

Two knobs on an `error_types` entry: `passes` reads a category N times (the
validator dedups the overlapping findings), and `token_budget` chunks that one
category at its own size. The default entry forms — a bare key or a list — must
behave exactly as before, down to the batch custom ids, so a run built before
these knobs existed resumes and collects unchanged.
"""
from __future__ import annotations

import itertools

import pytest

from docproof import batch as batchlib
from docproof.chunker import chunk_document
from docproof.config import Config, _normalize_error_entry, load_config
from docproof.models import DocumentModel, ParagraphRef
from docproof.pipeline import prepare
from .conftest import FIXTURES
from .fakes import ScriptedBatchProvider
from .test_error_types import ERROR_DIR

CONFIG = FIXTURES.parent.parent / "config" / "default.yaml"


def _doc(*texts) -> DocumentModel:
    paras = tuple(ParagraphRef(f"body-{i:04d}", "word/document.xml", "body", t,
                               "Normal") for i, t in enumerate(texts))
    return DocumentModel(source_path="x.docx", paragraphs=paras)


def _cfg(error_types):
    cfg = load_config(CONFIG)
    cfg.error_types = error_types
    return cfg


# --- schema ------------------------------------------------------------------

def test_entry_forms_normalize():
    assert _normalize_error_entry("spelling").keys == ("spelling",)
    assert _normalize_error_entry(["a", "b"]).keys == ("a", "b")
    spec = _normalize_error_entry({"group": ["a", "b"], "passes": 3,
                                   "token_budget": 1800})
    assert spec == (("a", "b"), 3, 1800)


@pytest.mark.parametrize("bad", [
    {"group": ["a"], "passes": 0},          # passes must be >= 1
    {"group": ["a"], "passes": True},       # bool is not a count
    {"group": [], "passes": 1},             # empty group
    {"group": ["a"], "token_budget": 0},    # budget must be >= 1
    {"group": ["a"], "nope": 1},            # unknown key
])
def test_bad_entries_are_rejected(bad):
    with pytest.raises(Exception):
        Config.model_validate({"error_types": [bad]})


def test_a_key_still_cannot_appear_twice():
    # Repeats are `passes`, never the same key in two categories.
    with pytest.raises(Exception):
        Config.model_validate({"error_types": ["spelling", ["spelling"]]})


def test_specs_expose_passes_and_budget():
    cfg = _cfg([["a", "b"], {"group": ["c"], "passes": 2, "token_budget": 1800}])
    specs = cfg.error_type_specs
    assert [s.keys for s in specs] == [("a", "b"), ("c",)]
    assert [s.passes for s in specs] == [1, 2]
    assert [s.token_budget for s in specs] == [None, 1800]
    # keys/groups views are unchanged: each key once, in order.
    assert cfg.error_type_keys == ("a", "b", "c")


# --- chunker namespacing -----------------------------------------------------

def test_default_budget_keeps_bare_chunk_ids():
    doc = _doc("Mara opened the box.", "She looked inside it.")
    cfg = load_config(CONFIG)
    cfg.chunking.token_budget = 8
    ids = [c.chunk_id for c in chunk_document(doc, cfg)]
    assert ids == ["chunk-000", "chunk-001"]


def test_a_custom_budget_namespaces_its_ids():
    doc = _doc("Mara opened the box.", "She looked inside it.")
    cfg = load_config(CONFIG)
    ids = [c.chunk_id for c in
           chunk_document(doc, cfg, token_budget=8, id_prefix="b8-")]
    assert ids == ["b8-chunk-000", "b8-chunk-001"]


# --- prepare() builds the plan ----------------------------------------------

def test_default_config_plan_is_one_pass_per_group_sharing_chunks():
    cfg = _cfg([["comma_splice", "spelling"], "repeated_word"])
    prepared = prepare(cfg, FIXTURES / "simple.docx", ERROR_DIR)
    plan = prepared.pass_plan
    assert [p.index for p in plan] == [0, 1]
    # Both passes read the one default-budget set (same ids as prepared.chunks).
    default_ids = [c.chunk_id for c in prepared.chunks]
    assert all([c.chunk_id for c in p.chunks] == default_ids for p in plan)
    assert prepared.request_count == 2 * len(prepared.chunks)


def test_passes_repeat_the_same_category_with_distinct_indices():
    cfg = _cfg([{"group": ["comma_splice"], "passes": 3}])
    prepared = prepare(cfg, FIXTURES / "simple.docx", ERROR_DIR)
    plan = prepared.pass_plan
    assert [p.index for p in plan] == [0, 1, 2]
    # Same types and same chunk set on every read...
    keys = [tuple(t.key for t in p.types) for p in plan]
    assert keys == [("comma_splice",)] * 3
    assert all([c.chunk_id for c in p.chunks]
               == [c.chunk_id for c in prepared.chunks] for p in plan)
    # ...so the review runs three times over the document.
    assert prepared.request_count == 3 * len(prepared.chunks)


def test_a_custom_budget_gives_a_category_its_own_chunks():
    tight = _cfg([{"group": ["comma_splice"], "token_budget": 40}])
    prepared = prepare(tight, FIXTURES / "simple.docx", ERROR_DIR)
    only = prepared.pass_plan[0]
    # Its ids are namespaced and its chunk count reflects the tighter budget,
    # independent of the default set prepared.chunks still exposes.
    assert all(c.chunk_id.startswith("b40-") for c in only.chunks)
    assert prepared.request_count == len(only.chunks)


# --- the default path stays byte-identical -----------------------------------

def test_default_form_and_dict_form_agree_on_custom_ids():
    plain = _cfg([["comma_splice", "spelling"]])
    wrapped = _cfg([{"group": ["comma_splice", "spelling"]}])
    a = batchlib.build_requests(plain, prepare(plain, FIXTURES / "simple.docx",
                                               ERROR_DIR))
    b = batchlib.build_requests(wrapped, prepare(wrapped, FIXTURES / "simple.docx",
                                                 ERROR_DIR))
    assert [r.custom_id for r in a] == [r.custom_id for r in b]
    assert all(cid.startswith("p0-chunk-") for cid in
               [r.custom_id for r in a])


def test_repeated_category_emits_distinct_batch_requests():
    cfg = _cfg([{"group": ["comma_splice"], "passes": 2}])
    prepared = prepare(cfg, FIXTURES / "simple.docx", ERROR_DIR)
    reqs = batchlib.build_requests(cfg, prepared)
    ids = [r.custom_id for r in reqs]
    assert len(ids) == len(set(ids)) == 2 * len(prepared.chunks)
    # Two reads: pass 0 and pass 1, each over every chunk. Round-trips.
    passes = {batchlib.parse_custom_id(cid)[0] for cid in ids}
    assert passes == {0, 1}


def test_custom_budget_custom_ids_round_trip():
    cfg = _cfg([{"group": ["comma_splice"], "token_budget": 40}])
    prepared = prepare(cfg, FIXTURES / "simple.docx", ERROR_DIR)
    reqs = batchlib.build_requests(cfg, prepared)
    for r in reqs:
        pass_i, chunk_id = batchlib.parse_custom_id(r.custom_id)
        assert pass_i == 0
        assert chunk_id.startswith("b40-chunk-")


def test_submit_manifest_pins_every_pass_chunk_id(tmp_path):
    cfg = _cfg([{"group": ["comma_splice"], "token_budget": 40}])
    provider = ScriptedBatchProvider()
    job = batchlib.submit(cfg, FIXTURES / "simple.docx", ERROR_DIR, provider,
                          tmp_path)
    prepared = prepare(cfg, FIXTURES / "simple.docx", ERROR_DIR)
    # The manifest pins the union: the tight-budget pass ids AND the default set
    # the rewrite pass rides, so collect reproduces the same partial review.
    pinned = set(job.selection)
    assert {c.chunk_id for p in prepared.pass_plan for c in p.chunks} <= pinned
    assert {c.chunk_id for c in prepared.chunks} <= pinned
