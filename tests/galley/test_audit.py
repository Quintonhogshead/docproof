"""C1 (offline half) — density, sampling, parsing, and the fake-provider path.

The live smoke (a real model call, <=$5) is NOT here: it is the one paid step,
run by a human. Everything below is deterministic and fenced to a fake provider.
"""

import json

from docproof.models import Usage
from docproof.providers import NormalizedUsage, ProviderResult

from galley.audit import (
    MAX_HYPOTHESES,
    AuditResult,
    audit_run,
    build_user_prompt,
    chapter_densities,
    is_heading_text,
    parse_hypotheses,
    plan_sample,
    read_findings,
    sample_pages,
)
from galley.contracts import Chapter, Hypothesis, Manuscript

from tests.galley.fakes import make_manuscript

_U = NormalizedUsage(input_tokens=120, output_tokens=40)


def _ms():
    # Two chapters of two paragraphs; every paragraph is three words.
    return make_manuscript(
        "one two three", "four five six",       # chapter 0
        "seven eight nine", "ten eleven twelve",  # chapter 1
        chapter_size=2,
    )


def _prose(tag, words=45):
    """A body paragraph of ``words`` words, terminated like prose, tagged so a
    test can tell which one was sampled."""
    return " ".join(f"{tag}{i}" for i in range(words)) + "."


def _book(n_chapters=3, paras=6, words=100):
    """Realistic shape: every chapter starts with its heading paragraph (as
    docproof.continuity.chapters keeps it), then ``paras`` body paragraphs."""
    paragraphs, order, chapters = {}, [], []
    for c in range(n_chapters):
        ids = []
        hid = f"body-{c:02d}00"
        paragraphs[hid] = f"Chapter {c + 1}"
        ids.append(hid)
        for i in range(1, paras + 1):
            pid = f"body-{c:02d}{i:02d}"
            paragraphs[pid] = _prose(f"c{c}p{i}w", words)
            ids.append(pid)
        order += ids
        chapters.append(Chapter(c, f"Chapter {c + 1}", tuple(ids)))
    return Manuscript(paragraphs=paragraphs, order=tuple(order),
                      chapters=tuple(chapters))


def _write_findings(tmp_path, para_ids, status=None):
    rows = [{"para_id": p} for p in para_ids]
    if status is not None:
        for r in rows:
            r["status"] = status
    (tmp_path / "findings.json").write_text(
        json.dumps({"findings": rows}), encoding="utf-8")


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


def test_density_counts_only_delivered_rows(tmp_path):
    # A real findings.json carries every candidate; only what reached the
    # author (an applied edit, a margin query) is evidence of coverage — the
    # app auditor counts case-file findings, which are exactly those.
    rows = [
        {"para_id": "body-0001", "status": "validated"},
        {"para_id": "body-0001", "status": "query"},
        {"para_id": "body-0001", "status": "rejected_no_anchor"},
        {"para_id": "body-0001", "status": "rejected_by_verifier"},
        {"para_id": "body-0001", "status": "skipped_low_confidence"},
        {"para_id": "body-0001", "status": "pending"},
    ]
    (tmp_path / "findings.json").write_text(
        json.dumps({"findings": rows}), encoding="utf-8")
    out = read_findings(tmp_path)
    assert len(out) == 6                        # read_findings stays raw
    ms = _ms()
    assert chapter_densities(out, ms)[0].findings == 2
    # The app auditor's status-less rows count as delivered.
    assert chapter_densities([{"para_id": "body-0001"}], ms)[0].findings == 1


# ---- sampling ----------------------------------------------------------

def test_heading_test_is_structural():
    assert is_heading_text("Chapter Nine")
    assert is_heading_text("Prologue")
    assert is_heading_text("12")
    assert is_heading_text("A Cold Morning")     # short, unterminated: a title
    assert is_heading_text("")
    assert not is_heading_text("It was late.")   # short but terminated prose
    assert not is_heading_text(_prose("w"))


