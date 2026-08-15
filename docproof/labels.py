"""Which finding labels are error types, and which only look like one.

A `Finding.error_type` is usually a key into `config/error_types/<key>.yaml` —
the registry that owns the detection prompt, the channel (correct or ask), and
the display name. Several passes tag their findings with a free-form string
instead, because there is no prompt to write: the sweeps, LanguageTool, Sapling,
the low-confidence valve, the continuity read and the consistency scan all
decide what they found without a model ever being shown a type definition.

THE TRAP THIS MODULE EXISTS FOR. `reporting.py` writes
`list(cfg.error_type_keys)` into every findings.json as `config.error_types`,
and `error_type_keys` is the *configured registry keys*. A free-form label can
therefore never appear in that list — not "usually does not", never, by
construction. So the inference

    label not in config.error_types  ->  this run was not asked to produce it

is false for every label in `FREE_FORM`, on every run, and it fails silently:
the check that draws it looks like it was correctly suppressed. That is not
hypothetical. The private harness's total-loss alarm keyed off exactly this and
was disabled on every production run — the alarm built to catch a failure
disguised as success was itself a failure disguised as success.

THE RULE. Prefer positive evidence: the pass's own findings, or its own
telemetry in the artifact. Fall back to configuration only for labels the
configuration actually speaks about. Where there is no evidence either way the
answer is `UNKNOWN`, never `DID_NOT_RUN` — a wrong confident negative silences
whatever was supposed to notice, and an admitted gap does not.

`tests/test_labels.py` walks the source for every `error_type=` assignment and
fails if a new free-form label appears here without `FREE_FORM` being updated,
so the list below cannot rot into a lie.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Findings labels with no `config/error_types/<name>.yaml` behind them. Each is
# a whole pass's output, so "is this label absent because the pass found
# nothing, or because it never ran?" is a real question about a real pass — and
# `config.error_types` cannot answer it for any of them.
FREE_FORM: frozenset[str] = frozenset({
    "languagetool",       # pipeline.py / batch.py — the LanguageTool floor
    "sapling",            # pipeline.py — the Sapling grammar pass
    "low_confidence",     # pipeline.py — the below-gate confirm valve
    "continuity",         # continuity.py — the whole-book contradiction read
    "term_consistency",   # consistency.py CONSISTENCY_KEY
    "name_consistency",   # consistency.py NAME_KEY
})

# Every scripted sweep's key is free-form too. They are enumerated by
# `sweeps.SWEEPS` rather than listed here, so a new sweep needs no edit in this
# file — but `sweeps.py` is the only place allowed to mint one, which is what
# the prefix rule below asserts.
SWEEP_PREFIX = "sweep_"

# Tri-state. Deliberately three values: the two-valued version of this question
# is what produced the bug in the docstring.
RAN = "ran"
DID_NOT_RUN = "did-not-run"
UNKNOWN = "unknown"

# The config switch that turns each free-form label's pass on. Consulted only
# when the artifact carries no positive evidence — it is a statement about
# intent, not about what happened.
_SWITCH: dict[str, str] = {
    "languagetool": "languagetool",
    "sapling": "sapling",
    "continuity": "continuity",
    "term_consistency": "consistency",
    "name_consistency": "consistency",
}


def is_free_form(label: str) -> bool:
    """True if `label` is a pass's own tag rather than a configured error type.

    The one predicate every caller should use instead of testing membership in
    `cfg.error_type_keys` — that test answers a different question and answers
    it wrongly for exactly these labels."""
    return label in FREE_FORM or label.startswith(SWEEP_PREFIX)


def free_form_labels() -> frozenset[str]:
    """Every free-form label this build can actually mint, sweeps included."""
    from .sweeps import SWEEPS
    return FREE_FORM | frozenset(s.key for s in SWEEPS)


def will_produce(cfg, label: str, *, known_types=None) -> str:
    """Whether a run under `cfg` can produce findings labelled `label`.

    The pre-run half of the contract, for code that has to decide what to
    expect before anything has run — the eval corpus filter is the caller that
    matters. Every branch consults the switch that actually governs the pass in
    question; none of them consults `error_type_keys` about a pass that has no
    error type.

    `RAN` here means "will run", not "found something": a pass that runs and
    finds nothing is still a pass that ran, and its empty output is evidence
    rather than a gap.

    `known_types` is the registry's closed world — `error_registry.shipped_keys`
    of the run's error-type directory. Without it a label absent from
    `error_type_keys` cannot be told apart from a label nobody declared, so the
    answer is `UNKNOWN` rather than a guess in either direction."""
    if label.startswith(SWEEP_PREFIX):
        # `cfg.sweeps` is the authoritative list of sweeps this run performs.
        return RAN if label in (cfg.sweeps or ()) else DID_NOT_RUN
    if label == "low_confidence":
        return RAN if cfg.low_confidence.confirm else DID_NOT_RUN
    switch = _SWITCH.get(label)
    if switch is not None:
        return RAN if getattr(cfg, switch).enabled else DID_NOT_RUN
    # Not free-form, so it is a registry key and `error_type_keys` is exactly
    # the right question to ask — of a registry key, and of nothing else.
    if label in cfg.error_type_keys:
        return RAN
    if known_types is None:
        return UNKNOWN
    return DID_NOT_RUN if label in known_types else UNKNOWN


def produced_in(payload: Mapping[str, Any], label: str) -> str:
    """Whether the run recorded in a findings.json produced `label`, read in
    the order that makes a wrong negative impossible.

    The artifact-side half of the contract, and the one a scorer wants: given a
    findings.json (already parsed), say whether the pass behind `label` ran.

      1. The pass's own findings. A count in `stats_by_error_type` is proof,
         and it outranks every inference below — including any inference from
         `config.error_types`, which is the ordering the bug in the module
         docstring got backwards.
      2. The pass's own telemetry: the scripted-check rows for a sweep, the
         consistency block's `ran`, Sapling's character bill. A block that is
         present but shaped differently than expected degrades to `UNKNOWN` —
         a rename upstream must never read as "it did not run".
      3. Configuration, and only where configuration speaks: `config.sweeps`
         for a sweep, `config.error_types` for a registry key. For any other
         free-form label the configuration is silent and so are we.
    """
    if not isinstance(payload, Mapping):
        return UNKNOWN

    tally = payload.get("stats_by_error_type")
    if isinstance(tally, Mapping):
        if _positive(tally.get(label)):
            return RAN
    elif tally is not None:
        return UNKNOWN                       # a block we cannot read is not a no

    verdict = _from_telemetry(payload, label)
    if verdict is not None:
        return verdict

    config = payload.get("config")
    if not isinstance(config, Mapping):
        return UNKNOWN
    if label.startswith(SWEEP_PREFIX):
        sweeps = config.get("sweeps")
        if not isinstance(sweeps, list):
            return UNKNOWN
        return RAN if label in sweeps else DID_NOT_RUN
    if is_free_form(label):
        # `config.error_types` cannot contain this label whatever happened, so
        # reading it here would be reading a field that is empty by
        # construction and calling the emptiness an answer.
        return UNKNOWN
    configured = config.get("error_types")
    if not isinstance(configured, list):
        return UNKNOWN
    return RAN if label in configured else DID_NOT_RUN


def _from_telemetry(payload: Mapping[str, Any], label: str) -> str | None:
    """A verdict from the pass's own record in the artifact, or None to fall
    through. Only the four labels that have such a record answer here; the rest
    genuinely have none, which is why `produced_in` can end at `UNKNOWN`."""
    if label.startswith(SWEEP_PREFIX):
        rows = payload.get("scripted_checks")
        if not isinstance(rows, list) or not rows:
            return None
        keys = [r["key"] for r in rows
                if isinstance(r, Mapping) and isinstance(r.get("key"), str)]
        if len(keys) != len(rows):
            return UNKNOWN                   # rows we cannot read
        # A row is proof the sweep ran even at zero flagged, and the block
        # carries a row for every sweep the run performed — so a missing row is
        # a real negative here, unlike an empty `config.error_types`.
        return RAN if label in keys else DID_NOT_RUN
    if label in ("term_consistency", "name_consistency"):
        block = payload.get("consistency")
        if block is None:
            return None
        if not isinstance(block, Mapping) or not isinstance(block.get("ran"),
                                                            bool):
            return UNKNOWN
        return RAN if block["ran"] else DID_NOT_RUN
    if label == "sapling":
        usage = payload.get("usage")
        if isinstance(usage, Mapping) and _positive(usage.get("sapling_chars")):
            return RAN                       # it billed, so it ran
        return None                          # zero chars proves nothing
    return None


def _positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and value > 0
