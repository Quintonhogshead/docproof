"""docproof/canvas/assistant.py: the resident art director.

Nothing here spawns the real CLI or touches the network -- the SDK seam is one
function (`_sdk`) and every end-to-end test replaces it with a scripted fake.
The suite is about the three promises the AI box makes: plan mode cannot edit
(enforced by which tools exist, not by asking nicely), a refused op comes back
as a sentence the model can act on, and the turn never bills an API key.

No pytest-asyncio in this repo (see tests/test_cover_pipeline.py): the async
handlers and `chat` are driven with a plain asyncio.run() inside ordinary
tests.
"""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest

from docproof.canvas import assistant
from docproof.canvas.assistant import (AssistantUnavailable, ChatResult,
                                       _Session, chat, resolve_model)
from docproof.canvas.model import (ArtLayer, CanvasDoc, Frame, ShapeLayer,
                                   Size, TextLayer)

ART_ID = "ly_aaa111"
TEXT_ID = "ly_bbb222"
SHAPE_ID = "ly_ccc333"

PNG = b"\x89PNG\r\n\x1a\nfake pixels"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIFfake pixels"


def _doc() -> CanvasDoc:
    """Three layers, bottom to top: a plate, a title, a panel."""
    return CanvasDoc(
        job_id="20260831T120000Z-a1b2c3",
        canvas=Size(w=1600, h=2560),
        layers=[
            ArtLayer(id=ART_ID, name="background",
                     frame=Frame(x=0.5, y=0.5, w=1.0, h=1.0),
                     source="assets/c0_background.png",
                     prompt="A smoky brass foundry at dusk, " + "flat "
                            "screen-print, uniform density edge to edge " * 4),
            TextLayer(id=TEXT_ID, name="title",
                      frame=Frame(x=0.5, y=0.7, w=0.84, h=0.18),
                      text="THE LIGHTHOUSE\nAT GULL POINT",
                      family="Playfair Display", size=0.08, color="#f5f1e8"),
            ShapeLayer(id=SHAPE_ID, name="panel", locked=True,
                       frame=Frame(x=0.5, y=0.9, w=0.6, h=0.08),
                       shape="rect", fill="#101820"),
        ])


def _session(mode: str = "act", **kwargs) -> _Session:
    kwargs.setdefault("snapshot_png", None)
    return _Session(job_dir=Path("/tmp/job"), doc=_doc(), mode=mode, **kwargs)


def _run(coro):
    return asyncio.run(coro)


def _names(session: _Session) -> list[str]:
    return [spec.name for spec in session.specs()]


def _body(result: dict) -> str:
    return "\n".join(block["text"] for block in result["content"]
                     if block["type"] == "text")


# -- the tool registry is the enforcement -------------------------------------

def test_act_mode_gets_the_mutating_tools():
    session = _session("act", image_client=lambda: object())
    assert _names(session) == ["inspect", "look", "apply_ops", "rebalance",
                               "reroll", "finalize", "ground_figure"]


def test_plan_mode_registry_excludes_every_mutator():
    # Plan mode's read-only promise is kept by absence, so this is the test
    # that the promise exists at all.
    session = _session("plan", image_client=lambda: object())
    assert _names(session) == ["inspect", "look"]


def test_reroll_is_omitted_when_there_is_no_image_client():
    assert "reroll" not in _names(_session("act", image_client=None))


def test_a_plan_mode_turn_never_holds_an_image_client(monkeypatch):
    doc = _doc()
    captured = {}

    def fake_options(sdk, session, model):
        captured["session"] = session
        return _FakeOptions()

    monkeypatch.setattr(assistant, "_sdk", lambda: _fake_sdk())
    monkeypatch.setattr(assistant, "_require_login", lambda: None)
    monkeypatch.setattr(assistant, "_options", fake_options)
    _run(chat(Path("/tmp/job"), doc, [{"role": "user", "content": "critique"}],
              "plan", image_client=lambda: object()))
    assert captured["session"].image_client is None


# -- inspect ------------------------------------------------------------------

def test_inspect_reports_every_layer_bottom_to_top_with_its_frame():
    session = _session()
    payload = json.loads(_body(_run(session.inspect({}))))
    assert payload["canvas"] == {"w": 1600, "h": 2560}
    assert [l["id"] for l in payload["layers"]] == [ART_ID, TEXT_ID, SHAPE_ID]
    assert [l["index"] for l in payload["layers"]] == [0, 1, 2]
    assert payload["layers"][1]["frame"]["y"] == 0.7
    assert payload["layers"][2]["locked"] is True


def test_inspect_carries_the_fields_that_say_which_layer_this_is():
    payload = json.loads(_body(_run(_session().inspect({}))))
    art, text, shape = payload["layers"]
    assert art["source"] == "assets/c0_background.png"
    assert text["text"] == "THE LIGHTHOUSE\nAT GULL POINT"
    assert text["family"] == "Playfair Display"
    assert text["color"] == "#f5f1e8"
    assert shape["shape"] == "rect" and shape["fill"] == "#101820"


def test_inspect_clips_a_long_plate_prompt_to_its_subject():
    # It has to stay cheap enough to re-call after every batch.
    art = json.loads(_body(_run(_session().inspect({}))))["layers"][0]
    assert art["prompt"].startswith("A smoky brass foundry at dusk")
    assert len(art["prompt"]) <= 121
    assert art["prompt"].endswith("…")


def test_inspect_omits_a_plate_history_that_is_not_there():
    assert "previous_plates" not in \
        json.loads(_body(_run(_session().inspect({}))))["layers"][0]


# -- apply_ops ----------------------------------------------------------------

