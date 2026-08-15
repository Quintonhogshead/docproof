"""The chapter-scoped continuity read: the third reading distance.

Everything here defends the pass's two promises. First, that it only ever asks —
every finding is force_query'd, a margin question and never a tracked change,
because which of two contradictory facts is right is the author's to settle.
Second, its precision guardrails: a break survives only when both its quotes land
in the SAME chapter, the whole-book read never gets its question asked twice, and
the volume is capped per chapter with the cap reported rather than hidden.

Dialogue is IN scope here, the one deliberate inversion of the smoothing pass — a
contradiction spoken aloud is still a contradiction — and there is a test that
pins it.

See docproof/continuity.py and docproof.pipeline._chapter_continuity_findings.
"""
from __future__ import annotations

import itertools

from docproof.config import Config
from docproof.continuity import (ChapterUnit, breaks_to_findings, chapters,
                                 default_chapter_continuity_prompt)
from docproof.models import DocumentModel, ParagraphRef, Usage
from docproof.pipeline import Prepared, _chapter_continuity_findings
from docproof.providers.base import ProviderResult

from .fakes import FakeProvider


def _para(pid: str, text: str, style: str = "Normal") -> ParagraphRef:
    return ParagraphRef(para_id=pid, part="word/document.xml", location="body",
                        text=text, style=style)


def _prepared(*paras: ParagraphRef) -> Prepared:
    doc = DocumentModel(source_path="x.docx", paragraphs=tuple(paras))
    return Prepared(pkg=None, doc=doc, chunks=[], groups=[], fmt=None)


def _cfg(**kw) -> Config:
    cfg = Config()
    cfg.chapter_continuity.enabled = True
    cfg.api.concurrency = 1              # deterministic scripted-provider order
    for k, v in kw.items():
        setattr(cfg.chapter_continuity, k, v)
    return cfg


def _breaks(*items) -> ProviderResult:
    return ProviderResult(parsed={"findings": list(items)})


def _verdicts(*items) -> ProviderResult:
    return ProviderResult(parsed={"verdicts": list(items)})


def _b(earlier, quote, *, category="blocking", question="Intended?",
       confidence="high"):
    return {"category": category, "earlier_quote": earlier, "quote": quote,
            "question": question, "confidence": confidence}


def _run(monkeypatch, cfg, prepared, results, *, book_anchors=frozenset()):
    """Run the pass with a scripted provider for both stages, cache disabled."""
    prov = FakeProvider(results)
    import docproof.providers as _p
    monkeypatch.setattr(_p, "build_provider", lambda cfg, **kw: prov)
    import docproof.pipeline as _pipe
    monkeypatch.setattr(_pipe, "cache_dir_for", lambda c: None)
    findings, report = _chapter_continuity_findings(
        cfg, prepared, Usage(), book_anchors=book_anchors)
    return findings, report, prov


# --- segmentation -------------------------------------------------------------

def _heading(style: str) -> bool:
    return style.startswith("Heading")


def test_chapters_split_on_headings_and_keep_the_heading():
    paras = [
        _para("h1", "Chapter One", "Heading1"),
        _para("b1", "The first chapter runs on. " * 60),
        _para("h2", "Chapter Two", "Heading1"),
        _para("b2", "The second chapter runs on. " * 60),
    ]
    units = chapters(paras, _heading, min_tokens=50)
    assert [u.title for u in units] == ["Chapter One", "Chapter Two"]
    assert units[0].paragraphs[0].para_id == "h1"   # the heading leads its chapter
    assert [u.index for u in units] == [0, 1]


def test_front_matter_before_the_first_heading_is_not_dropped():
    """Paragraphs before the first heading are real text; a tiny opener merges
    forward into the first chapter rather than vanishing."""
    paras = [
        _para("ded", "For my mother."),                       # tiny front matter
        _para("h1", "Chapter One", "Heading1"),
        _para("b1", "The chapter proper. " * 60),
    ]
    units = chapters(paras, _heading, min_tokens=50)
    assert len(units) == 1
    ids = [p.para_id for p in units[0].paragraphs]
    assert ids == ["ded", "h1", "b1"]
    assert units[0].title == "Chapter One"                    # found past the opener


