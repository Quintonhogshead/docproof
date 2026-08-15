#!/usr/bin/env python3
"""Load and validate the smoothing trap set — the passages that gate a
language-smoothing pass in both directions.

DEV TOOLING. Not shipped in the wheel, never imported by the pipeline.

`eval/smoothing/traps.yaml` holds hand-authored prose of two opposite kinds.
The `silence` items — dialect dialogue, refrains, deliberate fragments,
stylized narration, rhythm-driven pointing — must draw nothing at all; a
smoothing pass that proposes an edit on one of them has flattened a voice,
which is the one failure mode the pass cannot be allowed to have. The
`control` items are the mirror: prose a line editor would certainly smooth,
with no voice defence available, where SILENCE is the failure. A set of the
first kind alone is not a gate, because a pass that emits nothing anywhere
scores a perfect 0% firing rate on it; read the two numbers together or
neither means anything.

Silence items also carry a `difficulty`. `overt` is the floor — the voice
signal is loud and unmistakable, and a firing there says the pass cannot see
stylization at all. `marginal` is where a gate discriminates: one quiet device
in an otherwise plain paragraph, where a suggestion would be a defensible call
and still the wrong one. A pass clean on `overt` and noisy on `marginal` has
learned to recognize costume, not voice, so the scorer reports the firing rate
per band, not just overall.

Why this loader lives in the repo and not in `~/.docproof-private-eval/`: the
trap file is a repo file, and a repo file whose only reader is code outside the
repo cannot be tested, refactored, or kept honest by `pytest`. The private tier
imports this module (`sys.path.insert(0, REPO)`; `from eval.tools.style_traps
import load_traps`) instead of re-parsing the YAML itself, so the schema has
exactly one implementation and `tests/test_style_traps.py` guards it.

The schema is deliberately incompatible with `eval/cases/*.yaml`. That corpus is
keyed `error_type` / `cases` / `expect`; this file is keyed `kind` / `traps` /
`why_silence`, and carries `kind: smoothing_traps` as an explicit discriminator.
Feed either file to the other's loader and you get a named error rather than a
silently mis-scored run — the same reason `docproof/eval/corpus.py` raises
`CorpusError` on a duplicate id. The trap file also sits outside `eval/cases/`,
which `load_corpus` globs non-recursively, so the standard scorecard cannot pick
it up even by accident; `tests/test_style_traps.py` verifies that mechanism
rather than trusting it.

The file also declares, in a `reconciliation` block, WHICH MODEL its header's
detector-reconciliation table was measured on, whether that model is the one
`config/default.yaml` configures as the detector, and WHICH SCRIPT regenerates
the table. Those are fields rather than sentences because both omissions they
guard against already happened: the whole table was produced against a stand-in
model while the header called it "the real pipeline", and the header claimed the
table "is regenerated whenever a passage changes" while the script that produced
it lived in a scratchpad that no longer exists. Findings do not carry between
models, so a reconciliation is a claim about one model and has to name it;
`tests/test_style_traps.py` checks the `is_configured_detector` flag against the
config and the `runner` path against the filesystem rather than trusting either.
The runner that satisfies it is `eval/tools/reconcile_traps.py`.

    python -m eval.tools.style_traps              # validate + composition table
    python -m eval.tools.style_traps --list       # one line per trap
    python -m eval.tools.reconcile_traps --runs 6 # regenerate the reconciliation
"""
from __future__ import annotations

import argparse
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml

# eval/tools/style_traps.py -> eval/ -> eval/smoothing/traps.yaml
DEFAULT_TRAPS = Path(__file__).resolve().parents[1] / "smoothing" / "traps.yaml"

# The discriminator. A file without it is not a trap file, whatever it contains.
KIND = "smoothing_traps"
# 1 was silence-only, with no `role` and no `difficulty`. 2 adds both, so a
# scorer written against 1 would silently score controls as silence traps and
# report a firing rate that is wrong in the flattering direction. 3 turns
# `mechanical_overlap` from one type key into a LIST of them, because running
# the real detector over the file showed passages where two first-class types
# have a claim at once (rhy-08: a splice AND a withheld introductory comma).
# 4 adds the required `reconciliation` block. A v3 file could carry a whole
# detector-reconciliation section measured on some model other than the one
# `config/default.yaml` configures and say nothing about it — which is exactly
# what v3 did — so the model is a field a test cross-checks rather than a
# parenthesis inside a comment nobody can verify.
# 5 adds `reconciliation.runner`: the repo path of the script that REGENERATES
# the section. v4's header claimed the section "is regenerated whenever a
# passage changes rather than being inherited" while the script that produced
# it lived in a scratchpad that did not survive the session — an unfalsifiable
# claim about an unrepeatable measurement. The runner is now a field, and
# `tests/test_style_traps.py` checks that the file it names exists, so the
# claim cannot outlive the tool again.
# A v2 scalar read as a v3 list iterates its characters, so every version below
# the current one is refused outright rather than coerced.
SCHEMA_VERSIONS = (5,)

