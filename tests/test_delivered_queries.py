"""Which questions actually reached the reader.

findings.json has always said whether a finding became a tracked change. It said
nothing about the other channel, and `status == "query"` is not the answer:
between a query and a margin comment sit an anchor check, a story-part check and
an attach that can fail. Anything counting questions off this file was counting
the ones that were GENERATED and calling them delivered.

See docproof.reporting.write_findings_json and docproof/reassembler.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import docx

from docproof.config import load_config
from docproof.models import Finding, Usage
from docproof.pipeline import finish, prepare

ERRORS = "config/error_types"
PARA = "He could not have known the road was barred."


def _run(tmp_path, findings, text=PARA):
    d = docx.Document()
    d.add_paragraph(text)
    src = tmp_path / "book.docx"
    d.save(src)
    cfg = load_config("config/default.yaml")
    cfg.audit = "off"
    cfg.error_types, cfg.sweeps = [], []
    prepared = prepare(cfg, src, ERRORS)
    out = finish(prepared, findings, Usage(), cfg, out_dir=tmp_path / "run",
                 source_path=src)
    record = json.loads(Path(out.findings_json).read_text(encoding="utf-8"))
    return out, {f["finding_id"]: f for f in record["findings"]}


def _query(fid="q-1", **over) -> Finding:
    fields = dict(finding_id=fid, chunk_id="c0", para_id="body-0000",
                  error_type="continuity", original_text=PARA, occurrence=1,
                  corrected_text=PARA, explanation="worth a look",
                  confidence="high", force_query=True)
    fields.update(over)
    return Finding(**fields)


def test_a_delivered_question_is_marked_delivered(tmp_path):
    _out, rows = _run(tmp_path, [_query()])
    row = rows["q-1"]
    assert row["status"] == "query"
    assert row["queried"] is True and row["unplaced"] is False
    assert row["applied"] is False          # the other channel, untouched


def test_a_tracked_change_is_not_marked_queried(tmp_path):
    fix = Finding(finding_id="f-1", chunk_id="c0", para_id="body-0000",
                  error_type="homophone_confusion", original_text=PARA,
                  occurrence=1,
                  corrected_text="He could have known the road was barred.",
                  explanation="dropped negation", confidence="high")
    _out, rows = _run(tmp_path, [fix])
    row = rows["f-1"]
    assert row["applied"] is True
    assert row["queried"] is False and row["unplaced"] is False


def test_a_question_in_a_footnote_is_marked_unplaced_not_delivered(tmp_path):
    """The case the whole change exists for.

    A comment can only hang in the main story part, so a question about a
    footnote is dropped at reassembler.py's `part != "word/document.xml"`
    check. Its status is still "query" and its anchor is still real — nothing
    about the finding says it went missing. Only `unplaced` does."""
    note = "The note was short, the implications were not."
    cfg = load_config("config/default.yaml")
    cfg.audit = "off"
    cfg.error_types, cfg.sweeps = [], []
    prepared = prepare(cfg, "tests/fixtures/footnotes.docx", ERRORS)
    q = _query(fid="q-2", para_id="footnote-2-p0", original_text=note,
               corrected_text=note)
    out = finish(prepared, [q], Usage(), cfg, out_dir=tmp_path / "run",
                 source_path="tests/fixtures/footnotes.docx")

    rows = {f["finding_id"]: f for f in
            json.loads(Path(out.findings_json).read_text("utf-8"))["findings"]}
    row = rows["q-2"]
    # Everything about the finding says "delivered"...
    assert row["status"] == "query" and row["anchor"] is not None
    # ...and it was not.
    assert row["unplaced"] is True and row["queried"] is False
    # This is the overcount the change exists to expose: `queried` counts the
    # question that was generated, the deliverable contains none.
    assert out.queried == 1


def test_queried_and_unplaced_survive_a_rejudge_round_trip(tmp_path):
    """These are reporting fields, not Finding fields. read_run rebuilds
    Findings from this file, so a key it does not know to strip is a TypeError
    on the next re-judge — which is how the last such field was nearly a bug."""
    from docproof.rejudge import read_run

    _out, rows = _run(tmp_path, [_query()])
    assert "queried" in rows["q-1"] and "unplaced" in rows["q-1"]
    findings, source = read_run(tmp_path / "run")
    assert [f.finding_id for f in findings] == ["q-1"]
    assert source.endswith("book.docx")


def test_every_finding_carries_both_keys(tmp_path):
    """A consumer should not have to guess whether a missing key means false or
    means an older run wrote the file."""
    _out, rows = _run(tmp_path, [_query()])
    for row in rows.values():
        assert isinstance(row["queried"], bool)
        assert isinstance(row["unplaced"], bool)


# --- comment collapse (DP-008) -------------------------------------------------

def test_repeated_rule_comments_collapse_to_one_counted_note():
    from docproof.models import Anchor, Finding, DocumentModel, ParagraphRef
    from docproof.pipeline import _collapse_repeated_comments

    paras = tuple(ParagraphRef(f"body-{i:04d}", "word/document.xml", "body",
                               "Some prose - and more.", "Normal")
                  for i in range(5))
    doc = DocumentModel(source_path="x.docx", paragraphs=paras)
    why = "House style sets a sentence-break dash as an unspaced em dash."

    def edit(i):
        return Finding(f"s-{i:04d}", "sweep", f"body-{i:04d}", "sweep_dash",
                       "Some prose - and more.", 1, "Some prose—and more.",
                       why, "high", status="validated",
                       anchor=Anchor(10, 13, " - ", "—"))

    validated = [edit(i) for i in range(5)]
    silenced = _collapse_repeated_comments(validated, doc, threshold=3)
    assert silenced == 4
    assert not validated[0].silent
    assert "Applied 5 times" in validated[0].explanation
    assert all(f.silent for f in validated[1:])
    # The copies keep their own explanation for the change log's Why column.
    assert validated[1].explanation == why


def test_collapse_leaves_small_groups_and_queries_alone():
    from docproof.models import Anchor, Finding, DocumentModel, ParagraphRef
    from docproof.pipeline import _collapse_repeated_comments

    paras = tuple(ParagraphRef(f"body-{i:04d}", "word/document.xml", "body",
                               "text", "Normal") for i in range(4))
    doc = DocumentModel(source_path="x.docx", paragraphs=paras)
    small = [Finding(f"s-{i}", "sweep", f"body-{i:04d}", "sweep_dash",
                     "text", 1, "text2", "same why", "high",
                     status="validated", anchor=Anchor(0, 1, "t", "T"))
             for i in range(3)]
    queries = [Finding(f"q-{i}", "sweep", f"body-{i:04d}", "unclosed_quote",
                       "text", 1, "text", "same question", "medium",
                       status="query", anchor=Anchor(0, 4, "text", ""))
               for i in range(4)]
    validated = small + queries
    assert _collapse_repeated_comments(validated, doc, threshold=3) == 0
    assert not any(f.silent for f in validated)
