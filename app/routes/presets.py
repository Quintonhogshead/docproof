"""The effort-tier presets: named bundles of per-run controls the picker applies
client-side. Read-only — no job is created here; the browser folds a chosen tier
onto the ordinary POST /api/jobs body (see app/presets.py)."""
from __future__ import annotations

from fastapi import FastAPI

from ..presets import DEFAULT_TIER_ID, TIERS


def register(app: FastAPI) -> None:

    @app.get("/api/presets")
    def presets() -> dict:
        """The four effort tiers. The client maps a chosen tier onto the review
        panel's existing controls; the job payload shape is unchanged. Sapling is
        a policy ("off" | "if_keyed" | "always") the client resolves against the
        live key status from /api/models."""
        return {"default": DEFAULT_TIER_ID,
                "tiers": [t.to_json() for t in TIERS]}