# `status` gates use: the set is agent-drafted, and `docs/accuracy-eval-plan.md`
# requires trap material to be human-authored, so nothing may be scored against
# it until the user has signed off and flipped this to "approved".
DRAFT = "draft-pending-user-signoff"
APPROVED = "approved"
STATUSES = (DRAFT, APPROVED)

# Whether the trap document is fed through `docproof.normalize` before the pass
# runs. A run contract, not a preference: straight and curly quotes are not the
# same input, and Phase 3 and the scorer have to agree or their firing rates are
# not comparable. See the file header for the reasoning.
NORMALIZATIONS = ("on", "off")

SILENCE = "silence"
CONTROL = "control"
ROLES = (SILENCE, CONTROL)

OVERT = "overt"
MARGINAL = "marginal"
DIFFICULTIES = (OVERT, MARGINAL)

# One category per voice-risk family. A smoothing pass fails differently against
# each, so the scorer reports the firing rate per category, not just overall.
SILENCE_CATEGORIES = (
    "dialect-idiolect",
    "intentional-repetition",
    "deliberate-fragment",
    "stylized-archaic-narration",
    "rhythm-punctuation",
    "free-indirect-interiority",
    "embedded-document-register",
    "lyric-inversion",
)

# Control families. Kept separate from the silence vocabulary so a category
# typo cannot quietly move an item between the two halves of the metric.
CONTROL_CATEGORIES = (
    "control-deadwood",
    "control-clumsy-syntax",
)

CATEGORIES = SILENCE_CATEGORIES + CONTROL_CATEGORIES

# Field sets by role. The two roles ask opposite questions of a passage, so
# they carry opposite prose: a silence trap records the edit that must NOT
# happen and why; a control records the edit that must.
REQUIRED_COMMON = ("id", "category", "text")
REQUIRED_SILENCE = ("tempting_edit", "why_silence")
REQUIRED_CONTROL = ("expected_edit", "why_edit")

# Design range from the phase plan. A passage under the floor gives a smoothing
# pass nothing to be tempted by; one over the ceiling stops being a single
# paragraph and starts being a document, which changes what a firing means.
MIN_WORDS = 40
MAX_WORDS = 120

# Straight double quotes only, so the dialogue heuristic below is one rule and
# the normalization contract has exactly one direction to run in.
_QUOTED = re.compile(r'"[^"]*"')


@dataclass(frozen=True)
class Trap:
    id: str                 # stable, kebab-case, unique across the file
    category: str           # one of CATEGORIES, consistent with `role`
    text: str               # the passage, one paragraph, mechanically clean
    role: str = SILENCE     # SILENCE: must draw nothing. CONTROL: must draw
                            # something — silence on a control is a FAILURE.
    difficulty: str = ""    # SILENCE only: OVERT (loud) or MARGINAL (quiet).
                            # Empty on controls, which are not graded on how
                            # hidden the voice is; there is none to hide.
    tempting_edit: str = ""  # SILENCE: the smoothing a pass might wrongly propose
    why_silence: str = ""    # SILENCE: why proposing it would be wrong
    expected_edit: str = ""  # CONTROL: the smoothing a pass ought to propose
    why_edit: str = ""       # CONTROL: why no voice defence is available
    # The keys of the first-class error types that legitimately own some span
    # in this passage (e.g. a stylistic comma splice, which
    # config/error_types/comma_splice.yaml asks the model to report at low
    # confidence). A mechanical finding from one of them is NOT a smoothing
    # failure; filter these when scoring anything but the smoothing channel.
    # A tuple, not a single key: a real detector run over this file found
    # passages where two types have a claim on different spans at once.
    mechanical_overlap: tuple[str, ...] = ()
    source_file: str = ""   # provenance, for error messages

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def quoted_chars(self) -> int:
        """Characters inside paired double quotes. The dialogue heuristic is
        deliberately crude — it cannot see a speech running across paragraphs,
        and it counts a quoted document (doc-08) as dialogue — but every
        passage here is a single paragraph with balanced quotes, so on this
        file it is exact."""
        return sum(len(m.group()) for m in _QUOTED.finditer(self.text))

    @property
    def is_majority_dialogue(self) -> bool:
        return self.quoted_chars * 2 > len(self.text)


