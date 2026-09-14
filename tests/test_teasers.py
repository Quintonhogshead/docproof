from __future__ import annotations

from dataclasses import replace
import io
import json
from pathlib import Path
import time

from docx import Document
from fastapi.testclient import TestClient
import pytest

from app.jobs import Job, JobRunner, JobStore
from app.main import create_app
from app.settings import Paths, Settings
from app.teasers import (Queue, accept_story, generate_draft, accept_review,
                         MAX_DRAFTS_PER_CYCLE, MAX_DRAFTS_PER_DAY, TeaserError, accept_writer_brief)
from app.teaser_delivery import deliver, ensure_folder, verify_document
from docproof.providers.base import ProviderResult
from docproof.teasers import QWEN_MODEL, SOL_MODEL
from docproof.teasers import pipeline
from docproof.teasers.document import write_document
from docproof.teasers.models import (Draft, Teaser, Element, Fact, Storysheet,
    Review, OptionCheck, SmallEdit, WriterBrief, apply_small_edits, digest, draft_issues, approval_issues)


@pytest.fixture
def story(draft):
    return Storysheet(title="The Ferry Ledger", author="", source_complete=True,
        source_limitations=[], reader_promise="A quiet family reconciliation on a working island.",
        narrative_center="Mara and her brother", premise="Mara returns to repair the island ferry.",
        central_pressure="They disagree about selling their father's boat.", stakes="Their bond and the crossing.",
        genre_and_audience="Adult family fiction", voice="Intimate and restrained",
        public_facts=[Fact(claim="Mara returns to repair the ferry.", paragraph_ids=[1])],
        conditional_disclosures=[], protected_revelations=["The final decision about the boat."],
        five_angles=["Return", "Siblings", "Island", "Inheritance", "Repair"],
        qwen_instructions="Ground all five options in Mara's return and the siblings' dilemma.",
        writer_brief=WriterBrief(title="The Ferry Ledger", author="",
            public_setup="Mara returns to repair the ferry; her brother wants to sell it.",
            reader_promise="A restrained family story.", central_pressure="A disputed inheritance.",
            stakes="Their relationship and the ferry's future.", genre_and_audience="Adult family fiction",
            voice="Restrained and concrete", public_facts=["Mara returns; her brother wants to sell the ferry."],
            five_angles=["Return", "Siblings", "Island", "Inheritance", "Repair"],
            writing_instructions="Leave the final decision unresolved.", author_copy=draft.model_copy(deep=True)))


@pytest.fixture
def draft():
    # Mechanical fixture: realistic-length content, deliberately not a model-quality benchmark.
    base = ("Mara returns to the island to repair her father's ferry and finds her brother waiting "
            "at the landing. The crossing has always connected their family to the mainland, "
            "but keeping it open means deciding what they owe the place they left behind. "
            "Every repair draws them into a conversation neither has learned to finish. "
            "The boat offers work they understand, while the familiar shore gives them "
            "reasons to question the lives they have built elsewhere. ")
    teasers = []
    for n in range(1, 6):
        opening = ["Coming home requires more than a ticket.", "A boat can carry an unfinished argument.",
                   "The island remembers both of its children.", "An inheritance can demand a different future.",
                   "Some repairs begin with a difficult conversation."][n-1]
        teasers.append(Teaser(number=n, angle=["Return", "Siblings", "Island", "Inheritance", "Repair"][n-1],
                             paragraphs=[opening + " " + base, base]))
    return Draft(teasers=teasers, opening_hooks=["Coming home requires more than a ticket.",
                 "A boat can carry an unfinished argument.", "Some repairs begin with a difficult conversation."],
        editorial_note="These options emphasize family pressure and a working island while protecting the final decision.",
        elements=[Element(name=name, purpose="Make the invitation clear.",
                          book_specific_guidance="Keep Mara's return tied to the ferry and her brother.")
                  for name in ["Opening", "Narrative center", "Disruption", "Stakes", "Ending"]],
        best_practices=["Keep the ferry concrete.", "Retain the restrained tone.", "Preserve both siblings' agency.",
                        "Protect the final decision.", "Read the copy aloud."],
        modification_checklist=["Check names.", "Check length.", "Check spoilers.", "Check voice.", "Check clarity."])


def approved(draft, chunk_ids=(1,)):
    return Review(draft_sha256=digest(draft), covered_chunk_ids=list(chunk_ids), approved=True,
                  recommended_option=2, guidance_approved=True, feedback=[],
                  options=[OptionCheck(number=i, accurate=True, spoiler_safe=True, clear=True,
                                       faithful_voice=True, distinct_angle=True, feedback="") for i in range(1, 6)])


