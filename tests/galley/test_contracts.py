"""A1 — every contract shape round-trips through JSON; unknown keys are dropped."""

import json

import pytest

from galley.contracts import (
    CASEFILE_SCHEMA_VERSION,
    Chapter,
    GFinding,
    Hypothesis,
    Manuscript,
    Provenance,
    RULINGS,
    Span,
    Verdict,
    WaveRecord,
)


def _roundtrip(obj):
    """Serialize -> json.dumps -> json.loads -> deserialize, assert equality."""
    revived = type(obj).from_json(json.loads(json.dumps(obj.to_json())))
    assert revived == obj
    return revived


SAMPLES = [
    Span("body-0007", 4, 11),
    Chapter(2, "The Long Road", ("body-0007", "body-0008")),
    Manuscript(
        paragraphs={"body-0001": "Hello there.", "body-0002": "General Kenobi."},
        order=("body-0001", "body-0002"),
        chapters=(Chapter(0, "One", ("body-0001",)),),
    ),
    Provenance("docproof_ladder", wave=1, model="claude-opus-5", cost_usd=0.42),
    GFinding(
        id="g-0001",
        error_type="comma_splice",
        span=Span("body-0002", 0, 7),
        find="General",
        replace="General,",
        note="direct address",
        confidence="high",
        provenance=Provenance("single_pass", 2, "claude-sonnet-5", 0.01),
    ),
    Verdict("g-0001", "keep", reason="cites CMOS 6.53", judge="panel", wave=2),
    Hypothesis(3, "missing_serial_comma", why="lists run dense here", span_hint="apples oranges", confidence="low"),
    WaveRecord(
        index=1,
        actions=({"kind": "RerunGroup", "group": "comma"},),
        spend_usd=1.25,
        findings_added=9,
        started_at="2026-08-21T10:00:00Z",
        ended_at="2026-08-21T11:30:00Z",
    ),
]


@pytest.mark.parametrize("obj", SAMPLES, ids=lambda o: type(o).__name__)
def test_roundtrip(obj):
    _roundtrip(obj)


def test_to_json_is_json_serializable():
    # Every to_json() output must survive json.dumps without a custom encoder.
    for obj in SAMPLES:
        json.dumps(obj.to_json())


def test_unknown_keys_dropped_not_fatal():
    payload = Span("body-0001", 1, 2).to_json()
    payload["future_field"] = {"nested": [1, 2, 3]}
    payload["schema_version"] = 999
    revived = Span.from_json(payload)  # must not raise
    assert revived == Span("body-0001", 1, 2)


def test_unknown_keys_dropped_nested():
    gf = SAMPLES[4]
    payload = gf.to_json()
    payload["surprise"] = True
    payload["span"]["surprise"] = "x"
    payload["provenance"]["surprise"] = 1.0
    assert GFinding.from_json(payload) == gf


def test_missing_optional_provenance():
    gf = GFinding("g-9", "typo", Span("body-1", 0, 1), "teh", "the")
    assert gf.provenance is None
    revived = GFinding.from_json(gf.to_json())
    assert revived == gf
    # a provenance key that is explicitly null also decodes to None
    payload = gf.to_json()
    payload["provenance"] = None
    assert GFinding.from_json(payload).provenance is None


def test_malformed_record_degrades_to_defaults():
    # A non-dict payload must not crash from_json; it yields defaults.
    assert Span.from_json([]) == Span("", 0, 0)  # type: ignore[arg-type]


def test_frozen():
    s = Span("body-1", 0, 1)
    with pytest.raises(Exception):
        s.start = 5  # type: ignore[misc]


def test_schema_version_constant():
    assert CASEFILE_SCHEMA_VERSION == 1


def test_ruling_vocabulary():
    assert RULINGS == ("keep", "reject", "downgrade", "query")
    for r in RULINGS:
        v = Verdict("g-1", r)
        assert Verdict.from_json(v.to_json()).ruling == r


def test_manuscript_text_of():
    ms = SAMPLES[2]
    assert ms.text_of("body-0001") == "Hello there."
    assert ms.text_of("nope") == ""
