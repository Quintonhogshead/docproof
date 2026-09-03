"""docproof/replay.py — the shared machinery behind `docproof import-findings`
and `docproof replay`: row shape-checking, error-type resolution (the
"don't hand-roll a curated_fix.yaml" fix), quote sanitizing, and the $0
paid-pass isolation.
"""
from __future__ import annotations

import json

from docproof.config import load_config
from docproof.error_registry import ErrorType
from docproof.replay import (DEFAULT_IMPORT_TYPE, build_findings,
                             load_findings_file, resolve_error_type,
                             rows_from_payload, sanitize_corrected,
                             zero_paid_passes)

ERROR_DIR = "config/error_types"


def _registry() -> dict[str, ErrorType]:
    return {
        "spelling": ErrorType(key="spelling", name="Spelling", version=1,
                              detection_prompt="x", fix_guidance="x",
                              confidence_guidance="", examples=(),
                              channel="change"),
        "general_error": ErrorType(key="general_error", name="General",
                                   version=1, detection_prompt="x",
                                   fix_guidance="x", confidence_guidance="",
                                   examples=(), channel="query"),
        "title_italics": ErrorType(key="title_italics", name="Title", version=1,
                                   detection_prompt="x", fix_guidance="x",
                                   confidence_guidance="", examples=(),
                                   channel="format"),
    }


# --- resolve_error_type ---------------------------------------------------

def test_a_known_change_type_is_kept_as_is():
    key, remapped = resolve_error_type("spelling", _registry(), DEFAULT_IMPORT_TYPE)
    assert key == "spelling"
    assert remapped is False


def test_general_error_is_remapped_the_redding_trap():
    """general_error is channel:query in the shipped registry — replaying a
    row labelled that way, unchanged, would silently turn it into a margin
    comment instead of a tracked change. This is the exact trap the Redding
    stand-in run hit and had to route around with a hand-rolled
    curated_fix.yaml; import-findings must not need one."""
    key, remapped = resolve_error_type("general_error", _registry(),
                                       DEFAULT_IMPORT_TYPE)
    assert key == DEFAULT_IMPORT_TYPE
    assert remapped is True


def test_a_format_channel_type_is_also_remapped():
    key, remapped = resolve_error_type("title_italics", _registry(),
                                       DEFAULT_IMPORT_TYPE)
    assert key == DEFAULT_IMPORT_TYPE
    assert remapped is True


def test_missing_or_unknown_type_is_remapped():
    key, remapped = resolve_error_type("", _registry(), DEFAULT_IMPORT_TYPE)
    assert (key, remapped) == (DEFAULT_IMPORT_TYPE, True)
    key, remapped = resolve_error_type("not_a_real_type", _registry(),
                                       DEFAULT_IMPORT_TYPE)
    assert (key, remapped) == (DEFAULT_IMPORT_TYPE, True)


# --- sanitize_corrected -----------------------------------------------------

def test_sanitize_curls_straight_quotes():
    out = sanitize_corrected('She said "hello" to him.', None)
    assert '"' not in out
    assert "“" in out and "”" in out


def test_sanitize_never_touches_original_text():
    """sanitize_corrected is never called on original_text — this test
    documents the contract at the call site: build_findings only sanitizes
    the corrected side. See test_build_findings_sanitizes_only_corrected."""
    original = 'He said "wait" and left.'
    assert sanitize_corrected(original, None) != original  # would curl it too


# --- rows_from_payload / load_findings_file ---------------------------------

def test_bare_list_passes_through():
    assert rows_from_payload([{"a": 1}]) == [{"a": 1}]


def test_envelope_dict_unwraps_findings_key():
    assert rows_from_payload({"findings": [{"a": 1}], "cost": {}}) == [{"a": 1}]


def test_checkpoint_shaped_dict_unwraps_findings_key():
    payload = {"version": 1, "fingerprint": {}, "usage": {}, "coverage": None,
              "findings": [{"a": 1}, {"a": 2}]}
    assert rows_from_payload(payload) == [{"a": 1}, {"a": 2}]


def test_non_list_non_dict_raises():
    import pytest
    with pytest.raises(ValueError):
        rows_from_payload(42)


def test_load_findings_file_reads_and_unwraps(tmp_path):
    p = tmp_path / "findings.json"
    p.write_text(json.dumps({"findings": [{"para_id": "body-0000"}]}),
                encoding="utf-8")
    assert load_findings_file(p) == [{"para_id": "body-0000"}]


# --- build_findings ----------------------------------------------------------

def test_build_findings_shape_checks_and_reports_rejects():
    rows = [
        {"para_id": "body-0000", "original_text": "teh", "corrected_text": "the"},
        {"para_id": "", "original_text": "x", "corrected_text": "y"},  # bad para_id
        {"para_id": "body-0001", "original_text": "", "corrected_text": "y"},
        {"para_id": "body-0002", "original_text": "x"},                # no corrected
        "not a dict",
    ]
    findings, rejects, remapped = build_findings(
        rows, variant=None, error_dir=ERROR_DIR, remap_unchanneled=True)
    assert len(findings) == 1
    assert len(rejects) == 4
    assert findings[0].para_id == "body-0000"


