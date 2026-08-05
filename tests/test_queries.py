"""The query channel: findings that ask instead of correcting.

The house brief calls this "two channels, never blurred" — a tracked change is
a correction the author accepts or rejects, a comment is a question that edits
nothing. The tests here mostly guard the "never blurred" half: a query must
leave the document byte-identical, and must still be there to read.
"""
from __future__ import annotations

import itertools
import json
import re
import zipfile

import pytest

from docproof.analyzer import build_system_prompt
from docproof.config import Config, load_config
from docproof.error_registry import load_error_types
from docproof.models import DocumentModel, ParagraphRef, Usage
from docproof.pipeline import finish, prepare
from docproof.reassembler import paragraph_view_text
from docproof.utils.xml_helpers import DocxPackage, walk_package
from docproof.validator import validate_findings

from .test_error_types import ERROR_DIR

QUERY = frozenset({"speaker_change"})


def _doc(*texts):
    paras = [ParagraphRef(f"body-{i:04d}", "word/document.xml", "body", t,
                          "Normal") for i, t in enumerate(texts)]
    return DocumentModel(source_path="x.docx", paragraphs=tuple(paras))


def _finding(**kw):
    from docproof.models import Finding
    base = dict(finding_id="f-0001", chunk_id="chunk-000", para_id="body-0000",
                error_type="speaker_change", original_text="", occurrence=1,
                corrected_text="", explanation="ask", confidence="high")
    return Finding(**{**base, **kw})


# --- the error type contract --------------------------------------------------

def test_speaker_change_is_a_query_type():
    et = load_error_types(ERROR_DIR, ["speaker_change"])["speaker_change"]
    assert et.is_query and et.channel == "query"


def test_change_types_are_not_queries():
    et = load_error_types(ERROR_DIR, ["comma_splice"])["comma_splice"]
    assert not et.is_query


def test_an_unknown_channel_is_rejected(tmp_path):
    src = (ERROR_DIR / "speaker_change.yaml").read_text()
    (tmp_path / "speaker_change.yaml").write_text(
        src.replace("channel: query", "channel: nonsense"))
    with pytest.raises(ValueError, match="channel must be"):
        load_error_types(tmp_path, ["speaker_change"])


def test_the_prompt_tells_a_query_type_not_to_edit():
    types = list(load_error_types(ERROR_DIR, ["speaker_change"]).values())
    prompt = build_system_prompt(types)
    assert "CHANNEL: QUERY" in prompt
    assert "repeat it unchanged" in prompt
    # A change type gets no such section.
    change = build_system_prompt(
        list(load_error_types(ERROR_DIR, ["comma_splice"]).values()))
    assert "CHANNEL: QUERY" not in change


# --- the validator ------------------------------------------------------------

def test_a_query_anchors_to_its_whole_sentence():
    doc = _doc("He left. She waited a long time. Nobody came.")
    out = validate_findings(
        [_finding(original_text="She waited a long time.",
                  corrected_text="She waited a long time.")],
        doc, "medium", query_types=QUERY)
    assert out[0].status == "query"
    assert out[0].anchor.start == 9
    assert out[0].anchor.end == 9 + len("She waited a long time.")


def test_a_query_is_not_rejected_for_changing_nothing():
    """A change type repeating its sentence is a no-op and gets thrown away.
    For a query that is the whole point."""
    doc = _doc("Two speakers here.")
    same = dict(original_text="Two speakers here.",
                corrected_text="Two speakers here.")
    assert validate_findings([_finding(**same)], doc, "medium",
                             query_types=QUERY)[0].status == "query"
    assert validate_findings([_finding(error_type="comma_splice", **same)],
                             doc, "medium")[0].status == "rejected_noop"


def test_a_query_ignores_the_confidence_gate():
    """A question is the output, not a fallback from a correction, so a
    low-confidence query is still a query."""
    doc = _doc("Two speakers here.")
    out = validate_findings(
        [_finding(original_text="Two speakers here.",
                  corrected_text="Two speakers here.", confidence="low")],
        doc, "high", query_types=QUERY)
    assert out[0].status == "query"