def test_a_headingless_manuscript_is_one_unit():
    paras = [_para("b0", "No headings here."), _para("b1", "Just prose.")]
    units = chapters(paras, _heading)
    assert len(units) == 1
    assert units[0].title == ""


def test_chapters_marked_by_plain_text_are_split():
    """The common manuscript: chapter titles are body text, not a Word heading
    style (Michalak marks all 23 chapters as Normal paragraphs). The text rule
    must split on 'Chapter N', 'Prologue', and a bare number, or the whole book
    collapses into one window."""
    paras = [
        _para("p0", "Prologue"),                          # plain-text marker
        _para("p1", "The story opens. " * 40),
        _para("c1", "Chapter One: The World Is Watching"),  # Normal style, not Heading
        _para("c1b", "The first chapter. " * 40),
        _para("c2", "Chapter 2: Setting Things Up"),
        _para("c2b", "The second chapter. " * 40),
        _para("c3", "17"),                                 # a bare-number chapter mark
        _para("c3b", "The third. " * 40),
    ]
    units = chapters(paras, _heading, min_tokens=50)       # no Heading styles at all
    assert [u.title for u in units] == [
        "Prologue", "Chapter One: The World Is Watching",
        "Chapter 2: Setting Things Up", "17"]


def test_a_body_sentence_beginning_with_chapter_is_not_a_split():
    """The text rule is anchored and length-capped, so a sentence that merely
    starts with the word is not mistaken for a title."""
    from docproof.continuity import looks_like_chapter_heading
    assert not looks_like_chapter_heading(
        _para("b0", "Chapter and verse, she quoted the whole passage from memory "
                    "as the room fell silent around her."))
    assert looks_like_chapter_heading(_para("b0", "Chapter Nine"))
    assert not looks_like_chapter_heading(                 # a page number in a footer
        ParagraphRef(para_id="f", part="p", location="footer", text="12",
                     style="Footer"))


def test_a_huge_unit_is_size_split():
    big = [_para(f"b{i}", "word " * 400) for i in range(6)]   # ~450 tok each
    units = chapters(big, _heading, min_tokens=1, max_tokens=1000)
    assert len(units) > 1                                     # split into windows
    # every paragraph survives exactly once, in order
    got = [p.para_id for u in units for p in u.paragraphs]
    assert got == [p.para_id for p in big]


# --- the invariant ------------------------------------------------------------

def test_every_finding_is_a_query_never_an_edit(monkeypatch):
    p = _para("body-0", "He was still on his feet by the door. "
                        "He stood up from the chair once more.")
    findings, _report, _prov = _run(monkeypatch, _cfg(), _prepared(p), [
        _breaks(_b("He was still on his feet by the door.",
                   "He stood up from the chair once more.")),
        _verdicts({"index": 1, "is_break": True, "confidence": "high"}),
    ])
    assert len(findings) == 1
    f = findings[0]
    assert f.force_query is True
    assert f.corrected_text == f.original_text          # a question edits nothing
    assert f.error_type == "chapter_continuity"
    assert f.finding_id.startswith("xc-")
    assert "Earlier:" in f.explanation


# --- precision guardrails -----------------------------------------------------

def test_a_break_whose_quote_is_not_in_the_chapter_is_dropped(monkeypatch):
    """The both-quotes-must-locate guard, which is also the same-chapter guard: a
    quote the read invented (or lifted from a different chapter) does not anchor,
    so the break is dropped rather than guessed at — and the judge is never paid
    for it."""
    p = _para("body-0", "She had green eyes that caught the light.")
    findings, report, prov = _run(monkeypatch, _cfg(), _prepared(p), [
        _breaks(_b("She had green eyes that caught the light.",
                   "Her blue eyes gave nothing away.")),      # not in the chapter
    ])
    assert findings == [] and report.proposed == 0
    assert len(prov.calls) == 1                               # judge never paid for


