"""The sweep guard: refuse a correction the house sweeps would immediately undo.

The deterministic sweeps (docproof/sweeps.py) are the house rules as code and
run FIRST on every build. A row from any other lane that writes a form a sweep
exists to remove — the typed `capitalization` pass lowercasing "2:30 AM" to
"2:30 am" (three rows on Georgis, 2026-09-04), a settle composite writing
"4:00 a.m." — is either re-fired on at the next build or rejected as an
overlap: a round of churn either way. Settle grew this guard first
(galley/settle.py); the main finish()/validator path had no such check, so the
ladder's own rows could undo house style. It lives here so the validator can
apply the same refusal to EVERY lane's row.
"""
from __future__ import annotations

from typing import Any, Sequence


class SweepGuard:
    """Refuses a settlement the deterministic sweeps would immediately undo.

    The sweeps (docproof/sweeps.py) are the house rules as code, and the
    rebuild runs them first with first claim on every span: a composite that
    writes "4:00 a.m." over the house "4:00 AM" is either re-fired on at the
    next build or rejected as an overlap — a round of churn either way, and
    on the Georgis run sixty-seven of them. So before a settlement is
    applied, the paragraph AS IT WOULD READ is scanned with the run's own
    sweep set; a hit inside the changed span that was not already there
    before the change means the settlement undoes house style, and the item
    is dropped with the sweep's key as the reason."""

    def __init__(self, sweeps: Sequence[Any], variant: Any = None, *,
                 ellipsis_style: str = "nbsp"):
        self.sweeps = list(sweeps)
        self.variant = variant
        self.ellipsis_style = ellipsis_style

    @classmethod
    def from_config(cls, cfg: Any, variant: Any = None) -> "SweepGuard":
        from docproof.sweeps import resolve as _resolve_sweeps
        keys = list(getattr(cfg, "sweeps", None) or [])
        style = getattr(getattr(cfg, "style", None), "ellipsis", "nbsp")
        try:
            sweeps = _resolve_sweeps(keys)
        except ValueError:
            sweeps = []
        return cls(sweeps, variant, ellipsis_style=style or "nbsp")

    def _hits_in(self, text: str, lo: int, hi: int) -> list[tuple[str, int, int]]:
        from functools import partial
        out: list[tuple[str, int, int]] = []
        for sweep in self.sweeps:
            scan = (partial(sweep.scan, style=self.ellipsis_style)
                    if sweep.key == "sweep_ellipsis" else sweep.scan)
            try:
                hits = scan(text, self.variant)
            except Exception:                                # noqa: BLE001
                continue
            for h in hits:
                # overlap, with a zero-width insertion counting as inside
                if h.start < hi and h.end > lo or (lo == hi and h.start <= lo <= h.end):
                    out.append((sweep.key, h.start, h.end))
        return out

    def refires(self, before: str, before_span: tuple[int, int],
                after: str, after_span: tuple[int, int]) -> str | None:
        """The key of the first sweep that would fire inside `after_span` of
        `after` and did not fire inside `before_span` of `before`, else
        None."""
        if not self.sweeps:
            return None
        after_hits = self._hits_in(after, *after_span)
        if not after_hits:
            return None
        before_keys = {k for k, _s, _e in self._hits_in(before, *before_span)}
        for key, _s, _e in after_hits:
            if key not in before_keys:
                return key
        return None



__all__ = ["SweepGuard"]
