"""LanguageTool mechanical-floor pass: the local rules checker proposes
candidates, and the shared `rewrite.confirm` valve rules on each. The only new
logic is `propose()`'s filter — exercised here against a fake tool, so nothing
requires Java or the LanguageTool jar. The confirm reuse (which tags findings
for this pass) and the config are covered too."""
from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from docproof import languagetool as lt
from docproof.config import Config, LanguageToolConfig
from docproof.models import ParagraphRef, Usage
from docproof.providers.base import ProviderResult
from docproof.rewrite import RewriteCandidate, confirm

from .fakes import FakeProvider


def _para(pid: str, text: str, *, reviewable: bool = True) -> ParagraphRef:
    return ParagraphRef(para_id=pid, part="word/document.xml", location="body",
                        text=text, style="Normal", reviewable=reviewable)


def _match(rule_id, issue, offset, length, replacements, matched_text):
    """A stand-in for a language_tool_python Match: only the attributes
    propose() reads. Kept faithful to the real names verified against the
    installed library (rule_id / rule_issue_type / error_length / matched_text /
    replacements as a list of plain strings)."""
    return SimpleNamespace(rule_id=rule_id, rule_issue_type=issue, offset=offset,
                           error_length=length, replacements=list(replacements),
                           matched_text=matched_text)


class _FakeTool:
    """Stands in for a running LanguageTool server: check(text) -> matches.

    Batch-aware, because propose() sends several paragraphs per request, joined
    by a blank line. This splits the request back up the way the server does and
    returns each paragraph's matches at their offset in the text it was handed —
    so the tests below are checking propose()'s offset mapping for real, not a
    fake that happens to be keyed the way propose() asks."""

    def __init__(self, by_text):
        self._by_text = by_text

    def check(self, text):
        out, at = [], 0
        for part in text.split("\n\n"):
            for m in self._by_text.get(part, []):
                out.append(_match(m.rule_id, m.rule_issue_type, m.offset + at,
                                  m.error_length, m.replacements, m.matched_text))
            at += len(part) + 2
        return out

    def close(self):
        pass


def _install(monkeypatch, by_text):
    monkeypatch.setattr(lt, "AVAILABLE", True)
    monkeypatch.setattr(lt, "_get_tool", lambda dictionary: _FakeTool(by_text))


def _verdicts(*items) -> ProviderResult:
    return ProviderResult(parsed={"verdicts": list(items)})


# --- the denylist merge -------------------------------------------------------

def test_all_disabled_rules_merges_defaults_and_config_extras():
    merged = lt.all_disabled_rules(["MY_HOUSE_RULE"])
    assert lt.DEFAULT_DISABLED_RULES <= merged
    assert "MY_HOUSE_RULE" in merged
    # the base call is just the built-in denylist
    assert lt.all_disabled_rules() == lt.DEFAULT_DISABLED_RULES


# --- propose: the only new logic ----------------------------------------------

def test_propose_returns_nothing_when_languagetool_is_absent(monkeypatch):
    monkeypatch.setattr(lt, "AVAILABLE", False)
    assert lt.propose([_para("body-0", "anything at all")]) == []


def test_propose_sets_picky_on_the_server_per_call(monkeypatch):
    # The server is long-lived and shared, so propose() must set the picky level
    # every call — a stale True would leak into a later non-picky run.
    tool = _FakeTool({})
    tool.picky = False
    monkeypatch.setattr(lt, "AVAILABLE", True)
    monkeypatch.setattr(lt, "_get_tool", lambda dictionary: tool)
    lt.propose([_para("body-0", "text")], picky=True)
    assert tool.picky is True
    lt.propose([_para("body-0", "text")], picky=False)
    assert tool.picky is False


