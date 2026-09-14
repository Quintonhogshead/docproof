"""Coverage-accounted Sol analysis, review and bounded corrections to Qwen drafts."""
from __future__ import annotations

from pathlib import Path
import json

from docproof.providers import strict_json_schema
from docproof.providers.base import inlined_json_schema
from . import SOL_MODEL, SOL_EFFORT
from .models import (Reading, Storysheet, SourceReview, Review, Draft, BriefReview, WriterBrief,
                     digest, draft_issues)
from . import prompts

CHUNK_CHARS = 90_000


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


def sol(prompt, model, work: Path, request_id, *, runner=None, attempt=0, validate=lambda value: None):
    # Completed, validated answers survive automatic retries. An unsuccessful
    # subscription turn gets a fresh bounded attempt after the server cooldown.
    schema = inlined_json_schema(strict_json_schema(model))
    key = digest({"prompt": prompt, "schema": schema, "model": SOL_MODEL})
    saved = Path(work) / "answers" / (key + ".json")
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
    progress("Sol is writing five complete teasers and the author guide")
    prompt = prompts.story_prompt([r.model_dump() for r in readings], evidence, feedback)
    def validate_prepared(story):
        validate_story(story, source_chunks)
        progress("Checking Sol's finished copy before rephrasing")
        brief_hash = digest(story.writer_brief)
        check_prompt = prompts.brief_review_prompt(story.model_dump(), evidence, brief_hash)
        check = sol(check_prompt, BriefReview, work, "brief-review-" + brief_hash,
                    runner=runner, attempt=attempt)
        if check.brief_sha256 != brief_hash or not check.accurate or not check.spoiler_safe:
            raise ValueError("The public writing brief needs revision before Qwen can receive it: " +
                             "; ".join(check.feedback))
    story = sol(prompt, Storysheet, work, "story-" + digest(prompt), runner=runner,
                attempt=attempt, validate=validate_prepared)
    return story


def validate_story(story, source_chunks):
    if not story.source_complete:
        raise ValueError("The storysheet reports a source limitation: " +
                         "; ".join(story.source_limitations or ["incomplete source"]))
    if (len(story.five_angles) != 5 or
            len({s.strip().casefold() for s in story.five_angles}) != 5):
        raise ValueError("The storysheet must specify five distinct teaser angles.")
    if not story.public_facts or any(not getattr(story, key).strip() for key in
            ("reader_promise", "narrative_center", "premise", "central_pressure",
             "stakes", "genre_and_audience", "voice", "qwen_instructions")):
        raise ValueError("The storysheet is missing an essential editorial field.")
    evidence_for(story.public_facts, source_chunks)
    if not story.writer_brief.public_setup:
        raise ValueError("Sol must prepare a separate public-only writing brief.")
    validate_writer_brief(story.writer_brief)


def validate_writer_brief(brief):
    if (len(brief.five_angles) != 5 or len(set(brief.five_angles)) != 5 or not brief.public_facts or
            any(not fact.strip() for fact in brief.public_facts) or
            any(not getattr(brief, name).strip() for name in ("public_setup", "reader_promise",
                "central_pressure", "stakes", "genre_and_audience", "voice", "writing_instructions"))):
        raise ValueError("The public-only writing brief is incomplete.")
    if not brief.author_copy.teasers:
        raise ValueError("Sol must write the complete author copy before Qwen can rephrase it.")
    issues = draft_issues(brief.author_copy)
    if issues:
        raise ValueError("Sol's finished copy needs correction: " + "; ".join(issues))


def revise_writer_brief(story, previous, feedback, source_chunks, work, *, runner=None,
                        progress=lambda stage: None, attempt=0):
    evidence = (source_chunks[0]["paragraphs"] if len(source_chunks) == 1
                else evidence_for(story.public_facts, source_chunks))
    prompt = prompts.revise_brief_prompt(story.model_dump(), previous.model_dump(), feedback, evidence)
    progress("Sol is correcting the finished copy before rephrasing")
    def validate(brief):
        validate_writer_brief(brief)
        progress("Checking the revised public brief for accuracy and spoilers")
        revised = story.model_copy(update={"writer_brief": brief})
        brief_hash = digest(brief)
        check_prompt = prompts.brief_review_prompt(revised.model_dump(), evidence, brief_hash)
        check = sol(check_prompt, BriefReview, work, "brief-review-" + brief_hash,
                    runner=runner, attempt=attempt)
        if check.brief_sha256 != brief_hash or not check.accurate or not check.spoiler_safe:
            raise ValueError("The revised public brief needs correction before Qwen receives it: " +
                             "; ".join(check.feedback))
    return sol(prompt, WriterBrief, work, "revise-brief-" + digest(prompt), runner=runner,
               attempt=attempt, validate=validate)


def review(story, draft, source_chunks, work, *, runner=None,
           progress=lambda stage: None, attempt=0):
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
    progress("Reviewing all five options and author guidance")
    prompt = prompts.review_prompt(story.model_dump(), draft.model_dump(), checks,
                                   draft_issues(draft), draft_hash,
                                   source_chunks=source_chunks if len(source_chunks) == 1 else None)
    def validate_final(result):
        if result.draft_sha256 != draft_hash or sorted(result.covered_chunk_ids) != [c["id"] for c in source_chunks]:
            raise ValueError("Sol's final review is missing draft or manuscript coverage.")
    return sol(prompt, Review, work, "review-" + digest(prompt), runner=runner,
               attempt=attempt, validate=validate_final)