@pytest.fixture
def queued(tmp_path):
    path = tmp_path / "manuscript.docx"
    doc = Document()
    doc.add_paragraph("Mara returns to repair her father's ferry. Her brother wants to sell it.")
    doc.add_paragraph("They repair the engine together and agree to keep the crossing open.")
    doc.save(path)
    job = Job(id="format-1", filename="Smith - Book Original.docx", source_path=str(path),
              model="test", mode="now", kind="prep", state="done", owner_id="owner")
    queue = Queue(tmp_path / "watch")
    queue.configure(enabled=True)
    queue.add(job)
    return queue, job, queue.claim("worker")


def drafted(queued, story, draft):
    queue, job, task = queued
    task = accept_story(queue, task, story.model_dump())
    class Provider:
        def complete_structured(self, **kw):
            assert kw["model"] == QWEN_MODEL
            assert "five" in kw["user"]
            return ProviderResult(parsed=draft.model_dump())
    return queue, generate_draft(queue, task, provider=Provider())


def next_brief(queue, task, story):
    return accept_writer_brief(queue, task, {"brief": story.writer_brief.model_dump(),
        "draft_sha256": task["drafts"][-1]["sha256"],
        "review_sha256": digest(Review.model_validate(task["reviews"][-1]))})


def test_five_options_and_gate(draft):
    assert draft_issues(draft) == []
    review = approved(draft)
    assert approval_issues(draft, review, [1]) == []
    changed = draft.model_copy(deep=True)
    changed.teasers[0].paragraphs[0] += " Unreviewed text."
    assert any("does not match" in s for s in approval_issues(changed, review, [1]))
    review.options[2].spoiler_safe = False
    assert approval_issues(draft, review, [1])
    assert approval_issues(draft, approved(draft), [1, 2])


def test_wire_schema_preserves_book_title(story):
    from docproof.providers import strict_json_schema
    from docproof.providers.base import inlined_json_schema
    from galley.codex_runner import _check_schema
    schema = inlined_json_schema(strict_json_schema(Storysheet))
    assert "title" in schema["properties"]
    assert "title" in schema["required"]
    assert schema["properties"]["title"] == {"type": "string"}
    _check_schema(schema)


def test_rephrasing_requests_direct_responses_without_changing_provider_defaults():
    from docproof.providers.deepinfra_provider import DeepInfraProvider
    args = dict(model=QWEN_MODEL, system="Rephrase faithfully.", user="Finished copy.",
                schema={"type": "object", "properties": {}, "additionalProperties": False},
                schema_name="teaser", max_tokens=100)
    direct = DeepInfraProvider(api_key="test-key", effort=None, reasoning_enabled=False)._body(**args)
    assert direct["extra_body"] == {"reasoning": {"enabled": False}}
    assert "reasoning_effort" not in direct
    default = DeepInfraProvider(api_key="test-key")._body(**args)
    assert "extra_body" not in default


def test_qwen_never_receives_private_ending_or_rejected_copy(queued, story, draft):
    secret = "PRIVATE_ENDING_SENTINEL"
    for name, value in story.model_dump().items():
        if isinstance(value, str):
            setattr(story, name, secret)
    story.protected_revelations = [secret]
    story.public_facts = [Fact(claim="A safe setup fact.", paragraph_ids=[1])]
    queue, _, task = queued
    task["chunks"][0]["paragraphs"][0]["text"] += " " + secret
    queue.save(task)
    task = accept_story(queue, task, story.model_dump())
    bad = draft.model_copy(deep=True)
    bad.teasers[0].paragraphs[0] += " " + secret
    class Initial:
        def complete_structured(self, **kw):
            assert secret not in kw["user"] and secret not in kw["system"]
            assert "SOL'S FINISHED COPY" in kw["user"]
            assert json.dumps(story.writer_brief.author_copy.model_dump(), ensure_ascii=False) in kw["user"]
            assert story.writer_brief.public_setup not in kw["user"]
            assert story.writer_brief.writing_instructions not in kw["user"]
            return ProviderResult(parsed=bad.model_dump())
    task = generate_draft(queue, task, provider=Initial())
    review = approved(bad)
    review.approved = False
    review.options[0].spoiler_safe = False
    review.guidance_approved = False
    review.feedback = ["Remove " + secret]
    task = accept_review(queue, task, review.model_dump())
    task["feedback"].append(secret)
    class Revision:
        def complete_structured(self, **kw):
            assert secret not in kw["user"] and secret not in kw["system"]
            assert "Make no editorial decisions" in kw["system"]
            assert "PUBLIC REVISION NOTES" not in kw["user"]
            assert "APPROVED OPTIONS TO PRESERVE:\n[2, 3, 4, 5]" in kw["user"]
            return ProviderResult(parsed=draft.model_dump())
    task = next_brief(queue, task, story)
    result = generate_draft(queue, task, provider=Revision())
    assert secret not in json.dumps(result["writer_handoffs"])


