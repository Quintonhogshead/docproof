"""Cancellation bills completed requests and retains their checkpoint receipts."""
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import pytest

from docproof.checkpoint import Checkpoint
from docproof.config import Config
from docproof.models import Usage
from docproof.pipeline import JobCancelled, _ckpt_key, run_sync
from docproof.providers import NormalizedUsage, ProviderResult


TOKENS = NormalizedUsage(input_tokens=100, output_tokens=20)


def _setup(monkeypatch, fetch, count):
    import docproof.pipeline as pipeline

    cfg = Config()
    cfg.api.concurrency = 2
    cfg.api.concurrency_by_provider = {}
    chunks = [SimpleNamespace(chunk_id=f"chunk-{i:03d}") for i in range(count)]
    prepared = SimpleNamespace(
        pass_types=[], vocabulary="", conventions="", story_sheet="",
        examination=None,
        effective_pass_plan=[SimpleNamespace(index=0, chunks=chunks)],
        request_count=count)

    class Detector:
        label = "spelling"

        def fetch(self, chunk):
            return fetch(chunk)

        def process_result(self, result, chunk, usage, **kwargs):
            usage.add(result.usage, model=cfg.api.model)
            return [], result.stop_reason == "ok"

    monkeypatch.setattr(pipeline, "build_analyzers", lambda *a, **k: [Detector()])
    return cfg, prepared, chunks


@pytest.mark.parametrize("stop_reason", ["ok", "refusal", "max_tokens"])
def test_cancel_drains_detector_calls_without_starting_queue_or_retries(
        tmp_path, monkeypatch, stop_reason):
    entered = Barrier(2)
    cancelled = Event()
    lock = Lock()
    called = []

    def fetch(chunk):
        with lock:
            called.append(chunk.chunk_id)
        entered.wait(timeout=5)
        cancelled.set()
        return ProviderResult(parsed={"findings": []}, usage=TOKENS,
                              stop_reason=stop_reason)

    cfg, prepared, chunks = _setup(monkeypatch, fetch, 6)
    checkpoint = Checkpoint(tmp_path / "checkpoint.json", fingerprint={"test": 1})
    receipts = {}
    with pytest.raises(JobCancelled) as stopped:
        run_sync(cfg, prepared, object(), checkpoint=checkpoint,
                 should_cancel=cancelled.is_set,
                 on_checkpoint_usage=lambda key, receipt: receipts.update({key: receipt}))
    assert len(called) == 2
    usage = stopped.value.usage
    assert usage.api_calls == 2 and usage.input_tokens == 200
    assert len(receipts) == 2
    assert sum(receipt["api_calls"] for receipt in receipts.values()) == 2
    for chunk in chunks[:2]:
        key = _ckpt_key(0, chunk.chunk_id, 0)
        receipt = checkpoint.get(key)
        if stop_reason == "ok":
            assert receipt is not None and receipt.usage["api_calls"] == 1
        else:
            assert receipt is None and checkpoint.burned(key)["api_calls"] == 1
    assert checkpoint.get(_ckpt_key(0, chunks[2].chunk_id, 0)) is None
    assert checkpoint.burned(_ckpt_key(0, chunks[2].chunk_id, 0)) is None


def test_cancel_drain_does_not_recount_replayed_or_already_folded_results(
        tmp_path, monkeypatch):
    both_started = Barrier(2)
    release_second = Event()
    cancelled = Event()
    called = []

    def fetch(chunk):
        called.append(chunk.chunk_id)
        both_started.wait(timeout=5)
        if chunk.chunk_id == "chunk-002":
            assert release_second.wait(timeout=5)
        return ProviderResult(parsed={"findings": []}, usage=TOKENS)

    cfg, prepared, chunks = _setup(monkeypatch, fetch, 3)
    checkpoint = Checkpoint(tmp_path / "checkpoint.json", fingerprint={"test": 2})
    checkpoint.put(_ckpt_key(0, chunks[0].chunk_id, 0), items=[], ok=True,
                   usage=Usage(input_tokens=100, output_tokens=20, api_calls=1))

    def progress(done, total):
        if done == 2:            # cached result and first fresh result are folded
            cancelled.set()
            release_second.set()

    receipts = []
    with pytest.raises(JobCancelled) as stopped:
        run_sync(cfg, prepared, object(), checkpoint=checkpoint, progress=progress,
                 should_cancel=cancelled.is_set,
                 on_checkpoint_usage=lambda key, receipt: receipts.append((key, receipt)))
    assert sorted(called) == ["chunk-001", "chunk-002"]
    assert stopped.value.usage.api_calls == 3
    assert stopped.value.usage.input_tokens == 300
    assert len(receipts) == 3 and len({key for key, _ in receipts}) == 3
    assert sum(receipt["api_calls"] for _, receipt in receipts) == 3
    for chunk in chunks:
        assert checkpoint.get(_ckpt_key(0, chunk.chunk_id, 0)).usage["api_calls"] == 1
