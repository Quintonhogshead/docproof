"""docproof/cover/atelier.py: the agent that builds one concept.

Two halves, tested separately:

- The TOOLS are ordinary methods on a _Session, so most tests call them with
  a plain dict and no SDK in sight. That is where the contracts live that
  actually protect money and pixels: the budget refusals, the guarded spec
  paths, the opaque-cutout note, "finish needs something composed first".
- The SESSION is driven through a scripted fake `claude_agent_sdk` (the same
  shape tests/test_canvas_assistant.py uses), so build_concept's own
  contract -- never raise for one concept's problem, always return a
  ConceptOutcome, distinguish an agent that finished from one that stopped --
  is exercised without spawning anything.

No network, no real image bytes, no real render pixels.
"""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest

from docproof.cover import atelier
from docproof.cover.archetypes import ARCHETYPES
from docproof.cover.atelier import (AtelierUnavailable, Budget, ConceptOutcome,
                                    build_concept)
from docproof.cover.director import ConceptAssignment
from docproof.cover.model import (Brief, Direction, Palette, RenderReport,
                                  build_spec)

FAKE_IMAGE = object()
IMAGE_CLIENT = object()


def _run(coro):
    return asyncio.run(coro)


def _brief() -> Brief:
    return Brief(title="Longsword", author="Q. Johnson", genre="literary",
                 concepts=1)


def _direction(archetype="probe_scene") -> Direction:
    return Direction(
        concept_name="The Piece", rationale="The book's own key image.",
        archetype=archetype,
        palette=Palette(background="#101010", primary="#e8e2d6",
                        accent="#e0621f", text="#f4efe6", scrim="#000000"),
        title_font="Libre Caslon Display", author_font="Space Mono",
        art_prompts=[{"slot": "background", "prompt": "An empty apron.",
                      "treatment": "none", "mask_intent": ""}],
        texture=False, recipe="", type_move="", emphasis_word="")


def _assignment() -> ConceptAssignment:
    return ConceptAssignment(direction=_direction(),
                             execution_notes="Generate the ground.",
                             done_when="It reads at thumbnail size.")


def _spec(archetype="probe_scene"):
    return build_spec(_direction(archetype), _brief(), ARCHETYPES[archetype])


def _report() -> RenderReport:
    return RenderReport(contrast={"title": 12.0}, scrim_final={0: 0.0},
                        fitted_sizes={"title": 0.1}, warnings=[],
                        occlusion={}, dead_band_frac=0.1, adjustments=[])


def _fake_save_renders(image, job_dir, version, concept):
    rel = f"renders/v{version}_c{concept}.png"
    (Path(job_dir) / rel).parent.mkdir(parents=True, exist_ok=True)
    (Path(job_dir) / rel).write_bytes(_PNG)
    return [rel]


# A real 1x1 PNG, so the `look` tool's Pillow path runs for real.
_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
        b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82")


def _session(tmp_path, monkeypatch, *, budget=None, generate=None,
             alpha=True, archetype="probe_scene"):
    monkeypatch.setattr(atelier, "compose",
                        lambda spec, job_dir: (FAKE_IMAGE, _report()))
    monkeypatch.setattr(atelier, "has_real_alpha", lambda png: alpha)
    if generate is not None:
        monkeypatch.setattr(atelier, "generate", generate)
    return atelier._Session(
        job_dir=tmp_path, index=0, brief=_brief(), spec=_spec(archetype),
        archetype=ARCHETYPES[archetype], image_client=IMAGE_CLIENT,
        assemble_prompt=lambda slot, arch: f"ASSEMBLED::{slot.prompt}",
        budget=budget or Budget(), sem=None,
        save_renders=_fake_save_renders)


# -- Budget --------------------------------------------------------------------

def test_the_budget_stops_on_generations_before_dollars_at_the_draft_tier():
    b = Budget()
    for _ in range(atelier.MAX_GENERATIONS):
        b.charge("1K")
    assert b.refusal("1K") is not None
    assert "of 12 generations used" in b.refusal("1K")


def test_the_budget_stops_on_dollars_when_the_agent_escalates_every_roll():
    """The count alone would let 12 x 2K through at $0.60."""
    b = Budget()
    while b.refusal("2K") is None:
        b.charge("2K")
    assert b.generations < atelier.MAX_GENERATIONS
    assert b.usd == pytest.approx(atelier.MAX_ART_USD)
    assert "Drop to a cheaper tier" in b.refusal("2K")


def test_a_nearly_spent_budget_still_allows_a_cheaper_roll():
    """The refusal is priced per TIER, so an agent told it cannot afford a
    2K roll is not thereby told it cannot afford anything."""
    b = Budget(max_generations=12, max_usd=0.12)
    b.charge("2K")
    b.charge("1K")                          # 0.08 spent
    assert b.refusal("2K") is not None      # 0.08 + 0.05 > 0.12 -- refused
    assert b.refusal("1K") is None          # 0.08 + 0.03 < 0.12 -- allowed


