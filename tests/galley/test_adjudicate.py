"""D4 — earliest-wins arbitration, panel screening, reject-to-query."""

from docproof.models import Usage
from docproof.providers import NormalizedUsage, ProviderResult

from galley.adjudicate import adjudicate, arbitrate, screen_disputes
from galley.contracts import GFinding, Provenance, Span

from tests.fakes import FakeProvider
from tests.galley.fakes import make_manuscript

_U = NormalizedUsage(input_tokens=10, output_tokens=5)


def _g(fid, para, start, end, *, wave, find="x", replace="y", etype="typo"):
    return GFinding(
        id=fid,
        error_type=etype,
        span=Span(para, start, end),
        find=find,
        replace=replace,
        provenance=Provenance("det", wave, "m", 0.0),
    )


def test_earliest_wave_wins_the_span():
    w1 = _g("g-1", "body-0001", 4, 11, wave=1)
    w2 = _g("g-2", "body-0001", 6, 9, wave=2)  # overlaps g-1
    res = arbitrate([w2, w1])  # order shouldn't matter; wave does
    assert [f.id for f in res.kept] == ["g-1"]
    assert [f.id for f in res.queries] == ["g-2"]
    v = res.verdicts[0]
    assert v.finding_id == "g-2" and v.ruling == "query"
    assert "g-1" in v.reason  # names the winner


def test_nonoverlapping_findings_both_kept():
    a = _g("g-1", "body-0001", 0, 3, wave=1)
    b = _g("g-2", "body-0001", 10, 13, wave=2)
    res = arbitrate([a, b])
    assert {f.id for f in res.kept} == {"g-1", "g-2"}
    assert res.queries == []


def test_wave1_findings_never_lose_to_each_other_by_input_order():
    # Two wave-1 findings on disjoint spans: both survive regardless of order.
    a = _g("g-1", "body-0001", 0, 2, wave=1)
    b = _g("g-2", "body-0001", 5, 7, wave=1)
    assert len(arbitrate([b, a]).kept) == 2


def test_panel_withhold_routes_to_query_with_reason():
    ms = make_manuscript("The cat sat on the mat and slept there.")
    # Two later-wave findings on the same paragraph, disjoint spans.
    keep = _g("g-2", "body-0001", 0, 3, wave=2, find="The", replace="A")
    drop = _g("g-3", "body-0001", 8, 11, wave=2, find="sat", replace="stood")

    # The panel keeps item 1, withholds item 2 (changes meaning).
    provider = FakeProvider(
        results=[
            ProviderResult(
                parsed={
                    "verdicts": [
                        {"item": 1, "verdict": "keep", "reason": ""},
                        {"item": 2, "verdict": "withhold", "reason": "changes the action"},
                    ]
                },
                usage=_U,
            )
        ]
    )
    usage = Usage()
    res = adjudicate([keep, drop], ms, provider, model="fake", usage=usage, spec_key="fix")

    kept_ids = {f.id for f in res.kept}
    query_ids = {f.id for f in res.queries}
    assert "g-2" in kept_ids
    assert "g-3" in query_ids  # withheld -> query, not deleted
    reject = [v for v in res.verdicts if v.finding_id == "g-3" and v.ruling == "reject"]
    assert reject and reject[0].judge.startswith("panel")
    assert reject[0].reason  # carries a reason


def test_wave1_finding_is_never_sent_to_the_panel():
    ms = make_manuscript("Hello there world.")
    w1 = _g("g-1", "body-0001", 0, 5, wave=1, find="Hello", replace="Hi")
    # A provider that would raise if the panel ever called it.
    class Boom:
        name = "boom"

        def complete_structured(self, **kw):
            raise AssertionError("wave-1 finding must not reach the panel")

    verdicts, rejected = screen_disputes(
        [w1], ms, Boom(), model="fake", usage=Usage()
    )
    assert verdicts == [] and rejected == set()


def test_reject_finding_survives_as_query_object():
    ms = make_manuscript("Alpha beta gamma delta.")
    drop = _g("g-9", "body-0001", 0, 5, wave=2, find="Alpha", replace="Omega")
    provider = FakeProvider(
        results=[
            ProviderResult(
                parsed={"verdicts": [{"item": 1, "verdict": "withhold", "reason": "no"}]},
                usage=_U,
            )
        ]
    )
    res = adjudicate([drop], ms, provider, model="fake", usage=Usage())
    assert res.kept == []
    assert [f.id for f in res.queries] == ["g-9"]  # present, not vanished
    assert res.queries[0].find == "Alpha"  # the original finding object intact


# ---- duplicate re-finds and provenance-less findings --------------------

def test_identical_refind_is_a_duplicate_not_a_query():
    w1 = _g("g-1", "body-0001", 4, 11, wave=1, find="recieve", replace="receive")
    w2 = _g("g-2", "body-0001", 4, 11, wave=2, find="recieve", replace="receive")
    res = arbitrate([w1, w2])
    assert [f.id for f in res.kept] == ["g-1"]
    assert res.queries == []                      # never a question for the author
    v = res.verdicts[0]
    assert v.finding_id == "g-2" and v.ruling == "downgrade"
    assert v.judge == "arbitrator" and v.wave == 2
    assert v.reason == "duplicate of g-1 (wave 2 re-find)"


def test_same_span_different_fix_is_still_a_query():
    w1 = _g("g-1", "body-0001", 4, 11, wave=1, find="recieve", replace="receive")
    w2 = _g("g-2", "body-0001", 4, 11, wave=2, find="recieve", replace="recover")
    res = arbitrate([w1, w2])
    assert [f.id for f in res.queries] == ["g-2"]
    assert res.verdicts[0].ruling == "query"


def _bare(fid, para, start, end, find="x", replace="y"):
    return GFinding(id=fid, error_type="typo", span=Span(para, start, end),
                    find=find, replace=replace)   # no provenance at all


def test_provenance_less_finding_ranks_as_the_latest_wave():
    # Before: wave 0 outranked wave 1 and took the span. Now the finding that
    # can say where it came from wins.
    w1 = _g("g-1", "body-0001", 4, 11, wave=1)
    unknown = _bare("g-u", "body-0001", 6, 9)
    res = arbitrate([unknown, w1])
    assert [f.id for f in res.kept] == ["g-1"]
    assert [f.id for f in res.queries] == ["g-u"]
    assert res.verdicts[0].wave == 0              # recorded as "unknown", not a sentinel
    # Even against wave 3 the unattributed finding loses.
    w3 = _g("g-3", "body-0002", 0, 3, wave=3)
    late = _bare("g-v", "body-0002", 0, 3, find="a", replace="b")
    assert [f.id for f in arbitrate([late, w3]).kept] == ["g-3"]


def test_provenance_less_finding_is_screened():
    ms = make_manuscript("Alpha beta gamma delta.")
    unknown = _bare("g-u", "body-0001", 0, 5, find="Alpha", replace="Omega")
    provider = FakeProvider(results=[ProviderResult(
        parsed={"verdicts": [{"item": 1, "verdict": "withhold", "reason": "no"}]},
        usage=_U)])
    verdicts, rejected = screen_disputes([unknown], ms, provider, model="fake",
                                         usage=Usage())
    assert rejected == {"g-u"}
    assert verdicts[0].ruling == "reject" and verdicts[0].wave == 0
