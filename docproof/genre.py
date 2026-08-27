"""Genre posture presets — a narrow, whitelisted config OVERLAY applied to a
loaded Config, the same shape as docproof/profiles.py's apply_profile.

House policy draws the line this module exists to enforce in code, not just
in the docs: mechanics/proofreading (Chicago enforcement, the sweeps, the
edit guard, the spell scan's denylist) are hammered the SAME in every genre.
Only the copy-edit / stylistic lane is genre-tunable — the smoothing pass's
taste, the rewrite pass's aggressiveness, the flights lane's judge posture,
and how eagerly the consistency scan corrects a proper name's spelling rather
than asking about it. `ALLOWED_PRESET_KEYS` is the whitelist that makes this
a guarantee rather than a convention: a preset naming any other key is
refused at load, before it ever touches a run. See tests/test_genre.py for
the enforcement test.

A genre preset lives at ``config/genres/<genre>.yaml`` (package-relative, like
config/variants/<key>.yaml) as::

    name: fantasy_sf
    description: >
      Protective, world-aware posture...
    overlay:
      flights.posture: lenient
      smoothing.judge_harshness: lenient
      consistency.name_dominance: 8

``overlay`` is a flat mapping of ``section.field`` dotted keys to the value
that field should take. Applying it reconstructs the named section through
pydantic (``SectionModel.model_validate``), so a malformed value in a preset
fails the same way a malformed config file would — at load, not silently.

Precedence with docproof/profiles.py's review profiles: apply_genre() is
meant to run BEFORE apply_profile() (see docproof/__main__.py's `_configure`).
A profile (detector-only, candidate-only) is a strict reproducibility
boundary that turns the whole stylistic lane off; if a genre ran after it,
"fantasy_sf turns smoothing back on" would silently break that boundary's own
promise (no comment channel). Landing genre first, profile last, means the
profile always wins — exactly the ordering docproof/__main__.py already uses
for `--model` and every other general config knob relative to `--profile`.

``flights.posture`` is a forward reference: the flights lane (a parallel
Galley track) is not yet a Config section in this checkout. Applying a genre
preset never fails on that account — an overlay key whose section does not
exist on the running Config is collected into the returned "pending" dict
instead of raised, so a preset written against the target schema still loads
today, and the caller (the CLI, or `docproof galley genre-pack`) can report
it rather than silently drop it. Once the flights lane lands, `hasattr(cfg,
"flights")` starts succeeding and the key applies with no change here.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from .config import Config

# Every dotted "section.field" key a genre preset's `overlay` may set. Nothing
# outside this set is reachable from a preset file, whatever the file says —
# see _apply_overlay's whitelist check. Grouped by section for readability;
# order carries no meaning.
ALLOWED_PRESET_KEYS: frozenset[str] = frozenset({
    # The flights lane's judge posture (forward reference; see module
    # docstring). "strict" | "lenient" is the shape the flights lane exposes.
    "flights.posture",

    # Smoothing (line-edit) aggressiveness — every knob here is already
    # documented in config.py's SmoothingConfig as a taste dial, not a
    # correctness one.
    "smoothing.enabled",
    "smoothing.edits",
    "smoothing.judge_harshness",
    "smoothing.proposer_restraint",
    "smoothing.include_dialogue",
    "smoothing.dialogue_categories",
    "smoothing.max_per_1000_words",
    "smoothing.min_confidence",

    # Rewrite-then-diff aggressiveness — a recall LEVER on the stylistic/
    # detection tradeoff (how hard to look, how many independent samples),
    # not a mechanics knob: it still runs through the ordinary edit_confidence
    # gate and the shared validator.
    "rewrite.enabled",
    "rewrite.samples",
    "rewrite.diverse",
    "rewrite.edit_confidence",

    # Consistency SEEDING and its correct-vs-ask bar. Deliberately narrow:
    # spelling_variants/abbreviations/acronym_case/chicago_notes stay off this
    # list because those are mechanical query scans, not a stylistic posture —
    # a genre does not change whether "grey"/"gray" gets asked about.
    "consistency.name_dominance",
    "consistency.name_min_count",
    "consistency.seeded_names",
})


def _genres_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "genres"


def available_genres(genres_dir: str | Path | None = None) -> tuple[str, ...]:
    """Every genre with a preset file, sorted. Package-relative by default,
    like docproof/variants.py's variant directory."""
    root = Path(genres_dir) if genres_dir else _genres_dir()
    if not root.is_dir():
        return ()
    return tuple(sorted(p.stem for p in root.glob("*.yaml")))


