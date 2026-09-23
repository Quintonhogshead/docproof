"""Coverage-accounted Sol analysis, review and bounded corrections to Qwen drafts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json

from docproof.providers import strict_json_schema
from docproof.providers.base import inlined_json_schema
from . import SOL_MODEL, SOL_EFFORT
from .models import (Reading, Storysheet, SourceReview, Review, Draft, BriefReview, WriterBrief,
                     digest, draft_issues, apply_small_edits)
from . import prompts

CHUNK_CHARS = 90_000
# Portion readings are independent, so they run side by side on one shared
# subscription session. The session holds the login lock only for that batch.
READ_CONCURRENCY = 4
# Reading a portion is note-taking; the judgments stay at SOL_EFFORT.
READ_EFFORT = "medium"
# A rejected fact selection is reselected in the same attempt, never queued.
FACT_ROUNDS = 3


class FactsRejected(ValueError):
    pass


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


def _answer_path(prompt, model, work):
    schema = inlined_json_schema(strict_json_schema(model))
    key = digest({"prompt": prompt, "schema": schema, "model": SOL_MODEL})
    return schema, key, Path(work) / "answers" / (key + ".json")


def sol(prompt, model, work: Path, request_id, *, runner=None, attempt=0, validate=lambda value: None,
        effort=SOL_EFFORT, session=None):
    # Completed, validated answers survive automatic retries. An unsuccessful
    # subscription turn gets a fresh bounded attempt after the server cooldown.
    # The effort is not part of the key: an answer is reusable however it was read.
    schema, key, saved = _answer_path(prompt, model, work)
    if saved.exists():
        raw = json.loads(saved.read_text())
        if digest(raw["answer"]) != raw["sha256"]:
            raise ValueError("A saved Sol answer changed after validation.")
        result = model.model_validate(raw["answer"])
        validate(result)
        return result
    options = {}
    if runner is None:
        from galley.codex_runner import run_structured
        runner = run_structured
        if session is not None:
            options["session"] = session
    # One directory per request: concurrent turns lock per directory.
    result = runner(prompt, schema, Path(work) / f"attempt-{attempt}" / key[:16],
                    request_id=request_id + "-" + key[:24],
                    timeout_seconds=1800, model=SOL_MODEL,
                    reasoning_effort=effort, no_tools=True, **options)
    result = model.model_validate(result)
    validate(result)
    saved.parent.mkdir(parents=True, exist_ok=True)
    temporary = saved.with_suffix(f".{key[:8]}.tmp")
    temporary.write_text(json.dumps({"answer": result.model_dump(), "sha256": digest(result)}))
    temporary.replace(saved)
    return result


def read_portions(source_chunks, work, *, runner=None, progress=lambda stage: None, attempt=0):
    """Every portion's reading, in order. Saved readings return at once; the rest
    run concurrently. A book that fits in one call has no readings."""
    if len(source_chunks) < 2:
        return []
    def validator(c):
        def validate_reading(reading):
            if (reading.chunk_id != c["id"] or
                reading.first_paragraph != c["paragraphs"][0]["id"] or
                reading.last_paragraph != c["paragraphs"][-1]["id"]):
                raise ValueError("Sol did not account for the complete manuscript portion.")
            if not reading.narrative.strip() or not reading.facts:
                raise ValueError("Sol returned an empty manuscript reading.")
            evidence_for(reading.facts, [c])
        return validate_reading
    missing = [c for c in source_chunks
               if not _answer_path(prompts.reading_prompt(c), Reading, work)[2].exists()]
    if missing:
        progress(f"Reading {len(missing)} manuscript portion(s), {min(READ_CONCURRENCY, len(missing))} at a time")
    session = None
    if missing and runner is None and len(missing) > 1:
        from galley.codex_session import SubscriptionSession
        session = SubscriptionSession()
    def read(c):
        return sol(prompts.reading_prompt(c), Reading, work, "reading-" + digest(c), runner=runner,
                   attempt=attempt, validate=validator(c), effort=READ_EFFORT, session=session)
    try:
        with ThreadPoolExecutor(max_workers=READ_CONCURRENCY) as pool:
            return list(pool.map(read, source_chunks))
    finally:
        if session is not None:
            session.close()


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
    readings = read_portions(source_chunks, work, runner=runner, progress=progress, attempt=attempt)
    evidence = (evidence_for([f for r in readings for f in r.facts], source_chunks)
                if readings else source_chunks[0]["paragraphs"])
    if public_briefs:
        from .facts import story_prompt
    else:
        story_prompt = prompts.story_prompt
    feedback = list(feedback or [])
    for round_number in range(1, (FACT_ROUNDS if public_briefs else 1) + 1):
        progress("Sol is selecting facts for five distinct teaser angles" if public_briefs
                 else "Sol is preparing author copy")
        prompt = story_prompt([r.model_dump() for r in readings], evidence, feedback or None)
        story = sol(prompt, Storysheet, work, "story-" + digest(prompt), runner=runner,
                    attempt=attempt, validate=lambda value: validate_story(value, source_chunks))
        # Save finished writing before its copy edit; retries must not rewrite it all.
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = candidate_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"source_sha256": digest(source_chunks),
                                        "story": story.model_dump(), "evidence": evidence,
                                        "feedback_sha256": digest(feedback or None)}))
        temporary.replace(candidate_path)
        try:
            return approve_prepared_copy(story, evidence, source_chunks, work, runner=runner,
                progress=progress, attempt=attempt, candidate_path=candidate_path)
        except FactsRejected as exc:
            # The audit's findings go straight back to Sol's own reselection.
            if round_number == FACT_ROUNDS:
                raise
            feedback.append(str(exc))


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
    # Every portion is supplied by construction, so for fact-sheet stories a
    # reported limitation (extraction damage, unsupported claims, absent outside
    # materials) is recorded, not a reason to refuse. Legacy copy still refuses.
    if not story.source_complete and not story.writer_brief.option_briefs:
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
    if draft.version == 2:
        # Check against Sol's saved full-book readings and the original cited
        # passages instead of reading the whole manuscript again per draft.
        from .facts import review_prompt
        readings = read_portions(source_chunks, work, runner=runner, progress=progress, attempt=attempt)
        evidence = (evidence_for([f for r in readings for f in r.facts] + list(story.public_facts),
                                 source_chunks) if readings else None)
        progress("Reviewing all five options against the full-book reading")
        prompt = review_prompt(story.model_dump(), draft.model_dump(), [r.model_dump() for r in readings],
                               draft_issues(draft), draft_hash, evidence=evidence,
                               source_chunks=source_chunks if len(source_chunks) == 1 else None)
    else:
        checks = []
        # Review a small source directly, rather than asking two editors in succession.
        for c in source_chunks if len(source_chunks) > 1 else []:
            progress(f"Checking teasers against manuscript portion {c['id']} of {len(source_chunks)}")
            source_prompt = prompts.source_review_prompt(c, draft.model_dump(), draft_hash)
            def validate_check(check, c=c):
                if check.chunk_id != c["id"] or check.draft_sha256 != draft_hash:
                    raise ValueError("The source review does not match this manuscript and draft.")
            check = sol(source_prompt, SourceReview, work, "source-review-" + digest(source_prompt), runner=runner,
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