def test_a_contradiction_in_dialogue_is_in_scope(monkeypatch):
    """The one inversion of the smoothing pass: a break the reader meets inside
    quoted speech is still a break, so it is NOT filtered the way a smoothing
    suggestion touching dialogue would be."""
    p = _para("body-0", '"I have never been to Paris," she said. '
                        '"When I was in Paris last spring, it rained," she added.')
    findings, report, _prov = _run(monkeypatch, _cfg(), _prepared(p), [
        _breaks(_b('"I have never been to Paris," she said.',
                   '"When I was in Paris last spring, it rained," she added.',
                   category="knowledge")),
        _verdicts({"index": 1, "is_break": True, "confidence": "high"}),
    ])
    assert report.proposed == 1
    assert len(findings) == 1                                 # kept, not dropped


def test_the_book_read_gets_there_first(monkeypatch):
    """When the whole-book continuity read has already flagged a sentence, the
    chapter read must not ask the author the same question twice — the anchor is
    dropped before the judge sees it."""
    p = _para("body-0", "He locked the door behind him. "
                        "The unlocked door swung open on its own.")
    anchors = frozenset({("body-0", "The unlocked door swung open on its own.")})
    findings, report, prov = _run(monkeypatch, _cfg(), _prepared(p), [
        _breaks(_b("He locked the door behind him.",
                   "The unlocked door swung open on its own.", category="object")),
    ], book_anchors=anchors)
    assert findings == [] and report.proposed == 0
    assert len(prov.calls) == 1                               # judge never paid for


# --- accounting ---------------------------------------------------------------

def test_every_candidate_is_accounted_for(monkeypatch):
    """The identity that makes the pass's numbers reconcilable:

        proposed == kept + withheld + below_floor + refused + unjudged

    Each term is a different thing that happened — the cap, the confidence floor,
    the judge's taste, and a fault (a batch the judge never answered) — so an
    unexplained remainder would mean a loss path nobody has found."""
    p = _para("body-0",
              "He stood by the window. He sat down again. "
              "The glass was full. The glass was empty. "
              "Dawn broke over the hill. It was already dusk. "
              "She had never met him. She greeted him by name.")
    findings, report, _prov = _run(
        monkeypatch, _cfg(sensitivity=2, max_per_chapter=10),   # medium floor
        _prepared(p), [
            _breaks(
                _b("He stood by the window.", "He sat down again."),
                _b("The glass was full.", "The glass was empty.", category="object"),
                _b("Dawn broke over the hill.", "It was already dusk.", category="time"),
                _b("She had never met him.", "She greeted him by name.",
                   category="knowledge")),
            _verdicts({"index": 1, "is_break": True, "confidence": "high"},
                      {"index": 2, "is_break": True, "confidence": "low"},
                      {"index": 3, "is_break": False, "confidence": "high"}),
        ])
    assert report.proposed == 4
    assert report.proposed == (report.kept + report.withheld
                               + report.below_floor + report.refused
                               + report.unjudged)
    assert report.kept == 1          # the high-confidence one
    assert report.below_floor == 1   # affirmed "low", under the medium floor
    assert report.refused == 1       # the judge said no
    assert report.unjudged == 1      # item 4 never ruled on at all
    assert len(findings) == report.kept


# --- one question per site (the validator-collapse guard) ---------------------

