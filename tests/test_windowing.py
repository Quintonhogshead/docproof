"""A batched structured pass must never lose an item quietly.

The failure these cover is not "the model got it wrong" — it is "the model was
never asked, and nothing said so". A truncated window used to vanish into
neither the findings nor the reject log, and the pass then reported "0
correction(s) from 40 candidate(s)", which reads as restraint rather than as an
outage.
"""
from __future__ import annotations

from itertools import count

import pytest

from docproof.models import ParagraphRef, Usage
from docproof.providers.base import NormalizedUsage, ProviderResult
from docproof.rewrite import RewriteCandidate, confirm
from docproof.windowing import WindowReport, resolve_window

U = NormalizedUsage(input_tokens=10, output_tokens=10)


def _verdicts(indices, *, is_error=True, confidence="high"):
    return {"verdicts": [{"index": i, "is_error": is_error,
                          "confidence": confidence} for i in indices]}


def _ok(indices, **kw):
    return ProviderResult(parsed=_verdicts(indices, **kw), usage=U)


def _truncated():
    return ProviderResult(usage=U, stop_reason="max_tokens",
                          error="output truncated")


def _rows_of(parsed):
    return {v["index"]: v for v in parsed["verdicts"]}


# --- the helper ---------------------------------------------------------------

def test_a_clean_answer_costs_no_extra_call():
    report = WindowReport(label="t")
    calls = []
    got = resolve_window(
        ["a", "b", "c"], _ok([1, 2, 3]),
        fetch=lambda items, ceiling: calls.append(items) or _ok([1]),
        rows_of=_rows_of, max_tokens=4000, report=report)
    assert set(got) == {0, 1, 2}
    assert calls == []                      # the answer was already in hand
    assert report.lost == 0 and report.clean


def test_a_truncated_window_is_halved_and_recovered():
    """The reported bug: 40 items in, 0 out, logged as if nothing was an error."""
    report = WindowReport(label="t")
    seen = []

    def fetch(items, ceiling):
        seen.append(len(items))
        return _ok(range(1, len(items) + 1))   # the halves fit

    got = resolve_window(list("abcd"), _truncated(), fetch=fetch,
                         rows_of=_rows_of, max_tokens=4000, report=report)
    assert set(got) == {0, 1, 2, 3}         # every item ruled on after all
    assert seen == [2, 2]                   # halved, not re-asked whole
    assert report.truncated_calls == 1
    assert report.lost == 0


def test_a_partial_answer_has_its_missing_items_re_asked():
    """A response can stop cleanly, parse cleanly, validate — and still cover
    six of the forty items it was given."""
    report = WindowReport(label="t")
    asked = []

    def fetch(items, ceiling):
        asked.append(list(items))
        return _ok([1, 2])

    got = resolve_window(list("abcd"), _ok([1, 2]), fetch=fetch,
                         rows_of=_rows_of, max_tokens=4000, report=report)
    assert set(got) == {0, 1, 2, 3}
    assert asked == [["c", "d"]]            # only the missing two
    assert report.lost == 0


def test_what_never_comes_back_is_counted_lost_not_silently_dropped():
    report = WindowReport(label="t")
    got = resolve_window(
        list("ab"), _truncated(),
        fetch=lambda items, ceiling: _truncated(),
        rows_of=_rows_of, max_tokens=4000, report=report)
    assert got == {}
    assert report.asked == 2 and report.answered == 0
    assert report.lost == 2                 # the number the run has to report
    assert not report.clean
    assert "LOST" in report.summary()


def test_a_lone_truncating_item_gets_one_bigger_ceiling_before_giving_up():
    """It cannot be split, so the only remaining lever is headroom — once."""
    report = WindowReport(label="t")
    ceilings = []

    def fetch(items, ceiling):
        ceilings.append(ceiling)
        return _ok([1]) if ceiling > 4000 else _truncated()

    got = resolve_window(["only"], _truncated(), fetch=fetch, rows_of=_rows_of,
                         max_tokens=4000, report=report)
    assert ceilings == [8000]
    assert set(got) == {0} and report.lost == 0


def test_recovery_is_bounded_so_a_hopeless_window_cannot_loop():
    report = WindowReport(label="t")
    calls = count()

    def fetch(items, ceiling):
        next(calls)
        return _ok([])                      # answers nothing, never truncates

    resolve_window(list("abcdefgh"), _ok([]), fetch=fetch, rows_of=_rows_of,
                   max_tokens=4000, report=report)
    assert next(calls) < 40                 # terminates; the exact count is slack
    assert report.lost == 8


def test_a_body_that_fails_the_schema_is_unanswered_not_ruled():
    """A junk body must re-ask, never count as a window of 'not an error'."""
    report = WindowReport(label="t")
    got = resolve_window(
        list("ab"), ProviderResult(parsed={"nonsense": True}, usage=U),
        fetch=lambda items, ceiling: ProviderResult(parsed={"nonsense": True},
                                                    usage=U),
        rows_of=lambda p: {}, max_tokens=4000, report=report)
    assert got == {} and report.lost == 2


# --- the shared confirm valve -------------------------------------------------

class _Provider:
    """Answers the first call with a truncation, then answers honestly."""
    name = "fake"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def complete_structured(self, **kw):
        self.calls += 1
        return self.script.pop(0) if self.script else _ok([])