def test_apply_ops_mutates_the_working_copy_and_records_the_batch():
    session = _session()
    result = _run(session.apply_ops({"ops": [
        {"op": "nudge", "layer_id": TEXT_ID, "dx": -0.2, "dy": 0.04},
        {"op": "set_text", "layer_id": TEXT_ID, "size": 0.09},
    ]}))
    assert result["is_error"] is False
    title = session.doc.layer(TEXT_ID)
    assert title.frame.x == pytest.approx(0.3)
    assert title.size == 0.09
    assert [op["op"] for op in session.ops_applied] == ["nudge", "set_text"]


def test_apply_ops_summarises_in_one_line_naming_the_layers_that_remain():
    session = _session()
    body = _body(_run(session.apply_ops(
        {"ops": [{"op": "remove_layer", "layer_id": TEXT_ID}]}))).strip()
    assert "\n" not in body
    assert "applied 1 op" in body
    assert TEXT_ID not in body
    assert ART_ID in body and SHAPE_ID in body


def test_a_refused_op_comes_back_as_the_sentence_not_an_exception():
    # The model is the thing that can fix this, so it gets ops.py's own words.
    session = _session()
    result = _run(session.apply_ops(
        {"ops": [{"op": "nudge", "layer_id": SHAPE_ID, "dx": 0.1}]}))
    assert result["is_error"] is True
    assert "locked" in _body(result) and "set_layer" in _body(result)


def test_a_refused_batch_lands_nothing_and_records_nothing():
    session = _session()
    _run(session.apply_ops({"ops": [
        {"op": "nudge", "layer_id": TEXT_ID, "dx": -0.2},
        {"op": "nudge", "layer_id": "ly_gone00", "dx": 0.1},
    ]}))
    assert session.doc.layer(TEXT_ID).frame.x == 0.5
    assert session.ops_applied == []


def test_a_hallucinated_field_is_refused_with_the_fields_that_exist():
    result = _run(_session().apply_ops(
        {"ops": [{"op": "nudge", "layer_id": TEXT_ID, "dz": 0.1}]}))
    assert result["is_error"] is True
    assert "dz" in _body(result)


def test_ops_that_are_not_a_list_are_refused_with_an_example():
    result = _run(_session().apply_ops({"ops": "move the title left"}))
    assert result["is_error"] is True
    assert "nudge" in _body(result)


def test_the_recorded_ops_are_copies_the_caller_cannot_rewrite():
    session = _session()
    op = {"op": "nudge", "layer_id": TEXT_ID, "dx": -0.2}
    _run(session.apply_ops({"ops": [op]}))
    op["dx"] = 99.0
    assert session.ops_applied[0]["dx"] == -0.2


# -- look ---------------------------------------------------------------------

def test_look_returns_the_snapshot_as_a_base64_png_image_block():
    import base64
    result = _run(_session(snapshot_png=PNG).look({}))
    block, = result["content"]
    assert block["type"] == "image"
    assert block["mimeType"] == "image/png"
    assert base64.b64decode(block["data"]) == PNG


def test_look_without_a_snapshot_says_so_instead_of_pretending():
    result = _run(_session().look({}))
    assert result["content"][0]["type"] == "text"
    assert "No snapshot" in _body(result)


# -- the declared image type is the real one ----------------------------------

def test_the_two_magic_numbers_are_sniffed_not_assumed():
    assert assistant._image_mime(PNG) == "image/png"
    assert assistant._image_mime(JPEG) == "image/jpeg"


@pytest.mark.parametrize("data", [b"", b"GIF89a", b"not an image at all"])
def test_bytes_that_are_neither_are_still_called_png(data):
    # The canvas has exported PNG its whole life; guessing a third format for
    # an upstream bug would only make the bug harder to see.
    assert assistant._image_mime(data) == "image/png"


def test_look_declares_jpeg_when_the_client_sent_jpeg():
    # The front end can move to JPEG snapshots (~10x smaller) without this
    # tool lying to a vision decoder about what it is handing over.
    block, = _run(_session(snapshot_png=JPEG).look({}))["content"]
    assert block["mimeType"] == "image/jpeg"


def test_the_attached_snapshot_declares_jpeg_too():
    message = assistant._user_message("look at this", JPEG)
    assert message["message"]["content"][1]["source"]["media_type"] == \
        "image/jpeg"


def test_both_attach_sites_agree_about_the_same_bytes():
    # One sniff, two callers: a `look` that disagreed with the message the
    # snapshot rode in on would be two different pictures as far as the model
    # can tell.
    for data in (PNG, JPEG):
        looked, = _run(_session(snapshot_png=data).look({}))["content"]
        attached = assistant._user_message("x", data)["message"]["content"][1]
        assert looked["mimeType"] == attached["source"]["media_type"]


# -- reroll -------------------------------------------------------------------

def test_reroll_calls_regen_and_charges_the_document_not_the_turn(monkeypatch):
    calls = {}

    def fake_reroll(job_dir, doc, layer_id, *, client, prompt=None):
        calls.update(job_dir=job_dir, layer_id=layer_id, client=client,
                     prompt=prompt)
        doc.layer(layer_id).source = "assets/c0_background_r1.png"
        # The real regen charges the document itself (§8's running total);
        # the tool must NOT add the returned price again — cost_usd staying
        # at exactly one call's price below is the double-charge regression.
        doc.cost_usd += 0.05
        return 0.05

    _install_regen(monkeypatch, fake_reroll)
    client = object()
    session = _session(image_client=lambda: client)
    body = _body(_run(session.reroll(
        {"layer_id": ART_ID, "prompt": "the same foundry, colder"})))
    assert calls["layer_id"] == ART_ID and calls["client"] is client
    assert calls["prompt"] == "the same foundry, colder"
    assert "assets/c0_background_r1.png" in body and "$0.05" in body
    assert session.doc.cost_usd == pytest.approx(0.05)


