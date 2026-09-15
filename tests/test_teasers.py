from __future__ import annotations

from dataclasses import replace
import io
import json
from pathlib import Path
import time

from docx import Document
from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from app.jobs import Job, JobRunner, JobStore
from app.main import create_app
from app.settings import Paths, Settings
from app.teasers import (Queue, accept_story, generate_draft, accept_review, revision_context,
                         MAX_DRAFTS_PER_CYCLE, MAX_DRAFTS_PER_DAY, MAX_BACKOFF_SECONDS, YIELD_SECONDS,
                         GATE_ROUNDS, INITIAL_WRITER_TOKENS, MAX_WRITER_TOKENS, TeaserError)
from app.teaser_delivery import deliver, ensure_folder, verify_document
from docproof.providers.base import ProviderResult
from docproof.teasers import WRITER_MODEL, WRITER_PROVIDER, SOL_MODEL
from docproof.teasers import pipeline
from docproof.teasers.document import write_document
from docproof.teasers.models import (Draft, Teaser, Element, Fact, Storysheet, Review, OptionCheck,
                                     WriterBrief, digest, draft_issues, approval_issues)


@pytest.fixture
def story():
    return Storysheet(title="The Ferry Ledger", author="", source_complete=True,
        source_limitations=[], reader_promise="A quiet family reconciliation on a working island.",
        narrative_center="Mara and her brother", premise="Mara returns to repair the island ferry.",
        central_pressure="They disagree about selling their father's boat.", stakes="Their bond and the crossing.",
        genre_and_audience="Adult family fiction", voice="Intimate and restrained",
        public_facts=[Fact(claim="Mara returns to repair the ferry.", paragraph_ids=[1])],
        conditional_disclosures=[], protected_revelations=["The final decision about the boat."],
        five_angles=["Return", "Siblings", "Island", "Inheritance", "Repair"],
        writer_brief=WriterBrief(title="The Ferry Ledger", author="",
            public_setup="Mara returns to repair the ferry; her brother wants to sell it.",
            reader_promise="A restrained family story.", central_pressure="A disputed inheritance.",
            stakes="Their relationship and the ferry's future.", genre_and_audience="Adult family fiction",
            voice="Restrained and concrete", public_facts=["Mara returns; her brother wants to sell the ferry."],
            five_angles=["Return", "Siblings", "Island", "Inheritance", "Repair"],
            writing_instructions="Leave the final decision unresolved."))


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


@pytest.fixture(autouse=True)
def feature_available(monkeypatch):
    """The engine tests run with the kill switch lifted; the switch itself is
    covered by test_switched_off_*."""
    monkeypatch.setattr("app.teasers.AVAILABLE", True)


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


class Writer:
    """A provider that returns each response in turn and records every call."""
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def complete_structured(self, **kw):
        assert kw["model"] == WRITER_MODEL
        assert "EDITORIAL BRIEF" in kw["user"] and "YOU ARE THE WRITER" in kw["system"]
        self.calls.append(kw)
        response = self.responses.pop(0)
        return response if isinstance(response, ProviderResult) else ProviderResult(parsed=response.model_dump())


def drafted(queued, story, draft):
    queue, job, task = queued
    task = accept_story(queue, task, story.model_dump())
    return queue, generate_draft(queue, task, provider=Writer(draft))


def rejected(draft, notes=("Option 1: keep the brother's decision open.",)):
    review = approved(draft)
    review.approved = False
    review.options[0].accurate = False
    review.options[0].feedback = "Option 1 states the boat is sold, which the ending reverses."
    review.options[0].writer_notes = notes[0].split(": ", 1)[1]
    review.writer_notes = list(notes[1:])
    return review


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


def test_review_schema_has_no_room_for_replacement_text(draft):
    review = approved(draft).model_dump()
    review["edits"] = [{"field": "teaser", "index": 1, "before": "Mara", "after": "Mara Ellis"}]
    with pytest.raises(ValidationError):
        Review.model_validate(review)


def test_writer_is_the_catalog_deepinfra_model_with_default_reasoning():
    from docproof.providers.catalog import lookup
    from docproof.providers.deepinfra_provider import DeepInfraProvider
    info = lookup(WRITER_MODEL)
    assert info is not None and info.provider == WRITER_PROVIDER == "deepinfra"
    body = DeepInfraProvider(api_key="test-key", effort=None)._body(
        model=WRITER_MODEL, system="Write.", user="Brief.",
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        schema_name="teaser", max_tokens=100)
    assert "extra_body" not in body and "reasoning_effort" not in body


