"""A phase that backgrounds its work and exits says so."""
from __future__ import annotations

from galley.driver import Driver


def _driver(tmp_path) -> Driver:
    (tmp_path / "runs" / "driver").mkdir(parents=True)
    return Driver(book=tmp_path / "b.docx", slug="s", workspace_root=tmp_path)


def _stream(tmp_path, phase: str, lines: list[str]) -> None:
    p = tmp_path / "s" / "runs" / "driver" / f"{phase}.stream.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")


def test_background_events_are_reported(tmp_path):
    d = Driver(book=tmp_path / "b.docx", slug="s", workspace_root=tmp_path)
    _stream(tmp_path, "ladder", [
        '{"type":"x","subtype":"assistant"}',
        '{"type":"x","subtype":"background_tasks_changed"}',
        '{"type":"x","subtype":"task_notification"}',
    ])
    said = d.backgrounded_work("ladder")
    assert "background-task event" in said


def test_a_clean_session_says_nothing(tmp_path):
    d = Driver(book=tmp_path / "b.docx", slug="s", workspace_root=tmp_path)
    _stream(tmp_path, "ladder", ['{"type":"x","subtype":"assistant"}'])
    assert d.backgrounded_work("ladder") == ""


def test_a_missing_stream_is_not_an_error(tmp_path):
    d = Driver(book=tmp_path / "b.docx", slug="s", workspace_root=tmp_path)
    assert d.backgrounded_work("ladder") == ""


def test_the_prompt_forbids_backgrounding():
    from galley.driver import _PROMPTS as PHASE_PROMPTS
    assert "FOREGROUND" in PHASE_PROMPTS["ladder"]
    assert "never background it" in PHASE_PROMPTS["ladder"]
