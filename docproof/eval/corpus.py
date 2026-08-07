"""Load and validate the eval corpus, and guard against test-set leakage.

A case is one paragraph with a label: either a seeded error (a span that should
be edited, and the correction we expect) or `clean` — a trap the model must
leave alone. Cases live in `eval/cases/<error_type>.yaml`, one file per type,
mirroring `config/error_types/`. A type's traps live in its own file as
`expect: clean` cases, so the paragraphs that look like a `tense_shift` but
aren't are scored right next to the ones that are.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIDENCES = ("low", "medium", "high")


@dataclass(frozen=True)
class Case:
    id: str            # unique across the whole corpus, e.g. "ts-001"
    error_type: str    # the type this file scores, e.g. "tense_shift"
    text: str          # the paragraph the model sees
    # A trap is is_clean=True with span/correction/confidence all None. A seeded
    # error carries at least a span; correction and confidence are optional
    # secondary labels.
    is_clean: bool
    span: str | None = None
    correction: str | None = None
    confidence: str | None = None
    source_file: str = ""   # provenance, for error messages


class CorpusError(ValueError):
    """A malformed case file, a duplicate id, or a calibration-example leak."""


def _norm(text: str) -> str:
    """Whitespace- and case-folded, for leak comparison only. Two paragraphs
    that differ solely in spacing or capitalization are the same case for the
    purpose of catching a calibration example that snuck into the corpus."""
    return " ".join(text.split()).casefold()


def load_case_file(path: Path) -> list[Case]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise CorpusError(f"{path}: expected a mapping at the top level")
    etype = data.get("error_type")
    if not etype or not isinstance(etype, str):
        raise CorpusError(f"{path}: missing or non-string 'error_type'")
    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise CorpusError(f"{path}: 'cases' must be a non-empty list")

    cases: list[Case] = []
    for i, c in enumerate(raw_cases):
        where = f"{path} case #{i} ({c.get('id', '?') if isinstance(c, dict) else '?'})"
        if not isinstance(c, dict):
            raise CorpusError(f"{where}: not a mapping")
        cid = c.get("id")
        if not cid or not isinstance(cid, str):
            raise CorpusError(f"{where}: missing or non-string 'id'")
        text = c.get("text")
        if not text or not isinstance(text, str):
            raise CorpusError(f"{where}: missing or non-string 'text'")
        expect = c.get("expect")

        if expect == "clean":
            cases.append(Case(id=cid, error_type=etype, text=text,
                              is_clean=True, source_file=str(path)))
            continue
        if not isinstance(expect, dict):
            raise CorpusError(
                f"{where}: 'expect' must be the literal 'clean' or a mapping "
                f"with at least a 'span'")
        span = expect.get("span")
        if not span or not isinstance(span, str):
            raise CorpusError(f"{where}: seeded error needs a non-empty 'span'")
        if span not in text:
            raise CorpusError(
                f"{where}: span {span!r} does not occur in the case text; the "
                f"span must be a verbatim substring so it can be located")
        correction = expect.get("correction")
        if correction is not None and not isinstance(correction, str):
            raise CorpusError(f"{where}: 'correction' must be a string")
        conf = expect.get("confidence")
        if conf is not None and conf not in CONFIDENCES:
            raise CorpusError(
                f"{where}: 'confidence' must be one of {CONFIDENCES}, not {conf!r}")
        cases.append(Case(id=cid, error_type=etype, text=text, is_clean=False,
                          span=span, correction=correction, confidence=conf,
                          source_file=str(path)))
    return cases


def load_corpus(cases_dir: str | Path) -> list[Case]:
    """Every case under `cases_dir`, in a stable order (file name, then file
    order). Raises CorpusError on a duplicate id across the whole corpus — ids
    are the join key to findings, so a collision would silently mis-score."""
    cases_dir = Path(cases_dir)
    files = sorted(p for p in cases_dir.glob("*.yaml")
                   if not p.name.startswith("_"))
    if not files:
        raise CorpusError(f"no case files found in {cases_dir}")
    all_cases: list[Case] = []
    seen: dict[str, str] = {}
    for path in files:
        for case in load_case_file(path):
            if case.id in seen:
                raise CorpusError(
                    f"duplicate case id {case.id!r} in {path} — already used in "
                    f"{seen[case.id]}")
            seen[case.id] = str(path)
            all_cases.append(case)
    return all_cases


def calibration_texts(error_dir: str | Path) -> set[str]:
    """Every paragraph shown to the model as a calibration example, normalized.
    These are the texts the corpus must NOT reuse: the model has already been
    handed the answer for them."""
    error_dir = Path(error_dir)
    texts: set[str] = set()
    for path in error_dir.glob("*.yaml"):
        if path.name.startswith("_"):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        for ex in data.get("examples", []) or []:
            if isinstance(ex, dict) and isinstance(ex.get("text"), str):
                texts.add(_norm(ex["text"]))
    return texts


def check_no_leakage(cases: list[Case], error_dir: str | Path) -> None:
    """Raise if any case reuses a calibration example's text. Scoring against a
    paragraph the model was shown in its prompt measures memorization, not
    accuracy."""
    shown = calibration_texts(error_dir)
    leaked = [c for c in cases if _norm(c.text) in shown]
    if leaked:
        detail = "; ".join(f"{c.id} ({c.source_file})" for c in leaked[:5])
        more = f" and {len(leaked) - 5} more" if len(leaked) > 5 else ""
        raise CorpusError(
            f"{len(leaked)} case(s) reuse a shipped calibration example, which "
            f"leaks the test set into the prompt: {detail}{more}")