def test_two_breaks_on_one_later_sentence_become_one_question(monkeypatch):
    """A margin query is keyed by its anchor span, and a question edits nothing
    (corrected_text == original), so two breaks pinned to the SAME later sentence
    would collide in the validator and the second would vanish uncounted. The
    proposer drops the duplicate itself, where it is counted as filtered and the
    report's `kept` stays honest — one pivotal sentence can contradict two earlier
    facts, and only one question can anchor there."""
    p = _para("body-0", "She had never left the house that morning. "
                        "The car was gone from the drive by nine. "
                        "She was seen downtown at noon.")
    findings, report, prov = _run(monkeypatch, _cfg(), _prepared(p), [
        _breaks(  # two DIFFERENT breaks, both anchored on the SAME later sentence
            _b("She had never left the house that morning.",
               "She was seen downtown at noon.", category="knowledge"),
            _b("The car was gone from the drive by nine.",
               "She was seen downtown at noon.", category="logic")),
        _verdicts({"index": 1, "is_break": True, "confidence": "high"}),
    ])
    assert report.proposed == 1          # the second same-site break was filtered
    assert report.filtered == 1
    assert len(findings) == 1            # exactly one question reaches the margin
    # And it does not collapse in the validator either (kept == what renders).
    from docproof.validator import validate_findings
    validated = validate_findings(findings, _prepared(p).doc, "low")
    assert [f.status for f in validated] == ["query"]


# --- honest accounting of a failed read ---------------------------------------

def test_a_failed_chapter_read_is_counted_not_swallowed(monkeypatch):
    """A chapter whose read fails loses every break in it. Counted as read_failed,
    never swallowed — an unread chapter that yields nothing looks exactly like a
    clean one that earned nothing, and on this pass silence is the norm."""
    p = _para("body-0", "The prose of a chapter that will not come back.")
    findings, report, _prov = _run(monkeypatch, _cfg(), _prepared(p), [
        ProviderResult(parsed=None, stop_reason="max_tokens", error="truncated"),
    ])
    assert findings == []
    assert report.read_failed == 1
    assert report.proposed == 0


def test_a_wholly_failed_run_still_prints_its_section(tmp_path):
    """The section is guarded on proposed OR read_failed: a run where every read
    failed proposes nothing, and that is exactly when the failure must show."""
    from docproof.continuity import ChapterContinuityReport
    from docproof.formats import DOCX
    from docproof.reporting import write_summary_md
    doc = DocumentModel(source_path="x.docx",
                        paragraphs=(_para("body-0", "Some prose here."),))
    write_summary_md(
        tmp_path / "summary.md", doc=doc, findings=[], usage=Usage(),
        cfg=Config(), applied_ids=(), fmt=DOCX,
        chapter_continuity=ChapterContinuityReport(chapters=3, read_failed=3,
                                                   proposed=0))
    text = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "## Chapter continuity" in text
    assert "3 chapter(s) could not be read" in text


# --- caching ------------------------------------------------------------------

def test_the_chapter_read_is_cached_per_draft(monkeypatch, tmp_path):
    """A second review of the same draft re-uses each chapter's cached read: the
    propose call does not fire again (its usage is zero), only the judge runs.
    This is what makes tuning by eye — read, adjust, re-run — nearly free on
    unchanged chapters."""
    import docproof.providers as _p
    cfg = _cfg(cache_dir=str(tmp_path))
    p = _para("body-0", "He was on his feet by the door. He sat down again.")
    scripted = [
        _breaks(_b("He was on his feet by the door.", "He sat down again.")),
        _verdicts({"index": 1, "is_break": True, "confidence": "high"}),
    ]
    prov1 = FakeProvider(list(scripted))
    monkeypatch.setattr(_p, "build_provider", lambda cfg, **kw: prov1)
    f1, _r1 = _chapter_continuity_findings(cfg, _prepared(p), Usage())
    assert len(f1) == 1
    assert len(prov1.calls) == 2                 # a propose read and a judge call

    prov2 = FakeProvider([  # the read must NOT be needed; only the judge
        _verdicts({"index": 1, "is_break": True, "confidence": "high"})])
    monkeypatch.setattr(_p, "build_provider", lambda cfg, **kw: prov2)
    f2, _r2 = _chapter_continuity_findings(cfg, _prepared(p), Usage())
    assert len(f2) == 1
    assert len(prov2.calls) == 1                 # judge only — the read was cached


