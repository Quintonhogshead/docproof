"""Where finished documents land, per build.

The web build runs on a server whose filesystem is thrown away on every
redeploy or restart; only the mounted volume (DOCPROOF_HOME) survives. Job
records live there, so the documents they point at must too — otherwise an
old job's results tab 404s "…is missing" the moment the machine recycles.
The desktop build has a real Documents folder and must keep using it.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.main import create_app
from app.settings import Paths, default_output_dir

SECRET = "test-session-secret"


def test_web_build_writes_results_onto_the_volume(tmp_path):
    app = create_app(tmp_path, start_runner=False, web=True,
                     session_secret=SECRET, https_only=False)
    out = app.state.settings.output_dir
    # Beside the job records on the volume, not in the desktop's Documents.
    assert out == str(Paths(tmp_path).results)
    assert tmp_path in Path(out).parents          # on the volume, under the home
    assert out != str(default_output_dir())


def test_desktop_build_keeps_the_documents_folder_default(tmp_path):
    app = create_app(tmp_path, start_runner=False)
    assert app.state.settings.output_dir == str(default_output_dir())


def test_a_persisted_output_dir_is_overridden_on_the_web_build(tmp_path):
    # A saved output_dir is the data-loss bug, not a preference to honour. The
    # Settings screen round-trips the field on every save, so a value stored
    # while it read the desktop default pins an ephemeral path that a redeploy
    # wipes — the whole "DocProof deleted my documents on update" report. The web
    # build overrides it onto the volume no matter what settings.json says.
    poisoned = str(default_output_dir())
    (tmp_path / "settings.json").write_text(
        json.dumps({"output_dir": poisoned}), encoding="utf-8")
    app = create_app(tmp_path, start_runner=False, web=True,
                     session_secret=SECRET, https_only=False)
    assert app.state.settings.output_dir == str(Paths(tmp_path).results)