def test_a_failing_reroll_is_a_tool_result_not_a_crashed_turn(monkeypatch):
    def boom(job_dir, doc, layer_id, *, client, prompt=None):
        raise RuntimeError("the image service refused")

    _install_regen(monkeypatch, boom)
    result = _run(_session(image_client=lambda: object()).reroll(
        {"layer_id": ART_ID}))
    assert result["is_error"] is True
    assert "the image service refused" in _body(result)


def test_reroll_without_a_layer_id_says_what_it_needs(monkeypatch):
    _install_regen(monkeypatch, lambda *a, **k: 0.0)
    result = _run(_session(image_client=lambda: object()).reroll({}))
    assert result["is_error"] is True
    assert "layer_id" in _body(result)


def test_the_reroll_tool_calls_regen_the_way_regen_is_actually_declared():
    """The tool and the regeneration lane were written in parallel, so the
    keyword contract is asserted rather than assumed -- a rename there should
    fail here, not in front of a person mid-turn."""
    import inspect as _inspect

    from docproof.canvas import regen
    params = _inspect.signature(regen.reroll).parameters
    assert list(params)[:3] == ["job_dir", "doc", "layer_id"]
    assert params["client"].kind is _inspect.Parameter.KEYWORD_ONLY
    assert params["prompt"].kind is _inspect.Parameter.KEYWORD_ONLY
    assert params["prompt"].default is None


def _install_regen(monkeypatch, reroll=None, **verbs):
    """Stand a `docproof.canvas.regen` up in sys.modules.

    The real module is written by another lane and imports an image client;
    each tool only ever needs its own function, and importing them lazily
    (per handler, by name) is exactly what makes this substitution possible.
    A verb the test does not name is simply absent from the stand-in, so a
    test only stands up the one it is about."""
    import sys
    import types
    module = types.ModuleType("docproof.canvas.regen")
    if reroll is not None:
        module.reroll = reroll
    for name, func in verbs.items():
        setattr(module, name, func)
    monkeypatch.setitem(sys.modules, "docproof.canvas.regen", module)
    return module


# -- model resolution ---------------------------------------------------------

def test_the_default_model_is_opus_5(monkeypatch):
    monkeypatch.delenv(assistant.MODEL_ENV, raising=False)
    assert resolve_model() == "claude-opus-5"


def test_the_env_var_overrides_the_default(monkeypatch):
    monkeypatch.setenv(assistant.MODEL_ENV, "claude-sonnet-4-5")
    assert resolve_model() == "claude-sonnet-4-5"


def test_an_explicit_argument_outranks_the_env_var(monkeypatch):
    monkeypatch.setenv(assistant.MODEL_ENV, "claude-sonnet-4-5")
    assert resolve_model("claude-haiku-4-5") == "claude-haiku-4-5"


def test_a_blank_env_var_falls_through_to_the_default(monkeypatch):
    monkeypatch.setenv(assistant.MODEL_ENV, "  ")
    assert resolve_model() == "claude-opus-5"


# -- the prompt ---------------------------------------------------------------

def test_a_lone_user_message_is_the_whole_prompt():
    assert assistant._prompt_text(
        [{"role": "user", "content": "move the title left"}]) == \
        "move the title left"


def test_earlier_turns_arrive_as_context_marked_already_done():
    text = assistant._prompt_text([
        {"role": "user", "content": "scrim the title"},
        {"role": "assistant", "content": "done, strength 0.4"},
        {"role": "user", "content": "now move it left"},
    ])
    assert "<transcript>" in text
    assert "do not repeat them" in text
    assert text.endswith("now move it left")


def test_a_transcript_that_does_not_end_with_the_user_is_refused():
    with pytest.raises(ValueError, match="last message must be the user"):
        assistant._prompt_text([{"role": "assistant", "content": "hi"}])


def test_the_snapshot_rides_on_the_user_message_as_an_image_block():
    import base64
    message = assistant._user_message("look at this", PNG)
    blocks = message["message"]["content"]
    assert blocks[0] == {"type": "text", "text": "look at this"}
    assert blocks[1]["source"]["media_type"] == "image/png"
    assert base64.b64decode(blocks[1]["source"]["data"]) == PNG


def test_without_a_snapshot_the_message_is_plain_text():
    assert assistant._user_message("hello", None)["message"]["content"] == \
        "hello"


# -- the system prompt --------------------------------------------------------

def test_the_system_prompt_carries_the_doctrine_the_geometry_and_the_ops():
    prompt = assistant.SYSTEM_PROMPT
    assert "standing on something" in prompt        # the cardinal rule
    assert "CENTER of the layer's box" in prompt    # the geometry contract
    assert "reorder_layer" in prompt                # the op vocabulary
    assert "PLAN MODE" in prompt and "ACT MODE" in prompt


def test_the_system_prompt_names_every_op_the_module_actually_implements():
    # A vocabulary the model cannot see is a vocabulary it will not use, and
    # one it can see but that does not exist is a batch that gets refused.
    from docproof.canvas.ops import OP_NAMES
    missing = [name for name in OP_NAMES if name not in assistant.SYSTEM_PROMPT]
    assert missing == []


def test_the_system_prompt_teaches_the_two_new_contracts():
    prompt = assistant.SYSTEM_PROMPT
    assert "corners" in prompt and "TL, TR" in prompt
    assert "history strip" in prompt                # set_art is a swap-back
    assert "IN PLACE" in prompt                     # not remove-and-re-add


