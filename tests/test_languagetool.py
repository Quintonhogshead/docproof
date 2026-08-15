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
    """Stands in for a running LanguageTool server: check(text) -> matches."""

    def __init__(self, by_text):
        self._by_text = by_text

    def check(self, text):
        return self._by_text.get(text, [])

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
