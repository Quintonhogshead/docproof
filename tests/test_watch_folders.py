"""Finding one author's subfolder, and refusing to guess.

The resolver's whole job is a single scoped query and an honest answer: exactly
one confident folder, or `None`. These hold it to that — case and surrounding
whitespace are forgiven, but a name that matches zero folders, matches two, or
matches only a file (not a folder) or a folder under the wrong parent is `None`,
because writing a manuscript into a guessed folder is the one mistake that costs
an author their book in the wrong place.

Nothing here touches a network: `fake_drive` answers the scoped query the same
way Drive would, honouring the parent, name and mimeType clauses.
"""
from __future__ import annotations

from app.watch import folders
from app.watch.drive import FOLDER_MIME

from .fakes import drive_entry, fake_drive

PARENT = "parent-author-folder"


def _folder(name: str, parent: str = PARENT) -> dict:
    entry = drive_entry(name, mime=FOLDER_MIME)
    entry["parents"] = [parent]
    return entry


def _file(name: str, parent: str = PARENT) -> dict:
    entry = drive_entry(name)
    entry["parents"] = [parent]
    return entry


def _resolve(files: dict, first: str = "Quinton", last: str = "Johnson"):
    return folders.resolve(first, last, PARENT, "tok",
                           opener=fake_drive(files))


# --- compose ------------------------------------------------------------------

def test_compose_titlecases_the_two_names_with_one_space():
    assert folders.compose("QUINTON", "JOHNSON") == "Quinton Johnson"
    assert folders.compose("  jane ", " smith ") == "Jane Smith"


# --- the confident single match -----------------------------------------------

def test_an_exact_folder_resolves_to_its_id():
    assert _resolve({"sf-1": _folder("Quinton Johnson")}) == "sf-1"


def test_case_only_drift_is_forgiven_by_the_fallback():
    """Drive's `name =` is case-sensitive, so a lower-case folder misses the
    exact query; the `name contains` fallback and a case-folded compare find it."""
    assert _resolve({"sf-1": _folder("quinton johnson")}) == "sf-1"


def test_surrounding_whitespace_is_forgiven():
    assert _resolve({"sf-1": _folder("Quinton Johnson ")}) == "sf-1"


# --- refusing to guess --------------------------------------------------------

def test_no_matching_folder_is_none():
    assert _resolve({"sf-1": _folder("Someone Else")}) is None


def test_two_matching_folders_is_none():
    assert _resolve({"sf-1": _folder("Quinton Johnson"),
                     "sf-2": _folder("Quinton Johnson")}) is None


def test_a_file_of_the_same_name_is_not_a_folder_match():
    """The manuscript inside can share the author's spelling; only a folder is
    a folder, and the scoped query says so."""
    assert _resolve({"m-1": _file("Quinton Johnson")}) is None


def test_a_folder_under_a_different_parent_is_not_matched():
    assert _resolve({"sf-1": _folder("Quinton Johnson",
                                     parent="somewhere-else")}) is None


def test_a_blank_name_never_resolves():
    assert folders.resolve("", "", PARENT, "tok",
                           opener=fake_drive({"sf-1": _folder("")})) is None
