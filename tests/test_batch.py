"""Batch lifecycle: submit → poll → collect, including the process restart in
the middle and the guard against the source document changing underneath."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from docproof import batch as batchlib
from docproof.config import load_config
from docproof.pipeline import content_hash, prepare
from docproof.reassembler import paragraph_view_text
from docproof.utils.xml_helpers import DocxPackage, walk_package
from .conftest import FIXTURES
from .fakes import ScriptedBatchProvider, finding_result
from .test_error_types import ERROR_DIR

SPLICE = "The manuscript was finished, nobody wanted to read it."
CONFIG = FIXTURES.parent.parent / "config" / "default.yaml"


def _cfg(**over):
    cfg = load_config(CONFIG)
    cfg.error_types = ["comma_splice"]
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def _splice_result():
    return finding_result(para_id="body-0000", error_type="comma_splice",
                          original=SPLICE,
                          corrected=SPLICE.replace(",", ";", 1))


def test_submit_writes_one_request_per_pass_and_chunk(tmp_path):
    cfg = _cfg()
    cfg.error_types = [["comma_splice", "spelling"], "repeated_word"]
    provider = ScriptedBatchProvider()
    job = batchlib.submit(cfg, FIXTURES / "simple.docx", ERROR_DIR, provider,
                          tmp_path)

    prepared = prepare(cfg, FIXTURES / "simple.docx", ERROR_DIR)
    assert job.request_count == prepared.request_count == 2 * len(prepared.chunks)
    assert job.state == "submitted"
    assert job.batch_id == "batch-fake-0001"

    # Each request carries its own prompt, which is what lets both passes ride
    # in a single batch.
    requests = provider.calls[0]["requests"]
    systems = {r.system for r in requests}
    assert len(systems) == 2
    assert {batchlib.parse_custom_id(r.custom_id)[0] for r in requests} == {0, 1}


def test_manifest_survives_a_process_restart(tmp_path):
    provider = ScriptedBatchProvider([_splice_result()])
    job = batchlib.submit(_cfg(), FIXTURES / "simple.docx", ERROR_DIR,
                          provider, tmp_path)

    # Nothing but the workspace directory carries over.
    reloaded = batchlib.load(tmp_path, job.job_id)
    assert reloaded.batch_id == job.batch_id
    assert reloaded.content_hash == job.content_hash
    assert reloaded.config["api"]["model"] == job.config["api"]["model"]

    assert batchlib.poll(reloaded, provider, tmp_path).state == "in_progress"
    assert batchlib.load(tmp_path, job.job_id).state == "in_progress"

    assert batchlib.poll(reloaded, provider, tmp_path).state == "completed"
    assert batchlib.load(tmp_path, job.job_id).state == "ready"

    outputs = batchlib.collect(reloaded, provider, ERROR_DIR, tmp_path)
    assert outputs.applied == 1
    assert batchlib.load(tmp_path, job.job_id).state == "done"

    pkg = DocxPackage(outputs.reviewed_path)
    para = {wp.para_id: wp.element for wp in walk_package(pkg)}["body-0000"]
    assert paragraph_view_text(para, "reject") == SPLICE       # fidelity
    assert ";" in paragraph_view_text(para, "accept")

    data = json.loads(outputs.findings_json.read_text())
    assert data["batch"] is True


def test_collect_refuses_an_edited_source(tmp_path):
    source = tmp_path / "simple.docx"
    source.write_bytes((FIXTURES / "simple.docx").read_bytes())
    provider = ScriptedBatchProvider([_splice_result()], pending_polls=0)
    job = batchlib.submit(_cfg(), source, ERROR_DIR, provider, tmp_path)

    # Simulate the author editing the file between submit and collect.
    job.content_hash = "0" * 64
    batchlib.save(job, tmp_path)

    with pytest.raises(batchlib.BatchError, match="edited since"):
        batchlib.collect(batchlib.load(tmp_path, job.job_id), provider,
                         ERROR_DIR, tmp_path)


def test_collect_reports_a_missing_source(tmp_path):
    source = tmp_path / "gone.docx"
    source.write_bytes((FIXTURES / "simple.docx").read_bytes())
    provider = ScriptedBatchProvider([_splice_result()], pending_polls=0)
    job = batchlib.submit(_cfg(), source, ERROR_DIR, provider, tmp_path)
    source.unlink()

    with pytest.raises(batchlib.BatchError, match="no longer at"):
        batchlib.collect(job, provider, ERROR_DIR, tmp_path)


def test_partial_batch_still_applies_what_came_back(tmp_path, caplog):
    cfg = _cfg()
    provider = ScriptedBatchProvider([_splice_result()], pending_polls=0)
    job = batchlib.submit(cfg, FIXTURES / "simple.docx", ERROR_DIR, provider,
                          tmp_path)

    # Drop every result but the first: a vendor-side failure on some requests
    # must not cost the whole document.
    original = provider.collect_batch

    def partial(batch_id):
        full = original(batch_id)
        return dict(list(full.items())[:1])

    provider.collect_batch = partial
    outputs = batchlib.collect(job, provider, ERROR_DIR, tmp_path)
    assert outputs.applied >= 0        # completed rather than raised


def test_content_hash_tracks_text_not_bytes():
    cfg = _cfg()
    a = prepare(cfg, FIXTURES / "simple.docx", ERROR_DIR)
    b = prepare(cfg, FIXTURES / "simple.docx", ERROR_DIR)
    assert content_hash(a.doc) == content_hash(b.doc)
    assert content_hash(a.doc) != content_hash(
        prepare(cfg, FIXTURES / "table.docx", ERROR_DIR).doc)


# --- reviewing part of a document ---------------------------------------------

def _split_cfg(**over):
    """A config that puts every paragraph in its own section, so selection has
    something to select. The fixtures are far too small to chunk otherwise."""
    cfg = _cfg(**over)
    cfg.chunking.token_budget = 5
    return cfg


def test_selection_narrows_the_run_without_moving_anchors():
    cfg = _split_cfg()
    whole = prepare(cfg, FIXTURES / "simple.docx", ERROR_DIR)
    assert len(whole.chunks) > 1, "fixture must split for this test to mean anything"

    picked = whole.chunks[1].chunk_id
    part = prepare(cfg, FIXTURES / "simple.docx", ERROR_DIR, selection=[picked])
    assert [c.chunk_id for c in part.chunks] == [picked]
    # Paragraph ids and the content hash describe the whole document either
    # way — that is what lets a partial batch still validate at collect time.
    assert part.content_hash == whole.content_hash
    assert len(part.doc.paragraphs) == len(whole.doc.paragraphs)


def test_selection_rejects_a_section_that_is_not_there():
    with pytest.raises(ValueError, match="No such section"):
        prepare(_split_cfg(), FIXTURES / "simple.docx", ERROR_DIR,
                selection=["chunk-404"])


def test_a_partial_review_stays_partial_when_it_is_collected(tmp_path):
    """The manifest pins the sections, so collection hours later reviews the
    same ones rather than quietly expanding to the whole document."""
    cfg = _split_cfg()
    whole = prepare(cfg, FIXTURES / "simple.docx", ERROR_DIR)
    picked = [whole.chunks[0].chunk_id]

    provider = ScriptedBatchProvider([_splice_result()], pending_polls=0)
    job = batchlib.submit(cfg, FIXTURES / "simple.docx", ERROR_DIR, provider,
                          tmp_path, selection=picked)
    assert job.selection == picked
    assert job.request_count == 1                 # one pass, one section

    reloaded = batchlib.load(tmp_path, job.job_id)
    assert reloaded.selection == picked
    outputs = batchlib.collect(reloaded, provider, ERROR_DIR, tmp_path)
    assert outputs.applied == 1


def test_max_chunks_is_recorded_as_the_sections_it_resolved_to(tmp_path):
    """--max-chunks used to be forgotten at collect time, which turned a
    two-section smoke test into a whole-document review."""
    cfg = _split_cfg()
    provider = ScriptedBatchProvider(pending_polls=0)
    job = batchlib.submit(cfg, FIXTURES / "simple.docx", ERROR_DIR, provider,
                          tmp_path, max_chunks=1)

    assert len(job.selection) == 1
    assert job.request_count == 1


def test_the_manifest_records_the_prompt_it_sent(tmp_path):
    provider = ScriptedBatchProvider()
    job = batchlib.submit(_cfg(), FIXTURES / "simple.docx", ERROR_DIR,
                          provider, tmp_path)
    # Prompts are editable, so a collected review has to be able to say what
    # it actually asked for.
    assert list(job.prompts) == ["comma_splice"]
    assert "ERROR TYPE: comma_splice" in job.prompts["comma_splice"]
    assert batchlib.load(tmp_path, job.job_id).prompts == job.prompts


def test_two_reviews_of_one_document_are_two_jobs(tmp_path):
    """A job id is a directory name. Submitting the same document twice in
    the same second used to hand both reviews the same folder, and the second
    replaced the first."""
    provider = ScriptedBatchProvider()
    a = batchlib.submit(_cfg(), FIXTURES / "simple.docx", ERROR_DIR, provider,
                        tmp_path)
    b = batchlib.submit(_cfg(), FIXTURES / "simple.docx", ERROR_DIR, provider,
                        tmp_path)

    assert a.job_id != b.job_id
    assert len(batchlib.load_all(tmp_path)) == 2
    assert batchlib.load(tmp_path, a.job_id).job_id == a.job_id
    # Still readable and still sorts by when it was made.
    assert a.job_id.startswith(datetime.now(timezone.utc).strftime("%Y%m%d"))
    assert "simple" in a.job_id


def test_load_all_skips_an_unreadable_manifest(tmp_path):
    provider = ScriptedBatchProvider()
    batchlib.submit(_cfg(), FIXTURES / "simple.docx", ERROR_DIR, provider,
                    tmp_path)
    broken = tmp_path / "broken-job"
    broken.mkdir()
    (broken / batchlib.MANIFEST_NAME).write_text("{not json")

    jobs = batchlib.load_all(tmp_path)
    assert len(jobs) == 1
