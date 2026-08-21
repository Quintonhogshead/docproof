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

_SPEECH_VERBS = (
    "said", "asked", "answered", "replied", "whispered", "shouted",
    "yelled", "murmured", "cried", "called", "added", "continued",
    "remarked", "observed", "began", "stammered", "muttered",
)
_DIALOGUE_TAG = re.compile(
    r"(?P<punct>[,.!?]?)(?P<quote>[”\"])(?P<space>\s+)"
    r"(?P<subject>he|she|they|we|i|[A-Z][a-z]+)\s+"
    rf"(?P<verb>{'|'.join(_SPEECH_VERBS)})\b")

_STRONG_INTRO = re.compile(
    r"(?P<prefix>(?:^|(?<=[.!?]\s)))"
    r"(?P<phrase>However|Therefore|Meanwhile|Instead|Finally|"
    r"First|Second|Third|Yes|No|Well|Of course|For example|In fact)\b",
    re.IGNORECASE)
_CLAUSE_INTRO = re.compile(
    r"(?P<prefix>(?:^|(?<=[.!?]\s)))"
    r"(?P<phrase>After|Before|When|While|If|Although|Because|Since|"
    r"Once|Unless|Until|Whenever|Whereas)\b", re.IGNORECASE)

_DIRECT_ADDRESS = re.compile(
    r"(?P<lead>\b(?:hello|hi|hey|goodbye|please|listen|look|wait|"
    r"come on|thank you|thanks))(?P<gap>\s+)(?P<name>[A-Z][a-z]+)\b",
    re.IGNORECASE)

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
        sweep_findings: Sequence[Finding] = ()
        ) -> list[Candidate]:
    """Generate the initial rollout's candidates with no paid calls."""
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
        if punct == ".":
            decision, correction = "error", ","
            reason = "period_before_dialogue_tag"
            explanation = "A continuing dialogue tag takes a comma here."
            start, end, observed = match.start("punct"), match.end("punct"), punct
        elif not punct:
            decision, correction = "error", ","
            reason = "missing_punctuation_before_dialogue_tag"
            explanation = "The dialogue tag needs punctuation before the closing quote."
            start = end = quote_start
            observed = ""
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
                      "speech_verb": match.group("verb")},
            risk_prior=0.8 if decision == "error" else 0.1,
            channel="edit"))
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
    for regex, strong in ((_STRONG_INTRO, True), (_CLAUSE_INTRO, False)):
        for match in regex.finditer(para.text):
            boundary = match.end("phrase")
            rest = para.text[boundary:boundary + 90]
            comma = re.search(",", rest)
            immediate = para.text[boundary:boundary + 1] == ","
            if immediate:
                decision, correction = "pass", None
                reason = "introductory_boundary_has_comma"
                explanation = "The introductory expression is set off locally."
                start, end, observed = boundary, boundary + 1, ","
            elif strong:
                decision, correction = "error", ","
                reason = "strong_introductory_expression_missing_comma"
                explanation = "This introductory expression conventionally takes a comma."
                start = end = boundary
                observed = ""
            else:
                decision, correction = "needs_model_judgment", ","
                reason = "introductory_clause_boundary_ambiguous"
                explanation = "Clause length and attachment determine whether a comma is needed."
                start = end = (boundary + comma.start()) if comma else boundary
                observed = ""
            out.append(_candidate(
                "introductory_comma", para, start, end,
                generator="candidate.introductory_boundary", observed=observed,
                correction=correction, decision=decision, reason_code=reason,
                explanation=explanation,
                evidence={"introductory_expression": match.group("phrase"),
                          "comma_within_90_chars": bool(comma)},
                risk_prior=0.75 if decision == "error" else 0.45,
                channel="edit"))
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
        if len(key) < 4 or key in _ECHO_STOP:
            continue
        previous = recent.get(key)
        recent[key] = (index, start, end)
        if previous is None or not 2 <= index - previous[0] <= 12:
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
}


def _candidates_from_existing_sweeps(
        doc: DocumentModel, findings: Sequence[Finding], enabled: set[str]
        ) -> list[Candidate]:
    """Expose mature sweep hits as candidates without changing sweep output."""
    from .site_generators import site_from_finding
    from .validator import shrink

    by_id = index_paragraphs(doc)
    out = []
    for finding in findings:
        candidate_type = _SWEEP_CANDIDATE_TYPES.get(finding.error_type)
        if candidate_type not in enabled:
            continue
        site = site_from_finding(finding, doc)
        para = by_id.get(finding.para_id)
        if site is None or para is None:
            continue
        anchor = site.anchors[0]
        if anchor.start_offset is None or anchor.end_offset is None:
            continue
        observed = para.text[anchor.start_offset:anchor.end_offset]
        correction = None
        decision = "needs_model_judgment"
        reason = "existing_sweep_query_requires_context"
        explanation = "An existing deterministic check raised a reviewer query."
        if finding.corrected_text != finding.original_text:
            _prefix, _deleted, correction = shrink(
                finding.original_text, finding.corrected_text)
            decision = "error"
            reason = "existing_sweep_confirmed_error"
            explanation = "An existing deterministic sweep confirmed this exact change."
        out.append(_candidate(
            candidate_type, para, anchor.start_offset, anchor.end_offset,
            generator=f"candidate.adapter.{finding.error_type}",
            observed=observed, correction=correction,
            decision=decision, reason_code=reason, explanation=explanation,
            evidence={"finding_id": finding.finding_id,
                      "sweep": finding.error_type,
                      "legacy_explanation": finding.explanation},
            risk_prior=0.99 if decision == "error" else 0.7,
            meaning_change_risk=("medium" if candidate_type == "quote_balance"
                                 else "low"),
            channel=("query" if finding.force_query else "edit")))
    return out
