"""The two files prep can write, from one plan.

`clean` produces what a designer places; `tracked` produces the same decisions
as Word revisions. Both consume the same `PrepPlan`, which is what keeps them
from drifting into two different opinions about the manuscript.
"""
from __future__ import annotations

from .clean import WriteStats, write_clean
from .tracked import write_tracked

__all__ = ["WriteStats", "write_clean", "write_tracked"]
