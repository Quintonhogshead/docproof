"""Which English this manuscript is written in, and what that changes.

Most of the house style is the same everywhere. A small number of conventions
flip — which quotation mark opens dialogue, whether a decade takes a leading
apostrophe, percent or per cent, how a date is set, and whether the that/which
distinction is a rule or a habit to preserve.

Those could have been four copies of every error type. They are not, because
four copies drift: fix a do-not-flag list in the U.S. file and the other three
quietly keep the bug. Instead each variant is one small file of conventions,
injected into every pass the way the manuscript's own vocabulary is, plus a
handful of parameters the scripted stages read directly.

The brief asks for one more thing, and it is the reason `confirm` exists:
Canadian and Australian manuscripts are hybrids — Canadian takes U.S.
punctuation with Canadian spelling — so the press is asked to confirm the
choice before a pass is treated as final. That disclosure is not optional and
lands in the change log.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

log = logging.getLogger("docproof.variants")

VARIANT_KEYS = ("us", "uk", "ca", "au")


@dataclass(frozen=True)
class Variant:
    key: str
    name: str                       # "U.K. English"
    authorities: tuple[str, ...]    # the style guide and dictionary in force
    dictionary: str                 # Hunspell set for the spell scan
    primary_quote: str              # "double" | "single" — what opens dialogue
    conventions: str                # the prose the model is given
    confirm: bool = False           # ask the press to confirm before final

    @property
    def closing_quotes(self) -> str:
        """The characters that can close a line of dialogue. The dialogue-tag
        sweep keys off these, and getting them wrong in either direction means
        it silently finds nothing."""
        return "’'" if self.primary_quote == "single" else "”\""

    @property
    def opens_dialogue_with_single(self) -> bool:
        return self.primary_quote == "single"

    def prompt_section(self) -> str:
        """What the model is told, once per pass, cached with the rest of the
        system prompt."""
        authorities = "\n".join(f"- {a}" for a in self.authorities)
        confirm = ("\nThis variant is a hybrid and the press has been asked to "
                   "confirm it. Where a rule below is genuinely ambiguous for "
                   "this manuscript, prefer a query over a correction.\n"
                   if self.confirm else "")
        return (f"ENGLISH VARIANT: {self.name}\n"
                f"This manuscript is being proofread as {self.name}. The "
                f"authorities in force are:\n{authorities}\n{confirm}\n"
                f"Where a rule below differs by variant, these are the ones "
                f"that apply. They override any convention stated in an error "
                f"type's own section:\n\n{self.conventions}")


def _dir() -> Path:
    return Path(__file__).parent.parent / "config" / "variants"


@lru_cache(maxsize=8)
def load_variant(key: str, dir_path: str | None = None) -> Variant:
    root = Path(dir_path) if dir_path else _dir()
    path = root / f"{key}.yaml"
    if not path.is_file():
        raise FileNotFoundError(
            f"No such English variant: '{key}'. Expected {path}. "
            f"Available: {', '.join(VARIANT_KEYS)}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    missing = [f for f in ("key", "name", "dictionary", "primary_quote",
                           "conventions") if not data.get(f)]
    if missing:
        raise ValueError(f"{path} is missing required fields: {missing}")
    if data["key"] != key:
        raise ValueError(
            f"{path}: key '{data['key']}' must match the filename '{key}'")
    if data["primary_quote"] not in ("single", "double"):
        raise ValueError(
            f"{path}: primary_quote must be 'single' or 'double', "
            f"not {data['primary_quote']!r}")
    return Variant(
        key=data["key"], name=data["name"],
        authorities=tuple(data.get("authorities", ())),
        dictionary=data["dictionary"],
        primary_quote=data["primary_quote"],
        conventions=data["conventions"].strip(),
        confirm=bool(data.get("confirm", False)),
    )
