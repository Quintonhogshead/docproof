"""The review defaults, and the keys that pay for them."""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from docproof.config import load_config
from docproof.providers import MODELS

from . import common
from .. import settings as settingslib
from ..settings import CONFIG_PATH, Settings


class SettingsUpdate(BaseModel):
    model: str | None = None
    min_confidence: str | None = None
    effort: str | None = None
    rounds: int | None = None
    output_dir: str | None = None
    default_mode: str | None = None
    comments: bool | None = None
    explanations: bool | None = None
    prep_output: str | None = None
    indesign_template: str | None = None
    anthropic_key: str | None = None
    openai_key: str | None = None
    gemini_key: str | None = None
    # Not an AI key: a read-only token for checking published releases.
    github_token: str | None = None


def register(app: FastAPI) -> None:

    may_edit = common.admin_gate(
        app, "Only an administrator can change the review defaults.")

    @app.get("/api/settings")
    def read_settings() -> dict:
        s: Settings = app.state.settings
        return {"settings": s.__dict__, "keys": settingslib.key_status()}

    @app.put("/api/settings", dependencies=[Depends(may_edit)])
    def write_settings(update: SettingsUpdate) -> dict:
        s: Settings = app.state.settings
        if update.effort is not None and update.effort not in settingslib.EFFORT_LEVELS:
            raise HTTPException(
                400, f"effort must be one of {', '.join(settingslib.EFFORT_LEVELS)}")
        if update.rounds is not None and not 1 <= update.rounds <= 4:
            raise HTTPException(400, "rounds must be between 1 and 4")
        editable = ["model", "min_confidence", "effort", "rounds",
                    "output_dir", "default_mode", "prep_output",
                    "comments", "explanations", "indesign_template"]
        if app.state.web:
            # Results live on the mounted volume on a server; an output folder is
            # a desktop notion, and persisting one here is precisely how the path
            # gets poisoned so a redeploy writes finished documents to the
            # container's throwaway disk (see create_app, which clamps it at boot
            # regardless). So refuse to store one at all rather than accept a
            # value the boot clamp will only override.
            editable.remove("output_dir")
        for field_name in editable:
            value = getattr(update, field_name)
            if value is not None:
                setattr(s, field_name, value)
        # An explicit reviewer choice is a deliberate one, so mark the settings
        # current: the one-shot legacy-default boot migration only rewrites a
        # stored model whose settings_version is behind, so stamping it here means
        # a model an admin actually picked (even claude-sonnet-5 on a fresh,
        # never-migrated install) is never silently reverted to the shipped
        # default on the next boot. Only an explicit model PUT stamps — an
        # effort-only save (model is None) leaves the version alone, so a legacy
        # volume still frozen on Sonnet is still repaired by the migration.
        if update.model is not None:
            s.settings_version = settingslib.CURRENT_SETTINGS_VERSION
        for provider, value in (("anthropic", update.anthropic_key),
                                ("openai", update.openai_key),
                                ("gemini", update.gemini_key),
                                ("github", update.github_token)):
            if value is None:
                continue
            if value.strip():
                settingslib.set_api_key(provider, value.strip())
            else:
                settingslib.delete_api_key(provider)
        s.save(app.state.paths)
        return {"settings": s.__dict__, "keys": settingslib.key_status()}

    @app.post("/api/settings/test/{provider}")
    def test_key(provider: str) -> dict:
        """One cheap real call, so a bad key fails here and not at 11pm."""
        if provider == "sapling":
            # Sapling is not a review provider, so it never builds a model
            # provider — one tiny edits call is its "does the key work?".
            key = settingslib.get_api_key("sapling")
            if not key:
                return {"ok": False, "message": "No key saved yet."}
            from docproof.sapling import SaplingError, check
            try:
                check("This are a test.", key)
            except SaplingError as e:
                return {"ok": False, "message": str(e)}
            return {"ok": True, "message": "Key works."}
        if provider not in ("anthropic", "openai", "gemini"):
            raise HTTPException(404, "Unknown provider")
        key = settingslib.get_api_key(provider)
        if not key:
            return {"ok": False, "message": "No key saved yet."}
        model = next((m.id for m in MODELS if m.provider == provider), None)
        cfg = load_config(CONFIG_PATH)
        cfg.api.model = model
        try:
            from docproof.providers import build_provider
            p = build_provider(cfg, api_key=key)
            result = p.complete_structured(
                model=model, system="Reply with the JSON object {\"ok\": true}.",
                user="ping",
                schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"], "additionalProperties": False},
                schema_name="ping", max_tokens=64)
        except Exception as e:                # noqa: BLE001 - surface verbatim
            return {"ok": False, "message": str(e)}
        if result.stop_reason != "ok":
            return {"ok": False, "message": result.error or result.stop_reason}
        return {"ok": True, "message": "Key works."}
