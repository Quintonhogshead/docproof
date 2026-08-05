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
    # Which channel this type's findings go down, and with it what
    # corrected_text means:
    #   change — a tracked change; corrected_text is the fixed sentence.
    #   query  — a margin comment that edits nothing; corrected_text repeats
    #            the sentence unchanged, because the answer is the author's to
    #            make.
    #   format — a tracked formatting revision; corrected_text is the exact
    #            span to mark, because the text itself does not change at all.
    channel: str = "change"
    # What a format channel applies. Only "italic" today; the field exists so
    # the reassembler is told rather than assuming.
    format: str = "italic"

    @property
    def is_query(self) -> bool:
        return self.channel == "query"

    @property
    def is_format(self) -> bool:
        return self.channel == "format"


REQUIRED = ("key", "name", "version", "detection_prompt", "fix_guidance")


def load_error_types(dir_path: str | Path, enabled: list[str], *,
                     override_dir: str | Path | None = None) -> dict[str, ErrorType]:
    """Read the enabled error types, preferring an edited copy where one exists.

    `override_dir` is where the app writes prompts the user has edited. It
    shadows per key rather than wholesale, so editing one prompt does not
    freeze the other twenty at the version shipped that day."""
    dir_path = Path(dir_path)
    overrides = Path(override_dir) if override_dir else None
    registry: dict[str, ErrorType] = {}
    for key in enabled:
        path = dir_path / f"{key}.yaml"
        if overrides is not None and (overrides / f"{key}.yaml").is_file():
            path = overrides / f"{key}.yaml"
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
        channel = data.get("channel", "change")
        if channel not in ("change", "query", "format"):
            raise ValueError(
                f"{path}: channel must be 'change', 'query' or 'format', "
                f"not {channel!r}")
        registry[key] = ErrorType(
            key=data["key"], name=data["name"], version=int(data["version"]),
            detection_prompt=data["detection_prompt"].strip(),
            fix_guidance=data["fix_guidance"].strip(),
            confidence_guidance=data.get("confidence_guidance", "").strip(),
            examples=tuple(data.get("examples", [])),
            channel=channel, format=data.get("format", "italic"),
        )
    return registry