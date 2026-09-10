"""Phase loading must preserve doctrine without attaching unrelated manuals."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest

from galley import driver


REFERENCE = re.compile(r"references/[a-zA-Z0-9_./-]+\.md")


@pytest.fixture
def book(tmp_path):
    source = tmp_path / "Example Book.docx"
    source.write_bytes(b"source bytes for workspace seeding")
    return source


def test_reference_tree_is_seeded_and_refreshed_without_deleting_local_files(book, tmp_path):
    manual = driver.practitioner_dir()
    workspace = driver.seed_workspace(book, "example", workspace_root=tmp_path / "books")
    for source in (manual / "references").rglob("*"):
        if source.is_file():
            target = workspace / source.relative_to(manual)
            assert target.read_bytes() == source.read_bytes()

    (workspace / "references" / "local-notes.md").write_text("keep me")
    (workspace / "references" / "config.md").write_text("stale shipped reference")
    driver.seed_workspace(book, "example", workspace_root=tmp_path / "books")
    assert (workspace / "references" / "local-notes.md").read_text() == "keep me"
    assert (workspace / "references" / "config.md").read_bytes() == (
        manual / "references" / "config.md").read_bytes()


def test_old_custom_manual_without_references_still_seeds(book, tmp_path):
    manual = tmp_path / "old-manual"
    (manual / "skills").mkdir(parents=True)
    (manual / "CLAUDE.md").write_text("custom legacy policy")
    (manual / "KNOBS.md").write_text("custom legacy contracts")
    workspace = driver.seed_workspace(
        book, "legacy", workspace_root=tmp_path / "books", source_dir=manual)
    assert (workspace / "CLAUDE.md").read_text() == "custom legacy policy"
    assert not (workspace / "references").exists()


def test_all_current_reference_paths_resolve_from_seeded_workspace(book, tmp_path):
    workspace = driver.seed_workspace(book, "example", workspace_root=tmp_path / "books")
    docs = [workspace / "CLAUDE.md", workspace / "KNOBS.md"]
    docs += list((workspace / ".claude" / "skills").rglob("*.md"))
    docs += list((workspace / "references").glob("*.md"))
    for doc in docs:
        for reference in REFERENCE.findall(doc.read_text()):
            assert (workspace / reference).is_file(), (doc, reference)
    # An archive must never become another automatic harness instruction file.
    assert not list((workspace / "references").rglob("CLAUDE.md"))
    assert not list((workspace / "references").rglob("AGENTS.md"))


@pytest.mark.parametrize("phase, required", [
    ("profile", "intake.md"), ("approve", "config.md"),
    ("sweeps", "sweeps.md"), ("ladder", "lanes.md"),
    ("audit", "house-rules.md"), ("verify", "verification.md"),
    ("settle", "comment-reconciliation.md"), ("certify", "delivery.md"),
    ("deliver", "delivery.md"),
])
def test_mechanical_phase_reads_only_its_relevant_current_references(phase, required):
    prompt = driver.phase_prompt(phase, "Book.docx")
    assert f"references/{required}" in prompt
    for reference in REFERENCE.findall(prompt):
        assert (driver.practitioner_dir() / reference).is_file()
    assert "references/legacy-copyedit.md" not in prompt
    assert "references/history/" not in prompt
    assert "Read CLAUDE.md" not in prompt
    assert "do not load the full reference directory" in prompt


@pytest.mark.parametrize("phase", ["flights", "reread"])
def test_legacy_copyedit_references_require_explicit_scope(phase):
    with pytest.raises(driver.DriverError, match="copy-edit scope"):
        driver.phase_prompt(phase, "Book.docx")
    prompt = driver.phase_prompt(phase, "Book.docx", mechanical_only=False)
    assert "references/legacy-copyedit.md" in prompt


def test_ladder_reuses_approved_config_and_preserves_independent_reading():
    ladder = driver.phase_prompt("ladder", "Book.docx")
    assert "Reuse the exact approved config unchanged" in ladder
    assert "do not rewrite or regenerate it after approval" in ladder
    assert "Write the run config" not in ladder
    assert "six-window Sonnet" in ladder
    verify = driver.phase_prompt("verify", "Book.docx")
    assert "each walk window is read TWICE" in verify
    assert "never verifies that window" in verify
    assert "type-and-compare" in verify


def test_driver_thresholds_and_internal_repairs_are_unambiguous():
    prompt = driver.phase_prompt("settle", "Book.docx")
    assert "--until-clean --rounds 3 --quiet-floor 4 --quiet-share 0" in prompt
    assert "Internal repairs stay open and block certification" in prompt
    assert "leave the editorial verdict to Astra" in prompt
    skill = (driver.practitioner_dir() / "skills" / "settle" / "SKILL.md").read_text()
    assert "Standalone CLI defaults (quiet floor 3," in skill
    assert "do not substitute them for the driver flags" in skill
    assert "leftovers ship as" not in skill


def test_pass_metadata_does_not_replace_required_independent_reads():
    reference = (driver.practitioner_dir() / "references" / "verification.md").read_text()
    for flag in ("--verification-pass", "--verification-policy",
                 "--required-verification-passes"):
        assert flag in reference
    assert "do not schedule additional reads" in reference
    assert "A completed repeat is always fresh" in reference
    assert "never\ndeclare only `primary`" in reference
    assert "Do not weaken the declared policy" in reference


def test_common_policy_keeps_hard_constraints_and_reduces_automatic_context():
    manual = driver.practitioner_dir()
    common = (manual / "CLAUDE.md").read_text()
    for rule in (
        "MECHANICAL PROOFREADING ONLY", "poetry-touch", "Haiku is retired",
        "Claude never bills the Anthropic API", "Queries are an absolute LAST resort",
        "comment ceiling frozen in approval", "Internal repairs stay open",
        "Readers never", "verify their own edits", "reject-all round trip",
        "Astra alone decides", "Reuse the exact approved config unchanged",
    ):
        assert rule in common
    original = manual / "references" / "history" / "practitioner-full.md"
    assert len(common.encode()) < original.stat().st_size * 0.20
    assert (manual / "KNOBS.md").stat().st_size < 2000


def test_historical_guidance_is_preserved_byte_for_byte():
    history = driver.practitioner_dir() / "references" / "history"
    rows = re.findall(r"\| ([a-z-]+\.md) \| ([a-f0-9]{64}) \|",
                      (history / "README.md").read_text())
    assert len(rows) == 9  # both full manuals and all seven original skills
    for name, expected in rows:
        assert hashlib.sha256((history / name).read_bytes()).hexdigest() == expected
    assert "not the current operating instructions" in (
        history / "README.md").read_text().replace("\n", " ")


def test_shell_intake_launcher_seeds_references_without_a_live_model(book, tmp_path):
    tools = tmp_path / "fake-tools"
    tools.mkdir()
    for name, body in {
        "docproof": "#!/bin/sh\nexit 0\n",
        "claude": '#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE_ARGS"\n',
    }.items():
        file = tools / name
        file.write_text(body)
        file.chmod(0o755)
    capture = tmp_path / "arguments.txt"
    env = {
        **os.environ, "BOOK": str(book), "SLUG": "example",
        "WORKROOT": str(tmp_path / "books"), "WRAPBIN": str(tools),
        "PATH": str(tools) + os.pathsep + os.environ.get("PATH", ""),
        "CLAUDE_CODE_OAUTH_TOKEN": "test-only", "CAPTURE_ARGS": str(capture),
    }
    result = subprocess.run(
        ["bash", str(driver.practitioner_dir() / "launch.sh")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    workspace = tmp_path / "books" / "example"
    assert (workspace / "references" / "intake.md").is_file()
    assert (workspace / "references" / "history" / "knobs-full.md").is_file()
    assert "references/intake.md" in capture.read_text()
    assert "references/config.md" in capture.read_text()
