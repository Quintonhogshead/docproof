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

Paragraphs share a request rather than getting one each — joined by a blank
line, which LanguageTool reads as a paragraph break, so each is still analysed on
its own. The fixed cost of asking is most of the scan on a book of ordinary
paragraphs: 43k words took 9.7s one paragraph at a time and 4.8s in 20,000-
character requests, on one thread, for a candidate set that came back identical.
That is the lever that helps a one-core box, where the thread pool cannot.

Opt-in and off by default: it needs Java + the LanguageTool jar, so a run only
pays for it when `languagetool.enabled` is set. See RewriteCandidate reuse below
— the confirm machinery is shared verbatim.
"""
from __future__ import annotations

import logging
import os
import threading
import unicodedata
from bisect import bisect_right
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


def _fold_accents(s: str) -> str:
    """`s` with every combining diacritic stripped (café -> cafe)."""
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def _mech_key(s: str) -> str:
    """Letters and digits only, accent-preserving, casefolded — so two strings
    that differ only in whitespace, punctuation, or letter case share a key
    (`5-6` and `5–6`; `Bf` and `BF`; `anymore` and `any more`)."""
    return "".join(ch for ch in s.casefold() if ch.isalnum())


def classify_replacement(original: str, replacement: str) -> str:
    """How a LanguageTool replacement differs from the text it replaces. This is
    the guard that keeps LanguageTool from spelling a voice coinage into a real
    word and applying it as a blind tracked edit (Purpura: boop->book, sesh->mesh,
    crudité->erudite — 35 real-word corruptions auto-applied and passed every
    gate).

    - "mechanical": only whitespace, punctuation, or letter case changed — a
      comma, a hyphen to an en-dash, a capital, a closed compound. LanguageTool's
      reliable territory; safe to route as a possible tracked edit.
    - "deaccent": the same letters with a diacritic STRIPPED (cliché -> cliche).
      Never a proofreading fix we want: house style keeps the accent Merriam-
      Webster sets, so these are dropped outright.
    - "word": the letters themselves changed (boop -> book). A real-word
      substitution LanguageTool is not trusted to make blind on voice-heavy
      prose; routed to a margin query so a human rules, never applied silently.
    """
    o, r = _mech_key(original), _mech_key(replacement)
    if o == r:
        return "mechanical"
    fo, fr = _fold_accents(o), _fold_accents(r)
    if fo == fr:
        # Letters match once accents are folded away → an accent-only change.
        # Stripping an accent the original carried is a de-accent; adding one (or
        # a pure case/punct shuffle around the accent) is mechanical.
        return "deaccent" if o != fo and r == fr else "mechanical"
    return "word"


_tool_cache: dict[str, object] = {}
# Guards the cache dict and shutdown()'s teardown, so a create and a close can't
# race to leave a half-built or already-closed server behind. It protects the
# cache *structure* only — it is NOT what makes concurrent LanguageTool use
# safe. The batch engine guarantees that: submit and collect (the two heavy
# passes that call in here) both run on the one worker thread and so never
# overlap. This lock is cheap insurance for any future caller that forgets that.
_cache_lock = threading.Lock()


def _get_tool(dictionary: str):
    """One long-lived local server per dictionary. The first call downloads the
    jar (~260 MB, cached under ~/.cache) and boots a JVM; reused thereafter.

    The denylist is deliberately NOT pushed to the server. Handing it
    `disabledRules` so the JVM skips those rules sounds like the obvious saving
    and measures as nothing: on 28k words of prose with a warm server it was
    6.96s against 6.93s, the same run to run. (An earlier reading of that pair as
    a 1.4x win was the JVM's JIT warming up on the first of the two.) The
    filtering in `propose` is where the denylist belongs — it is the guarantee,
    it covers the issue types too, and it costs nothing worth measuring."""
    with _cache_lock:
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
    with _cache_lock:
        for tool in _tool_cache.values():
            try:
                tool.close()
            except Exception:                # pragma: no cover
                pass
        _tool_cache.clear()


# Paragraphs are sent several to a request, joined by a blank line. The blank
# line is a paragraph break to LanguageTool, so each one is still analysed on its
# own — what changes is how many times the cost of *asking* is paid. That cost is
# most of the scan: on 28k words of prose, one request per paragraph took 6.4s
# and the same text in ~20k-character requests took 3.0s, for a candidate set
# that was identical at every batch size tried (1, 5, 10, 25, 50, 100, 200
# paragraphs per request). Grouping by characters rather than by paragraph count
# is what keeps that true of a book of long paragraphs as well as one of verse.
_JOIN = "\n\n"


def _groups(texts: Sequence[str], budget: int) -> list[list[int]]:
    """Runs of consecutive paragraph indices to send as one request, each run
    under `budget` characters. A paragraph longer than the budget is a group of
    its own rather than being split — a check has to see a whole sentence."""
    if budget <= 0:
        return [[i] for i in range(len(texts))]
    out: list[list[int]] = []
    cur: list[int] = []
    size = 0
    for i, t in enumerate(texts):
        if cur and size + len(t) > budget:
            out.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += len(t) + len(_JOIN)
    if cur:
        out.append(cur)
    return out


def _scan(tool, texts: Sequence[str]) -> list[list]:
    """One request for all of `texts`; the matches split back out per text, with
    offsets made paragraph-local again.

    A match whose span crosses the join is dropped. A per-paragraph check could
    not have produced one, so keeping it would be the batching inventing a
    finding — and there is no single paragraph to anchor it to anyway."""
    if len(texts) == 1:
        return [tool.check(texts[0])]
    starts, at = [], 0
    for t in texts:
        starts.append(at)
        at += len(t) + len(_JOIN)
    out: list[list] = [[] for _ in texts]
    for m in tool.check(_JOIN.join(texts)):
        i = bisect_right(starts, m.offset) - 1
        if i < 0:
            continue
        local = m.offset - starts[i]
        if local + m.error_length > len(texts[i]):
            continue                      # straddles the join
        m.offset = local
        out[i].append(m)
    return out


def propose(paragraphs: Sequence[ParagraphRef], *,
            lexicon: Sequence[str] = (),
            dictionary: str = "en-US",
            picky: bool = False,
            disabled_rules: frozenset[str] = DEFAULT_DISABLED_RULES,
            disabled_issue_types: frozenset[str] = DEFAULT_DISABLED_ISSUE_TYPES,
            edit_word_replacements: bool = False,
            workers: int = 0,
            scan_chars: int = 20000,
            coverage=None,
            progress: Callable[[int, int], None] | None = None,
            ) -> list[RewriteCandidate]:
    """Run LanguageTool over each paragraph and return the surviving matches as
    RewriteCandidates (para_id, span, original, replacement) for `rewrite.confirm`.

    Only matches with a concrete replacement are kept — the valve needs something
    to apply or to show in a query. `lexicon` is the spell scan's protected words;
    a misspelling flag on one of them is a name/coinage and is dropped here.

    Paragraphs are checked several to a request — `scan_chars` of text per
    request, 0 for one request per paragraph — because the fixed cost of a
    request is most of the scan on a book of ordinary paragraphs. They are joined
    by a blank line, which LanguageTool reads as a paragraph break, so each is
    still analysed on its own; see `_scan`. This is the lever that matters on a
    one-core box, where the thread pool below cannot help.

    The requests run over a thread pool so a multi-core box scans in parallel;
    the pool is capped at the usable CPU count (`workers` lowers it, 0 = auto),
    so on a single-core VM it stays a serial loop. `progress`, if given, is
    called `(done, total)` in paragraphs as each request lands — the scan is
    otherwise silent for minutes on a long manuscript, and the job card reads
    that as a hang. Candidate order stays deterministic: the filtering below
    walks the paragraphs in order, not in completion order."""
    if not AVAILABLE:
        log.warning("LanguageTool not installed; propose() returns nothing.")
        if coverage is not None:
            coverage.note("LanguageTool", "the pass is enabled but LanguageTool "
                          "is not installed, so the mechanical-floor scan "
                          "(commas, missing words, hyphenation) did not run",
                          "skipped")
        return []
    lex = {w.strip("'’\".,").lower() for w in lexicon}
    tool = _get_tool(dictionary)
    # `picky` toggles LanguageTool's stricter rule level (adds level=picky to
    # each request). The server is long-lived and shared, so set it per call —
    # a run that leaves it True would leak the level into a later non-picky run
    # against the same cached server.
    if hasattr(tool, "picky"):
        tool.picky = picky

    reviewable = [p for p in paragraphs if getattr(p, "reviewable", True)]
    texts = [p.text for p in reviewable]
    total = len(reviewable)
    pool = _usable_cpus()
    if workers:
        pool = max(1, min(workers, pool))

    def scan(group: list[int]) -> tuple[list[int], list[list], int]:
        """One request's worth of paragraphs. On failure the group is retried one
        paragraph at a time, so a paragraph the scanner chokes on costs that
        paragraph and not the fifty it was batched with."""
        try:
            return group, _scan(tool, [texts[i] for i in group]), 0
        except Exception as e:           # pragma: no cover - server/JVM trouble
            if len(group) == 1:
                log.warning("LanguageTool: check failed on %s: %s",
                            reviewable[group[0]].para_id, e)
                return group, [[]], 1
            log.warning("LanguageTool: request of %d paragraph(s) failed (%s); "
                        "retrying them one at a time", len(group), e)
            out, bad = [], 0
            for i in group:
                _, got, f = scan([i])
                out.append(got[0])
                bad += f
            return group, out, bad

    # Scan concurrently, but keep results keyed by input position so the ordered
    # walk afterwards is unaffected by which request finished first.
    matches_by_idx: dict[int, list] = {}
    done = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=pool) as ex:
        futs = [ex.submit(scan, g) for g in _groups(texts, scan_chars)]
        for fut in as_completed(futs):
            group, got, bad = fut.result()
            for i, ms in zip(group, got):
                matches_by_idx[i] = ms
            failed += bad
            done += len(group)
            if progress:
                progress(done, total)
    # A handful of paragraphs the scanner choked on read downstream as fewer
    # mechanical findings, indistinguishable from clean paragraphs — say how
    # many went unscanned rather than let the thinner result pass for a full one.
    if failed and coverage is not None:
        coverage.note("LanguageTool", f"{failed} of {total} paragraph(s) could "
                      f"not be scanned and were left unchecked for mechanical "
                      f"errors", "partial")

    cands: list[RewriteCandidate] = []
    seen: set[tuple[str, int, int]] = set()
    dropped = {"rule": 0, "issue": 0, "name": 0, "no_fix": 0, "deaccent": 0}
    queried_word = 0
    for i, p in enumerate(reviewable):
        for m in matches_by_idx.get(i, ()):
            issue = m.rule_issue_type
            if m.rule_id in disabled_rules:
                dropped["rule"] += 1; continue
            if issue in disabled_issue_types:
                dropped["issue"] += 1; continue
            original = m.matched_text
            # Protected names/coinages/brands (the manuscript's own lexicon plus
            # the house allowlist) are off-limits to EVERY LanguageTool rule, not
            # just the misspelling class — a "grammar" or "typography" rule read
            # UGGs/Skechers as an error and "fixed" them past the old
            # misspelling-only guard.
            if original.strip("'’\".,").lower() in lex:
                dropped["name"] += 1; continue
            reps = [r for r in (m.replacements or []) if r is not None]
            if not reps:
                dropped["no_fix"] += 1; continue
            replacement = reps[0]
            kind = classify_replacement(original, replacement)
            if kind == "deaccent":
                # House style keeps the accent Merriam-Webster sets (cliché,
                # crudité); LanguageTool must never strip it.
                dropped["deaccent"] += 1; continue
            # A real-word substitution (boop->book) is where LanguageTool corrupts
            # voice on this kind of book. It may still be worth a question, but it
            # can never ride the edit channel blind: force it to a margin query
            # regardless of the confirm verdict (edit_word_replacements re-opens
            # the old behaviour for a house that trusts it).
            force_query = kind == "word" and not edit_word_replacements
            if force_query:
                queried_word += 1
            start, end = m.offset, m.offset + m.error_length
            key = (p.para_id, start, end)
            if key in seen:
                continue
            seen.add(key)
            cands.append(RewriteCandidate(
                para_id=p.para_id, start=start, end=end,
                original=p.text[start:end], replacement=replacement,
                force_query=force_query))

    log.info("LanguageTool: %d candidate(s) after filter "
             "(dropped %d name, %d style, %d artifact-rule, %d de-accent, "
             "%d no-fix; %d real-word swap%s query-only)",
             len(cands), dropped["name"], dropped["issue"], dropped["rule"],
             dropped["deaccent"], dropped["no_fix"], queried_word,
             "" if queried_word == 1 else "s")
    return cands
