"""Bespoke, book-scoped sweeps — the front door for a fix the agent writes.

A shipped sweep in :mod:`docproof.sweeps` handles a pathology the press sees in
every manuscript. But authors invent their own: one wraps every line of dialogue
in doubled quotes (``""hello""``), one types a scene break as five hyphens. A
human editor spots the pattern and does one find-replace; Galley's practitioner
does the same in kind — it writes a throwaway rule for *this* book and runs it as
a $0 deterministic sweep.

This module turns an agent-authored rule file into a :class:`~docproof.sweeps.Sweep`
that enters the exact same machinery as the built-ins (``run_sweep_objects`` →
``s-`` findings → tracked changes → the idempotency re-scan). Two forms:

* **Declarative** (``.yaml`` / ``.yml`` / ``.json``) — a regex ``pattern`` and a
  ``replacement``. Safe: no code runs. This is the common case and covers the
  doubled-quote example. Guarded against the ways a one-off regex goes wrong: it
  must compile, it may not match the empty string, its backreferences must exist,
  and a zero-width or identity match is dropped rather than emitted.
* **Python** (``.py``) — the escape hatch for a fix a regex can't express. The
  module defines ``SWEEP`` (a ready ``Sweep``) or a ``scan(text, variant)``
  callable plus ``KEY``/``NAME``. Loading it executes the file: it is trusted
  code the agent wrote and a human confirmed at the plan gate, the same trust
  boundary as the rest of the pipeline — never point it at a file you have not
  read.

Idempotency is not enforced here; it is *measured* downstream. ``run_sweep_objects``
re-scans after applying and reports ``remaining`` on the :class:`SweepReport`, and
the ``docproof sweep`` command refuses to ``--apply`` a rule whose ``remaining`` is
non-zero. So a rule that would fire forever is caught before it edits anything.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .sweeps import Hit, Sweep

log = logging.getLogger("docproof.custom_sweep")

# The regex flags a declarative rule may name, by their friendly names. Kept to a
# small allowlist so a rule file can't reach for anything surprising.
_FLAGS = {
    "ignorecase": re.IGNORECASE,
    "multiline": re.MULTILINE,
    "dotall": re.DOTALL,
    "verbose": re.VERBOSE,
}

_KEY_RE = re.compile(r"[a-z0-9_]+")
# Numeric backreferences in a replacement: \1, \g<1>. Named ones (\g<name>) are
# validated against the pattern's named groups separately.
_NUM_BACKREF = re.compile(r"\\(\d+)|\\g<(\d+)>")
_NAMED_BACKREF = re.compile(r"\\g<([A-Za-z_]\w*)>")


class RuleError(ValueError):
    """A rule file is malformed or unsafe to run. The message is for the human
    who wrote (or approved) the rule, so it names the specific problem."""


def load_rule(path: str | Path) -> Sweep:
    """Load a bespoke sweep rule from ``path`` and return a :class:`Sweep`.

    Dispatches on the file extension: ``.py`` is the Python escape hatch,
    everything else is parsed as a declarative regex rule (YAML if PyYAML can
    read it, else JSON). Raises :class:`RuleError` with a specific message on any
    malformed or unsafe rule.
    """

    p = Path(path)
    if not p.exists():
        raise RuleError(f"no such rule file: {p}")
    if p.suffix.lower() == ".py":
        return _load_python_rule(p)
    return _compile_regex_rule(_parse_data_rule(p), source=str(p))


# --- declarative regex rules --------------------------------------------------

def _parse_data_rule(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data: Any
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:  # pragma: no cover - PyYAML is a hard dependency
            raise RuleError("PyYAML is not installed; write the rule as JSON")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise RuleError(f"could not parse YAML: {e}")
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise RuleError(f"could not parse JSON: {e}")
    if not isinstance(data, dict):
        raise RuleError("a rule file must be a mapping with 'key', 'pattern', "
                        "and 'replacement'")
    return data


def _compile_regex_rule(rule: dict[str, Any], *, source: str = "") -> Sweep:
    """Turn a validated declarative rule into a :class:`Sweep`."""

    key = str(rule.get("key") or "").strip()
    if not key or not _KEY_RE.fullmatch(key):
        raise RuleError("'key' must be a non-empty identifier of [a-z0-9_] "
                        "(it labels the edit in the change log)")
    name = str(rule.get("name") or key).strip()
    explanation = str(rule.get("explanation") or name).strip()

    pattern = rule.get("pattern")
    replacement = rule.get("replacement")
    if not isinstance(pattern, str) or not pattern:
        raise RuleError("'pattern' must be a non-empty regular expression")
    if not isinstance(replacement, str):
        raise RuleError("'replacement' must be a string (it may be empty, for a "
                        "pure deletion)")

    flags = 0
    raw_flags = rule.get("flags") or []
    if not isinstance(raw_flags, (list, tuple)):
        raise RuleError("'flags' must be a list, e.g. [ignorecase]")
    for f in raw_flags:
        if f not in _FLAGS:
            raise RuleError(f"unknown flag {f!r}; allowed: "
                            f"{', '.join(sorted(_FLAGS))}")
        flags |= _FLAGS[f]

    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        raise RuleError(f"pattern does not compile: {e}")
    # A pattern that matches the empty string would fire at every position — a
    # runaway rule. Reject it up front rather than emit thousands of no-ops.
    if rx.match("") is not None:
        raise RuleError("pattern matches the empty string, so it would fire "
                        "everywhere; tighten it to target real text")
    _check_backrefs(rx, replacement)

    def scan(text: str, variant: Any = None) -> list[Hit]:
        hits: list[Hit] = []
        for m in rx.finditer(text):
            if m.start() == m.end():
                continue  # zero-width match — nothing to replace
            rep = m.expand(replacement)
            if text[m.start():m.end()] == rep:
                continue  # identity: the "fix" changes nothing, so don't file it
            hits.append(Hit(m.start(), m.end(), rep, explanation))
        return hits

    log.info("loaded bespoke sweep %r (%s) from %s", key, name, source or "rule")
    return Sweep(key=key, name=name, scan=scan)


def _check_backrefs(rx: "re.Pattern[str]", replacement: str) -> None:
    """Every backreference in ``replacement`` must name a real group in ``rx``.

    A typo'd ``\\2`` in a one-group pattern would otherwise raise deep inside the
    scan, mid-run; catching it here turns it into a clear rule error at load."""

    for m in _NUM_BACKREF.finditer(replacement):
        idx = int(m.group(1) or m.group(2))
        if idx > rx.groups:
            raise RuleError(
                f"replacement references group \\{idx}, but the pattern has "
                f"{rx.groups} capturing group(s)")
    for m in _NAMED_BACKREF.finditer(replacement):
        name = m.group(1)
        if name.isdigit():
            continue  # numeric \g<1> already handled above
        if name not in rx.groupindex:
            raise RuleError(
                f"replacement references group \\g<{name}>, which the pattern "
                f"does not define")


# --- python escape hatch ------------------------------------------------------

def _load_python_rule(path: Path) -> Sweep:
    """Import a ``.py`` rule and read a :class:`Sweep` out of it.

    Contract: the module defines either ``SWEEP`` (a ready ``Sweep``), or a
    ``scan(text, variant) -> list[Hit]`` callable plus module-level ``KEY`` and
    ``NAME`` strings. Importing runs the file — trusted, agent-authored,
    human-confirmed code only.
    """

    import importlib.util

    spec = importlib.util.spec_from_file_location(f"docproof_bespoke_{path.stem}",
                                                  path)
    if spec is None or spec.loader is None:
        raise RuleError(f"could not load {path} as a Python module")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:  # noqa: BLE001 - any import-time error is a rule error
        raise RuleError(f"the rule module failed to import: {e}")

    sweep = getattr(module, "SWEEP", None)
    if isinstance(sweep, Sweep):
        return sweep

    scan = getattr(module, "scan", None)
    if callable(scan):
        key = str(getattr(module, "KEY", path.stem)).strip()
        if not key or not _KEY_RE.fullmatch(key):
            raise RuleError("module KEY must be an identifier of [a-z0-9_]")
        name = str(getattr(module, "NAME", key)).strip()
        return Sweep(key=key, name=name, scan=scan)

    raise RuleError(
        f"{path.name} defines neither SWEEP nor a scan() function — a Python "
        "rule needs one of them (see docproof/custom_sweep.py)")


__all__ = ["RuleError", "load_rule"]
