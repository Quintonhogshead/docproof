"""Ordered fan-out: the ladder's concurrency pattern, extracted once.

Every paid loop in the pipeline that fans out — the typed-pass ladder
(pipeline.run_sync), adjudicate, the judges, flights, repair, continuity —
does the same four things: submit every call to a ThreadPoolExecutor sized by
`cfg.concurrency_for(model)`, fold the results back IN SUBMISSION ORDER on
the calling thread (ids, usage, and checkpoints all depend on that order),
cancel every unstarted call when the folding thread raises, and stay
strictly sequential at width 1. The Galley delivery loops — the chapter
sweep's windows, the change verifier's batches, the finished-text walk's
reads, settle's judge calls — were written as plain for-loops instead, one
Luna call at a time: on Georgis (2026-09-04) that was 7 chapter-sweep
windows in over 50 minutes, ~95 verify calls in ~5 minutes and 100+ settle
calls in 10+ minutes, every one of which would have fanned out to about a
minute under the concurrency the ladder already runs at.

`fan_out` is that pattern as one function so those loops can adopt it
without a fourth copy of the pool code. The limiter is still the config's
(`api.concurrency` / `api.concurrency_by_provider` through
`Config.concurrency_for`); this module only spends what it is handed.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, Iterator, TypeVar

from .models import Usage

T = TypeVar("T")
R = TypeVar("R")


def fan_out(items: Iterable[T], fetch: Callable[[T], R], *,
            concurrency: int = 1) -> Iterator[tuple[T, R]]:
    """Yield `(item, fetch(item))` for every item, in ITEM ORDER, with up to
    `concurrency` calls in flight.

    Width 1 is the plain sequential loop — no pool, no thread, each call
    made only when its turn to be folded comes — so a caller's behaviour at
    concurrency 1 is byte-identical to the loop it replaced. Above 1, every
    call is submitted up front and results are folded in order as they are
    awaited; a fetch that raises propagates from the folding thread, and
    every call not yet started is cancelled first (the pool would otherwise
    drain its queue on the way out and keep buying the rest of the book).
    The same cancellation runs when the consumer stops early (a ceiling
    reached mid-book): calls in flight finish, unstarted ones do not.

    `fetch` runs on a worker thread: it must not touch shared mutable state
    (a `Usage`, a module-level list); return what the caller needs and fold
    it here, on the calling thread."""
    items = list(items)
    if concurrency <= 1:
        for item in items:
            yield item, fetch(item)
        return
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        pending = [(item, pool.submit(fetch, item)) for item in items]
        try:
            for item, future in pending:
                yield item, future.result()
        finally:
            for _item, unstarted in pending:
                unstarted.cancel()


def fold_usage(into: Usage, other: Usage) -> None:
    """Add every counter of `other` into `into`, per-model buckets included —
    how a worker thread's private Usage reaches the caller's, on the calling
    thread (Usage.add is not thread-safe)."""
    for f in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens", "api_calls", "sapling_chars"):
        setattr(into, f, getattr(into, f) + getattr(other, f))
    into.sapling_cost += other.sapling_cost
    for model, bucket in other.by_model.items():
        dst = into.by_model.setdefault(model, {"api_calls": 0})
        for k, v in bucket.items():
            dst[k] = dst.get(k, 0) + v


__all__ = ["fan_out", "fold_usage"]
