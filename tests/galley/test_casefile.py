"""A3 — case file store: atomic save, round-trip, append-only waves."""

import json

import pytest

from galley.casefile import BudgetLedger, CaseFile, Charge, Ruling, append_wave
from galley.contracts import (
    CASEFILE_SCHEMA_VERSION,
    GFinding,
    Hypothesis,
    Span,
    Verdict,
    WaveRecord,
)

from tests.galley.fakes import gfinding


def _sample_casefile() -> CaseFile:
    cf = CaseFile(book="Redding")
    cf.style_sheet.append(Ruling("traveled / travelled", "traveled", "US spelling", "CMOS 7.1"))
    cf.findings.append(gfinding("g-1", "body-0001", "teh", "the"))
    cf.verdicts.append(Verdict("g-1", "keep", reason="obvious typo", judge="gate", wave=1))
    cf.hypotheses.append(Hypothesis(2, "missing_comma", why="dense dialogue"))
    cf.budget.charge("wave-1 ladder", 1.25, wave=1)
    append_wave(cf, WaveRecord(index=1, spend_usd=1.25, findings_added=1))
    return cf


def test_roundtrip_in_memory():
    cf = _sample_casefile()
    revived = CaseFile.from_json(json.loads(json.dumps(cf.to_json())))
    assert revived.to_json() == cf.to_json()
    assert revived.schema_version == CASEFILE_SCHEMA_VERSION


def test_save_load_roundtrip(tmp_path):
    cf = _sample_casefile()
    path = tmp_path / "casefile.json"
    cf.save(path)
    loaded = CaseFile.load(path)
    assert loaded.to_json() == cf.to_json()


def test_wave_order_preserved(tmp_path):
    cf = CaseFile()
    for i in range(1, 5):
        append_wave(cf, WaveRecord(index=i))
    path = tmp_path / "cf.json"
    cf.save(path)
    loaded = CaseFile.load(path)
    assert [w.index for w in loaded.waves] == [1, 2, 3, 4]


def test_append_wave_is_append_only():
    cf = CaseFile()
    append_wave(cf, WaveRecord(index=1))
    append_wave(cf, WaveRecord(index=2))
    with pytest.raises(ValueError):
        append_wave(cf, WaveRecord(index=2))  # rewrite a closed wave
    with pytest.raises(ValueError):
        append_wave(cf, WaveRecord(index=1))  # reorder
    assert [w.index for w in cf.waves] == [1, 2]


def test_unknown_keys_dropped(tmp_path):
    cf = _sample_casefile()
    payload = cf.to_json()
    payload["future_top_level"] = 123
    payload["findings"][0]["future_field"] = "x"
    revived = CaseFile.from_json(payload)  # must not raise
    assert revived.book == "Redding"
    assert revived.findings[0].id == "g-1"


def test_atomic_save_survives_crash_mid_write(tmp_path, monkeypatch):
    """A crash during save leaves the previous version intact, never a torn file."""
    path = tmp_path / "cf.json"

    v1 = CaseFile(book="v1")
    v1.save(path)
    assert CaseFile.load(path).book == "v1"

    # Simulate a kill mid-write: the staging write raises before os.replace.
    import galley.casefile as cfmod
    from pathlib import Path as _Path

    real_write_text = _Path.write_text

    def boom(self, *a, **k):
        if str(self).endswith(".writing"):
            raise OSError("killed mid-write")
        return real_write_text(self, *a, **k)

    monkeypatch.setattr(_Path, "write_text", boom)

    v2 = CaseFile(book="v2")
    with pytest.raises(OSError):
        v2.save(path)

    # File is still the OLD version — never torn, never empty.
    monkeypatch.undo()
    reloaded = CaseFile.load(path)
    assert reloaded.book == "v1"
    # No staging file left behind.
    assert not list(tmp_path.glob("*.writing"))


def test_atomic_save_replaces_cleanly(tmp_path):
    path = tmp_path / "cf.json"
    CaseFile(book="v1").save(path)
    CaseFile(book="v2").save(path)
    assert CaseFile.load(path).book == "v2"
    assert not list(tmp_path.glob("*.writing"))


def test_budget_ledger_spent():
    ledger = BudgetLedger()
    ledger.charge("a", 1.0, wave=1)
    ledger.charge("b", 2.5, wave=1)
    assert ledger.spent_usd == 3.5
    revived = BudgetLedger.from_json(ledger.to_json())
    assert revived.spent_usd == 3.5
    assert [c.label for c in revived.charges] == ["a", "b"]


def test_empty_casefile_roundtrips(tmp_path):
    cf = CaseFile()
    path = tmp_path / "cf.json"
    cf.save(path)
    loaded = CaseFile.load(path)
    assert loaded.waves == [] and loaded.findings == []
    assert loaded.schema_version == CASEFILE_SCHEMA_VERSION
