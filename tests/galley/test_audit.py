"""C1 (offline half) — density, sampling, parsing, and the fake-provider path.

The live smoke (a real model call, <=$5) is NOT here: it is the one paid step,
run by a human. Everything below is deterministic and fenced to a fake provider.
"""

import json

from docproof.models import Usage
from docproof.providers import NormalizedUsage, ProviderResult

from galley.audit import (
    MAX_HYPOTHESES,
    audit_run,
    build_user_prompt,
    chapter_densities,
    parse_hypotheses,
    read_findings,
    sample_pages,
)
from galley.contracts import Hypothesis

from tests.galley.fakes import make_manuscript

_U = NormalizedUsage(input_tokens=120, output_tokens=40)


def _ms():
    # Two chapters of two paragraphs; every paragraph is three words.
    return make_manuscript(
        "one two three", "four five six",       # chapter 0
        "seven eight nine", "ten eleven twelve",  # chapter 1
        chapter_size=2,
    )


def _write_findings(tmp_path, para_ids):
    (tmp_path / "findings.json").write_text(
        json.dumps({"findings": [{"para_id": p} for p in para_ids]}),
        encoding="utf-8")


# ---- density -----------------------------------------------------------

def test_density_by_chapter():
    ms = _ms()
    findings = [{"para_id": "body-0001"}, {"para_id": "body-0001"}]  # 2 in ch0, 0 in ch1
    dens = chapter_densities(findings, ms)
    assert [d.index for d in dens] == [0, 1]
    assert dens[0].findings == 2 and dens[1].findings == 0
    assert dens[0].words == 6 and dens[1].words == 6
    assert round(dens[0].per_1k, 2) == round(2 / 6 * 1000, 2)
    assert dens[1].per_1k == 0.0


def test_density_no_chapters_is_whole_book():
    ms = make_manuscript("a b c", "d e f")  # no chapters
    dens = chapter_densities([{"para_id": "body-0001"}], ms)
    assert len(dens) == 1
    assert dens[0].title == "(whole book)"
    assert dens[0].findings == 1 and dens[0].words == 6


def test_density_unplaced_paragraphs_bucketed():
    # A finding on a paragraph outside any chapter is not dropped.
    from galley.contracts import Chapter, Manuscript
    ms = Manuscript(
        paragraphs={"body-0001": "a b", "body-0002": "c d", "body-0003": "e f"},
        order=("body-0001", "body-0002", "body-0003"),
        chapters=(Chapter(0, "One", ("body-0001", "body-0002")),),  # body-0003 uncovered
    )
    dens = chapter_densities([{"para_id": "body-0003"}], ms)
    unplaced = [d for d in dens if d.title == "(unplaced)"]
    assert unplaced and unplaced[0].findings == 1


# ---- read_findings -----------------------------------------------------

def test_read_findings(tmp_path):
    _write_findings(tmp_path, ["body-0001", "body-0003"])
    rows = read_findings(tmp_path)
    assert [r["para_id"] for r in rows] == ["body-0001", "body-0003"]


def test_read_findings_missing_file_is_empty(tmp_path):
    assert read_findings(tmp_path) == []


# ---- sampling ----------------------------------------------------------

def test_sample_draws_from_the_quietest_chapter_first():
    ms = _ms()
    dens = chapter_densities([{"para_id": "body-0001"}, {"para_id": "body-0001"}], ms)
    # ch1 has density 0 (quieter) than ch0, so the first sample is ch1's first para.
    sample = sample_pages(ms, dens, 1)
    assert sample == [("body-0003", "seven eight nine")]


def test_sample_is_deterministic_and_capped():
    ms = _ms()
    dens = chapter_densities([], ms)
    a = sample_pages(ms, dens, 3)
    b = sample_pages(ms, dens, 3)
    assert a == b
    assert len(a) == 3
    # Never returns more pages than exist.
    assert len(sample_pages(ms, dens, 99)) == 4
    assert sample_pages(ms, dens, 0) == []


# ---- parsing -----------------------------------------------------------

