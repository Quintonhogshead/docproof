"""The chapter-continuity propose stage riding its own batch.

submit builds a SECOND batch on the chapter model (a continuity-only run has no
review batch to ride, and the reader is usually a different model — a batch is
one model); poll waits for both; collect fetches the reads and hands them to
finish, where the judge still runs synchronously. And the backward-compat that
keeps in-flight jobs collecting: a manifest with no continuity batch loads and
simply has none.
"""
from __future__ import annotations

from docproof import batch as batchlib
from docproof.config import load_config
from docproof.continuity import chapter_read_custom_id
from docproof.providers import BatchStatus, ProviderResult
from .conftest import FIXTURES
from .test_error_types import ERROR_DIR

CONFIG = FIXTURES.parent.parent / "config" / "default.yaml"


class MultiBatchProvider:
    """A batch fake that keeps EACH submitted batch distinct — the shared
    single-batch fake overwrites its one id, which cannot model a job that holds
    two. Maps each batch id to the custom_ids it carried, and each custom_id to a
    scripted result (empty findings by default)."""
    name = "fake"

    def __init__(self, results_by_custom_id=None):
        self.results = dict(results_by_custom_id or {})
        self.batches: dict[str, list[str]] = {}      # batch_id -> custom_ids
        self.submits: list[tuple[str, list[str]]] = []
        self._n = 0

    def submit_batch(self, *, model, requests, max_tokens):
        self._n += 1
        bid = f"batch-{self._n}"
        cids = [r.custom_id for r in requests]
        self.batches[bid] = cids
        self.submits.append((model, cids))
        return bid

    def poll_batch(self, batch_id):
        n = len(self.batches.get(batch_id, []))
        return BatchStatus(state="completed", total=n, succeeded=n)

    def collect_batch(self, batch_id):
        empty = ProviderResult(parsed={"findings": []})
        return {cid: self.results.get(cid, empty)
                for cid in self.batches.get(batch_id, [])}

    def complete_structured(self, **kw):             # unused; nothing goes sync here
        return ProviderResult(parsed={"findings": []})


def _cfg(**over):
    """Only the comma-splice detector and chapter continuity are live, so no other
    collect-time pass reaches for a provider. The chapter model matches the review
    model, so both batches ride this one fake."""
    cfg = load_config(CONFIG)
    cfg.error_types = ["comma_splice"]
    cfg.sweeps = []
    for name in ("storysheet", "glossary", "adjudicate", "rewrite", "languagetool",
                 "sapling", "consistency", "spellcheck", "continuity", "smoothing",
                 "meaning_check", "fix_check"):
        sub = getattr(cfg, name, None)
        if sub is not None and hasattr(sub, "enabled"):
            sub.enabled = False
    cfg.low_confidence.confirm = False
    cfg.ensemble.detectors = []
    cfg.chapter_continuity.enabled = True
    # Same model AND effort as the review, so _cc_provider reuses this one fake
    # provider for the chapter batch rather than building a real one.
    cfg.chapter_continuity.model = cfg.api.model
    cfg.chapter_continuity.effort = cfg.api.effort
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def test_submit_makes_a_second_batch_for_the_chapter_reads(tmp_path):
    prov = MultiBatchProvider()
    job = batchlib.submit(_cfg(), FIXTURES / "simple.docx", ERROR_DIR, prov, tmp_path)
    assert job.batch_id and job.continuity_batch_id
    assert job.batch_id != job.continuity_batch_id           # two distinct batches
    review_cids = prov.batches[job.batch_id]
    cc_cids = prov.batches[job.continuity_batch_id]
    assert review_cids and all(c.startswith("p") for c in review_cids)
    assert cc_cids and all(c.startswith("cc-") for c in cc_cids)
    assert job.request_count == len(review_cids) + len(cc_cids)


