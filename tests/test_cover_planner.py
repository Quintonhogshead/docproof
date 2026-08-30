"""docproof/cover/planner.py: the frontier composition planner (§15.16) —
plan_composition (text-only planning) and review_stage (the staged
mid-generation vision review), both on the `anthropic` SDK directly, the
critique.py precedent.

No network, no real anthropic.Anthropic call anywhere here — every test
drives the same fake client shape tests/test_cover_critique.py established:
`.messages.stream(**kwargs)` returning a context manager whose
`get_final_message()` gives back a Message-shaped object, with a
`behavior(kwargs, call_number)` callback deciding what each call does
(return a canned Message, or raise on __enter__ exactly where the real SDK
would). The PIPELINE half of §15.16 — the COVER_PLANNER gate, staged
painting order, plan.json persistence, degrade-to-spontaneous — lives in
tests/test_cover_pipeline.py; this file only ever tests what the two
planner calls themselves do with what they were handed.
"""
from __future__ import annotations

import base64
import io
import json

import anthropic
import httpx
import pytest
from PIL import Image

from docproof.cover.archetypes import ARCHETYPES
from docproof.cover.direction import Direction
from docproof.cover.model import Brief, Palette, build_spec
from docproof.cover.planner import (MAX_WIDTH, PLAN_EFFORT,
                                    PLANNER_FALLBACK_MODEL, PLANNER_MODEL,
                                    REVIEW_EFFORT, CompositionPlan,
                                    PlannerError, StageReview,
                                    plan_composition, review_stage)
from docproof.providers import strict_json_schema

# -- fixtures: brief/spec, canned plans, canned PNGs, canned SDK errors --------

SUFFIX = ("Warm amber dusk key light from the west, anchored to #101010 and "
          "#c9a227, painterly gouache, timeless coastal era.")


def _brief(**overrides) -> Brief:
    data = dict(title="The Lighthouse at Gull Point", author="J. R. Vance",
               genre="literary", mood="elegiac, wintry")
    data.update(overrides)
    return Brief(**data)


def _spec(archetype: str = "cutout_sandwich"):
    """cutout_sandwich: two generatable slots (background + focal), the
    natural staged-plan shape."""
    palette = Palette(background="#101010", primary="#f5f1e8",
                      accent="#c9a227", text="#f5f1e8", scrim="#000000")
    direction = Direction(
        concept_name="Ash and Brass", rationale="A test concept.",
        archetype=archetype, palette=palette,
        title_font="Playfair Display", author_font="Spectral",
        art_prompts={"background": "A misty pine forest, gouache.",
                     "focal": "A cloaked figure, cutout subject only."},
        texture=False)
    return build_spec(direction, _brief(), ARCHETYPES[archetype])


def _plan(**overrides) -> CompositionPlan:
    data = dict(
        light="Key light low from the west, warm amber, dusk.",
        palette_anchors=["background #101010", "accent #c9a227"],
        depth=[{"slot": "background", "plane": "far",
                "negative_space": "leave the upper third as empty sky"},
               {"slot": "focal", "plane": "near",
                "negative_space": "keep the left edge clear"}],
        horizon_y=0.62,
        generation_order=["background", "focal"],
        conditioning=[{"slot": "focal", "review": "background"}],
        unify_recipe="", unify_stops=[],
        consistency_suffix=SUFFIX,
        prompts=[{"slot": "background",
                  "prompt": f"A misty pine forest, vast empty sky. {SUFFIX}"},
                 {"slot": "focal",
                  "prompt": f"A cloaked figure on a ridge. {SUFFIX}"}],
        cost=0.12, model=PLANNER_MODEL)
    data.update(overrides)
    return CompositionPlan(**data)


def _plan_reply_json(**overrides) -> str:
    """A canned wire reply — the payload half of _plan (no cost/model:
    the wire never carries bookkeeping)."""
    payload = _plan(**overrides).model_dump(exclude={"cost", "model"})
    return json.dumps(payload)