@dataclass(frozen=True)
class Reconciliation:
    """Which model the file's DETECTOR RECONCILIATION section was measured on.

    Required as of schema 4, and required because of a specific failure: the
    whole section was produced against gpt-5.6-luna while `config/default.yaml`
    configured claude-haiku-4-5 as the detector, and the header — which called
    the section "the real pipeline" — named the model only in passing, so
    nothing in the file or its tests could tell the two apart. Findings are not
    portable between models (`eval/tools/anchor_dump.py` exists because the
    accuracy baseline put claude-haiku-4-5 at a 29% anchor-failure rate against
    gpt-5.6-luna's 0%), so a reconciliation is a claim about ONE model and has
    to say which.

    `is_configured_detector` is the author's assertion that `model` is what
    `config/default.yaml` sets as `api.model`; `tests/test_style_traps.py`
    checks the assertion against the config rather than trusting it, so the flag
    fails loudly instead of drifting when either side moves.

    `runner` is the repo-relative path of the script that regenerates the
    section, required as of schema 5 for the same reason: the header claimed
    the table "is regenerated whenever a passage changes" while the script that
    produced it lived in a scratchpad and was gone, so the claim could be
    neither checked nor acted on. A test asserts the named file exists.
    """
    model: str                    # catalog id the reconciliation ran on
    effort: str                   # effort it ran at, or "" for a model with none
    runs: int                     # how many runs the section's counts rest on
    is_configured_detector: bool  # is `model` config/default.yaml's api.model?
    runner: str                   # repo-relative path of the regenerating script


class TrapError(ValueError):
    """A malformed trap file, a duplicate id, or a file that is not one."""


def _check_str(item: dict, field: str, where: str) -> None:
    value = item.get(field)
    if not value or not isinstance(value, str) or not value.strip():
        raise TrapError(f"{where}: missing or non-string {field!r}")