def test_the_apply_ops_description_lists_the_typed_verbs():
    spec, = [s for s in _session("act").specs() if s.name == "apply_ops"]
    for name in ("set_scrim", "set_frame_style", "set_shape", "corners"):
        assert name in spec.description


# -- the billing guard --------------------------------------------------------

def test_the_child_environment_blanks_the_api_key():
    # A leaked key turns a $0 subscription turn into a metered API bill with
    # no visible symptom -- this is the whole guard.
    assert assistant._child_env()["ANTHROPIC_API_KEY"] == ""


def test_the_child_environment_leaves_the_oauth_token_alone():
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in assistant._child_env()


def test_the_options_fence_the_agent_to_the_canvas_tools_and_the_job_dir():
    sdk = _fake_sdk()
    session = _session("act", image_client=lambda: object())
    options = assistant._options(sdk, session, "claude-opus-5")
    assert options.tools == []
    assert options.allowed_tools == [
        "mcp__canvas__inspect", "mcp__canvas__look",
        "mcp__canvas__apply_ops", "mcp__canvas__rebalance",
        "mcp__canvas__reroll", "mcp__canvas__finalize",
        "mcp__canvas__ground_figure"]
    assert options.setting_sources == []
    assert options.strict_mcp_config is True
    assert options.permission_mode == "bypassPermissions"
    assert options.cwd == "/tmp/job"
    assert options.env == {"ANTHROPIC_API_KEY": ""}
    assert options.max_turns == assistant.MAX_TURNS


def test_the_mode_is_stamped_onto_the_system_prompt():
    options = assistant._options(_fake_sdk(), _session("plan"), "m")
    assert options.system_prompt.endswith("You are in PLAN MODE.")


# -- availability -------------------------------------------------------------

def test_a_missing_sdk_is_a_sentence_saying_how_to_install_it(monkeypatch):
    def no_module(name):
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr(importlib, "import_module", no_module)
    with pytest.raises(AssistantUnavailable) as excinfo:
        _run(chat(Path("/tmp/job"), _doc(),
                  [{"role": "user", "content": "hi"}], "act"))
    assert "pip install claude-agent-sdk" in str(excinfo.value)


def test_a_machine_with_no_claude_login_is_told_to_set_a_token(monkeypatch,
                                                               tmp_path):
    monkeypatch.setattr(assistant, "_sdk", lambda: _fake_sdk())
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
    with pytest.raises(AssistantUnavailable, match="setup-token"):
        _run(chat(Path("/tmp/job"), _doc(),
                  [{"role": "user", "content": "hi"}], "act"))


