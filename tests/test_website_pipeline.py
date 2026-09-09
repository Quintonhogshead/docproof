from __future__ import annotations

from docproof.providers import NormalizedUsage, ProviderResult
from docproof.website import (Asset, PipelineConfig, Questionnaire, SiteSpec,
                              SourceBundle, WebsitePipeline)
from docproof.website.pipeline import ModelCapability, _chunk_manuscript
from tests.fakes import FakeProvider


SHA = "a" * 64


def _asset() -> Asset:
    return Asset(id="cover-1", filename="cover.jpg", media_type="image/jpeg",
                 sha256=SHA, alt="The book cover", approved=True)


def _bundle() -> SourceBundle:
    return SourceBundle(
        project_id="project-1",
        public_hubspot={"author_name": "Canonical Name", "book_title": "Canonical Book",
                        "publication_date": "2026-10-01", "retailer_url": "https://book.example/buy"},
        questionnaire=Questionnaire(public_name="Different Form Name", book_title="Different Form Book",
                                    publication_date="2027-01-01", cover_asset_id="cover-1",
                                    private_notes="DO NOT PUT THIS ON THE SITE"),
        manuscript="Mira crosses the Aldous bridge at first light.\nShe keeps the letter hidden.\n",
        assets=[_asset()], fingerprint="source-1")


def _spec(headline: str = "A story about crossing the river") -> dict:
    return {
        "schema_version": 1, "renderer_version": "1.0", "template_id": "literary-journal",
        "author": {"name": "Hallucinated", "bio_short": "A thoughtful novelist writing enduring stories.",
                   "bio": "A thoughtful novelist writing enduring stories about courage and change.",
                   "portrait_asset_id": ""},
        "headline": headline, "intro": "A novel of courage, letters, and a crossing before nightfall.",
        "primary_cta": {"label": "Click", "url": "https://unsafe.example"},
        "books": [{"id": "made-up", "title": "Wrong", "subtitle": "", "description": "A long enough description grounded in the supplied book evidence.",
                   "hook": "One woman crosses a bridge before dark.", "cover_asset_id": "made-up",
                   "publication_date": "", "links": []}],
        "contact": {"email": "wrong@example.test", "headline": "Contact", "message": "Get in touch.", "links": []},
        "praise": [], "excerpt": "", "seo": {"description": "A novel of courage and secrets."},
    }


def _chunk() -> dict:
    return {"chunk_id": "whatever", "summary": "Mira crosses a bridge with a letter.",
            "premise": "A woman crosses a bridge.", "themes": ["courage"],
            "audience_signals": ["literary suspense"], "spoiler_boundaries": ["Do not reveal the letter."],
            "evidence": [{"chunk_id": "whatever", "quote": "Mira crosses the Aldous bridge", "note": "Opening action."}]}


def _synthesis() -> dict:
    return {"premise": "Mira crosses a bridge while keeping a letter hidden.", "themes": ["courage"],
            "audience": "Readers of quiet literary suspense.", "voice": "tense and intimate",
            "spoiler_boundaries": ["Do not reveal the letter."],
            "evidence": [{"chunk_id": "chunk-0001", "quote": "", "note": "Opening action."}]}


def _verification() -> dict:
    return {"findings": [{"path": "headline", "supported": True,
                           "message": "Grounded in the brief.", "severity": "info"}]}


def _pipeline(results, **kwargs) -> WebsitePipeline:
    cfg = PipelineConfig(capabilities={"test-model": ModelCapability(16_000, 8_192)}, **kwargs)
    return WebsitePipeline(FakeProvider(results=[ProviderResult(parsed=x, usage=NormalizedUsage()) for x in results]), cfg)


def test_pipeline_uses_all_chunks_and_keeps_canonical_and_private_data_out_of_copy_prompt():
    pipeline = _pipeline([_chunk(), _synthesis(), _spec(), _verification()])
    result = pipeline.run(_bundle(), model="test-model")
    assert result.brief.complete
    assert result.brief.chunk_ids == ["chunk-0001"]
    assert result.spec.author.name == "Canonical Name"
    assert result.spec.books[0].title == "Canonical Book"
    assert result.spec.books[0].publication_date == "2026-10-01"
    assert result.spec.books[0].links[0].url == "https://book.example/buy"
    assert "DO NOT PUT THIS" not in pipeline.provider.calls[2]["user"]
    assert result.validation.status == "failed"  # staff resolves canonical conflict