def test_sample_skips_headings_and_short_paragraphs():
    ms = _book(n_chapters=2, paras=4, words=60)
    # A short beat inside chapter 0 that is prose but under the word floor.
    ms.paragraphs["body-0002"] = "No, she said."
    dens = chapter_densities([], ms)
    sample = sample_pages(ms, dens, 6, min_chapter_words=0)
    ids = [pid for pid, _ in sample]
    assert ids                                      # something was sampled
    assert "body-0000" not in ids and "body-0100" not in ids   # headings
    assert "body-0002" not in ids                   # under MIN_SAMPLE_WORDS
    for _, text in sample:
        assert len(text.split()) >= 40 and not is_heading_text(text)


def test_sample_draws_from_the_quietest_chapter_first():
    ms = _book(n_chapters=3, paras=4, words=60)
    # Chapter 1 has findings, chapters 0 and 2 have none: 0 and 2 are quiet.
    findings = [{"para_id": "body-0101"}, {"para_id": "body-0102"}]
    dens = chapter_densities(findings, ms)
    plan = plan_sample(ms, dens, 1, min_chapter_words=0)
    assert [p.chapter for p in plan.pages] == [0]
    assert plan.control_chapter is None             # n=1: no room for a control


def test_sample_is_seeded_random_within_the_chapter_not_always_first():
    ms = _book(n_chapters=1, paras=8, words=60)
    dens = chapter_densities([], ms)
    picks = {sample_pages(ms, dens, 1, seed=seed,
                          min_chapter_words=0)[0][0] for seed in range(12)}
    assert len(picks) > 1                           # not always body-0001
    # ...but deterministic for a given seed.
    a = sample_pages(ms, dens, 3, seed=4, min_chapter_words=0)
    b = sample_pages(ms, dens, 3, seed=4, min_chapter_words=0)
    assert a == b


def test_sample_is_deterministic_and_capped():
    ms = _book(n_chapters=2, paras=3, words=60)
    dens = chapter_densities([], ms)
    a = sample_pages(ms, dens, 3, min_chapter_words=0)
    b = sample_pages(ms, dens, 3, min_chapter_words=0)
    assert a == b
    assert len(a) == 3
    # Never returns more pages than there are body paragraphs (headings excluded).
    assert len(sample_pages(ms, dens, 99, min_chapter_words=0)) == 6
    assert sample_pages(ms, dens, 0) == []


def test_sample_always_includes_one_control_chapter():
    ms = _book(n_chapters=3, paras=4, words=60)
    # Chapter 2 is the densest — the review demonstrably read it — so it is the
    # control; chapters 0 and 1 are the quiet ones the sample targets.
    findings = [{"para_id": "body-0201"}, {"para_id": "body-0202"},
                {"para_id": "body-0203"}, {"para_id": "body-0101"}]
    dens = chapter_densities(findings, ms)
    plan = plan_sample(ms, dens, 4, min_chapter_words=0)
    assert plan.control_chapter == 2
    assert plan.quiet_chapters == (0, 1)
    controls = [p for p in plan.pages if p.control]
    assert len(controls) == 1 and controls[0].chapter == 2
    assert len(plan.pages) == 4
    assert all(p.chapter in (0, 1) for p in plan.pages if not p.control)
    # The same page objects still unpack as plain (para_id, text) tuples.
    for pid, text in plan.pages:
        assert pid in ms.paragraphs and ms.paragraphs[pid] == text


def test_no_control_when_every_chapter_is_equally_quiet():
    ms = _book(n_chapters=3, paras=4, words=60)
    plan = plan_sample(ms, chapter_densities([], ms), 4, min_chapter_words=0)
    assert plan.control_chapter is None
    assert not any(p.control for p in plan.pages)
    assert len(plan.pages) == 4


