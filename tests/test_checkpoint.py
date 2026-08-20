"""The checkpoint is the file that decides whether a resumed review re-pays for
calls it already made, so its failure modes are all money.

It had no direct tests before the format became append-only; these pin the three
paths the format introduces — a torn final line, a key written twice, a file
deleted mid-run — plus the fingerprint contract that was always there.
"""
from __future__ import annotations

import json

from docproof.checkpoint import Checkpoint
from docproof.models import Usage

FP = {"document": "abc123", "model": "claude-haiku-4-5"}


def _usage(n: int = 1) -> Usage:
    return Usage(input_tokens=100 * n, output_tokens=20 * n, api_calls=1)


def _ckpt(tmp_path, fingerprint=None) -> Checkpoint:
    return Checkpoint(tmp_path / "checkpoint.json",
                      fingerprint=fingerprint or FP)


def test_a_written_entry_comes_back_after_a_reload(tmp_path):
    c = _ckpt(tmp_path)
    c.put("p0-chunk-000", items=[{"finding_id": "f-0001"}], usage=_usage(),
          ok=True)
    c.put("p0-chunk-001", items=[], usage=_usage(), ok=True)

    again = _ckpt(tmp_path)
    assert again.load() == 2
    assert again.get("p0-chunk-000").items == [{"finding_id": "f-0001"}]
    assert again.get("p0-chunk-001").items == []
    assert again.max_finding_id() == 1


def test_optional_shadow_metadata_survives_without_changing_old_entries(tmp_path):
    c = _ckpt(tmp_path)
    receipt = {"examination_production_verdicts": {
        "outcomes": {"X-site": False}}}
    c.put("p0-chunk-000", items=[], usage=_usage(), ok=True,
          metadata=receipt)
    c.put("p0-chunk-001", items=[], usage=_usage(), ok=True)

    again = _ckpt(tmp_path)
    assert again.load() == 2
    assert again.get("p0-chunk-000").metadata == receipt
    assert again.get("p0-chunk-001").metadata == {}


def test_a_failed_call_is_kept_for_its_cost_but_never_replayed(tmp_path):
    c = _ckpt(tmp_path)
    c.put("p0-chunk-000", items=[], usage=_usage(3), ok=False)

    again = _ckpt(tmp_path)
    assert again.load() == 0                 # nothing usable to resume from
    assert again.get("p0-chunk-000") is None
    assert again.burned("p0-chunk-000")["input_tokens"] == 300


def test_a_retry_supersedes_the_attempt_that_failed(tmp_path):
    """Both attempts are on disk — append-only keeps history — so the load has
    to end on the later one. Reading the failed line last would resurrect its
    finding ids and start the counter past the ids actually in use."""
    c = _ckpt(tmp_path)
    c.put("p0-chunk-000", items=[{"finding_id": "f-0009"}], usage=_usage(),
          ok=False)
    c.put("p0-chunk-000", items=[{"finding_id": "f-0002"}], usage=_usage(),
          ok=True)

    again = _ckpt(tmp_path)
    assert again.load() == 1
    assert again.get("p0-chunk-000").items == [{"finding_id": "f-0002"}]
    assert again.burned("p0-chunk-000") is None
    assert again.max_finding_id() == 2


def test_a_torn_final_line_costs_only_that_call(tmp_path):
    """The crash the format exists to survive. Everything written before the
    half-line is paid for and must still resume."""
    c = _ckpt(tmp_path)
    for i in range(3):
        c.put(f"p0-chunk-00{i}", items=[], usage=_usage(), ok=True)
    path = tmp_path / "checkpoint.json"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"key": "p0-chunk-003", "items": [], "usa')

    again = _ckpt(tmp_path)
    assert again.load() == 3
    assert again.get("p0-chunk-003") is None
    assert again.get("p0-chunk-002") is not None


def test_a_different_fingerprint_starts_clean(tmp_path):
    c = _ckpt(tmp_path)
    c.put("p0-chunk-000", items=[], usage=_usage(), ok=True)

    moved = _ckpt(tmp_path, {"document": "edited", "model": FP["model"]})
    assert moved.load() == 0
    assert not (tmp_path / "checkpoint.json").exists()


def test_a_file_with_no_header_starts_clean(tmp_path):
    path = tmp_path / "checkpoint.json"
    path.write_text('{"key": "p0-chunk-000", "items": [], "usage": {}, '
                    '"ok": true}\n', encoding="utf-8")
    assert _ckpt(tmp_path).load() == 0
    assert not path.exists()


def test_a_file_in_the_old_whole_document_format_starts_clean(tmp_path):
    """The one-time cost of the format change: a review mid-flight at the
    upgrade re-pays. It must be a clean start, not a crash."""
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(
        {"fingerprint": {"version": 1, **FP},
         "entries": {"p0-chunk-000": {"items": [], "usage": {}, "ok": True}}},
        indent=2), encoding="utf-8")
    assert _ckpt(tmp_path).load() == 0
    assert not path.exists()


def test_a_deleted_file_is_rebuilt_with_its_header(tmp_path):
    """Cancelling a job unlinks the checkpoint from the HTTP thread while the
    run may still be folding. The next append has to re-emit the header, or it
    leaves behind a headerless file that the next load can only discard."""
    c = _ckpt(tmp_path)
    c.put("p0-chunk-000", items=[], usage=_usage(), ok=True)
    (tmp_path / "checkpoint.json").unlink()

    c.put("p0-chunk-001", items=[{"finding_id": "f-0004"}], usage=_usage(),
          ok=True)
    again = _ckpt(tmp_path)
    assert again.load() == 1
    assert again.get("p0-chunk-001") is not None
    assert again.max_finding_id() == 4


def test_the_file_grows_by_one_line_per_call(tmp_path):
    """The point of the format. The old one re-serialized every entry on every
    put, so a long review wrote its own results back O(n^2) times."""
    c = _ckpt(tmp_path)
    big = [{"finding_id": f"f-{i:04d}", "explanation": "x" * 200}
           for i in range(20)]
    for i in range(5):
        c.put(f"p0-chunk-00{i}", items=big, usage=_usage(), ok=True)

    lines = (tmp_path / "checkpoint.json").read_text("utf-8").splitlines()
    assert len(lines) == 6                   # one header, five calls
    # Each line stands alone: no line repeats an earlier call's payload.
    keys = [json.loads(x)["key"] for x in lines[1:]]
    assert keys == [f"p0-chunk-00{i}" for i in range(5)]


def test_the_entry_after_a_torn_line_is_not_swallowed(tmp_path):
    """The bug the append-only format could have introduced. `load` drops a torn
    final line but leaves it on disk; if the next append wrote straight onto it,
    the fragment and the new entry would splice into one unparseable line — and
    the call just paid for would be dropped by every later load and bought
    again on every resume."""
    c = _ckpt(tmp_path)
    c.put("p0-chunk-000", items=[], usage=_usage(), ok=True)
    c.put("p0-chunk-001", items=[], usage=_usage(), ok=True)
    path = tmp_path / "checkpoint.json"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"key": "p0-chunk-002", "items": [], "usa')   # killed mid-write

    resumed = _ckpt(tmp_path)
    assert resumed.load() == 2
    resumed.put("p0-chunk-002", items=[], usage=_usage(), ok=True)
    resumed.put("p0-chunk-003", items=[], usage=_usage(), ok=True)

    again = _ckpt(tmp_path)
    assert again.load() == 4
    assert again.get("p0-chunk-002") is not None, "re-paid call was lost again"
    assert again.get("p0-chunk-003") is not None
