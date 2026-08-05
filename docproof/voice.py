"""Whose grammar is it — the author's, or the character's?

Fourteen of the twenty-five error types carry some version of the same clause:
leave dialect alone, leave nonstandard register alone, *"me and him" is how
people speak*. They are there because the alternative is a proofreader who
levels a character's speech into standard English and hands the author back a
book with one voice in it.

But the same clause, on a manuscript whose nonstandard grammar is simply
error, silently declines to find half the errors in it. On the test manuscript
this module was written for, twenty-seven of the fifty-four missed errors were
inside quotation marks — every one of them sitting under a rule that said not
to look. Nothing was broken. The instructions were followed exactly.

So it stops being an assumption and becomes a statement, the way `variant`
already is. The press says which kind of book this is:

  * **preserve** — the nonstandard grammar is voice. The type sections already
    say so, and this adds nothing to the prompt and costs no tokens. Default,
    because it is right for most fiction and because the failure it risks is
    the recoverable one: a missed error, not a rewritten character.
  * **query** — the press cannot tell, which is the honest answer more often
    than not. The model reports it, and the pipeline turns anything inside
    dialogue into a margin question instead of a correction. Nobody's speech
    is changed; the author is asked.
  * **correct** — the nonstandard grammar is error. Report and correct it in
    dialogue as readily as in narration.

One boundary holds at every setting: this is about grammar, not pronunciation.
*Nobody never found* is grammar. *nothin'*, *gonna*, *ain't*, *'bout* are how
a word sounds, and a pass that "corrects" those is not proofreading a book, it
is deleting an accent from it.
"""
from __future__ import annotations

from dataclasses import dataclass

VOICE_KEYS = ("preserve", "query", "correct")


@dataclass(frozen=True)
class Voice:
    key: str = "preserve"

    @property
    def reports_dialect(self) -> bool:
        """Whether the model is asked to look at nonstandard grammar at all."""
        return self.key != "preserve"

    @property
    def queries_dialect(self) -> bool:
        """Whether what it finds inside dialogue asks instead of correcting."""
        return self.key == "query"

    def prompt_section(self) -> str:
        """What the model is told, once per pass, cached with the prompt.

        Empty at the default, so a press that never thinks about this pays
        nothing for it."""
        if not self.reports_dialect:
            return ""
        channel = (
            "These are raised as questions for the author, not as corrections "
            "— the pipeline turns them into margin comments itself, and no "
            "speech is changed. Report them exactly as you would report a "
            "correction; do not hedge the wording or lower the confidence to "
            "compensate, because the asking is already handled."
            if self.queries_dialect else
            "These are corrected like any other error, in dialogue as readily "
            "as in narration.")
        return (
            "NONSTANDARD GRAMMAR IN THIS MANUSCRIPT\n"
            "Several error types below tell you to leave dialect, nonstandard "
            "register and \"how people speak\" alone, and to stay silent when "
            "a speaker's whole register is nonstandard. For this manuscript "
            "that instruction is withdrawn. Where this section and a type's "
            "own do-not-flag list disagree, this section wins.\n\n"
            "The press has read this book and says its nonstandard grammar is "
            "error rather than voice. Report it wherever it falls, inside "
            "quotation marks as readily as outside them:\n"
            "- Double negatives: \"Nobody never found it\", \"won't never go "
            "out\", \"without hardly moving\", \"didn't say nothing\".\n"
            "- \"them\" for \"those\": \"them things\", \"past them shoals\".\n"
            "- Nonstandard verb forms: \"have went\", \"had wrote\", \"he "
            "seen it\", \"the light done the work\", \"didn't finished\".\n"
            "- Pronoun case in subjects: \"Me and him watched\", \"You and me "
            "are going\".\n"
            "- Agreement: \"the rules was\", \"Wasn't you scared\", \"every "
            "mystery have\".\n\n"
            f"{channel}\n\n"
            "This covers grammar, and only grammar. Spelling that represents "
            "how a word is pronounced is still the author's and is still left "
            "exactly alone: \"nothin'\", \"runnin'\", \"gonna\", \"outta\", "
            "\"ain't\", \"y'all\", \"'bout\", \"sez\", \"o'er\". Correcting "
            "those does not proofread a book, it deletes an accent from it. "
            "Regional idiom and word choice that break no rule stay too.")


def load_voice(key: str) -> Voice:
    if key not in VOICE_KEYS:
        raise ValueError(
            f"Unknown voice setting: {key!r}. Expected one of "
            f"{', '.join(VOICE_KEYS)}.")
    return Voice(key)


def in_dialogue(text: str, pos: int, *, single: bool = False) -> bool:
    """Does the character at `pos` stand inside quoted speech?

    Counted rather than matched, because a paragraph can open speech it never
    closes — a speech running on into the next paragraph is correctly written
    with no closing mark at all.

    The awkward case is the single-quote variant, where the closing mark and
    the apostrophe are the same character. A mark with letters on both sides is
    an apostrophe (*don't*), and a mark with a letter after it is an elision
    (*'tis*); neither closes anything. Straight quotation marks are ambiguous
    in principle — the same character opens and closes — so they are counted
    for parity, which is the best that can be done and is why the normalizer
    curls them before any of this runs.
    """
    opens, closes, plain = ("‘", "’", "'") if single else ("“", "”", '"')
    depth = straight = 0
    for i, ch in enumerate(text[:pos]):
        before = text[i - 1] if i else ""
        after = text[i + 1] if i + 1 < len(text) else ""
        if ch == opens and not before.isalpha():
            depth += 1
        elif ch == closes and not after.isalpha():
            # A letter after the mark means it is an apostrophe, not a close:
            # "don’t" mid-speech, or the elision in "’tis".
            depth -= 1
        elif ch == plain:
            straight += 1
    return depth > 0 or straight % 2 == 1
