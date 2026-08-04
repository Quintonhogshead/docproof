"""Showing and editing the instructions that actually get sent.

The prompt is assembled from the shipped error-type YAML files. A user who
wants to change what the model looks for should not have to find those files
inside an app bundle, so edits are written to a parallel directory in the app
home and shadow the originals per key. The shipped files are never touched:
"reset" is deleting one small file, not restoring from a backup.
"""
from __future__ import annotations

import itertools
import logging
from pathlib import Path

import yaml

from docproof.analyzer import render_chunk
from docproof.config import Config
from docproof.error_registry import load_error_types
from docproof.models import Chunk, ParagraphRef
from docproof.pipeline import build_analyzers

log = logging.getLogger("docproof.app.prompts")

# Stand-in text for the "what gets sent" preview. Deliberately carries an
# error of its own, so the preview shows a realistic request.
SAMPLE = ParagraphRef(
    para_id="body-0000", part="word/document.xml", location="body",
    style="Normal",
    text="The manuscript was finished, nobody wanted to read it.")


class PromptError(ValueError):
    """An edit that cannot be saved."""


def _read(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def list_prompts(shipped_dir: Path, override_dir: Path,
                 keys: list[str]) -> list[dict]:
    """One row per enabled error type: what it says now, what it shipped
    saying, and whether those differ."""
    rows = []
    for key in keys:
        shipped_path = shipped_dir / f"{key}.yaml"
        if not shipped_path.is_file():
            continue
        shipped = _read(shipped_path)
        override_path = override_dir / f"{key}.yaml"
        edited = _read(override_path) if override_path.is_file() else None
        current = (edited or shipped).get("detection_prompt", "").strip()
        rows.append({
            "key": key,
            "name": shipped.get("name", key),
            "version": shipped.get("version", 0),
            "detection_prompt": current,
            "default_detection_prompt": shipped.get("detection_prompt",
                                                    "").strip(),
            "fix_guidance": shipped.get("fix_guidance", "").strip(),
            "edited": edited is not None
            and current != shipped.get("detection_prompt", "").strip(),
        })
    return rows


def assembled_passes(cfg: Config, shipped_dir: Path,
                     override_dir: Path | None) -> list[dict]:
    """The exact system prompt each pass will send, with the edits applied."""
    registry = load_error_types(shipped_dir, list(cfg.error_type_keys),
                                override_dir=override_dir)
    groups = [[registry[k] for k in group] for group in cfg.error_type_groups]
    analyzers = build_analyzers(cfg, groups, None, itertools.count(1))
    return [{"label": a.label, "keys": list(a.keys),
             "system_prompt": a.system_prompt,
             "characters": len(a.system_prompt)}
            for a in analyzers]


def sample_user_turn() -> str:
    return render_chunk(Chunk(chunk_id="chunk-000", paragraphs=(SAMPLE,),
                              est_tokens=20))


def save_override(shipped_dir: Path, override_dir: Path, key: str,
                  detection_prompt: str) -> dict:
    """Write an edited prompt. The override is a full copy of the shipped
    file with one field replaced, so it stays a valid error type on its own
    and `load_error_types` needs no special case for it."""
    shipped_path = shipped_dir / f"{key}.yaml"
    if not shipped_path.is_file():
        raise PromptError(f"There is no error type called {key!r}.")
    if not detection_prompt.strip():
        raise PromptError("A prompt cannot be empty. Use reset to go back to "
                          "the original.")
    data = _read(shipped_path)
    data["detection_prompt"] = detection_prompt.strip() + "\n"
    override_dir.mkdir(parents=True, exist_ok=True)
    (override_dir / f"{key}.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                       default_flow_style=False),
        encoding="utf-8")
    log.info("Saved an edited prompt for %s", key)
    return data


def clear_override(override_dir: Path, key: str) -> bool:
    """Drop back to the shipped prompt. True if there was one to drop."""
    path = override_dir / f"{key}.yaml"
    if not path.is_file():
        return False
    path.unlink()
    log.info("Reset %s to the prompt it shipped with", key)
    return True