def test_continuity_only_has_no_review_batch(tmp_path):
    prov = MultiBatchProvider()
    job = batchlib.submit(_cfg(error_types=[]), FIXTURES / "simple.docx",
                          ERROR_DIR, prov, tmp_path)
    assert job.batch_id == ""                                # nothing to review
    assert job.continuity_batch_id                           # the whole run is chapters
    assert len(prov.submits) == 1                            # exactly one batch went out
    assert all(c.startswith("cc-") for c in prov.batches[job.continuity_batch_id])


def test_poll_waits_for_both_and_collect_fetches_the_reads(tmp_path):
    # a scripted break on the first chapter's read, so the collected dict is real
    prov = MultiBatchProvider({
        chapter_read_custom_id(0): ProviderResult(parsed={"findings": []})})
    job = batchlib.submit(_cfg(error_types=[]), FIXTURES / "simple.docx",
                          ERROR_DIR, prov, tmp_path)
    status = batchlib.poll(job, prov, tmp_path)
    assert status.state == "completed" and job.state == "ready"
    r = batchlib.collect_findings(job, prov, ERROR_DIR)
    assert r.chapter_reads is not None
    assert chapter_read_custom_id(0) in r.chapter_reads


def test_collect_hands_the_chapter_reads_to_finish_exactly_once(tmp_path, monkeypatch):
    """The headline path end to end: collect() -> finish() runs the chapter pass
    ONCE, with the batch reads (a non-None dict) — so the reads are parsed, not
    re-bought — and collect_findings alone never runs it."""
    import docproof.pipeline as pipe
    from docproof.continuity import ChapterContinuityReport
    prov = MultiBatchProvider({
        chapter_read_custom_id(0): ProviderResult(parsed={"findings": []})})
    job = batchlib.submit(_cfg(error_types=[]), FIXTURES / "simple.docx",
                          ERROR_DIR, prov, tmp_path)
    batchlib.poll(job, prov, tmp_path)

    seen = {"calls": 0, "batch_reads": "unset"}

    def spy(cfg, prepared, usage=None, **kw):
        seen["calls"] += 1
        seen["batch_reads"] = kw.get("batch_reads")
        return [], ChapterContinuityReport()
    monkeypatch.setattr(pipe, "_chapter_continuity_findings", spy)

    # collect_findings on its own must NOT run the pass (it runs in finish).
    batchlib.collect_findings(job, prov, ERROR_DIR)
    assert seen["calls"] == 0

    batchlib.collect(job, prov, ERROR_DIR, tmp_path, out_dir=tmp_path / "out")
    assert seen["calls"] == 1
    assert isinstance(seen["batch_reads"], dict)          # the batch path, not a re-buy
    assert chapter_read_custom_id(0) in seen["batch_reads"]


def test_poll_holds_when_a_batch_is_still_running(tmp_path):
    """A job is ready only when EVERY batch it holds has completed."""
    prov = MultiBatchProvider()
    job = batchlib.submit(_cfg(), FIXTURES / "simple.docx", ERROR_DIR, prov, tmp_path)

    # Make the chapter batch still in-progress; the review batch is complete.
    real_poll = prov.poll_batch
    def one_pending(batch_id):
        if batch_id == job.continuity_batch_id:
            return BatchStatus(state="in_progress",
                               total=len(prov.batches[batch_id]))
        return real_poll(batch_id)
    prov.poll_batch = one_pending
    status = batchlib.poll(job, prov, tmp_path)
    assert status.state == "in_progress" and job.state == "in_progress"


def test_combine_status_is_failed_completed_or_in_progress():
    from docproof.batch import _combine_status
    ok = BatchStatus(state="completed", total=3, succeeded=3)
    running = BatchStatus(state="in_progress", total=2)
    bad = BatchStatus(state="failed", total=1)
    assert _combine_status([ok, ok]).state == "completed"
    assert _combine_status([ok, ok]).total == 6            # totals sum across batches
    assert _combine_status([ok, running]).state == "in_progress"
    assert _combine_status([ok, bad]).state == "failed"    # any failure fails the job


