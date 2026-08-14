"""The judge gates: strong models reading every proposed change before it ships.

Two ship — meaning ("does the sentence still say what it said?") and fix ("is
this the right correction?") — on one shared machinery. The contract these tests
hold to is the fail-safe one: a change a judge will not vouch for becomes a
margin question and never a silent edit; a judge that cannot answer changes
nothing; and a gate only ever SUBTRACTS, so turning one on can never promote an
edit or evict a change another gate approved. See docproof/judges.py and the gate
runner in docproof.pipeline.
"""
from __future__ import annotations

from dataclasses import replace

from docproof.judges import FIX, MEANING, Judge, default_prompt, screen
from docproof.models import DocumentModel, Finding, ParagraphRef, Usage
from docproof.providers.base import ProviderResult

from .fakes import USAGE, FakeProvider

PARA = "He could not have known the road was barred."


def _para(pid: str = "body-0001", text: str = PARA) -> ParagraphRef:
    return ParagraphRef(para_id=pid, part="word/document.xml", location="body",
                        text=text, style="Normal")


def _edit(fid: str, quote: str, corrected: str,
          pid: str = "body-0001") -> Finding:
    return Finding(finding_id=fid, chunk_id="c0", para_id=pid,
                   error_type="spelling", original_text=quote, occurrence=1,
                   corrected_text=corrected, explanation="typo",
                   confidence="high")


def _verdicts(*items) -> ProviderResult:
    return ProviderResult(parsed={"verdicts": list(items)}, usage=USAGE)


def _text(*paras: ParagraphRef) -> dict[str, str]:
    return {p.para_id: p.text for p in paras}


def _screen(findings, para_text, provider, **kw):
    kw.setdefault("spec", MEANING)
    kw.setdefault("model", "claude-fable-5")
    kw.setdefault("usage", Usage())
    return screen(findings, para_text, provider, **kw)


# --- verdict routing ---------------------------------------------------------

def test_preserved_change_is_left_alone():
    p = _para()
    f = _edit("f-1", PARA, "He could not have known the road was barred.")
    provider = FakeProvider([_verdicts(
        {"item": 1, "verdict": "keep", "reason": ""})])
    report = _screen([f], _text(p), provider)
    assert report.withheld == []
    assert report.checked == 1
    assert report.calls == 1


def test_meaning_changing_edit_is_downgraded_with_its_reason():
    p = _para()
    f = _edit("f-1", PARA, "He could have known the road was barred.")
    provider = FakeProvider([_verdicts(
        {"item": 1, "verdict": "withhold",
         "reason": "Drops the negation, reversing what he knew."})])
    report = _screen([f], _text(p), provider)
    assert len(report.withheld) == 1
    held = report.withheld[0]
    assert held.force_query is True
    # The margin note says both what was proposed and why it was withheld — it
    # is all the author gets beside the sentence.
    assert "typo" in held.explanation                      # the original reason
    assert "Drops the negation, reversing what he knew." in held.explanation
    assert "Not applied" in held.explanation
    # The change itself is untouched — only its channel moved.
    assert held.corrected_text == f.corrected_text
    assert held.finding_id == "f-1"


def test_unsure_is_held_back_by_default_and_applied_when_configured():
    p = _para()
    f = _edit("f-1", PARA, "He could not have known the road was bared.")
    unsure = {"item": 1, "verdict": "unsure", "reason": "Ambiguous."}

    strict = _screen([f], _text(p), FakeProvider([_verdicts(unsure)]))
    assert [x.finding_id for x in strict.withheld] == ["f-1"]

    lenient = _screen([f], _text(p), FakeProvider([_verdicts(unsure)]),
                      flag_unsure=False)
    assert lenient.withheld == []


def test_downgrade_without_a_reason_still_carries_a_margin_note():
    """The reason becomes what the author reads, so an empty one is filled in
    rather than leaving a blank comment in the margin."""
    p = _para()
    f = _edit("f-1", PARA, "He could have known the road was barred.")
    provider = FakeProvider([_verdicts(
        {"item": 1, "verdict": "withhold", "reason": "  "})])
    report = _screen([f], _text(p), provider)
    assert report.withheld[0].explanation.strip()


