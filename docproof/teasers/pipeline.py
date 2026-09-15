"""Sol's side of the pipeline: coverage-accounted reading, a public-safe brief,
and a findings-only review of each draft. Nothing here produces published text."""
from __future__ import annotations

from pathlib import Path
import json

from docproof.providers import strict_json_schema
from docproof.providers.base import inlined_json_schema
from . import SOL_MODEL, SOL_EFFORT
from .models import (Reading, Storysheet, SourceReview, Review, BriefReview, digest)
from . import prompts

CHUNK_CHARS = 90_000
# How many times the briefing stage may take its own checker's findings and
# rebrief before giving up on this attempt.
BRIEF_ROUNDS = 2


def chunks(text: str) -> list[dict]:
    """Preserve the complete accepted manuscript, including very long paragraphs."""
    units = []
    for paragraph in text.splitlines():
        if not paragraph.strip():
            continue
        # A single huge paragraph is split into evidence units, never truncated.
        units.extend(paragraph[i:i + CHUNK_CHARS // 2]
                     for i in range(0, len(paragraph), CHUNK_CHARS // 2))
    result, batch, size = [], [], 0
    for number, text in enumerate(units, 1):
        if batch and size + len(text) > CHUNK_CHARS:
            result.append({"id": len(result) + 1, "paragraphs": batch})
            batch, size = [], 0
        batch.append({"id": number, "text": text})
        size += len(text)
    if batch:
        result.append({"id": len(result) + 1, "paragraphs": batch})
    if not result:
        raise ValueError("The formatting manuscript contains no readable text.")
    return result


def _answer_path(work, prompt, model) -> Path:
    schema = inlined_json_schema(strict_json_schema(model))
    key = digest({"prompt": prompt, "schema": schema, "model": SOL_MODEL})
    return Path(work) / "answers" / (key + ".json")


def forget(work, prompt, model) -> None:
    """Drop a validated answer so the next attempt asks Sol again."""
    path = _answer_path(work, prompt, model)
    if path.exists():
        path.unlink()


def sol(prompt, model, work: Path, request_id, *, runner=None, attempt=0, validate=lambda value: None):
    # Completed, validated answers survive automatic retries. An unsuccessful
    # subscription turn gets a fresh bounded attempt after the server cooldown.
    schema = inlined_json_schema(strict_json_schema(model))
    saved = _answer_path(work, prompt, model)
    key = saved.stem
    if saved.exists():
        raw = json.loads(saved.read_text())
        if digest(raw["answer"]) != raw["sha256"]:
            raise ValueError("A saved Sol answer changed after validation.")
        result = model.model_validate(raw["answer"])
        validate(result)
        return result
    if runner is None:
        from galley.codex_runner import run_structured
        runner = run_structured
    result = runner(prompt, schema, Path(work) / f"attempt-{attempt}",
                    request_id=request_id + "-" + key[:24],
                    timeout_seconds=1800, model=SOL_MODEL,
                    reasoning_effort=SOL_EFFORT, no_tools=True)
    result = model.model_validate(result)
    validate(result)
    saved.parent.mkdir(parents=True, exist_ok=True)
    temporary = saved.with_suffix(".tmp")
    temporary.write_text(json.dumps({"answer": result.model_dump(), "sha256": digest(result)}))
    temporary.replace(saved)
    return result


def evidence_for(facts, source_chunks):
    paragraphs = {p["id"]: p for c in source_chunks for p in c["paragraphs"]}
    ids = set()
    for fact in facts:
        if not fact.paragraph_ids or not set(fact.paragraph_ids) <= paragraphs.keys():
            raise ValueError("Sol cited missing manuscript evidence.")
        ids.update(fact.paragraph_ids)
    return [paragraphs[i] for i in sorted(ids)]


def analyze(source_chunks, work, *, runner=None, progress=lambda stage: None, feedback=None, attempt=0):
    """Read the book and produce a checked, public-safe writer brief.

    `feedback` is the private findings of an earlier review cycle; a brief
    prepared for different findings is never reused, so a rebrief actually
    rebriefs. Readings are cached per portion and survive everything."""
    feedback = list(feedback or [])
    prepared = Path(work) / "prepared-brief.json"
    if prepared.exists():
        saved = json.loads(prepared.read_text())
        if (saved.get("source_sha256") == digest(source_chunks)
                and saved.get("feedback_sha256") == digest(feedback)):
            story = Storysheet.model_validate(saved["story"])
            validate_story(story, source_chunks)
            return story
    readings = []
    # A manuscript that fits in one call needs no intermediate reading summary.
    for c in source_chunks if len(source_chunks) > 1 else []:
        progress(f"Reading manuscript portion {c['id']} of {len(source_chunks)}")
        def validate_reading(reading):
            if (reading.chunk_id != c["id"] or
                reading.first_paragraph != c["paragraphs"][0]["id"] or
                reading.last_paragraph != c["paragraphs"][-1]["id"]):
                raise ValueError("Sol did not account for the complete manuscript portion.")
            if not reading.narrative.strip() or not reading.facts:
                raise ValueError("Sol returned an empty manuscript reading.")
            evidence_for(reading.facts, [c])
        reading = sol(prompts.reading_prompt(c), Reading, work,
                      "reading-" + digest(c), runner=runner, attempt=attempt, validate=validate_reading)
        readings.append(reading)
    evidence = (evidence_for([f for r in readings for f in r.facts], source_chunks)
                if readings else source_chunks[0]["paragraphs"])
    readings = [r.model_dump() for r in readings]
    notes = feedback
    for _ in range(BRIEF_ROUNDS):
        progress("Sol is briefing the writer")
        prompt = prompts.story_prompt(readings, evidence, notes)
        story = sol(prompt, Storysheet, work, "story-" + digest(prompt), runner=runner,
                    attempt=attempt, validate=lambda value: validate_story(value, source_chunks))
        progress("Sol is checking the brief for accuracy and spoilers")
        brief_hash = digest(story.writer_brief)
        check_prompt = prompts.brief_review_prompt(story.model_dump(), evidence, brief_hash)
        def validate_check(check):
            if check.brief_sha256 != brief_hash:
                raise ValueError("The brief check does not match Sol's saved brief.")
        check = sol(check_prompt, BriefReview, work, "brief-review-" + brief_hash,
                    runner=runner, attempt=attempt, validate=validate_check)
        if check.accurate and check.spoiler_safe:
            prepared.parent.mkdir(parents=True, exist_ok=True)
            temporary = prepared.with_suffix(".tmp")
            temporary.write_text(json.dumps({"source_sha256": digest(source_chunks),
                                            "feedback_sha256": digest(feedback),
                                            "story": story.model_dump()}))
            temporary.replace(prepared)
            return story
        notes = notes + check.feedback
    # Two rejected briefs on this attempt. Forget the last pair so the next
    # attempt asks again instead of replaying the same rejected answer.
    forget(work, prompt, Storysheet)
    forget(work, check_prompt, BriefReview)
    raise ValueError("Sol could not produce a publication-safe brief: " + "; ".join(check.feedback))


def validate_story(story, source_chunks):
    if not story.source_complete:
        raise ValueError("The storysheet reports a source limitation: " +
                         "; ".join(story.source_limitations or ["incomplete source"]))
    if (len(story.five_angles) != 5 or
            len({s.strip().casefold() for s in story.five_angles}) != 5):
        raise ValueError("The storysheet must specify five distinct teaser angles.")
    if not story.public_facts or any(not getattr(story, key).strip() for key in
            ("reader_promise", "narrative_center", "premise", "central_pressure",
             "stakes", "genre_and_audience", "voice")):
        raise ValueError("The storysheet is missing an essential editorial field.")
    evidence_for(story.public_facts, source_chunks)
    validate_writer_brief(story.writer_brief)


def validate_writer_brief(brief):
    if (len(brief.five_angles) != 5 or len(set(brief.five_angles)) != 5 or not brief.public_facts or
            any(not fact.strip() for fact in brief.public_facts) or
            any(not getattr(brief, name).strip() for name in ("public_setup", "reader_promise",
                "central_pressure", "stakes", "genre_and_audience", "voice", "writing_instructions"))):
        raise ValueError("The public-only writing brief is incomplete.")


def review(story, draft, source_chunks, work, *, runner=None,
           progress=lambda stage: None, attempt=0, issues=()):
    draft_hash = digest(draft)
    checks = []
    # Review a small source directly, rather than asking two editors in succession.
    for c in source_chunks if len(source_chunks) > 1 else []:
        progress(f"Checking teasers against manuscript portion {c['id']} of {len(source_chunks)}")
        prompt = prompts.source_review_prompt(c, draft.model_dump(), draft_hash)
        def validate_check(check):
            if check.chunk_id != c["id"] or check.draft_sha256 != draft_hash:
                raise ValueError("The source review does not match this manuscript and draft.")
        check = sol(prompt, SourceReview, work, "source-review-" + digest(prompt), runner=runner,
                    attempt=attempt, validate=validate_check)
        checks.append(check.model_dump())
    progress("Sol is reviewing all five options and the author guidance")
    prompt = prompts.review_prompt(story.model_dump(), draft.model_dump(), checks,
                                   list(issues), draft_hash,
                                   source_chunks=source_chunks if len(source_chunks) == 1 else None)
    def validate_final(result):
        if result.draft_sha256 != draft_hash or sorted(result.covered_chunk_ids) != [c["id"] for c in source_chunks]:
            raise ValueError("Sol's final review is missing draft or manuscript coverage.")
    return sol(prompt, Review, work, "review-" + digest(prompt), runner=runner,
               attempt=attempt, validate=validate_final)
