"""Shared house-style rules for finished-text prompts and settlement guards.

``HOUSE_RULES`` is the single source rendered by :func:`house_rules_block`;
forms listed there are correct and must not be flagged or changed. The
rules are the Atmosphere Press House Style Guide
(https://atmospherepress.com/atmosphere-press-house-style-guide/) as the
press publishes it: Chicago 17 for U.S.-oriented manuscripts, the Oxford
Style Manual for U.K.-oriented ones, plus the press's own supplement. Where
a rule differs by English variant the line names both forms, because most
readers of this block do not know the run's variant; a caller that does
passes it and gets only the applicable half.
"""
from __future__ import annotations

# Rules that are the same in every English variant. One rule per line,
# phrased as the form that is CORRECT in this house. Kept short and literal:
# these lines are read by a model deciding whether to flag a span, and a rule
# it can match by shape beats one it must interpret.
COMMON_RULES: tuple[str, ...] = (
    "The serial (Oxford) comma is used before “and”/“or” in a series of "
    "three or more (“apples, oranges, and bananas”).",
    "A comma follows an introductory word, phrase, or clause (“After the "
    "meeting, we left.”); commas set off a nonrestrictive clause (“My "
    "sister, who lives in Boston, is visiting.”); a comma separates a "
    "greeting from a name in direct address (“Hey, Sarah.”); semicolons "
    "separate complex list items that themselves contain commas.",
    "Em dashes are unspaced (“word—word”) for breaks and emphasis. An en "
    "dash joins numeric and date ranges (“1999–2005”), linked terms (“New "
    "York–London flight”), and an open compound used as a modifier "
    "(“post–World War II era”, “pre–Civil War tensions”). A hyphen joins "
    "compound modifiers (“well-known author”) and ages (“a twelve-year-old”). "
    "A prefixed compound is CLOSED wherever Merriam-Webster closes it "
    "(“nonstandard”, “reread”, “predawn”, “prelaunch”, “reevaluate”, "
    "“nonreflective”) and hyphenated only where the dictionary hyphenates it "
    "or the closed form is not a dictionary word (“multi-week”, "
    "“under-caffeinated”); the dictionary's own exceptions stand as it prints "
    "them, for a vowel collision (“pre-empt”) and before a proper noun or "
    "numeral (“anti-American”, “pre-1914”). A compound whose adverb ends in -ly is "
    "NOT hyphenated (“a highly regarded author”); a series uses suspended "
    "hyphenation (“first-, second-, and third-round winners”).",
    "An ellipsis is the single … character with a space on either side "
    "(“This ellipsis … is preferred.”); never three typed periods.",
    "A direct question ends with ? and an exclamation with !, one mark "
    "only: never stacked marks (“!!”) and never an interrobang (“?!”).",
    "Whole numbers from one through one hundred are spelled out in prose "
    "(“fifty-six participants”); 101 and up are numerals (“256 "
    "participants”) — except where Chicago keeps digits: parts of a book "
    "(“book 1”, “chapter 5”, “page 7”) and eras (“45 BC”, “AD 70”). Chapter "
    "and part HEADINGS follow the book's own label style.",
    "Simple fractions are spelled out and hyphenated when adjectival "
    "(“one-half”, “two-thirds”, “a two-thirds majority”); thousands take a "
    "comma (“1,000”); centuries are spelled out (“twenty-first century”).",
    "Clock times are digits with minutes, a colon, and a lowercase "
    "meridiem — U.S. (Chicago): with periods, “8:30 a.m.”, “3:00 p.m.”; "
    "U.K. (Oxford): without periods, “8:30 am”, “3:00 pm” (an author's "
    "point separator, “8.30 am”, is kept if used consistently). Never "
    "“3:00 PM”, “3 PM”, or “3PM”, and never a meridiem invented for a bare "
    "hour. A time already written in 24-hour form (“17:03”, “00:05 UTC”, "
    "“0830”, “thirteen hundred”) stays 24-hour: never convert it to a.m./p.m. "
    "or invent a meridiem for it. A range takes an unspaced en dash "
    "(“9:00–5:00 p.m.”).",
    "A formal date never takes an ordinal (“24 July”, not “24th July”). "
    "U.S.: month day, year, with a comma after the day and after the year "
    "when the sentence continues (“July 14, 1989, was rainy.”). U.K.: day "
    "month year with no commas (“14 July 1989 was rainy.”).",
    "Decades — U.S.: an apostrophe for the dropped century and none before "
    "the s (“’60s”); U.K.: no apostrophe at all (“60s”).",
    "Numerals go with units (“10 kg”, “30°C”) and currency symbols "
    "(“$5.00”, “£12.50”) — except in dialogue, where the amount follows the "
    "numbers rule and the currency is spelled out (“I need five dollars.”, "
    "“I need 500 pounds.”). Percent is spelled out — U.S.: “40 percent”; "
    "U.K.: “40 per cent”; the % sign belongs in data tables only.",
    "Dialogue — U.S.: double quotation marks, single for a quotation "
    "nested inside; U.K.: single quotation marks, double nested inside. A "
    "dialogue tag continuing the sentence is lowercase (“I'm ready,” she "
    "said.). One speaker's speech across paragraphs opens each paragraph "
    "with a quotation mark and closes only at the end of the speech.",
    "Periods and commas — U.S.: sit INSIDE closing quotation marks; U.K.: "
    "follow the manuscript's own consistent (logical) pattern.",
    "Titles of long works (books, plays, films, albums, artworks, "
    "newspapers, journals, video games) are italic; titles of short works "
    "(articles, essays, poems, short stories, songs, chapters, TV "
    "episodes) take quotation marks — double in U.S. style, single in "
    "U.K. style.",
    "“That” introduces a restrictive clause with no comma; “which” "
    "introduces a nonrestrictive clause set off by commas. U.K. authors may "
    "use either in a restrictive clause if consistent with their voice.",
    "Capitalize official names of places, organizations, institutions, "
    "and products (“Oxford University”, “Mount Everest”); lowercase a "
    "generic reference (“the university”). A family term or personal title "
    "is capitalized as a proper noun or in direct address (“I asked Mom for "
    "help.”) and lowercase generically or after a possessive (“I asked my "
    "mom for help.”). A job title or honorific is capitalized before a name "
    "(“President Harris”) and lowercase generically or after one (“Harris, "
    "the president”). Days, months, holidays, nationalities, and languages "
    "are capitalized; compass directions are lowercase unless part of a "
    "proper name (“She drove south.” / “the West Coast”).",
    "Spelling follows the manuscript's English variant consistently — "
    "U.S.: Merriam-Webster (color, organize, traveled); U.K.: Oxford "
    "(colour, organise, travelled). Intentional variation, such as dialect "
    "in dialogue, is respected.",
    "Author preference wins: a clearly communicated, consistently applied "
    "stylistic choice or intentional deviation is never an error.",
)