# --- concurrent fan-out -------------------------------------------------------

class _ByChapterProvider:
    """A content-addressed fake: returns the propose result whose chapter text the
    request contains, and the next verdicts for a judge call — so a concurrent
    fan-out is deterministic regardless of which worker thread asks first."""
    name = "fake-by-chapter"

    def __init__(self, propose_by_marker, verdicts):
        self.propose_by_marker = dict(propose_by_marker)
        self.verdicts = list(verdicts)
        self.calls = []

    def complete_structured(self, *, user, **kw):
        self.calls.append({"user": user, **kw})
        if "Later:" in user and "Question:" in user:          # a judge batch
            return self.verdicts.pop(0) if self.verdicts \
                else ProviderResult(parsed={"verdicts": []})
        for marker, res in self.propose_by_marker.items():    # a chapter read
            if marker in user:
                return res
        return ProviderResult(parsed={"findings": []})


def test_the_fan_out_folds_usage_and_findings_across_chapters(monkeypatch):
    """Two chapters read concurrently (concurrency > 1): both breaks survive, and
    usage folds serially in the main thread — every call is billed exactly once."""
    paras = [
        _para("h1", "Chapter One", "Heading1"),
        _para("c1", "MARKER_ONE. He was standing at the rail. "
                    "He rose from his seat once more."),
        _para("h2", "Chapter Two", "Heading1"),
        _para("c2", "MARKER_TWO. The lamp was dark all evening. "
                    "The lamp had been burning for hours."),
    ]
    cfg = _cfg(min_chapter_tokens=1)
    cfg.api.concurrency = 4
    prov = _ByChapterProvider(
        {"MARKER_ONE": _breaks(_b("He was standing at the rail.",
                                  "He rose from his seat once more.")),
         "MARKER_TWO": _breaks(_b("The lamp was dark all evening.",
                                  "The lamp had been burning for hours.",
                                  category="object"))},
        [_verdicts({"index": 1, "is_break": True, "confidence": "high"},
                   {"index": 2, "is_break": True, "confidence": "high"})])
    import docproof.providers as _p
    monkeypatch.setattr(_p, "build_provider", lambda cfg, **kw: prov)
    import docproof.pipeline as _pipe
    monkeypatch.setattr(_pipe, "cache_dir_for", lambda c: None)
    usage = Usage()
    findings, report = _chapter_continuity_findings(cfg, _prepared(*paras), usage)
    assert report.chapters == 2
    assert report.proposed == 2 and len(findings) == 2
    # two chapter reads + one judge batch = three calls, each folded exactly once
    # (api_calls counts every result the main thread absorbed — no double-add, no
    # dropped-add from the worker pool)
    assert len(prov.calls) == 3
    assert usage.api_calls == 3


# --- the per-chapter cap ------------------------------------------------------

def test_the_cap_is_per_chapter():
    """Ten questions in one chapter is where a margin stops being read, so the cap
    is per chapter, not per book: twelve breaks in one chapter lose two, while a
    single break in another chapter is untouched."""
    def cand(chapter, i):
        from docproof.continuity import _ChapterCand
        return _ChapterCand(chapter=chapter, category="logic", para_id=f"p{i}",
                            occurrence=1, original=f"Sentence {i}.",
                            earlier=f"Earlier {i}.", question="q?",
                            confidence="high")
    crowded = [cand(0, i) for i in range(12)]
    lonely = [cand(1, 99)]
    findings, withheld, below = breaks_to_findings(
        crowded + lonely, itertools.count(1),
        min_confidence="medium", max_per_chapter=10)
    assert withheld == 2                 # only chapter 0 overflowed, by two
    assert below == 0
    assert len(findings) == 11           # 10 from chapter 0 + 1 from chapter 1


# --- reporting ----------------------------------------------------------------