def test_propose_keeps_a_real_fix_and_drops_every_kind_of_noise(monkeypatch):
    #        0         1
    #        0123456789012345678
    text = "aaaa bbbb cccc dddd"   # a 0:4  b 5:9  c 10:14  d 15:19
    matches = [
        _match("R_OK", "grammar", 5, 4, ["BB"], "bbbb"),               # KEEP
        _match("EN_UNPAIRED_QUOTES", "grammar", 0, 4, ["X"], "aaaa"),  # drop: built-in denylist
        _match("R_EXTRA", "grammar", 0, 4, ["Y"], "aaaa"),            # drop: config extra
        _match("R_STYLE", "style", 10, 4, ["CC"], "cccc"),             # drop: style is advice
        _match("MORFOLOGIK_RULE", "misspelling", 15, 4, ["dude"], "Dddd"),  # drop: name in lexicon
        _match("R_NOFIX", "grammar", 0, 4, [], "aaaa"),               # drop: no replacement
        _match("R_DUP", "grammar", 5, 4, ["ZZ"], "bbbb"),             # drop: duplicate span
    ]
    _install(monkeypatch, {text: matches})

    cands = lt.propose(
        [_para("body-0", text)],
        lexicon=["Dddd"],                                  # the author's own coinage
        disabled_rules=lt.all_disabled_rules(["R_EXTRA"]))

    assert len(cands) == 1
    (c,) = cands
    assert (c.para_id, c.start, c.end) == ("body-0", 5, 9)
    assert c.original == "bbbb"        # sliced from the paragraph, not the match
    assert c.replacement == "BB"


def test_propose_skips_unreviewable_paragraphs(monkeypatch):
    text = "aaaa bbbb"
    _install(monkeypatch, {text: [_match("R_OK", "grammar", 0, 4, ["X"], "aaaa")]})
    assert lt.propose([_para("body-0", text, reviewable=False)]) == []


@pytest.mark.parametrize("scan_chars", [0, 20000])
def test_propose_is_deterministic_and_reports_progress(monkeypatch, scan_chars):
    """Candidate order follows the input paragraphs — not whichever request
    finished first — and progress climbs to the total, which is the signal that
    keeps a long scan from reading as a hang. True whether the paragraphs are
    batched into one request or sent one at a time."""
    paras = [_para(f"body-{i}", f"word{i} here") for i in range(6)]
    by_text = {p.text: [_match("R_OK", "grammar", 0, 5,
                               [f"WORD{i}"], f"word{i}")]
               for i, p in enumerate(paras)}
    _install(monkeypatch, by_text)

    seen = []
    cands = lt.propose(paras, workers=4, scan_chars=scan_chars,
                       progress=lambda d, t: seen.append((d, t)))

    assert [c.para_id for c in cands] == [f"body-{i}" for i in range(6)]
    # Every candidate is anchored in its OWN paragraph, at the offset the match
    # had there — the round trip through the joined request and back.
    assert all(c.start == 0 and c.end == 5 for c in cands)
    assert [c.original for c in cands] == [f"word{i}" for i in range(6)]
    assert seen[-1] == (6, 6)                              # done climbs to total
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)  # monotonic


def test_a_match_that_straddles_a_batch_join_is_dropped(monkeypatch):
    """Paragraphs share a request, so a rule could in principle span the blank
    line between two of them. A per-paragraph check could not have produced such
    a match and there is no single paragraph to anchor it to, so it is dropped
    rather than pinned to whichever paragraph it started in."""
    a, b = _para("body-0", "alpha"), _para("body-1", "beta")

    class _StraddlingTool:
        def check(self, text):
            # One match covering "alpha\n\nbeta" end to end.
            return [_match("R_SPAN", "grammar", 0, len(text), ["X"], text)]

        def close(self):
            pass

    monkeypatch.setattr(lt, "AVAILABLE", True)
    monkeypatch.setattr(lt, "_get_tool", lambda dictionary: _StraddlingTool())
    assert lt.propose([a, b], scan_chars=20000) == []