def load_genre_preset(genre: str,
                      genres_dir: str | Path | None = None) -> dict[str, Any]:
    """Load and lightly validate one genre preset file's raw YAML.

    Raises ValueError on a missing file, a non-mapping root, a non-mapping
    `overlay`, or an overlay key outside `ALLOWED_PRESET_KEYS` — every one of
    these is a preset-authoring bug, caught here rather than downstream where
    it would look like a run-time config error."""
    root = Path(genres_dir) if genres_dir else _genres_dir()
    path = root / f"{genre}.yaml"
    if not path.is_file():
        available = ", ".join(available_genres(genres_dir)) or "(none found)"
        raise ValueError(
            f"Unknown genre preset {genre!r}. Expected {path}. "
            f"Available: {available}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{path}: root must be a YAML mapping, not {type(raw).__name__}")
    overlay = raw.get("overlay") or {}
    if not isinstance(overlay, dict):
        raise ValueError(f"{path}: 'overlay' must be a mapping of dotted "
                         f"'section.field' keys to values")
    bad = sorted(set(overlay) - ALLOWED_PRESET_KEYS)
    if bad:
        raise ValueError(
            f"{path}: overlay key(s) not in the posture whitelist: "
            f"{', '.join(bad)}. A genre preset may only touch the "
            f"stylistic lane — see docproof/genre.py's ALLOWED_PRESET_KEYS.")
    return raw


def _apply_overlay(cfg: Config, overlay: dict[str, Any]) -> dict[str, Any]:
    """Apply a whitelisted dotted-key overlay onto a loaded Config, in place.

    Each touched section is rebuilt through `SectionModel.model_validate` (not
    a bare attribute set) so a bad value in a preset is caught by that
    section's own field validators — the same failure mode as a malformed
    config file — and then assigned back onto `cfg` as one top-level field,
    which is what `Config`'s `validate_assignment=True` actually guards (see
    docproof/config.py:1592 and docproof/profiles.py's identical pattern).

    Returns the subset of `overlay` whose SECTION does not exist on this
    Config build (e.g. `flights.*` before the flights lane lands) — collected
    rather than applied or raised, for the caller to report. Every key here
    has already cleared the ALLOWED_PRESET_KEYS whitelist by the time this is
    called (load_genre_preset checks it), so this function trusts its input
    and does not re-check it — it exists to be reused by direct callers
    (tests) that already have a raw overlay dict in hand.
    """
    by_section: dict[str, dict[str, Any]] = defaultdict(dict)
    for dotted, value in overlay.items():
        section, _, field = dotted.partition(".")
        by_section[section][field] = value

    pending: dict[str, Any] = {}
    for section, updates in by_section.items():
        if not hasattr(cfg, section):
            for field, value in updates.items():
                pending[f"{section}.{field}"] = value
            continue
        current = getattr(cfg, section)
        merged = {**current.model_dump(), **updates}
        setattr(cfg, section, type(current).model_validate(merged))
    return pending


def apply_genre(cfg: Config, genre: str | None, *,
                genres_dir: str | Path | None = None) -> tuple[Config, dict]:
    """Apply `genre`'s posture preset to `cfg` in place. Returns (cfg, pending)
    for convenient chaining, where `pending` names any overlay keys whose
    section this Config build does not have yet (see module docstring).

    `genre=None` is a no-op, exactly like `apply_profile(cfg, None)`."""
    if not genre:
        return cfg, {}
    preset = load_genre_preset(genre, genres_dir)
    pending = _apply_overlay(cfg, preset.get("overlay") or {})
    return cfg, pending