def load_traps(path: str | Path = DEFAULT_TRAPS) -> list[Trap]:
    """Every trap in `path`, in file order.

    Raises TrapError on anything that would make a trap-firing number a lie: a
    file of the wrong kind, an unknown schema version, category, role or
    difficulty, a missing field, a duplicate id (ids are the join key to a
    scorer's per-trap results, so a collision silently merges two passages), a
    multi-paragraph passage, a passage outside the design word range, or an
    item whose `role` and `category` disagree — which would move a passage
    between the two halves of the metric without anyone noticing.
    """
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TrapError(f"no trap file at {path}") from None
    if not isinstance(data, dict):
        raise TrapError(f"{path}: expected a mapping at the top level")

    kind = data.get("kind")
    if kind != KIND:
        raise TrapError(
            f"{path}: 'kind' must be {KIND!r}, not {kind!r}. This loader reads "
            f"the smoothing trap set; the accuracy corpus (error_type/cases/"
            f"expect) is docproof.eval.corpus.load_corpus's job")
    version = data.get("schema_version")
    if version not in SCHEMA_VERSIONS:
        raise TrapError(
            f"{path}: unknown schema_version {version!r}, expected one of "
            f"{SCHEMA_VERSIONS}. Version 1 was silence-only; a v1 file read as "
            f"v2 would report a firing rate with no controls in it")
    status = data.get("status")
    if status not in STATUSES:
        raise TrapError(
            f"{path}: 'status' must be one of {STATUSES}, not {status!r}")
    normalization = data.get("normalization")
    if normalization not in NORMALIZATIONS:
        raise TrapError(
            f"{path}: 'normalization' must be one of {NORMALIZATIONS}, not "
            f"{normalization!r} — the scorer and the pass have to agree on "
            f"whether the trap document is normalized before the run")
    _parse_reconciliation(data.get("reconciliation"), path)
    raw = data.get("traps")
    if not isinstance(raw, list) or not raw:
        raise TrapError(f"{path}: 'traps' must be a non-empty list")

    traps: list[Trap] = []
    seen: dict[str, int] = {}
    for i, item in enumerate(raw):
        where = f"{path} trap #{i} ({item.get('id', '?') if isinstance(item, dict) else '?'})"
        if not isinstance(item, dict):
            raise TrapError(f"{where}: not a mapping")
        for field in REQUIRED_COMMON:
            _check_str(item, field, where)
        tid = item["id"]
        if tid in seen:
            raise TrapError(
                f"{where}: duplicate trap id {tid!r} — already used by trap "
                f"#{seen[tid]}; ids are the join key to per-trap results")
        seen[tid] = i

        category = item["category"]
        if category not in CATEGORIES:
            raise TrapError(
                f"{where}: unknown category {category!r}, expected one of "
                f"{CATEGORIES}")
        # `role` defaults to silence: the file is overwhelmingly silence traps
        # and repeating the key on every one of them would bury the ten that
        # matter. A control has to opt in, and the category cross-check below
        # is what stops a typo from misfiling either way.
        role = item.get("role", SILENCE)
        if role not in ROLES:
            raise TrapError(
                f"{where}: unknown role {role!r}, expected one of {ROLES}")
        expected_role = CONTROL if category in CONTROL_CATEGORIES else SILENCE
        if role != expected_role:
            raise TrapError(
                f"{where}: category {category!r} implies role "
                f"{expected_role!r}, but the item says {role!r}. A mismatch "
                f"here moves a passage between the two halves of the metric")

        difficulty = item.get("difficulty")
        if role == SILENCE:
            for field in REQUIRED_SILENCE:
                _check_str(item, field, where)
            for field in REQUIRED_CONTROL:
                if item.get(field) is not None:
                    raise TrapError(
                        f"{where}: {field!r} belongs to a control, not to a "
                        f"silence trap")
            if difficulty not in DIFFICULTIES:
                raise TrapError(
                    f"{where}: 'difficulty' must be one of {DIFFICULTIES}, not "
                    f"{difficulty!r} — the firing rate is reported per band")
        else:
            for field in REQUIRED_CONTROL:
                _check_str(item, field, where)
            for field in REQUIRED_SILENCE:
                if item.get(field) is not None:
                    raise TrapError(
                        f"{where}: {field!r} belongs to a silence trap, not to "
                        f"a control")
            if difficulty is not None:
                raise TrapError(
                    f"{where}: a control carries no 'difficulty' — it is not "
                    f"graded on how hidden the voice is, having none")
            difficulty = ""

        text = item["text"]
        if "\n" in text.strip():
            raise TrapError(
                f"{where}: 'text' must be a single paragraph — use a folded "
                f"block scalar (>-), not a literal one (|-)")
        if "  " in text:
            raise TrapError(
                f"{where}: 'text' contains a double space. The normalization "
                f"contract depends on the space rule being a no-op, so that "
                f"normalizing the trap document cannot shift an offset")
        if any(c in text for c in "“”‘’"):
            raise TrapError(
                f"{where}: 'text' contains a curly quote. The passages are "
                f"written straight and curled by the normalizer at run time; "
                f"mixing the two makes the run contract meaningless")
        words = len(text.split())
        if not MIN_WORDS <= words <= MAX_WORDS:
            raise TrapError(
                f"{where}: {words} words, outside the design range "
                f"{MIN_WORDS}-{MAX_WORDS}")
        if text.count('"') % 2:
            raise TrapError(
                f"{where}: odd number of double quotes — the dialogue-share "
                f"measurement needs balanced pairs inside one paragraph")

        overlap = item.get("mechanical_overlap", [])
        # A bare string is refused rather than wrapped: v2 wrote one key here,
        # and silently accepting it would let a v2 file that a v3 loader
        # already rejected by version sneak through some other reader.
        if isinstance(overlap, str):
            raise TrapError(
                f"{where}: 'mechanical_overlap' is a LIST of error-type keys as "
                f"of schema 3, not the single key {overlap!r} — write "
                f"[{overlap}]")
        if not isinstance(overlap, list) or any(
                not isinstance(k, str) or not k.strip() for k in overlap):
            raise TrapError(
                f"{where}: 'mechanical_overlap' must be a list of non-empty "
                f"error-type keys")
        if len(set(overlap)) != len(overlap):
            raise TrapError(
                f"{where}: 'mechanical_overlap' repeats a key {overlap!r} — a "
                f"duplicate would double-count in the reconciliation table")
        traps.append(Trap(
            id=tid, category=category, text=text, role=role,
            difficulty=difficulty,
            tempting_edit=item.get("tempting_edit", ""),
            why_silence=item.get("why_silence", ""),
            expected_edit=item.get("expected_edit", ""),
            why_edit=item.get("why_edit", ""),
            mechanical_overlap=tuple(overlap), source_file=str(path)))
    return traps


