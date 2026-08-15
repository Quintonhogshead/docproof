"""Re-judge a manuscript that has already been reviewed.

A review is expensive and a judge gate is cheap, so the two should not be welded
together. This runs the gates — and only the gates — over a run that already
happened: the corrections are read back from what that run recorded, put to the
judges, and a fresh deliverable is written with the ones that fail withheld as
margin questions.

That makes three things possible that a full re-review does not:

  * a book proofread before these gates existed can be gated now, for the price
    of the gates alone rather than the price of the review again;
  * a gate can be tried on a finished book — different model, different
    instructions — and the result compared, without re-running detectors whose
    output would drift between runs anyway;
  * a run whose gates were off can have them switched on afterwards.

Nothing is re-detected and no detector call is made. The recorded findings are
re-validated against the manuscript exactly as `finish` would have, which is
deterministic — the same corrections, in the same order, claiming the same
spans — and then the gates run over the survivors. A re-judge of a run whose
gates were already on and are still on simply pays them again; the point is to
change what they are, or to add them where there were none.

The source manuscript has to be the one the run read. `findings.json` records
its path, and the content is re-ingested from it: a manuscript edited since the
original run has moved every offset, and the anchors would land in the wrong
place. That is a hard failure here rather than a silent misplacement.
"""
from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import replace
from pathlib import Path

from .checkpoint import finding_from_dict
from .config import Config
from .models import Finding, Usage
from .pipeline import Outputs, finish, prepare

log = logging.getLogger("docproof.rejudge")


class RejudgeError(Exception):
    """A re-judge could not be set up — carries the message a person sees."""


# A findings.json row is `dataclasses.asdict(finding)` plus whatever reporting
# hangs off it ("applied" today). Derived from the dataclass rather than listed
# by hand: a hand-kept list of the reporting keys is a list that rots, and it
# rots into a TypeError on a button press — every re-judge of every run written
# after the new key appeared, for a key that was never a Finding field and was
# always safe to drop. Asking Finding what its fields are cannot fall behind
# reporting.py, because reporting.py is not the one being asked.
_FIELDS = frozenset(f.name for f in dataclasses.fields(Finding))


def read_run(results_dir: str | Path) -> tuple[list, str]:
    """The findings a finished run recorded, and the manuscript it read.

    Raises RejudgeError with something a person can act on: this is reached from
    a button, and "no findings.json" needs to say which folder it looked in."""
    path = Path(results_dir, "findings.json")
    try:
        payload = json.loads(path.read_text("utf-8"))
    except OSError as e:
        raise RejudgeError(
            f"No findings.json in {results_dir} — a re-judge needs the record "
            f"the original run left behind ({e}).") from e
    except json.JSONDecodeError as e:
        raise RejudgeError(f"{path} is not readable JSON ({e}).") from e

    rows = payload.get("findings")
    if not isinstance(rows, list):
        raise RejudgeError(f"{path} has no findings to re-judge.")
    findings = []
    for row in rows:
        try:
            findings.append(finding_from_dict(
                {k: v for k, v in row.items() if k in _FIELDS}))
        except TypeError as e:                # a record missing a Finding field
            raise RejudgeError(
                f"{path} holds a finding this build does not understand "
                f"({e}).") from e
    return findings, str(payload.get("source") or "")


def _gates_only(cfg: Config) -> Config:
    """A copy of `cfg` with every paid pass off except the judge gates.

    "The gates and nothing else" has to be enforced, not merely intended:
    `prepare` and `finish` both still spend on their own account, and a re-judge
    inherits the ORIGINAL run's config, so a book reviewed with Sapling on would
    silently pay Sapling again — for suggestions the recorded findings already
    contain, which would then be applied twice.

      * the story sheet is a whole-book read inside `prepare`, and feeds detector
        prompts this mode never runs;
      * Sapling is a per-character bill inside `finish`;
      * the low-confidence valve and the ensemble overseer are model calls inside
        `finish`;
      * smoothing is a whole-manuscript read plus a judge inside `finish`, and
        its suggestions are already in the record — paying again would buy a
        second, differently-worded set of the same questions.

    Everything left free — the sweeps, the spell scan, the consistency scan —
    stays on, because `finish` needs a prepared document either way and those
    cost nothing. Their FINDINGS still come from the record (see `rejudge`)."""
    out = cfg.model_copy(deep=True)
    out.storysheet.enabled = False
    out.sapling.enabled = False
    out.smoothing.enabled = False
    # Chapter continuity is a per-chapter read plus a judge inside finish(), the
    # same shape as smoothing — and unlike the whole-book continuity read (which
    # runs pre-finish and so is never reached here), it WOULD re-fire on a
    # re-judge and buy a second, differently-worded set of the same questions.
    out.chapter_continuity.enabled = False
    out.low_confidence.confirm = False
    out.ensemble.detectors = []
    out.ensemble.verifier_model = None
    # A re-judge writes its own deliverable; the change log and reports describe
    # this run, which is the point of running it.
    return out


def rejudge(cfg: Config, results_dir: str | Path, *, out_dir: str | Path,
            error_dir: str | Path, source: str | Path | None = None,
            usage: Usage | None = None) -> Outputs:
    """Run the enabled judge gates over a finished run and write a deliverable.

    `source` overrides the manuscript path recorded in findings.json, for a run
    whose file has since moved. Everything else comes off the record.

    The sweeps and the consistency scan are emptied on the prepared document and
    taken from the record instead: they already ran, their findings are in there,
    and re-adding the fresh ones would apply every house-style fix twice."""
    if not (cfg.meaning_check.enabled or cfg.fix_check.enabled):
        raise RejudgeError(
            "Nothing to do: switch on the meaning check, the fix check, or "
            "both. A re-judge runs the gates and nothing else.")
    cfg = _gates_only(cfg)

    findings, recorded = read_run(results_dir)
    src = Path(source or recorded)
    if not src.is_file():
        raise RejudgeError(
            f"The manuscript this run read is not where it was: {src}. Point "
            f"the re-judge at the file it reviewed.")

    prepared = prepare(cfg, src, error_dir)
    # The record already contains the sweep and consistency edits this run made;
    # a fresh prepare has just generated them again. Empty the fresh ones so the
    # recorded set is the only one, exactly as the multi-round finalizer does.
    prepared = replace(prepared, sweep_findings=[], consistency_findings=[])
    log.info("Re-judging %d recorded finding(s) from %s", len(findings),
             results_dir)
    return finish(prepared, findings, usage if usage is not None else Usage(),
                  cfg, out_dir=out_dir, source_path=src)