def test_writer_never_receives_private_ending_manuscript_or_private_feedback(queued, story, draft):
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
    first = Writer(draft)
    task = generate_draft(queue, task, provider=first)
    call = first.calls[0]
    assert secret not in call["user"] and secret not in call["system"]
    assert json.dumps(story.writer_brief.model_dump(), ensure_ascii=False) in call["user"]
    review = rejected(draft)
    review.feedback = ["Option 1 reveals " + secret]
    review.options[0].feedback = "It names " + secret
    task = accept_review(queue, task, review.model_dump())
    assert task["state"] == "story_ready"
    assert any(secret in line for line in task["feedback"])
    second = Writer(draft)
    task = generate_draft(queue, task, provider=second)
    call = second.calls[0]
    assert secret not in call["user"] and secret not in call["system"]
    assert "Option 1: keep the brother's decision open." in call["user"]
    assert "APPROVED OPTIONS TO RETURN UNCHANGED:\n[2, 3, 4, 5]" in call["user"]
    assert secret not in json.dumps(task["writer_handoffs"])


def test_legacy_storysheet_without_brief_returns_to_sol(queued, story):
    queue, _, task = queued
    task["storysheet"] = story.model_dump(exclude={"writer_brief"})
    task["state"] = "story_ready"
    queue.save(task)
    result = generate_draft(queue, task, provider=object())
    assert result["state"] == "queued"
    assert "storysheet" not in result and "generation_times" not in result
    assert result["prior_storysheets"]


def test_every_saved_draft_is_the_writers(queued, story, draft):
    queue, task = drafted(queued, story, draft)
    entry = task["drafts"][-1]
    assert entry["model"] == WRITER_MODEL and entry["provider"] == WRITER_PROVIDER
    assert entry["operation"] == "generation" and entry["gate_rounds"] == 1
    assert entry["brief_sha256"] == digest(story.writer_brief.model_dump())


def test_mechanical_gate_retries_the_writer_before_sol_sees_a_draft(queued, story, draft):
    long = draft.model_copy(deep=True)
    long.teasers[0].paragraphs[0] += " extra" * 70
    queue, _, task = queued
    task = accept_story(queue, task, story.model_dump())
    writer = Writer(long, draft)
    result = generate_draft(queue, task, provider=writer)
    assert result["state"] == "drafted" and len(writer.calls) == 2
    assert "REQUIRED FIXES" in writer.calls[1]["user"]
    assert "Option 1 has" in writer.calls[1]["user"]
    assert json.dumps(long.model_dump(), ensure_ascii=False) in writer.calls[1]["user"]
    assert result["drafts"][-1]["content"] == draft.model_dump()
    assert result["drafts"][-1]["gate_rounds"] == 2
    assert len(result["generation_receipts"]) == 2


def test_persistent_mechanical_failure_retries_without_saving_a_draft(queued, story, draft):
    long = draft.model_copy(deep=True)
    long.opening_hooks = ["short"] * 3
    queue, _, task = queued
    task = accept_story(queue, task, story.model_dump())
    writer = Writer(*[long] * GATE_ROUNDS)
    with pytest.raises(TeaserError, match="mechanical checks"):
        generate_draft(queue, task, provider=writer)
    result = queue.get(task["id"])
    assert result["state"] == "retry_wait" and not result["drafts"]
    assert len(writer.calls) == GATE_ROUNDS
    assert result.get("feedback") in (None, [])


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


def test_writer_result_reused_and_wrong_review_blocked(queued, story, draft):
    queue, task = drafted(queued, story, draft)
    assert generate_draft(queue, task, provider=object())["drafts"] == task["drafts"]
    review = approved(draft)
    review.draft_sha256 = "wrong"
    with pytest.raises(TeaserError):
        accept_review(queue, task, review.model_dump())
    assert queue.get(task["id"])["state"] == "drafted"
    task = accept_review(queue, task, approved(draft).model_dump())
    assert task["state"] == "approved" and task["feedback"] == []


