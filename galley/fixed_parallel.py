"""Concurrent reads with shared transport limits and ordered result consumption."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from contextvars import copy_context
from threading import BoundedSemaphore, local


class ReadScheduler:
    def __init__(self, cfg):
        from galley.fixed_workflow import SONNET, LUNA
        serial = cfg.api.concurrency == 1
        self.widths = {"claude": cfg.concurrency_for(SONNET),
                       "api": cfg.concurrency_for(LUNA),
                       # Subscription concurrency follows its own configured allowance.
                       "codex": 1 if serial else cfg.api.subagent_concurrency}
        self.slots = {key: BoundedSemaphore(width) for key, width in self.widths.items()}
        self.serial = BoundedSemaphore(1) if serial else None
        self.active = local()

    @staticmethod
    def lane(model):
        from galley.fixed_workflow import SOL, ASTRA
        return "claude" if model.startswith("claude-") else "codex" if model in {SOL, ASTRA} else "api"

    def run(self, model, operation):
        lane = self.lane(model)
        if getattr(self.active, "lane", None) == lane:
            return operation()
        with self.slots[lane]:
            self.active.lane = lane
            try:
                if self.serial is not None:
                    with self.serial:
                        return operation()
                return operation()
            finally:
                self.active.lane = None

    def map(self, jobs):
        """Jobs are (model, callable); only model reads run on worker threads.

        Independent providers have separate pools: queued Claude work cannot
        monopolize the API workers. Nested batches share the same lane limits.
        Consumers apply results in input order against their frozen snapshot.
        """
        jobs = list(jobs)
        if not jobs:
            return []
        with ExitStack() as stack:
            lanes = {self.lane(model) for model, _ in jobs}
            pools = {lane: stack.enter_context(ThreadPoolExecutor(
                max_workers=min(self.widths[lane], sum(self.lane(m) == lane for m, _ in jobs)),
                thread_name_prefix="galley-" + lane)) for lane in sorted(lanes)}
            futures = [pools[self.lane(model)].submit(copy_context().run, self.run, model, operation)
                       for model, operation in jobs]
            try:
                return [future.result() for future in futures]
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
