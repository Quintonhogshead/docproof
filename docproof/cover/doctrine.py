"""The house doctrine — ONE copy, read by every surface that makes or judges
a cover.

docs/cover_designer_spec.md is 1,563 lines of provenance, build lists and
superseded waves; §15.18-15.23 are the parts a model has to have in the room
on every turn. This module is that distillation, and it exists because the
distillation was being made TWICE: once by hand into
docproof.canvas.assistant's system prompt, and once not at all — Cover
Studio's own direction, planner and critique calls never got it, so the
newest four addenda (scene agreement, the clipped-art value contract, the
second pass, the standing-figure cardinal rule) reached a model only when a
person was driving the canvas. A cover the studio generated unattended was
judged against the vintage of doctrine that happened to be hardcoded in
critique.py the day it was written.

**One numbering, four audiences.** `RULES` is the canonical list and a rule
keeps its number everywhere, so "rule 2" means the same thing in a canvas
critique, a planner brief and a judge's tell — that stability is the point,
and it is why `render` filters rules rather than renumbering them. Expect
gaps in the numbering on any surface but the canvas; a numbered list with
holes is a much smaller cost than a rule whose number depends on who is
reading it.

Which surface gets which rule is about what that surface can ACT on:

- `direction` writes concepts before anything exists, so it gets the rules
  that shape what to ask for (a cutout implies its grounding; type and
  palette are claims about the world) and none of the ones about editing a
  finished cover.
- `plan` (§15.16's composition planner) gets the arrangement rules — the
  grounding stack, depth bands by value, ground-contact agreement — because
  it is the one call that decides how plates will relate before a dollar is
  spent.
- `critique` gets the rules a judge can NAME in a finished render, plus
  rule 14, which is the standing warning against the judge inventing a
  metric.
- `canvas` gets all of them, including the three that are about conduct in
  an editing session (measure before you move, change the KIND of move,
  fewest ops) and mean nothing to a one-shot call with no tools.

Adding an addendum to the spec is now ONE edit here plus its surface list.
The rule text itself is deliberately imperative and short: it is read by a
model mid-task, not by an implementer choosing what to build.
"""
from __future__ import annotations

from dataclasses import dataclass

# Every surface `render` knows how to dress. A typo'd name would silently
# render an empty doctrine block — the one failure mode of a filter — so
# render() checks against this tuple and raises instead.
#
# "atelier" replaced "atelier" when the fixed judge loop became an agent
# (docproof.cover.atelier): the surface was never really "the judge", it was
# "the eyes that look at a finished render", and the agent is those eyes now.
# It shares "canvas"'s conduct rules because, like the canvas assistant, it
# is a tool-using conversation that can measure before it moves.
SURFACES = ("direction", "plan", "atelier", "canvas")


@dataclass(frozen=True)
class Rule:
    """One numbered rule, stored with its wrapping intact.

    `text` is the rule EXACTLY as it renders, leading "N. " and four-space
    continuation indents included, rather than a paragraph this module
    re-wraps. Two reasons: the canvas block below has to come out
    byte-identical to the hand-written one it replaces (tests/
    test_cover_doctrine.py asserts it), and a rule reads to a model the way
    it reads in the spec — re-wrapping is a chance to change emphasis that
    buys nothing."""
    n: int
    text: str
    surfaces: tuple[str, ...]