def test_failed_review_sends_notes_to_the_writer_then_rebriefs(queued, story, draft):
    queue, task = drafted(queued, story, draft)
    review = rejected(draft, ("Option 1: keep the brother's decision open.", "Shorten the editorial note."))
    task = accept_review(queue, task, review.model_dump())
    assert task["state"] == "story_ready"
    assert any(line.startswith("One or more options") for line in task["feedback"])
    assert "Option 1: Option 1 states the boat is sold" in task["feedback"][-1]
    previous, retained, notes = revision_context(task)
    assert previous == draft.model_dump() and sorted(retained) == [2, 3, 4, 5]
    assert notes == ["Shorten the editorial note.", "Option 1: keep the brother's decision open."]
    task["drafts"] *= MAX_DRAFTS_PER_CYCLE
    task["state"] = "drafted"
    queue.save(task)
    task = accept_review(queue, task, review.model_dump())
    assert task["state"] == "queued" and "storysheet" not in task
    assert task["prior_storysheets"] == [story.model_dump()]
    assert any("boat is sold" in line for line in task["feedback"])


def test_revision_preserves_passing_options_but_requires_fresh_review(queued, story, draft):
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
    writer = Writer(changed)
    result = generate_draft(queue, task, provider=writer)
    assert "APPROVED OPTIONS TO RETURN UNCHANGED:\n[2]" in writer.calls[0]["user"]
    assert "Option 1: revise this option" in writer.calls[0]["user"]
    content = Draft.model_validate(result["drafts"][-1]["content"])
    assert content.teasers[1] == draft.teasers[1]
    assert content.teasers[0] == changed.teasers[0]
    assert result["drafts"][-1]["retained_from"] == {"draft_sha256": prior_hash, "options": [2]}
    assert result["drafts"][-1]["sha256"] != prior_hash
    assert result["state"] == "drafted"
    with pytest.raises(TeaserError, match="different draft"):
        accept_review(queue, result, approved(draft).model_dump())


def test_rebriefed_story_starts_the_writer_clean(queued, story, draft):
    queue, task = drafted(queued, story, draft)
    task = accept_review(queue, task, rejected(draft).model_dump())
    task["storysheet"]["protected_revelations"].append("A newly protected development.")
    queue.save(task)
    writer = Writer(draft)
    result = generate_draft(queue, task, provider=writer)
    assert 'PREVIOUS DRAFT (revise it; absent on a first draft):\nnull' in writer.calls[0]["user"]
    assert "APPROVED OPTIONS TO RETURN UNCHANGED:\n[]" in writer.calls[0]["user"]
    assert result["drafts"][-1]["retained_from"] is None


def test_generation_daily_ceiling_resumes_without_counting_a_failure(queued, story, draft):
    queue, _, task = queued
    task = accept_story(queue, task, story.model_dump())
    task["generation_times"] = [time.time()] * MAX_DRAFTS_PER_DAY
    task = generate_draft(queue, task, provider=object())
    assert task["state"] == "retry_wait"
    assert task["retry_at"] > time.time() + 86000
    assert task.get("failures", 0) == 0


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


def test_truncated_package_doubles_the_allowance_in_the_same_call(queued, story, draft):
    queue, _, task = queued
    task = accept_story(queue, task, story.model_dump())
    writer = Writer(ProviderResult(stop_reason="max_tokens", error="truncated"), draft)
    result = generate_draft(queue, task, provider=writer)
    assert [c["max_tokens"] for c in writer.calls] == [INITIAL_WRITER_TOKENS, MAX_WRITER_TOKENS]
    assert result["state"] == "drafted" and result["writer_token_limit"] == MAX_WRITER_TOKENS
    assert result["generation_receipts"][0]["stop_reason"] == "max_tokens"


def test_retry_caps_backoff_and_never_feeds_errors_to_sol(queued):
    queue, _, task = queued
    task["feedback"] = ["Option 2: the ferry belongs to both siblings."]
    task["failures"] = 20
    queue.retry(task, "HTTP 503 from the writer")
    saved = queue.get(task["id"])
    assert saved["failures"] == 21
    assert saved["retry_at"] <= time.time() + MAX_BACKOFF_SECONDS
    assert saved["feedback"] == ["Option 2: the ferry belongs to both siblings."]
    assert saved["error"] == "HTTP 503 from the writer"
    queue.retry(saved, "The subscription reviewer is busy; no new request was submitted.", counted=False)
    yielded = queue.get(task["id"])
    assert yielded["failures"] == 21
    assert yielded["retry_at"] <= time.time() + YIELD_SECONDS


