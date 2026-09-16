"""Generated (pydantic) schemas reach the Codex transport in its small vocabulary."""
import pytest

from galley import codex_runner as cr
from galley.astra_review import AstraReviewError


def test_generated_typed_and_story_sheet_schemas_pass_after_normalization():
    """Kyler 2, 2026-09-16: 694 Luna reads refused for $defs/$ref/const."""
    from docproof.analyzer import build_output_model, strict_json_schema
    from docproof.storysheet import StorySheet
    typed = strict_json_schema(build_output_model(("spelling",)))
    story = strict_json_schema(StorySheet)
    for raw in (typed, story):
        with pytest.raises(AstraReviewError):
            cr._check_schema(raw)
        cr._check_schema(cr.normalize_schema(raw))     # does not raise
    finding = cr.normalize_schema(typed)["properties"]["findings"]["items"]
    assert finding["type"] == "object" and "$ref" not in finding
    assert finding["properties"]["error_type"] == {"enum": ["spelling"], "type": "string"}
    assert "$defs" not in cr.normalize_schema(story)
    assert cr.normalize_schema(story)["properties"]["characters"]["items"]["properties"]["pronouns"]["type"] == "string"


def test_normalization_leaves_hand_written_schemas_untouched_and_refuses_the_rest():
    from galley.fixed_workflow import CHECK_SCHEMA, READ_SCHEMA
    assert cr.normalize_schema(READ_SCHEMA) == READ_SCHEMA
    assert cr.normalize_schema(CHECK_SCHEMA) == CHECK_SCHEMA
    with pytest.raises(AstraReviewError, match="unknown definition"):
        cr.normalize_schema({"type": "object", "properties": {"x": {"$ref": "#/$defs/Missing"}},
                             "required": ["x"], "additionalProperties": False})
    still_bad = cr.normalize_schema({"type": "object", "properties": {"n": {"type": "integer", "minimum": 0}},
                                     "required": ["n"], "additionalProperties": False})
    with pytest.raises(AstraReviewError, match="constraint"):
        cr._check_schema(still_bad)


def test_run_structured_submits_a_generated_schema(tmp_path, monkeypatch):
    """The normalized schema is what the transport sees; the reply still parses."""
    from docproof.analyzer import build_output_model, strict_json_schema
    raw = strict_json_schema(build_output_model(("spelling",)))
    seen = {}

    class Session:
        closed = False
        failure = None

        def check_login(self, binary, home, deadline):
            pass

        def execute(self, argv, *, prompt, env, cwd, timeout):
            seen["schema"] = cr._load(__import__("pathlib").Path(argv[argv.index("--output-schema") + 1]))
            out = __import__("pathlib").Path(argv[argv.index("--output-last-message") + 1])
            out.write_text('{"findings": []}')
            return {"returncode": 0, "timed_out": False, "stdout_tail": "", "stderr_tail": "",
                    "events": {"execution_kind": "app_server", "turn_terminal": True, "turn_status": "completed",
                               "event_counts": {"turn.completed": 1}}}
    monkeypatch.setenv("GALLEY_CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(cr, "_binary", lambda explicit: "fake-codex")
    result = cr.run_structured("Read this.", raw, tmp_path / "work", request_id="req-1",
                               model="gpt-5.6-luna", reasoning_effort="low", no_tools=True, session=Session())
    assert result == {"findings": []}
    assert "$defs" not in seen["schema"] and "$ref" not in str(seen["schema"])
