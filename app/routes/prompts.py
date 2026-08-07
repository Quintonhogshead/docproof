"""What docproof says to the model, and the publisher's edits to it."""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from docproof.config import load_config

from . import common
from ..prompts import (PromptError, assembled_passes, clear_override,
                       list_prompts, sample_user_turn, save_override)
from ..settings import CONFIG_PATH, ERROR_DIR, Paths


class PromptUpdate(BaseModel):
    detection_prompt: str = Field(min_length=1)


def register(app: FastAPI) -> None:

    may_edit = common.admin_gate(
        app, "Only an administrator can change what DocProof looks for.")

    @app.get("/api/prompts")
    def read_prompts() -> dict:
        """What docproof is about to say to the model, and where to change it."""
        paths: Paths = app.state.paths
        cfg = load_config(CONFIG_PATH)
        cfg.error_type_override_dir = str(paths.prompts)
        cfg.report_explanations = app.state.settings.explanations
        return {
            "types": list_prompts(ERROR_DIR, paths.prompts,
                                  list(cfg.error_type_keys)),
            "passes": assembled_passes(cfg, ERROR_DIR, paths.prompts),
            "sample_user_turn": sample_user_turn(),
            "explanations": cfg.report_explanations,
        }

    @app.put("/api/prompts/{key}", dependencies=[Depends(may_edit)])
    def write_prompt(key: str, update: PromptUpdate) -> dict:
        try:
            save_override(ERROR_DIR, app.state.paths.prompts, key,
                          update.detection_prompt)
        except PromptError as e:
            raise HTTPException(400, str(e))
        return read_prompts()

    @app.delete("/api/prompts/{key}", dependencies=[Depends(may_edit)])
    def reset_prompt(key: str) -> dict:
        clear_override(app.state.paths.prompts, key)
        return read_prompts()
