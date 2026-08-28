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
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import yaml

log = logging.getLogger("docproof.variants")

VARIANT_KEYS = ("us", "uk", "ca", "au")

_DETECT_WORD = re.compile(r"[A-Za-z]+")


# High-signal spelling discriminators for variant DETECTION, as (British,
# American) stem pairs. The per-variant respell maps are a deliberately short
# ENFORCEMENT list (~30 words), too sparse to detect from on their own, so
# detection also reads the productive families — -our/-or (colour/color),
# -ise/-ize and -yse/-yze (realise/realize) — inflected on BOTH sides, plus a set
# of unambiguous one-offs. Every entry is wrong in the other variant; forms valid
# in both (learned, while, program) are excluded so ordinary prose never tips it.
_MARKER_PAIRS = (
    # -our / -or
    ("colour", "color"), ("honour", "honor"), ("favour", "favor"),
    ("behaviour", "behavior"), ("neighbour", "neighbor"), ("labour", "labor"),
    ("humour", "humor"), ("flavour", "flavor"), ("harbour", "harbor"),
    ("rumour", "rumor"), ("savour", "savor"), ("splendour", "splendor"),
    ("odour", "odor"), ("vigour", "vigor"), ("endeavour", "endeavor"),
    ("favourite", "favorite"), ("neighbourhood", "neighborhood"),
    ("colourful", "colorful"), ("honourable", "honorable"),
    # -ise / -ize
    ("realise", "realize"), ("organise", "organize"), ("recognise", "recognize"),
    ("apologise", "apologize"), ("criticise", "criticize"),
    ("emphasise", "emphasize"), ("summarise", "summarize"),
    ("memorise", "memorize"), ("prioritise", "prioritize"),
    ("characterise", "characterize"), ("specialise", "specialize"),
    ("categorise", "categorize"), ("civilise", "civilize"),
    ("authorise", "authorize"), ("minimise", "minimize"),
    ("maximise", "maximize"),
    # -yse / -yze
    ("analyse", "analyze"), ("paralyse", "paralyze"), ("catalyse", "catalyze"),
    # one-offs
    ("defence", "defense"), ("offence", "offense"), ("licence", "license"),
    ("catalogue", "catalog"), ("dialogue", "dialog"), ("litre", "liter"),
    ("fibre", "fiber"), ("calibre", "caliber"), ("sombre", "somber"),
    ("tyre", "tire"), ("mould", "mold"), ("smoulder", "smolder"),
    ("draught", "draft"), ("plough", "plow"), ("aluminium", "aluminum"),
    ("sceptical", "skeptical"), ("cosy", "cozy"), ("kerb", "curb"),
    ("manoeuvre", "maneuver"), ("ageing", "aging"))


def _inflect(stem: str) -> set[str]:
    """`stem` plus its regular inflections, e-aware (realise -> realised /
    realising, not realiseed)."""
    forms = {stem, stem + "s"}
    if stem.endswith("e"):
        forms |= {stem + "d", stem[:-1] + "ing"}
    else:
        forms |= {stem + "ed", stem + "ing"}
    return forms


@lru_cache(maxsize=1)
def _variant_markers() -> tuple[frozenset[str], frozenset[str]]:
    """(British-only forms, American-only forms) for variant detection: the
    curated respell maps unioned with the inflected marker pairs. A form on both
    sides (rare) is dropped so it never counts either way."""
    british = set(load_variant("us").respell_map)     # forms the US run respells
    american = set(load_variant("uk").respell_map)     # forms the UK run respells
    for b, a in _MARKER_PAIRS:
        british |= _inflect(b)
        american |= _inflect(a)
    shared = british & american
    return frozenset(british - shared), frozenset(american - shared)


def detect_variant(texts: Iterable[str], *, min_markers: int = 6,
                   margin: float = 1.5) -> str | None:
    """Guess the English variant from a manuscript's spelling, or None.

    Counts British-only spellings against American-only ones (the curated respell
    maps) and returns "us" when the American forms clearly dominate, "uk" when
    the British forms do (the general British bucket — Canadian and Australian
    are punctuation/spelling hybrids the practitioner confirms, never auto-set),
    or None when the evidence is thin (< `min_markers` total) or the two are
    mixed (neither leads by `margin`). Deterministic, $0, and only ever a
    starting guess — the variant it picks is logged and every mark it drives is a
    rejectable tracked change."""
    british, american = _variant_markers()
    b = a = 0
    for t in texts:
        for m in _DETECT_WORD.finditer(t):
            w = m.group(0).lower()
            if w in british:
                b += 1
            elif w in american:
                a += 1
    if b + a < min_markers:
        return None
    if a >= b * margin:
        return "us"
    if b >= a * margin:
        return "uk"
    return None


@dataclass(frozen=True)
class Variant:
    key: str
    name: str                       # "U.K. English"
    authorities: tuple[str, ...]    # the style guide and dictionary in force
    dictionary: str                 # Hunspell set for the spell scan
    primary_quote: str              # "double" | "single" — what opens dialogue
    conventions: str                # the prose the model is given
    confirm: bool = False           # ask the press to confirm before final
    # Spellings VALID in English but wrong for this variant, mapped to the
    # variant's own form (grey → gray for a U.S. run). The dictionary cannot
    # catch these — "grey" passes en_US — and a per-chunk read glides over
    # them, so each occurrence is put in front of the adjudication pass to
    # rule on in context; a proper noun ("Mr. Grey", the Aldwych Theatre)
    # survives that ruling. Stored as pairs because the dataclass is frozen.
    respell: tuple[tuple[str, str], ...] = ()

    @property
    def respell_map(self) -> dict[str, str]:
        return dict(self.respell)

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
    # "auto" is resolved to a concrete variant by pipeline.prepare (which has the
    # manuscript text to detect from). Any other path that loads a config still
    # set to the sentinel — a standalone profile read, say — has no text here, so
    # it falls back to US English rather than crashing on a missing auto.yaml.
    if key == "auto":
        log.info("Variant 'auto' reached load_variant with no text to detect "
                 "from; falling back to US English.")
        key = "us"
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
    respell = data.get("respell") or {}
    if not isinstance(respell, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and k.strip()
            and v.strip() for k, v in respell.items()):
        raise ValueError(
            f"{path}: respell must map wrong-variant spellings to this "
            f"variant's own (grey: gray), strings both sides")
    return Variant(
        key=data["key"], name=data["name"],
        authorities=tuple(data.get("authorities", ())),
        dictionary=data["dictionary"],
        primary_quote=data["primary_quote"],
        conventions=data["conventions"].strip(),
        confirm=bool(data.get("confirm", False)),
        respell=tuple(sorted((k.strip(), v.strip())
                             for k, v in respell.items())),
    )