def test_a_query_does_not_block_a_change_on_the_same_text():
    """Different channels: a comment and an edit may sit on the same words."""
    doc = _doc("The gate was open, the dogs were gone.")
    out = validate_findings([
        _finding(original_text="The gate was open, the dogs were gone.",
                 corrected_text="The gate was open, the dogs were gone."),
        _finding(finding_id="f-0002", error_type="comma_splice",
                 original_text="The gate was open, the dogs were gone.",
                 corrected_text="The gate was open; the dogs were gone."),
    ], doc, "medium", query_types=QUERY)
    assert [f.status for f in out] == ["query", "validated"]


def test_duplicate_queries_are_dropped():
    doc = _doc("Two speakers here.")
    kw = dict(original_text="Two speakers here.",
              corrected_text="Two speakers here.")
    out = validate_findings([_finding(**kw),
                             _finding(finding_id="f-0002", **kw)],
                            doc, "medium", query_types=QUERY)
    assert [f.status for f in out] == ["query", "rejected_duplicate"]


def test_a_query_that_cannot_anchor_is_rejected():
    doc = _doc("Nothing like that here.")
    out = validate_findings([_finding(original_text="Absent sentence.",
                                      corrected_text="Absent sentence.")],
                            doc, "medium", query_types=QUERY)
    assert out[0].status == "rejected_no_anchor"


# --- into the document --------------------------------------------------------

def _run(tmp_path, paragraphs, mock, *, error_types, **cfg_kw):
    import docx
    d = docx.Document()
    for t in paragraphs:
        d.add_paragraph(t)
    src = tmp_path / "q.docx"
    d.save(src)

    cfg = load_config("config/default.yaml")
    cfg.error_types = error_types
    for k, v in cfg_kw.items():
        setattr(cfg, k, v)
    prepared = prepare(cfg, src, "config/error_types")

    from docproof.analyzer import MockAnalyzer
    findings = []
    ids = itertools.count(1)
    for group in prepared.groups:
        for chunk in prepared.chunks:
            found, _ = MockAnalyzer(group, mock, ids).analyze_chunk(chunk, Usage())
            findings += found
    out = finish(prepared, findings, Usage(), cfg, out_dir=tmp_path / "out",
                 source_path=src)
    return out


def _comment_ranges(path) -> dict[str, str]:
    """What each comment actually highlights, read back out of the file."""
    doc = zipfile.ZipFile(path).read("word/document.xml").decode()
    out = {}
    for cid in re.findall(r'commentRangeStart w:id="(\d+)"', doc):
        seg = doc.split(f'commentRangeStart w:id="{cid}"')[1] \
                 .split(f'commentRangeEnd w:id="{cid}"')[0]
        out[cid] = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", seg, re.S))
    return out


def _comment_texts(path) -> list[str]:
    z = zipfile.ZipFile(path)
    if "word/comments.xml" not in z.namelist():
        return []
    body = z.read("word/comments.xml").decode()
    return re.findall(r"<w:t[^>]*>(.*?)</w:t>", body, re.S)


SPEAKERS = "“I won’t go.” “You will.”"


def test_a_query_writes_a_comment_and_changes_nothing(tmp_path):
    out = _run(tmp_path, [f"{SPEAKERS} He said it twice."],
               [{"para_id": "body-0000", "error_type": "speaker_change",
                 "original_text": SPEAKERS, "corrected_text": SPEAKERS,
                 "explanation": "Two speakers — new paragraph for the reply?",
                 "confidence": "high"}],
               error_types=["speaker_change"])
    assert out.applied == 0

    pkg = DocxPackage(out.reviewed_path)
    for wp in walk_package(pkg):
        # No revision markup at all: accept and reject read the same.
        assert (paragraph_view_text(wp.element, "accept")
                == paragraph_view_text(wp.element, "reject"))
    assert list(_comment_ranges(out.reviewed_path).values()) == [SPEAKERS]
    assert any("new paragraph for the reply" in t
               for t in _comment_texts(out.reviewed_path))