# -- paint ---------------------------------------------------------------------

def test_paint_charges_the_tier_it_rolled_and_attaches_the_asset(tmp_path,
                                                                 monkeypatch):
    seen = {}

    def generate(client, prompt, *, transparent=False, resolution="2K"):
        seen.update(client=client, prompt=prompt, resolution=resolution)
        return _PNG

    s = _session(tmp_path, monkeypatch, generate=generate)
    out = _run(s.paint({"slot": "background", "prompt": "A cold sea.",
                        "resolution": "1K"}))

    assert seen["client"] is IMAGE_CLIENT
    assert seen["resolution"] == "1K"
    # the pipeline's own assembler ran, so the negative suffix and the
    # archetype's composition note are not bypassed by the agent
    assert seen["prompt"] == "ASSEMBLED::A cold sea."
    assert s.budget.generations == 1
    assert s.budget.usd == pytest.approx(0.03)
    slot = next(a for a in s.spec.art if a.id == "background")
    assert slot.asset == "assets/c0_background.png"
    assert (tmp_path / slot.asset).read_bytes() == _PNG
    assert s.ledger[0]["usd"] == pytest.approx(0.03)
    assert "(1K, atelier)" in s.ledger[0]["detail"]
    assert "Painted background at 1K" in out["content"][0]["text"]


def test_paint_is_refused_once_the_budget_is_gone_and_costs_nothing(
        tmp_path, monkeypatch):
    calls = []

    def generate(*a, **k):
        calls.append(1)
        return _PNG

    b = Budget()
    for _ in range(atelier.MAX_GENERATIONS):
        b.charge("1K")
    s = _session(tmp_path, monkeypatch, budget=b, generate=generate)
    out = _run(s.paint({"slot": "background", "prompt": "x",
                        "resolution": "1K"}))

    assert out["is_error"] is True
    assert "Budget spent" in out["content"][0]["text"]
    assert calls == [], "a refused generation still called the image model"


def test_paint_refuses_an_unknown_slot_and_names_the_real_ones(tmp_path,
                                                               monkeypatch):
    s = _session(tmp_path, monkeypatch)
    out = _run(s.paint({"slot": "nope", "prompt": "x"}))
    assert out["is_error"] is True
    assert "background" in out["content"][0]["text"]


def test_paint_refuses_a_resolution_the_cost_table_cannot_price(tmp_path,
                                                                monkeypatch):
    s = _session(tmp_path, monkeypatch)
    out = _run(s.paint({"slot": "background", "prompt": "x",
                        "resolution": "8K"}))
    assert out["is_error"] is True
    assert s.budget.generations == 0


def test_an_opaque_cutout_is_reported_rather_than_silently_composed(
        tmp_path, monkeypatch):
    """§5.2.3: the transparency feature is in preview, and a cutout that
    comes back opaque changes the layer order underneath the agent. It has
    to be told, or it judges a composite it does not understand."""
    s = _session(tmp_path, monkeypatch, archetype="probe_sandwich",
                 alpha=False, generate=lambda *a, **k: _PNG)
    out = _run(s.paint({"slot": "focal", "prompt": "A man from behind.",
                        "resolution": "1K"}))
    assert "came back\nOPAQUE" in out["content"][0]["text"].replace(" ", "\n") \
        or "OPAQUE" in out["content"][0]["text"]


