"""The teaser speed rework: parallel medium-effort readings, reviews against the
saved reading, in-attempt fact reselection, immediate length rewrites, and
subscription outages that never count against a book."""
import json
import threading
import time

import pytest

from test_teasers import queued, story, draft, approved
from test_teasers_v2 import v2
from app import teasers
from app.teasers import generate_draft, Queue
from docproof.providers.base import ProviderResult
from docproof.teasers import pipeline
from docproof.teasers.models import Draft, OptionBrief, Teaser, digest


def long_source(portions=3):
    # Each paragraph is over half a chunk, so every one becomes its own portion.
    return pipeline.chunks("\n".join(f"PORTION_{n} " + "word " * (pipeline.CHUNK_CHARS // 8)
                                     for n in range(1, portions + 1)))


def reading_for(chunk):
    first = chunk["paragraphs"][0]["id"]
    return dict(chunk_id=chunk["id"], first_paragraph=first, last_paragraph=chunk["paragraphs"][-1]["id"],
                narrative=f"Reading {chunk['id']}", facts=[dict(claim="A fact.", paragraph_ids=[first])],
                revelations=[], source_limitations=[])


def fact_story(story):
    story.writer_brief.author_copy = Draft(teasers=[], opening_hooks=[], editorial_note='',
        elements=[], best_practices=[], modification_checklist=[])
    story.writer_brief.option_briefs = [OptionBrief(number=n, angle=story.writer_brief.five_angles[n - 1],
        facts=['Mara returns to repair her father’s ferry.'], direction='Stay with the siblings.')
        for n in range(1, 6)]
    return story


def test_portions_are_read_concurrently_at_medium_effort_and_reused(tmp_path):
    source = long_source(3)
    barrier = threading.Barrier(3)
    calls = []
    def runner(prompt, schema, work, **kw):
        calls.append(kw["reasoning_effort"])
        barrier.wait(timeout=5)          # all three must be in flight together
        chunk = next(c for c in source if f"PORTION_{c['id']} " in prompt)
        return reading_for(chunk)
    readings = pipeline.read_portions(source, tmp_path, runner=runner)
    assert [r.chunk_id for r in readings] == [1, 2, 3]
    assert calls == ["medium"] * 3
    assert pipeline.read_portions(source, tmp_path, runner=runner) == readings
    assert len(calls) == 3


def test_review_uses_saved_reading_not_a_second_pass_over_the_book(tmp_path, story, draft):
    source = long_source(3)
    seen = []
    def runner(prompt, schema, work, **kw):
        seen.append(schema)
        if "narrative" in schema["properties"]:
            chunk = next(c for c in source if f"PORTION_{c['id']} " in prompt)
            return reading_for(chunk)
        assert "full_book_readings" in prompt and "cited_passages" in prompt
        assert kw["reasoning_effort"] == "high"
        return approved(result, chunk_ids=(1, 2, 3)).model_dump()
    result = Draft(version=2, teasers=draft.teasers, opening_hooks=[], editorial_note="",
                   elements=[], best_practices=[], modification_checklist=[])
    pipeline.read_portions(source, tmp_path, runner=runner)       # the analysis already read it
    seen.clear()
    assert pipeline.review(story, result, source, tmp_path, runner=runner).approved
    assert len(seen) == 1                                          # one call, no re-reading


def test_rejected_fact_selection_is_reselected_in_the_same_attempt(tmp_path, story):
    story = fact_story(story)
    audits = []
    def runner(prompt, schema, work, **kw):
        if "brief_sha256" in schema["properties"]:
            audits.append(prompt)
            ok = len(audits) > 1
            return dict(brief_sha256=digest(story.writer_brief), accurate=True, spoiler_safe=ok,
                        feedback=[] if ok else ["Option 4 discloses a protected revelation."])
        if len(audits) == 1:
            assert "Option 4 discloses a protected revelation." in prompt
            story.voice = "Revised"            # a genuinely new selection
        return story.model_dump()
    result = pipeline.analyze(pipeline.chunks("Mara returns."), tmp_path, runner=runner, public_briefs=True)
    assert result.voice == "Revised" and len(audits) == 2


def test_extraction_damage_is_recorded_not_a_refusal(tmp_path, story):
    story = fact_story(story)
    story.source_complete = False
    story.source_limitations = ["Tables appear as linear fragments."]
    pipeline.validate_story(story, pipeline.chunks("Mara returns."))
    legacy = story.model_copy(deep=True)
    legacy.writer_brief.option_briefs = []
    with pytest.raises(ValueError, match="source limitation"):
        pipeline.validate_story(legacy, pipeline.chunks("Mara returns."))


def test_length_miss_is_rewritten_immediately_without_the_queue(v2):
    queue, job, task, outputs = v2
    calls = []
    class Provider:
        def complete_structured(self, **kw):
            number = json.loads(kw['user'])['number']
            calls.append(number)
            if number == 2 and calls.count(2) == 1:
                short = outputs[2].model_copy(deep=True)
                short.paragraphs = [" ".join(p.split()[:40]) for p in short.paragraphs]
                return ProviderResult(parsed=short.model_dump())
            if number == 2:
                assert "was rejected" in kw['system'] and "words" in kw['system']
                assert "Mara returns to the island" not in kw['system']   # never its own rejected copy
            return ProviderResult(parsed=outputs[number].model_dump())
    task = generate_draft(queue, task, provider=Provider())
    assert task['state'] == 'drafted'
    assert calls.count(2) == 2 and len(calls) == 6
    assert not task.get('failures')


def test_subscription_outage_does_not_count_or_become_feedback(queued):
    queue, job, task = queued
    error = ("Codex subscription review stopped (model_unavailable). "
             "No automatic retry or paid API fallback was submitted.")
    assert teasers.is_transient(error)
    before = time.time()
    queue.retry(task, error, counted=False)
    saved = queue.get(task['id'])
    assert saved['state'] == 'retry_wait' and not saved.get('failures')
    assert error not in saved.get('feedback', [])
    assert saved['retry_at'] - before <= 125
    assert not teasers.is_transient("Sol must correct the selected facts: Option 2 overstates.")


def test_counted_backoff_is_capped_at_half_an_hour(queued):
    queue, job, task = queued
    task['failures'] = 20
    before = time.time()
    queue.retry(task, "Sol must correct the selected facts.")
    assert queue.get(task['id'])['retry_at'] - before <= teasers.MAX_BACKOFF_SECONDS + 5


def test_worker_error_route_classifies_outages(queued, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.routes.teasers import dispatch, WorkerMessage
    queue, job, task = queued
    app = SimpleNamespace(state=SimpleNamespace(watch=SimpleNamespace(home=queue.root.parent)))
    message = WorkerMessage(action="error", worker="worker", task_id=task["id"],
        payload={"error": "Codex subscription review stopped (cli_failure). Retry later."})
    dispatch(app, message)
    saved = queue.get(task["id"])
    assert saved["state"] == "retry_wait" and not saved.get("failures")
    assert saved["transient_failures"] == 1
