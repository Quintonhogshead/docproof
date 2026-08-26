"""Proper-noun triage and the profile-correction overlay.

Two related, $0, deterministic tools that sit between a raw
`docproof.genre_profile.Profile` and the run config it seeds:

* **triage_proper_nouns** groups the harvested proper-noun candidates into four
  buckets an editor actually reasons in — ``protect`` (a real invented or
  technical name to shield from silent correction), ``enforce`` (a frequent
  name whose spelling
  should be made consistent), ``reject`` (a common word capitalized only at a
  sentence start — a false positive), and ``suspect`` (a tokenization artifact,
  or a near-match of another candidate, e.g. ``Deut``/``Deute``, that a human
  must look at). It is a suggestion, not a verdict: the human's decisions live
  in the correction overlay.

* **ProfileCorrections** is that overlay. It stores a human's corrections —
  a genre override ("the fantasy heuristic was wrong; this is theological
  non-fiction"), a corrected chapter map, and per-name classifications — as a
  SEPARATE document. ``apply_corrections`` returns a corrected copy of the
  profile; the raw deterministic profile is never rewritten, so the record of
  "what the machine found" and "what the human changed" stay distinct and
  auditable.

Only ``protect`` and ``enforce`` names are seeded into the run config
(consistency/spellcheck); ``reject`` and ``suspect`` are held back, so a false
positive or an unreviewed near-match never becomes a silent allowlist entry.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .genre_profile import ChapterProfile, Profile, ProperNounCandidate

NOUN_CLASSES = ("protect", "enforce", "reject", "suspect")

# A candidate seen at least this many times, and not a common word, is frequent
# enough that consistent spelling is worth ENFORCING rather than merely
# protecting. Below it, protect (shield from correction) is the safer default.
_ENFORCE_MIN_COUNT = 8
# A zipf frequency at or above this marks a token common enough in ordinary
# English that its capitalization is probably sentence-initial, not a proper
# noun — a reject candidate. "the" is 7.7; "River" ~5; a coined name is ~0.
_COMMON_WORD_ZIPF = 4.6


class TriageEntry(BaseModel):
    name: str
    count: int
    suggested_class: str             # one of NOUN_CLASSES
    reason: str = ""


class ProfileCorrections(BaseModel):
    """A human's corrections to a deterministic Profile, stored as an overlay.

    Every field is optional; an empty overlay applies as a no-op. Kept as its
    own document (``<profile>.corrections.json`` by convention) so the raw
    profile is preserved verbatim."""
    source: str = ""                 # the profile this corrects (for the record)
    recommended_preset: str | None = None
    chapters: list[ChapterProfile] | None = None
    # name -> one of NOUN_CLASSES. Overrides the triage suggestion for that name.
    proper_noun_classes: dict[str, str] = Field(default_factory=dict)
    note: str = ""

    def model_post_init(self, _ctx) -> None:
        bad = {c for c in self.proper_noun_classes.values()
               if c not in NOUN_CLASSES}
        if bad:
            raise ValueError(
                f"proper_noun_classes values must be one of {NOUN_CLASSES}; "
                f"got {sorted(bad)}")


def _zipf(token: str) -> float:
    from wordfreq import zipf_frequency
    return zipf_frequency(token.lower(), "en")


def _is_tokenization_artifact(name: str) -> bool:
    t = name.strip()
    if not t:
        return True
    if any(ch.isdigit() for ch in t):
        return True
    # leading/trailing non-alphanumeric, or a stray single capital letter
    if not (t[0].isalpha() and t[-1].isalpha()):
        return True
    if len(t) == 1:
        return True
    return False


def _near_matches(names: list[str]) -> set[str]:
    """Names that are a prefix of, or one edit from, another candidate — the
    Deut/Deute class. Both members of such a pair are flagged suspect: which is
    correct is a human call."""
    flagged: set[str] = set()
    lowered = [(n, n.lower()) for n in names]
    for i, (n_i, l_i) in enumerate(lowered):
        for n_j, l_j in lowered:
            if n_i == n_j:
                continue
            if l_i == l_j:
                continue
            if l_j.startswith(l_i) and 0 < len(l_j) - len(l_i) <= 2:
                flagged.add(n_i)
                flagged.add(n_j)
            elif _within_one_edit(l_i, l_j):
                flagged.add(n_i)
                flagged.add(n_j)
    return flagged


def _within_one_edit(a: str, b: str) -> bool:
    """Cheap Damerau-lite: True if `a` and `b` differ by at most one
    substitution or one insertion/deletion. Only called on short proper-noun
    tokens, so the O(len) work is negligible."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diffs = sum(1 for x, y in zip(a, b) if x != y)
        return diffs == 1
    # length differs by 1: is the shorter an insertion-away from the longer?
    short, long = (a, b) if len(a) < len(b) else (b, a)
    i = j = 0
    skipped = False
    while i < len(short) and j < len(long):
        if short[i] == long[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            j += 1
    return True


def triage_proper_nouns(profile: Profile) -> list[TriageEntry]:
    """Classify a profile's proper-noun candidates into the four buckets. Pure
    and deterministic; the order is by descending count then name so the output
    is stable."""
    names = [pn.name for pn in profile.proper_nouns]
    near = _near_matches(names)
    entries: list[TriageEntry] = []
    for pn in profile.proper_nouns:
        name, count = pn.name, pn.count
        if _is_tokenization_artifact(name):
            cls, reason = "suspect", "looks like a tokenization artifact"
        elif name in near:
            cls, reason = "suspect", "near-match of another candidate"
        elif _zipf(name) >= _COMMON_WORD_ZIPF:
            cls, reason = "reject", "a common English word (likely sentence-" \
                                    "initial capitalization)"
        elif count >= _ENFORCE_MIN_COUNT:
            cls, reason = "enforce", f"frequent ({count}x): enforce a " \
                                     f"consistent spelling"
        else:
            cls, reason = "protect", "an invented/uncommon name to shield from " \
                                     "correction"
        entries.append(TriageEntry(name=name, count=count,
                                   suggested_class=cls, reason=reason))
    entries.sort(key=lambda e: (-e.count, e.name))
    return entries


def apply_corrections(profile: Profile,
                      corrections: ProfileCorrections) -> Profile:
    """Return a corrected COPY of `profile` with the overlay applied — the raw
    profile is never mutated. A genre override replaces ``recommended_preset``;
    a corrected chapter map replaces ``chapters``; per-name classifications
    drop ``reject``/``suspect`` names from ``proper_nouns`` so only vetted names
    survive into seeding (an unclassified name is kept, unchanged)."""
    data = profile.model_copy(deep=True)
    if corrections.recommended_preset:
        data.recommended_preset = corrections.recommended_preset
    if corrections.chapters is not None:
        data.chapters = [c.model_copy(deep=True) for c in corrections.chapters]
    classes = corrections.proper_noun_classes
    if classes:
        kept: list[ProperNounCandidate] = []
        for pn in data.proper_nouns:
            cls = classes.get(pn.name)
            if cls in ("reject", "suspect"):
                continue
            kept.append(pn)
        data.proper_nouns = kept
    return data


def seedable_names(profile: Profile,
                   corrections: ProfileCorrections | None = None) -> list[str]:
    """The proper nouns safe to seed into the run config: after corrections,
    every surviving candidate the human did not mark reject/suspect. With no
    corrections, all candidates (the current behaviour)."""
    p = apply_corrections(profile, corrections) if corrections else profile
    return [pn.name for pn in p.proper_nouns]


__all__ = [
    "NOUN_CLASSES", "TriageEntry", "ProfileCorrections",
    "triage_proper_nouns", "apply_corrections", "seedable_names",
]