# --- fail-open discipline ----------------------------------------------------

def test_a_refusal_changes_nothing():
    p = _para()
    f = _edit("f-1", PARA, "He could have known the road was barred.")
    provider = FakeProvider([ProviderResult(parsed=None, stop_reason="refusal")])
    report = _screen([f], _text(p), provider)
    assert report.withheld == []


def test_an_unparseable_answer_changes_nothing():
    p = _para()
    f = _edit("f-1", PARA, "He could have known the road was barred.")
    provider = FakeProvider([ProviderResult(parsed={"nope": 1})])
    assert _screen([f], _text(p), provider).withheld == []


def test_a_verdict_for_an_item_that_was_never_sent_is_ignored():
    """An item number outside the batch is dropped, not wrapped around onto some
    other change."""
    p = _para()
    f = _edit("f-1", PARA, "He could have known the road was barred.")
    provider = FakeProvider([_verdicts(
        {"item": 99, "verdict": "withhold", "reason": "x"})])
    assert _screen([f], _text(p), provider).withheld == []


def test_item_zero_is_ignored_rather_than_read_as_the_last_change():
    """1-based numbering: a 0 must not index backwards onto the final item."""
    p = _para()
    a = _edit("f-1", PARA, "He could not have known the road was bared.")
    b = _edit("f-2", PARA, "He could not have known the road was barred!")
    provider = FakeProvider([_verdicts(
        {"item": 0, "verdict": "withhold", "reason": "x"})])
    assert _screen([a, b], _text(p), provider).withheld == []


def test_a_finding_with_no_paragraph_is_never_judged():
    p = _para()
    orphan = _edit("f-1", PARA, "x", pid="body-9999")
    provider = FakeProvider()
    report = _screen([orphan], _text(p), provider)
    assert report.calls == 0 and report.checked == 0
    assert provider.calls == []


# --- batching and accounting -------------------------------------------------

def test_one_call_per_paragraph_carries_all_its_changes():
    a, b = _para("body-0001"), _para("body-0002", "She sat down, slowly.")
    findings = [_edit("f-1", PARA, "He could not have known the road was bared."),
                _edit("f-2", PARA, "He could not have known the road was barred!"),
                _edit("f-3", "She sat down, slowly.", "She sat down slowly.",
                      pid="body-0002")]
    provider = FakeProvider([
        _verdicts({"item": 1, "verdict": "keep", "reason": ""},
                  {"item": 2, "verdict": "keep", "reason": ""}),
        _verdicts({"item": 1, "verdict": "keep", "reason": ""})])
    report = _screen(findings, _text(a, b), provider)
    assert report.calls == 2 and report.checked == 3
    assert len(provider.calls) == 2


def test_usage_is_recorded_for_every_call():
    p = _para()
    usage = Usage()
    provider = FakeProvider([_verdicts(
        {"item": 1, "verdict": "keep", "reason": ""})])
    _screen([_edit("f-1", PARA, PARA + " ")], _text(p), provider, usage=usage)
    assert usage.api_calls == 1
    assert usage.input_tokens > 0


def test_the_configured_model_and_prompt_are_what_get_sent():
    p = _para()
    provider = FakeProvider([_verdicts(
        {"item": 1, "verdict": "keep", "reason": ""})])
    _screen([_edit("f-1", PARA, PARA + " ")], _text(p), provider,
            model="claude-opus-5", instructions="Only ask about meaning.")
    call = provider.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["system"].startswith("Only ask about meaning.")
    # The paragraph and both versions of the sentence reach the judge.
    assert PARA in call["user"]


def test_an_empty_instruction_falls_back_to_the_built_in_default():
    judge = Judge(MEANING, FakeProvider(), "claude-fable-5", instructions="   ")
    assert judge.system_prompt.startswith(default_prompt('meaning')[:40])


