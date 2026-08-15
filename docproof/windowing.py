"""Asking a structured question about a list of items without losing any.

Several passes share one shape: a list of candidates is cut into windows, each
window goes to the model as one numbered list, and the answers are folded back
in order. The failure this module exists to prevent is what that shape does when
a window does not come back whole.

TRUNCATION. A reasoning model spends most of its ceiling on thinking, so the
visible answer is the small part of what `max_tokens` has to cover — and the
ceiling is usually a number that was chosen against a non-reasoning model and
inherited. When the ceiling is hit, both vendors return `stop_reason` of
"max_tokens" with NOTHING parsed (see providers/*.py: both bail on the
incomplete body rather than parse half-written JSON). The old loop logged one
line and moved on, so a whole window of paid-for candidates vanished into
neither the findings nor the reject log, and the pass then reported "N
correction(s) from M candidate(s)" — which reads as restraint, not as an
outage. Measured on a real manuscript: the shipped 4,000-token confirm ceiling
needs ~4,400 tokens for 40 items at effort `medium` and ~8,100 at `high`, so
the default configuration truncated on real input.

PARTIAL ANSWERS. The general case of the same failure, and raising the ceiling
does not fix it. A response can stop cleanly, parse cleanly, validate against
the schema, and still carry verdicts for six of the forty items it was given —
the schema says "a list of verdicts", not "a verdict for every item". The
folding writes only the rows it was handed, so the other thirty-four used to
end the run unruled with nothing at any layer saying so.

Both are handled the same way here: absorb whatever DID come back, re-ask only
what is missing, and count anything still missing at the end as lost so the
caller can say so out loud. Recovery is bounded twice over, because every retry
is paid for — a window that truncates is halved (a size problem, which shrinking
fixes), a window that answered only partly is re-asked at most `MAX_ASKS` times,
and a single item that still truncates gets ONE doubled ceiling before it is
reported lost. The measure (asks remaining, window size) strictly decreases on
every re-queue, so the loop always terminates.

Recovery runs on the calling thread, not the pool. The main path is unchanged —
callers still fan their windows out concurrently and fold serially, which is
what keeps ids coming off a shared counter in document order — and recovery is
rare enough that doing it serially costs nothing but keeps the ordering
guarantee obvious.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Sequence

log = logging.getLogger("docproof.windowing")

MAX_ASKS = 2          # how many times a partially-answered window is re-asked
MAX_CEILING_BUMPS = 1  # how often a lone truncating item gets a bigger ceiling


@dataclass
class WindowReport:
    """What one batched pass actually got answers for.

    Reported rather than inferred: a pass that lost every window emits no
    findings, which is indistinguishable from a pass that ruled on everything
    and affirmed nothing — unless the run says which."""
    label: str = ""
    asked: int = 0
    answered: int = 0
    truncated_calls: int = 0
    extra_calls: int = 0

    @property
    def lost(self) -> int:
        """Items that went out and never came back with a verdict."""
        return max(0, self.asked - self.answered)

    @property
    def clean(self) -> bool:
        return self.lost == 0

    def summary(self) -> str:
        bits = [f"{self.answered}/{self.asked} item(s) ruled on"]
        if self.truncated_calls:
            bits.append(f"{self.truncated_calls} call(s) hit the token ceiling")
        if self.extra_calls:
            bits.append(f"{self.extra_calls} recovery call(s)")
        if self.lost:
            bits.append(f"{self.lost} LOST (no verdict)")
        return "; ".join(bits)


@dataclass
class _Ask:
    """One outstanding question: which slice of the window, how many times it
    has already been asked, and under what ceiling."""
    offsets: list[int]        # 0-based offsets into the caller's window
    asks: int = 1
    bumps: int = 0
    ceiling: int = 0


def resolve_window(window: Sequence[Any], first_result, *,
                   fetch: Callable[[Sequence[Any], int], Any],
                   rows_of: Callable[[Any], dict[int, Any]],
                   max_tokens: int,
                   report: WindowReport,
                   usage_sink: Callable[[Any], None] | None = None,
                   ) -> dict[int, Any]:
    """Every answer this window can be made to give, keyed by 0-based offset.

    `fetch(items, ceiling)` asks the model about a sub-list and returns a
    ProviderResult; `rows_of(parsed)` turns a parsed body into {1-based item
    number: row}, and returns {} for a body that does not validate. `first_result`
    is the answer the caller already has in hand, so the happy path costs no
    extra call.

    Anything still unanswered when recovery is exhausted is counted in `report`
    and simply absent from the returned mapping — the caller folds what it got
    and reports the rest."""
    got: dict[int, Any] = {}
    report.asked += len(window)

    def absorb(result, offsets: list[int]) -> None:
        """Write through whatever came back, mapping the model's 1-based item
        numbers onto the caller's offsets. A number outside the list it was
        given is dropped rather than wrapped around."""
        if result is None or result.parsed is None:
            return
        for n, row in rows_of(result.parsed).items():
            if 1 <= n <= len(offsets):
                got[offsets[n - 1]] = row

    def follow_up(ask: _Ask, result) -> list[_Ask]:
        """What still needs asking after `result` came back for `ask`."""
        before = len(got)
        absorb(result, ask.offsets)
        if result is not None and result.stop_reason == "max_tokens":
            report.truncated_calls += 1
        missing = [o for o in ask.offsets if o not in got]
        if not missing:
            return []
        # Shrink when the model gave us nothing to go on — an explicit
        # truncation, or a window that answered none of what it was asked, which
        # is likelier to be too big than to be unanswerable.
        shrink = (result is None or result.stop_reason == "max_tokens"
                  or len(got) == before)
        return _requeue(ask, missing, shrink)

    head = _Ask(offsets=list(range(len(window))), ceiling=max_tokens)
    queue = follow_up(head, first_result)

    while queue:
        ask = queue.pop(0)
        result = fetch([window[o] for o in ask.offsets], ask.ceiling)
        report.extra_calls += 1
        if usage_sink is not None:
            usage_sink(result.usage)
        queue.extend(follow_up(ask, result))

    report.answered += len(got)
    return got


def _requeue(ask: _Ask, missing: list[int], shrink: bool) -> list[_Ask]:
    """The next question(s) to ask about what did not come back, or nothing when
    recovery is spent.

    A truncated window is a SIZE problem, so it is halved — that is the only
    answer that actually changes the outcome, and it costs two cheap calls
    instead of one that cannot fit. A window that answered part of what it was
    asked is simply re-asked for the remainder. A lone item that still truncates
    cannot be split, so it gets one doubled ceiling and is then given up on:
    re-asking it forever would buy the same failure at a rising price.

    Every branch strictly decreases (asks remaining, window size), which is what
    makes the caller's loop terminate."""
    if shrink and len(missing) == 1:
        if ask.bumps < MAX_CEILING_BUMPS:
            return [_Ask(offsets=missing, asks=ask.asks, bumps=ask.bumps + 1,
                         ceiling=ask.ceiling * 2)]
        log.error("A single item still came back unanswered at a %d-token "
                  "ceiling; it goes unruled.", ask.ceiling)
        return []
    if shrink:
        half = max(1, len(missing) // 2)
        return [_Ask(offsets=missing[:half], asks=ask.asks, bumps=ask.bumps,
                     ceiling=ask.ceiling),
                _Ask(offsets=missing[half:], asks=ask.asks, bumps=ask.bumps,
                     ceiling=ask.ceiling)]
    if ask.asks >= MAX_ASKS:
        return []                      # counted as lost by the caller's report
    return [_Ask(offsets=missing, asks=ask.asks + 1, bumps=ask.bumps,
                 ceiling=ask.ceiling)]


def log_report(report: WindowReport) -> None:
    """Say what was lost, loudly enough that a short result is never read as a
    clean one. A pass that answered everything logs nothing here."""
    if report.lost:
        log.error("%s: %d of %d item(s) never got a verdict — they were paid "
                  "for and dropped, NOT ruled 'not an error'. %s",
                  report.label, report.lost, report.asked, report.summary())
    elif report.truncated_calls:
        log.warning("%s: recovered from %d truncated call(s) with %d extra "
                    "call(s); every item was ruled on.", report.label,
                    report.truncated_calls, report.extra_calls)
