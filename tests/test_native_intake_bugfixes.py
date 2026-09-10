from types import SimpleNamespace

import pytest

from app.watch import native_corrections as native


FILE_URL = (
    "https://api-na1.hubspot.com/form-integrations/v1/uploaded-files/"
    "signed-url-redirect/123?filename=notes.pdf"
)


def _settings(**overrides):
    values = {
        "corrections_native_folder_property": "",
        "corrections_native_project_first_property": "first",
        "corrections_native_project_last_property": "last",
        "hubspot_first_property": "first",
        "hubspot_last_property": "last",
        "folder_id": "root",
        "corrections_folder_name": "Interior Design",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _record():
    return SimpleNamespace(properties={"first": "Quinton", "last": "Johnson"})


def test_manual_files_ready_validates_urls_and_iterates_the_url_list(tmp_path, monkeypatch):
    monkeypatch.setattr(native.native_files, "cached_file", lambda url, root: tmp_path / "notes.pdf")
    assert native._manual_files_ready({"missing_attachments": [{"url": FILE_URL}]}, tmp_path)

    for missing in (
        [{"url": ""}],
        [{"url": "not-a-hubspot-file-url"}],
        [{}],
        ["malformed"],
    ):
        assert not native._manual_files_ready({"missing_attachments": missing}, tmp_path)


@pytest.mark.parametrize("children", [[], [SimpleNamespace(id="one", name="Cover Design")],
                                       [SimpleNamespace(id="one", name="Interior Design"),
                                        SimpleNamespace(id="two", name=" Interior   Design ")]])
def test_author_fallback_requires_exactly_one_interior_design_child(monkeypatch, children):
    monkeypatch.setattr(native.folders, "resolve", lambda *args, **kwargs: "author")
    monkeypatch.setattr(native.drive, "find_children", lambda *args, **kwargs: children)
    assert native._folder_for("token", _settings(), _record(), opener=None) is None


def test_author_fallback_returns_the_single_interior_design_child(monkeypatch):
    child = SimpleNamespace(id="interior", name=" Interior   Design ")
    monkeypatch.setattr(native.folders, "resolve", lambda *args, **kwargs: "author")
    monkeypatch.setattr(native.drive, "find_children", lambda *args, **kwargs: [child])
    assert native._folder_for("token", _settings(), _record(), opener=None) == "interior"


def test_explicit_mapped_folder_remains_accepted(monkeypatch):
    ws = _settings(corrections_native_folder_property="mapped")
    record = SimpleNamespace(properties={"mapped": "mapped-folder-id"})
    monkeypatch.setattr(native.folders, "resolve", lambda *args, **kwargs: pytest.fail("author fallback used"))
    assert native._folder_for("token", ws, record, opener=None) == "mapped-folder-id"