def test_legacy_task_refreshes_private_brief_before_any_writer_call(queued, story):
    queue, _, task = queued
    task["storysheet"] = story.model_dump(exclude={"writer_brief"})
    task["state"] = "story_ready"
    queue.save(task)
    result = generate_draft(queue, task, provider=object())
    assert result["state"] == "queued"
    assert "storysheet" not in result and "generation_times" not in result
    assert result["prior_storysheets"]


def test_outline_only_task_must_get_finished_sol_copy(queued, story):
    queue, _, task = queued
    task["storysheet"] = story.model_dump()
    task["storysheet"]["writer_brief"].pop("author_copy")
    queue.save(task, "story_ready")
    result = generate_draft(queue, queue.get(task["id"]), provider=object())
    assert result["state"] == "queued" and "generation_times" not in result


def test_changed_sol_copy_invalidates_previously_passing_rephrasing(queued, story, draft):
    queue, task = drafted(queued, story, draft)
    review = approved(draft)
    review.approved = False
    task = accept_review(queue, task, review.model_dump())
    story.writer_brief.author_copy.teasers[0].paragraphs[0] = (
        story.writer_brief.author_copy.teasers[0].paragraphs[0].replace("Mara returns", "Mara comes back"))
    task = next_brief(queue, task, story)
    class Provider:
        def complete_structured(self, **kw):
            assert "APPROVED OPTIONS TO PRESERVE:\n[2, 3, 4, 5]" in kw["user"]
            return ProviderResult(parsed=story.writer_brief.author_copy.model_dump())
    task = generate_draft(queue, task, provider=Provider())
    assert "Mara comes back" in task["drafts"][-1]["content"]["teasers"][0]["paragraphs"][0]


def test_revised_spoiler_boundary_does_not_reuse_old_approved_copy(queued, story, draft):
    queue, task = drafted(queued, story, draft)
    review = approved(draft)
    review.approved = False
    task = accept_review(queue, task, review.model_dump())
    task["storysheet"]["protected_revelations"].append("A newly protected development.")
    class Revision:
        def complete_structured(self, **kw):
            assert 'APPROVED COPY ONLY:\n{"teasers": []}' in kw["user"]
            return ProviderResult(parsed=draft.model_dump())
    task = next_brief(queue, task, story)
    result = generate_draft(queue, task, provider=Revision())
    assert result["drafts"][-1]["retained_from"] is None


def test_broader_revision_waits_for_bound_public_brief(queued, story, draft):
    queue, task = drafted(queued, story, draft)
    review = approved(draft)
    review.approved = False
    review.options[0].accurate = False
    task = accept_review(queue, task, review.model_dump())
    assert task["state"] == "brief_ready"
    with pytest.raises(TeaserError, match="cannot generate"):
        generate_draft(queue, task, provider=object())
    brief = story.writer_brief.model_copy(deep=True)
    brief.writing_instructions = "Describe the ferry as temporarily out of service for inspection."
    payload = {"brief": brief.model_dump(), "draft_sha256": digest(draft), "review_sha256": "wrong"}
    with pytest.raises(TeaserError, match="does not match"):
        accept_writer_brief(queue, task, payload)
    payload["review_sha256"] = digest(review)
    task = accept_writer_brief(queue, task, payload)
    assert task["state"] == "story_ready"
    assert accept_writer_brief(queue, task, payload)["writer_brief"] == brief.model_dump()
    class Revision:
        def complete_structured(self, **kw):
            assert brief.writing_instructions not in kw["user"]
            assert json.dumps(brief.author_copy.model_dump(), ensure_ascii=False) in kw["user"]
            return ProviderResult(parsed=draft.model_dump())
    result = generate_draft(queue, task, provider=Revision())
    assert result["writer_handoffs"][-1]["author_copy"] == brief.author_copy.model_dump()


def test_count_structure_and_guidance_cannot_be_omitted(draft):
    draft.teasers.pop()
    draft.elements = []
    draft.modification_checklist = []
    assert len(draft_issues(draft)) == 3


def test_whole_manuscript_including_giant_paragraph(monkeypatch):
    monkeypatch.setattr(pipeline, "CHUNK_CHARS", 80)
    text = "Beginning " + "x" * 180 + "\nMiddle\nEnding"
    source = pipeline.chunks(text)
    assert len(source) > 1
    assert "".join(p["text"] for c in source for p in c["paragraphs"]) == text.replace("\n", "")
    assert [p["id"] for c in source for p in c["paragraphs"]] == list(range(1, 8))
    with pytest.raises(ValueError):
        pipeline.chunks(" \n")


def test_queue_snapshots_source_and_never_duplicates(queued):
    queue, job, task = queued
    before = task["chunks"]
    Path(job.source_path).unlink()
    assert queue.add(job) == task["id"]
    assert queue.get(task["id"])["chunks"] == before
    assert len(queue.list()) == 1
    assert queue.claim("other") is None
    with pytest.raises(TeaserError):
        queue.owned(task["id"], "other")