RULES: tuple[Rule, ...] = (
    Rule(
        1,
        "1.  Focal dominance: the subject is the loudest thing on the cover. If type or\n"
        "    a prop out-shouts it, fix the subject's scale or value — do not quiet the\n"
        "    title to compensate.",
        ("direction", "plan", "atelier", "canvas")),
    Rule(
        2,
        "2.  CARDINAL: a standing figure must LOOK like it is standing on something. A\n"
        "    reader adjudicates this instantly and without vocabulary, and no palette,\n"
        "    type or mark survives behind a floating figure.",
        ("direction", "plan", "atelier", "canvas")),
    Rule(
        3,
        "3.  When the plate has no floor, the answer is a new near-band plate, not\n"
        "    wedging the figure onto the nearest horizontal-ish surface. Generate the\n"
        "    ground; do not cram them onto some other platform.",
        ("direction", "plan", "atelier", "canvas")),
    Rule(
        4,
        "4.  Grounding a cutout is a STACK and all of it is required: a receiving\n"
        "    surface at the feet; a cast shadow on a plane lifted light enough to show\n"
        "    it; a hard, unblurred weld at the contact (2-3px); and no rim light in the\n"
        "    bottom 5% of the figure. A lit edge at the contact reads as hovering and\n"
        "    outvotes correct shadows.",
        ("plan", "atelier", "canvas")),
    Rule(
        5,
        "5.  A cutout implies its integration work at the moment you ask for it —\n"
        "    enumerate the grounding when you plan the plate, not when someone\n"
        "    complains it floats.",
        ("direction", "plan", "canvas")),
    Rule(
        6,
        "6.  Depth bands must differ in VALUE, not only in z-order: blend the far band\n"
        "    toward the sky behind it. Correct layer order makes depth stop being\n"
        "    thought about.",
        ("direction", "plan", "atelier", "canvas")),
    Rule(
        7,
        "7.  Ground contacts are shown-and-seated or hidden entirely, never mixed. One\n"
        "    visible termination against sky or canopy floats the whole scene.",
        ("direction", "plan", "atelier", "canvas")),
    Rule(
        8,
        "8.  Type has homes: eyebrow above the title, author line on a painted stable\n"
        "    ground. A scrim fighting busy texture at escalated strength reads as a\n"
        "    bezel, not as protection.",
        ("direction", "plan", "atelier", "canvas")),
    Rule(
        9,
        "9.  Clipped art — art visible only through letterforms — must be value-\n"
        "    OPPOSITE to its field and uniform edge to edge. A title that still needs a\n"
        "    scrim is a failed value direction, not a protected title.",
        ("direction", "plan", "atelier", "canvas")),
    Rule(
        10,
        "10. When the real object is the wrong value, change the reproduction medium,\n"
        "    not the palette: microfilm negative, blueprint, photostat, X-ray.",
        ("direction", "plan", "atelier", "canvas")),
    Rule(
        11,
        "11. Type and palette are claims about the world, not decoration over it. A\n"
        "    face or a hue from a different story reads \"off\" without the reader being\n"
        "    able to name why.",
        ("direction", "plan", "atelier", "canvas")),
    Rule(
        12,
        "12. Dead space is the enemy: a flat band over roughly a fifth of the height is\n"
        "    a failure, not breathing room.",
        ("direction", "plan", "atelier", "canvas")),
    Rule(
        13,
        "13. Measure before you move. `inspect` and `look` are the measurements; an\n"
        "    estimate off a preview carries ~10% error. Never assert a clearance, a\n"
        "    contact or a containment you have not actually read.",
        ("atelier", "canvas")),
    Rule(
        14,
        "14. A number proves legality, not quality. Look at the cover before making any\n"
        "    claim about it, and use each number only for the question it answers.",
        ("atelier", "canvas")),
    Rule(
        15,
        "15. Every element needs a reason on the page. A prop with no story job dies in\n"
        "    review.",
        ("direction", "plan", "atelier", "canvas")),
    Rule(
        16,
        "16. If a fix does not move the read, change the KIND of move rather than its\n"
        "    parameters again. Two failed refinements of one approach is the signal to\n"
        "    swap approaches.",
        ("atelier", "canvas")),
    Rule(
        17,
        "17. Fewest ops that achieve the ask. You are editing one thing a person named,\n"
        "    not re-litigating their cover.",
        ("atelier", "canvas")),
)


def render(surface: str) -> str:
    """The doctrine block for one surface: its rules, canonical numbering
    kept, joined the way the hand-written block joined them.

    Raises ValueError on an unknown surface rather than answering with an
    empty block — a prompt that quietly lost its doctrine is exactly the
    failure this module was built to end, and it would be invisible in a
    render."""
    if surface not in SURFACES:
        raise ValueError(
            f"{surface!r} is not a doctrine surface — expected one of "
            f"{', '.join(SURFACES)}")
    return "\n".join(r.text for r in RULES if surface in r.surfaces)


__all__ = ["RULES", "SURFACES", "Rule", "render"]
