"""LanguageTool candidate source: a local, deterministic mechanical-floor pass
that proposes edits for the SAME confirm/query valve every other pass uses.

LanguageTool is a rules-based checker (≈1,600 English rules + n-gram perplexity)
run as a LOCAL Java server — no network, no per-call cost, no client text leaving
the machine. It is orthogonal to the LLM detector: on a real manuscript it lands
on commas, missing words, and compound-modifier hyphenation the detector glides
past, and it overlaps the detector's catches by only ~5%. Its weakness is the
mirror image — it cannot see mid-sentence capitalization or word-choice errors,
which stay with the model passes.

So this module does NOT trust LanguageTool to edit. It proposes candidates and
hands each to `rewrite.confirm`, which rules on it in a literary context and
routes the result through the standard valve: a beyond-doubt error becomes a
tracked change, a softer one a margin query, a "keep" nothing. LanguageTool's
own precision noise — a character name it reads as a misspelling, an intentional
repetition, an over-eager hyphen — is caught there, never emitted blind.

Two filters run before the model ever sees a candidate, because they are cheap
and certain:
  * the spell scan's lexicon (the author's own names/coinages) suppresses the
    proper-noun "misspelling" flags that are LanguageTool's largest FP source;
  * a small rule/issue-type denylist drops paragraph-isolation artifacts
    (unpaired-quote, sentence-start caps) and pure style advice (wordiness).

Opt-in and off by default: it needs Java + the LanguageTool jar, so a run only
pays for it when `languagetool.enabled` is set. See RewriteCandidate reuse below
— the confirm machinery is shared verbatim.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Sequence

from .models import ParagraphRef
from .rewrite import RewriteCandidate

log = logging.getLogger("docproof.languagetool")

try:                                    # optional dependency; the pass is opt-in
    import language_tool_python          # noqa: F401
    AVAILABLE = True
except Exception:                        # pragma: no cover - import environment
    AVAILABLE = False


# Rules that fire on artifacts of per-paragraph checking or on pure style, never
# on the mechanical errors we want. Dropped before the model, at zero recall cost
# (measured: removing these left located recall unchanged, 155 -> 153 on Michalak).
DEFAULT_DISABLED_RULES = frozenset({
    "EN_UNPAIRED_QUOTES",        # dialogue that opens/closes across paragraphs
    "UPPERCASE_SENTENCE_START",  # sentence fragments / dialogue lead-ins
    "WHITESPACE_RULE",           # normalization territory, not proofreading
})
DEFAULT_DISABLED_ISSUE_TYPES = frozenset({
    "style",                     # GONNA, wordiness — advice, not error
})


def all_disabled_rules(extra: Sequence[str] = ()) -> frozenset[str]:
    """The built-in artifact/whitespace denylist plus any config extras."""
    return DEFAULT_DISABLED_RULES | frozenset(extra)


_tool_cache: dict[str, object] = {}


def _get_tool(dictionary: str):
    """One long-lived local server per dictionary. The first call downloads the
    jar (~260 MB, cached under ~/.cache) and boots a JVM; reused thereafter."""
    tool = _tool_cache.get(dictionary)
    if tool is None:
        log.info("LanguageTool: starting local server (%s)…", dictionary)
        tool = language_tool_python.LanguageTool(dictionary)
        _tool_cache[dictionary] = tool
        log.info("LanguageTool: server up.")
    return tool


def _usable_cpus() -> int:
    """Cores this process may actually run on — the cgroup/affinity view, not the
    host's total. On a Fly shared-cpu-1x this is 1, so the scan pool below stays
    serial; on a bigger VM it opens up automatically."""
    try:
        return max(1, len(os.sched_getaffinity(0)))   # Linux, honours cgroups
    except AttributeError:                             # pragma: no cover - non-Linux
        return max(1, os.cpu_count() or 1)


def shutdown() -> None:
    """Stop any running local servers (call at end of run)."""
    for tool in _tool_cache.values():
        try:
            tool.close()
        except Exception:                # pragma: no cover
            pass
    _tool_cache.clear()


def propose(paragraphs: Sequence[ParagraphRef], *,
            lexicon: Sequence[str] = (),
            dictionary: str = "en-US",
            disabled_rules: frozenset[str] = DEFAULT_DISABLED_RULES,
            disabled_issue_types: frozenset[str] = DEFAULT_DISABLED_ISSUE_TYPES,
            workers: int = 0,
            progress: Callable[[int, int], None] | None = None,
            ) -> list[RewriteCandidate]:
    """Run LanguageTool over each paragraph and return the surviving matches as
    RewriteCandidates (para_id, span, original, replacement) for `rewrite.confirm`.

    Only matches with a concrete replacement are kept — the valve needs something
    to apply or to show in a query. `lexicon` is the spell scan's protected words;
    a misspelling flag on one of them is a name/coinage and is dropped here.

    The per-paragraph checks run over a thread pool so a multi-core box scans in
    parallel; the pool is capped at the usable CPU count (`workers` lowers it,
    0 = auto), so on a single-core VM it stays the old serial loop. `progress`,
    if given, is called `(done, total)` as each paragraph's check lands — the
    scan is otherwise silent for minutes on a long manuscript, and the job card
    reads that as a hang. Candidate order stays deterministic: the filtering
    below walks the paragraphs in order, not in completion order."""
    if not AVAILABLE:
        log.warning("LanguageTool not installed; propose() returns nothing.")
        return []
    lex = {w.strip("'’\".,").lower() for w in lexicon}
    tool = _get_tool(dictionary)

    reviewable = [p for p in paragraphs if getattr(p, "reviewable", True)]
    total = len(reviewable)
    pool = _usable_cpus()
    if workers:
        pool = max(1, min(workers, pool))

    # Scan concurrently, but keep results keyed by input position so the ordered
    # walk afterwards is unaffected by which check finished first.
    matches_by_idx: dict[int, list] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=pool) as ex:
        futs = {ex.submit(tool.check, p.text): i
                for i, p in enumerate(reviewable)}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                matches_by_idx[i] = fut.result()
            except Exception as e:       # pragma: no cover - one bad paragraph
                log.warning("LanguageTool: check failed on %s: %s",
                            reviewable[i].para_id, e)
                matches_by_idx[i] = []
            done += 1
            if progress:
                progress(done, total)

    cands: list[RewriteCandidate] = []
    seen: set[tuple[str, int, int]] = set()
    dropped = {"rule": 0, "issue": 0, "name": 0, "no_fix": 0}
    for i, p in enumerate(reviewable):
        for m in matches_by_idx.get(i, ()):
            issue = m.rule_issue_type
            if m.rule_id in disabled_rules:
                dropped["rule"] += 1; continue
            if issue in disabled_issue_types:
                dropped["issue"] += 1; continue
            original = m.matched_text
            if issue == "misspelling" and original.strip("'’\".,").lower() in lex:
                dropped["name"] += 1; continue
            reps = [r for r in (m.replacements or []) if r is not None]
            if not reps:
                dropped["no_fix"] += 1; continue
            start, end = m.offset, m.offset + m.error_length
            key = (p.para_id, start, end)
            if key in seen:
                continue
            seen.add(key)
            cands.append(RewriteCandidate(
                para_id=p.para_id, start=start, end=end,
                original=p.text[start:end], replacement=reps[0]))

    log.info("LanguageTool: %d candidate(s) after filter "
             "(dropped %d name, %d style, %d artifact-rule, %d no-fix)",
             len(cands), dropped["name"], dropped["issue"],
             dropped["rule"], dropped["no_fix"])
    return cands
