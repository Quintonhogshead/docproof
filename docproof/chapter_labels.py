"""Chapter and part LABELS are mechanics, not facts — fix them.

Quinton, 2026-09-04: "When Galley encounters chapter title inconsistencies,
she needs to just fix them." A numbering gap (Fifteen, Seventeen, …), a
mangled number ("Chapter Twenty-Thirty"), a style drift ("PART 3" beside
"PART ONE", "chapter 4" beside "Chapter Four") is a label out of step with
its neighbours, and the right answer is deterministic: renumber downstream
so the sequence is continuous, match the dominant style, ship the changes
as tracked heading edits noted once in the letter — never author queries.

Everything else in the pipeline treats a changed number as a FACT that
only the author can settle (flights.fact_change; settle's `fact:` reason).
That is right for a count in a sentence and wrong for a label, so this
module is both the detector and the exemption: `is_chapter_label` tells the
fact guards to stand down on a label paragraph, and `renumber_rows` emits
the import-findings rows a practitioner would otherwise hand-script.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

# A label line: "Chapter Seventeen", "CHAPTER 17", "Part III", "Chapter
# Twenty-One: The Storm", "Chapter Twenty-Thirty". Anchored at the start;
# the number token is everything up to a separator or the end.
LABEL_RE = re.compile(
    r"^\s*(?P<kind>chapter|part|book)\s+(?P<num>[A-Za-z]+(?:[- ][A-Za-z]+)?|"
    r"\d{1,3}|[IVXLCivxlc]{1,7})\b(?P<sep>\s*[:.\-—–]?)(?P<rest>.*)$",
    re.IGNORECASE)
# A label is a heading-sized line, not a sentence that happens to begin with
# "Chapter three of the report says…".
MAX_LABEL_CHARS = 80

_UNITS = ["zero", "one", "two", "three", "four", "five", "six", "seven",
          "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
          "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}


def parse_number(token: str) -> int | None:
    """15 for "15", "fifteen", "XV"; 21 for "twenty-one" / "twenty one";
    None for anything that is not one number ("Twenty-Thirty")."""
    t = token.strip().lower().replace(" ", "-")
    if t.isdigit():
        return int(t)
    if t in _UNITS:
        return _UNITS.index(t)
    if t in _TENS:
        return _TENS[t]
    if "-" in t:
        a, b = t.split("-", 1)
        if a in _TENS and b in _UNITS and 0 < _UNITS.index(b) < 10:
            return _TENS[a] + _UNITS.index(b)
        return None
    if re.fullmatch(r"[ivxlc]+", t):
        total, prev = 0, 0
        for ch in reversed(t):
            val = _ROMAN[ch]
            total += -val if val < prev else val
            prev = max(prev, val)
        return total if total > 0 else None
    return None


def spell_number(n: int) -> str:
    """"twenty-one" — the house spelled form, lowercase; the caller cases it."""
    if 0 <= n < 20:
        return _UNITS[n]
    if n < 100:
        tens, unit = divmod(n, 10)
        word = {v: k for k, v in _TENS.items()}[tens * 10]
        return word if unit == 0 else f"{word}-{_UNITS[unit]}"
    return str(n)


def _roman(n: int) -> str:
    out = ""
    for val, sym in ((100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"),
                     (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= val:
            out += sym
            n -= val
    return out


def _form_of(token: str) -> str:
    t = token.strip()
    if t.isdigit():
        return "numeral"
    if re.fullmatch(r"[IVXLC]+", t) or re.fullmatch(r"[ivxlc]+", t):
        return "roman"
    return "spelled"


def _case_of(kind_word: str, token: str) -> str:
    sample = kind_word + token
    if sample.isupper():
        return "upper"
    if sample.islower():
        return "lower"
    return "title"


def _cased(text: str, case: str) -> str:
    if case == "upper":
        return text.upper()
    if case == "lower":
        return text.lower()
    return "-".join(w[:1].upper() + w[1:] for w in text.split("-"))


@dataclass(frozen=True)
class Label:
    para_id: str
    kind: str            # chapter | part | book (lowercase)
    text: str            # the paragraph's text
    kind_word: str       # as written ("CHAPTER")
    number_token: str    # as written ("Twenty-Thirty")
    number: int | None   # parsed, None when it is not one number
    form: str            # numeral | spelled | roman
    case: str            # upper | title | lower
    sep: str
    rest: str

    @property
    def label_text(self) -> str:
        """The label span itself — kind word plus number — as written."""
        return f"{self.kind_word} {self.number_token}"


def is_chapter_label(text: str) -> bool:
    """Whether a paragraph IS a chapter/part label line (the fact guards
    stand down on it)."""
    if not text or len(text.strip()) > MAX_LABEL_CHARS:
        return False
    m = LABEL_RE.match(text)
    if not m:
        return False
    rest = m.group("rest").strip()
    # "Chapter three of the report says" is prose; a label's remainder is a
    # short title, not a clause with a verb — bound it by length.
    return len(rest) <= 60


def label_map(paragraphs: Sequence[Any] | Mapping[str, str]) -> list[Label]:
    """Every chapter/part label in document order. Accepts ParagraphRefs (or
    anything with para_id/text) or a {para_id: text} mapping."""
    items = (list(paragraphs.items()) if isinstance(paragraphs, Mapping)
             else [(p.para_id, p.text) for p in paragraphs])
    out: list[Label] = []
    for pid, text in items:
        if not is_chapter_label(text):
            continue
        m = LABEL_RE.match(text)
        assert m is not None
        kind_word, token = m.group("kind"), m.group("num")
        out.append(Label(para_id=str(pid), kind=kind_word.lower(), text=text,
                         kind_word=kind_word, number_token=token,
                         number=parse_number(token), form=_form_of(token),
                         case=_case_of(kind_word, token), sep=m.group("sep"),
                         rest=m.group("rest")))
    return out


def dominant_style(labels: Sequence[Label]) -> tuple[str, str]:
    """(form, case) most of the labels use; ties go to spelled/title, the
    house default for a chapter line."""
    from collections import Counter
    if not labels:
        return "spelled", "title"
    forms = Counter(lb.form for lb in labels)
    cases = Counter(lb.case for lb in labels)
    form = max(forms, key=lambda f: (forms[f], f == "spelled"))
    case = max(cases, key=lambda c: (cases[c], c == "title"))
    return form, case


def render(kind_word: str, n: int, form: str, case: str) -> str:
    """"Chapter Twenty-One" / "CHAPTER 21" / "Part III" in the given style."""
    if form == "numeral":
        num = str(n)
    elif form == "roman":
        num = _roman(n)
        num = num if case != "lower" else num.lower()
    else:
        num = _cased(spell_number(n), case)
    return f"{_cased(kind_word.lower(), case)} {num}"


def renumber_rows(labels: Sequence[Label], *,
                  error_type: str = "chapter_label") -> list[dict[str, Any]]:
    """The import-findings rows that make every kind's sequence continuous
    in the dominant style. Per kind, the sequence starts at the first
    label's own number (a book whose chapters begin at Fifteen after a Part
    break keeps starting there) — or 1 when the first is unreadable — and
    counts up by one in document order; a label whose number or style
    differs gets one row replacing the label span only (a title after the
    separator is untouched). Georgis: Fifteen, Seventeen, Eighteen,
    Nineteen, Twenty-One, "Twenty-Thirty" -> 15, 16, 17, 18, 19, 20."""
    rows: list[dict[str, Any]] = []
    by_kind: dict[str, list[Label]] = {}
    for lb in labels:
        by_kind.setdefault(lb.kind, []).append(lb)
    for kind, seq in by_kind.items():
        form, case = dominant_style(seq)
        start = seq[0].number if seq[0].number is not None else 1
        for i, lb in enumerate(seq):
            expected = start + i
            wanted = render(lb.kind_word, expected, form, case)
            if lb.label_text == wanted:
                continue
            why = []
            if lb.number != expected:
                why.append(f"numbered {lb.number_token!r} where the sequence "
                           f"reaches {expected}")
            if (lb.form, lb.case) != (form, case):
                why.append(f"styled {lb.form}/{lb.case} where the book's "
                           f"{kind} labels are {form}/{case}")
            rows.append({
                "para_id": lb.para_id, "original_text": lb.label_text,
                "occurrence": 1, "corrected_text": wanted,
                "error_type": error_type, "confidence": "high",
                "explanation": (f"{kind.capitalize()} label {'; '.join(why)}"
                                f" — labels are mechanics: renumbered to keep "
                                f"the sequence continuous in the dominant "
                                f"style (noted once in the letter)."),
            })
    return rows


__all__ = ["LABEL_RE", "Label", "dominant_style", "is_chapter_label",
           "label_map", "parse_number", "render", "renumber_rows",
           "spell_number"]
