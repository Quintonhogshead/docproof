"""B2 — the single-pass adapter: one group, a scoped span set, a fake provider.

Money is fenced: every provider here is an inline fake. The fixture book is the
same ``tiny_novel.docx`` B1 uses; its Chapter Three paragraph ``body-0028`` holds
the planted ``teh`` typo.
"""

import re
from pathlib import Path

import pytest

from docproof.config import load_config
from docproof.models import Usage
from docproof.providers import NormalizedUsage, ProviderResult

from galley.adapters import AdapterResult, Scope
from galley.adapters.single_pass import SinglePassAdapter
from galley.contracts import Manuscript

ROOT = Path(__file__).resolve().parents[2]
CONFIG = str(ROOT / "config" / "default.yaml")
FIXTURE = ROOT / "tests" / "fixtures" / "tiny_novel.docx"

# The fixture's paragraphs run body-0000 .. body-0036; body-0028 holds "teh".
TEH_PARA = "body-0028"
_U = NormalizedUsage(input_tokens=20, output_tokens=8)


def _lean_cfg():
    """A self-contained, offline config: one spelling pass, nothing model-heavy.

    Mirrors B1's ``_lean_cfg`` — analyses are off inside the adapter anyway, but
    disabling the network passes keeps a stray key from ever being needed.
    """
    cfg = load_config(CONFIG)
    cfg.error_types = [["spelling"]]
    cfg.audit = "off"
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


def _fixture_manuscript() -> Manuscript:
    """A manuscript whose ids mirror the fixture (body-0000 .. body-0036).

    ``Scope.paragraph_ids`` resolves against this, so the ids must match the
    ingested document's ids; the text is a placeholder — only membership and
    order matter to the scope resolver.
    """
    order = tuple(f"body-{i:04d}" for i in range(37))
    paragraphs = {pid: f"placeholder {pid}" for pid in order}
    return Manuscript(paragraphs=paragraphs, order=order)


def _para_blocks(user: str) -> dict[str, str]:
    """Parse the reviewed paragraphs (after the context block) out of a prompt."""
    tail = user.split("</context>")[-1]
    return dict(re.findall(r'<paragraph id="([^"]+)">(.*?)</paragraph>', tail, re.S))


class _RecordingTeh:
    """Flags 'teh' -> 'the'; records every para id it was ever asked about."""

    name = "fake"

    def __init__(self):
        self.seen_para_ids: set[str] = set()

    def complete_structured(self, *, user, **kw) -> ProviderResult:
        findings = []
        for pid, text in _para_blocks(user).items():
            self.seen_para_ids.add(pid)
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


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture book missing")
def test_scope_restricts_the_read_to_the_scoped_paragraphs():
    provider = _RecordingTeh()
    adapter = SinglePassAdapter(
        source_path=FIXTURE, cfg=_lean_cfg(), provider=provider, wave=2)
    usage = Usage()
    scope = Scope(para_ids=(TEH_PARA,), error_groups=("spelling",))

    result = adapter.run(_fixture_manuscript(), scope, budget_usd=100.0,
                         usage=usage)

    assert isinstance(result, AdapterResult)
    # Only the scoped paragraph was ever put to the model — never an unscoped one.
    assert provider.seen_para_ids == {TEH_PARA}
    assert "body-0001" not in provider.seen_para_ids
    assert "body-0002" not in provider.seen_para_ids  # the "the the" paragraph


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture book missing")
def test_findings_land_with_correct_spans():
    adapter = SinglePassAdapter(
        source_path=FIXTURE, cfg=_lean_cfg(), provider=_RecordingTeh(), wave=2)
    usage = Usage()
    scope = Scope(para_ids=(TEH_PARA,), error_groups=("spelling",))

    result = adapter.run(_fixture_manuscript(), scope, budget_usd=100.0,
                         usage=usage)

    spelling = [f for f in result.findings if f.error_type == "spelling"]
    assert spelling, "the planted 'teh' typo should surface as a spelling finding"
    f = spelling[0]
    # The finding sits on the scoped paragraph, with a well-formed span, and its
    # minimal diff is exactly teh -> the.
    assert f.span.para_id == TEH_PARA
    assert f.span.end > f.span.start >= 0
    # The validator shrank the edit to its minimal diff (teh -> the collapses to
    # eh -> he), so find != replace and the correction is a real change.
    assert f.find != f.replace and f.replace
    # Wave-2 single-pass provenance rode through.
    assert f.provenance and f.provenance.detector == "single_pass"
    assert f.provenance.wave == 2
    assert f.provenance.model


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture book missing")
def test_cost_accrues_to_usage_by_model():
    adapter = SinglePassAdapter(
        source_path=FIXTURE, cfg=_lean_cfg(), provider=_RecordingTeh(), wave=2)
    usage = Usage()
    scope = Scope(para_ids=(TEH_PARA,), error_groups=("spelling",))

    result = adapter.run(_fixture_manuscript(), scope, budget_usd=100.0,
                         usage=usage)

    # The shared usage accreted this call's tokens, bucketed by model.
    assert usage.by_model, "per-model usage must accrue on the shared object"
    assert usage.input_tokens > 0
    assert usage.api_calls > 0
    assert result.cost_usd >= 0.0


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture book missing")
def test_scope_model_override_is_honoured():
    adapter = SinglePassAdapter(
        source_path=FIXTURE, cfg=_lean_cfg(), provider=_RecordingTeh(), wave=2)
    usage = Usage()
    scope = Scope(para_ids=(TEH_PARA,), error_groups=("spelling",),
                  model="claude-opus-override")

    result = adapter.run(_fixture_manuscript(), scope, budget_usd=100.0,
                         usage=usage)

    # The override became the finding's provenance model and the usage bucket.
    assert all(f.provenance.model == "claude-opus-override"
               for f in result.findings)
    assert "claude-opus-override" in usage.by_model


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture book missing")
def test_passes_two_unions_without_crashing():
    adapter = SinglePassAdapter(
        source_path=FIXTURE, cfg=_lean_cfg(), provider=_RecordingTeh(), wave=2)
    usage = Usage()
    scope = Scope(para_ids=(TEH_PARA,), error_groups=("spelling",), passes=2)

    result = adapter.run(_fixture_manuscript(), scope, budget_usd=100.0,
                         usage=usage)

    # Two reads over the same scope: the identical edit is unioned by identity,
    # so the single planted typo still yields exactly one finding.
    spelling = [f for f in result.findings if f.error_type == "spelling"]
    assert len(spelling) == 1
    # Both reads billed, so the shared usage counted two API calls.
    assert usage.api_calls == 2
