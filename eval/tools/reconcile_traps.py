#!/usr/bin/env python3
"""Run the real review pipeline over the smoothing trap set, N times, and print
the trap x error_type x runs-seen table the trap file's DETECTOR RECONCILIATION
section is written from.

DEV TOOLING. Not shipped in the wheel, never imported by the pipeline, and not
reachable from `docproof.eval.corpus.load_corpus` — that globs `eval/cases/
*.yaml` non-recursively, and this is a `.py` under `eval/tools/`. It writes
nothing into the repo: `--out` defaults to a directory under the system temp
dir.

WHY THIS EXISTS. The trap file's header used to claim its reconciliation
section "is regenerated whenever a passage changes rather than being
inherited", while the script that produced it lived in a scratchpad that did
not survive the session. The claim was therefore unfalsifiable and, for the
next person, unrepeatable. This file is that claim made true.

WHY MORE THAN ONE RUN. The detector is stochastic: observed finding counts over
the identical 88 passages have ranged from 14 to 22, and no two runs have ever
returned the same set. A single run cannot support a completeness claim in
either direction, so the tool takes `--runs` and reports, for every
(trap, error_type) pair, how many runs of N surfaced it. Six is the floor worth
quoting; the table prints the denominator so no number can be read without it.

WHY THE MODEL IS PRINTED IN EVERY HEADER LINE. Findings do not carry between
models (`eval/tools/anchor_dump.py` exists because the accuracy baseline put
claude-haiku-4-5 at a 29% anchor-failure rate against gpt-5.6-luna's 0%), so a
reconciliation is a claim about ONE model. The tool takes `--model` so a
stand-in can be used deliberately, and stamps the model, the effort and whether
that model is `config/default.yaml`'s configured detector onto the report and
the JSON, so a table can never be quoted without the model it was measured on.

    python -m eval.tools.reconcile_traps --runs 6
    python -m eval.tools.reconcile_traps --runs 6 --model gpt-5.6-luna \
        --effort medium --out /tmp/trap-recon
    python -m eval.tools.reconcile_traps --report /tmp/trap-recon  # re-print

Costs real API money — about five cents per run on gpt-5.6-luna at these
lengths, more on a pricier detector. `--runs 0` validates the wiring and prints
the config it would use without calling anything.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

# eval/tools/reconcile_traps.py -> eval/tools -> eval -> repo root
REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from docproof.config import load_config                     # noqa: E402
from docproof.eval.corpus import Case                       # noqa: E402
from docproof.eval.runner import run_eval                   # noqa: E402
from docproof.providers import build_provider               # noqa: E402
from docproof.providers.catalog import estimate_cost        # noqa: E402

from eval.tools.style_traps import DEFAULT_TRAPS, load_traps  # noqa: E402

DEFAULT_CONFIG = REPO / "config" / "default.yaml"

# A finding with one of these statuses has reached, or would reach, an author:
# a tracked change or a margin query. Everything else was thrown out by
# `validate_findings` and nobody will ever see it — which is a materially
# different fact about the file, so the table reports the two separately
# instead of summing them.
REACHED_AUTHOR = ("validated", "query")

# `error_type` on the Case is required by the corpus dataclass and is unused
# here: the trap file has no per-passage expected type, and nothing in this
# tool scores against one. A single obvious placeholder is clearer than
# inventing a plausible-looking type per passage that a reader might mistake
# for a label.
CASE_ERROR_TYPE = "smoothing_trap"


def _cases(traps) -> list[Case]:
    return [Case(id=t.id, error_type=CASE_ERROR_TYPE, text=t.text,
                 is_clean=True) for t in traps]


def _run_once(cfg, cases, error_dir, work_dir) -> tuple[list[dict], float]:
    """One pipeline pass. Returns one row per finding, already keyed to a trap
    id rather than to a paragraph id, and the run's estimated dollar cost —
    which the report prints, because a reconciliation nobody can budget for is
    a reconciliation nobody reruns."""
    provider = build_provider(cfg)
    run = run_eval(cfg, cases, error_dir, work_dir, provider=provider)
    cost = estimate_cost(cfg.api.model,
                         input_tokens=run.usage.input_tokens,
                         output_tokens=run.usage.output_tokens,
                         effort=cfg.api.effort) or 0.0
    rows = []
    for f in run.findings:
        rows.append({
            "trap": run.built.para_to_case.get(f.para_id, f.para_id),
            "error_type": f.error_type,
            "confidence": f.confidence,
            "status": f.status,
            # The span and the reason, kept because the table alone cannot
            # settle the file's FIX-vs-TAG question: "arch-09 drew a
            # subject_verb_agreement" does not say WHICH verb, and the answer
            # decides whether the passage is repunctuated or declared.
            "original_text": f.original_text,
            "corrected_text": f.corrected_text,
            "explanation": f.explanation,
        })
    return rows, cost


def _aggregate(runs: list[list[dict]]) -> list[dict]:
    """Collapse per-run findings into one row per (trap, error_type), counting
    the RUNS that surfaced it rather than the findings — a run that returns the
    same type twice on one passage is still one run of evidence that the type
    reaches that passage."""
    reached: dict[tuple[str, str], set[int]] = defaultdict(set)
    rejected: dict[tuple[str, str], set[int]] = defaultdict(set)
    confidences: dict[tuple[str, str], set[str]] = defaultdict(set)
    statuses: dict[tuple[str, str], set[str]] = defaultdict(set)
    for i, rows in enumerate(runs):
        for r in rows:
            key = (r["trap"], r["error_type"])
            (reached if r["status"] in REACHED_AUTHOR else rejected)[key].add(i)
            confidences[key].add(r["confidence"])
            statuses[key].add(r["status"])
    out = []
    for key in sorted(set(reached) | set(rejected)):
        trap, etype = key
        out.append({
            "trap": trap,
            "error_type": etype,
            "runs_reached_author": len(reached[key]),
            "runs_rejected_only": len(rejected[key] - reached[key]),
            "confidences": sorted(confidences[key]),
            "statuses": sorted(statuses[key]),
        })
    return out


def _report(data: dict) -> str:
    lines = []
    n = data["runs"]
    lines.append(
        f"DETECTOR RECONCILIATION — model {data['model']}, effort "
        f"{data['effort'] or '(none)'}, {n} run(s)")
    lines.append(
        f"  is_configured_detector: {data['is_configured_detector']} "
        f"(config/default.yaml api.model = {data['configured_model']})")
    lines.append(f"  findings per run: {data['findings_per_run']}")
    if data.get("estimated_cost_usd") is not None:
        lines.append(f"  estimated cost: ${data['estimated_cost_usd']:.4f}")
    lines.append("")
    declared = data["declared_overlap"]
    lines.append(f"{'trap':10} {'error_type':26} {'seen':>6}  {'rej':>4}  "
                 f"{'conf':16} declared")
    for row in data["table"]:
        tags = declared.get(row["trap"], [])
        mark = "yes" if row["error_type"] in tags else "NO <-- UNDECLARED"
        lines.append(
            f"{row['trap']:10} {row['error_type']:26} "
            f"{row['runs_reached_author']:>3}/{n:<2}  "
            f"{row['runs_rejected_only']:>4}  "
            f"{','.join(row['confidences']):16} {mark}")
    lines.append("")
    undeclared_seen = [r for r in data["table"]
                       if r["runs_reached_author"]
                       and r["error_type"] not in declared.get(r["trap"], [])]
    lines.append(f"undeclared types that REACHED AN AUTHOR: "
                 f"{len(undeclared_seen)}")
    for r in undeclared_seen:
        lines.append(f"    {r['trap']} -> {r['error_type']} "
                     f"({r['runs_reached_author']}/{n}, "
                     f"{','.join(r['confidences'])})")
    rejected_only = [r for r in data["table"] if not r["runs_reached_author"]]
    lines.append(f"findings thrown out before any author saw them: "
                 f"{len(rejected_only)}")
    for r in rejected_only:
        lines.append(f"    {r['trap']} -> {r['error_type']} "
                     f"({r['runs_rejected_only']}/{n}, "
                     f"{','.join(r['confidences'])}, {','.join(r['statuses'])})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--traps", default=str(DEFAULT_TRAPS))
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--runs", type=int, default=6,
                    help="how many pipeline passes. The detector is stochastic; "
                         "6 is the floor worth quoting. 0 = wiring check, no API "
                         "call")
    ap.add_argument("--model", default="",
                    help="override config/default.yaml's api.model. Use only "
                         "deliberately: the report records that the run was not "
                         "against the configured detector")
    ap.add_argument("--effort", default="",
                    help="override api.effort (a model with supports_effort="
                         "False has none)")
    ap.add_argument("--out", default="",
                    help="where per-run JSON and the report are written; "
                         "defaults to a directory under the system temp dir, "
                         "never inside the repo")
    ap.add_argument("--only", default="",
                    help="comma-separated trap ids: review just those passages. "
                         "A DIAGNOSTIC, not a reconciliation — a shorter "
                         "document is a different context and its counts are "
                         "not comparable to a whole-file run")
    ap.add_argument("--report", default="",
                    help="skip the runs and re-print the report from a previous "
                         "--out directory")
    args = ap.parse_args(argv)

    if args.report:
        data = json.loads(
            (Path(args.report) / "reconciliation.json").read_text("utf-8"))
        print(_report(data))
        return 0

    traps = load_traps(args.traps)
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        unknown = wanted - {t.id for t in traps}
        if unknown:
            raise SystemExit(f"unknown trap id(s): {', '.join(sorted(unknown))}")
        traps = [t for t in traps if t.id in wanted]
    cfg = load_config(args.config)
    configured_model = cfg.api.model
    if args.model:
        cfg.api.model = args.model
    if args.effort:
        cfg.api.effort = args.effort
    # Offsets have to survive so a finding lands on the paragraph that produced
    # it. The trap file's own `normalization: on` contract is about the
    # SMOOTHING pass, not about this mechanical reconciliation.
    cfg.normalize.quotes = False
    cfg.normalize.spaces = False
    error_dir = str(Path(args.config).parent / "error_types")

    out = Path(args.out) if args.out else (
        Path(tempfile.gettempdir()) / "docproof-trap-reconcile")
    # "never inside the repo" was a promise in the --out help and in this
    # module's docstring, and nothing enforced it: `--out eval/anything` wrote
    # per-run JSON straight into the working tree. Refuse instead of documenting.
    if out.resolve().is_relative_to(REPO):
        raise SystemExit(
            f"--out must not be inside the repo: {out.resolve()} is under "
            f"{REPO}. Per-run JSON carries the trap passages and, once a real "
            f"manuscript is reconciled this way, its text; write it to a temp "
            f"directory (the default) or anywhere outside the checkout.")
    out.mkdir(parents=True, exist_ok=True)

    print(f"{len(traps)} passages, {args.runs} run(s), model {cfg.api.model} "
          f"(effort {cfg.api.effort}), configured detector is "
          f"{configured_model}", flush=True)
    if args.runs < 1:
        return 0

    cases = _cases(traps)
    runs: list[list[dict]] = []
    spend = 0.0
    for i in range(args.runs):
        rows, cost = _run_once(cfg, cases, error_dir, out / f"run-{i:02d}")
        runs.append(rows)
        spend += cost
        (out / f"run-{i:02d}.json").write_text(
            json.dumps(rows, indent=2), encoding="utf-8")
        print(f"  run {i + 1}/{args.runs}: {len(rows)} finding(s), "
              f"${cost:.4f}", flush=True)

    data = {
        "model": cfg.api.model,
        "effort": cfg.api.effort or "",
        "configured_model": configured_model,
        "is_configured_detector": cfg.api.model == configured_model,
        "runs": args.runs,
        "estimated_cost_usd": round(spend, 4),
        "findings_per_run": [len(r) for r in runs],
        "declared_overlap": {t.id: list(t.mechanical_overlap) for t in traps},
        "table": _aggregate(runs),
    }
    (out / "reconciliation.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8")
    report = _report(data)
    (out / "reconciliation.txt").write_text(report + "\n", encoding="utf-8")
    print()
    print(report)
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
