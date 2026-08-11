"""The whole-book story sheet: parsing the model's read, rendering it as a
system-prompt section, and caching it per draft."""
from __future__ import annotations

from docproof.models import ParagraphRef, Usage
from docproof.providers.base import ProviderResult
from docproof.storysheet import StorySheet, build_storysheet, prompt_section

from .fakes import FakeProvider


def _para(pid: str, text: str) -> ParagraphRef:
    return ParagraphRef(para_id=pid, part="word/document.xml", location="body",
                        text=text, style="Normal")


def _result(**kw) -> ProviderResult:
    return ProviderResult(parsed=kw)


def test_build_parses_a_story_sheet():
    prov = FakeProvider([_result(
        narration="first person, past tense, narrated by Wren",
        characters=[{"name": "Rían", "pronouns": "he/him"}],
        notes=["The narrator is a man."])])
    s = build_storysheet([_para("body-0", "x")], prov, model="m",
                         max_tokens=100, usage=Usage())
    assert s.narration.startswith("first person")
    assert s.characters[0].pronouns == "he/him"


def test_build_degrades_to_empty_on_failure():
    prov = FakeProvider([ProviderResult(stop_reason="refusal")])
    s = build_storysheet([_para("body-0", "x")], prov, model="m",
                         max_tokens=100, usage=Usage())
    assert s.narration == "" and s.characters == []


def test_prompt_section_renders_the_useful_facts():
    s = StorySheet(narration="first person, past tense, narrated by Wren",
                   characters=[{"name": "Rían", "pronouns": "he/him"},
                               {"name": "Mob", "pronouns": ""}],  # no pronouns -> skipped
                   notes=["The narrator is a man."])
    section = prompt_section(s)
    assert "Narration: first person" in section
    assert "Rían — he/him" in section
    assert "Mob" not in section                       # only pronoun-bearing chars
    assert "narrator is a man" in section


def test_prompt_section_of_an_empty_sheet_is_blank():
    assert prompt_section(StorySheet()) == ""


def test_story_sheet_cache_reuses_without_a_second_call(tmp_path):
    prov = FakeProvider([_result(narration="third person past", characters=[],
                                 notes=[])])
    paras = [_para("body-0", "The city slept under a cold moon.")]
    build_storysheet(paras, prov, model="m", max_tokens=100, usage=Usage(),
                     cache_dir=str(tmp_path))
    prov2 = FakeProvider([])                            # empty queue
    s = build_storysheet(paras, prov2, model="m", max_tokens=100, usage=Usage(),
                         cache_dir=str(tmp_path))
    assert s.narration == "third person past"
    assert prov2.calls == []                            # served from cache
