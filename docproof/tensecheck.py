"""A whole-book narrative tense profiler that never edits anything.

The `tense_shift` error type is deliberately sentence-internal: it catches
"She walked in and sees the mess" because both verbs share a sentence. What it
structurally cannot see is a whole scene written in the historical present
inside a past-tense book — every sentence in that scene is internally
consistent, so every current detector reads it as clean. The calibration case
is real: a past-tense novel with entire chapters in the historical present
needed 1,054 corrections across 371 paragraphs, and nothing in the pipeline
raised a hand.

This module is the missing instrument, and it is only an instrument. It reads
the whole book once, deterministically, for $0, and reports *where the volume
is*: the book's baseline tense, and the contiguous runs of paragraphs that
read against it. It does NOT emit findings, does NOT propose edits, and is not
wired into any apply path — converting a present-tense scene to past is
judgment work (which verbs are narration, which are habitual statements, which
sentences the author aimed at deliberately), so the output is a map for the
operator — a human or the Galley agent — to plan targeted paid re-reads over,
nothing more.

Three design commitments, all in the service of precision over recall:

* **Dialogue is stripped before counting.** Present-tense dialogue inside past
  narration is normal English ("“I am here,” she said."), and counting it would
  drown the signal. Double-quoted spans (curly and straight) are removed;
  an unclosed opening double quote strips to the end of the paragraph, since
  Word manuscripts routinely carry multi-paragraph speeches where only the
  final paragraph closes the quote. Single-quoted spans are stripped only when
  both marks are present AND the closer does not read as an apostrophe
  (i.e. is not followed by a letter) — bare apostrophes make single quotes too
  risky to guess at. What survives is "narration", and only narration counts.

* **The verb lists are curated and conservative.** A missed verb costs a
  little recall on a profiler; a plural noun counted as a verb ("the steps",
  "his looks") costs precision on every paragraph. So PAST is a hand-picked
  irregular list plus the regular `...ed` rule with a literal stop-list, and
  PRESENT is a hand-picked list of third-person narrative verbs. Ambiguous
  forms whose past and present spellings coincide (put, set, let, cut, hit,
  read, shut...) are deliberately absent from the PAST list — they carry no
  tense information. Nouns that merely end in -ed (hundred, sacred, wicked...)
  and double-e presents that the -ed rule would swallow (bleed, speed,
  proceed...) sit on the stop-list. Perfect-tense subtleties are not
  attempted: "had been running" counts past via "had", and that is enough.

* **Paragraph verdicts demand evidence.** Fewer than three signals is verdict
  "none" — a one-line beat like "She paused." says nothing about the book's
  tense and must neither vote in the baseline nor break a run.

The run detection mirrors how the calibration book was actually caught: the
present-dominant paragraphs were *contiguous* (whole scenes, whole chapters),
so maximal contiguous sequences against the baseline, filtered to ≥ 2
paragraphs or ≥ 120 narration words, surface exactly the spans a targeted
re-read should cover, longest first. Scattered single mixed paragraphs — the
normal texture of flashback and habitual statement — fall below the floor and
stay out of the report.

Pure stdlib (re, dataclasses, json-compatible output); no model calls, no
network, no third-party imports. `profile()` takes the ingest's ParagraphRefs,
`render()` prints the terminal summary, `to_json()` feeds the job artifacts.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Sequence

from .models import ParagraphRef

log = logging.getLogger("docproof.tensecheck")

# Locations whose text is the author's running prose. Headers/footers are
# furniture; tables are data; headings are excluded by `reviewable` already.
_NARRATION_LOCATIONS = ("body", "footnote", "endnote", "textbox")

# --- dialogue stripping ----------------------------------------------------
#
# Order matters: curly double pairs, then an unclosed curly opener to end of
# paragraph, then the same two steps for straight doubles, then single-curly
# pairs (both marks required; the closer must not be an apostrophe, i.e. not
# followed by a letter — ‘don’t worry’ strips whole, not to the first ’).
_CURLY_PAIR = re.compile(r"“[^”]*”")
_CURLY_OPEN = re.compile(r"“.*$", re.DOTALL)
_STRAIGHT_PAIR = re.compile(r"\"[^\"]*\"")
_STRAIGHT_OPEN = re.compile(r"\".*$", re.DOTALL)
_SINGLE_PAIR = re.compile(r"‘.*?’(?![^\W\d_])")


def _strip_dialogue(text: str) -> str:
    text = _CURLY_PAIR.sub(" ", text)
    text = _CURLY_OPEN.sub(" ", text)
    text = _STRAIGHT_PAIR.sub(" ", text)
    text = _STRAIGHT_OPEN.sub(" ", text)
    text = _SINGLE_PAIR.sub(" ", text)
    return text


# --- tense signals ---------------------------------------------------------
#
# Counting is done over lowercased narration tokens, so a sentence-initial
# "Is" or "Walked" still counts — capitalization carries no tense information.
# Tokens split on apostrophes ("he's" -> "he","s"), which loses contractions
# as verb signals but keeps the pronoun half — the right trade for a profiler.
_TOKEN = re.compile(r"[a-z]+")

# Irregular pasts whose spelling is unambiguously past. Forms shared with the
# present (put, set, let, cut, hit, read, shut, cost, hurt, spread, bet) are
# deliberately omitted: they carry no tense signal on their own.
_PAST_IRREGULAR = frozenset("""
    was were had did said went came saw took got made knew thought told felt
    left stood gave found held brought began kept ran sat spoke heard became
    ate drank drove fell fled flew forgot grew hung lost meant met paid rang
    sang sent shook slept sold sought spent stole stuck swam swore taught
    threw tore understood woke won wore wrote bought built caught chose drew
    fought led