def test_the_report_reaches_summary_md(tmp_path):
    from docproof.continuity import ChapterContinuityReport
    from docproof.formats import DOCX
    from docproof.reporting import write_summary_md
    doc = DocumentModel(source_path="x.docx",
                        paragraphs=(_para("body-0", "Some prose here."),))
    write_summary_md(
        tmp_path / "summary.md", doc=doc, findings=[], usage=Usage(),
        cfg=Config(), applied_ids=(), fmt=DOCX,
        chapter_continuity=ChapterContinuityReport(
            chapters=14, proposed=9, kept=3, withheld=4, cap=10, refused=2,
            propose_model="claude-fable-5"))
    text = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "## Chapter continuity" in text
    assert "4 further question(s) withheld" in text


def test_a_run_that_proposed_nothing_prints_no_section(tmp_path):
    from docproof.continuity import ChapterContinuityReport
    from docproof.formats import DOCX
    from docproof.reporting import write_summary_md
    doc = DocumentModel(source_path="x.docx",
                        paragraphs=(_para("body-0", "Some prose here."),))
    write_summary_md(
        tmp_path / "summary.md", doc=doc, findings=[], usage=Usage(),
        cfg=Config(), applied_ids=(), fmt=DOCX,
        chapter_continuity=ChapterContinuityReport(chapters=14, proposed=0))
    assert "## Chapter continuity" not in (
        tmp_path / "summary.md").read_text(encoding="utf-8")


# --- isolation ----------------------------------------------------------------
#
# A per-chapter read plus a judge lives inside finish(), so every path that
# rebuilds from a finished run must force it off or silently pay for a second,
# differently-worded set of the same questions — exactly like smoothing.

def test_a_rejudge_never_pays_for_chapter_continuity_again():
    from docproof.rejudge import _gates_only
    cfg = Config()
    cfg.chapter_continuity.enabled = True
    assert _gates_only(cfg).chapter_continuity.enabled is False


def test_a_free_rebuild_never_pays_for_chapter_continuity_again():
    from app.jobs import _free_finish
    cfg = Config()
    cfg.chapter_continuity.enabled = True
    _free_finish(cfg)
    assert cfg.chapter_continuity.enabled is False


def test_the_eval_runner_disables_chapter_continuity():
    from docproof.eval.runner import _eval_config
    cfg = Config()
    cfg.chapter_continuity.enabled = True
    assert _eval_config(cfg).chapter_continuity.enabled is False


# --- the reads riding a batch -------------------------------------------------

def test_the_reads_can_come_from_a_batch(monkeypatch):
    """The batch path: the chapter reads arrive as a results dict (custom_id ->
    ProviderResult) instead of being bought live, and only the judge runs here.
    Same candidates, same findings — the read just came from a batch."""
    from docproof.continuity import chapter_read_custom_id
    p = _para("body-0", "He was on his feet by the door. He sat down again.")
    reads = {chapter_read_custom_id(0): _breaks(
        _b("He was on his feet by the door.", "He sat down again."))}
    prov = FakeProvider([_verdicts({"index": 1, "is_break": True,
                                    "confidence": "high"})])
    import docproof.providers as _p
    import docproof.pipeline as _pipe
    monkeypatch.setattr(_p, "build_provider", lambda cfg, **kw: prov)
    monkeypatch.setattr(_pipe, "cache_dir_for", lambda c: None)
    findings, report = _chapter_continuity_findings(
        _cfg(), _prepared(p), Usage(), batch_reads=reads)
    assert report.proposed == 1 and len(findings) == 1
    assert len(prov.calls) == 1              # the judge only — no propose call was bought