def test_the_prompt_and_the_wire_protocol_do_not_drift():
    """Verdicts come back keyed by item number, so the instructions must ask for
    item numbers. A prompt still asking for finding_id would produce answers the
    parser silently drops — a gate that quietly stops gating."""
    for spec in (MEANING, FIX):
        wire = Judge(spec, FakeProvider(), "claude-fable-5").system_prompt
        assert "finding_id" not in wire
        assert "item" in wire              # the numbering the parser keys on
        for verdict in ("keep", "withhold", "unsure"):
            assert verdict in wire


def test_the_rendered_batch_numbers_items_and_withholds_the_rule():
    """The judge is asked about sense, so it is given the sentences and not the
    error type or the rule that proposed the change."""
    judge = Judge(MEANING, FakeProvider(), "claude-fable-5")
    a = _edit("f-1", PARA, "He could not have known the road was bared.")
    b = _edit("f-2", PARA, "He could not have known the road was barred!")
    rendered = judge._render(PARA, [a, b])
    assert "item 1" in rendered and "item 2" in rendered
    assert "spelling" not in rendered          # the error type stays out of it
    assert "typo" not in rendered              # ...and so does its explanation


def test_a_paragraph_quoting_finding_is_cut_down_to_its_sentence():
    """The rewrite pass quotes a whole paragraph for a two-character fix, and
    the paragraph is already at the top of the prompt. Rendering it twice more
    per item would bury the change and triple the tokens."""
    from docproof.models import Anchor
    para = ("The old man walked down the road. The dust knew his boots. "
            "Nobody watched him go.")
    at = para.index("knew")
    f = Finding(finding_id="rw-1", chunk_id="c", para_id="body-0001",
                error_type="spelling", original_text=para, occurrence=1,
                corrected_text=para.replace("knew", "new"),
                explanation="typo", confidence="high", status="validated",
                anchor=Anchor(start=at, end=at + 4, delete_text="knew",
                              insert_text="new"))
    rendered = Judge(MEANING, FakeProvider(), "claude-fable-5")._render(para, [f])
    assert "as written: 'The dust knew his boots.'" in rendered
    assert "as corrected: 'The dust new his boots.'" in rendered
    # The paragraph appears once, as context — not three times.
    assert rendered.count("Nobody watched him go.") == 1


def test_a_finding_without_an_anchor_still_renders_its_own_quote():
    judge = Judge(MEANING, FakeProvider(), "claude-fable-5")
    f = _edit("f-1", PARA, "He could have known the road was barred.")
    rendered = judge._render(PARA, [f])       # status pending, anchor None
    assert PARA in rendered


def test_house_conventions_ride_along_when_given():
    judge = Judge(MEANING, FakeProvider(), "claude-fable-5",
                         context="HOUSE STYLE: serial comma.")
    assert "HOUSE STYLE: serial comma." in judge.system_prompt


def test_a_verdict_lands_on_the_finding_that_was_judged_not_a_namesake():
    """Ids are only unique per source — continuity and the consistency scan both
    mint "c-0001" — so no id travels to the judge at all: items are numbered and
    verdicts come back by number. Two findings sharing an id must still get
    their own answers."""
    a = _para("body-0001", "The clock struck one, nobody moved.")
    b = _para("body-0002", "She had seen him in April, not May.")
    twin_a = Finding(finding_id="c-0001", chunk_id="c0", para_id="body-0001",
                     error_type="consistency",
                     original_text="The clock struck one, nobody moved.",
                     occurrence=1,
                     corrected_text="The clock struck one; nobody moved.",
                     explanation="comma splice", confidence="high")
    twin_b = Finding(finding_id="c-0001", chunk_id="c0", para_id="body-0002",
                     error_type="continuity",
                     original_text="She had seen him in April, not May.",
                     occurrence=1,
                     corrected_text="She had seen him in May, not April.",
                     explanation="date drift", confidence="high")
    # Only the second is meaning-changing; the first must be left alone.
    provider = FakeProvider([
        _verdicts({"item": 1, "verdict": "keep", "reason": ""}),
        _verdicts({"item": 1, "verdict": "withhold",
                   "reason": "Swaps the two months."})])
    report = _screen([twin_a, twin_b], _text(a, b), provider)
    assert report.positions == (1,)
    assert len(report.withheld) == 1
    held = report.withheld[0]
    assert held.para_id == "body-0002" and held.error_type == "continuity"


