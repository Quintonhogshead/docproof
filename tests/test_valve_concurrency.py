"""The confirm and adjudication valves fan their windows out over a pool, and
must come back byte-for-byte identical to the old strictly-serial run.

These valves emit tracked changes, so their order is not cosmetic. The validator
gives the earliest finding to claim a span the right to it, the finding ids come
off one shared counter, and `reject_sink` is written to disk for a recall compare
to read — so a window folded out of turn changes which correction ships, what it
is called, and what the eval sees. Every fake here is content-addressed and
sleeps in reverse of submission order, the trick test_concurrency.py uses on the
detector loop: completions arrive scrambled, so an in-order fold is genuinely
exercised rather than accidentally observed.
"""
from __future__ import annotations

import time
from itertools import count

from docproof.adjudicate import Candidate, adjudicate
from docproof.models import ParagraphRef, Usage
from docproof.providers import NormalizedUsage, ProviderResult
from docproof.rewrite import RewriteCandidate, confirm

USAGE = NormalizedUsage(input_tokens=100, output_tokens=20,
                        cache_read_input_tokens=50)

WINDOW = 4          # small, so a handful of candidates make several windows
WINDOWS = 5


def _paragraphs(n: int) -> list[ParagraphRef]:
    """One paragraph per candidate, each holding a word unique to its index."""
    return [ParagraphRef(para_id=f"p-{i:03d}", part="word/document.xml",
                         location="body", style="Normal",
                         text=f"The wordx{i:03d} sat alone in this sentence.")
            for i in range(n)]


class _Scrambling:
    """Content-addressed, not call-order-addressed.

    It reads the wordxNNN tokens out of the window it was handed and answers
    about those, so a verdict is always matched to the right window however the
    fetches interleave. The sleep is longest for the window whose first index is
    lowest, so earlier windows finish last."""
    name = "fake"

    def __init__(self, verdict_for):
        self.verdict_for = verdict_for
        self.submitted: list[int] = []
        self.finished: list[int] = []

    def complete_structured(self, *, user, **kwargs) -> ProviderResult:
        import re
        idx = [int(m) for m in re.findall(r"wordx(\d+)", user)]
        # De-duplicate while keeping order: adjudicate names the word once and
        # then again inside the sentence it quotes.
        ordered = list(dict.fromkeys(idx))
        window_no = ordered[0] // WINDOW
        self.submitted.append(window_no)
        time.sleep(0.02 * (WINDOWS - window_no))
        self.finished.append(window_no)
        return ProviderResult(
            parsed={"verdicts": [self.verdict_for(n, i)
                                 for n, i in enumerate(ordered, 1)]},
            usage=USAGE)


def _adjudicate(concurrency: int):
    n = WINDOW * WINDOWS
    paras = _paragraphs(n)
    cands = [Candidate(para_id=p.para_id, word=f"wordx{i:03d}", start=4,
                       end=4 + len(f"wordx{i:03d}"),
                       suggestion=f"fixedx{i:03d}", kind="typo")
             for i, p in enumerate(paras)]
    prov = _Scrambling(lambda n_, i: {
        "index": n_, "is_error": True, "correction": f"fixedx{i:03d}",
        "confidence": "high"})
    usage = Usage()
    found = adjudicate(cands, paras, prov, model="m", max_tokens=100,
                       usage=usage, ids=count(1), batch_size=WINDOW,
                       concurrency=concurrency)
    return found, usage, prov


def test_adjudicate_fans_out_without_changing_the_result():
    serial, serial_usage, _ = _adjudicate(1)
    pooled, pooled_usage, prov = _adjudicate(8)

    assert len(prov.submitted) == WINDOWS, "the fan-out must window the same way"
    # If the windows had come back in the order they were asked for, this test
    # would prove nothing about the fold. They must not.
    assert prov.finished != prov.submitted, (
        "the fixture did not actually scramble the completions, so the in-order "
        "fold is untested")
    assert [f.finding_id for f in serial] == [f.finding_id for f in pooled]
    assert [f.para_id for f in serial] == [f.para_id for f in pooled]
    assert [f.corrected_text for f in serial] == [f.corrected_text for f in pooled]
    # The load-bearing assertion: which id landed on which paragraph. Ids come
    # off a shared counter in fold order, so a-0001 belongs to the first
    # paragraph of the first window only if the fold ran in window order. (The
    # id sequence alone proves nothing — it is monotonic by construction
    # whatever order the windows are folded in.)
    assert [(f.finding_id, f.para_id) for f in pooled] == [
        (f"a-{i + 1:04d}", f"p-{i:03d}") for i in range(WINDOW * WINDOWS)]
    assert pooled_usage.input_tokens == serial_usage.input_tokens
    assert pooled_usage.api_calls == serial_usage.api_calls == WINDOWS