def test_the_chapter_batch_carries_the_chapter_effort(monkeypatch):
    """The reads are batched at chapter_continuity.effort, NOT the review effort —
    effort is baked into every request body at provider construction, so a miss
    reads the whole book at the wrong depth (the sync path sets it explicitly)."""
    from docproof import batch as b
    captured = {}
    def fake_build(cfg, **kw):
        captured["model"], captured["effort"] = cfg.api.model, cfg.api.effort
        return object()
    import docproof.providers as _p
    monkeypatch.setattr(_p, "build_provider", fake_build)
    cfg = load_config(CONFIG)
    cfg.chapter_continuity.model = "gpt-5.6-sol"       # differs from the review model
    cfg.chapter_continuity.effort = "high"
    cfg.api.effort = "low"
    review_provider = object()
    prov = b._cc_provider(cfg, review_provider)
    assert prov is not review_provider                 # not reused: model/effort differ
    assert captured == {"model": "gpt-5.6-sol", "effort": "high"}


def test_the_chapter_batch_reuses_the_review_provider_only_on_a_full_match():
    from docproof import batch as b
    cfg = load_config(CONFIG)
    cfg.chapter_continuity.model = cfg.api.model
    cfg.chapter_continuity.effort = cfg.api.effort
    review = object()
    assert b._cc_provider(cfg, review) is review       # model AND effort match: reuse


def test_rounds_force_the_chapter_batch_off_per_round_but_not_overall():
    """The load-bearing multi-round change: every per-round config has chapter
    continuity OFF (so no per-round chapter batch is bought), while the original
    cfg is untouched so the final finish still runs it once."""
    from docproof.rounds import _round_config
    cfg = load_config(CONFIG)
    cfg.chapter_continuity.enabled = True
    assert _round_config(cfg, 1, True).chapter_continuity.enabled is False
    assert _round_config(cfg, 2, True).chapter_continuity.enabled is False
    assert _round_config(cfg, 1, False).chapter_continuity.enabled is False
    assert cfg.chapter_continuity.enabled is True      # no mutation leaked back


def test_a_failed_chapter_batch_does_not_discard_a_completed_review(tmp_path):
    """The pass is best-effort: a chapter-batch failure must not fail a job whose
    review batch completed. The job goes ready and collect reports the reads as
    failed."""
    prov = MultiBatchProvider()
    job = batchlib.submit(_cfg(), FIXTURES / "simple.docx", ERROR_DIR, prov, tmp_path)

    def split_poll(batch_id):
        if batch_id == job.continuity_batch_id:
            return BatchStatus(state="failed", total=1)
        n = len(prov.batches[batch_id])
        return BatchStatus(state="completed", total=n, succeeded=n)
    prov.poll_batch = split_poll
    batchlib.poll(job, prov, tmp_path)
    assert job.state == "ready"                         # review kept, chapter dropped


def test_a_failed_chapter_batch_fails_a_continuity_only_job(tmp_path):
    """When the chapter batch IS the whole run (continuity-only), its failure is
    the job's failure — there is no review to fall back on."""
    prov = MultiBatchProvider()
    job = batchlib.submit(_cfg(error_types=[]), FIXTURES / "simple.docx",
                          ERROR_DIR, prov, tmp_path)
    prov.poll_batch = lambda bid: BatchStatus(state="failed", total=1)
    batchlib.poll(job, prov, tmp_path)
    assert job.state == "failed"


def test_an_old_manifest_without_a_continuity_batch_still_loads(tmp_path):
    """The whole point of a defaulted field and no MANIFEST_VERSION bump: a job
    submitted before this change loads and collects with no second batch."""
    prov = MultiBatchProvider()
    job = batchlib.submit(_cfg(), FIXTURES / "simple.docx", ERROR_DIR, prov, tmp_path)
    # Rewrite the manifest as a pre-change one: strip the new key entirely.
    import json
    manifest = batchlib.job_dir(tmp_path, job.job_id) / batchlib.MANIFEST_NAME
    data = json.loads(manifest.read_text())
    data.pop("continuity_batch_id")
    manifest.write_text(json.dumps(data))
    reloaded = batchlib.load(tmp_path, job.job_id)
    assert reloaded.continuity_batch_id is None            # restored as None, not an error