def test_disabled_failed_and_nonformatting_jobs_do_not_queue(queued):
    queue, job, task = queued
    queue.configure(enabled=False)
    assert queue.add(replace(job, id="other")) is None
    queue.configure(enabled=True)
    assert queue.add(replace(job, id="other", state="failed")) is None
    assert queue.add(replace(job, id="other", kind="promo")) is None


def test_solicited_story_cannot_cite_unavailable_source(queued, story):
    queue, _, task = queued
    story.public_facts[0].paragraph_ids = [999]
    with pytest.raises(ValueError, match="missing manuscript"):
        accept_story(queue, task, story.model_dump())


def test_complete_source_can_have_qualified_character_perspectives(queued, story):
    queue, _, task = queued
    story.source_limitations = ["The father's motives appear through the siblings' differing memories."]
    assert accept_story(queue, task, story.model_dump())["state"] == "story_ready"
    story.source_complete = False
    with pytest.raises(ValueError, match="source limitation"):
        pipeline.validate_story(story, task["chunks"])


def test_qwen_result_reused_and_wrong_review_blocked(queued, story, draft):
    queue, task = drafted(queued, story, draft)
    assert generate_draft(queue, task, provider=object())["drafts"] == task["drafts"]
    review = approved(draft)
    review.draft_sha256 = "wrong"
    with pytest.raises(TeaserError):
        accept_review(queue, task, review.model_dump())
    assert queue.get(task["id"])["state"] == "drafted"
    task = accept_review(queue, task, approved(draft).model_dump())
    assert task["state"] == "approved"


def test_failed_review_revises_automatically_and_refreshes_brief(queued, story, draft):
    queue, task = drafted(queued, story, draft)
    review = approved(draft)
    review.approved = False
    review.feedback = ["Make the relationship pressure more specific."]
    task = accept_review(queue, task, review.model_dump())
    assert task["state"] == "brief_ready"
    task["drafts"] *= MAX_DRAFTS_PER_CYCLE
    task["state"] = "drafted"
    queue.save(task)
    task = accept_review(queue, task, review.model_dump())
    assert task["state"] == "retry_wait"
    assert task["resume_state"] == "queued"
    assert "storysheet" not in task
    task["retry_at"] = 0
    queue.save(task)
    queue.recover()
    assert queue.get(task["id"])["state"] == "queued"


def test_revision_preserves_passing_qwen_options_but_requires_fresh_review(queued, story, draft):
    queue, task = drafted(queued, story, draft)
    prior_hash = task["drafts"][-1]["sha256"]
    review = approved(draft)
    review.approved = False
    review.guidance_approved = False
    for option in review.options:
        option.accurate = option.number == 2
    task = accept_review(queue, task, review.model_dump())
    changed = draft.model_copy(deep=True)
    for option in changed.teasers:
        option.paragraphs[0] = "New wording. " + option.paragraphs[0]
    class Revision:
        def complete_structured(self, **kw):
            assert "APPROVED OPTIONS TO PRESERVE:\n[2]" in kw["user"]
            return ProviderResult(parsed=changed.model_dump())
    task = next_brief(queue, task, story)
    result = generate_draft(queue, task, provider=Revision())
    content = Draft.model_validate(result["drafts"][-1]["content"])
    assert content.teasers[1] == draft.teasers[1]
    assert content.teasers[0] == changed.teasers[0]
    assert result["drafts"][-1]["retained_from"]["options"] == [2]
    assert result["drafts"][-1]["sha256"] != prior_hash
    assert result["state"] == "drafted"
    with pytest.raises(TeaserError, match="different draft"):
        accept_review(queue, result, approved(draft).model_dump())


def test_generation_daily_ceiling_resumes_without_editor(queued, story, draft):
    queue, _, task = queued
    task = accept_story(queue, task, story.model_dump())
    task["generation_times"] = [time.time()] * MAX_DRAFTS_PER_DAY
    task = generate_draft(queue, task, provider=object())
    assert task["state"] == "retry_wait"
    assert task["retry_at"] > time.time() + 86000


def correction(draft):
    review = approved(draft)
    review.approved = False
    review.options[0].accurate = False
    review.edits = [SmallEdit(field="teaser", index=1, paragraph=1, before="Mara returns",
                             after="Mara comes back", reason="Clarify the return.", paragraph_ids=[1])]
    return review


