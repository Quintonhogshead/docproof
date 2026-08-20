import pytest

from docproof.context_service import ContextService
from docproof.models import DocumentModel, ParagraphRef
from docproof.providers import ProviderResult
from docproof.site_judge import judge_packet, parse_judgments
from docproof.site_ledger import IncompleteVerdicts
from docproof.site_models import ExaminationSite, SiteAnchor
from docproof.site_packet import build_packets
from .fakes import FakeProvider


def _doc():
    para = ParagraphRef("body-0000", "word/document.xml", "body",
                        "The teh cat slept.", "Normal")
    return DocumentModel("book.docx", (para,))


def _site(site_id, start, end):
    return ExaminationSite(
        site_id=site_id, site_type="spelling",
        anchors=(SiteAnchor(part="word/document.xml", paragraph_id="body-0000",
                            start_offset=start, end_offset=end),),
        generator="test", evidence={"word": "teh"},
        context_recipe=("current sentence", "current paragraph"))


def test_packets_reuse_context_and_keep_every_site_id():
    packet = build_packets(
        [_site("X-a", 4, 7), _site("X-b", 8, 11)],
        ContextService(_doc()), batch_size=100)[0]
    assert packet.site_ids == ("X-a", "X-b")
    assert len(packet.contexts) == 1
    assert packet.sites[0].context_id == packet.sites[1].context_id

    verdicts = parse_judgments(packet, {
        "pass_ids": ["X-b"],
        "errors": [{"site_id": "X-a", "correction": "the",
                    "explanation": "transposed letters", "confidence": "high"}],
        "uncertain": [], "defer_ids": [],
    }, judge="fake")
    assert [v.site_id for v in verdicts] == ["X-a", "X-b"]
    assert [v.decision for v in verdicts] == ["error", "pass"]


def test_missing_packet_verdict_is_not_a_pass():
    packet = build_packets(
        [_site("X-a", 4, 7), _site("X-b", 8, 11)],
        ContextService(_doc()))[0]
    with pytest.raises(IncompleteVerdicts) as exc:
        parse_judgments(packet, {
            "pass_ids": ["X-a"], "errors": [], "uncertain": [],
            "defer_ids": [],
        }, judge="fake")
    assert exc.value.missing == ("X-b",)


def test_judge_retries_only_the_missing_site():
    packet = build_packets(
        [_site("X-a", 4, 7), _site("X-b", 8, 11)],
        ContextService(_doc()))[0]
    provider = FakeProvider([
        ProviderResult(parsed={
            "pass_ids": ["X-a"], "errors": [], "uncertain": [],
            "defer_ids": [],
        }),
        ProviderResult(parsed={
            "pass_ids": ["X-b"], "errors": [], "uncertain": [],
            "defer_ids": [],
        }),
    ])
    verdicts = judge_packet(
        packet, provider, model="fake-judge", max_tokens=500)
    assert [v.site_id for v in verdicts] == ["X-a", "X-b"]
    assert len(provider.calls) == 2
    assert '"site_id":"X-a"' in provider.calls[0]["user"]
    assert '"site_id":"X-a"' not in provider.calls[1]["user"]
    assert '"site_id":"X-b"' in provider.calls[1]["user"]