def _apply_section_updates(cfg: Config, section: str, updates: dict) -> None:
    """Like `_apply_overlay` for one already-known-to-exist section, used for
    the genre-PACK-only additions (`genre_scans`) that are not part of the
    whitelisted `overlay` and so are exempt from ALLOWED_PRESET_KEYS — see
    `materialize_genre_pack`'s docstring for why that exemption is safe."""
    current = getattr(cfg, section)
    merged = {**current.model_dump(), **updates}
    setattr(cfg, section, type(current).model_validate(merged))


def materialize_genre_pack(base_config: str | Path, genre: str, *,
                           profile: Any | None = None,
                           corrections: Any | None = None,
                           era: int | None = None,
                           stage: str | None = None,
                           genres_dir: str | Path | None = None,
                           stages_dir: str | Path | None = None
                           ) -> tuple[Config, dict[str, Any]]:
    """Base config + a genre's posture preset + (optionally) profile-derived
    seeding, as one materialized Config plus a summary dict of what changed —
    the run config `docproof galley genre-pack` writes to disk.

    Unlike `apply_genre` (the bare `--genre` review flag, restricted to the
    narrow, whitelisted `overlay`), this ALSO applies a genre file's two
    genre-pack-only sections when present:

    * ``continuity_prompt`` — a path (relative to the genre file's own
      directory) to a prompt file whose text replaces `cfg.continuity.prompt`
      wholesale (see docproof/continuity.py's `prompt` field; it is part of
      the read's cache key, so an edited prompt re-reads rather than
      returning a stale result). Does NOT turn continuity on by itself —
      that stays a separate cost/opt-in decision.
    * ``genre_scans`` — a nested mapping applied onto `cfg.genre_scans`
      (anachronism / citation_format / reading_level), each field validated
      through its own section model exactly like `overlay` is.

    These two are exempt from `ALLOWED_PRESET_KEYS` deliberately: that
    whitelist exists to keep the bare `--genre` review flag from silently
    reaching mechanics, but `genre-pack` writes a REVIEWABLE file a human
    reads before it runs anything — the safety property here is "nothing
    happens until someone points a review at this file," not "this key is
    forbidden." continuity.prompt and genre_scans.* are both already
    query-only-by-construction passes (see docproof/continuity.py and
    docproof/genrescans.py), so even an unreviewed materialize cannot turn
    into a silent edit.

    `profile` (a docproof.genre_profile.Profile, or anything with the same
    `.proper_nouns` / `.reading_level.ari` shape) seeds:

    * every proper-noun candidate into `consistency.seeded_names` AND
      `spellcheck.allowlist` (union with whatever the base config already
      has — never a replace), and
    * the book's own median ARI into `genre_scans.reading_level.target_ari`,
      so a reading-level scan a preset turned on centers on this book's
      actual band, not the shipped default's self-referential guess.

    `era`, if given, sets `genre_scans.anachronism.era` explicitly — never
    inferred from the profile (see AnachronismScanConfig's docstring on why
    guessing the era defeats the point of the scan)."""
    from .config import load_config
    cfg = load_config(str(base_config))
    preset = load_genre_preset(genre, genres_dir)
    summary: dict[str, Any] = {"genre": genre, "base_config": str(base_config)}

    # A workflow-stage preset lands FIRST (which lanes run), then the genre
    # posture on top, then the stage's lane locks are re-asserted so the stage
    # wins over the genre — the same precedence the live --stage/--genre path
    # uses in docproof/__main__.py's _configure. See docproof/stages.py.
    stage_locks: dict[str, Any] = {}
    if stage:
        from .stages import apply_stage
        cfg, stage_locks = apply_stage(cfg, stage, stages_dir=stages_dir)
        summary["stage"] = stage

    pending = _apply_overlay(cfg, preset.get("overlay") or {})
    summary["overlay_applied"] = sorted(set(preset.get("overlay") or {})
                                        - set(pending))
    summary["pending"] = pending

    prompt_rel = preset.get("continuity_prompt")
    if prompt_rel:
        root = Path(genres_dir) if genres_dir else _genres_dir()
        prompt_path = root / prompt_rel
        cfg.continuity.prompt = prompt_path.read_text(encoding="utf-8")
        summary["continuity_prompt"] = str(prompt_path)

    scan_updates = preset.get("genre_scans") or {}
    if scan_updates:
        if not isinstance(scan_updates, dict):
            raise ValueError(f"genre {genre!r}: 'genre_scans' must be a "
                             f"mapping of scan name -> field updates")
        for scan_name, updates in scan_updates.items():
            if not hasattr(cfg.genre_scans, scan_name):
                raise ValueError(f"genre {genre!r}: genre_scans has no scan "
                                 f"named {scan_name!r}")
            current = getattr(cfg.genre_scans, scan_name)
            merged = {**current.model_dump(), **updates}
            setattr(cfg.genre_scans, scan_name,
                    type(current).model_validate(merged))
        summary["genre_scans_applied"] = scan_updates

    seeded_names: list[str] = []
    if profile is not None:
        # With a correction overlay, seed only the vetted names (reject/suspect
        # dropped); the raw profile is untouched. Without it, every candidate,
        # exactly as before.
        if corrections is not None:
            from .profile_corrections import seedable_names
            names = seedable_names(profile, corrections)
            summary["corrections_applied"] = True
        else:
            names = [n.name for n in getattr(profile, "proper_nouns", [])]
        if names:
            merged_names = sorted(set(cfg.consistency.seeded_names) | set(names))
            cfg.consistency.seeded_names = merged_names
            cfg.spellcheck.allowlist = sorted(
                set(cfg.spellcheck.allowlist) | set(names))
            seeded_names = names
        reading = getattr(profile, "reading_level", None)
        target_ari = getattr(reading, "ari", None) if reading else None
        if target_ari is not None:
            _apply_section_updates(cfg, "genre_scans", {
                "reading_level": {
                    **cfg.genre_scans.reading_level.model_dump(),
                    "target_ari": target_ari}})
            summary["reading_level_target_ari"] = target_ari
    summary["seeded_names_count"] = len(seeded_names)

    if era is not None:
        _apply_section_updates(cfg, "genre_scans", {
            "anachronism": {
                **cfg.genre_scans.anachronism.model_dump(), "era": era}})
        summary["anachronism_era"] = era

    # Re-assert the stage's lane locks LAST — after the genre overlay and every
    # profile-derived nudge — so the stage wins over the genre. A non-empty
    # `stage_lock_violations` names a lane the genre tried to reopen; the
    # materialized config still ends in the stage-correct state.
    if stage_locks:
        from .stages import enforce_locks
        summary["stage_lock_violations"] = enforce_locks(cfg, stage_locks)

    return cfg, summary


