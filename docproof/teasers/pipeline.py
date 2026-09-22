"""Coverage-accounted Sol analysis, review and bounded corrections to Qwen drafts."""
from __future__ import annotations

from pathlib import Path
import json

from docproof.providers import strict_json_schema
from docproof.providers.base import inlined_json_schema
from . import SOL_MODEL, SOL_EFFORT
from .models import (Reading, Storysheet, SourceReview, Review, Draft, BriefReview, WriterBrief,
                     digest, draft_issues, apply_small_edits)
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


def analyze(source_chunks, work, *, runner=None, progress=lambda stage: None, feedback=None, attempt=0, public_briefs=False):
    candidate_path = Path(work) / ("prepared-briefs.json" if public_briefs else "prepared-copy.json")
    if candidate_path.exists():
        candidate = json.loads(candidate_path.read_text())
        if (candidate["source_sha256"] == digest(source_chunks) and
                (not public_briefs or candidate.get("feedback_sha256") == digest(feedback))):
            story = Storysheet.model_validate(candidate["story"])
            validate_story(story, source_chunks)
            return approve_prepared_copy(story, candidate["evidence"], source_chunks, work,
                runner=runner, progress=progress, attempt=attempt, candidate_path=candidate_path)
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
    progress("Sol is selecting facts for five distinct teaser angles" if public_briefs else "Sol is preparing author copy")
    if public_briefs:
        from .facts import story_prompt
    else:
        story_prompt = prompts.story_prompt
    prompt = story_prompt([r.model_dump() for r in readings], evidence, feedback)
    story = sol(prompt, Storysheet, work, "story-" + digest(prompt), runner=runner,
                attempt=attempt, validate=lambda value: validate_story(value, source_chunks))
    # Save finished writing before its copy edit; retries must not rewrite it all.
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = candidate_path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"source_sha256": digest(source_chunks),
                                    "story": story.model_dump(), "evidence": evidence,
                                    "feedback_sha256": digest(feedback)}))
    temporary.replace(candidate_path)
    return approve_prepared_copy(story, evidence, source_chunks, work, runner=runner,
        progress=progress, attempt=attempt, candidate_path=candidate_path)


def approve_prepared_copy(story, evidence, source_chunks, work, *, runner=None,
                          progress=lambda stage: None, attempt=0, candidate_path=None):
    if story.writer_brief.option_briefs:
        from .facts import approve_brief
        return approve_brief(story, evidence, source_chunks, work, runner=runner, attempt=attempt)
    progress("Sol is checking and correcting its finished copy before rephrasing")
    brief_hash = digest(story.writer_brief)
    prompt = prompts.brief_review_prompt(story.model_dump(), evidence, brief_hash)
    def validate(check):
        if check.brief_sha256 != brief_hash:
            raise ValueError("The copy edit does not match Sol's saved writing.")
        if check.edits:
            apply_small_edits(story.writer_brief.author_copy, check.edits,
                {p["id"] for c in source_chunks for p in c["paragraphs"]}, max_edits=10, max_words=160)
    check = sol(prompt, BriefReview, work, "brief-review-" + brief_hash,
                runner=runner, attempt=attempt, validate=validate)
    if not check.accurate or not check.spoiler_safe:
        if candidate_path is not None:
            candidate_path.replace(candidate_path.with_name("rejected-copy-" + brief_hash + ".json"))
        raise ValueError("The public writing brief needs revision before Qwen can receive it: " +
                         "; ".join(check.feedback))
    if check.edits:
        story.writer_brief.author_copy = apply_small_edits(story.writer_brief.author_copy, check.edits,
            {p["id"] for c in source_chunks for p in c["paragraphs"]}, max_edits=10, max_words=160)
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
    if brief.option_briefs:
        from .facts import validate_brief
        validate_brief(brief)
        return
    if not brief.author_copy.teasers:
        raise ValueError("Sol must write the complete author copy before Qwen can rephrase it.")
    issues = draft_issues(brief.author_copy)
    if issues:
        raise ValueError("Sol's finished copy needs correction: " + "; ".join(issues))


def revise_writer_brief(story, previous, feedback, source_chunks, work, *, runner=None,
                        progress=lambda stage: None, attempt=0):
    evidence = (source_chunks[0]["paragraphs"] if len(source_chunks) == 1
                else evidence_for(story.public_facts, source_chunks))
    if previous.option_briefs:
        from .facts import revise_prompt
        prompt = revise_prompt(story.model_dump(), previous.model_dump(), feedback, evidence)
    else:
        prompt = prompts.revise_brief_prompt(story.model_dump(), previous.model_dump(), feedback, evidence)
    progress("Sol is correcting the finished copy before rephrasing")
    def validate(brief):
        validate_writer_brief(brief)
        revised = story.model_copy(update={"writer_brief": brief})
        approve_prepared_copy(revised, evidence, source_chunks, work,
            runner=runner, progress=progress, attempt=attempt)
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
    if draft.version == 2:
        from .facts import review_prompt
    else:
        review_prompt = prompts.review_prompt
    prompt = review_prompt(story.model_dump(), draft.model_dump(), checks,
                                   draft_issues(draft), draft_hash,
                                   source_chunks=source_chunks if len(source_chunks) == 1 else None)
    def validate_final(result):
        if result.draft_sha256 != draft_hash or sorted(result.covered_chunk_ids) != [c["id"] for c in source_chunks]:
            raise ValueError("Sol's final review is missing draft or manuscript coverage.")
    return sol(prompt, Review, work, "review-" + digest(prompt), runner=runner,
               attempt=attempt, validate=validate_final)