def test_tiny_chapters_and_unplaced_bucket_are_never_quiet():
    ms = _book(n_chapters=3, paras=4, words=60)      # ~240 body words each
    # An epigraph-sized "chapter" at zero density must not top the ranking.
    ms.paragraphs["body-9000"] = "Epigraph"
    ms.paragraphs["body-9001"] = _prose("epi", 40)
    ms = Manuscript(
        paragraphs=ms.paragraphs,
        order=ms.order + ("body-9000", "body-9001", "body-9002"),
        chapters=ms.chapters + (Chapter(3, "Epigraph", ("body-9000", "body-9001")),),
    )
    ms.paragraphs["body-9002"] = _prose("unplaced", 80)   # covered by no chapter
    findings = [{"para_id": "body-0001"}, {"para_id": "body-0101"},
                {"para_id": "body-0201"}, {"para_id": "body-0202"}]
    dens = chapter_densities(findings, ms)
    assert [d.title for d in dens][-1] == "(unplaced)"
    plan = plan_sample(ms, dens, 4, min_chapter_words=200)
    assert 3 not in plan.quiet_chapters              # 41-word epigraph unit
    assert 4 not in plan.quiet_chapters              # the (unplaced) bucket
    assert all(p.chapter in (0, 1, 2) for p in plan.pages)


def test_sample_relaxes_floors_rather_than_returning_nothing():
    # A book of nothing but short paragraphs still gets its longest prose read.
    ms = _ms()
    ms.paragraphs["body-0002"] = "Four five six, said the man to the woman."
    plan = plan_sample(ms, chapter_densities([], ms), 2)
    assert [p[0] for p in plan.pages] == ["body-0002"]
    assert plan.control_chapter is None


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
    ms = _book(n_chapters=2, paras=3, words=60)
    dens = chapter_densities([{"para_id": "body-0001"}], ms)
    samples = sample_pages(ms, dens, 2, min_chapter_words=0)
    prompt = build_user_prompt(dens, samples)
    assert "per 1k" in prompt
    assert "Chapter 0" in prompt and "Chapter 1" in prompt
    assert "<page id=" in prompt
    assert 'chapter="1"' in prompt


def test_user_prompt_names_the_control_chapter():
    ms = _book(n_chapters=3, paras=4, words=60)
    findings = [{"para_id": "body-0201"}, {"para_id": "body-0202"}]
    dens = chapter_densities(findings, ms)
    samples = sample_pages(ms, dens, 3, min_chapter_words=0)
    prompt = build_user_prompt(dens, samples)
    assert "Chapter 2 is the CONTROL" in prompt
    assert 'control="true"' in prompt
    assert prompt.count('control="true"') == 1
    # Plain (para_id, text) tuples — a caller that built its own — still render.
    plain = build_user_prompt(dens, [("body-0001", "text.")])
    assert "CONTROL" not in plain and '<page id="body-0001">' in plain


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
    ms = _book(n_chapters=2, paras=3, words=60)
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
    # The result is still a list, and records the experiment it came from.
    assert isinstance(hyps, list) and isinstance(hyps, AuditResult)
    assert len(hyps.sample_ids) == 2 and hyps.seed == 0
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


def test_audit_run_records_the_control_chapter(tmp_path):
    ms = _book(n_chapters=3, paras=4, words=200)     # ~800 body words a chapter
    (tmp_path / "findings.json").write_text(json.dumps({"findings": [
        {"para_id": "body-0201", "status": "validated"},
        {"para_id": "body-0202", "status": "validated"},
        {"para_id": "body-0203", "status": "rejected_overlap"},  # not counted
    ]}), encoding="utf-8")
    provider = _Scripted(ProviderResult(parsed={"hypotheses": []}, usage=_U))
    hyps = audit_run(tmp_path, ms, provider, "m", Usage(), n_samples=4, seed=3)
    assert hyps == [] and hyps.control_chapter == 2 and hyps.seed == 3
    assert len(hyps.sample_ids) == 4
    assert hyps.to_json()["control_chapter"] == 2
    assert "Chapter 2 is the CONTROL" in provider._last_kwargs["user"]
    # A truncated reply still reports what was sampled.
    provider = _Scripted(ProviderResult(parsed=None, usage=_U, stop_reason="max_tokens"))
    lost = audit_run(tmp_path, ms, provider, "m", Usage(), n_samples=4)
    assert lost == [] and lost.control_chapter == 2
