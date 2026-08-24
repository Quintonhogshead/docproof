"""Local generators for candidate screening.

Generation is intentionally model-free. The heuristics below are broad: a
candidate is a question to screen, not a claim that the manuscript is wrong.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, Sequence

from .candidate_models import (
    Candidate, CandidateAnchor, CandidateStatus, deterministic_candidate_id)
from .models import DocumentModel, Finding, ParagraphRef, index_paragraphs


INITIAL_CANDIDATE_TYPES = (
    "dialogue_tag_punctuation",
    "quote_balance",
    "introductory_comma",
    "direct_address_comma",
    "number_style",
    "currency_style",
    "repeated_word",
    "word_echo",
    "heading_sequence",
    "list_punctuation",
    # P2-04 punctuation across more boundaries (semicolon, colon, parenthesis
    # balance) plus the adapted deterministic punctuation ERROR sweeps.
    "punctuation_style",
    # The top documented detector gap: the comma before a coordinating
    # conjunction joining two independent clauses.
    "compound_sentence_comma",
    # P2-03 lexical: commonly confused homophones/near-homophones.
    "homophone",
    # P2-03 lexical: document-wide term/spelling inconsistency (adapted from the
    # existing consistency scan).
    "term_consistency",
    # P2-05 grammar: LanguageTool / parser-backed mechanical floor (adapted).
    "grammar",
    # Exhaustive comma-boundary sweep: every comma-eligible seam becomes a
    # question — clause joins in both directions (missing AND removable),
    # every remaining comma, fronted-opener seams, bare nonrestrictive
    # who/which. Deliberately low-precision; the judge buys the precision.
    "comma_boundary",
)

_WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
_REPEATED = re.compile(
    r"\b(?P<first>[A-Za-z][A-Za-z'’\-]*)"
    r"(?P<gap>\s+)(?P<second>(?P=first))\b", re.IGNORECASE)
_NUMBER = re.compile(
    r"(?<![\w])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    r"(?:st|nd|rd|th)?(?![\w])", re.IGNORECASE)
_CURRENCY = re.compile(
    r"(?<!\w)(?:US\$|CA\$|AU\$|[$£€¥])\s?"
    r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?"
    r"|(?<!\w)(?:USD|CAD|AUD|GBP|EUR|JPY)\s+"
    r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?(?!\w)",
    re.IGNORECASE)

# Verbs that almost always report speech: a period before the closing quote is
# reliably a mispunctuated continuing tag.
_CORE_SPEECH_VERBS = (
    "said", "asked", "answered", "replied", "whispered", "shouted",
    "yelled", "murmured", "cried", "stammered", "muttered",
)
# Verbs that report speech OR narrate an action ("Tannithan continued his
# search", "she added a log"). After a period these are ambiguous, so they are
# never auto-corrected; a following direct object marks a clear action beat.
_DUAL_USE_VERBS = (
    "called", "added", "continued", "remarked", "observed", "began",
)
_SPEECH_VERBS = _CORE_SPEECH_VERBS + _DUAL_USE_VERBS
_DIALOGUE_TAG = re.compile(
    r"(?P<punct>[,.!?]?)(?P<quote>[”\"])(?P<space>\s+)"
    r"(?P<subject>he|she|they|we|i|[A-Z][a-z]+)\s+"
    rf"(?P<verb>{'|'.join(_SPEECH_VERBS)})\b")
# A determiner/possessive right after the verb ("continued HIS search") is the
# tell of an action beat rather than a speech tag.
_ACTION_OBJECT = re.compile(
    r"\s+(?:his|her|its|their|my|our|your|the|a|an|another|toward|towards|"
    r"into|onto|across|down|up|over|through|past)\b", re.IGNORECASE)

# Transitional adverbs that reliably take a comma when they open a sentence.
_STRONG_INTRO = re.compile(
    r"(?P<prefix>(?:^|(?<=[.!?]\s)))"
    r"(?P<phrase>However|Therefore|Meanwhile|Finally|"
    r"Of course|For example|In fact)\b",
    re.IGNORECASE)
# Words that OPEN a sentence sometimes as an interjection/ordinal wanting a
# comma ("No, I won't"; "First, we eat") and sometimes as a determiner or the
# head of a phrase that must NOT be split ("No matter", "No one", "Instead of",
# "First base"). A local generator cannot tell which, so these are always sent
# to the judge — never auto-inserted — and the clearest phrase traps are
# excluded outright so the judge is not even asked. (The Johnson canary applied
# "No, matter", "No, servant girl", and "Instead, of" as hard errors.)
_AMBIGUOUS_INTRO = re.compile(
    r"(?P<prefix>(?:^|(?<=[.!?]\s)))"
    r"(?P<phrase>Yes"
    r"|No(?!\s+(?:matter|one|longer|more|doubt|sign|way|such)\b)"
    r"|Well(?!\s+(?:done|enough)\b)"
    r"|Instead(?!\s+of\b)"
    r"|First(?!\s+(?:base|class|aid|place|time|floor|name)\b)"
    r"|Second(?!\s+(?:base|class|hand|floor|time|nature)\b)"
    r"|Third(?!\s+(?:base|class|floor|time|party)\b))\b",
    re.IGNORECASE)
_CLAUSE_INTRO = re.compile(
    r"(?P<prefix>(?:^|(?<=[.!?]\s)))"
    r"(?P<phrase>After|Before|When|While|If|Although|Because|Since|"
    r"Once|Unless|Until|Whenever|Whereas)\b", re.IGNORECASE)

_DIRECT_ADDRESS = re.compile(
    r"(?P<lead>\b(?:hello|hi|hey|goodbye|please|listen|look|wait|"
    r"come on|thank you|thanks))(?P<gap>\s+)(?P<name>[A-Z][a-z]+)\b",
    re.IGNORECASE)

# P2-03: confusable words, signal-gated. Flagging every there/their/its for
# judgment burned real money confirming correct usage (the Johnson canary
# judged 5,214 of them for ~zero errors). Instead, each pattern below fires
# only when the surrounding words suggest the WRONG member of the pair — the
# small-word slips a chunked model read glides straight past. Unsignaled
# occurrences generate nothing: correct usage is the overwhelming case and is
# not this lane's question.
_CONFUSABLE_SIGNALS: tuple[tuple[str, str], ...] = (
    # there/their/they're
    (r"\btheir\s+(?:is|are|was|were|will|might|may|seems?|comes?|goes?)\b",
     "their_used_as_there"),
    (r"\b(?:left|put|took|grabbed|packed|forgot|lost|placed|dropped|brought|"
     r"found|raised|shook|wagged|held|carried)\s+(?P<w>there)\s+[a-z]+",
     "there_used_as_their"),
    (r"\b(?P<w>there)\s+own\b", "there_used_as_their"),
    # your/you're
    (r"\b(?P<w>[Yy]our)\s+(?:\w+ing|a|an|the|not|so|too|very|welcome|right|"
     r"wrong|sure|done|going)\b", "your_used_as_youre"),
    (r"\b(?P<w>[Yy]ou're)\s+own\b", "youre_used_as_your"),
    # its/it's
    (r"\b(?P<w>[Ii]ts)\s+(?:a|an|the|been|not|no|so|too|very|just|only|all|"
     r"time|what|how|because)\b", "its_used_as_its_contraction"),
    (r"\b(?P<w>[Ii]t's)\s+own\b", "its_contraction_used_as_possessive"),
    (r"\b(?P<w>[Ii]t's)\s+(?!a\b|an\b|the\b|been\b|not\b|no\b|so\b|too\b|"
     r"very\b|just\b|only\b|all\b|what\b|how\b|why\b|where\b|who\b|when\b|"
     r"like\b|time\b|because\b|as\b|if\b|still\b|already\b|never\b|always\b|"
     r"almost\b|about\b|over\b|done\b|okay\b|ok\b|fine\b|true\b|hard\b|"
     r"easy\b|good\b|bad\b|better\b|worse\b|more\b|less\b|such\b|quite\b|"
     r"really\b|probably\b|certainly\b|clearly\b|me\b|you\b|him\b|her\b|"
     r"them\b|us\b|\w+ing\b|\w+ed\b|\w+ly\b)(?:[a-z]+)\b",
     "its_contraction_used_as_possessive"),
    # then/than
    (r"\b(?:more|less|rather|other|\w+er)\s+(?P<w>then)\b",
     "then_used_as_than"),
    (r"\b(?P<w>than)\s+(?:I|he|she|we|they|it)\s+(?:went|came|said|left|"
     r"turned|walked|ran)\b", "than_used_as_then"),
    # to/too
    (r"\b(?P<w>too)\s+(?:the|a|an|my|his|her|their|our|your|him|them|us|me)\b",
     "too_used_as_to"),
    (r"\b(?P<w>to)\s+(?:much|many|late|soon|far|long|often|big|small|hard|"
     r"easy|hot|cold|old|young|fast|slow|heavy|light|expensive|dangerous)\b",
     "to_used_as_too"),
    # loose/lose
    (r"\b(?P<w>loose)\s+(?:the|my|his|her|their|our|your|it|him|them|us|"
     r"track|sight|count|interest|control|touch|hope|weight|money)\b",
     "loose_used_as_lose"),
    # affect/effect
    (r"\b(?:an|the)\s+(?P<w>affect)s?\s+(?:of|on|was|is)\b",
     "affect_used_as_effect"),
    # passed/past
    (r"\b(?:walked|drove|ran|went|flew|moved|rushed|hurried|strolled|"
     r"marched|sailed|rode|slipped|brushed)\s+(?P<w>passed)\b",
     "passed_used_as_past"),
    (r"\bin\s+the\s+(?P<w>passed)\b", "passed_used_as_past"),
    # form/from (typo pair, same mechanism)
    (r"\b(?P<w>form)\s+(?:the|a|an|my|his|her|their|our|your|here|there|now|"
     r"him|them|us|me|it|this|that|these|those|where|which|what|whom|whose)\b",
     "form_used_as_from"),
    (r"\b(?P<w>from)\s+of\b", "from_used_as_form"),
)
_COMPILED_CONFUSABLES = tuple(
    (re.compile(pattern, re.IGNORECASE), reason)
    for pattern, reason in _CONFUSABLE_SIGNALS)

# P2-04: space before a comma/semicolon/colon/terminal mark is a deterministic
# error (period excluded — ellipsis and abbreviations make it ambiguous).
_SPACE_BEFORE_PUNCT = re.compile(r"(?P<span>\s+(?P<mark>[,;:!?]))")

# Compound sentences: two independent clauses joined by a coordinating
# conjunction conventionally take a comma before the conjunction. This is the
# comma class the chunked model passes demonstrably miss (the Redding analysis
# put it at the top of the detector gaps, and the shipped
# ``compound_sentence_comma`` error type has been inert). Pronoun subjects only
# — the high-precision core; "bread and butter" lists never fire.
_COMPOUND_JOIN = re.compile(
    r"(?P<pre>\w)(?P<comma>,)?(?P<sp>\s+)"
    r"(?P<conj>and|but|or|yet|so)\s+"
    r"(?P<subj>I|he|she|we|they|you|it)\s+(?P<verb>[a-z]\w+)\b")
_SENTENCE_TERMINAL = re.compile(r"[.!?]")

_ECHO_STOP = frozenset({
    "about", "after", "again", "also", "because", "before", "being",
    "could", "every", "first", "from", "have", "into", "just", "like",
    "more", "other", "over", "said", "some", "than", "that", "their",
    "there", "these", "they", "this", "through", "very", "were", "what",
    "when", "where", "which", "while", "with", "would", "your",
})
_LEGITIMATE_REPEATS = frozenset({"had", "that"})
_NUMBER_WORDS = {
    0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
}
_WRITTEN_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def generate_initial_candidates(
        doc: DocumentModel, paragraphs: Sequence[ParagraphRef] | None = None,
        *, candidate_types: Iterable[str] = INITIAL_CANDIDATE_TYPES,
        sweep_findings: Sequence[Finding] = (),
        finding_sources: "dict[str, Sequence[Finding]] | None" = None
        ) -> list[Candidate]:
    """Generate the initial rollout's candidates with no paid calls.

    ``finding_sources`` maps a candidate type to Findings from a reused
    deterministic analyzer (P2-01/02); each is adapted into a screened candidate
    of that type.
    """
    enabled = set(candidate_types)
    unknown = enabled - set(INITIAL_CANDIDATE_TYPES)
    if unknown:
        raise ValueError(f"unknown initial candidate type(s): {sorted(unknown)}")
    rows = list(paragraphs if paragraphs is not None else doc.paragraphs)
    out: list[Candidate] = []
    per_paragraph = {
        "dialogue_tag_punctuation": _dialogue_candidates,
        "quote_balance": _quote_candidates,
        "introductory_comma": _introductory_candidates,
        "direct_address_comma": _direct_address_candidates,
        "number_style": _number_candidates,
        "currency_style": _currency_candidates,
        "repeated_word": _repeated_word_candidates,
        "word_echo": _word_echo_candidates,
        "homophone": _homophone_candidates,
        "punctuation_style": _punctuation_style_candidates,
        "compound_sentence_comma": _compound_comma_candidates,
        "comma_boundary": _comma_boundary_candidates,
    }
    for candidate_type, generator in per_paragraph.items():
        if candidate_type in enabled:
            for para in rows:
                out.extend(generator(para))
    if "heading_sequence" in enabled:
        out.extend(_heading_candidates(rows))
    if "list_punctuation" in enabled:
        out.extend(_list_candidates(rows))
    out.extend(_candidates_from_existing_sweeps(
        doc, sweep_findings, enabled))
    for candidate_type, findings in (finding_sources or {}).items():
        out.extend(candidates_from_findings(
            doc, findings, candidate_type, enabled))
    return out


def _candidate(candidate_type: str, para: ParagraphRef, start: int, end: int,
               *, generator: str, observed: str | None = None,
               correction: str | None = None, evidence: dict | None = None,
               decision: str = "needs_model_judgment",
               reason_code: str = "ambiguous_local_signal",
               explanation: str = "Local evidence is not sufficient for a safe decision.",
               context_recipe: tuple[str, ...] = ("current sentence",
                                                  "current paragraph"),
               risk_prior: float = 0.5,
               meaning_change_risk: str = "low",
               channel: str = "either",
               extra_anchors: tuple[CandidateAnchor, ...] = ()) -> Candidate:
    anchor = CandidateAnchor(
        document_part=para.part, paragraph_id=para.para_id,
        start_offset=start, end_offset=end)
    anchors = (anchor,) + tuple(extra_anchors)
    evidence = dict(evidence or {})
    evidence["local_screening"] = {
        "decision": decision,
        "reason_code": reason_code,
        "explanation": explanation,
        "judge_id": generator,
    }
    candidate_id = deterministic_candidate_id(
        candidate_type, anchors, observed, correction)
    return Candidate(
        candidate_id=candidate_id, candidate_type=candidate_type,
        generator_id=generator, anchors=anchors, observed_text=observed,
        candidate_correction=correction, evidence=evidence,
        context_recipe=context_recipe, risk_prior=risk_prior,
        meaning_change_risk=meaning_change_risk,
        channel_preference=channel, status=CandidateStatus.GENERATED)


def _dialogue_candidates(para: ParagraphRef) -> list[Candidate]:
    out = []
    for match in _DIALOGUE_TAG.finditer(para.text):
        punct = match.group("punct")
        quote_start = match.start("quote")
        verb = match.group("verb").lower()
        dual = verb in _DUAL_USE_VERBS
        # An action beat: a dual-use verb taking a direct object ("continued his
        # search") is narration, not a speech tag — the period is correct.
        action_beat = dual and bool(
            _ACTION_OBJECT.match(para.text, match.end("verb")))
        channel = "edit"
        if punct == ".":
            start, end, observed = match.start("punct"), match.end("punct"), punct
            if action_beat:
                decision, correction = "pass", None
                reason = "dialogue_action_beat_keeps_period"
                explanation = ("A dual-use verb with a direct object is an "
                               "action beat; the period is correct.")
            elif dual:
                decision, correction = "needs_model_judgment", ","
                reason = "ambiguous_dialogue_or_action_beat"
                explanation = ("This verb can report speech or narrate an "
                               "action; whether the period should be a comma "
                               "depends on the sentence.")
            else:
                decision, correction = "error", ","
                reason = "period_before_dialogue_tag"
                explanation = "A continuing dialogue tag takes a comma here."
        elif not punct:
            start = end = quote_start
            observed = ""
            if dual:
                # Could be a missing comma (speech tag) or a missing period
                # (dialogue then action beat) — let the judge choose the mark.
                decision, correction = "needs_model_judgment", ","
                reason = "missing_punctuation_before_dialogue_or_beat"
                explanation = ("Punctuation is missing before the closing "
                               "quote; the mark depends on whether an action "
                               "beat or a speech tag follows.")
            else:
                decision, correction = "error", ","
                reason = "missing_punctuation_before_dialogue_tag"
                explanation = "The dialogue tag needs punctuation before the closing quote."
        else:
            decision, correction = "pass", None
            reason = "valid_dialogue_tag_boundary"
            explanation = "The dialogue-closing punctuation is locally valid."
            start, end, observed = match.start("punct"), match.end("punct"), punct
        out.append(_candidate(
            "dialogue_tag_punctuation", para, start, end,
            generator="candidate.dialogue_tag_boundary", observed=observed,
            correction=correction, decision=decision, reason_code=reason,
            explanation=explanation,
            evidence={"tag_subject": match.group("subject"),
                      "speech_verb": verb, "action_beat": action_beat},
            risk_prior=0.8 if decision == "error" else 0.1,
            channel=channel))
    return out


def _quote_candidates(para: ParagraphRef) -> list[Candidate]:
    straight = [m.start() for m in re.finditer(r'"', para.text)]
    opens = [m.start() for m in re.finditer("“", para.text)]
    closes = [m.start() for m in re.finditer("”", para.text)]
    positions = sorted(straight + opens + closes)
    if not positions:
        return []
    balanced = (len(straight) % 2 == 0 and len(opens) == len(closes))
    extra: tuple[CandidateAnchor, ...] = ()
    if not balanced:
        extra = (CandidateAnchor(
            document_part=para.part, paragraph_id=para.para_id,
            virtual_location="expected matching quotation mark at a "
                             "paragraph or dialogue boundary"),)
    return [_candidate(
        "quote_balance", para, positions[0], positions[-1] + 1,
        generator="candidate.quote_stack",
        observed=para.text[positions[0]:positions[-1] + 1],
        evidence={"straight_quotes": len(straight),
                  "opening_curly_quotes": len(opens),
                  "closing_curly_quotes": len(closes)},
        decision="pass" if balanced else "needs_model_judgment",
        reason_code=("balanced_quote_stack" if balanced
                     else "unmatched_quote_requires_context"),
        explanation=("Quotation delimiters are locally balanced."
                     if balanced else
                     "The paragraph has an unmatched quotation mark; "
                     "multi-paragraph dialogue must be considered."),
        context_recipe=("current paragraph", "previous paragraph",
                        "next paragraph"),
        risk_prior=0.15 if balanced else 0.75,
        meaning_change_risk="medium", channel="query",
        extra_anchors=extra)]


def _introductory_candidates(para: ParagraphRef) -> list[Candidate]:
    style = para.style.lower().replace(" ", "")
    if "list" in style or "bullet" in style or style.startswith("heading"):
        return []
    out = []
    for regex, mode in ((_STRONG_INTRO, "strong"),
                        (_AMBIGUOUS_INTRO, "ambiguous"),
                        (_CLAUSE_INTRO, "clause")):
        for match in regex.finditer(para.text):
            boundary = match.end("phrase")
            rest = para.text[boundary:boundary + 90]
            comma = re.search(",", rest)
            immediate = para.text[boundary:boundary + 1] == ","
            channel = "edit"
            if immediate:
                # A comma sits directly at the boundary — anchor it as observed
                # text and pass. Never propose inserting a second one.
                decision, correction = "pass", None
                reason = "introductory_boundary_has_comma"
                explanation = "The introductory expression is set off locally."
                start, end, observed = boundary, boundary + 1, ","
            elif mode == "strong":
                # A reliable transitional adverb ("However", "Therefore") takes
                # its comma immediately after the word — a safe insertion.
                decision, correction = "error", ","
                reason = "strong_introductory_expression_missing_comma"
                explanation = "This introductory expression conventionally takes a comma."
                start = end = boundary
                observed = ""
            elif mode == "ambiguous":
                # An opener that takes a comma as an interjection/ordinal but not
                # as a determiner or phrase head ("No, I won't" vs "No matter").
                # Never auto-insert — hand it to the judge with the sentence.
                decision, correction = "needs_model_judgment", ","
                reason = "ambiguous_introductory_expression"
                explanation = ("This opener is set off with a comma only as an "
                               "interjection or ordinal, not as a determiner or "
                               "the head of a phrase; the sentence decides.")
                start = end = boundary
                observed = ""
                channel = "edit"
            elif comma:
                # The introductory clause already carries a comma within its
                # span. Anchor that existing comma and pass — inserting another
                # produced the reported double-comma failure. We cannot prove it
                # is *this* clause's terminator without a parser, but a comma
                # present is sufficient local evidence not to add one.
                decision, correction = "pass", None
                reason = "introductory_clause_already_punctuated"
                explanation = "The introductory clause already has a comma; no insertion."
                comma_pos = boundary + comma.start()
                start, end, observed = comma_pos, comma_pos + 1, ","
            else:
                # No comma anywhere in the clause region: the true clause
                # boundary needs syntactic parsing to locate. Fail closed —
                # surface a non-editing query, never a wrong-location comma
                # after the conjunction. A parser-backed generator (P2-05) may
                # later place the edit.
                decision, correction = "needs_model_judgment", None
                reason = "introductory_clause_boundary_needs_parse"
                explanation = ("A possible missing introductory-clause comma; "
                               "the boundary needs parsing to place safely.")
                start = end = boundary
                observed = ""
                channel = "query"
            out.append(_candidate(
                "introductory_comma", para, start, end,
                generator="candidate.introductory_boundary", observed=observed,
                correction=correction, decision=decision, reason_code=reason,
                explanation=explanation,
                evidence={"introductory_expression": match.group("phrase"),
                          "comma_within_90_chars": bool(comma)},
                risk_prior=(0.75 if decision == "error"
                            else 0.4 if channel == "query" else 0.1),
                meaning_change_risk="medium" if channel == "query" else "low",
                channel=channel))
    return out


def _direct_address_candidates(para: ParagraphRef) -> list[Candidate]:
    out = []
    for match in _DIRECT_ADDRESS.finditer(para.text):
        # IGNORECASE applies to the lead phrase, but an apparent addressee must
        # still look like a name. Without this guard, ordinary "please wait"
        # would become a high-confidence punctuation error.
        if not match.group("name")[:1].isupper():
            continue
        boundary = match.end("lead")
        has_comma = para.text[boundary:boundary + 1] == ","
        start, end = ((boundary, boundary + 1) if has_comma
                      else (boundary, boundary))
        out.append(_candidate(
            "direct_address_comma", para, start, end,
            generator="candidate.direct_address_pattern",
            observed="," if has_comma else "",
            correction=None if has_comma else ",",
            decision="pass" if has_comma else "error",
            reason_code=("direct_address_is_set_off" if has_comma
                         else "direct_address_missing_comma"),
            explanation=("The apparent addressee is set off with a comma."
                         if has_comma else
                         "The greeting or imperative is followed by an apparent addressee."),
            evidence={"lead": match.group("lead"),
                      "possible_addressee": match.group("name")},
            risk_prior=0.8 if not has_comma else 0.1,
            meaning_change_risk="medium", channel="edit"))
    return out


def _number_candidates(para: ParagraphRef) -> list[Candidate]:
    out = []
    currency_spans = [match.span() for match in _CURRENCY.finditer(para.text)]
    for match in _NUMBER.finditer(para.text):
        if any(start <= match.start() and match.end() <= end
               for start, end in currency_spans):
            continue
        token = match.group(0)
        plain = token.replace(",", "")
        correction = None
        if plain.isdigit() and int(plain) in _NUMBER_WORDS:
            correction = _NUMBER_WORDS[int(plain)]
        year_like = plain.isdigit() and 1000 <= int(plain) <= 2099
        out.append(_candidate(
            "number_style", para, match.start(), match.end(),
            generator="candidate.every_numeral", observed=token,
            correction=correction,
            decision="pass" if year_like else "needs_model_judgment",
            reason_code=("year_like_numeral" if year_like
                         else "number_style_depends_on_usage"),
            explanation=("The numeral has a year-like value."
                         if year_like else
                         "House style depends on whether the numeral is a measurement, label, age, date, or prose number."),
            evidence={"numeric_token": token, "year_like": year_like},
            risk_prior=0.15 if year_like else 0.4,
            channel="either"))
    return out


def _currency_candidates(para: ParagraphRef) -> list[Candidate]:
    return [_candidate(
        "currency_style", para, match.start(), match.end(),
        generator="candidate.every_currency_amount", observed=match.group(0),
        decision="needs_model_judgment",
        reason_code="currency_style_requires_locale_and_house_style",
        explanation="Currency symbol, code, spacing, and precision require locale and context.",
        evidence={"currency_token": match.group(0)}, risk_prior=0.45,
        channel="either") for match in _CURRENCY.finditer(para.text)]


def _repeated_word_candidates(para: ParagraphRef) -> list[Candidate]:
    out = []
    for match in _REPEATED.finditer(para.text):
        word = match.group("first")
        legitimate = word.lower() in _LEGITIMATE_REPEATS
        out.append(_candidate(
            "repeated_word", para, match.start(), match.end(),
            generator="candidate.adjacent_word_repeat", observed=match.group(0),
            correction=word,
            decision="needs_model_judgment" if legitimate else "error",
            reason_code=("repeat_can_be_grammatical" if legitimate
                         else "accidental_adjacent_repeat"),
            explanation=("This repeated form can be grammatical and needs sentence context."
                         if legitimate else
                         "The same word appears twice with only whitespace between it."),
            evidence={"word": word, "gap": match.group("gap")},
            risk_prior=0.45 if legitimate else 0.98,
            channel="edit"))
    return out


def _word_echo_candidates(para: ParagraphRef) -> list[Candidate]:
    tokens = [(match.group(0), match.start(), match.end())
              for match in _WORD.finditer(para.text)]
    recent: dict[str, tuple[int, int, int]] = {}
    out = []
    for index, (word, start, end) in enumerate(tokens):
        key = word.lower().replace("’", "'")
        # ≥6 chars and a tight window: the wide net judged ~1,100 echoes on one
        # novel and surfaced almost nothing — short common words echo naturally.
        if len(key) < 6 or key in _ECHO_STOP:
            continue
        previous = recent.get(key)
        recent[key] = (index, start, end)
        if previous is None or not 2 <= index - previous[0] <= 8:
            continue
        first = CandidateAnchor(
            document_part=para.part, paragraph_id=para.para_id,
            start_offset=previous[1], end_offset=previous[2])
        out.append(_candidate(
            "word_echo", para, start, end,
            generator="candidate.local_word_echo", observed=word,
            decision="needs_model_judgment",
            reason_code="nearby_content_word_repetition",
            explanation="The same content word recurs within a short prose window.",
            evidence={"word": word, "token_distance": index - previous[0]},
            risk_prior=0.35, meaning_change_risk="medium", channel="query",
            extra_anchors=(first,)))
    return out


def _homophone_candidates(para: ParagraphRef) -> list[Candidate]:
    out = []
    seen: set[tuple[int, int]] = set()
    for regex, reason in _COMPILED_CONFUSABLES:
        for match in regex.finditer(para.text):
            group = "w" if "w" in (regex.groupindex or {}) else 0
            start, end = match.start(group), match.end(group)
            if (start, end) in seen:
                continue
            seen.add((start, end))
            word = para.text[start:end]
            out.append(_candidate(
                "homophone", para, start, end,
                generator="candidate.confusable_signal", observed=word,
                correction=None, decision="needs_model_judgment",
                reason_code="confusable_misuse_signal",
                explanation="The surrounding words suggest this may be the "
                            "wrong member of a commonly confused pair.",
                evidence={"word": word, "signal": reason,
                          "signal_window": para.text[max(0, start - 30):end + 30]},
                risk_prior=0.6, meaning_change_risk="medium", channel="either"))
    return out


def _compound_comma_candidates(para: ParagraphRef) -> list[Candidate]:
    out = []
    for match in _COMPOUND_JOIN.finditer(para.text):
        # The first clause must be a clause, not a fragment: require some
        # distance back to the previous sentence terminal (or paragraph start).
        terminals = [m.end() for m in _SENTENCE_TERMINAL.finditer(
            para.text, 0, match.start())]
        clause_start = terminals[-1] if terminals else 0
        if match.start("sp") - clause_start < 20:
            continue
        if match.group("comma"):
            start = match.start("comma")
            out.append(_candidate(
                "compound_sentence_comma", para, start, start + 1,
                generator="candidate.compound_join", observed=",",
                correction=None, decision="pass",
                reason_code="compound_join_has_comma",
                explanation="The compound sentence is set off before its "
                            "conjunction.",
                evidence={"conjunction": match.group("conj"),
                          "subject": match.group("subj")},
                risk_prior=0.1, channel="edit"))
        else:
            insert_at = match.start("sp")
            out.append(_candidate(
                "compound_sentence_comma", para, insert_at, insert_at,
                generator="candidate.compound_join", observed="",
                correction=",", decision="needs_model_judgment",
                reason_code="compound_join_missing_comma",
                explanation="Two clauses joined by a coordinating conjunction "
                            "conventionally take a comma; whether both sides "
                            "are independent needs the sentence.",
                evidence={"conjunction": match.group("conj"),
                          "subject": match.group("subj"),
                          "join_window": para.text[
                              max(0, insert_at - 40):insert_at + 40]},
                risk_prior=0.55, channel="edit"))
    return out


# Every comma-eligible seam, exhaustively. The Redding miss inventory is 73%
# comma-class edits, and the clever generators above locate only a sliver of
# them; this sweep trades rule precision for notice and hands the judge the
# whole decision. Four seam families, all needs_model_judgment:
#   join_missing  — clause join (FANBOYS or subordinator) with no comma before
#   join_present  — clause join WITH a comma (the removal direction)
#   loose_comma   — any other comma in running prose (removal direction)
#   opener_seam   — fronted opener with no comma before the likely main-clause
#                   subject; bare nonrestrictive who/which
_CB_CONJ = re.compile(
    r"(?P<pre>[\w’'])(?P<comma>,)?(?P<sp>\s+)"
    r"(?P<conj>and|but|or|nor|for|so|yet|as|because|when|if|after|before|"
    r"while|since|though|although|until|unless|whereas)\s+[\w“‘\"']",
    re.IGNORECASE)
_CB_RELATIVE = re.compile(r"(?P<pre>[\w’'])(?P<sp>\s+)(?P<rel>who|which)\s+\w")
_CB_OPENER = frozenset({
    "as", "because", "when", "if", "after", "before", "while", "since",
    "though", "although", "until", "unless", "whereas", "in", "at", "on",
    "with", "during", "despite", "throughout", "over", "under", "through",
    "from", "by", "for", "to", "once", "yes", "no", "well", "oh", "now",
    "so", "first", "second", "finally", "however", "instead", "sometimes",
    "often", "usually", "eventually", "someday", "today", "tomorrow",
    "yesterday", "growing", "being", "having", "looking", "trying",
})
_CB_SUBJECT = frozenset({
    "i", "you", "he", "she", "it", "we", "they", "there", "the", "a", "an",
    "my", "your", "his", "her", "our", "their", "this", "these", "those",
})
_CB_PRONOUN = frozenset({"i", "he", "she", "we", "they", "there", "you", "it",
               "that", "this"})
# Predecessors after which a pronoun is routinely an object or a licensed
# clause continuation, not a bare seam: conjunctions and subordinators (the
# join seams own those), complementizers, and the verbs/prepositions that most
# commonly take a pronoun straight after them.
_CB_NO_SEAM = frozenset({
    "and", "but", "or", "nor", "for", "so", "yet", "as", "because", "when",
    "if", "after", "before", "while", "since", "though", "although", "until",
    "unless", "whereas", "that", "which", "who", "whom", "than", "whether",
    "where", "why", "how", "what", "to", "of", "with", "at", "on", "in",
    "by", "from", "about", "like", "let", "lets", "make", "makes", "made",
    "making", "give", "gave", "gives", "giving", "tell", "told", "tells",
    "telling", "say", "said", "says", "saying", "ask", "asked", "asks",
    "want", "wants", "wanted", "need", "needs", "needed", "love", "loves",
    "loved", "thank", "think", "thought", "know", "knew", "knows", "believe",
    "hope", "wish", "mean", "means", "meant", "help", "helps", "helped",
    "watch", "watched", "see", "saw", "hear", "heard", "do", "did", "does",
    "am", "is", "are", "was", "were", "be", "been", "being",
})
_CB_WINDOW = 32


def _cb_window(text: str, start: int, end: int) -> str:
    return text[max(0, start - _CB_WINDOW):end + _CB_WINDOW]


def _comma_boundary_candidates(para: ParagraphRef) -> list[Candidate]:
    text = para.text
    out: list[Candidate] = []
    claimed_commas: set[int] = set()

    def seam(kind: str, start: int, end: int, *, observed, correction,
             reason: str, explanation: str, extra: dict) -> None:
        evidence = dict(extra)
        evidence["seam"] = kind
        evidence["window"] = _cb_window(text, start, end)
        out.append(_candidate(
            "comma_boundary", para, start, end,
            generator="candidate.comma_boundary", observed=observed,
            correction=correction, decision="needs_model_judgment",
            reason_code=reason, explanation=explanation, evidence=evidence,
            risk_prior=0.4, channel="edit"))

    sentence_starts = [0] + [m.end() for m in _SENTENCE_TERMINAL.finditer(text)]

    for match in _CB_CONJ.finditer(text):
        conj = match.group("conj").lower()
        # Mid-sentence only: a couple of words must precede the join.
        clause_start = max(s for s in sentence_starts if s <= match.start())
        if match.start("sp") - clause_start < 8:
            continue
        if match.group("comma"):
            comma_at = match.start("comma")
            claimed_commas.add(comma_at)
            seam("join_present", comma_at, comma_at + 1, observed=",",
                 correction=None, reason="comma_present_at_clause_join",
                 explanation="A comma sits before this conjunction; whether "
                             "the join earns one needs the sentence.",
                 extra={"conjunction": conj})
        else:
            insert_at = match.start("sp")
            seam("join_missing", insert_at, insert_at, observed="",
                 correction=",", reason="no_comma_at_clause_join",
                 explanation="No comma before this conjunction; whether the "
                             "join needs one depends on the clauses.",
                 extra={"conjunction": conj})

    for match in _CB_RELATIVE.finditer(text):
        insert_at = match.start("sp")
        seam("opener_seam", insert_at, insert_at, observed="",
             correction=",", reason="bare_relative_clause",
             explanation="A who/which clause with no comma; nonrestrictive "
                         "readings take one.",
             extra={"relative": match.group("rel").lower()})

    # Fronted opener: sentence starts on an opener cue, carries no comma yet,
    # and a plausible main-clause subject appears a few words in — the seam at
    # the right edge of the word before that subject is a comma question (the
    # comma lands on the preceding word, so the anchor must too). One per
    # sentence, plus the transition/interjection comma straight after a
    # single-word opener.
    for index, s_start in enumerate(sentence_starts):
        s_end = (sentence_starts[index + 1]
                 if index + 1 < len(sentence_starts) else len(text))
        tokens = [(m.group(0), m.start(), m.end())
                  for m in _WORD.finditer(text, s_start, s_end)]
        if len(tokens) < 4:
            continue
        first = tokens[0][0].lower()
        opener = first in _CB_OPENER or first.endswith(("ly", "ing"))
        if opener:
            seam("opener_seam", tokens[0][2], tokens[0][2], observed="",
                 correction=",", reason="transition_opener_no_comma",
                 explanation="A transition or interjection opener often takes "
                             "a comma straight after it.",
                 extra={"opener": first})
            for word, w_start, w_end in tokens[2:10]:
                if "," in text[s_start:w_start]:
                    break
                if word.lower() in _CB_SUBJECT:
                    seam("opener_seam", w_start - 1, w_start - 1, observed="",
                         correction=",", reason="fronted_opener_no_comma",
                         explanation="The sentence opens on fronted matter "
                                     "with no comma before the likely main "
                                     "clause.",
                         extra={"opener": first, "subject": word.lower()})
                    break
        # Mid-sentence bare-clause seam: a subject pronoun with no comma or
        # conjunction in front of it deep in the sentence is where a clause
        # boundary may want a comma ("I had a good friend they would move").
        for word, w_start, w_end in tokens[3:]:
            if word.lower() not in _CB_PRONOUN:
                continue
            gap = text[max(s_start, w_start - 24):w_start]
            if "," in gap:
                continue
            prev = re.findall(r"[\w’']+", gap)
            if not prev or prev[-1].lower() in _CB_NO_SEAM:
                continue
            seam("clause_seam", w_start - 1, w_start - 1, observed="",
                 correction=",", reason="bare_pronoun_clause_seam",
                 explanation="A pronoun opens what may be a new clause with "
                             "no comma before it.",
                 extra={"subject": word.lower()})

    # Every remaining comma in running prose is a removal question.
    for pos, char in enumerate(text):
        if char != "," or pos in claimed_commas:
            continue
        # Skip list-like environments the list/number generators own: a comma
        # directly between digits, and a comma before a closing quote.
        before = text[pos - 1] if pos else ""
        after = text[pos + 1:pos + 2]
        if before.isdigit() and text[pos + 1:pos + 4].strip()[:1].isdigit():
            continue
        if after in "”’\"'":
            continue
        seam("loose_comma", pos, pos + 1, observed=",", correction=None,
             reason="comma_in_running_prose",
             explanation="Whether this comma belongs is the judge's call.",
             extra={})
    return out


def _punctuation_style_candidates(para: ParagraphRef) -> list[Candidate]:
    out = []
    # Space before a comma/semicolon/colon/terminal mark — a safe deletion.
    for match in _SPACE_BEFORE_PUNCT.finditer(para.text):
        mark = match.group("mark")
        out.append(_candidate(
            "punctuation_style", para, match.start("span"), match.end("span"),
            generator="candidate.space_before_punctuation",
            observed=match.group("span"), correction=mark, decision="error",
            reason_code="space_before_punctuation",
            explanation="A space precedes this punctuation mark.",
            evidence={"mark": mark}, risk_prior=0.9, channel="edit"))
    # Parenthesis / bracket balance — a query anchored at the lone mark.
    for opener, closer, name in (("(", ")", "parenthesis"),
                                 ("[", "]", "bracket")):
        opens = [m.start() for m in re.finditer(re.escape(opener), para.text)]
        closes = [m.start() for m in re.finditer(re.escape(closer), para.text)]
        if len(opens) == len(closes):
            continue
        lone = (opens + closes)
        at = min(lone) if len(opens) > len(closes) else max(lone)
        out.append(_candidate(
            "punctuation_style", para, at, at + 1,
            generator="candidate.bracket_balance",
            observed=para.text[at:at + 1], correction=None,
            decision="needs_model_judgment",
            reason_code=f"unbalanced_{name}",
            explanation=f"The {name}es in this paragraph are unbalanced.",
            evidence={"opens": len(opens), "closes": len(closes)},
            risk_prior=0.7, meaning_change_risk="medium", channel="query"))
    return out


def _heading_candidates(paragraphs: Sequence[ParagraphRef]) -> list[Candidate]:
    headings = [para for para in paragraphs
                if para.style.lower().replace(" ", "").startswith("heading")]
    out = []
    previous: ParagraphRef | None = None
    previous_number: int | None = None
    previous_level: int | None = None
    for para in headings:
        level_match = re.search(r"(\d+)", para.style)
        level = int(level_match.group(1)) if level_match else 1
        number = _heading_number(para.text)
        if previous is not None:
            bad_level = (previous_level is not None and level > previous_level + 1)
            bad_number = (previous_number is not None and number is not None
                          and number != previous_number + 1)
            decision = "error" if bad_level or bad_number else "pass"
            correction = None
            if bad_number and previous_number is not None:
                correction = _replace_heading_number(
                    para.text, previous_number + 1)
            first = CandidateAnchor(
                document_part=previous.part, paragraph_id=previous.para_id,
                start_offset=0, end_offset=len(previous.text))
            out.append(_candidate(
                "heading_sequence", para, 0, len(para.text),
                generator="candidate.heading_sequence", observed=para.text,
                correction=correction, decision=decision,
                reason_code=("heading_sequence_break" if decision == "error"
                             else "heading_sequence_continues"),
                explanation=("The heading level or explicit number skips the preceding sequence."
                             if decision == "error" else
                             "The heading follows the preceding level and number sequence."),
                evidence={"previous_heading": previous.text,
                          "previous_level": previous_level,
                          "current_level": level,
                          "previous_number": previous_number,
                          "current_number": number,
                          "level_jump": bad_level,
                          "number_jump": bad_number},
                context_recipe=("heading hierarchy", "current paragraph"),
                risk_prior=0.9 if decision == "error" else 0.1,
                meaning_change_risk="high", channel="query",
                extra_anchors=(first,)))
        previous, previous_number, previous_level = para, number, level
    return out


def _heading_number(text: str) -> int | None:
    match = re.search(
        r"\b(?:chapter|part|book|section)?\s*"
        r"(?P<number>\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
        text, re.IGNORECASE)
    if not match:
        return None
    value = match.group("number").lower()
    return int(value) if value.isdigit() else _WRITTEN_NUMBERS.get(value)


def _replace_heading_number(text: str, number: int) -> str | None:
    match = re.search(r"\b\d+\b", text)
    if not match:
        return None
    return text[:match.start()] + str(number) + text[match.end():]


def _is_list_paragraph(para: ParagraphRef) -> bool:
    style = para.style.lower().replace(" ", "")
    return ("list" in style or "bullet" in style
            or bool(re.match(r"^\s*(?:[-•*]|\d+[.)])\s+", para.text)))


def _list_candidates(paragraphs: Sequence[ParagraphRef]) -> list[Candidate]:
    groups: list[list[ParagraphRef]] = []
    current: list[ParagraphRef] = []
    for para in paragraphs:
        if _is_list_paragraph(para):
            current.append(para)
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    out = []
    for group_index, group in enumerate(groups):
        endings = [para.text.rstrip()[-1:] for para in group]
        classes = [ending if ending in ".;,:!?" else "none"
                   for ending in endings]
        majority = Counter(classes).most_common(1)[0][0]
        consistent = len(set(classes)) == 1
        for item_index, (para, ending, klass) in enumerate(
                zip(group, endings, classes)):
            stripped = para.text.rstrip()
            if ending and ending in ".;,:!?":
                start, end, observed = len(stripped) - 1, len(stripped), ending
            else:
                start = end = len(stripped)
                observed = ""
            correction = None if consistent or klass == majority else (
                "" if majority == "none" else majority)
            out.append(_candidate(
                "list_punctuation", para, start, end,
                generator="candidate.list_terminal_consistency",
                observed=observed, correction=correction,
                decision="pass" if consistent else "needs_model_judgment",
                reason_code=("list_endings_consistent" if consistent
                             else "list_endings_inconsistent"),
                explanation=("The list items use a consistent terminal style."
                             if consistent else
                             "List-item terminal punctuation differs within the same run."),
                evidence={"group_index": group_index,
                          "item_index": item_index,
                          "ending_classes": classes,
                          "majority_ending": majority},
                context_recipe=("current paragraph", "previous paragraph",
                                "next paragraph"),
                risk_prior=0.15 if consistent else 0.65,
                channel="either"))
    return out


_SWEEP_CANDIDATE_TYPES = {
    "sweep_dialogue_tag": "dialogue_tag_punctuation",
    "sweep_doubled_word": "repeated_word",
    "sweep_compound_number": "number_style",
    "sweep_century": "number_style",
    "unclosed_quote": "quote_balance",
    # P2-04: the deterministic punctuation ERROR sweeps become candidates
    # instead of bypassing the ledger. The ellipsis/dash sweeps are style
    # normalization and stay out of this lane on purpose.
    "sweep_stacked_punctuation": "punctuation_style",
    "sweep_terminal_period": "punctuation_style",
    "sweep_quote_punctuation": "punctuation_style",
    "sweep_nested_quote": "punctuation_style",
}


def _candidate_from_finding(finding: Finding, doc: DocumentModel,
                            candidate_type: str, by_id) -> Candidate | None:
    """Convert one deterministic Finding into a screened candidate, preserving
    provenance. A confirmed edit becomes an ``error`` with the shrunk minimal
    correction; a query becomes ``needs_model_judgment``."""
    from .site_generators import site_from_finding
    from .validator import shrink

    site = site_from_finding(finding, doc)
    para = by_id.get(finding.para_id)
    if site is None or para is None:
        return None
    anchor = site.anchors[0]
    if anchor.start_offset is None or anchor.end_offset is None:
        return None
    observed = para.text[anchor.start_offset:anchor.end_offset]
    correction = None
    decision = "needs_model_judgment"
    reason = "existing_check_query_requires_context"
    explanation = "An existing deterministic check raised a reviewer query."
    if finding.corrected_text != finding.original_text and not finding.force_query:
        _prefix, _deleted, correction = shrink(
            finding.original_text, finding.corrected_text)
        decision = "error"
        reason = "existing_check_confirmed_error"
        explanation = "An existing deterministic check confirmed this exact change."
    return _candidate(
        candidate_type, para, anchor.start_offset, anchor.end_offset,
        generator=f"candidate.adapter.{finding.error_type}",
        observed=observed, correction=correction,
        decision=decision, reason_code=reason, explanation=explanation,
        evidence={"finding_id": finding.finding_id,
                  "source": finding.error_type,
                  "legacy_explanation": finding.explanation},
        risk_prior=0.99 if decision == "error" else 0.7,
        meaning_change_risk=(
            "medium" if candidate_type in {"quote_balance", "term_consistency"}
            else "low"),
        channel=("query" if finding.force_query or correction is None
                 else "edit"))


def _candidates_from_existing_sweeps(
        doc: DocumentModel, findings: Sequence[Finding], enabled: set[str]
        ) -> list[Candidate]:
    """Expose mature sweep hits as candidates without changing sweep output."""
    by_id = index_paragraphs(doc)
    out = []
    for finding in findings:
        candidate_type = _SWEEP_CANDIDATE_TYPES.get(finding.error_type)
        if candidate_type is None or candidate_type not in enabled:
            continue
        candidate = _candidate_from_finding(finding, doc, candidate_type, by_id)
        if candidate is not None:
            out.append(candidate)
    return out


def candidates_from_findings(
        doc: DocumentModel, findings: Sequence[Finding], candidate_type: str,
        enabled: set[str]) -> list[Candidate]:
    """Adapt an analyzer's Findings into candidates of one explicit type
    (P2-01/02): the analyzer output is screened through the ledger rather than
    emitted straight to the document."""
    if candidate_type not in enabled:
        return []
    by_id = index_paragraphs(doc)
    out = []
    for finding in findings:
        candidate = _candidate_from_finding(finding, doc, candidate_type, by_id)
        if candidate is not None:
            out.append(candidate)
    return out