def test_parse_envelope_and_bare_list():
    row = {"chapter": 2, "error_class": "missing_comma", "why": "dense",
           "span_hint": "a b", "confidence": "high"}
    env = parse_hypotheses({"hypotheses": [row]})
    bare = parse_hypotheses([row])
    assert env == bare
    assert env[0] == Hypothesis(2, "missing_comma", "dense", "a b", "high")


def test_parse_skips_malformed_rows_and_caps():
    rows = [{"chapter": 1, "error_class": "x", "why": "", "span_hint": "", "confidence": "low"},
            "not a dict", 42]
    out = parse_hypotheses({"hypotheses": rows})
    assert len(out) == 1
    big = [{"chapter": i, "error_class": "x", "why": "", "span_hint": "", "confidence": "low"}
           for i in range(MAX_HYPOTHESES + 10)]
    assert len(parse_hypotheses({"hypotheses": big})) == MAX_HYPOTHESES


def test_parse_garbage_is_empty():
    assert parse_hypotheses(None) == []
    assert parse_hypotheses("nope") == []


# ---- build_user_prompt -------------------------------------------------

def test_user_prompt_carries_density_and_pages():
    ms = _ms()
    dens = chapter_densities([{"para_id": "body-0001"}], ms)
    samples = sample_pages(ms, dens, 2)
    prompt = build_user_prompt(dens, samples)
    assert "per 1k" in prompt
    assert "Chapter 0" in prompt and "Chapter 1" in prompt
    assert "<page id=" in prompt


# ---- audit_run (fenced to a fake provider) -----------------------------

class _Scripted:
    """A minimal fake provider that replays one structured result."""

    name = "fake"

    def __init__(self, result):
        self._result = result
        self.calls = 0

    def complete_structured(self, **kwargs):
        self.calls += 1
        self._last_kwargs = kwargs
        return self._result


def test_audit_run_returns_and_threads_usage(tmp_path):
    _write_findings(tmp_path, ["body-0001", "body-0001"])
    ms = _ms()
    provider = _Scripted(ProviderResult(
        parsed={"hypotheses": [{"chapter": 1, "error_class": "missing_comma",
                                "why": "quiet chapter", "span_hint": "seven eight",
                                "confidence": "medium"}]},
        usage=_U))
    usage = Usage()
    hyps = audit_run(tmp_path, ms, provider, "claude-opus-5", usage, n_samples=2)

    assert provider.calls == 1
    assert [h.error_class for h in hyps] == ["missing_comma"]
    assert hyps[0].chapter == 1
    # usage threaded onto the shared object under the model bucket
    assert usage.input_tokens == _U.input_tokens
    assert "claude-opus-5" in usage.by_model


def test_audit_run_truncated_reply_is_a_loss(tmp_path):
    _write_findings(tmp_path, [])
    ms = _ms()
    # A token-ceiling truncation parses as empty and must be recorded as a loss,
    # never parsed as if it were a real (empty) answer.
    provider = _Scripted(ProviderResult(parsed=None, usage=_U, stop_reason="max_tokens"))
    usage = Usage()
    assert audit_run(tmp_path, ms, provider, "m", usage) == []
    assert usage.input_tokens == _U.input_tokens  # still counted


def test_audit_run_refusal_yields_nothing(tmp_path):
    _write_findings(tmp_path, [])
    ms = _ms()
    provider = _Scripted(ProviderResult(parsed=None, usage=_U,
                                        stop_reason="refusal", error="no"))
    assert audit_run(tmp_path, ms, provider, "m", Usage()) == []


def test_audit_run_passes_a_schema_to_the_provider(tmp_path):
    _write_findings(tmp_path, [])
    ms = _ms()
    provider = _Scripted(ProviderResult(parsed={"hypotheses": []}, usage=_U))
    audit_run(tmp_path, ms, provider, "m", Usage())
    kw = provider._last_kwargs
    assert kw["schema_name"] == "hypotheses"
    assert isinstance(kw["schema"], dict) and kw["schema"]
