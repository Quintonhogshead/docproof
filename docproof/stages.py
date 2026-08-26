"""Workflow-stage presets — which LANES run, with locks a genre cannot reopen.

A genre preset (docproof/genre.py) sets *posture*: how the copy-edit lane
judges, how eagerly it corrects a name. A STAGE preset sets something
orthogonal — *which lanes run at all* in this pass of the loop. The two were
being conflated: a non-fiction genre preset that flips ``smoothing.edits`` and
``rewrite.enabled`` on turned what was meant to be a mechanical proofread into
a copy-edit pass, silently. That is the contamination this module removes.

Two mechanisms:

* ``patch`` — a nested ``section -> {field: value}`` (or top-level
  ``key: value``) mapping applied onto a loaded Config, each touched section
  rebuilt through its own ``model_validate`` so a bad value fails at load, the
  same as ``genre.py``'s overlay and ``profiles.py``. Unlike a genre overlay
  (flat, whitelisted, scalar-only), a stage patch may set whole sections —
  ``ensemble.detectors`` is a list of dicts — because a stage is a deliberate,
  human-authored recipe, not a per-book posture nudge.

* ``locks`` — a list of dotted ``section.field`` keys whose value, AFTER the
  stage patch, is frozen. :func:`enforce_locks` re-asserts them after any later
  overlay (a genre) has been applied, and reports every key a later overlay
  tried to move. This is what makes "a stage cannot accidentally enable a lane
  belonging to another stage" a guarantee rather than a convention: compose the
  stage first, the genre second, then re-enforce the locks — the stage wins,
  exactly as ``profiles.py`` wins over ``genre.py``.

Composition order for a full run config is therefore:

    base config  ->  stage patch  ->  genre overlay  ->  enforce stage locks
                                                       ->  profile (stricter still)

so the strict-to-loose precedence runs profile > stage > genre > base. See
docproof/__main__.py's ``_configure`` (the live ``--stage``/``--genre`` path)
and docproof/genre.py's ``materialize_genre_pack`` (the genre-pack file path).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import Config

# The lane switches a stage is allowed to LOCK. A lock outside this set is a
# preset-authoring bug (a stage locking a mechanics knob would be exactly the
# cross-contamination in the other direction). Kept deliberately to the
# stylistic/recall lanes plus the recall ensemble — never normalize/style/
# sweeps/edit_guard, which are hammered the same in every stage anyway.
LOCKABLE_KEYS: frozenset[str] = frozenset({
    "smoothing.enabled",
    "smoothing.edits",
    "rewrite.enabled",
    "ensemble.verify_policy",
    "repair.enabled",
    "chapter_sweep.enabled",
    "sapling.enabled",
    "rounds.count",
})


def _stages_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "stages"


def available_stages(stages_dir: str | Path | None = None) -> tuple[str, ...]:
    """Every stage with a preset file, sorted (package-relative by default)."""
    root = Path(stages_dir) if stages_dir else _stages_dir()
    if not root.is_dir():
        return ()
    return tuple(sorted(p.stem for p in root.glob("*.yaml")))


def load_stage_preset(stage: str,
                      stages_dir: str | Path | None = None) -> dict[str, Any]:
    """Load and lightly validate one stage preset file's raw YAML.

    Raises ValueError on a missing file, a non-mapping root, a non-mapping
    ``patch``, a non-list ``locks``, a lock outside :data:`LOCKABLE_KEYS`, or a
    lock whose key the patch does not set (a lock must pin something the stage
    actually established — otherwise it silently freezes a base-config default
    the author never saw)."""
    root = Path(stages_dir) if stages_dir else _stages_dir()
    path = root / f"{stage}.yaml"
    if not path.is_file():
        available = ", ".join(available_stages(stages_dir)) or "(none found)"
        raise ValueError(
            f"Unknown stage preset {stage!r}. Expected {path}. "
            f"Available: {available}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{path}: root must be a YAML mapping, not {type(raw).__name__}")
    patch = raw.get("patch") or {}
    if not isinstance(patch, dict):
        raise ValueError(f"{path}: 'patch' must be a mapping of "
                         f"section -> field updates")
    locks = raw.get("locks") or []
    if not isinstance(locks, list):
        raise ValueError(f"{path}: 'locks' must be a list of dotted "
                         f"'section.field' keys")
    bad = sorted(set(locks) - LOCKABLE_KEYS)
    if bad:
        raise ValueError(
            f"{path}: lock key(s) not lockable by a stage: {', '.join(bad)}. "
            f"A stage may only lock a stylistic/recall lane — see "
            f"docproof/stages.py's LOCKABLE_KEYS.")
    for dotted in locks:
        section, _, field = dotted.partition(".")
        if section not in patch or not isinstance(patch[section], dict) \
                or field not in patch[section]:
            raise ValueError(
                f"{path}: lock {dotted!r} names a value the patch does not "
                f"set. A lock must pin a value this stage establishes.")
    return raw


def _apply_patch(cfg: Config, patch: dict[str, Any]) -> None:
    """Apply a nested ``section -> {field: value}`` (or top-level ``key: value``)
    patch onto a loaded Config in place. A section that is itself a pydantic
    model is rebuilt through ``model_validate`` (so a bad value fails at load);
    any other top-level key is assigned directly (guarded by Config's
    ``validate_assignment``)."""
    for key, value in patch.items():
        if not hasattr(cfg, key):
            raise ValueError(f"stage patch: Config has no field {key!r}")
        current = getattr(cfg, key)
        if hasattr(current, "model_validate") and isinstance(value, dict):
            merged = {**current.model_dump(), **value}
            setattr(cfg, key, type(current).model_validate(merged))
        else:
            setattr(cfg, key, value)


def _read_dotted(cfg: Config, dotted: str) -> Any:
    section, _, field = dotted.partition(".")
    return getattr(getattr(cfg, section), field)


def apply_stage(cfg: Config, stage: str | None, *,
                stages_dir: str | Path | None = None
                ) -> tuple[Config, dict[str, Any]]:
    """Apply ``stage``'s patch to ``cfg`` in place and return ``(cfg, locks)``,
    where ``locks`` maps each locked dotted key to the value the stage just
    established — the values :func:`enforce_locks` will re-assert after a genre
    overlay. ``stage=None`` is a no-op with an empty lock map."""
    if not stage:
        return cfg, {}
    preset = load_stage_preset(stage, stages_dir)
    _apply_patch(cfg, preset.get("patch") or {})
    locks = {dotted: _read_dotted(cfg, dotted)
             for dotted in (preset.get("locks") or [])}
    return cfg, locks


def enforce_locks(cfg: Config, locks: dict[str, Any]) -> list[str]:
    """Re-assert every locked value on ``cfg`` in place, returning the sorted
    list of keys whose current value DIFFERED from the lock (i.e. a later
    overlay moved a lane the stage had frozen). The caller logs the violations;
    the point is that the run still ends in the stage-correct state."""
    violated: list[str] = []
    for dotted, value in locks.items():
        section, _, field = dotted.partition(".")
        current = getattr(cfg, section)
        if getattr(current, field) != value:
            violated.append(dotted)
            merged = {**current.model_dump(), field: value}
            setattr(cfg, section, type(current).model_validate(merged))
    return sorted(violated)


__all__ = [
    "LOCKABLE_KEYS",
    "apply_stage",
    "available_stages",
    "enforce_locks",
    "load_stage_preset",
]