def test_worker_error_route_distinguishes_transient_from_failure(queued):
    from types import SimpleNamespace
    from app.routes.teasers import dispatch, WorkerMessage
    queue, _, task = queued
    app = SimpleNamespace(state=SimpleNamespace(watch=SimpleNamespace(home=queue.root.parent)))
    result = dispatch(app, WorkerMessage(action="error", worker="worker", task_id=task["id"],
                                        payload={"error": "busy", "transient": True}))["task"]
    assert result["state"] == "retry_wait" and result.get("failures", 0) == 0
    before = result
    result = dispatch(app, WorkerMessage(action="error", worker="worker", task_id=task["id"],
                                        payload={"error": "Duplicate transport report"}))["task"]
    assert result.get("failures", 0) == before.get("failures", 0)
    assert result["retry_at"] == before["retry_at"]


def test_busy_lock_is_transient_for_the_worker():
    from galley.astra_review import AstraReviewError
    from docproof.teasers.worker import transient
    assert transient(AstraReviewError("The subscription reviewer is busy; no new request was submitted."))
    assert not transient(AstraReviewError("Unsupported subscription model or reasoning effort."))
    assert not transient(ValueError("busy"))


def test_document_contains_all_options_guide_and_no_internal_evidence(tmp_path, story, draft):
    path = write_document(tmp_path / "teasers.docx", story, draft, approved(draft), book_label="Smith")
    doc = Document(path)
    text = "\n".join(p.text for p in doc.paragraphs)
    assert text.index("Option 2 — Recommended") < text.index("Option 1")
    assert all(p in text for t in draft.teasers for p in t.paragraphs)
    assert "Teaser elements & best practices" in text
    assert "paragraph_ids" not in text and "DeepSeek" not in text and "Sol" not in text
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
        rejected_action = client.post("/api/teasers/worker", headers={"Authorization": "Bearer secret-long-enough-for-the-agent-gate"},
                                      json={"action": "brief", "worker": "fly-test"})
        assert rejected_action.status_code == 409


def sol_runner(story, *, check=lambda brief_hash: dict(accurate=True, spoiler_safe=True, feedback=[]),
               vary=False):
    """`vary` makes each briefing call return a slightly different brief, as a
    real rebrief would; without it an identical brief reuses its cached check."""
    calls = []
    def runner(prompt, schema, work, **kw):
        calls.append((prompt, schema, kw))
        if vary and "writer_brief" in schema["properties"]:
            rounds = sum("writer_brief" in c[1]["properties"] for c in calls)
            varied = story.model_copy(deep=True)
            varied.writer_brief.writing_instructions += f" (brief {rounds})"
            return varied.model_dump()
        assert kw["model"] == SOL_MODEL and kw["reasoning_effort"] == "high"
        assert kw["no_tools"] is True
        from galley.codex_runner import _check_schema
        _check_schema(schema)
        if "narrative" in schema["properties"]:
            return dict(chunk_id=1, first_paragraph=1, last_paragraph=1, narrative="Mara returns.",
                        facts=[dict(claim="Mara returns.", paragraph_ids=[1])], revelations=[], source_limitations=[])
        if "brief_sha256" in schema["properties"]:
            brief_hash = json.loads(prompt[prompt.index('{"private_storysheet"'):])["brief_sha256"]
            return dict(brief_sha256=brief_hash, **check(brief_hash))
        return story.model_dump()
    return runner, calls


def test_sol_is_subscription_high_and_validated_answers_resume(tmp_path, story):
    runner, calls = sol_runner(story)
    source = pipeline.chunks("Mara returns to repair the ferry.")
    assert pipeline.analyze(source, tmp_path, runner=runner) == story
    assert len(calls) == 2
    assert "that is the writer's job" in calls[0][0]
    pipeline.analyze(source, tmp_path, runner=runner, attempt=1)
    assert len(calls) == 2
    assert (tmp_path / "prepared-brief.json").exists()