def test_small_sol_edit_is_exact_durable_and_requires_new_approval(queued, story, draft):
    queue, task = drafted(queued, story, draft)
    review = correction(draft)
    corrected = accept_review(queue, task, review.model_dump())
    assert corrected["state"] == "drafted"
    entry = corrected["drafts"][-1]
    result = Draft.model_validate(entry["content"])
    assert result.teasers[0].paragraphs[0] == draft.teasers[0].paragraphs[0].replace("Mara returns", "Mara comes back")
    assert result.teasers[0].paragraphs[1] == draft.teasers[0].paragraphs[1]
    assert result.teasers[1:] == draft.teasers[1:]
    assert result.elements == draft.elements
    assert entry["model"] == SOL_MODEL and entry["base_sha256"] == digest(draft)
    assert entry["sha256"] != digest(draft)
    assert accept_review(queue, corrected, review.model_dump())["drafts"] == corrected["drafts"]
    with pytest.raises(TeaserError, match="Only an approved"):
        deliver(queue, corrected, queue.root.parent)
    assert approval_issues(draft, review, [1])
    assert accept_review(queue, corrected, approved(result).model_dump())["state"] == "approved"


def test_sol_can_correct_and_approve_in_one_pass(queued, story, draft):
    queue, task = drafted(queued, story, draft)
    review = correction(draft)
    review.approved = True
    review.options[0].accurate = True
    result = accept_review(queue, task, review.model_dump())
    assert result["state"] == "approved"
    corrected = Draft.model_validate(result["drafts"][-1]["content"])
    final_review = Review.model_validate(result["reviews"][-1])
    assert not approval_issues(corrected, final_review, [1])
    assert len(result["reviews"]) == 1 and len(result["drafts"]) == 2
    assert result["correction_approvals"] == [review.model_dump()]
    assert result["drafts"][-1]["review_sha256"] == digest(review)
    replay = accept_review(queue, result, review.model_dump())
    assert replay["drafts"] == result["drafts"] and replay["state"] == "approved"
    review.edits[0].after = "Unapproved different wording"
    with pytest.raises(TeaserError):
        accept_review(queue, result, review.model_dump())


def test_edit_and_approve_cannot_hide_an_unresolved_option(queued, story, draft):
    queue, task = drafted(queued, story, draft)
    review = correction(draft)
    review.approved = True
    result = accept_review(queue, task, review.model_dump())
    assert result["state"] == "brief_ready" and len(result["drafts"]) == 1


