"""The one-book test must freeze the newest event and refuse remote writes."""
import importlib.util
from pathlib import Path
import urllib.request
import pytest
from app.watch.drive import DriveFile, FOLDER_MIME

spec = importlib.util.spec_from_file_location('local_trial', Path(__file__).resolve().parents[1]/'tools/run_local_corrections_test.py')
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


def test_newest_is_selected_by_timestamp_not_api_order():
    rows = [{'submittedAt': 1700000000000}, {'submittedAt': 1800000000000}, {'submittedAt': 1750000000000}]
    assert trial.newest_submission(rows) is rows[1]


@pytest.mark.parametrize('rows', [[], [{'submittedAt': 1}, {}], [{'submittedAt': 3}, {'submittedAt': 3}]])
def test_missing_or_tied_timestamp_does_not_pick_an_older_submission(rows):
    with pytest.raises(ValueError):
        trial.newest_submission(rows)


def test_exact_interior_folder_required():
    correct = DriveFile('inside', 'Interior Design', FOLDER_MIME)
    assert trial.single_interior([DriveFile('cover', 'Cover Design', FOLDER_MIME), correct]) is correct
    with pytest.raises(ValueError):
        trial.single_interior([])
    with pytest.raises(ValueError):
        trial.single_interior([correct, DriveFile('duplicate', 'INTERIOR DESIGN', FOLDER_MIME)])


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
def test_remote_mutation_cannot_reach_network(method, monkeypatch):
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *a, **k: pytest.fail('No remote request is permitted'))
    with pytest.raises(RuntimeError, match='prohibits remote writes'):
        trial.read_only(urllib.request.Request('https://www.googleapis.com/drive/v3/files', method=method))