""".split())

# Words the regular `[a-z]{3,}ed` rule would count that are not past-tense
# verbs. Two literal families, kept small and documented rather than clever:
#   * -ed words that are nouns/adjectives, not verbs;
#   * present-tense verbs and nouns ending -eed that the pattern swallows.
# (Three-letter cases — bed, red, led, wed, fed, need, seed... — never match:
# the rule requires three letters BEFORE the -ed.)
_ED_STOPLIST = frozenset("""
    hundred sacred naked wicked hatred kindred wretched rugged jagged crooked
    beloved indeed
    bleed breed creed greed speed steed tweed proceed succeed exceed misdeed
    nosebleed embed shred infrared
""".split())

_REGULAR_ED = re.compile(r"[a-z]{3,}ed$")

# Third-person narrative presents, hand-picked for how the historical present
# actually reads on the page ("She walks in. He looks up. She reaches for the
# door."). Some double as plural nouns (looks, steps, calls, smiles) — the
# 2:1 verdict dominance and the 3-signal floor absorb that noise.
_PRESENT = frozenset("""
    is are am has does says goes comes sees takes gets makes knows thinks
    tells feels stands looks turns walks runs sits speaks hears watches gives
    finds holds brings begins keeps seems wants asks replies smiles nods
    shakes reaches pulls pushes opens closes starts stops moves follows waits
    tries calls leans steps stares glances whispers shrugs sighs grabs drops
    lifts throws catches