def test_nothing_to_judge_makes_no_calls():
    provider = FakeProvider()
    report = _screen([], {}, provider)
    assert report.calls == 0 and report.withheld == []
    assert provider.calls == []


# --- the pipeline seam -------------------------------------------------------

def _make_docx(path, *paragraphs):
    import docx
    d = docx.Document()
    for para in paragraphs:
        d.add_paragraph(para)
    d.save(path)
    return path


def _finish_with(tmp_path, monkeypatch, text, findings, results,
                 gates=("meaning",), **mc):
    """Run pipeline.finish over a real document with the named gate(s) on and the
    judges' answers scripted. Returns (outputs, provider)."""
    import docproof.providers as providers_mod
    from docproof.config import load_config
    from docproof.pipeline import finish, prepare

    src = _make_docx(tmp_path / "book.docx", text)
    # Ingest numbers body paragraphs from zero; the findings must name the id
    # the real document actually has.
    findings = [replace(f, para_id="body-0000") for f in findings]
    cfg = load_config("config/default.yaml")
    cfg.audit = "off"
    for name in gates:
        gate = getattr(cfg, f"{name}_check")
        gate.enabled = True
        for k, v in mc.items():
            setattr(gate, k, v)

    provider = FakeProvider(list(results))
    monkeypatch.setattr(providers_mod, "build_provider", lambda _c: provider)
    prepared = prepare(cfg, src, "config/error_types")
    out = finish(prepared, list(findings), Usage(), cfg,
                 out_dir=tmp_path / "out", source_path=src)
    return out, provider


def test_finish_holds_back_a_meaning_changing_edit(tmp_path, monkeypatch):
    """End to end through pipeline.finish over a real document: an edit the gate
    flags is not applied, and the summary says so."""
    out, provider = _finish_with(
        tmp_path, monkeypatch, PARA,
        [_edit("f-1", PARA, "He could have known the road was barred.")],
        [_verdicts({"item": 1, "verdict": "withhold",
                    "reason": "Drops the negation."})])
    assert out.applied == 0                      # nothing reached the manuscript
    assert provider.calls, "the gate never called its judge"
    summary = (tmp_path / "out" / "summary.md").read_text("utf-8")
    assert "The meaning check" in summary
    assert "Drops the negation." in summary


def test_finish_applies_an_edit_that_preserves_meaning(tmp_path, monkeypatch):
    out, _ = _finish_with(
        tmp_path, monkeypatch, PARA,
        [_edit("f-1", PARA, "He could not have known the road was bared.")],
        [_verdicts({"item": 1, "verdict": "keep", "reason": ""})])
    assert out.applied == 1
    assert "Held back" not in (tmp_path / "out" / "summary.md").read_text("utf-8")


def test_withdrawing_a_change_does_not_promote_the_edit_it_was_blocking(
        tmp_path, monkeypatch):
    """The gate only ever subtracts. Withdrawing an edit must NOT free its span
    for the competitor the validator had already set aside — re-arbitrating the
    run is how a promoted edit ends up evicting a change the gate approved, and
    how an unread edit reaches the manuscript."""
    a = _edit("f-1", PARA, "He could not have known the road was blocked.")
    b = _edit("f-2", PARA, "He could not have known the road was bolted.")
    out, provider = _finish_with(
        tmp_path, monkeypatch, PARA, [a, b],
        [_verdicts({"item": 1, "verdict": "withhold",
                    "reason": "Blocked is not barred."})])
    # One round only: f-2 was overlapping before the gate and stays that way.
    assert len(provider.calls) == 1
    assert out.applied == 0                      # nothing shipped, read or not
    summary = (tmp_path / "out" / "summary.md").read_text("utf-8")
    assert "Blocked is not barred." in summary
    assert "bolted" not in summary.lower()       # never promoted, never applied