def test_build_findings_remaps_general_error_when_unchanneled():
    rows = [{"para_id": "body-0000", "original_text": "x", "corrected_text": "y",
            "error_type": "general_error"}]
    findings, rejects, remapped = build_findings(
        rows, variant=None, error_dir=ERROR_DIR, remap_unchanneled=True)
    assert not rejects
    assert remapped == 1
    assert findings[0].error_type == DEFAULT_IMPORT_TYPE


def test_build_findings_preserves_error_type_when_replaying():
    """replay's rows came FROM docproof — trust the label, don't remap it,
    even though this same row would be remapped under import-findings."""
    rows = [{"para_id": "body-0000", "original_text": "x", "corrected_text": "y",
            "error_type": "general_error"}]
    findings, rejects, remapped = build_findings(
        rows, variant=None, error_dir=ERROR_DIR, remap_unchanneled=False)
    assert not rejects
    assert remapped == 0
    assert findings[0].error_type == "general_error"


def test_build_findings_sanitizes_only_corrected():
    rows = [{"para_id": "body-0000", "original_text": 'She said "hi".',
             "corrected_text": 'She said "hey".'}]
    findings, rejects, remapped = build_findings(
        rows, variant=None, error_dir=ERROR_DIR, remap_unchanneled=True)
    f = findings[0]
    assert f.original_text == 'She said "hi".'          # untouched
    assert '"' not in f.corrected_text                  # curled


def test_build_findings_defaults_and_clamps_optional_fields():
    rows = [{"para_id": "body-0000", "original_text": "x", "corrected_text": "y",
            "confidence": "extremely high", "occurrence": "not a number"}]
    findings, rejects, remapped = build_findings(
        rows, variant=None, error_dir=ERROR_DIR, remap_unchanneled=True)
    assert not rejects
    assert findings[0].confidence == "medium"
    assert findings[0].occurrence == 1


# --- format rows cannot round-trip through replay --------------------------

def test_build_findings_drops_a_format_channel_row_on_both_paths():
    """title_italics is a real format-channel type. Its row records the whole
    sentence as original_text and only the title span as corrected_text — a
    shape that means "mark this span" ONLY on the format channel. Replay has no
    format slot, so if this became a Finding it would read as "delete the
    sentence, keep the span" and eat prose. Drop it, on import AND replay."""
    row = {"para_id": "body-0000",
           "original_text": "She loved The Great Gatsby dearly.",
           "corrected_text": "The Great Gatsby",
           "error_type": "title_italics"}
    for remap in (True, False):
        findings, rejects, remapped = build_findings(
            [row], variant=None, error_dir=ERROR_DIR, remap_unchanneled=remap)
        assert findings == []
        assert len(rejects) == 1
        assert "format-carrying" in rejects[0]["reason"]


def test_build_findings_drops_a_row_carrying_a_format_field():
    """Even a row whose error_type is unremarkable is dropped when it carries a
    `format` field — that field is exactly what a serialized format finding
    (finding_to_dict is dataclasses.asdict) writes, and the tell that its
    find/replace are a mark, not an edit."""
    row = {"para_id": "body-0000", "original_text": "the whole sentence here",
           "corrected_text": "whole", "error_type": "spelling",
           "format": "italic"}
    findings, rejects, remapped = build_findings(
        [row], variant=None, error_dir=ERROR_DIR, remap_unchanneled=True)
    assert findings == []
    assert len(rejects) == 1
    assert "format-carrying" in rejects[0]["reason"]


# --- zero_paid_passes ---------------------------------------------------------

def test_zero_paid_passes_disables_every_provider_backed_stage():
    cfg = load_config("config/default.yaml")
    # Flip everything on first, so the test proves zero_paid_passes actually
    # turns them off rather than merely agreeing with already-off defaults.
    # ensemble.enabled is a read-only property of `detectors`, so it is
    # flipped on by giving it one.
    from docproof.config import DetectorSpec
    cfg.ensemble.detectors = [DetectorSpec(model="claude-haiku-4-5")]
    assert cfg.ensemble.enabled is True
    cfg.sapling.enabled = True
    cfg.repair.enabled = True
    cfg.low_confidence.confirm = True
    cfg.smoothing.enabled = True
    cfg.continuity.enabled = True
    cfg.chapter_continuity.enabled = True
    cfg.meaning_check.enabled = True
    cfg.fix_check.enabled = True
    cfg.storysheet.enabled = True
    cfg.candidate_screening.mode = "apply"
    cfg.rounds.count = 3

    zero_paid_passes(cfg)

    assert cfg.ensemble.enabled is False
    assert cfg.sapling.enabled is False
    assert cfg.repair.enabled is False
    assert cfg.low_confidence.confirm is False
    assert cfg.smoothing.enabled is False
    assert cfg.continuity.enabled is False
    assert cfg.chapter_continuity.enabled is False
    assert cfg.meaning_check.enabled is False
    assert cfg.fix_check.enabled is False
    assert cfg.storysheet.enabled is False
    assert cfg.candidate_screening.mode == "off"
    assert cfg.rounds.count == 1