def _parse_reconciliation(raw, path: Path) -> Reconciliation:
    """The `reconciliation` block, validated. Required as of schema 4: a
    reconciliation with no model named is a table of numbers with no claim
    attached to it."""
    if not isinstance(raw, dict):
        raise TrapError(
            f"{path}: 'reconciliation' must be a mapping naming the model the "
            f"DETECTOR RECONCILIATION section was measured on. Required as of "
            f"schema 4 — findings do not carry between models, so a table with "
            f"no model on it is not a claim about anything")
    model = raw.get("model")
    if not model or not isinstance(model, str) or not model.strip():
        raise TrapError(f"{path}: 'reconciliation.model' must be a catalog id")
    effort = raw.get("effort", "")
    if effort is None:
        effort = ""
    if not isinstance(effort, str):
        raise TrapError(
            f"{path}: 'reconciliation.effort' must be a string (\"\" for a "
            f"model that has no effort setting), not {effort!r}")
    runs = raw.get("runs")
    # bool is an int subclass, and `runs: true` is a typo, not one run.
    if not isinstance(runs, int) or isinstance(runs, bool) or runs < 1:
        raise TrapError(
            f"{path}: 'reconciliation.runs' must be a positive integer — the "
            f"detector is stochastic, so every count in the section is only "
            f"meaningful with the number of runs behind it")
    flag = raw.get("is_configured_detector")
    if not isinstance(flag, bool):
        raise TrapError(
            f"{path}: 'reconciliation.is_configured_detector' must be true or "
            f"false — whether {model!r} is what config/default.yaml sets as "
            f"api.model. Omitting it is how a reconciliation against a stand-in "
            f"model gets read as one against the shipped detector")
    runner = raw.get("runner")
    if not runner or not isinstance(runner, str) or not runner.strip():
        raise TrapError(
            f"{path}: 'reconciliation.runner' must be the repo-relative path of "
            f"the script that regenerates the DETECTOR RECONCILIATION section. "
            f"Required as of schema 5 — the previous header claimed the section "
            f"was regenerated on every passage change while the script that "
            f"produced it existed nowhere in the repo")
    return Reconciliation(model=model, effort=effort, runs=runs,
                          is_configured_detector=flag, runner=runner)


def trap_file_reconciliation(path: str | Path = DEFAULT_TRAPS) -> Reconciliation:
    """Which model the file's DETECTOR RECONCILIATION section was measured on,
    at what effort, over how many runs, and whether that model is the one
    `config/default.yaml` configures. Read this before quoting any number from
    that section."""
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TrapError(f"{path}: expected a mapping at the top level")
    return _parse_reconciliation(data.get("reconciliation"), path)


def trap_file_status(path: str | Path = DEFAULT_TRAPS) -> str:
    """The file's `status` field, validated. `APPROVED` means the user has
    signed the passages off and a firing rate measured against them can be
    quoted; `DRAFT` means it cannot."""
    return _top_level(path, "status", STATUSES)


def trap_file_normalization(path: str | Path = DEFAULT_TRAPS) -> str:
    """The file's `normalization` run contract — "on" means the scorer must
    push the trap document through `docproof.normalize` before the smoothing
    pass sees it, exactly as a manuscript is. Phase 3 reads this rather than
    deciding for itself."""
    return _top_level(path, "normalization", NORMALIZATIONS)


def _top_level(path: str | Path, field: str, allowed: tuple[str, ...]) -> str:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    value = data.get(field) if isinstance(data, dict) else None
    if value not in allowed:
        raise TrapError(f"{path}: {field!r} must be one of {allowed}, not {value!r}")
    return value


def silence_traps(traps: list[Trap]) -> list[Trap]:
    """The passages that must draw nothing. Firing here is the failure."""
    return [t for t in traps if t.role == SILENCE]


def controls(traps: list[Trap]) -> list[Trap]:
    """The passages that must draw something. Silence here is the failure."""
    return [t for t in traps if t.role == CONTROL]


def count_by_category(traps: list[Trap]) -> Counter[str]:
    """Per-category counts, for the coverage table. A category with one or two
    passages measures nothing; the spread is part of the trap set's value."""
    return Counter(t.category for t in traps)