def test_the_gate_never_evicts_a_change_it_approved(tmp_path, monkeypatch):
    """Three findings where the middle one loses a span contest. Holding back
    the first must not let that loser in, and must not cost the third — a
    safety pass that deletes a correction it vouched for is worse than no pass."""
    text = "The old man walked slowly down the road."
    a = _edit("f-1", text, "The old man strode slowly down the road.")
    b = _edit("f-2", text, "The old man ran down the road.")
    c = _edit("f-3", text, "The old man walked slow down the road.")
    out, provider = _finish_with(
        tmp_path, monkeypatch, text, [a, b, c],
        [_verdicts({"item": 1, "verdict": "withhold", "reason": "Strode is not walked."},
                   {"item": 2, "verdict": "keep", "reason": ""})])
    assert len(provider.calls) == 1
    # f-1 withdrawn, f-3 (approved) still applied, f-2 still not promoted.
    assert out.applied == 1
    summary = (tmp_path / "out" / "summary.md").read_text("utf-8")
    assert "Strode is not walked." in summary
    assert "ran down the road" not in summary


def test_two_held_back_fixes_for_one_sentence_both_reach_the_author(tmp_path,
                                                                    monkeypatch):
    """Nothing is ever dropped. Two different corrections to the same sentence,
    same error type, both held back, are two things to tell the author — the
    query channel must not collapse them into one and silently lose the second."""
    # Two edits to the same sentence in different places, so both apply — and
    # both are then held back.
    a = _edit("f-1", PARA, "He could not have known the road was bared.")
    b = _edit("f-2", PARA, "She could not have known the road was barred.")
    out, _ = _finish_with(
        tmp_path, monkeypatch, PARA, [a, b],
        [_verdicts({"item": 1, "verdict": "withhold",
                    "reason": "Bared, not barred."},
                   {"item": 2, "verdict": "withhold",
                    "reason": "She, not he."})])
    assert out.applied == 0
    summary = (tmp_path / "out" / "summary.md").read_text("utf-8")
    assert "Bared, not barred." in summary
    assert "She, not he." in summary


def test_the_loop_stops_once_nothing_new_appears(tmp_path, monkeypatch):
    """The ordinary case: one round, no downgrades, no second pass."""
    out, provider = _finish_with(
        tmp_path, monkeypatch, PARA,
        [_edit("f-1", PARA, "He could not have known the road was bared.")],
        [_verdicts({"item": 1, "verdict": "keep", "reason": ""})])
    assert len(provider.calls) == 1
    assert out.applied == 1


def test_a_clean_manuscript_never_builds_a_judge(tmp_path, monkeypatch):
    """The gate sits at the far end of a run that has already been paid for. A
    manuscript with no changes in it must not construct a client there — a
    missing key would raise inside finish() and take the whole run's outputs
    down with it, having judged nothing."""
    import docproof.providers as providers_mod
    from docproof.config import load_config
    from docproof.pipeline import finish, prepare

    def explode(_cfg):                       # stands in for a missing key
        raise RuntimeError("no key — and nothing should have asked for one")

    src = _make_docx(tmp_path / "book.docx", "A clean sentence, nothing to fix.")
    cfg = load_config("config/default.yaml")
    cfg.audit = "off"
    cfg.meaning_check.enabled = True
    cfg.sweeps = []                          # no scripted changes either
    monkeypatch.setattr(providers_mod, "build_provider", explode)
    prepared = prepare(cfg, src, "config/error_types")
    out = finish(prepared, [], Usage(), cfg, out_dir=tmp_path / "out",
                 source_path=src)
    assert out.applied == 0
    assert (tmp_path / "out" / "summary.md").is_file()   # the run still shipped