def test_zero_paid_passes_leaves_free_deterministic_stages_alone():
    cfg = load_config("config/default.yaml")
    cfg.consistency.enabled = True
    cfg.spellcheck.enabled = True
    zero_paid_passes(cfg)
    assert cfg.consistency.enabled is True
    assert cfg.spellcheck.enabled is True


# --- word_count_delta_guard -------------------------------------------------

import pytest

from docproof.config import Config
from docproof.ingest import build_document_model, preflight
from docproof.models import Anchor, Finding
from docproof.reassembler import apply_tracked_changes
from docproof.replay import WordCountDelta, word_count_delta_guard
from docproof.utils.xml_helpers import walk_package

from .conftest import FIXTURES

_ORIG = "It was late, we were tired, the road went on."


def _reviewed_with_deletion(tmp_path, deleted_span: str) -> str:
    """styled.docx with one tracked deletion of `deleted_span` from its body
    paragraph — the shape a formatting row misrouted onto the change channel
    produces. Returns the saved path."""
    cfg = Config()
    pkg = preflight(FIXTURES / "styled.docx", "abort")
    doc = build_document_model(pkg, cfg)
    pid = next(p.para_id for p in doc.paragraphs if p.text == _ORIG)
    start = _ORIG.index(deleted_span)
    f = Finding("f-1", "chunk-000", pid, "comma_splice", _ORIG, 1, "", "test",
                "high", status="validated",
                anchor=Anchor(start, start + len(deleted_span), deleted_span, ""))
    stats = apply_tracked_changes(pkg, doc, [f], cfg)
    assert stats.applied == ("f-1",)
    out = tmp_path / "reviewed.docx"
    pkg.save(out)
    return str(out)


def test_word_count_delta_guard_raises_on_a_sentence_eating_deletion(tmp_path):
    path = _reviewed_with_deletion(tmp_path, "we were tired, the road went on")
    # Thresholds lowered so the 7-word drop on this tiny fixture clears the
    # bars deterministically — the production defaults are validated below.
    with pytest.raises(WordCountDelta) as exc:
        word_count_delta_guard(path, max_drop_ratio=0.1, min_drop=3)
    assert exc.value.reject_words > exc.value.accept_words


def test_word_count_delta_guard_floor_spares_a_tiny_document(tmp_path):
    """The min_drop floor keeps a small document with one legitimate deletion
    from tripping: the same 7-word drop is under the 25-word default floor."""
    path = _reviewed_with_deletion(tmp_path, "we were tired, the road went on")
    reject_words, accept_words = word_count_delta_guard(path)  # defaults
    drop = reject_words - accept_words
    assert 0 < drop < 25  # counted, but under the floor, so not raised


def test_word_count_delta_guard_passes_a_clean_document(tmp_path):
    """A document whose only edit inserts/keeps words never trips the guard."""
    cfg = Config()
    pkg = preflight(FIXTURES / "styled.docx", "abort")
    doc = build_document_model(pkg, cfg)
    pid = next(p.para_id for p in doc.paragraphs if p.text == _ORIG)
    # ", w" -> ". W": a real proofreading fix, zero net word change.
    f = Finding("f-1", "chunk-000", pid, "comma_splice", _ORIG, 1, "", "test",
                "high", status="validated", anchor=Anchor(11, 14, ", w", ". W"))
    apply_tracked_changes(pkg, doc, [f], cfg)
    out = tmp_path / "clean.docx"
    pkg.save(out)
    reject_words, accept_words = word_count_delta_guard(str(out))
    assert reject_words == accept_words


def test_build_findings_folds_a_findings_json_split_pair_into_one_finding():
    """A findings.json lists a decision the validator split as its row plus a
    lettered sibling quoting the same span; replayed as two rows they became
    one fresh finding and one rejected duplicate of it."""
    row = {"finding_id": "f-0077", "para_id": "body-0000",
           "original_text": "a bunch of frekin idiots",
           "corrected_text": "a bunch of freaking idiots",
           "error_type": "spelling", "status": "validated"}
    sib = dict(row, finding_id="f-0077b")
    findings, rejects, _ = build_findings(
        [sib, row], variant=None, error_dir=ERROR_DIR, remap_unchanneled=False)
    assert not rejects
    assert len(findings) == 1
    assert findings[0].corrected_text == "a bunch of freaking idiots"
