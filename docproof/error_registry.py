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
    # An optional recall-tuned variant of detection_prompt, used only when the
    # ensemble runs with a verifier: with a second reviewer checking every
    # finding, a detector can report a borderline case at low confidence instead
    # of staying silent. Empty (the default) means the standard prompt is used
    # in every mode, so a type without one behaves identically — this is the
    # inert hook the Phase 3 prompt work fills in, one type at a time.
    ensemble_detection_prompt: str = ""

    def detection(self, ensemble: bool) -> str:
        """The detection prompt to send. Falls back to the standard one, so an
        untuned type is unaffected whether or not the ensemble is on."""
        if ensemble and self.ensemble_detection_prompt:
            return self.ensemble_detection_prompt
        return self.detection_prompt

    @property
    def is_query(self) -> bool:
        return self.channel == "query"

    @property
    def is_format(self) -> bool:
        return self.channel == "format"


REQUIRED = ("key", "name", "version", "detection_prompt", "fix_guidance")


def shipped_keys(dir_path: str | Path) -> frozenset[str]:
    """Every error type a directory defines, enabled or not.

    The closed world of registry keys, which is what tells a caller whether a
    label it does not recognise is a configured-but-switched-off type or
    something with no error type at all — see `labels.py` for why those two
    must not be confused."""
    return frozenset(p.stem for p in Path(dir_path).glob("*.yaml")
                     if not p.stem.startswith("_"))


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
                f"Available: {sorted(shipped_keys(dir_path))}")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            # The override dir holds user-edited copies, and an editor that
            # saved one blank must read as a broken file, not a crash.
            what = ("empty" if data is None
                    else f"a YAML {type(data).__name__}, not a mapping")
            raise ValueError(f"{path} is {what}; restore it or delete it so "
                             f"the shipped copy is used.")
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
            ensemble_detection_prompt=data.get(
                "ensemble_detection_prompt", "").strip(),
        )
    return registry