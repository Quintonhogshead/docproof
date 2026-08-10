"""The model catalog, priced for the files in hand."""
from __future__ import annotations

from fastapi import Depends, FastAPI

from docproof.providers import EFFORT_MULTIPLIER, MODELS, estimate_cost

from . import common
from .. import settings as settingslib
from ..auth import owner_for
from ..settings import Paths


def register(app: FastAPI) -> None:

    @app.get("/api/models")
    def models(file_ids: str = "", owner: str = Depends(owner_for)) -> dict:
        """The catalog, plus what these specific files would cost on each."""
        paths: Paths = app.state.paths
        keys = settingslib.key_status()
        ids = [f for f in file_ids.split(",") if f]
        totals = common.token_totals(paths, ids, owner)

        out = []
        for m in MODELS:
            entry = {"id": m.id, "provider": m.provider, "display": m.display,
                     "blurb": m.blurb,
                     "available": keys.get(m.provider, {}).get("configured",
                                                               False),
                     # Rates travel with the model so the picker can re-price a
                     # partial selection without another round trip.
                     "input_per_mtok": m.input_per_mtok,
                     "output_per_mtok": m.output_per_mtok,
                     "supports_effort": m.supports_effort,
                     "batch_discount": m.batch_discount}
            if totals:
                inp, req = totals
                entry["cost_now"] = estimate_cost(
                    m.id, input_tokens=inp,
                    output_tokens=req * common.OUTPUT_TOKEN_GUESS)
                entry["cost_batch"] = estimate_cost(
                    m.id, input_tokens=inp,
                    output_tokens=req * common.OUTPUT_TOKEN_GUESS, batch=True)
            out.append(entry)
        # The effort table and the configured default ride along so the picker
        # can factor the reasoning dial into its live estimate and open on the
        # house default, both without hardcoding server-owned values.
        return {"models": out, "keys": keys,
                "output_token_guess": common.OUTPUT_TOKEN_GUESS,
                "effort_multipliers": EFFORT_MULTIPLIER,
                "default_model": app.state.settings.model,
                "default_glossary_model": app.state.settings.glossary_model}
