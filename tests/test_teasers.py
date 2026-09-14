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
                         MAX_DRAFTS_PER_CYCLE, MAX_DRAFTS_PER_DAY, TeaserError)
from app.teaser_delivery import deliver, ensure_folder, verify_document
from docproof.providers.base import ProviderResult
from docproof.teasers import QWEN_MODEL, SOL_MODEL
from docproof.teasers import pipeline
from docproof.teasers.document import write_document
from docproof.teasers.models import (Draft, Teaser, Element, Fact, Storysheet,
    Review, OptionCheck, digest, draft_issues, approval_issues)


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
        qwen_instructions="Ground all five options in Mara's return and the siblings' dilemma.")


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
    assert task["state"] == "story_ready"
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


def test_generation_daily_ceiling_resumes_without_editor(queued, story, draft):
    queue, _, task = queued
    task = accept_story(queue, task, story.model_dump())
    task["generation_times"] = [time.time()] * MAX_DRAFTS_PER_DAY
    task = generate_draft(queue, task, provider=object())
    assert task["state"] == "retry_wait"
    assert task["retry_at"] > time.time() + 86000


def test_provider_failure_retries_and_does_not_publish(queued, story):
    queue, _, task = queued
    task = accept_story(queue, task, story.model_dump())
    class Failure:
        def complete_structured(self, **kw):
            return ProviderResult(stop_reason="max_tokens", error="truncated")
    with pytest.raises(TeaserError):
        generate_draft(queue, task, provider=Failure())
    assert queue.get(task["id"])["state"] == "retry_wait"
    assert not queue.get(task["id"])["drafts"]


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
        return story.model_dump()
    source = pipeline.chunks("Mara returns to repair the ferry.")
    assert pipeline.analyze(source, tmp_path, runner=runner) == story
    assert len(calls) == 2
    pipeline.analyze(source, tmp_path, runner=runner, attempt=1)
    assert len(calls) == 2


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
