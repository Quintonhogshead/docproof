"""B1 — the full-ladder adapter over the fixture book, on a fake provider."""

import json
import re
from pathlib import Path

import pytest

from docproof.config import load_config
from docproof.models import Usage
from docproof.providers import NormalizedUsage, ProviderResult

from galley.adapters import AdapterResult
from galley.adapters.docproof_ladder import (
    DocproofLadderAdapter,
    gfindings_from_json,
)
from galley.adapters import Scope
from tests.galley.fakes import make_manuscript

ROOT = Path(__file__).resolve().parents[2]
CONFIG = str(ROOT / "config" / "default.yaml")
FIXTURE = ROOT / "tests" / "fixtures" / "tiny_novel.docx"

_U = NormalizedUsage(input_tokens=20, output_tokens=8)


def _lean_cfg():
    """A self-contained, offline ladder config: core detector pass only."""
    cfg = load_config(CONFIG)
    cfg.error_types = [["spelling"]]
    cfg.audit = "off"  # don't let strict audit refuse to write the throwaway doc
    for path, attr in [
        (("languagetool",), "enabled"),
        (("sapling",), "enabled"),
        (("glossary",), "enabled"),
        (("adjudicate",), "enabled"),
        (("examination_graph",), "enabled"),
        (("candidate_screening",), "enabled"),
        (("candidate_screening",), "judgment_enabled"),
        (("consistency",), "enabled"),
    ]:
        obj = cfg
        for p in path:
            obj = getattr(obj, p, None)
            if obj is None:
                break
        if obj is not None and hasattr(obj, attr):
            setattr(obj, attr, False)
    return cfg


def _para_blocks(user: str) -> dict[str, str]:
    """Parse the reviewed paragraphs (after the context block) out of a prompt."""
    tail = user.split("</context>")[-1]
    return dict(re.findall(r'<paragraph id="([^"]+)">(.*?)</paragraph>', tail, re.S))


class _CorrectTeh:
    """Answers every chunk, flagging 'teh' -> 'the' wherever it appears."""

    name = "fake"

    def complete_structured(self, *, user, **kw) -> ProviderResult:
        findings = []
        for pid, text in _para_blocks(user).items():
            if "teh" in text:
                findings.append(
                    {
                        "para_id": pid,
                        "error_type": "spelling",
                        "original_text": text.strip(),
                        "corrected_text": text.strip().replace("teh", "the"),
                        "confidence": "high",
                        "explanation": "misspelling",
                    }
                )
        return ProviderResult(parsed={"findings": findings}, usage=_U)


class _AlwaysRefuse:
    name = "fake"

    def complete_structured(self, *, user, **kw) -> ProviderResult:
        return ProviderResult(stop_reason="refusal", error="no", usage=_U)


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture book missing")
def test_ladder_runs_and_finds_the_planted_typo(tmp_path):
    adapter = DocproofLadderAdapter(
        source_path=FIXTURE,
        cfg=_lean_cfg(),
        provider=_CorrectTeh(),
        wave=1,
        workspace=tmp_path,
    )
    usage = Usage()
    result = adapter.run(make_manuscript("ignored"), Scope(), budget_usd=100.0, usage=usage)

    assert isinstance(result, AdapterResult)
    # The model flagged the planted 'teh' as a spelling error; the deterministic
    # sweep independently caught the planted doubled word 'the the'. Both ride the
    # ladder into the union.
    types = {f.error_type for f in result.findings}
    assert "spelling" in types
    assert any(f.error_type.startswith("sweep") for f in result.findings)

    # Every ladder finding is well-formed and carries wave-1 ladder provenance.
    for f in result.findings:
        assert f.provenance and f.provenance.detector == "docproof_ladder"
        assert f.provenance.wave == 1
        assert f.span.para_id and f.span.end >= f.span.start >= 0
        # replacing the anchored span's find-text with replace-text is the fix;
        # the validator shrank each edit to its minimal diff.
        assert f.find != f.replace


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture book missing")
def test_ladder_threads_usage_and_cost(tmp_path):
    usage = Usage()
    adapter = DocproofLadderAdapter(
        source_path=FIXTURE, cfg=_lean_cfg(), provider=_CorrectTeh(), workspace=tmp_path
    )
    result = adapter.run(make_manuscript("x"), Scope(), budget_usd=100.0, usage=usage)
    # The fake reported usage on every call, so the shared object accreted it.
    assert usage.input_tokens > 0
    assert usage.api_calls > 0
    assert result.cost_usd >= 0.0


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture book missing")
def test_coverage_notes_appear_when_a_pass_degrades(tmp_path):
    adapter = DocproofLadderAdapter(
        source_path=FIXTURE, cfg=_lean_cfg(), provider=_AlwaysRefuse(), workspace=tmp_path
    )
    result = adapter.run(make_manuscript("x"), Scope(), budget_usd=100.0, usage=Usage())
    # Every model chunk refused -> the coverage ledger has gaps -> notes surface.
    assert result.coverage_notes, "a refused pass must leave a coverage note"
    assert any("gap" in n for n in result.coverage_notes)


# ---- unit: findings.json -> GFinding conversion is lossless -------------

def test_gfindings_from_json_lossless(tmp_path):
    payload = {
        "findings": [
            {
                "finding_id": "f-0001",
                "para_id": "body-0007",
                "error_type": "comma_splice",
                "confidence": "high",
                "explanation": "two clauses",
                "status": "validated",
                "anchor": {"start": 4, "end": 11, "delete_text": "sat down",
                           "insert_text": "sat down;"},
            },
            {
                "finding_id": "f-0002",
                "para_id": "body-0009",
                "error_type": "typo",
                "confidence": "medium",
                "explanation": "",
                "status": "validated",
                "anchor": None,  # unplaced -> dropped, counted
            },
            {
                "finding_id": "f-0003",
                "para_id": "body-0011",
                "error_type": "continuity",
                "confidence": "medium",
                "explanation": "possible break",
                "status": "query",  # anchored, but never applied -> dropped, counted
                "anchor": {"start": 0, "end": 5, "delete_text": "Later", "insert_text": ""},
            },
        ]
    }
    p = tmp_path / "findings.json"
    p.write_text(json.dumps(payload), encoding="utf-8")

    gfindings, dropped = gfindings_from_json(p, wave=3, model="claude-opus-5")
    # f-0002 (no anchor) and f-0003 (anchored, but status "query" not
    # "validated" — never applied as a tracked change) are both dropped.
    assert dropped == 2
    assert len(gfindings) == 1
    g = gfindings[0]
    assert g.id == "f-0001"
    assert g.error_type == "comma_splice"
    assert (g.span.para_id, g.span.start, g.span.end) == ("body-0007", 4, 11)
    assert g.find == "sat down" and g.replace == "sat down;"
    assert g.confidence == "high"
    assert g.provenance.wave == 3 and g.provenance.model == "claude-opus-5"