def test_an_oauth_token_is_login_enough(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-whatever")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))
    assistant._require_login()


def test_an_unknown_mode_is_refused_before_anything_is_spawned():
    with pytest.raises(ValueError, match="'plan' or 'act'"):
        _run(chat(Path("/tmp/job"), _doc(),
                  [{"role": "user", "content": "hi"}], "critique"))


# -- end to end, against a scripted SDK ---------------------------------------

class _FakeOptions:
    """ClaudeAgentOptions' stand-in: whatever it is handed, verbatim."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeTool:
    def __init__(self, name, description, schema, handler):
        self.name = name
        self.description = description
        self.schema = schema
        self.handler = handler


class _FakeTextBlock:
    def __init__(self, text):
        self.text = text


class _FakeAssistantMessage:
    def __init__(self, content, parent_tool_use_id=None):
        self.content = content
        self.parent_tool_use_id = parent_tool_use_id


class _FakeResultMessage:
    def __init__(self, result=None, total_cost_usd=None):
        self.result = result
        self.total_cost_usd = total_cost_usd


class _FakeServer:
    def __init__(self, name, tools):
        self.name = name
        self.tools = {t.name: t for t in tools}


class _FakeCLINotFound(Exception):
    pass


def _fake_sdk(script=None, seen=None):
    """A claude_agent_sdk stand-in whose `query` runs a scripted turn.

    `script` is a list of (tool_name, args) the fake model "calls" before it
    answers -- which is how the end-to-end tests prove that a tool call
    reaches the working document."""
    import types

    module = types.ModuleType("claude_agent_sdk_fake")
    module.ClaudeAgentOptions = _FakeOptions
    module.AssistantMessage = _FakeAssistantMessage
    module.ResultMessage = _FakeResultMessage
    module.TextBlock = _FakeTextBlock
    module.CLINotFoundError = _FakeCLINotFound
    module.tool = lambda name, description, schema: (
        lambda handler: _FakeTool(name, description, schema, handler))
    module.create_sdk_mcp_server = lambda *, name, tools: _FakeServer(name,
                                                                     tools)

    async def query(*, prompt, options):
        if seen is not None:
            seen["prompt"] = [m async for m in prompt]
            seen["options"] = options
        else:
            async for _ in prompt:
                pass
        servers = getattr(options, "mcp_servers", {})
        server = servers.get(assistant.SERVER_NAME)
        for name, args in (script or []):
            result = await server.tools[name].handler(args)
            if seen is not None:
                seen.setdefault("results", []).append(result)
        yield _FakeAssistantMessage([_FakeTextBlock("thinking out loud")])
        yield _FakeAssistantMessage([_FakeTextBlock("Moved the title left.")])
        yield _FakeResultMessage(result="Moved the title left.",
                                 total_cost_usd=0.0)

    module.query = query
    return module


def _chat(monkeypatch, doc, messages, mode, sdk=None, **kwargs) -> ChatResult:
    monkeypatch.setattr(assistant, "_sdk", lambda: sdk or _fake_sdk())
    monkeypatch.setattr(assistant, "_require_login", lambda: None)
    return _run(chat(Path("/tmp/job"), doc, messages, mode, **kwargs))


def test_a_tool_call_in_the_turn_reaches_the_returned_document(monkeypatch):
    doc = _doc()
    sdk = _fake_sdk(script=[
        ("inspect", {}),
        ("apply_ops", {"ops": [
            {"op": "nudge", "layer_id": TEXT_ID, "dx": -0.2}]}),
    ])
    result = _chat(monkeypatch, doc, [{"role": "user", "content": "left"}],
                   "act", sdk=sdk)
    assert result.reply == "Moved the title left."
    assert result.doc.layer(TEXT_ID).frame.x == pytest.approx(0.3)
    assert [op["op"] for op in result.ops_applied] == ["nudge"]
    assert result.cost_usd == 0.0


def test_the_callers_document_is_never_touched(monkeypatch):
    doc = _doc()
    sdk = _fake_sdk(script=[("apply_ops", {"ops": [
        {"op": "nudge", "layer_id": TEXT_ID, "dx": -0.2}]})])
    result = _chat(monkeypatch, doc, [{"role": "user", "content": "left"}],
                   "act", sdk=sdk)
    assert doc.layer(TEXT_ID).frame.x == 0.5
    assert result.doc is not doc


def test_a_reported_cost_is_passed_through_rather_than_estimated(monkeypatch):
    sdk = _fake_sdk()

    async def paid(*, prompt, options):
        async for _ in prompt:
            pass
        yield _FakeAssistantMessage([_FakeTextBlock("done")])
        yield _FakeResultMessage(result="done", total_cost_usd=0.0731)

    sdk.query = paid
    result = _chat(monkeypatch, _doc(), [{"role": "user", "content": "hi"}],
                   "act", sdk=sdk)
    assert result.cost_usd == pytest.approx(0.0731)


def test_a_result_with_no_cost_reports_zero(monkeypatch):
    sdk = _fake_sdk()

    async def free(*, prompt, options):
        async for _ in prompt:
            pass
        yield _FakeResultMessage(result="done", total_cost_usd=None)

    sdk.query = free
    assert _chat(monkeypatch, _doc(), [{"role": "user", "content": "hi"}],
                 "act", sdk=sdk).cost_usd == 0.0


def test_the_last_assistant_text_stands_in_when_the_result_says_nothing(
        monkeypatch):
    sdk = _fake_sdk()

    async def silent_result(*, prompt, options):
        async for _ in prompt:
            pass
        yield _FakeAssistantMessage([_FakeTextBlock("The scrim is too heavy.")])
        yield _FakeResultMessage(result=None, total_cost_usd=0.0)

    sdk.query = silent_result
    assert _chat(monkeypatch, _doc(), [{"role": "user", "content": "hi"}],
                 "plan", sdk=sdk).reply == "The scrim is too heavy."


def test_a_turn_that_says_nothing_at_all_still_answers_the_person(monkeypatch):
    sdk = _fake_sdk()

    async def mute(*, prompt, options):
        async for _ in prompt:
            pass
        yield _FakeResultMessage(result=None, total_cost_usd=0.0)

    sdk.query = mute
    assert "try asking again" in _chat(
        monkeypatch, _doc(), [{"role": "user", "content": "hi"}], "plan",
        sdk=sdk).reply


def test_the_snapshot_and_the_transcript_reach_the_query(monkeypatch):
    seen = {}
    sdk = _fake_sdk(seen=seen)
    _chat(monkeypatch, _doc(), [
        {"role": "user", "content": "scrim the title"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "now move it left"},
    ], "act", sdk=sdk, snapshot_png=PNG)
    message, = seen["prompt"]
    text, image = message["message"]["content"]
    assert "scrim the title" in text["text"]
    assert text["text"].endswith("now move it left")
    assert image["type"] == "image"


def test_a_plan_mode_query_is_handed_only_the_read_only_tools(monkeypatch):
    seen = {}
    sdk = _fake_sdk(seen=seen)
    _chat(monkeypatch, _doc(), [{"role": "user", "content": "critique this"}],
          "plan", sdk=sdk, image_client=lambda: object())
    server = seen["options"].mcp_servers[assistant.SERVER_NAME]
    assert sorted(server.tools) == ["inspect", "look"]
    assert seen["options"].allowed_tools == [
        "mcp__canvas__inspect", "mcp__canvas__look"]


def test_the_model_is_resolved_onto_the_options(monkeypatch):
    seen = {}
    monkeypatch.setenv(assistant.MODEL_ENV, "claude-sonnet-4-5")
    _chat(monkeypatch, _doc(), [{"role": "user", "content": "hi"}], "act",
          sdk=_fake_sdk(seen=seen))
    assert seen["options"].model == "claude-sonnet-4-5"


def test_a_missing_cli_becomes_the_sentence_that_names_the_install(monkeypatch):
    sdk = _fake_sdk()

    async def no_cli(*, prompt, options):
        async for _ in prompt:
            pass
        raise _FakeCLINotFound("Claude Code not found")
        yield                                               # pragma: no cover

    sdk.query = no_cli
    monkeypatch.setattr(assistant, "_sdk", lambda: sdk)
    monkeypatch.setattr(assistant, "_require_login", lambda: None)
    with pytest.raises(AssistantUnavailable, match="claude-code"):
        _run(chat(Path("/tmp/job"), _doc(),
                  [{"role": "user", "content": "hi"}], "act"))


def test_subagent_chatter_never_becomes_the_reply(monkeypatch):
    sdk = _fake_sdk()

    async def nested(*, prompt, options):
        async for _ in prompt:
            pass
        yield _FakeAssistantMessage([_FakeTextBlock("the real answer")])
        yield _FakeAssistantMessage([_FakeTextBlock("a subagent muttering")],
                                    parent_tool_use_id="toolu_1")
        yield _FakeResultMessage(result=None, total_cost_usd=0.0)

    sdk.query = nested
    assert _chat(monkeypatch, _doc(), [{"role": "user", "content": "hi"}],
                 "plan", sdk=sdk).reply == "the real answer"


# -- the plate verbs: who is registered, and when -----------------------------
#
# The three M2 verbs (finalize, ground_figure, rebalance) join reroll as tools
# that are not ops. Two rules divide them, and both are enforced by the
# registry rather than by asking the model nicely: plan mode gets none of them,
# and the ones that SPEND need an image lane while the one that measures does
# not.

def test_rebalance_is_registered_without_an_image_client():
    # It makes no vendor call, so gating it behind a key would put the
    # cheapest way to answer "is this plate too dark" out of reach of exactly
    # the session that cannot afford the expensive one.
    assert "rebalance" in _names(_session("act", image_client=None))


def test_the_spending_verbs_are_omitted_when_there_is_no_image_client():
    names = _names(_session("act", image_client=None))
    assert names == ["inspect", "look", "apply_ops", "rebalance"]


def test_the_fake_lane_registers_the_spending_verbs_without_a_key(monkeypatch):
    # DOCPROOF_CANVAS_FAKE_IMAGING exists so the whole loop runs on a machine
    # with no OpenAI key at all -- a registry that still hid the verbs would
    # leave the assistant as the one part of that loop nobody could exercise.
    from docproof.canvas import regen
    monkeypatch.setenv(regen.FAKE_ENV, "1")
    assert _names(_session("act", image_client=None)) == [
        "inspect", "look", "apply_ops", "rebalance", "reroll", "finalize",
        "ground_figure"]


def test_plan_mode_stays_read_only_even_in_the_fake_lane(monkeypatch):
    from docproof.canvas import regen
    monkeypatch.setenv(regen.FAKE_ENV, "1")
    assert _names(_session("plan", image_client=lambda: object())) == [
        "inspect", "look"]


def test_each_verbs_description_says_what_it_costs_and_what_it_is_for():
    specs = {s.name: s.description
             for s in _session("act", image_client=lambda: object()).specs()}
    assert "full quality" in specs["finalize"]
    assert "composition anchored" in specs["finalize"]
    assert "contact shadow" in specs["ground_figure"]
    assert "bottom of the plate" in specs["ground_figure"]
    assert "Free" in specs["rebalance"]
    assert "no image call" in specs["rebalance"]
    for name in ("finalize", "ground_figure", "reroll"):
        assert "money" in specs[name]


# -- finalize -----------------------------------------------------------------

def test_finalize_calls_regen_and_charges_the_document_not_the_turn(
        monkeypatch):
    calls = {}

    def fake_finalize(job_dir, doc, layer_id, *, client, prompt=None):
        calls.update(job_dir=job_dir, layer_id=layer_id, client=client,
                     prompt=prompt)
        doc.layer(layer_id).source = "assets/canvas_ly_aaa111_2.png"
        # regen charges the document itself; the tool must not add the
        # returned price a second time.
        doc.cost_usd += 0.19
        return 0.19

    _install_regen(monkeypatch, finalize=fake_finalize)
    client = object()
    session = _session(image_client=lambda: client)
    body = _body(_run(session.finalize(
        {"layer_id": ART_ID, "prompt": "especially the water"})))
    assert calls["job_dir"] == Path("/tmp/job")
    assert calls["layer_id"] == ART_ID and calls["client"] is client
    assert calls["prompt"] == "especially the water"
    assert "assets/canvas_ly_aaa111_2.png" in body and "$0.19" in body
    assert session.doc.cost_usd == pytest.approx(0.19)


def test_finalize_says_the_measurements_have_to_be_taken_again(monkeypatch):
    # imaging.refine is composition-faithful, not pixel-identical, so anything
    # measured off the draft is stale the moment this returns.
    _install_regen(monkeypatch, finalize=lambda *a, **k: 0.19)
    body = _body(_run(_session(image_client=lambda: object()).finalize(
        {"layer_id": ART_ID})))
    assert "measured again" in body
    assert "history" in body


def test_finalize_needs_no_prompt_of_its_own(monkeypatch):
    # The plate is the request -- an uploaded plate with no prompt finalizes
    # perfectly well, where a re-roll of the same layer is refused.
    seen = {}

    def fake_finalize(job_dir, doc, layer_id, *, client, prompt=None):
        seen["prompt"] = prompt
        return 0.19

    _install_regen(monkeypatch, finalize=fake_finalize)
    result = _run(_session(image_client=lambda: object()).finalize(
        {"layer_id": ART_ID}))
    assert result["is_error"] is False
    assert seen["prompt"] is None


def test_a_failing_finalize_is_a_tool_result_not_a_crashed_turn(monkeypatch):
    def boom(job_dir, doc, layer_id, *, client, prompt=None):
        raise RuntimeError("layer 'ly_aaa111' is locked")

    _install_regen(monkeypatch, finalize=boom)
    result = _run(_session(image_client=lambda: object()).finalize(
        {"layer_id": ART_ID}))
    assert result["is_error"] is True
    assert "locked" in _body(result)


def test_finalize_without_a_layer_id_says_what_it_needs(monkeypatch):
    _install_regen(monkeypatch, finalize=lambda *a, **k: 0.0)
    result = _run(_session(image_client=lambda: object()).finalize({}))
    assert result["is_error"] is True
    assert "layer_id" in _body(result)


def test_finalize_without_an_image_lane_refuses_in_its_own_words(monkeypatch):
    # Reachable by a handler called directly (the registry hides the tool),
    # and the refusal has to name THIS button, not "the image lane".
    _install_regen(monkeypatch, finalize=lambda *a, **k: 0.0)
    result = _run(_session(image_client=None).finalize({"layer_id": ART_ID}))
    assert result["is_error"] is True
    assert "finalizing is not available" in _body(result)


# -- ground_figure ------------------------------------------------------------

def test_ground_figure_passes_the_instruction_and_charges_the_document(
        monkeypatch):
    calls = {}

    def fake_ground(job_dir, doc, layer_id, *, client, instruction=None):
        calls.update(layer_id=layer_id, client=client, instruction=instruction)
        doc.layer(layer_id).source = "assets/canvas_ly_aaa111_3.png"
        doc.cost_usd += 0.19
        return 0.19

    _install_regen(monkeypatch, ground_figure=fake_ground)
    client = object()
    session = _session(image_client=lambda: client)
    body = _body(_run(session.ground_figure(
        {"layer_id": ART_ID, "instruction": "wet cobblestones"})))
    assert calls["layer_id"] == ART_ID and calls["client"] is client
    assert calls["instruction"] == "wet cobblestones"
    assert "assets/canvas_ly_aaa111_3.png" in body and "$0.19" in body
    assert "contact shadow" in body
    assert session.doc.cost_usd == pytest.approx(0.19)


def test_ground_figure_needs_no_instruction(monkeypatch):
    # The recipe is the server's; the instruction is only the scene's own
    # specifics, so the button works with nothing typed into it.
    seen = {}

    def fake_ground(job_dir, doc, layer_id, *, client, instruction=None):
        seen["instruction"] = instruction
        return 0.19

    _install_regen(monkeypatch, ground_figure=fake_ground)
    assert _run(_session(image_client=lambda: object()).ground_figure(
        {"layer_id": ART_ID}))["is_error"] is False
    assert seen["instruction"] is None


def test_a_failing_grounding_is_a_tool_result_not_a_crashed_turn(monkeypatch):
    def boom(job_dir, doc, layer_id, *, client, instruction=None):
        raise RuntimeError("the plate could not be read as an image")

    _install_regen(monkeypatch, ground_figure=boom)
    result = _run(_session(image_client=lambda: object()).ground_figure(
        {"layer_id": ART_ID}))
    assert result["is_error"] is True
    assert "could not be read" in _body(result)


def test_ground_figure_without_a_layer_id_says_what_it_needs(monkeypatch):
    _install_regen(monkeypatch, ground_figure=lambda *a, **k: 0.0)
    result = _run(_session(image_client=lambda: object()).ground_figure({}))
    assert result["is_error"] is True
    assert "layer_id" in _body(result)


def test_ground_figure_without_an_image_lane_refuses_in_its_own_words(
        monkeypatch):
    _install_regen(monkeypatch, ground_figure=lambda *a, **k: 0.0)
    result = _run(_session(image_client=None).ground_figure(
        {"layer_id": ART_ID}))
    assert result["is_error"] is True
    assert "grounding a figure is not available" in _body(result)


# -- rebalance ----------------------------------------------------------------

def test_rebalance_returns_the_measured_sentence_verbatim(monkeypatch):
    # The sentence IS the tool result: every number that drove the correction,
    # so "why did it do that" never costs a second turn.
    sentence = ("Measured on this plate alone: mean luminance 12%, contrast "
                "spread 5% — so levels were nudged brightness +0.15.")
    _install_regen(monkeypatch, rebalance=lambda *a, **k: sentence)
    result = _run(_session(image_client=None).rebalance({"layer_id": ART_ID}))
    assert result["is_error"] is False
    assert _body(result) == sentence


def test_rebalance_runs_without_an_image_client_and_costs_nothing(monkeypatch):
    _install_regen(monkeypatch, rebalance=lambda *a, **k: "measured.")
    session = _session(image_client=None)
    assert _run(session.rebalance({"layer_id": ART_ID}))["is_error"] is False
    assert session.doc.cost_usd == 0.0


def test_rebalances_op_reaches_ops_applied(monkeypatch):
    """regen lands the correction through ops.apply on the working document,
    which records it in `doc.history` -- but ops_applied is only ever appended
    to by apply_ops, and ops_applied is what the UI folds into undo. Without
    the copy-across, the one edit a person most wants back is the one they
    cannot take back."""
    from docproof.canvas import ops as canvas_ops

    def fake_rebalance(job_dir, doc, layer_id):
        canvas_ops.apply(doc, {
            "op": "set_effects", "layer_id": layer_id,
            "effects": [{"type": "levels",
                         "params": {"brightness": 0.1, "contrast": 0.05}}]})
        return "measured."

    _install_regen(monkeypatch, rebalance=fake_rebalance)
    session = _session(image_client=None)
    _run(session.rebalance({"layer_id": ART_ID}))
    op, = session.ops_applied
    assert op["op"] == "set_effects" and op["layer_id"] == ART_ID
    assert [e["type"] for e in op["effects"]] == ["levels"]


def test_rebalance_records_only_what_this_call_applied(monkeypatch):
    # Read as a range from history's tail, so an op that was already in the
    # log before the call is not claimed a second time.
    from docproof.canvas import ops as canvas_ops

    def fake_rebalance(job_dir, doc, layer_id):
        canvas_ops.apply(doc, {"op": "set_layer", "layer_id": layer_id,
                               "visible": True})
        return "measured."

    _install_regen(monkeypatch, rebalance=fake_rebalance)
    session = _session(image_client=None)
    _run(session.apply_ops({"ops": [
        {"op": "nudge", "layer_id": TEXT_ID, "dx": -0.1}]}))
    _run(session.rebalance({"layer_id": ART_ID}))
    assert [op["op"] for op in session.ops_applied] == ["nudge", "set_layer"]


def test_the_recorded_rebalance_op_is_a_copy_of_the_history_entry(monkeypatch):
    from docproof.canvas import ops as canvas_ops

    def fake_rebalance(job_dir, doc, layer_id):
        canvas_ops.apply(doc, {"op": "set_layer", "layer_id": layer_id,
                               "visible": True})
        return "measured."

    _install_regen(monkeypatch, rebalance=fake_rebalance)
    session = _session(image_client=None)
    _run(session.rebalance({"layer_id": ART_ID}))
    session.doc.history[-1]["layer_id"] = "ly_rewritten"
    assert session.ops_applied[0]["layer_id"] == ART_ID


def test_rebalance_against_the_real_lane_measures_and_lands_a_levels_effect(
        tmp_path):
    """End to end against the actual regeneration module: no vendor client,
    no key, no spend -- and the op it applies is a real, validated set_effects
    that the undo stack can take back."""
    from PIL import Image

    (tmp_path / "assets").mkdir()
    Image.new("RGB", (48, 72), "#101014").save(
        tmp_path / "assets" / "c0_background.png")
    session = _Session(job_dir=tmp_path, doc=_doc(), mode="act",
                       snapshot_png=None, image_client=None)
    result = _run(session.rebalance({"layer_id": ART_ID}))
    assert result["is_error"] is False
    assert "mean luminance" in _body(result)
    op, = session.ops_applied
    assert op["op"] == "set_effects" and op["layer_id"] == ART_ID
    assert [e["type"] for e in op["effects"]] == ["levels"]
    assert [e.type for e in session.doc.layer(ART_ID).effects] == ["levels"]
    assert session.doc.cost_usd == 0.0


def test_a_failing_rebalance_is_a_tool_result_not_a_crashed_turn(monkeypatch):
    def boom(job_dir, doc, layer_id):
        raise RuntimeError("only art layers have plates to regenerate")

    _install_regen(monkeypatch, rebalance=boom)
    session = _session(image_client=None)
    result = _run(session.rebalance({"layer_id": TEXT_ID}))
    assert result["is_error"] is True
    assert "only art layers" in _body(result)
    assert session.ops_applied == []


def test_rebalance_without_a_layer_id_says_what_it_needs(monkeypatch):
    _install_regen(monkeypatch, rebalance=lambda *a, **k: "measured.")
    result = _run(_session(image_client=None).rebalance({}))
    assert result["is_error"] is True
    assert "layer_id" in _body(result)


# -- the keyword contract with the regeneration lane --------------------------

def test_the_three_verbs_are_called_the_way_regen_declares_them():
    """The tools and the regeneration lane were written in parallel, so the
    keyword contract is asserted rather than assumed -- a rename there should
    fail here, not in front of a person mid-turn."""
    import inspect as _inspect

    from docproof.canvas import regen
    for name, extra in (("finalize", "prompt"),
                        ("ground_figure", "instruction")):
        params = _inspect.signature(getattr(regen, name)).parameters
        assert list(params)[:3] == ["job_dir", "doc", "layer_id"], name
        assert params["client"].kind is _inspect.Parameter.KEYWORD_ONLY, name
        assert params[extra].kind is _inspect.Parameter.KEYWORD_ONLY, name
        assert params[extra].default is None, name
    # rebalance takes no client at all, and answers with the sentence.
    params = _inspect.signature(regen.rebalance).parameters
    assert list(params) == ["job_dir", "doc", "layer_id"]
    assert _inspect.signature(regen.rebalance).return_annotation == "str"


# -- the corner pin is only reported when there is one ------------------------

def test_inspect_omits_the_corner_pin_from_an_unpinned_frame():
    # A `"corners":null` on every frame of every inspect is a field the model
    # re-reads on each refresh and learns nothing from.
    for layer in json.loads(_body(_run(_session().inspect({}))))["layers"]:
        assert "corners" not in layer["frame"]


def test_inspect_reports_a_corner_pin_that_is_actually_set():
    doc = _doc()
    doc.layer(ART_ID).frame.corners = [[0.08, 0.12], [0.94, 0.05],
                                       [0.97, 0.88], [0.11, 0.95]]
    session = _Session(job_dir=Path("/tmp/job"), doc=doc, mode="act",
                       snapshot_png=None)
    art = json.loads(_body(_run(session.inspect({}))))["layers"][0]
    assert art["frame"]["corners"] == [[0.08, 0.12], [0.94, 0.05],
                                       [0.97, 0.88], [0.11, 0.95]]


def test_a_corner_pins_points_are_rounded_like_every_other_number():
    doc = _doc()
    doc.layer(ART_ID).frame.corners = [[0.0800000001, 0.123456789]] * 4
    session = _Session(job_dir=Path("/tmp/job"), doc=doc, mode="act",
                       snapshot_png=None)
    art = json.loads(_body(_run(session.inspect({}))))["layers"][0]
    assert art["frame"]["corners"][0] == [0.08, 0.1235]


# -- the system prompt teaches the plate verbs --------------------------------

def test_the_system_prompt_carries_one_line_for_each_plate_verb():
    prompt = assistant.SYSTEM_PROMPT
    assert "`finalize`" in prompt and "keep this plate" in prompt
    assert "`ground_figure`" in prompt and "stands on something" in prompt
    assert "`rebalance`" in prompt and "Costs NOTHING" in prompt


def test_the_system_prompt_says_the_plate_verbs_are_not_destructive():
    assert "history strip" in assistant.VERBS
    assert "Costs money" in assistant.VERBS
