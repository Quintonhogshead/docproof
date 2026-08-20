"""Human- and machine-readable examination coverage reports."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .examination_graph import ExaminationGraph
from .site_ledger import ExaminationLedger
from .site_models import PENDING_STATES, TERMINAL_STATES


def build_coverage_report(ledger: ExaminationLedger, graph: ExaminationGraph,
                          *, mode: str, omitted: dict[str, int] | None = None,
                          source: str = "") -> dict:
    ledger.assert_accounted()
    projection = ledger.projection()
    total = len(ledger)
    terminal = sum(state in TERMINAL_STATES for state in projection.values())
    pending = sum(state in PENDING_STATES for state in projection.values())
    by_generator = Counter(site.generator for site in ledger.sites)
    by_type = Counter(site.site_type for site in ledger.sites)
    state_by_type: dict[str, Counter] = defaultdict(Counter)
    for site in ledger.sites:
        state_by_type[site.site_type][projection[site.site_id].value] += 1
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "source": source,
        "accounting": {
            "generated_sites": total,
            "terminal_sites": terminal,
            "explicitly_pending_sites": pending,
            "terminal_percent": round((terminal / total * 100) if total else 100.0,
                                      2),
            "all_sites_have_state": len(projection) == total,
            "events": len(ledger.events),
        },
        "states": ledger.state_counts(),
        "site_types": dict(sorted(by_type.items())),
        "generators": dict(sorted(by_generator.items())),
        "states_by_site_type": {
            key: dict(sorted(counts.items()))
            for key, counts in sorted(state_by_type.items())
        },
        "graph": graph.counts(),
        "generation_omissions": dict(sorted((omitted or {}).items())),
        "scope": {
            "phase": 1,
            "shadow_only": True,
            "may_create_edits": False,
            "implemented": [
                "site and anchor models",
                "append-only ledger and current-state projection",
                "legacy sweep, spell, consistency, adjudication, and finding adapters",
                "paragraph-level model examination obligations",
                "context-sharing judgment packets",
                "exact verdict coverage validation",
                "sparse document/paragraph graph",
            ],
            "not_yet_implemented": [
                "lossless run/field/comment/footnote OOXML IR",
                "explicit site verdicts from the production model prompts",
                "entity, scene, event, and continuity graph edges",
                "rendered-page geometry and visual review",
                "site-derived manuscript edits (shadow mode cannot write edits)",
            ],
        },
    }


def write_coverage_json(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                    encoding="utf-8")


def write_coverage_markdown(path: Path, report: dict) -> None:
    a = report["accounting"]
    lines = [
        "# Examination graph shadow report",
        "",
        f"Mode: **{report['mode']}**. This layer observed the review and did "
        "not create or alter any manuscript edit.",
        "",
        f"- Generated sites: **{a['generated_sites']:,}**",
        f"- Terminal decisions: **{a['terminal_sites']:,}** "
        f"({a['terminal_percent']:.2f}%)",
        f"- Explicitly pending: **{a['explicitly_pending_sites']:,}**",
        f"- Append-only events: **{a['events']:,}**",
        f"- Every site has a recorded state: "
        f"**{'yes' if a['all_sites_have_state'] else 'NO'}**",
        "",
        "## Current states",
        "",
        "| State | Sites |",
        "|---|---:|",
    ]
    for state, count in report["states"].items():
        lines.append(f"| `{state}` | {count:,} |")
    lines += ["", "## Site types", "", "| Site type | Sites |",
              "|---|---:|"]
    for site_type, count in report["site_types"].items():
        lines.append(f"| `{site_type}` | {count:,} |")
    lines += ["", "## Generators", "", "| Generator | Sites |",
              "|---|---:|"]
    for generator, count in report["generators"].items():
        lines.append(f"| `{generator}` | {count:,} |")
    if report["generation_omissions"]:
        lines += ["", "## Explicit generation cap", "",
                  "The configured site cap was reached. These obligations were "
                  "not generated and are counted here rather than silently lost:", ""]
        for label, count in report["generation_omissions"].items():
            lines.append(f"- `{label}`: {count:,}")
    lines += ["", "## What the pending count means", "",
              "The current production reviewer returns findings, not an explicit "
              "pass/error verdict for every paragraph-level obligation. Shadow "
              "mode therefore leaves an absent finding as `needs_judgment`; it "
              "does not reinterpret silence as a pass. This is intentionally "
              "strict and shows the migration work still required.", "",
              "## Phase-one boundary", ""]
    for item in report["scope"]["not_yet_implemented"]:
        lines.append(f"- {item}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
