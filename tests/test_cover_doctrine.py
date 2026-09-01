"""The house doctrine: one copy, and every surface that makes or judges a
cover actually carrying it.

The bug this module was written against was silent — the newest four
addenda (§15.20-15.23) existed in the spec and in the canvas assistant's
hand-typed prompt, and reached Cover Studio's own direction, planner and
critique calls not at all, so an unattended cover was judged against
whatever vintage of doctrine happened to be hardcoded the day critique.py
was written. Nothing failed; the covers were just worse. These tests are
therefore mostly INTEGRATION assertions: the point is not that
doctrine.render() returns a string, it is that the string is in the prompt.
"""
from __future__ import annotations

import pytest

from docproof.canvas import assistant
from docproof.cover import atelier, direction, doctrine, planner

# Rule 2 is the cardinal one (§15.23) and the reason the whole module
# exists: it must reach every surface, so it doubles as the marker that a
# given prompt carries doctrine at all.
CARDINAL = "a standing figure must LOOK like it is standing on something"


def test_every_rule_is_numbered_once_and_in_order():
    numbers = [r.n for r in doctrine.RULES]
    assert numbers == list(range(1, len(doctrine.RULES) + 1))


def test_every_rule_states_its_number_in_its_own_text():
    # render() joins the stored text verbatim rather than renumbering, so a
    # rule whose prefix disagreed with its `n` would cite wrongly forever.
    for rule in doctrine.RULES:
        assert rule.text.startswith(f"{rule.n}.")


def test_every_rule_reaches_at_least_one_surface():
    for rule in doctrine.RULES:
        assert rule.surfaces, f"rule {rule.n} is rendered nowhere"
        for surface in rule.surfaces:
            assert surface in doctrine.SURFACES


def test_the_canvas_gets_all_of_them():
    # The editing session is the only surface with tools and a conversation,
    # so it is the only one the conduct rules mean anything to.
    assert doctrine.render("canvas") == "\n".join(r.text for r in doctrine.RULES)


def test_numbering_is_stable_across_surfaces():
    """"Rule 2" must mean the same thing to a judge and to the canvas — the
    reason render() filters instead of renumbering. Gaps are the accepted
    cost."""
    for surface in doctrine.SURFACES:
        for rule in doctrine.RULES:
            if surface in rule.surfaces:
                assert rule.text in doctrine.render(surface)


def test_an_unknown_surface_raises_rather_than_rendering_nothing():
    # An empty doctrine block is invisible in a finished cover; a typo'd
    # surface name has to fail where it is written.
    with pytest.raises(ValueError, match="not a doctrine surface"):
        doctrine.render("judge")


@pytest.mark.parametrize("surface", doctrine.SURFACES)
def test_the_cardinal_rule_reaches_every_surface(surface):
    assert CARDINAL in doctrine.render(surface)


# -- the integration half: the prompts actually carry it ----------------------

def test_the_art_direction_prompt_carries_the_doctrine():
    prompt = direction._direction_system_prompt(
        4, has_sample=True, genre="thriller")
    assert CARDINAL in prompt
    assert doctrine.render("direction") in prompt


def test_the_planner_prompt_carries_the_doctrine():
    assert doctrine.render("plan") in planner._plan_system_prompt()


def test_the_stage_review_prompt_carries_the_doctrine():
    # The last moment anything can change before pixels are bought, and the
    # one call looking at the real plate.
    assert doctrine.render("plan") in planner._review_system_prompt()


def test_the_building_agents_prompt_carries_the_doctrine():
    """The agent that plans, buys and judges one cover -- the surface that
    replaced the fixed critique loop."""
    assert doctrine.render("atelier") in atelier._system_prompt()


def test_the_canvas_assistant_prompt_carries_the_doctrine():
    assert doctrine.render("canvas") in assistant.SYSTEM_PROMPT


def test_the_conduct_rules_stay_out_of_the_one_shot_calls():
    """Rules 13/16/17 are about conduct across a tool-using conversation
    ("measure before you move", "fewest ops"). Shipping them to a one-shot
    call with no tools would be instructions the model cannot follow, which
    is how a prompt teaches a model to answer loosely."""
    conduct = [r for r in doctrine.RULES
               if r.surfaces == ("atelier", "canvas")]
    assert conduct, "the conduct rules were re-tagged; re-check this test"
    for rule in conduct:
        for surface in ("direction", "plan"):
            assert rule.text not in doctrine.render(surface)
        # ...but they DO reach the two tool-using surfaces, which can obey them
        for surface in ("atelier", "canvas"):
            assert rule.text in doctrine.render(surface)
