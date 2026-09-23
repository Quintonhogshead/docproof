"""Five concurrent DeepSeek requests; persist successes before retrying failures."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

from docproof.providers import strict_json_schema
from docproof.teasers import DEEPSEEK_MODEL
from docproof.teasers.models import Draft, Teaser, digest, teaser_issues, draft_issues
from docproof.teasers.facts import writer_prompt, validate_brief

# Immediate rewrites per option for a length or paragraph miss.
WRITE_ROUNDS = 4


def generate(queue, task, *, provider=None):
    from .teasers import TeaserError, current_writer_brief, MAX_DRAFTS_PER_DAY
    brief = current_writer_brief(task)
    validate_brief(brief)
    recent = [t for t in task.get("generation_times", []) if t > time.time() - 86400]
    if len(recent) >= MAX_DRAFTS_PER_DAY:
        queue.retry(task, "Daily teaser allowance reached; resuming automatically tomorrow.",
                    delay=max(60, recent[0] + 86400 - time.time()), resume="story_ready")
        return queue.get(task["id"])
    if provider is None:
        from app.settings import get_api_key
        from docproof.providers.deepinfra_provider import DeepInfraProvider
        key = get_api_key("deepinfra")
        if not key:
            raise TeaserError("Add the DeepInfra key to DocProof's cloud settings.")
        class DeepSeekWriter(DeepInfraProvider):
            def _body(self, **kwargs):
                body = super()._body(**kwargs)
                body.update(reasoning_effort="none", temperature=0.7, top_p=1)
                return body
        provider = DeepSeekWriter(api_key=key, max_retries=0, effort=None)
        provider.client = provider.client.with_options(timeout=180)
    cache = task.setdefault("option_results", {})
    # Rejected drafts are never reused after editorial review, even if an angle is unchanged.
    revision = len(task.get("reviews", []))
    keys = {o.number: digest({"brief": o.model_dump(), "revision": revision, "model": DEEPSEEK_MODEL})
            for o in brief.option_briefs}
    pending = [o for o in brief.option_briefs if keys[o.number] not in cache]
    task["generation_times"] = recent + ([time.time()] if pending else [])
    task["progress"] = "DeepSeek is writing five teasers from Sol's selected facts"
    task["state"] = "generating"
    queue.save(task)
    errors = []
    attempts = task.setdefault("option_attempts", {})

    def write(option):
        """One option, with its mechanical gate enforced here: a length or
        paragraph miss is rewritten at once (seconds), never sent round the queue."""
        receipts, handoffs = [], []
        system, user = writer_prompt(option)
        if attempts.get(str(option.number), 0):
            system += "\nPlan three short paragraphs of about 55 words each, 165 words total. Use normal spaces between all words.\n"
        schema = strict_json_schema(Teaser)
        schema["properties"]["paragraphs"].update(minItems=3, maxItems=3)
        issues = []
        for _ in range(WRITE_ROUNDS):
            prompt = system
            if issues:
                # Only the counts go back; the writer never sees its rejected copy.
                prompt += ("\nYour previous version was rejected: " + "; ".join(issues) +
                           " Write it again: exactly three paragraphs of about 55 words each, 150–200 words total.\n")
            result = provider.complete_structured(model=DEEPSEEK_MODEL, system=prompt, user=user,
                        schema=schema, schema_name="book_teaser", max_tokens=1024)
            receipts.append({"at": time.time(), "option": option.number, "model": DEEPSEEK_MODEL,
                             "stop_reason": result.stop_reason, "usage": vars(result.usage)})
            handoffs.append({"option": option.number, "public_brief": option.model_dump(),
                             "prompt_sha256": digest([prompt, user])})
            if result.stop_reason != "ok" or result.parsed is None:
                raise TeaserError("The writer did not return a complete teaser.")
            teaser = Teaser.model_validate(result.parsed)
            if teaser.number != option.number or teaser.angle != option.angle:
                raise TeaserError("The writer changed the selected teaser identity.")
            issues = teaser_issues(teaser, version=2)
            if not issues:
                return option, teaser, receipts, handoffs
        raise TeaserError('; '.join(issues))

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(write, o): o for o in pending}
        for future in as_completed(futures):
            option = futures[future]
            attempts[str(option.number)] = attempts.get(str(option.number), 0) + 1
            try:
                option, teaser, receipts, handoffs = future.result()
                cache[keys[option.number]] = teaser.model_dump()
            except Exception as exc:
                errors.append(f"Option {option.number}: {exc}")
                receipts, handoffs = [], []
            task.setdefault("generation_receipts", []).extend(receipts)
            task.setdefault("writer_handoffs", []).extend(handoffs)
            # One owning thread persists each completed request, even when others fail.
            queue.save(task)
    if errors:
        queue.retry(task, '; '.join(errors), resume="story_ready", delay=60)
        return queue.get(task["id"])
    draft = Draft(version=2, teasers=[Teaser.model_validate(cache[keys[n]]) for n in range(1, 6)],
                  opening_hooks=[], editorial_note="", elements=[], best_practices=[], modification_checklist=[])
    issues = draft_issues(draft)
    if issues:
        # Duplicate options need fresh writing; never feed the rejected copy back.
        task["option_results"] = {}
        queue.retry(task, '; '.join(issues), resume="story_ready", delay=60)
        return queue.get(task["id"])
    task["drafts"].append({"content": draft.model_dump(), "sha256": digest(draft),
                          "model": DEEPSEEK_MODEL, "provider": "deepinfra", "operation": "generation"})
    task.pop("error", None)
    task["small_edit_rounds"] = 0
    task["progress"] = "Sol is checking the five teasers against the manuscript"
    queue.save(task, "drafted")
    return queue.get(task["id"])
