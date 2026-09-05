"""Shared Galley tier names and default per-book budgets."""

GALLEY_TIERS = ("T0", "T1", "T2", "T3", "T4")
GALLEY_DEFAULT_BUDGET = {
    "T0": 15.0,
    "T1": 30.0,
    "T2": 60.0,
    "T3": 150.0,
    "T4": 300.0,
}

__all__ = ["GALLEY_DEFAULT_BUDGET", "GALLEY_TIERS"]
