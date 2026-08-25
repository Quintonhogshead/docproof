"""`pipeline.finish`'s widened `enforce_cluster_atomicity` gate.

The repair channel's atomicity enforcement (docproof/repair.py) used to run
only `if cfg.repair.enabled` — fine when the cluster in `validated` was always
this run's own repair read. The merge desk (docproof/mergedesk.py) breaks that
assumption: it carries a cluster over from an EARLIER run's findings into a
fresh, $0 arbitration where repair.enabled is off (no new repair read is
wanted or paid for). `pipeline.finish` widens the gate to `cfg.repair.enabled
OR any cluster_id present` so that carried-over cluster is still enforced
atomic even though this run never touched the repair channel. This test
reproduces exactly that: two cluster members plus an unrelated, earlier-listed
finding that steals one member's span — with `repair.enabled` left at its
default (False) throughout.
"""
from __future__ import annotations

import docx

from docproof.config import load_config
from docproof.pipeline import finish, prepare
from docproof.models import Finding, Usage

TEXT = "He watching the door, waited there for her quietly."


def _prepared(tmp_path, cfg):
    d = docx.Document()
    d.add_paragraph(TEXT)
    src = tmp_path / "m.docx"
    d.save(src)
    return prepare(cfg, src, "config/error_types"), src


def _cfg():
    cfg = load_config("config/default.yaml")
    cfg.error_types = ["comma_splice"]
    assert cfg.repair.enabled is False              # the precondition
    return cfg


def test_carried_over_cluster_is_atomic_even_with_repair_disabled(tmp_path):
    cfg = _cfg()
    prepared, src = _prepared(tmp_path, cfg)

    chunk_id = prepared.chunks[0].chunk_id
    # `finish()` takes raw, pending Finding objects directly — no need to
    # route these through MockAnalyzer, whose RawFinding shape does not carry
    # cluster_id (mock findings never come from the repair channel).
    findings = [
        # An ordinary, non-cluster finding — listed first, so it claims the
        # comma span before the cluster member gets a chance at it.
        Finding("f-1", chunk_id, "body-0000", "comma_splice",
               "the door, waited", 1, "the door; waited", "semicolon",
               "high"),
        # Cluster member 1 — an unrelated span, would validate cleanly alone.
        Finding("f-2", chunk_id, "body-0000", "comma_splice",
               "He watching the door", 1, "He was watching the door",
               "missing verb", "high", cluster_id="rp-c-0001"),
        # Cluster member 2 — the SAME span the earlier finding just claimed.
        Finding("f-3", chunk_id, "body-0000", "comma_splice",
               "the door, waited", 1, "the door. Waited", "period",
               "high", cluster_id="rp-c-0001"),
    ]

    out = finish(prepared, findings, Usage(), cfg, out_dir=tmp_path / "out",
                source_path=src)

    # Neither cluster member was applied — member 2 lost the span outright,
    # and atomicity withdrew member 1 with it, even though repair.enabled is
    # False and this run's own repair channel never ran.
    assert out.applied == 1                # only the non-cluster finding
    assert cfg.repair.enabled is False      # unchanged for the whole test

    import json
    data = json.loads((tmp_path / "out" / "findings.json").read_text())
    statuses = {f["finding_id"]: f["status"] for f in data["findings"]}
    assert statuses["f-1"] == "validated"
    assert statuses["f-3"] == "rejected_overlap"
    assert statuses["f-2"] == "query"       # withdrawn to keep the cluster atomic


def test_a_non_cluster_finding_is_never_touched_by_the_widened_gate(tmp_path):
    """Control: two ORDINARY findings, one overlapping the other, with no
    cluster_id anywhere — the widened gate must be a no-op here, same as
    before it existed."""
    cfg = _cfg()
    prepared, src = _prepared(tmp_path, cfg)
    chunk_id = prepared.chunks[0].chunk_id
    findings = [
        Finding("f-1", chunk_id, "body-0000", "comma_splice",
               "the door, waited", 1, "the door; waited", "semicolon",
               "high"),
        Finding("f-2", chunk_id, "body-0000", "comma_splice",
               "the door, waited", 1, "the door. Waited", "period", "high"),
    ]
    out = finish(prepared, findings, Usage(), cfg, out_dir=tmp_path / "out",
                source_path=src)
    assert out.applied == 1
