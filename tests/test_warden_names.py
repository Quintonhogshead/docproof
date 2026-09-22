"""app/warden/names.py: the four-way name comparison, pure over a snapshot.
Fixtures cover the four verdicts; this file also probes the comparison logic
directly with small hand-built snapshots so a verdict's boundary is pinned
down, not just its headline case."""
from __future__ import annotations

import json
from pathlib import Path

from app.warden import names
from app.warden.rules import Finding

FIXTURES = Path(__file__).parent / "fixtures" / "warden"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text("utf-8"))


def one(snapshot: dict) -> names.NameFinding:
    results = names.compare(snapshot)
    assert len(results) == 1, results
    return results[0]


# -- fixture-driven verdicts --------------------------------------------------

def test_healthy_fixture_is_agree():
    result = one(load("healthy.json"))
    assert result.verdict == names.AGREE
    assert result.as_finding() is None


def test_names_fill_fixture():
    result = one(load("names_fill.json"))
    assert result.verdict == names.FILL
    assert result.tier == 0
    assert result.proposal == {"first": "Riley", "last": "Bishop"}
    finding = result.as_finding()
    assert isinstance(finding, Finding)
    assert finding.rule == "hubspot-name-suspect"
    assert finding.tier == 0
    assert finding.verbs == ("hubspot-fill-name",)
    assert finding.severity == "low"


def test_names_propose_fixture():
    result = one(load("names_propose.json"))
    assert result.verdict == names.PROPOSE
    assert result.tier == 1
    assert result.proposal == {"first": "Kris", "last": "Hamilton"}
    finding = result.as_finding()
    assert finding.tier == 1
    assert finding.verbs == ("hubspot-set-name",)
    assert finding.severity == "medium"


def test_names_conflict_fixture():
    result = one(load("names_conflict.json"))
    assert result.verdict == names.CONFLICT
    assert result.tier == 2
    finding = result.as_finding()
    assert finding.tier == 2
    assert finding.verbs == ()
    assert finding.severity == "high"


def test_as_findings_drops_agree_and_keeps_the_rest():
    all_results = (names.compare(load("healthy.json"))
                  + names.compare(load("names_fill.json"))
                  + names.compare(load("names_propose.json"))
                  + names.compare(load("names_conflict.json")))
    findings = names.as_findings(all_results)
    assert len(findings) == 3
    assert {f.rule for f in findings} == {"hubspot-name-suspect"}
    assert {f.tier for f in findings} == {0, 1, 2}


# -- hand-built boundary cases -------------------------------------------------

def _snapshot(project: dict, file_record: dict | None = None,
             submission: dict | None = None) -> dict:
    return {
        "hubspot": {"projects": [project],
                   "form_submissions": [submission] if submission else []},
        "docwatch": {"files": [file_record] if file_record else []},
    }


def test_all_four_sources_agree_is_agree_even_with_a_form_submission():
    snap = _snapshot(
        {"id": "p1", "author_first": "Jamie", "author_last": "Reeve"},
        {"hubspot_id": "p1", "author_first": "Jamie", "author_last": "Reeve",
         "subfolder_name": "Reeve, Jamie", "byline": "Jamie Reeve"},
        {"project_id": "p1", "firstname": "Jamie", "lastname": "Reeve"})
    result = one(snap)
    assert result.verdict == names.AGREE


def test_missing_sources_do_not_count_as_disagreement():
    """Only PRESENT sources are compared — a project with no matching Drive
    record and no form submission is `agree`, not `conflict`, because there
    is nothing to disagree about."""
    snap = _snapshot({"id": "p1", "author_first": "Jamie", "author_last": "Reeve"})
    result = one(snap)
    assert result.verdict == names.AGREE


def test_blank_hubspot_with_conflicting_other_sources_is_conflict():
    snap = _snapshot(
        {"id": "p1", "author_first": "", "author_last": ""},
        {"hubspot_id": "p1", "subfolder_name": "Reeve, Jamie", "byline": "Alex Carter"})
    result = one(snap)
    assert result.verdict == names.CONFLICT
    assert result.tier == 2


def test_folder_disagrees_with_hubspot_and_there_is_no_byline_to_confirm_it():
    """Folder alone disagreeing with HubSpot, with no byline to corroborate
    it, is a conflict — not enough agreement to safely propose."""
    snap = _snapshot(
        {"id": "p1", "author_first": "Morgan", "author_last": "Reyes"},
        {"hubspot_id": "p1", "subfolder_name": "Alvarez, Dana", "byline": ""})
    result = one(snap)
    assert result.verdict == names.CONFLICT


def test_name_key_folds_accents_and_case():
    snap = _snapshot(
        {"id": "p1", "author_first": "rené", "author_last": "GARCÍA"},
        {"hubspot_id": "p1", "subfolder_name": "Garcia, Rene", "byline": "Rene Garcia"})
    result = one(snap)
    assert result.verdict == names.AGREE


def test_project_with_no_id_is_skipped():
    snap = {"hubspot": {"projects": [{"author_first": "No", "author_last": "Id"}],
                        "form_submissions": []},
           "docwatch": {"files": []}}
    assert names.compare(snap) == []


def test_compare_never_raises_on_missing_sections():
    assert names.compare({}) == []
    assert names.compare({"hubspot": None, "docwatch": None}) == []