def _para(n=6):
    text = "The cat sat on the mat and the dog barked at the moon tonight."
    return [ParagraphRef(f"p{i}", "word/document.xml", "body", text, "Normal")
            for i in range(n)]


def _cands(paras, n):
    return [RewriteCandidate(para_id=paras[i % len(paras)].para_id,
                             start=4, end=7, original="cat", replacement="bat")
            for i in range(n)]


def test_confirm_recovers_a_truncated_window_instead_of_dropping_it():
    paras = _para()
    cands = _cands(paras, 4)
    prov = _Provider([_truncated(), _ok([1, 2]), _ok([1, 2])])
    losses: list = []
    out = confirm(cands, paras, prov, model="m", max_tokens=4000,
                  usage=Usage(), ids=count(1), batch_size=4,
                  loss_sink=losses)
    assert len(out) == 4                    # all four ruled on, none dropped
    assert losses[0].lost == 0
    assert losses[0].truncated_calls == 1


def test_confirm_reports_what_it_never_got_a_verdict_on():
    """The bug in one line: without this count, 0 findings from 4 candidates is
    indistinguishable from 4 candidates the model kept."""
    paras = _para()
    cands = _cands(paras, 4)
    prov = _Provider([_truncated()] * 20)
    losses: list = []
    out = confirm(cands, paras, prov, model="m", max_tokens=4000,
                  usage=Usage(), ids=count(1), batch_size=4,
                  loss_sink=losses)
    assert out == []
    assert losses[0].lost == 4              # NOT "4 kept as not-an-error"
    assert losses[0].asked == 4


def test_a_candidate_that_is_never_ruled_on_does_not_reach_the_reject_log():
    """The reject log is read as 'confirm looked at this and said no'. An
    unanswered candidate must not land there — it would read as a measured
    rejection in the recall eval."""
    paras = _para()
    cands = _cands(paras, 4)
    prov = _Provider([_truncated()] * 20)
    rejects: list = []
    confirm(cands, paras, prov, model="m", max_tokens=4000, usage=Usage(),
            ids=count(1), batch_size=4, reject_sink=rejects)
    assert rejects == []


def test_confirm_still_folds_in_the_order_asked():
    """Ids come off a shared counter and findings claim spans in order, so the
    folding order is load-bearing however the answers arrive."""
    paras = _para()
    cands = _cands(paras, 3)
    prov = _Provider([_ok([3, 1, 2])])      # answered out of order
    out = confirm(cands, paras, prov, model="m", max_tokens=4000,
                  usage=Usage(), ids=count(1), batch_size=3)
    assert [f.finding_id for f in out] == ["r-0001", "r-0002", "r-0003"]
    assert [f.para_id for f in out] == [c.para_id for c in cands]


def test_recovery_calls_are_billed():
    """Every retry is paid for, so it has to show up in the run's usage."""
    paras = _para()
    cands = _cands(paras, 2)
    prov = _Provider([_truncated(), _ok([1]), _ok([1])])
    usage = Usage()
    confirm(cands, paras, prov, model="m", max_tokens=4000, usage=usage,
            ids=count(1), batch_size=2)
    assert usage.output_tokens == 30        # the first call plus both halves


@pytest.mark.parametrize("script,expect_lost", [
    ([_truncated(), _ok([1]), _ok([1])], 0),
    ([_truncated()] * 20, 2),
])
def test_adjudicate_shares_the_same_guarantee(script, expect_lost):
    from docproof.adjudicate import Candidate, adjudicate

    paras = _para(2)
    cands = [Candidate(para_id=p.para_id, word="cat", start=4, end=7,
                       suggestion="bat", kind="typo") for p in paras]
    prov = _Provider(script)
    losses: list = []
    adjudicate(cands, paras, prov, model="m", max_tokens=4000, usage=Usage(),
               ids=count(1), batch_size=2, loss_sink=losses)
    assert losses[0].lost == expect_lost


# --- the run report -----------------------------------------------------------

def test_the_report_says_what_never_got_a_verdict(tmp_path):
    """A short result must never be readable as a clean one."""
    from docproof.config import load_config
    from docproof.formats import DOCX
    from docproof.models import CoverageLedger, DocumentModel
    from docproof.reporting import write_summary_md

    ledger = CoverageLedger()
    ledger.record_windows([
        WindowReport(label="languagetool confirm", asked=40, answered=0,
                     truncated_calls=1),
        WindowReport(label="adjudication", asked=10, answered=10),  # clean
    ])
    assert ledger.unruled_total == 40
    assert len(ledger.unruled) == 1          # the clean one is not carried

    doc = DocumentModel(source_path="Book.docx", paragraphs=(ParagraphRef(
        "p0", "word/document.xml", "body", "The cat sat.", "Normal"),))
    out = tmp_path / "summary.md"
    write_summary_md(out, doc=doc, findings=[], usage=Usage(),
                     cfg=load_config("config/default.yaml"), applied_ids=(),
                     fmt=DOCX, coverage=ledger)
    text = out.read_text()
    assert "40 candidate(s) never got a verdict" in text
    assert "- **languagetool confirm**" in text
    assert "- **adjudication**" not in text   # nothing was lost there