def test_a_replay_honours_the_original_runs_held_back_changes(tmp_path,
                                                              monkeypatch):
    """The gate's verdicts live only in what the run recorded — the checkpoint
    carries raw detector output written long before finish(). A rebuild that
    ignored them would apply every held-back change and hand the author the
    un-gated document their summary said they were protected from."""
    from docproof.config import load_config
    from docproof.pipeline import finish, prepare, read_meaning_held

    f = _edit("f-1", PARA, "He could have known the road was barred.")
    out, _ = _finish_with(
        tmp_path, monkeypatch, PARA, [f],
        [_verdicts({"item": 1, "verdict": "withhold",
                    "reason": "Drops the negation."})])
    assert out.applied == 0
    held = read_meaning_held(tmp_path / "out")
    assert len(held) == 1 and held[0]["para_id"] == "body-0000"

    # Now the replay: gate off, no provider at all, findings as first minted.
    cfg = load_config("config/default.yaml")
    cfg.audit = "off"
    cfg.meaning_check.enabled = False
    src = tmp_path / "book.docx"
    prepared = prepare(cfg, src, "config/error_types")
    again = finish(prepared, [replace(f, para_id="body-0000")], Usage(), cfg,
                   out_dir=tmp_path / "out2", source_path=src,
                   judge_held=held)
    assert again.applied == 0, "the replay re-applied a held-back change"

    # ...and without the record, the same replay would have applied it — which
    # is exactly the bug the record exists to prevent.
    ungated = finish(prepared, [replace(f, para_id="body-0000")], Usage(), cfg,
                     out_dir=tmp_path / "out3", source_path=src)
    assert ungated.applied == 1


def test_a_judge_that_never_answered_is_reported_not_hidden(tmp_path,
                                                            monkeypatch):
    """The pass fails open, so a judge that refused every call downgrades
    nothing — which looks exactly like a judge that read everything and approved
    it. The run has to say which one happened."""
    out, _ = _finish_with(
        tmp_path, monkeypatch, PARA,
        [_edit("f-1", PARA, "He could have known the road was barred.")],
        [ProviderResult(parsed=None, stop_reason="refusal")])
    assert out.applied == 1                  # fail open: the change still ships
    summary = (tmp_path / "out" / "summary.md").read_text("utf-8")
    assert "got no answer" in summary
    assert "WITHOUT being read" in summary


def test_a_clean_pass_says_it_read_everything(tmp_path, monkeypatch):
    out, _ = _finish_with(
        tmp_path, monkeypatch, PARA,
        [_edit("f-1", PARA, "He could not have known the road was bared.")],
        [_verdicts({"item": 1, "verdict": "keep", "reason": ""})])
    summary = (tmp_path / "out" / "summary.md").read_text("utf-8")
    assert "The meaning check" in summary
    assert "all of them passed" in summary
    assert "got no answer" not in summary


def test_the_gate_leaves_the_scripted_sweeps_alone_by_default(tmp_path,
                                                              monkeypatch):
    """scope model_sources: a house-style sweep is punctuation-sized and cannot
    move a sense, so it is not sent to the judge and still applies."""
    text = "He waited… and waited."          # ellipsis: the nbsp sweep fires
    out, provider = _finish_with(tmp_path, monkeypatch, text, [], [])
    assert provider.calls == []                  # nothing was judged
    assert out.applied >= 1                      # the sweep still landed


def test_a_composed_sweep_edit_is_skipped_even_off_the_rounds_path(
        tmp_path, monkeypatch):
    """Multi-round hands finish() a composed list with the sweep and consistency
    source lists emptied — the sweeps were folded in every round — so the
    position range alone would put every house-style edit in front of a frontier
    model. Both scripted sources stamp their own chunk_id, and that is what the
    scope actually keys on."""
    from dataclasses import replace as dc_replace
    swept = dc_replace(_edit("s-0001", PARA,
                             "He could not have known the road was barred!"),
                       chunk_id="sweep", error_type="terminal_period")
    out, provider = _finish_with(tmp_path, monkeypatch, PARA, [swept], [])
    assert provider.calls == []                  # never judged, never billed
    assert out.applied == 1                      # and still applied


# --- the fix judge, and two gates together -----------------------------------

