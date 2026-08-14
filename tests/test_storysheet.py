"""The whole-book story sheet: parsing the model's read, rendering it as a
system-prompt section, and caching it per draft."""
from __future__ import annotations

from pathlib import Path

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


def test_prepare_reads_the_book_once_across_a_batch_submit_and_collect(
        tmp_path, monkeypatch):
    """A batch review calls prepare twice — once to build and send the requests,
    once at collect to rebuild the same chunking — and the story sheet is the one
    whole-book read that happens inside prepare. Without a cache folder that is
    two reads of the entire manuscript for one job, and the second one is thrown
    away: at collect the detector prompts it feeds are only used on the recovery
    path. With the folder wired (load_config now fills it in), the second prepare
    is served from disk."""
    import docx

    from docproof.config import load_config
    from docproof.pipeline import prepare

    root = Path(__file__).parent.parent
    doc = docx.Document()
    for i in range(4):
        doc.add_paragraph(f"She walked on, and paragraph {i} said so plainly.")
    src = tmp_path / "book.docx"
    doc.save(src)

    prov = FakeProvider([_result(narration="third person past", characters=[],
                                 notes=[])])
    monkeypatch.setattr("docproof.providers.build_provider",
                        lambda cfg, **kw: prov)

    cfg = load_config(str(root / "config" / "default.yaml"))
    cfg.storysheet.enabled = True
    cfg.storysheet.cache_dir = str(tmp_path / "cache")

    first = prepare(cfg, src, root / "config" / "error_types")
    second = prepare(cfg, src, root / "config" / "error_types")

    assert "third person past" in first.story_sheet
    assert second.story_sheet == first.story_sheet
    assert len(prov.calls) == 1, "the second prepare re-read the whole book"