def count_by_difficulty(traps: list[Trap]) -> Counter[str]:
    """Per-band counts over the silence traps. Controls are not counted: they
    have no difficulty, and folding them in would make the denominator of the
    per-band firing rate wrong."""
    return Counter(t.difficulty for t in silence_traps(traps))


def dialogue_share(traps: list[Trap]) -> float:
    """Fraction of the silence set's characters that sit inside quoted speech.

    Reported because `config/error_types/general_error.yaml` already exempts
    dialect and intentional phrasing unconditionally, so a trap set made of
    quoted speech would score a guard that ships for free while saying nothing
    about narration — which is where Phase 3 does its work and where the risk
    actually is. Controls are excluded: they carry no dialogue by construction
    and would only dilute the number.
    """
    items = silence_traps(traps)
    total = sum(len(t.text) for t in items)
    return sum(t.quoted_chars for t in items) / total if total else 0.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--traps", default=str(DEFAULT_TRAPS),
                    help="path to the trap YAML (default: %(default)s)")
    ap.add_argument("--list", action="store_true",
                    help="print one line per trap instead of the summary table")
    args = ap.parse_args(argv)

    try:
        traps = load_traps(args.traps)
        status = trap_file_status(args.traps)
        normalization = trap_file_normalization(args.traps)
        recon = trap_file_reconciliation(args.traps)
    except TrapError as exc:
        print(f"INVALID: {exc}")
        return 1

    silence, ctl = silence_traps(traps), controls(traps)
    print(f"{len(traps)} items in {args.traps}: "
          f"{len(silence)} silence, {len(ctl)} control")
    print(f"status: {status}"
          + ("   <-- NOT user-approved; do not quote a firing rate yet"
             if status != APPROVED else ""))
    print(f"normalization: {normalization}")
    print(f"reconciled against: {recon.model}"
          + (f" (effort {recon.effort})" if recon.effort else "")
          + f", {recon.runs} run(s)"
          + ("" if recon.is_configured_detector else
             "   <-- NOT the detector config/default.yaml configures; the "
             "reconciliation table is about this model only"))
    print()
    if args.list:
        for t in traps:
            flag = (f"  [also {', '.join(t.mechanical_overlap)}]"
                    if t.mechanical_overlap else "")
            band = t.difficulty or t.role.upper()
            print(f"{t.id:10} {t.category:28} {band:8} {t.word_count:3}w{flag}")
        return 0

    counts = count_by_category(traps)
    by_band: Counter[tuple[str, str]] = Counter(
        (t.category, t.difficulty) for t in silence)
    width = max(len(c) for c in CATEGORIES)
    print(f"{'category':{width}}  {'overt':>5} {'marg':>5} {'total':>5}")
    for category in SILENCE_CATEGORIES:
        print(f"{category:{width}}  {by_band.get((category, OVERT), 0):5} "
              f"{by_band.get((category, MARGINAL), 0):5} "
              f"{counts.get(category, 0):5}")
    bands = count_by_difficulty(traps)
    print(f"{'SILENCE':{width}}  {bands.get(OVERT, 0):5} "
          f"{bands.get(MARGINAL, 0):5} {len(silence):5}")
    print()
    for category in CONTROL_CATEGORIES:
        print(f"{category:{width}}  {'':5} {'':5} {counts.get(category, 0):5}")
    print(f"{'CONTROL':{width}}  {'':5} {'':5} {len(ctl):5}")
    print()
    print(f"dialogue share (silence set): {dialogue_share(traps):.1%}")
    majority = [t.id for t in silence if t.is_majority_dialogue]
    print(f"majority-dialogue passages:   {len(majority)}/{len(silence)}"
          + (f"  ({', '.join(majority)})" if majority else ""))

    overlaps = [t for t in traps if t.mechanical_overlap]
    if overlaps:
        declared = sum(len(t.mechanical_overlap) for t in overlaps)
        print(f"\n{len(overlaps)} trap(s) declare {declared} overlap(s) with a "
              f"first-class error type (exclude when scoring a non-smoothing "
              f"channel):")
        for t in overlaps:
            print(f"  {t.id} -> {', '.join(t.mechanical_overlap)}")
        by_type = Counter(k for t in overlaps for k in t.mechanical_overlap)
        print("  by type: " + ", ".join(f"{k} ({n})"
                                        for k, n in sorted(by_type.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