def write_genre_pack(base_config: str | Path, genre: str, out_path: str | Path,
                     *, profile: Any | None = None,
                     corrections: Any | None = None, era: int | None = None,
                     stage: str | None = None,
                     genres_dir: str | Path | None = None,
                     stages_dir: str | Path | None = None) -> dict[str, Any]:
    """`materialize_genre_pack`, written to `out_path` as YAML. Returns the
    same summary dict for the caller (the CLI) to print."""
    cfg, summary = materialize_genre_pack(
        base_config, genre, profile=profile, corrections=corrections, era=era,
        stage=stage, genres_dir=genres_dir, stages_dir=stages_dir)
    data = cfg.model_dump(mode="json")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Provenance header: a materialized config otherwise carries no trace of
    # the stage/genre that shaped it, so `galley approve` printed "stage None"
    # for a config that WAS a stage. Comment-only — invisible to yaml.safe_load.
    header = (f"# galley: genre={genre}"
              + (f" stage={stage}" if stage else "") + "\n")
    out.write_text(header
                   + yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                   encoding="utf-8")
    summary["out_path"] = str(out)
    return summary


__all__ = [
    "ALLOWED_PRESET_KEYS",
    "apply_genre",
    "available_genres",
    "load_genre_preset",
    "materialize_genre_pack",
    "write_genre_pack",
]
