"""Driving InDesign, without driving InDesign.

Same rule as the vendor calls: no test here starts a real application. The one
subprocess boundary — osascript — is passed in, so what is checked is the
script docproof writes and what it makes of the answers it gets back.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from docproof.prep.place import (BUNDLE_ID, PlaceError, build_jsx,
                                 find_indesign, place_into_template)


class FakeRunner:
    """Stands in for subprocess.run. Records the command and replies."""

    def __init__(self, stdout="", stderr="", returncode=0, raises=None,
                 writes: Path | None = None):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode
        self.raises, self.writes = raises, writes
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if self.raises:
            raise self.raises
        if self.writes:
            self.writes.write_bytes(b"an InDesign document")
        return subprocess.CompletedProcess(command, self.returncode,
                                           self.stdout, self.stderr)

    @property
    def script(self) -> str:
        """The .jsx the last command pointed at, read before it was deleted."""
        return self._script

    def keep_script(self):
        """Capture the script's text, since place() deletes it afterwards."""
        outer = self

        def run(command, **kwargs):
            path = command[-1].split('POSIX file "')[1].rsplit('"', 1)[0]
            outer._script = Path(path).read_text("utf-8")
            return outer(command, **kwargs)
        return run


@pytest.fixture
def files(tmp_path):
    template = tmp_path / "House prose.indd"
    template.write_bytes(b"template")
    tagged = tmp_path / "tagged_novel.docx"
    tagged.write_bytes(b"manuscript")
    return template, tagged, tmp_path / "out" / "placed_novel.indd"


# --- the script ---------------------------------------------------------------

def test_the_script_names_the_three_files_it_was_given(files):
    template, tagged, out = files
    jsx = build_jsx(template, tagged, out)
    assert str(template) in jsx and str(tagged) in jsx and str(out) in jsx


def test_the_template_is_saved_under_the_new_name_before_anything_else(files):
    """The designer's template is the one file in this that must not change."""
    jsx = build_jsx(*files)
    opened = jsx.index("app.open(template)")
    saved = jsx.index("doc.save(new File(OUT))")
    placed = jsx.index(".place(tagged)")
    assert opened < saved < placed


def test_the_script_asks_indesign_not_to_stop_and_ask(files):
    """A missing font dialog on somebody else's Mac at 11pm is a hung job."""
    assert "NEVER_INTERACT" in build_jsx(*files)


def test_incoming_styles_resolve_to_the_templates_own(files):
    """Prep's whole output is style NAMES that match the template. Placing has
    to mean "use the template's definition of that name", or the manuscript
    arrives carrying Word's formatting instead of the house's."""
    jsx = build_jsx(*files)
    assert "RESOLVE_CLASH_USE_EXISTING" in jsx
    assert "importUnusedStyles = false" in jsx
    assert "preserveLocalOverrides = true" in jsx       # the author's italics


def test_the_script_merges_the_styles_the_importer_invents(files):
    """Verified against InDesign 2026: the Word importer capitalises the first
    letter of every incoming style name, so "chapter # / title" arrives as
    "Chapter # / title", misses the template's own style by one character, and
    the manuscript lands in a new style carrying Word's formatting — the exact
    outcome prep exists to prevent. The script has to put them back together."""
    jsx = build_jsx(*files)
    assert "mergeImportedStyles" in jsx
    assert "toLowerCase()" in jsx                    # matched ignoring case
    assert "remove(target)" in jsx                   # reassigned, not deleted
    # Recorded before the place, or every style looks like it was always there.
    assert jsx.index("styleState(doc)") < jsx.index(".place(tagged)")


def test_how_many_styles_were_merged_is_reported(files):
    template, tagged, out = files

    def run(command, **kwargs):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"done")
        return subprocess.CompletedProcess(command, 0, "OK:120:ok:7", "")

    place_into_template(template, tagged, out, runner=run)


def test_a_path_with_a_quote_in_it_cannot_break_out_of_the_script(tmp_path):
    odd = tmp_path / 'Bob\'s "House" template.indd'
    jsx = build_jsx(odd, tmp_path / "t.docx", tmp_path / "o.indd")
    line = next(l for l in jsx.splitlines() if l.startswith("var TEMPLATE"))
    assert line.endswith(";") and line.count('";') == 1


# --- running it ---------------------------------------------------------------

def test_a_successful_place_returns_the_written_file(files):
    template, tagged, out = files
    runner = FakeRunner(stdout="OK:312:ok\n", writes=None)
    out.parent.mkdir(parents=True, exist_ok=True)

    def run(command, **kwargs):
        out.write_bytes(b"an InDesign document")
        return runner(command, **kwargs)

    assert place_into_template(template, tagged, out, runner=run) == out
    command = runner.commands[0]
    assert command[0] == "osascript"
    assert BUNDLE_ID in command[-1] and "language javascript" in command[-1]


def test_the_temporary_script_does_not_outlive_the_run(files):
    template, tagged, out = files
    runner = FakeRunner(stdout="OK:1:ok")
    keeper = runner.keep_script()

    def run(command, **kwargs):
        result = keeper(command, **kwargs)
        out.write_bytes(b"done")
        return result

    place_into_template(template, tagged, out, runner=run)
    path = runner.commands[0][-1].split('POSIX file "')[1].rsplit('"', 1)[0]
    assert not Path(path).exists()
    assert "var TEMPLATE" in runner.script


def test_indesigns_own_complaint_is_passed_on_verbatim(files):
    runner = FakeRunner(stdout="ERR:The template is locked.")
    with pytest.raises(PlaceError, match="The template is locked."):
        place_into_template(*files, runner=runner)


def test_a_file_that_never_appeared_is_not_reported_as_success(files):
    """InDesign says it worked; nothing is on disk. Believe the disk."""
    with pytest.raises(PlaceError, match="not there"):
        place_into_template(*files, runner=FakeRunner(stdout="OK:10:ok"))


def test_being_refused_permission_says_where_to_grant_it(files):
    runner = FakeRunner(returncode=1, stderr="execution error: Not authorized "
                                             "to send Apple events (-1743)")
    with pytest.raises(PlaceError, match="Privacy & Security"):
        place_into_template(*files, runner=runner)


def test_waiting_too_long_says_so_rather_than_hanging(files):
    runner = FakeRunner(raises=subprocess.TimeoutExpired("osascript", 600))
    with pytest.raises(PlaceError, match="stopped waiting"):
        place_into_template(*files, runner=runner, timeout=600)


def test_overset_text_is_not_a_failure(files, caplog):
    """A template whose frame cannot take the whole book still produced a file;
    the designer deals with the last page, not with an error."""
    template, tagged, out = files

    def run(command, **kwargs):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"done")
        return subprocess.CompletedProcess(command, 0, "OK:400:overset", "")

    assert place_into_template(template, tagged, out, runner=run) == out
    assert "overset" in caplog.text


# --- finding the application --------------------------------------------------

def test_the_newest_indesign_wins():
    runner = FakeRunner(stdout="/Applications/Adobe InDesign 2024/x.app\n"
                               "/Applications/Adobe InDesign 2026/x.app\n")
    assert "2026" in find_indesign(runner=runner)


def test_a_mac_without_spotlight_still_answers(monkeypatch):
    """mdfind is a convenience, not a dependency."""
    runner = FakeRunner(raises=OSError("no mdfind"))
    monkeypatch.setattr(Path, "glob", lambda self, pattern: iter([]))
    assert find_indesign(runner=runner) is None