def _review_reply_json(*, prompt: str = f"Final cloaked figure. {SUFFIX}",
                       anchor=(0.5, 0.62), scale: float = 1.2,
                       offset=(0.05, 0.0), mask_angle=None) -> str:
    return json.dumps({"prompt": prompt, "anchor": list(anchor),
                       "scale": scale, "offset": list(offset),
                       "mask_angle": mask_angle})


def _png_bytes(size: tuple[int, int] = (1200, 1800), color=(20, 20, 20)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _api_error(cls, status: int, message: str):
    resp = httpx.Response(status, request=_REQUEST,
                          json={"error": {"message": message}})
    return cls(message, response=resp, body={"error": {"message": message}})


def _rate_limited() -> anthropic.RateLimitError:
    return _api_error(anthropic.RateLimitError, 429, "Rate limit reached.")


def _model_not_found() -> anthropic.NotFoundError:
    return _api_error(anthropic.NotFoundError, 404, "model not found")


def _bad_request() -> anthropic.BadRequestError:
    return _api_error(anthropic.BadRequestError, 400, "bad request shape")


# -- fake client: .messages.stream() (test_cover_critique.py's exact shape) ----

class _FakeUsage:
    def __init__(self, input_tokens: int = 800, output_tokens: int = 60):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _FakeMessage:
    def __init__(self, *, content=None, stop_reason: str = "end_turn",
                usage: _FakeUsage | None = None):
        self.content = content or []
        self.stop_reason = stop_reason
        self.usage = usage or _FakeUsage()


def _message(text: str, **kw) -> _FakeMessage:
    return _FakeMessage(content=[_FakeTextBlock(text)], **kw)


class _FakeStreamManager:
    def __init__(self, message_or_exc):
        self._m = message_or_exc

    def __enter__(self):
        if isinstance(self._m, BaseException):
            raise self._m
        return self

    def __exit__(self, *exc_info):
        return False

    def get_final_message(self):
        return self._m


class FakePlannerClient:
    def __init__(self, behavior):
        self._behavior = behavior
        self.calls: list[dict] = []

        class _Messages:
            def stream(inner_self, **kwargs):
                self.calls.append(kwargs)
                return _FakeStreamManager(self._behavior(kwargs, len(self.calls)))

        self.messages = _Messages()


def _once(message_or_exc):
    return lambda kwargs, call_number: message_or_exc


def _first_call_raises_then(exc: BaseException, message: _FakeMessage):
    def behavior(kwargs, call_number):
        return exc if call_number == 1 else message
    return behavior


# -- the plan schema round-trips (§15.15 PR7) ----------------------------------

def test_plan_schema_round_trips_through_json():
    plan = _plan()
    reloaded = CompositionPlan.model_validate_json(plan.model_dump_json())
    assert reloaded == plan
    # Bookkeeping survives the trip too — that is what makes plan.json a
    # complete, replayable record.
    assert reloaded.cost == pytest.approx(0.12)
    assert reloaded.model == PLANNER_MODEL


def test_plan_wire_schema_is_strict_mode_safe():
    from docproof.cover.planner import _PlanPayload, _ReviewPayload
    for model in (_PlanPayload, _ReviewPayload):
        schema = strict_json_schema(model)
        assert schema["additionalProperties"] is False
        # Every property is required on the wire (a Python default is still
        # a required key — strict_json_schema's own contract).
        assert set(schema["required"]) == set(schema["properties"])
    plan_schema = strict_json_schema(_PlanPayload)
    # Bookkeeping never rides the wire.
    assert "cost" not in plan_schema["properties"]
    assert "model" not in plan_schema["properties"]


def test_plan_helper_lookups():
    plan = _plan()
    assert plan.prompt_for("focal").endswith(SUFFIX)
    assert plan.prompt_for("nonexistent") is None
    assert plan.review_source_for("focal") == "background"
    assert plan.review_source_for("background") == ""


def test_judge_lines_carry_light_and_unify():
    lines = _plan(unify_recipe="quiet_literary",
                  unify_stops=["background", "primary"]).judge_lines()
    assert any("lighting contract" in line and "warm amber" in line
               for line in lines)
    assert any("unify bind" in line and "quiet_literary" in line
               and "background, primary" in line for line in lines)
    # No bind declared -> no unify line at all.
    assert len(_plan().judge_lines()) == 1


# -- plan_composition ----------------------------------------------------------

def test_plan_composition_happy_path():
    client = FakePlannerClient(_once(_message(_plan_reply_json())))
    plan = plan_composition(_brief(), _spec(), ARCHETYPES["cutout_sandwich"],
                            "OPENING SAMPLE: fog on the water.", client)

    assert isinstance(plan, CompositionPlan)
    assert plan.light.startswith("Key light low")
    assert plan.generation_order == ["background", "focal"]
    assert [c.slot for c in plan.conditioning] == ["focal"]
    assert plan.model == PLANNER_MODEL
    # Priced through the same catalog helper critique uses: 800 in / 60 out
    # on claude-fable-5 at $10/$50 per MTok.
    assert plan.cost == pytest.approx((800 * 10.0 + 60 * 50.0) / 1_000_000)

    (kwargs,) = client.calls
    assert kwargs["model"] == PLANNER_MODEL
    assert kwargs["max_tokens"] >= 8000            # the truncation house rule
    fmt = kwargs["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert kwargs["output_config"]["effort"] == PLAN_EFFORT

    (message,) = kwargs["messages"]
    text = message["content"][0]["text"]
    # Brief, palette, code-computed text zones, the draft prompts, the
    # recipe shelf, and the manuscript sample all reach the model.
    assert '"The Lighthouse at Gull Point"' in text
    assert "#c9a227" in text
    assert "title x" in text and "%" in text
    assert "A misty pine forest, gouache." in text
    assert "quiet_literary" in text                # describe_recipes()
    assert "fog on the water" in text


def test_plan_composition_enforces_suffix_and_repairs_ranges():
    """The consistency suffix is a code guarantee, not a hope: a reply whose
    prompts forgot it comes back with it appended; a horizon past the canvas
    is clamped; a duplicated generation_order is de-duplicated."""
    raw = json.loads(_plan_reply_json())
    raw["prompts"] = [{"slot": "background", "prompt": "A misty pine forest."},
                      {"slot": "focal", "prompt": "A cloaked figure."}]
    raw["horizon_y"] = 1.7
    raw["generation_order"] = ["background", "background", "focal"]
    client = FakePlannerClient(_once(_message(json.dumps(raw))))

    plan = plan_composition(_brief(), _spec(), ARCHETYPES["cutout_sandwich"],
                            "", client)
    assert all(p.prompt.endswith(SUFFIX) for p in plan.prompts)
    assert plan.horizon_y == 1.0
    assert plan.generation_order == ["background", "focal"]


def test_plan_composition_falls_back_on_model_not_found_only():
    client = FakePlannerClient(_first_call_raises_then(
        _model_not_found(), _message(_plan_reply_json())))
    plan = plan_composition(_brief(), _spec(), ARCHETYPES["cutout_sandwich"],
                            "", client)
    assert [c["model"] for c in client.calls] == [PLANNER_MODEL,
                                                  PLANNER_FALLBACK_MODEL]
    # The answering model is what the plan records (and what the cost was
    # priced at).
    assert plan.model == PLANNER_FALLBACK_MODEL
    assert plan.cost == pytest.approx((800 * 5.0 + 60 * 25.0) / 1_000_000)


def test_plan_composition_does_not_fall_back_on_other_errors():
    client = FakePlannerClient(_once(_bad_request()))
    with pytest.raises(PlannerError, match="planning call failed"):
        plan_composition(_brief(), _spec(), ARCHETYPES["cutout_sandwich"],
                         "", client)
    assert len(client.calls) == 1                  # no fallback, no retry


def test_plan_composition_retries_a_transient_failure_once():
    client = FakePlannerClient(_first_call_raises_then(
        _rate_limited(), _message(_plan_reply_json())))
    plan = plan_composition(_brief(), _spec(), ARCHETYPES["cutout_sandwich"],
                            "", client)
    assert isinstance(plan, CompositionPlan)
    assert [c["model"] for c in client.calls] == [PLANNER_MODEL, PLANNER_MODEL]


@pytest.mark.parametrize("message, match", [
    (_FakeMessage(content=[_FakeTextBlock("{}")], stop_reason="refusal"),
     "declined"),
    (_FakeMessage(content=[_FakeTextBlock("{\"light\":")],
                 stop_reason="max_tokens"), "cut off"),
    (_message("this is not json"), "schema"),
])
def test_plan_composition_bad_replies_raise_planner_error(message, match):
    client = FakePlannerClient(_once(message))
    with pytest.raises(PlannerError, match=match):
        plan_composition(_brief(), _spec(), ARCHETYPES["cutout_sandwich"],
                         "", client)


# -- review_stage --------------------------------------------------------------

def _decoded_image_widths(kwargs) -> list[int]:
    widths = []
    for block in kwargs["messages"][0]["content"]:
        if block.get("type") == "image":
            data = base64.b64decode(block["source"]["data"])
            with Image.open(io.BytesIO(data)) as img:
                widths.append(img.width)
    return widths


def test_review_stage_downscales_prior_renders_and_names_the_slot():
    client = FakePlannerClient(_once(_message(_review_reply_json())))
    review = review_stage(_plan(), "focal", [_png_bytes(size=(1200, 1800))],
                          "A cloaked figure draft.", client)

    assert isinstance(review, StageReview)
    (kwargs,) = client.calls
    assert kwargs["output_config"]["effort"] == REVIEW_EFFORT
    # The prior render rides as one image block, downscaled to the critique
    # discipline's ceiling.
    assert _decoded_image_widths(kwargs) == [MAX_WIDTH]
    text = "".join(b.get("text", "") for b in kwargs["messages"][0]["content"])
    assert "Pending slot: focal" in text
    assert "A cloaked figure draft." in text
    assert SUFFIX in text                          # the plan's suffix restated


def test_review_stage_returns_placement_and_guarantees_the_suffix():
    client = FakePlannerClient(_once(_message(_review_reply_json(
        prompt="Final cloaked figure, no suffix here.",
        anchor=(0.4, 0.7), scale=1.15, offset=(0.02, -0.03), mask_angle=90.0))))
    review = review_stage(_plan(), "focal", [_png_bytes()], "draft", client)

    assert review.prompt.endswith(SUFFIX)          # appended in code
    assert review.anchor == [0.4, 0.7]
    assert review.scale == pytest.approx(1.15)
    assert review.offset == [0.02, -0.03]
    assert review.mask_angle == pytest.approx(90.0)
    assert review.cost and review.cost > 0
    assert review.model == PLANNER_MODEL


def test_review_stage_mask_angle_null_means_no_mask():
    client = FakePlannerClient(_once(_message(_review_reply_json(mask_angle=None))))
    review = review_stage(_plan(), "focal", [_png_bytes()], "draft", client)
    assert review.mask_angle is None


def test_review_stage_with_no_prior_renders_is_a_caller_error():
    client = FakePlannerClient(_once(_message(_review_reply_json())))
    with pytest.raises(PlannerError, match="no prior renders"):
        review_stage(_plan(), "focal", [], "draft", client)
    assert client.calls == []                      # never reached the wire


def test_review_stage_bad_placement_shape_raises_planner_error():
    client = FakePlannerClient(_once(_message(json.dumps(
        {"prompt": "p", "anchor": [0.5], "scale": 1.0,
         "offset": [0, 0], "mask_angle": None}))))
    with pytest.raises(PlannerError, match="schema"):
        review_stage(_plan(), "focal", [_png_bytes()], "draft", client)
