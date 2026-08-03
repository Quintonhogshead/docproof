from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ErrorType:
    key: str
    name: str
    version: int
    detection_prompt: str          # what the error IS, what to flag, what not to
    fix_guidance: str              # how corrected_text should be produced
    confidence_guidance: str
    examples: tuple[dict, ...]     # {"text": ..., "finding": {...} | None}


REQUIRED = ("key", "name", "version", "detection_prompt", "fix_guidance")


def load_error_types(dir_path: str | Path, enabled: list[str]) -> dict[str, ErrorType]:
    dir_path = Path(dir_path)
    registry: dict[str, ErrorType] = {}
    for key in enabled:
        path = dir_path / f"{key}.yaml"
        if not path.exists():
            raise FileNotFoundError(
                f"Error type '{key}' is enabled but {path} does not exist. "
                f"Available: {sorted(p.stem for p in dir_path.glob('*.yaml') if not p.stem.startswith('_'))}")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        missing = [f for f in REQUIRED if not data.get(f)]
        if missing:
            raise ValueError(f"{path} is missing required fields: {missing}")
        if data["key"] != key:
            raise ValueError(f"{path}: key '{data['key']}' must match filename '{key}'")
        registry[key] = ErrorType(
            key=data["key"], name=data["name"], version=int(data["version"]),
            detection_prompt=data["detection_prompt"].strip(),
            fix_guidance=data["fix_guidance"].strip(),
            confidence_guidance=data.get("confidence_guidance", "").strip(),
            examples=tuple(data.get("examples", [])),
        )
    return registry