def test_a_below_gate_finding_becomes_a_query_comment(tmp_path):
    out = _run(tmp_path, ["The manuscript was finished, nobody wanted it."],
               [{"para_id": "body-0000", "error_type": "comma_splice",
                 "original_text": "The manuscript was finished, nobody wanted it.",
                 "corrected_text": "The manuscript was finished; nobody wanted it.",
                 "explanation": "Two independent clauses joined by a comma.",
                 "confidence": "low"}],
               error_types=["comma_splice"])
    assert out.applied == 0
    texts = _comment_texts(out.reviewed_path)
    assert any("left as written" in t for t in texts)
    # The suggestion travels with the question, so the author can judge it.
    assert any("Suggested:" in t for t in texts)


def test_a_query_comment_highlights_the_sentence_not_the_edit_site(tmp_path):
    """A below-gate comma splice would have changed one comma. A comment
    hanging off a single comma tells the author nothing."""
    text = "The manuscript was finished, nobody wanted it."
    out = _run(tmp_path, [text],
               [{"para_id": "body-0000", "error_type": "comma_splice",
                 "original_text": text,
                 "corrected_text": "The manuscript was finished; nobody wanted it.",
                 "explanation": "Splice.", "confidence": "low"}],
               error_types=["comma_splice"])
    assert list(_comment_ranges(out.reviewed_path).values()) == [text]


def test_query_comments_can_be_turned_off(tmp_path):
    out = _run(tmp_path, ["The manuscript was finished, nobody wanted it."],
               [{"para_id": "body-0000", "error_type": "comma_splice",
                 "original_text": "The manuscript was finished, nobody wanted it.",
                 "corrected_text": "The manuscript was finished; nobody wanted it.",
                 "explanation": "Splice.", "confidence": "low"}],
               error_types=["comma_splice"], query_comments=False)
    assert _comment_texts(out.reviewed_path) == []
    # ...but it is still reported, so nothing is lost silently.
    assert "Possibly intentional" in out.summary_md.read_text()


def test_a_query_and_a_change_coexist_in_one_paragraph(tmp_path):
    """The ordering that matters: comments are anchored in canonical-text
    offsets, and applying a deletion moves text out of w:t. Queries therefore
    go on first, and this proves both still land."""
    text = "The gate was open, the dogs were gone. She waited for them."
    out = _run(tmp_path, [text], [
        {"para_id": "body-0000", "error_type": "comma_splice",
         "original_text": "The gate was open, the dogs were gone.",
         "corrected_text": "The gate was open; the dogs were gone.",
         "explanation": "Splice.", "confidence": "high"},
        {"para_id": "body-0000", "error_type": "speaker_change",
         "original_text": "She waited for them.",
         "corrected_text": "She waited for them.",
         "explanation": "Whose line is this?", "confidence": "high"},
    ], error_types=[["comma_splice", "speaker_change"]])

    assert out.applied == 1
    pkg = DocxPackage(out.reviewed_path)
    para = next(iter(walk_package(pkg))).element
    assert paragraph_view_text(para, "accept") == (
        "The gate was open; the dogs were gone. She waited for them.")
    assert paragraph_view_text(para, "reject") == text
    assert "She waited for them." in _comment_ranges(out.reviewed_path).values()


def test_findings_json_records_the_query_status(tmp_path):
    out = _run(tmp_path, [f"{SPEAKERS} He said it twice."],
               [{"para_id": "body-0000", "error_type": "speaker_change",
                 "original_text": SPEAKERS, "corrected_text": SPEAKERS,
                 "explanation": "Two speakers.", "confidence": "high"}],
               error_types=["speaker_change"])
    payload = json.loads(out.findings_json.read_text())
    assert payload["stats"]["query"] == 1
    assert payload["findings"][0]["status"] == "query"
    assert "Queries" in out.summary_md.read_text()


def test_the_default_config_ships_the_query_channel_on():
    cfg = load_config("config/default.yaml")
    assert cfg.query_comments is True
    assert "speaker_change" in cfg.error_type_keys
    assert Config().query_comments is True
