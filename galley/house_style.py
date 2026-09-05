"""Shared house-style rules for finished-text prompts and settlement guards.

``HOUSE_RULES`` is the single source rendered by :func:`house_rules_block`;
forms listed there are correct and must not be flagged or changed.
"""
from __future__ import annotations

# One rule per line, phrased as the form that is CORRECT in this house. Kept
# short and literal: these lines are read by a model deciding whether to
# flag a span, and a rule it can match by shape beats one it must interpret.
HOUSE_RULES: tuple[str, ...] = (
    "Clock times are digits with minutes and a capital meridiem with no "
    "periods: “4:00 AM”, “11:30 PM” — never "
    "“4 a.m.”, “4:00 a.m.”, or “4AM”.",
    "“Percent” is written out after a numeral (“40 "
    "percent”), not the % sign.",
    "Em dashes are unspaced (“word—word”).",
    "The serial (Oxford) comma is used.",
    "Periods and commas sit INSIDE closing quotation marks.",
    "Dialogue takes double quotation marks; a quotation inside dialogue "
    "takes single marks.",
    "Whole numbers from one through one hundred are spelled out in prose.",
)


def house_rules_block(role: str = "reader") -> str:
    """The prompt section listing the house rules, plus the instruction the
    Georgis run showed to be load-bearing: a house form is never an error.
    `role` names the reader in the closing line so the sentence reads
    naturally in each prompt ("the walk", "the verifier", "the judge")."""
    lines = "\n".join(f"  - {rule}" for rule in HOUSE_RULES)
    return (
        "HOUSE STYLE (the press's own rules; where these differ from Chicago "
        "17, the HOUSE form is correct and Chicago's is not):\n"
        f"{lines}\n"
        f"A span already in one of these house forms is CORRECT. As the "
        f"{role}, never flag it, never suggest the Chicago form for it, and "
        f"never change text that already follows a house rule."
    )


__all__ = ["HOUSE_RULES", "house_rules_block"]
