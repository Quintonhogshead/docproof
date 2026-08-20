from pathlib import Path

import pytest

from docproof.site_ledger import (ExaminationLedger, IncompleteVerdicts,
                                  read_jsonl, validate_verdict_coverage)
from docproof.site_models import (ExaminationSite, LedgerState, SiteAnchor,
                                  Verdict)


def _site(site_id="X-one", start=0):
    return ExaminationSite(
        site_id=site_id, site_type="spelling",
        anchors=(SiteAnchor(part="word/document.xml", paragraph_id="body-0000",
                            start_offset=start, end_offset=start + 4),),
        generator="test", status=LedgerState.NEEDS_JUDGMENT)


def test_ledger_is_append_only_and_projects_current_state(tmp_path: Path):
    ledger = ExaminationLedger()
    ledger.register(_site(), initial_state=LedgerState.NEEDS_JUDGMENT)
    ledger.apply_verdict(Verdict(
        site_id="X-one", decision="error", correction="word",
        confidence="high", judge="test-judge"))
    ledger.transition("X-one", LedgerState.EDIT, actor="validator")
    ledger.transition("X-one", LedgerState.APPLIED, actor="writer")

    assert ledger.state("X-one") == LedgerState.APPLIED
    assert [e.state.value for e in ledger.events] == [
        "generated", "needs_judgment", "model_confirmed", "edit", "applied"]

    path = tmp_path / "ledger.jsonl.gz"
    ledger.write_jsonl(path)
    rows = read_jsonl(path)
    assert [row["sequence"] for row in rows] == [1, 2, 3, 4, 5]
    assert rows[0]["site"]["site_id"] == "X-one"
    assert "site" not in rows[-1]


def test_packet_coverage_must_be_exact():
    validate_verdict_coverage(["a", "b"], ["b", "a"])
    with pytest.raises(IncompleteVerdicts) as missing:
        validate_verdict_coverage(["a", "b"], ["a"])
    assert missing.value.missing == ("b",)
    with pytest.raises(IncompleteVerdicts) as duplicate:
        validate_verdict_coverage(["a", "b"], ["a", "a", "b"])
    assert duplicate.value.duplicate == ("a",)
    with pytest.raises(IncompleteVerdicts) as unknown:
        validate_verdict_coverage(["a"], ["other"])
    assert unknown.value.unknown == ("other",)
