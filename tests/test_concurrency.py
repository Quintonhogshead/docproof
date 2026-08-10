"""Concurrent fetch must not change the result: same findings, same order, same
ids, same token totals as the old strictly-serial run. Only faster."""
from __future__ import annotations

import re
import time
from pathlib import Path

import docx

from docproof.config import load_config
from docproof.models import Usage
from docproof.pipeline import prepare, run_sync
from docproof.providers import NormalizedUsage, ProviderResult

ROOT = Path(__file__).parent.parent
ERROR_DIR = ROOT / "config" / "error_types"
CONFIG = str(ROOT / "config" / "default.yaml")

USAGE = NormalizedUsage(input_tokens=100, output_tokens=20,
                        cache_read_input_tokens=50)


class ScramblingProvider:
    """Content-addressed (not call-order-addressed): returns a finding for the
    first paragraph in whatever chunk it is handed, so a result is always
    matched to the right chunk however the fetches interleave. Sleeps in reverse
    of submission order, so completions arrive scrambled and the in-order commit
    is actually exercised."""
    name = "fake"

    def __init__(self, total: int):
        self.total = total
        self.seen = 0
        self._lock_free_counter = iter(range(total))

    def complete_structured(self, *, user, **kwargs) -> ProviderResult:
        # The user turn now leads with a read-only <context> block; the
        # paragraph under review is the first one after it, so match there.
        body = user.split("</context>")[-1]
        pid = re.search(r'id="([^"]+)"', body).group(1)
        n = int(pid.split("-")[-1])
        time.sleep(0.02 * (self.total - n))     # earlier paragraphs finish later
        return ProviderResult(
            parsed={"findings": [{
                "para_id": pid, "error_type": "spelling",
                "original_text": "teh", "corrected_text": "the",
                "confidence": "high", "explanation": ""}]},
            usage=USAGE)


def _prepared(tmp_path):
    d = docx.Document()
    # Eight sentences, each its own paragraph, each with a misspelling to echo.
    for i in range(8):
        d.add_paragraph(f"Paragraph number {i} has teh word spelled wrong here.")
    src = tmp_path / "many.docx"
    d.save(src)
    cfg = load_config(CONFIG)
    cfg.error_types = [["spelling"]]
    cfg.chunking.token_budget = 1        # force one paragraph per chunk
    return cfg, prepare(cfg, str(src), ERROR_DIR)


def _run(cfg, prepared, concurrency):
    cfg.api.concurrency = concurrency
    findings, usage = run_sync(cfg, prepared, ScramblingProvider(
        len(prepared.chunks)))
    return findings, usage


def test_concurrent_matches_serial(tmp_path):
    cfg, prepared = _prepared(tmp_path)
    assert len(prepared.chunks) == 8, "expected one chunk per paragraph"

    serial, u1 = _run(cfg, prepared, 1)
    parallel, u8 = _run(cfg, prepared, 8)

    # Same findings, in the same document order, with the same ids.
    key = lambda fs: [(f.finding_id, f.para_id, f.corrected_text) for f in fs]
    assert key(serial) == key(parallel)
    # Ids are sequential in document order regardless of completion order.
    assert [f.finding_id for f in parallel] == [f"f-{i:04d}" for i in range(1, 9)]
    assert [f.para_id for f in parallel] == [f"body-{i:04d}" for i in range(8)]
    # Token totals identical.
    assert (u1.input_tokens, u1.output_tokens) == (u8.input_tokens,
                                                   u8.output_tokens)


def test_concurrency_is_actually_faster(tmp_path):
    cfg, prepared = _prepared(tmp_path)
    # Total serial sleep is 0.02 * (8+7+...+1) = 0.72s; concurrency should beat
    # that comfortably. A generous bound keeps this from being flaky on a busy
    # machine while still failing if the calls run serially.
    start = time.monotonic()
    _run(cfg, prepared, 8)
    assert time.monotonic() - start < 0.5