def test_the_fix_judge_holds_back_a_wrong_correction(tmp_path, monkeypatch):
    """The other question: the sense may be untouched and the repair still
    wrong — "their" corrected to "there" where "they're" was wanted."""
    text = "Their going to the fair tomorrow."
    out, provider = _finish_with(
        tmp_path, monkeypatch, text,
        [_edit("f-1", text, "There going to the fair tomorrow.")],
        [_verdicts({"item": 1, "verdict": "withhold",
                    "reason": "Wanted “they're”, not “there”."})],
        gates=("fix",))
    assert out.applied == 0
    assert len(provider.calls) == 1
    summary = (tmp_path / "out" / "summary.md").read_text("utf-8")
    assert "The fix check" in summary
    assert "Wanted" in summary


def test_the_two_gates_are_independent_passes(tmp_path, monkeypatch):
    """Each gate is its own read of the same changes: two gates on means two
    calls, and a change has to satisfy both to survive."""
    out, provider = _finish_with(
        tmp_path, monkeypatch, PARA,
        [_edit("f-1", PARA, "He could not have known the road was bared.")],
        [_verdicts({"item": 1, "verdict": "keep", "reason": ""}),
         _verdicts({"item": 1, "verdict": "keep", "reason": ""})],
        gates=("meaning", "fix"))
    assert len(provider.calls) == 2
    assert out.applied == 1
    summary = (tmp_path / "out" / "summary.md").read_text("utf-8")
    assert "The meaning check" in summary and "The fix check" in summary


def test_a_change_the_first_gate_withdrew_is_not_judged_again(tmp_path,
                                                              monkeypatch):
    """Gates run in sequence over the surviving changes. One the meaning gate
    withdrew is no longer a tracked change, so the fix gate neither sees it nor
    is billed for it — with one change and both gates on, that is ONE call."""
    out, provider = _finish_with(
        tmp_path, monkeypatch, PARA,
        [_edit("f-1", PARA, "He could have known the road was barred.")],
        [_verdicts({"item": 1, "verdict": "withhold",
                    "reason": "Drops the negation."})],
        gates=("meaning", "fix"))
    assert len(provider.calls) == 1        # the fix gate had nothing left to read
    assert out.applied == 0
    summary = (tmp_path / "out" / "summary.md").read_text("utf-8")
    assert "Drops the negation." in summary


def test_either_gate_can_run_without_the_other(tmp_path, monkeypatch):
    out, provider = _finish_with(
        tmp_path, monkeypatch, PARA,
        [_edit("f-1", PARA, "He could not have known the road was bared.")],
        [_verdicts({"item": 1, "verdict": "keep", "reason": ""})],
        gates=("fix",))
    assert len(provider.calls) == 1
    summary = (tmp_path / "out" / "summary.md").read_text("utf-8")
    assert "The fix check" in summary
    assert "The meaning check" not in summary


def test_the_record_names_which_judge_held_each_change(tmp_path, monkeypatch):
    """Both gates write to one record, so a replay has to know which judge
    withheld what — and the author's reason must not be attributed wrongly."""
    from docproof.pipeline import read_meaning_held
    _finish_with(
        tmp_path, monkeypatch, PARA,
        [_edit("f-1", PARA, "He could have known the road was barred.")],
        [_verdicts({"item": 1, "verdict": "withhold", "reason": "Negation."})],
        gates=("meaning", "fix"))
    rows = read_meaning_held(tmp_path / "out")
    assert [r["judge"] for r in rows] == ["meaning"]


def test_the_fix_judge_gets_its_own_prompt_and_model(tmp_path, monkeypatch):
    judge = Judge(FIX, FakeProvider(), "claude-opus-5")
    assert "correct fix" in judge.system_prompt.lower()
    assert "still mean what the original meant" not in judge.system_prompt
    # ...and the meaning judge does not carry the fix judge's question.
    other = Judge(MEANING, FakeProvider(), "claude-opus-5")
    assert "still mean what the original meant" in other.system_prompt
