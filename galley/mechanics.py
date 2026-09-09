"""Meaning-preserving surface equivalences used by settlement guards.

These recognize proposed corrections; they do not rewrite arbitrary prose.
Unrecognized grammatical changes go to the internal editorial judge.
"""
from __future__ import annotations

import re

_SMALL = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split()
_TENS = 'zero ten twenty thirty forty fifty sixty seventy eighty ninety'.split()
_NUMBERS = {word: str(i) for i, word in enumerate(_SMALL)}
for _i in range(20, 100):
    _NUMBERS[_TENS[_i // 10] + ('-' + _SMALL[_i % 10] if _i % 10 else '')] = str(_i)
_NUMBERS['one hundred'] = '100'
_NUMBER_RE = re.compile(r'\b(?:' + '|'.join(
    re.escape(k).replace(r'\-', '[- ]') for k in sorted(_NUMBERS, key=len, reverse=True)
) + r')\b', re.I)


def canonical_surface(text: str) -> str:
    """Normalize presentation without discarding number values or word order."""
    text = text.replace('’', "'").replace('‘', "'")
    text = re.sub(r'\b([ap])\.m\.', lambda m: m[1].upper() + 'M', text, flags=re.I)
    text = re.sub(r'\bWIFI\b|\bWi-Fi\b', 'wifi', text, flags=re.I)
    text = _NUMBER_RE.sub(lambda m: _NUMBERS.get(m[0].lower(),
                         _NUMBERS.get(m[0].lower().replace(' ', '-'), m[0])), text)
    text = re.sub(r'(?<=\d),(?=\d{3}\b)', '', text)
    # 7 PM and 7:00 PM express the same time; never invent a meridiem.
    text = re.sub(r'\b(\d{1,2}):00(?=\s*[AP]M\b)', r'\1', text, flags=re.I)
    return text


def equivalent_surface(before: str, after: str) -> bool:
    return canonical_surface(before).casefold() == canonical_surface(after).casefold()


# Inflections whose spelling does not share the three-letter stem used by
# the spelling guard. Direction is decided by the proofreader in context.
_INFLECTIONS = (
    'sink sinks sank sunk sinking', 'say says said saying',
    'go goes went gone going', 'do does did done doing',
    'be am is are was were been being', 'have has had having',
    'lie lies lay lain lying', 'lay lays laid laying',
    'run runs ran running', 'write writes wrote written writing',
    'take takes took taken taking', 'come comes came coming',
    'see sees saw seen seeing', 'eat eats ate eaten eating',
    'rise rises rose risen rising', 'begin begins began begun beginning',
    'give gives gave given giving', 'know knows knew known knowing',
    'fall falls fell fallen falling', 'speak speaks spoke spoken speaking',
    'break breaks broke broken breaking', 'choose chooses chose chosen choosing',
)
_FAMILIES = [frozenset(s.split()) for s in _INFLECTIONS]


def same_inflection(a: str, b: str) -> bool:
    return any(a.lower() in family and b.lower() in family for family in _FAMILIES)


def abbreviation_fixes(text: str):
    """Known missing abbreviation periods, as exact (start, end, replacement).

    Already punctuated abbreviations cannot match, making retries idempotent.
    Word boundaries exclude URLs, identifiers, and larger abbreviations.
    """
    for match in re.finditer(r'(?<![\w./])U\.S(?![\w./])', text):
        yield match.start(), match.end(), 'U.S.'


def rebase_correction(before: str, current: str, quote: str, fix: str) -> str | None:
    """Move independent edit atoms onto the current paragraph during recovery.

    A conflicting atom is never guessed. Already applied atoms are recognized
    so overlapping proposals can retain earlier successful corrections.
    """
    from difflib import SequenceMatcher
    if not quote or before.count(quote) != 1:
        return None
    offset = before.index(quote)
    changes = SequenceMatcher(a=before, b=current, autojunk=False).get_opcodes()
    patches = []
    for tag, a, b, c, d in SequenceMatcher(a=quote, b=fix, autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        lo, hi, replacement = offset + a, offset + b, fix[c:d]
        # An identical prior atom already satisfied this part of the proposal.
        if any(i == lo and j == hi and current[x:y] == replacement
               for kind, i, j, x, y in changes if kind != 'equal'):
            continue
        if any(kind != 'equal' and (i < hi and j > lo or i == j == lo)
               for kind, i, j, x, y in changes):
            return None
        match = next(((x + lo - i, x + hi - i)
                      for kind, i, j, x, y in changes
                      if kind == 'equal' and i <= lo <= hi <= j), None)
        if match is None:
            return None
        patches.append((*match, replacement))
    result = current
    for lo, hi, replacement in reversed(patches):
        result = result[:lo] + replacement + result[hi:]
    return result