def test_cached_chunks_are_not_called_again_and_locked_staff_text_survives_revision():
    cache = {"website:source-1:chunk:chunk-0001": _chunk()}
    previous = SiteSpec.model_validate(_spec("Staff's approved headline"))
    pipeline = _pipeline([_synthesis(), _spec("New generated headline"), _verification()])
    result = pipeline.run(_bundle(), model="test-model", cache=cache,
                          previous_spec=previous, locked_paths=["headline"])
    assert result.spec.headline == "Staff's approved headline"
    assert len(pipeline.provider.calls) == 3


def test_revision_instructions_reach_composition_and_change_its_cache_key():
    cache = {}
    first = _pipeline([_chunk(), _synthesis(), _spec("First headline"), _verification()])
    first.run(_bundle(), model="test-model", cache=cache, instructions="Make the opening more intimate.")
    composition = [call for call in first.provider.calls if call["schema_name"] == "website_composition"][0]
    assert "Trusted staff revision request" in composition["user"]
    assert "Make the opening more intimate." in composition["user"]

    # The same request can use its cached composition. A different request must
    # make a fresh call, rather than silently returning the prior revision.
    second = _pipeline([_verification()])
    second.run(_bundle(), model="test-model", cache=cache, instructions="Make the opening more intimate.")
    assert [call["schema_name"] for call in second.provider.calls] == ["website_verification"]
    third = _pipeline([_spec("Different direction"), _verification()])
    revised = third.run(_bundle(), model="test-model", cache=cache,
                        instructions="Make the opening warmer.")
    assert revised.spec.headline == "Different direction"
    assert any(call["schema_name"] == "website_composition" for call in third.provider.calls)


def test_unavailable_verifier_cannot_be_reported_as_a_pass():
    pipeline = _pipeline([_chunk(), _synthesis(), _spec(), {"bad": True}, {"bad": True}])
    result = pipeline.run(_bundle(), model="test-model")
    assert result.validation.status == "unavailable"
    assert result.validation.findings[-1].code == "verification_unavailable"


def test_long_book_chunking_covers_every_character_and_caches_every_chunk():
    manuscript = ("A paragraph with enough words to need its own chunk.\n" * 6)
    chunks = _chunk_manuscript(manuscript, 10)
    assert "".join(text for _, text in chunks) == manuscript
    bundle = _bundle().model_copy(update={"manuscript": manuscript, "fingerprint": "long-source"})
    results = [_chunk() for _ in chunks] + [_synthesis(), _spec(), _verification()]
    class FalseyDiskCache(dict):
        # Some disk-backed mapping adapters are false when currently empty.
        # The pipeline must test against None, not use ``cache or {}``.
        def __bool__(self):
            return False
    cache = FalseyDiskCache()
    pipeline = _pipeline(results, chunk_tokens=10)
    output = pipeline.run(bundle, model="test-model", cache=cache)
    assert output.brief.complete
    assert output.brief.chunk_ids == [chunk_id for chunk_id, _ in chunks]
    assert all(f"website:long-source:chunk:{chunk_id}" in cache for chunk_id, _ in chunks)


def test_malformed_output_gets_one_bounded_repair():
    pipeline = _pipeline([_chunk(), _synthesis(), {"nope": True}, _spec(), _verification()])
    pipeline.run(_bundle(), model="test-model")
    assert len(pipeline.provider.calls) == 5


def test_unconfigured_model_fails_before_any_provider_call():
    pipeline = WebsitePipeline(FakeProvider(), PipelineConfig(capabilities={}))
    try:
        pipeline.run(_bundle(), model="unknown")
    except Exception as exc:
        assert "no configured capability" in str(exc)
    else:
        raise AssertionError("expected capability failure")