def _confirm(concurrency: int, *, sink=None):
    n = WINDOW * WINDOWS
    paras = _paragraphs(n)
    cands = [RewriteCandidate(para_id=p.para_id, start=4,
                              end=4 + len(f"wordx{i:03d}"),
                              original=f"wordx{i:03d}",
                              replacement=f"fixedx{i:03d}")
             for i, p in enumerate(paras)]
    # Every third candidate is ruled not-an-error, so the reject log has
    # something to keep an order for.
    prov = _Scrambling(lambda n_, i: {
        "index": n_, "is_error": i % 3 != 0, "confidence": "high"})
    usage = Usage()
    found = confirm(cands, paras, prov, model="m", max_tokens=100, usage=usage,
                    ids=count(1), batch_size=WINDOW, reject_sink=sink,
                    concurrency=concurrency)
    return found, usage


def test_confirm_fans_out_without_changing_the_result():
    serial, serial_usage = _confirm(1)
    pooled, pooled_usage = _confirm(8)

    assert [f.finding_id for f in serial] == [f.finding_id for f in pooled]
    assert [f.para_id for f in serial] == [f.para_id for f in pooled]
    assert [f.corrected_text for f in serial] == [f.corrected_text for f in pooled]
    # Same pairing check as the adjudication valve: the id a finding carries is
    # decided by when it was folded, so this is what pins the fold to window
    # order rather than completion order.
    kept = [i for i in range(WINDOW * WINDOWS) if i % 3 != 0]
    assert [(f.finding_id, f.para_id) for f in pooled] == [
        (f"r-{n:04d}", f"p-{i:03d}") for n, i in enumerate(kept, 1)]
    assert pooled_usage.input_tokens == serial_usage.input_tokens
    assert pooled_usage.api_calls == serial_usage.api_calls == WINDOWS


def test_confirm_keeps_the_reject_log_in_document_order():
    """The reject sink is a persisted artifact a recall compare diffs, so its
    order has to be the document's, not whichever window answered first."""
    serial_sink: list = []
    _confirm(1, sink=serial_sink)
    pooled_sink: list = []
    _confirm(8, sink=pooled_sink)

    assert [r["para_id"] for r in serial_sink] == [r["para_id"] for r in pooled_sink]
    assert [r["para_id"] for r in pooled_sink] == sorted(
        r["para_id"] for r in pooled_sink)
    assert pooled_sink, "the fixture should have produced rejections to order"


class _Exploding:
    """Fails the first call and counts every call it is asked to make."""
    name = "fake"

    def __init__(self):
        self.calls = 0

    def complete_structured(self, **kwargs):
        self.calls += 1
        raise RuntimeError("provider fell over")


def test_a_failed_window_stops_the_run_paying_for_the_rest():
    """Every window is queued on the pool up front, and a pool drains its queue
    on the way out — so without cancelling the tail, one failure early in a
    novel would buy every remaining window anyway. The serial loop this replaced
    stopped at the first failure; so must this, at any width."""
    for concurrency in (1, 8):
        n = WINDOW * WINDOWS
        paras = _paragraphs(n)
        cands = [RewriteCandidate(para_id=p.para_id, start=4, end=9,
                                  original=f"wordx{i:03d}", replacement="fixed")
                 for i, p in enumerate(paras)]
        prov = _Exploding()
        try:
            confirm(cands, paras, prov, model="m", max_tokens=100,
                    usage=Usage(), ids=count(1), batch_size=WINDOW,
                    concurrency=concurrency)
        except RuntimeError:
            pass
        else:                                        # pragma: no cover
            raise AssertionError("the failure should have propagated")
        assert prov.calls < WINDOWS, (
            f"at concurrency={concurrency}, {prov.calls} of {WINDOWS} windows "
            f"were paid for after the first one failed")