def test_propose_survives_a_failing_check_and_keeps_the_rest(monkeypatch):
    """A request that raises is retried one paragraph at a time, so the paragraph
    the scanner chokes on costs that paragraph and not the batch it rode in
    with — the same blast radius as when every paragraph had its own request."""
    good, bad = _para("body-0", "ok text"), _para("body-1", "boom text")

    class _FlakyTool:
        def check(self, text):
            if bad.text in text:
                raise RuntimeError("server hiccup")
            return [_match("R_OK", "grammar", 0, 2, ["OK"], "ok")]

        def close(self):
            pass

    monkeypatch.setattr(lt, "AVAILABLE", True)
    monkeypatch.setattr(lt, "_get_tool", lambda dictionary: _FlakyTool())

    notes = []
    coverage = SimpleNamespace(note=lambda *a: notes.append(a))
    cands = lt.propose([good, bad], workers=2, coverage=coverage)
    assert [c.para_id for c in cands] == ["body-0"]
    # ...and the one that could not be scanned is declared, not silently thinner.
    assert notes and "1 of 2 paragraph(s)" in notes[0][1]


# --- batching -----------------------------------------------------------------

def test_groups_fill_the_character_budget_without_splitting_a_paragraph():
    texts = ["a" * 40, "b" * 40, "c" * 40, "d" * 500]
    assert lt._groups(texts, 100) == [[0, 1], [2], [3]]
    # A paragraph over the budget is its own request rather than being cut in two.
    assert lt._groups(["x" * 900], 100) == [[0]]
    # 0 is the old behaviour: one request per paragraph.
    assert lt._groups(texts, 0) == [[0], [1], [2], [3]]


def test_a_batched_scan_finds_what_the_per_paragraph_scan_finds(monkeypatch):
    """The two paths agree candidate for candidate — the property that makes
    batching a speed-up rather than a change to what the pass proposes."""
    paras = [_para(f"body-{i}", f"the {w} was here") for i, w in
             enumerate(("cat", "dog", "bird", "fish", "mouse"))]
    by_text = {p.text: [_match("R_OK", "grammar", 4, len(w), [w.upper()], w)]
               for p, w in zip(paras, ("cat", "dog", "bird", "fish", "mouse"))}
    _install(monkeypatch, by_text)

    def key(cs):
        return [(c.para_id, c.start, c.end, c.original, c.replacement) for c in cs]

    assert key(lt.propose(paras, scan_chars=20000)) == \
        key(lt.propose(paras, scan_chars=0))


# --- config -------------------------------------------------------------------

def test_config_defaults_are_off_and_conservative():
    c = LanguageToolConfig()
    assert c.enabled is False
    assert c.dictionary == "en-US"
    assert c.edit_confidence == "high"       # deterministic but context-blind
    # Room for the model's THINKING as well as its verdicts: a batch of 40 needs
    # ~4,400 output tokens at effort medium and ~8,100 at high on a reasoning
    # model, so the old 4,000 truncated on real input — and a truncated
    # structured response carries no verdicts at all.
    assert c.max_output_tokens == 16000


def test_top_level_config_carries_languagetool_off_by_default():
    assert Config().languagetool.enabled is False


def test_confirm_model_must_be_in_the_catalog_only_when_enabled():
    with pytest.raises(ValidationError, match="not in the catalog"):
        LanguageToolConfig(enabled=True, confirm_model="no-such-model")
    # off, an unknown model is never used, so it is not validated
    LanguageToolConfig(enabled=False, confirm_model="no-such-model")


# --- confirm reuse: findings are tagged for this pass -------------------------

def test_confirm_tags_findings_for_the_languagetool_pass():
    """LanguageTool rides rewrite.confirm but must own its label and id prefix,
    so findings from it read as `languagetool`, not `rewrite`."""
    p = _para("body-0", "He holds out hand for the bottle.")
    cand = RewriteCandidate("body-0", 13, 13, "", "a ")
    prov = FakeProvider([_verdicts(
        {"index": 1, "is_error": True, "confidence": "high"})])

    out = confirm([cand], [p], prov, model="m", max_tokens=100, usage=Usage(),
                  ids=itertools.count(1), error_type="languagetool",
                  chunk_id="languagetool", id_prefix="lt")

    assert len(out) == 1
    assert out[0].error_type == "languagetool"
    assert out[0].finding_id.startswith("lt-")
    assert out[0].corrected_text == "He holds out a hand for the bottle."