""".split())

_FIRST_PRONOUNS = frozenset(("i", "we", "my", "our", "me", "us"))
_THIRD_PRONOUNS = frozenset(("he", "she", "they", "his", "her", "their",
                             "him", "them"))

_SAMPLE_CHARS = 120

# Verdict floor and dominance ratio; run-keeping floors; baseline margin.
_MIN_SIGNALS = 3
_DOMINANCE = 2
_RUN_MIN_PARAS = 2
_RUN_MIN_WORDS = 120
_BASELINE_MARGIN = 0.6
_PERSON_MIN_PRONOUNS = 20
_HEALTH_WARN_SHARE = 0.15
_RENDER_MAX_RUNS = 12


@dataclass(frozen=True)
class ParagraphTense:
    para_id: str
    verdict: str          # "past" | "present" | "mixed" | "none"
    past: int             # past-tense signal count in narration
    present: int          # present-tense signal count in narration
    sample: str           # first ~120 chars of the paragraph's narration text


@dataclass(frozen=True)
class TenseRun:
    para_ids: tuple[str, ...]   # contiguous present-dominant paragraphs
    words: int                  # total narration words in the run
    sample: str                 # opening narration of the first paragraph


@dataclass(frozen=True)
class TenseProfile:
    baseline: str                     # "past" | "present" | "unclear"
    person: str                       # "first" | "third" | "mixed" | "unclear"
    narration_paragraphs: int         # paragraphs with enough narration to classify
    present_share: float              # present-dominant share of classified paragraphs
    paragraphs: tuple[ParagraphTense, ...]   # every profiled paragraph, in order
    runs: tuple[TenseRun, ...]        # contiguous runs AGAINST the baseline, longest first

    def to_json(self) -> dict:
        """Everything, as plain dicts/lists, suitable for json.dump."""
        return {
            "baseline": self.baseline,
            "person": self.person,
            "narration_paragraphs": self.narration_paragraphs,
            "present_share": round(self.present_share, 4),
            "paragraphs": [
                {"para_id": p.para_id, "verdict": p.verdict, "past": p.past,
                 "present": p.present, "sample": p.sample}
                for p in self.paragraphs
            ],
            "runs": [
                {"para_ids": list(r.para_ids), "words": r.words,
                 "sample": r.sample}
                for r in self.runs
            ],
        }


def _sample(narration: str) -> str:
    return " ".join(narration.split())[:_SAMPLE_CHARS]


def _count_signals(tokens: Sequence[str]) -> tuple[int, int]:
    past = present = 0
    for tok in tokens:
        if tok in _PAST_IRREGULAR:
            past += 1
        elif tok in _PRESENT:
            present += 1
        elif _REGULAR_ED.fullmatch(tok) and tok not in _ED_STOPLIST:
            past += 1
    return past, present


def _verdict(past: int, present: int) -> str:
    if past + present < _MIN_SIGNALS:
        return "none"
    if past >= _DOMINANCE * present:
        return "past"
    if present >= _DOMINANCE * past:
        return "present"
    return "mixed"


def profile(paragraphs: Sequence[ParagraphRef]) -> TenseProfile:
    """Profile the book's narrative tense. Reads everything, changes nothing."""
    entries: list[ParagraphTense] = []
    word_counts: list[int] = []          # narration words, aligned with entries
    first = third = 0

    for p in paragraphs:
        if not p.reviewable or p.location not in _NARRATION_LOCATIONS:
            continue
        if not p.text.strip():
            continue
        narration = _strip_dialogue(p.text)
        tokens = _TOKEN.findall(narration.lower())
        if not tokens:
            continue
        past, present = _count_signals(tokens)
        entries.append(ParagraphTense(
            para_id=p.para_id, verdict=_verdict(past, present),
            past=past, present=present, sample=_sample(narration)))
        word_counts.append(len(tokens))
        for tok in tokens:
            if tok in _FIRST_PRONOUNS:
                first += 1
            elif tok in _THIRD_PRONOUNS:
                third += 1

    # Baseline: majority of decisive (past/present) paragraphs, weighted by
    # narration word count so a handful of long chapters cannot be outvoted by
    # many one-line beats. Within 60/40 the book has no baseline to hold.
    past_words = sum(w for e, w in zip(entries, word_counts)
                     if e.verdict == "past")
    present_words = sum(w for e, w in zip(entries, word_counts)
                        if e.verdict == "present")
    decisive_words = past_words + present_words
    if decisive_words == 0:
        baseline = "unclear"
    elif past_words / decisive_words >= _BASELINE_MARGIN:
        baseline = "past"
    elif present_words / decisive_words >= _BASELINE_MARGIN:
        baseline = "present"
    else:
        baseline = "unclear"

    if first + third < _PERSON_MIN_PRONOUNS:
        person = "unclear"
    elif first >= _DOMINANCE * third:
        person = "first"
    elif third >= _DOMINANCE * first:
        person = "third"
    else:
        person = "mixed"

    classified = [e for e in entries if e.verdict != "none"]
    narration_paragraphs = len(classified)
    present_share = (
        sum(1 for e in classified if e.verdict == "present")
        / narration_paragraphs if narration_paragraphs else 0.0)

    runs = _find_runs(entries, word_counts, baseline)

    log.info("tensecheck: baseline=%s person=%s classified=%d "
             "present_share=%.3f runs=%d", baseline, person,
             narration_paragraphs, present_share, len(runs))

    return TenseProfile(
        baseline=baseline, person=person,
        narration_paragraphs=narration_paragraphs,
        present_share=present_share,
        paragraphs=tuple(entries), runs=tuple(runs))