# Backward-compatible name: the full rule list every reader is shown.
HOUSE_RULES: tuple[str, ...] = COMMON_RULES

# Illustrative house forms per variant, for callers that want one example
# of the clock time (the rule the Georgis run showed readers get wrong most).
TIME_EXAMPLE = {"us": "3:00 p.m.", "ca": "3:00 p.m.", "uk": "3:00 pm", "au": "3:00 pm"}


def house_rules_block(role: str = "reader", variant=None) -> str:
    """The prompt section listing the house rules, plus the instruction the
    Georgis run showed to be load-bearing: a house form is never an error.
    `role` names the reader in the closing line so the sentence reads
    naturally in each prompt ("the walk", "the verifier", "the judge").
    `variant` (a docproof Variant or its key) names the English in force so
    the closing line can say which half of a split rule applies."""
    key = getattr(variant, "key", variant)
    lines = "\n".join(f"  - {rule}" for rule in HOUSE_RULES)
    which = ""
    if key in ("us", "ca"):
        which = (" This manuscript is U.S.-oriented (Chicago 17): where a rule "
                 "above gives a U.S. and a U.K. form, the U.S. form applies.")
    elif key in ("uk", "au"):
        which = (" This manuscript is U.K.-oriented (Oxford): where a rule "
                 "above gives a U.S. and a U.K. form, the U.K. form applies.")
    return (
        "HOUSE STYLE (the Atmosphere Press House Style Guide; where these "
        "differ from Chicago 17 or Oxford, the HOUSE form is correct):\n"
        f"{lines}\n"
        f"A span already in one of these house forms is CORRECT. As the "
        f"{role}, never flag it, never suggest the Chicago or Oxford form for "
        f"it, and never change text that already follows a house rule.{which}"
    )


__all__ = ["COMMON_RULES", "HOUSE_RULES", "TIME_EXAMPLE", "house_rules_block"]
