"""Re-judging a review that already ran.

The promise is narrow and has to hold exactly: the judge gates run, nothing else
does, and nothing else is paid for. A manuscript proofread before the gates
existed can be gated afterwards for the price of the gates alone — which is only
true if `prepare` and `finish` are stopped from spending on their own account.
See docproof/rejudge.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import docx
import pytest

import docproof.providers as providers_mod
from docproof.config import load_config
from docproof.models import Finding, Usage
from docproof.pipeline import finish, prepare
from docproof.providers.base import ProviderResult
from docproof.rejudge import RejudgeError, read_run, rejudge

from .fakes import USAGE, FakeProvider

ERRORS = "config/error_types"
PARA = "He could not have known the road was barred."


def _verdicts(*items) -> ProviderResult:
    return ProviderResult(parsed={"verdicts": list(items)}, usage=USAGE)


def _book(tmp_path, text=PARA) -> Path:
    d = docx.Document()
    d.add_paragraph(text)
    src = tmp_path / "book.docx"
    d.save(src)
    return src


def _first_run(tmp_path, corrected="He could have known the road was barred.",
               **over):
    """A finished review with one correction and no gates — the thing a
    re-judge is pointed at."""
    src = _book(tmp_path)
    cfg = load_config("config/default.yaml")
    cfg.audit = "off"
    for k, v in over.items():
        obj, _, field = k.rpartition(".")
        target = cfg
        for part in obj.split("."):
            target = getattr(target, part)
        setattr(target, field, v)
    prepared = prepare(cfg, src, ERRORS)
    f = Finding(finding_id="f-1", chunk_id="c0", para_id="body-0000",
                error_type="homophone_confusion", original_text=PARA,
                occurrence=1, corrected_text=corrected,
                explanation="dropped negation", confidence="high")
    out = finish(prepared, [f], Usage(), cfg, out_dir=tmp_path / "run1",
                 source_path=src)
    return src, out


def _rejudged(tmp_path, monkeypatch, results, *, gates=("meaning",),
              usage=None, **over):
    cfg = load_config("config/default.yaml")
    cfg.audit = "off"
    for name in gates:
        getattr(cfg, f"{name}_check").enabled = True
    for k, v in over.items():
        obj, _, field = k.rpartition(".")
        target = cfg
        for part in obj.split("."):
            target = getattr(target, part)
        setattr(target, field, v)
    provider = FakeProvider(list(results))
    monkeypatch.setattr(providers_mod, "build_provider", lambda _c: provider)
    out = rejudge(cfg, tmp_path / "run1", out_dir=tmp_path / "run2",
                  error_dir=ERRORS, usage=usage)
    return out, provider


# --- the point of the thing --------------------------------------------------

def test_a_run_with_no_gates_can_be_gated_afterwards(tmp_path, monkeypatch):
    """The whole feature in one test: a review that applied a meaning-changing
    correction, re-judged later, holds it back — without reviewing again."""
    _, first = _first_run(tmp_path)
    assert first.applied == 1                    # it shipped, ungated

    out, provider = _rejudged(tmp_path, monkeypatch, [_verdicts(
        {"item": 1, "verdict": "withhold", "reason": "Drops the negation."})])
    assert out.applied == 0
    assert len(provider.calls) == 1              # the judge, and nothing else
    summary = (tmp_path / "run2" / "summary.md").read_text("utf-8")
    assert "Drops the negation." in summary


def test_an_approved_change_still_ships(tmp_path, monkeypatch):
    _first_run(tmp_path)
    out, _ = _rejudged(tmp_path, monkeypatch,
                       [_verdicts({"item": 1, "verdict": "keep", "reason": ""})])
    assert out.applied == 1


def test_the_original_deliverable_is_left_alone(tmp_path, monkeypatch):
    """A re-judge writes its own output. Comparing the two is the reason to run
    it, so overwriting the first would defeat the point."""
    _, first = _first_run(tmp_path)
    before = first.reviewed_path.read_bytes()
    _rejudged(tmp_path, monkeypatch, [_verdicts(
        {"item": 1, "verdict": "withhold", "reason": "no"})])
    assert first.reviewed_path.read_bytes() == before


def test_both_gates_can_run_over_a_finished_review(tmp_path, monkeypatch):
    _first_run(tmp_path)
    out, provider = _rejudged(
        tmp_path, monkeypatch,
        [_verdicts({"item": 1, "verdict": "keep", "reason": ""}),
         _verdicts({"item": 1, "verdict": "withhold",
                    "reason": "Wrong homophone."})],
        gates=("meaning", "fix"))
    assert len(provider.calls) == 2
    assert out.applied == 0
    summary = (tmp_path / "run2" / "summary.md").read_text("utf-8")
    assert "The meaning check" in summary and "The fix check" in summary


# --- "the gates and nothing else" --------------------------------------------

def test_nothing_else_is_paid_for(tmp_path, monkeypatch):
    """A re-judge inherits the ORIGINAL run's config. If that had Sapling, the
    story sheet or the low-confidence valve on, they must not run again: the
    recorded findings already contain their results, and paying twice for them
    is exactly what this mode promises not to do."""
    monkeypatch.setenv("SAPLING_API_KEY", "would-bill-if-called")
    _first_run(tmp_path)
    usage = Usage()
    out, provider = _rejudged(
        tmp_path, monkeypatch,
        [_verdicts({"item": 1, "verdict": "keep", "reason": ""})],
        usage=usage,
        **{"sapling.enabled": True, "storysheet.enabled": True,
           "low_confidence.confirm": True})
    assert len(provider.calls) == 1              # the judge only
    assert usage.api_calls == 1
    assert usage.sapling_chars == 0 and usage.sapling_cost == 0.0
    assert out.applied == 1


def test_running_with_no_gate_on_is_refused(tmp_path):
    _first_run(tmp_path)
    cfg = load_config("config/default.yaml")
    with pytest.raises(RejudgeError, match="Nothing to do"):
        rejudge(cfg, tmp_path / "run1", out_dir=tmp_path / "run2",
                error_dir=ERRORS)


# --- reading the record ------------------------------------------------------

def test_the_record_round_trips(tmp_path):
    src, _ = _first_run(tmp_path)
    findings, recorded = read_run(tmp_path / "run1")
    assert [f.finding_id for f in findings] == ["f-1"]
    assert Path(recorded) == src
    # `applied` is reporting, not a Finding field — loading must strip it.
    raw = json.loads((tmp_path / "run1" / "findings.json").read_text("utf-8"))
    assert "applied" in raw["findings"][0]


def test_a_missing_record_says_which_folder(tmp_path):
    with pytest.raises(RejudgeError, match="findings.json"):
        read_run(tmp_path / "nowhere")


def test_unreadable_json_is_a_clean_error(tmp_path):
    (tmp_path / "findings.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(RejudgeError, match="readable JSON"):
        read_run(tmp_path)


def test_a_moved_manuscript_fails_rather_than_guessing(tmp_path, monkeypatch):
    """Offsets are anchored to the file the run read. A missing or swapped
    manuscript must stop the re-judge, not land the anchors somewhere else."""
    src, _ = _first_run(tmp_path)
    src.unlink()
    cfg = load_config("config/default.yaml")
    cfg.meaning_check.enabled = True
    with pytest.raises(RejudgeError, match="not where it was"):
        rejudge(cfg, tmp_path / "run1", out_dir=tmp_path / "run2",
                error_dir=ERRORS)


def test_a_relocated_manuscript_can_be_named(tmp_path, monkeypatch):
    src, _ = _first_run(tmp_path)
    moved = tmp_path / "moved.docx"
    src.rename(moved)
    cfg = load_config("config/default.yaml")
    cfg.audit = "off"
    cfg.meaning_check.enabled = True
    provider = FakeProvider([_verdicts(
        {"item": 1, "verdict": "keep", "reason": ""})])
    monkeypatch.setattr(providers_mod, "build_provider", lambda _c: provider)
    result = rejudge(cfg, tmp_path / "run1", out_dir=tmp_path / "run2",
                     error_dir=ERRORS, source=moved)
    assert result.applied == 1


# --- the sweeps are not applied twice ----------------------------------------

def test_house_style_sweeps_are_not_reapplied(tmp_path, monkeypatch):
    """The record already holds the sweep edits the first run made, and a fresh
    prepare has just generated them again. Only one set may reach the file."""
    src = _book(tmp_path, "He waited… and waited.")
    cfg = load_config("config/default.yaml")
    cfg.audit = "off"
    prepared = prepare(cfg, src, ERRORS)
    first = finish(prepared, [], Usage(), cfg, out_dir=tmp_path / "run1",
                   source_path=src)
    assert first.applied >= 1                    # the nbsp sweep fired

    provider = FakeProvider()
    monkeypatch.setattr(providers_mod, "build_provider", lambda _c: provider)
    cfg2 = load_config("config/default.yaml")
    cfg2.audit = "off"
    cfg2.meaning_check.enabled = True
    again = rejudge(cfg2, tmp_path / "run1", out_dir=tmp_path / "run2",
                    error_dir=ERRORS)
    assert again.applied == first.applied        # the same edits, not double
    assert provider.calls == []                  # sweeps are not judged