def test_chapter_reads_from_batch_counts_missing_and_truncated():
    """A read that truncated and a read with no result at all are both failures —
    counted, never swallowed, exactly like the sync path's read failures."""
    from docproof.continuity import (chapter_read_custom_id,
                                     chapter_reads_from_batch, chapters)
    from docproof.providers.base import ProviderResult
    paras = [_para("h1", "Chapter One", "Heading1"), _para("b1", "Body one. " * 40),
             _para("h2", "Chapter Two", "Heading1"), _para("b2", "Body two. " * 40)]
    units = chapters(paras, _heading, min_tokens=1)
    assert len(units) == 2
    reads = {                                # unit 0 truncated; unit 1 absent entirely
        chapter_read_custom_id(0): ProviderResult(parsed=None,
                                                  stop_reason="max_tokens")}
    cands, dropped, read_failed = chapter_reads_from_batch(units, reads, Usage())
    assert cands == []
    assert read_failed == 2


# --- the sensitivity dial -----------------------------------------------------

def test_sensitivity_profile_is_monotonic_strict_to_loose():
    """The 1-5 dial bundles judge posture with the floor, getting looser as it
    rises: strict prompt at 1-3 then loose at 4-5, and the floor drops high ->
    medium -> low. Level 2 is the ship default."""
    from docproof.continuity import (_CHAPTER_JUDGE_SYSTEM,
                                     _CHAPTER_JUDGE_SYSTEM_LOOSE,
                                     sensitivity_profile)
    assert sensitivity_profile(1) == (_CHAPTER_JUDGE_SYSTEM, "high")
    assert sensitivity_profile(2) == (_CHAPTER_JUDGE_SYSTEM, "medium")
    assert sensitivity_profile(3) == (_CHAPTER_JUDGE_SYSTEM, "low")
    assert sensitivity_profile(4) == (_CHAPTER_JUDGE_SYSTEM_LOOSE, "medium")
    assert sensitivity_profile(5) == (_CHAPTER_JUDGE_SYSTEM_LOOSE, "low")
    # out-of-range clamps rather than raising
    assert sensitivity_profile(0) == sensitivity_profile(1)
    assert sensitivity_profile(9) == sensitivity_profile(5)
    assert Config().chapter_continuity.sensitivity == 2      # the default


def test_a_low_sensitivity_floor_drops_a_soft_affirmation(monkeypatch):
    """At sensitivity 1 the floor is 'high', so a break the judge only affirms
    'medium' is dropped below the floor; at 5 the floor is 'low' and it is kept.
    Same candidate, same verdict — only the dial moves."""
    p = _para("body-0", "He was on his feet by the door. "
                        "He sat down from the chair once more.")
    scripted = [
        _breaks(_b("He was on his feet by the door.",
                   "He sat down from the chair once more.")),
        _verdicts({"index": 1, "is_break": True, "confidence": "medium"}),
    ]
    cautious, rep_c, _ = _run(monkeypatch, _cfg(sensitivity=1), _prepared(p),
                              [r for r in scripted])
    assert cautious == [] and rep_c.below_floor == 1        # medium < high floor

    searching, rep_s, _ = _run(monkeypatch, _cfg(sensitivity=5), _prepared(p),
                               [r for r in scripted])
    assert len(searching) == 1 and rep_s.below_floor == 0   # medium >= low floor


def test_sensitivity_selects_the_judge_prompt(monkeypatch):
    """The dial's posture reaches the judge: at 5 the LOOSE prompt is what the
    judge is called with (a non-empty judge_prompt override would win instead)."""
    from docproof.continuity import _CHAPTER_JUDGE_SYSTEM_LOOSE
    p = _para("body-0", "He was on his feet. He sat down again.")
    _f, _r, prov = _run(monkeypatch, _cfg(sensitivity=5), _prepared(p), [
        _breaks(_b("He was on his feet.", "He sat down again.")),
        _verdicts({"index": 1, "is_break": True, "confidence": "high"}),
    ])
    judge_call = prov.calls[-1]                              # the judge is the last call
    assert _CHAPTER_JUDGE_SYSTEM_LOOSE.split("\n", 1)[0] in judge_call["system"]


# --- the built-in prompt ------------------------------------------------------

def test_the_default_prompt_is_available_for_the_panel():
    prompt = default_chapter_continuity_prompt()
    assert prompt and "chapter" in prompt.lower()