def test_rebrief_uses_the_review_findings(tmp_path, story):
    runner, calls = sol_runner(story)
    source = pipeline.chunks("Mara returns to repair the ferry.")
    pipeline.analyze(source, tmp_path, runner=runner)
    assert len(calls) == 2
    finding = "Option 3 invents a storm the book never has."
    pipeline.analyze(source, tmp_path, runner=runner, feedback=[finding], attempt=1)
    # The story is rewritten with the finding; the mock returns the same brief,
    # whose check is already cached by its hash.
    assert len(calls) == 3
    assert finding in calls[2][0]
    pipeline.analyze(source, tmp_path, runner=runner, feedback=[finding], attempt=2)
    assert len(calls) == 3


def test_rejected_brief_is_rebriefed_from_the_checkers_findings(tmp_path, story):
    verdicts = iter([dict(accurate=True, spoiler_safe=False, feedback=["The brief names the final decision."]),
                     dict(accurate=True, spoiler_safe=True, feedback=[])])
    runner, calls = sol_runner(story, check=lambda h: next(verdicts), vary=True)
    source = pipeline.chunks("Mara returns to repair the ferry.")
    result = pipeline.analyze(source, tmp_path, runner=runner)
    assert result.writer_brief.writing_instructions.endswith("(brief 2)")
    assert len(calls) == 4
    assert "The brief names the final decision." in calls[2][0]
    assert (tmp_path / "prepared-brief.json").exists()


def test_twice_rejected_brief_is_forgotten_for_the_next_attempt(tmp_path, story):
    runner, calls = sol_runner(story, check=lambda h: dict(
        accurate=False, spoiler_safe=True, feedback=["The brief invents a second boat."]), vary=True)
    source = pipeline.chunks("Mara returns to repair the ferry.")
    with pytest.raises(ValueError, match="publication-safe brief"):
        pipeline.analyze(source, tmp_path, runner=runner)
    assert len(calls) == 4
    assert not (tmp_path / "prepared-brief.json").exists()
    assert len(list((tmp_path / "answers").glob("*.json"))) == 2
    with pytest.raises(ValueError):
        pipeline.analyze(source, tmp_path, runner=runner, attempt=1)
    assert len(calls) == 6


def test_single_portion_review_reads_original_and_brief_in_one_call(tmp_path, story, draft):
    source = pipeline.chunks("ORIGINAL_OPENING\nORIGINAL_ENDING")
    calls = []
    def runner(prompt, schema, work, **kw):
        calls.append(prompt)
        assert "ORIGINAL_OPENING" in prompt and "ORIGINAL_ENDING" in prompt
        assert "writer_brief" in prompt and "YOU DO NOT WRITE OR CORRECT COPY" in prompt
        return approved(draft).model_dump()
    assert pipeline.review(story, draft, source, tmp_path, runner=runner).approved
    assert len(calls) == 1


def test_switched_off_hook_enqueues_nothing_and_settings_read_off(tmp_path, monkeypatch):
    monkeypatch.setattr("app.teasers.AVAILABLE", False)
    queue = Queue(tmp_path / "watch")
    (queue.root / "settings.json").write_text(json.dumps({"enabled": True, "folder_id": "f1"}))
    assert queue.settings()["enabled"] is False and queue.settings()["available"] is False
    job = Job(id="format-2", filename="Smith - Book Original.docx", source_path="x.docx",
              model="test", mode="now", kind="prep", state="done", owner_id="owner")
    assert queue.add(job) is None and queue.list() == []
    assert queue.claim("worker") is None
    with pytest.raises(TeaserError):
        queue.configure(enabled=True)
    queue.configure(enabled=False)                     # turning it off still works


def test_switched_off_routes_refuse_enable_and_hand_the_worker_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("app.teasers.AVAILABLE", False)
    monkeypatch.setenv("DOCPROOF_AGENT_TOKEN", "secret-long-enough-for-the-agent-gate")
    app = create_app(tmp_path, start_runner=False, web=False)
    with TestClient(app) as client:
        assert client.put("/api/teasers/settings", json={"enabled": True}).status_code == 409
        assert client.get("/api/teasers").json()["settings"]["enabled"] is False
        response = client.post("/api/teasers/worker", headers={"Authorization": "Bearer secret-long-enough-for-the-agent-gate"},
                               json={"action": "poll", "worker": "fly-test"})
        assert response.json() == {"task": None}


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