def test_repeated_phrase_corrections_do_not_force_a_rewrite(queued, story, draft):
    queue, task = drafted(queued, story, draft)
    review = approved(draft)
    review.edits = [SmallEdit(field="teaser", index=n, paragraph=p,
        before="Mara returns", after="Mara comes back", reason="Clarify the return.", paragraph_ids=[1])
        for n, p in [(1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (1, 2)]]
    result = accept_review(queue, task, review.model_dump())
    assert result["state"] == "approved" and len(result["reviews"]) == 1
    assert len(result["correction_approvals"][0]["edits"]) == 6


@pytest.mark.parametrize("change", [
    {"before": "not in draft"}, {"before": "the"}, {"paragraph_ids": [999]},
    {"paragraph_ids": []}, {"index": 0}, {"paragraph": 99},
    {"after": "word " * 41}, {"after": "x" * 321}, {"after": ""},
    {"after": "A new paragraph.\nAnother paragraph."},
])
def test_invalid_small_edit_falls_back_to_qwen_without_changing_copy(queued, story, draft, change):
    queue, task = drafted(queued, story, draft)
    review = correction(draft)
    review.edits[0] = SmallEdit.model_validate({**review.edits[0].model_dump(), **change})
    result = accept_review(queue, task, review.model_dump())
    assert result["state"] == "brief_ready"
    assert len(result["drafts"]) == 1
    assert result["drafts"][-1]["content"] == draft.model_dump()


def test_small_edit_batch_is_atomic_bounded_and_covers_guidance(draft):
    first = correction(draft).edits[0]
    second = SmallEdit(field="hook", index=1, paragraph=1,
                      before="ticket", after="boat ticket", reason="Clarify the hook.", paragraph_ids=[1])
    result = apply_small_edits(draft, [first, second], {1})
    assert result.opening_hooks[0] == draft.opening_hooks[0].replace("ticket", "boat ticket")
    second.before = "missing"
    with pytest.raises(ValueError):
        apply_small_edits(draft, [first, second], {1})
    assert "Mara returns" in draft.teasers[0].paragraphs[0]
    with pytest.raises(ValueError, match="at most 5"):
        apply_small_edits(draft, [first] * 6, {1})
    with pytest.raises(ValueError, match="80 words"):
        apply_small_edits(draft, [first.model_copy(update={"after": "word " * 30})] * 3, {1})


def test_small_edit_loop_returns_to_qwen_and_does_not_count_as_generation(queued, story, draft):
    queue, task = drafted(queued, story, draft)
    task["small_edit_rounds"] = 2
    task["drafts"] += [{**task["drafts"][0], "operation": "bounded_correction"}] * 4
    result = accept_review(queue, task, correction(draft).model_dump())
    assert result["state"] == "brief_ready" and "storysheet" in result
    assert len(result["drafts"]) == 5


def test_small_edit_cannot_skip_manuscript_coverage(queued, story, draft):
    queue, task = drafted(queued, story, draft)
    review = correction(draft)
    review.covered_chunk_ids = []
    result = accept_review(queue, task, review.model_dump())
    assert result["state"] == "brief_ready" and len(result["drafts"]) == 1


def test_valid_option_survives_unrelated_length_errors(queued, story, draft):
    draft.teasers[0].paragraphs[0] += " extra" * 70
    draft.opening_hooks[0] += " extra" * 20
    queue, task = drafted(queued, story, draft)
    review = approved(draft)
    review.approved = False
    review.options[0].clear = False
    review.guidance_approved = False
    task = accept_review(queue, task, review.model_dump())
    class Revision:
        def complete_structured(self, **kw):
            assert "APPROVED OPTIONS TO PRESERVE:\n[2, 3, 4, 5]" in kw["user"]
            replacement = draft.model_copy(deep=True)
            replacement.teasers[1].paragraphs[0] += " Changed."
            return ProviderResult(parsed=replacement.model_dump())
    task = next_brief(queue, task, story)
    result = generate_draft(queue, task, provider=Revision())
    assert result["drafts"][-1]["content"]["teasers"][1:] == draft.model_dump()["teasers"][1:]


def test_provider_failure_retries_and_does_not_publish(queued, story):
    queue, _, task = queued
    task = accept_story(queue, task, story.model_dump())
    class Failure:
        def complete_structured(self, **kw):
            return ProviderResult(stop_reason="error", error="provider unavailable")
    with pytest.raises(TeaserError):
        generate_draft(queue, task, provider=Failure())
    assert queue.get(task["id"])["state"] == "retry_wait"
    assert not queue.get(task["id"])["drafts"]


def test_truncated_package_retries_with_more_room_and_keeps_prior_draft(queued, story, draft):
    from app.teasers import INITIAL_WRITER_TOKENS, MAX_WRITER_TOKENS
    queue, task = drafted(queued, story, draft)
    review = approved(draft)
    review.approved = False
    review.feedback = ["Correct the ferry repair chronology."]
    task = accept_review(queue, task, review.model_dump())
    class Truncated:
        def complete_structured(self, **kw):
            assert kw["max_tokens"] == INITIAL_WRITER_TOKENS
            return ProviderResult(stop_reason="max_tokens", error="truncated")
    task = next_brief(queue, task, story)
    result = generate_draft(queue, task, provider=Truncated())
    assert result["state"] == "retry_wait"
    assert result["writer_token_limit"] == MAX_WRITER_TOKENS
    assert result["retry_at"] < time.time() + 31
    assert result["drafts"] == task["drafts"]
    assert result["generation_receipts"][-1]["stop_reason"] == "max_tokens"


def test_duplicate_worker_error_does_not_extend_saved_retry(queued):
    from types import SimpleNamespace
    from app.routes.teasers import dispatch, WorkerMessage
    queue, _, task = queued
    queue.retry(task, "Temporary provider failure", delay=30)
    before = queue.get(task["id"])
    app = SimpleNamespace(state=SimpleNamespace(watch=SimpleNamespace(home=queue.root.parent)))
    result = dispatch(app, WorkerMessage(action="error", worker="worker", task_id=task["id"],
                                        payload={"error": "Duplicate transport report"}))["task"]
    assert result["failures"] == before["failures"]
    assert result["retry_at"] == before["retry_at"]


def test_document_contains_all_options_guide_and_no_internal_evidence(tmp_path, story, draft):
    path = write_document(tmp_path / "teasers.docx", story, draft, approved(draft), book_label="Smith")
    doc = Document(path)
    text = "\n".join(p.text for p in doc.paragraphs)
    assert text.index("Option 2 — Recommended") < text.index("Option 1")
    assert all(p in text for t in draft.teasers for p in t.paragraphs)
    assert "Teaser elements & best practices" in text
    assert "paragraph_ids" not in text and "Qwen" not in text and "Sol" not in text
    assert story.protected_revelations[0] not in text
    assert doc.styles["Heading 1"].font.size.pt == 18


def test_delivery_requires_review_and_is_idempotent(queued, story, draft, monkeypatch):
    from app import teaser_delivery as delivery
    queue, task = drafted(queued, story, draft)
    with pytest.raises(TeaserError, match="Only an approved"):
        deliver(queue, task, queue.root.parent, token="google")
    task = accept_review(queue, task, approved(draft).model_dump())
    calls = []
    monkeypatch.setattr(delivery, "ensure_folder", lambda *a, **kw: "folder")
    monkeypatch.setattr(delivery.drive, "search_files", lambda *a, **kw: [])
    monkeypatch.setattr(delivery, "resume_import", lambda *a, **kw: calls.append("upload") or "doc")
    monkeypatch.setattr(delivery, "verify_document", lambda *a, **kw: "https://docs.google.com/document/d/doc/edit")
    task = deliver(queue, task, queue.root.parent, token="google")
    assert task["state"] == "complete"
    deliver(queue, task, queue.root.parent, token="google")
    assert calls == ["upload"]


def test_google_upload_recovers_lost_completion_without_second_document(queued, tmp_path):
    from app.teaser_delivery import resume_import
    queue, _, task = queued
    path = tmp_path / "upload.docx"
    path.write_bytes(b"document bytes")
    requests = []
    session = "https://www.googleapis.com/upload/drive/v3/files?upload_id=test"

    def opener(request):
        requests.append(request)
        if request.method == "POST":
            response = io.BytesIO(b"")
            response.headers = {"Location": session}
            return response
        # The session must be durable before sending any document bytes.
        assert queue.get(task["id"])["upload_session"] == session
        if request.data:
            raise TimeoutError("Google accepted the bytes but the reply was lost")
        return io.BytesIO(b'{"id":"one-native-doc"}')

    with pytest.raises(Exception, match="reply was lost"):
        resume_import(queue, task, "google", "folder", path, opener=opener)
    restored = queue.get(task["id"])
    assert resume_import(queue, restored, "google", "folder", path, opener=opener) == "one-native-doc"
    assert [request.method for request in requests] == ["POST", "PUT", "PUT"]
    assert requests[-1].get_header("Content-range") == "bytes */14"


def test_google_upload_resumes_remaining_bytes(queued, tmp_path):
    from email.message import Message
    from urllib.error import HTTPError
    from app.teaser_delivery import resume_import
    queue, _, task = queued
    path = tmp_path / "upload.docx"
    path.write_bytes(b"abcdefghij")
    task["upload_session"] = "https://www.googleapis.com/upload/drive/v3/files?upload_id=test"
    queue.save(task)
    requests = []

    def opener(request):
        requests.append(request)
        if not request.data:
            headers = Message()
            headers["Range"] = "bytes=0-3"
            raise HTTPError(request.full_url, 308, "Resume Incomplete", headers, io.BytesIO())
        assert request.data == b"efghij"
        assert request.get_header("Content-range") == "bytes 4-9/10"
        return io.BytesIO(b'{"id":"one-native-doc"}')

    assert resume_import(queue, task, "google", "folder", path, opener=opener) == "one-native-doc"
    assert len(requests) == 2


def test_worker_path_authenticates_and_settings_stay_private(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCPROOF_AGENT_TOKEN", "secret-long-enough-for-the-agent-gate")
    app = create_app(tmp_path, start_runner=False, web=True, session_secret="a-session-secret", https_only=False)
    with TestClient(app) as client:
        assert client.post("/api/teasers/worker", json={}).status_code == 401
        assert client.get("/api/teasers").status_code == 401
        assert client.put("/api/teasers/settings", json={"enabled": True}).status_code == 401
        response = client.post("/api/teasers/worker", headers={"Authorization": "Bearer secret-long-enough-for-the-agent-gate"},
                               json={"action": "poll", "worker": "fly-test"})
        assert response.status_code == 200, response.text
        assert response.json() == {"task": None}


def test_sol_is_subscription_high_and_validated_answers_resume(tmp_path, story):
    from docproof.teasers.models import Reading
    calls = []
    def runner(prompt, schema, work, **kw):
        calls.append(kw)
        assert kw["model"] == SOL_MODEL and kw["reasoning_effort"] == "high"
        assert kw["no_tools"] is True
        from galley.codex_runner import _check_schema
        _check_schema(schema)
        if "narrative" in schema["properties"]:
            return dict(chunk_id=1, first_paragraph=1, last_paragraph=1, narrative="Mara returns.",
                        facts=[dict(claim="Mara returns.", paragraph_ids=[1])], revelations=[], source_limitations=[])
        if "brief_sha256" in schema["properties"]:
            return dict(brief_sha256=digest(story.writer_brief), accurate=True, spoiler_safe=True, feedback=[])
        return story.model_dump()
    source = pipeline.chunks("Mara returns to repair the ferry.")
    assert pipeline.analyze(source, tmp_path, runner=runner) == story
    assert len(calls) == 2
    pipeline.analyze(source, tmp_path, runner=runner, attempt=1)
    assert len(calls) == 2


def test_public_brief_must_pass_sol_check_before_leaving_analysis(tmp_path, story):
    def runner(prompt, schema, work, **kw):
        if "narrative" in schema["properties"]:
            return dict(chunk_id=1, first_paragraph=1, last_paragraph=1, narrative="Mara returns.",
                facts=[dict(claim="Mara returns.", paragraph_ids=[1])], revelations=[], source_limitations=[])
        if "brief_sha256" in schema["properties"]:
            return dict(brief_sha256=digest(story.writer_brief), accurate=True, spoiler_safe=False,
                        feedback=["The public brief reveals a late decision."])
        return story.model_dump()
    with pytest.raises(ValueError, match="before Qwen can receive it"):
        pipeline.analyze(pipeline.chunks("Mara returns."), tmp_path, runner=runner)
    saved = [json.loads(p.read_text())["answer"] for p in (tmp_path / "answers").glob("*.json")]
    assert any("writer_brief" in answer for answer in saved)
    assert not (tmp_path / "prepared-copy.json").exists()
    assert list(tmp_path.glob("rejected-copy-*.json"))


def test_sol_copy_editor_applies_specific_changes_without_rewriting(tmp_path, story):
    calls = []
    original = story.writer_brief.author_copy.model_copy(deep=True)
    edit = correction(original).edits[0]
    def runner(prompt, schema, work, **kw):
        calls.append(schema)
        if "writer_brief" in schema["properties"]:
            return story.model_dump()
        return dict(brief_sha256=digest(story.writer_brief), accurate=True, spoiler_safe=True,
                    feedback=[], edits=[edit.model_dump()])
    source = pipeline.chunks("Mara comes home to repair the ferry.")
    result = pipeline.analyze(source, tmp_path, runner=runner)
    assert result.writer_brief.author_copy == apply_small_edits(original, [edit], {1})
    assert len(calls) == 2
    assert pipeline.analyze(source, tmp_path, runner=runner) == result
    assert len(calls) == 2  # The saved original and exact correction both resume.


def test_copy_editor_cannot_approve_invalid_edits(tmp_path, story):
    edit = correction(story.writer_brief.author_copy).edits[0]
    edit.before = "This phrase does not occur"
    def runner(prompt, schema, work, **kw):
        if "writer_brief" in schema["properties"]:
            return story.model_dump()
        return dict(brief_sha256=digest(story.writer_brief), accurate=True, spoiler_safe=True,
                    feedback=[], edits=[edit.model_dump()])
    with pytest.raises(ValueError, match="match exactly once"):
        pipeline.analyze(pipeline.chunks("Mara comes home."), tmp_path, runner=runner)
    assert (tmp_path / "prepared-copy.json").exists()


def test_single_portion_review_reads_original_and_sol_copy_in_one_call(tmp_path, story, draft):
    source = pipeline.chunks("ORIGINAL_OPENING\nORIGINAL_ENDING")
    calls = []
    def runner(prompt, schema, work, **kw):
        calls.append(prompt)
        assert "ORIGINAL_OPENING" in prompt and "ORIGINAL_ENDING" in prompt
        assert "author_copy" in prompt and "baseline" in prompt
        return approved(draft).model_dump()
    assert pipeline.review(story, draft, source, tmp_path, runner=runner).approved
    assert len(calls) == 1


def test_revision_translates_private_findings_and_rechecks_public_brief(tmp_path, story):
    revised = story.writer_brief.model_copy(deep=True)
    revised.writing_instructions = "Describe the service pause precisely and leave outcomes open."
    seen = []
    def runner(prompt, schema, work, **kw):
        seen.append(schema)
        if "public_setup" in schema["properties"]:
            assert "PRIVATE_FINDING" in prompt
            return revised.model_dump()
        return dict(brief_sha256=digest(revised), accurate=True, spoiler_safe=True, feedback=[])
    result = pipeline.revise_writer_brief(story, story.writer_brief, ["PRIVATE_FINDING"],
        pipeline.chunks("Mara returns to the ferry."), tmp_path, runner=runner)
    assert result == revised and len(seen) == 2
    assert "PRIVATE_FINDING" not in result.model_dump_json()


def test_completion_hook_queues_before_archiving(queued, tmp_path, monkeypatch):
    queue, job, task = queued
    store = JobStore(Paths(tmp_path / "app").ensure())
    store.save(job)
    runner = JobRunner(store, Settings(), config_path=Path("config/default.yaml"), notify_home=queue.root.parent)
    events = []
    monkeypatch.setattr("app.teasers.enqueue_completed", lambda home, j: events.append(("teaser", j.id)))
    monkeypatch.setattr(runner, "_archive_done", lambda jid: events.append(("archive", jid)))
    monkeypatch.setattr(runner, "_notify_done", lambda jid: None)
    runner._finish(job.id)
    assert events == [("teaser", job.id), ("archive", job.id)]


def test_fixed_reader_allows_only_the_known_disabled_tool_notice():
    from galley.codex_runner import _safe_events, _fixed_response_complete, _DISABLED_CODE_MODE_NOTICE
    def events(message):
        return _safe_events(io.BytesIO((json.dumps({"type": "item.completed", "item": {
            "type": "error", "message": message}}) + '\n' + json.dumps({"type": "turn.completed"}) + '\n').encode()))
    assert _fixed_response_complete(events(_DISABLED_CODE_MODE_NOTICE))
    assert not _fixed_response_complete(events("A real model error"))