def _find_runs(entries: Sequence[ParagraphTense],
               word_counts: Sequence[int], baseline: str) -> list[TenseRun]:
    """Maximal contiguous sequences of the verdict OPPOSITE the baseline.

    Verdict "none" neither joins nor breaks a run — a one-line beat inside a
    present-tense scene must not split the scene in two. "mixed" and the
    baseline verdict both break. Kept when ≥ 2 paragraphs or ≥ 120 narration
    words; sorted longest-by-words first so the operator reads top-down.
    """
    if baseline not in ("past", "present"):
        return []
    against = "present" if baseline == "past" else "past"

    runs: list[TenseRun] = []
    ids: list[str] = []
    words = 0
    sample = ""

    def close() -> None:
        nonlocal ids, words, sample
        if ids and (len(ids) >= _RUN_MIN_PARAS or words >= _RUN_MIN_WORDS):
            runs.append(TenseRun(para_ids=tuple(ids), words=words,
                                 sample=sample))
        ids, words, sample = [], 0, ""

    for e, w in zip(entries, word_counts):
        if e.verdict == against:
            if not ids:
                sample = e.sample
            ids.append(e.para_id)
            words += w
        elif e.verdict == "none":
            continue
        else:
            close()
    close()

    runs.sort(key=lambda r: r.words, reverse=True)
    return runs


def render(p: TenseProfile) -> str:
    """The compact terminal summary: where the book stands, where to read."""
    lines: list[str] = []
    lines.append(f"Narrative tense profile: baseline {p.baseline.upper()}, "
                 f"person {p.person}.")

    if p.baseline == "unclear":
        lines.append(
            f"{p.narration_paragraphs} narration paragraphs classified, but "
            f"no tense holds a 60/40 majority — the book has no single "
            f"baseline to profile against, so no runs are reported.")
    else:
        against = "present" if p.baseline == "past" else "past"
        n_against = sum(1 for e in p.paragraphs if e.verdict == against)
        share = (n_against / p.narration_paragraphs
                 if p.narration_paragraphs else 0.0)
        head = (f"{n_against} of {p.narration_paragraphs} narration "
                f"paragraphs ({share:.1%}) read {against}-dominant")
        if share > _HEALTH_WARN_SHARE:
            lines.append(head + f" — the {p.baseline}-tense baseline is NOT "
                         f"being held; plan targeted re-reads over the runs "
                         f"below.")
        else:
            lines.append(head + f" — consistent with a {p.baseline}-tense "
                         f"baseline.")

    for r in p.runs[:_RENDER_MAX_RUNS]:
        span = (r.para_ids[0] if len(r.para_ids) == 1
                else f"{r.para_ids[0]}..{r.para_ids[-1]}")
        lines.append(f"  {span}  ({len(r.para_ids)} ¶, {r.words} words)  "
                     f"“{r.sample}…”")
    if len(p.runs) > _RENDER_MAX_RUNS:
        lines.append(f"  …and {len(p.runs) - _RENDER_MAX_RUNS} more runs "
                     f"(see the JSON profile).")

    lines.append("Runs are where to read, not what to change: tense "
                 "conversion is judgment work, and nothing here auto-applies.")
    return "\n".join(lines)
