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
        for field_name in ("model", "min_confidence", "output_dir",
                           "default_mode", "prep_output", "comments",
                           "explanations", "indesign_template"):
            value = getattr(update, field_name)
            if value is not None:
                setattr(s, field_name, value)
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