def test_a_failed_generation_is_a_sentence_not_a_charge(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("the model refused")

    s = _session(tmp_path, monkeypatch, generate=boom)
    out = _run(s.paint({"slot": "background", "prompt": "x"}))
    assert out["is_error"] is True
    assert "the model refused" in out["content"][0]["text"]
    assert s.budget.generations == 0
    assert s.ledger == []


# -- render / look / edit_spec -------------------------------------------------

def test_render_reports_the_autopilot_as_a_defect_to_fix_at_the_source(
        tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    monkeypatch.setattr(atelier, "compose", lambda spec, job_dir: (
        FAKE_IMAGE, RenderReport(
            contrast={"title": 3.1}, scrim_final={0: 0.3},
            fitted_sizes={}, warnings=["dead band"], occlusion={},
            dead_band_frac=0.31, adjustments=["title scrim raised to 0.3"])))
    text = _run(s.render({}))["content"][0]["text"]
    assert "AUTOPILOT" in text
    assert "title scrim raised to 0.3" in text
    assert "WARNINGS" in text
    assert s.composed is True


def test_look_composes_first_and_returns_a_downscaled_image(tmp_path,
                                                            monkeypatch):
    s = _session(tmp_path, monkeypatch)
    out = _run(s.look({}))
    blocks = out["content"]
    assert blocks[0]["type"] == "text"
    assert blocks[1]["type"] == "image"
    assert blocks[1]["mimeType"] == "image/png"
    assert s.composed is True


def test_edit_spec_applies_recomposes_and_reports(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    out = _run(s.edit_spec({"edits": [
        {"path": "palette.accent", "value": '"#a83250"'}]}))
    assert s.spec.palette.accent == "#a83250"
    assert "Edits applied and recomposed" in out["content"][0]["text"]
    assert "contrast" in out["content"][0]["text"]


def test_edit_spec_refuses_the_guarded_paths_code_alone_may_write(
        tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    before = s.spec.version
    out = _run(s.edit_spec({"edits": [{"path": "version", "value": "99"}]}))
    assert s.spec.version == before
    assert "guarded" in out["content"][0]["text"]


def test_edits_that_do_not_add_up_to_a_valid_spec_keep_none_of_them(
        tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    before = s.spec.model_dump()
    out = _run(s.edit_spec({"edits": [
        {"path": "palette.accent", "value": '"#a83250"'},
        {"path": "palette.text", "value": '"not-a-colour"'}]}))
    assert out["is_error"] is True
    assert s.spec.model_dump() == before, "a half-applied spec survived"


def test_edit_spec_needs_edits(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    assert _run(s.edit_spec({"edits": []}))["is_error"] is True


# -- finish --------------------------------------------------------------------

def test_finish_needs_a_summary(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    _run(s.render({}))
    assert _run(s.finish({"summary": "  "}))["is_error"] is True
    assert s.finished is False


def test_finish_refuses_before_anything_has_been_composed(tmp_path,
                                                          monkeypatch):
    s = _session(tmp_path, monkeypatch)
    out = _run(s.finish({"summary": "done"}))
    assert out["is_error"] is True
    assert s.finished is False


def test_finish_records_the_summary(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    _run(s.render({}))
    _run(s.finish({"summary": "Shipped the piece; weak at 100px."}))
    assert s.finished is True
    assert "weak at 100px" in s.summary


# -- read_spec / archetype_info / budget --------------------------------------

def test_read_spec_hands_back_the_whole_document(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    parsed = json.loads(_run(s.read_spec({}))["content"][0]["text"])
    assert parsed["archetype"] == "probe_scene"


def test_archetype_info_names_the_slots_the_agent_may_paint(tmp_path,
                                                            monkeypatch):
    s = _session(tmp_path, monkeypatch)
    text = _run(s.archetype_info({}))["content"][0]["text"]
    assert "probe_scene" in text
    assert "background" in text
    assert "TEXT SLOTS" in text


# -- build_concept, through a scripted fake SDK --------------------------------

class _TextBlock:
    def __init__(self, text):
        self.text = text


class _AssistantMessage:
    def __init__(self, content):
        self.content = content
        self.parent_tool_use_id = None


class _ResultMessage:
    def __init__(self, cost=0.0, result=""):
        self.total_cost_usd = cost
        self.result = result


def _fake_sdk(script=None, raise_on_query=None):
    """A claude_agent_sdk stand-in whose `query` runs a scripted turn: each
    entry is a (tool_name, args) pair invoked against the real session."""
    import types

    mod = types.SimpleNamespace()
    mod.TextBlock = _TextBlock
    mod.AssistantMessage = _AssistantMessage
    mod.ResultMessage = _ResultMessage
    mod.CLINotFoundError = type("CLINotFoundError", (Exception,), {})
    mod.ProcessError = type("ProcessError", (Exception,), {})
    mod.ResultError = type("ResultError", (Exception,), {})
    handlers: dict = {}

    def tool(name, description, schema):
        def wrap(handler):
            handlers[name] = handler
            return {"name": name, "handler": handler}
        return wrap

    mod.tool = tool
    mod.create_sdk_mcp_server = lambda *, name, tools: {"name": name}
    mod.ClaudeAgentOptions = lambda **kw: kw

    async def query(*, prompt, options):
        if raise_on_query is not None:
            raise raise_on_query
        async for _ in prompt:
            pass
        for name, args in (script or []):
            await handlers[name](args)
        yield _AssistantMessage([_TextBlock("working")])
        yield _ResultMessage(cost=0.0, result="done")

    mod.query = query
    return mod


def _build(tmp_path, monkeypatch, sdk, **kw):
    monkeypatch.setattr(atelier, "_sdk", lambda: sdk)
    monkeypatch.setattr(atelier, "_require_login", lambda: None)
    monkeypatch.setattr(atelier, "compose",
                        lambda spec, job_dir: (FAKE_IMAGE, _report()))
    monkeypatch.setattr(atelier, "has_real_alpha", lambda png: True)
    monkeypatch.setattr(atelier, "generate", lambda *a, **k: _PNG)
    return _run(build_concept(
        job_dir=tmp_path, index=0, brief=_brief(), assignment=_assignment(),
        spec=_spec(), image_client=IMAGE_CLIENT,
        assemble_prompt=lambda slot, arch: slot.prompt,
        save_renders=_fake_save_renders, **kw))


def test_an_agent_that_paints_renders_and_finishes_comes_back_finished(
        tmp_path, monkeypatch):
    sdk = _fake_sdk(script=[
        ("paint", {"slot": "background", "prompt": "A cold sea.",
                   "resolution": "1K"}),
        ("render", {}),
        ("finish", {"summary": "Shipped it."})])
    outcome = _build(tmp_path, monkeypatch, sdk)

    assert isinstance(outcome, ConceptOutcome)
    assert outcome.finished is True
    assert outcome.error is None
    assert outcome.summary == "Shipped it."
    assert outcome.renders == ["renders/v1_c0.png"]
    assert outcome.report is not None
    assert any(r["kind"] == "image" for r in outcome.ledger)


def test_an_agent_that_never_calls_finish_still_returns_its_cover(
        tmp_path, monkeypatch):
    sdk = _fake_sdk(script=[("render", {})])
    outcome = _build(tmp_path, monkeypatch, sdk)

    assert outcome.finished is False
    assert outcome.renders, "the composed cover was thrown away"
    assert "ran out of turns" in outcome.error


def test_an_agent_that_composed_nothing_says_so(tmp_path, monkeypatch):
    outcome = _build(tmp_path, monkeypatch, _fake_sdk(script=[]))
    assert outcome.finished is False
    assert outcome.renders == []
    assert "never composed" in outcome.error


def test_a_dead_session_is_one_concepts_problem_not_a_raise(tmp_path,
                                                            monkeypatch):
    sdk = _fake_sdk(raise_on_query=RuntimeError("transport died"))
    outcome = _build(tmp_path, monkeypatch, sdk)
    assert outcome.error is not None
    assert "transport died" in outcome.error
    assert outcome.spec is not None      # the caller still has a spec to keep


def test_no_cli_is_the_whole_jobs_problem_and_is_raised(tmp_path, monkeypatch):
    sdk = _fake_sdk()
    err = sdk.CLINotFoundError("no cli")

    async def query(*, prompt, options):
        raise err
        yield                                        # pragma: no cover
    sdk.query = query

    with pytest.raises(AtelierUnavailable, match="claude-code"):
        _build(tmp_path, monkeypatch, sdk)


def test_a_signed_out_cli_is_raised_with_the_setup_token_remedy(tmp_path,
                                                                monkeypatch):
    sdk = _fake_sdk()

    async def query(*, prompt, options):
        raise sdk.ResultError("Not logged in")
        yield                                        # pragma: no cover
    sdk.query = query

    with pytest.raises(AtelierUnavailable, match="setup-token"):
        _build(tmp_path, monkeypatch, sdk)


def test_a_missing_sdk_is_a_sentence_saying_how_to_install_it(monkeypatch,
                                                              tmp_path):
    def no_module(name):
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr(importlib, "import_module", no_module)
    with pytest.raises(AtelierUnavailable, match="pip install claude-agent-sdk"):
        _run(build_concept(
            job_dir=tmp_path, index=0, brief=_brief(),
            assignment=_assignment(), spec=_spec(), image_client=IMAGE_CLIENT,
            assemble_prompt=lambda slot, arch: slot.prompt,
            save_renders=_fake_save_renders))


def test_the_agents_world_is_fenced(tmp_path, monkeypatch):
    """No built-in tools, no repo MCP config, no CLAUDE.md, and a cwd that is
    the job directory rather than the source tree -- and, above all, an
    environment with the API key blanked so a subscription turn cannot
    quietly become a metered one."""
    sdk = _fake_sdk()
    session = _session(tmp_path, monkeypatch)
    options = atelier._options(sdk, session, "claude-opus-5")

    assert options["tools"] == []
    assert options["strict_mcp_config"] is True
    assert options["setting_sources"] == []
    assert options["cwd"] == str(tmp_path)
    assert options["env"]["ANTHROPIC_API_KEY"] == ""
    assert all(t.startswith("mcp__atelier__") for t in options["allowed_tools"])
    assert "mcp__atelier__finish" in options["allowed_tools"]


def test_the_assignment_reaches_the_agents_prompt():
    text = atelier._user_prompt(_brief(), _assignment(), 0)
    assert "Generate the ground." in text          # execution notes
    assert "It reads at thumbnail size." in text   # done_when
    assert "The book's own key image." in text     # rationale
    assert "probe_scene" in text
