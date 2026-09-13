"""Real Word revision intake: originals, comments, crash recovery and tampering."""
from zipfile import ZipFile

from docx import Document
from lxml import etree
import pytest

from docproof.ingest import IngestError
from docproof.utils.xml_helpers import qn
from galley import fixed_intake as intake
from galley.fixed_documents import paragraph_views, write_manuscripts


def revised_book(tmp_path):
    document = Document()
    paragraph = document.add_paragraph("She ")
    run = paragraph.add_run("recieved")
    run.bold = True
    document.add_comment(run, text="Preserve this author note.", author="Author")
    paragraph.add_run(" two letters.")
    old = etree.Element(qn("w:del"), {qn("w:id"): "7", qn("w:author"): "Editor"})
    deleted = etree.SubElement(old, qn("w:r"))
    etree.SubElement(deleted, qn("w:delText")).text = "received"
    run._r.addprevious(old)
    new = etree.Element(qn("w:ins"), {qn("w:id"): "8", qn("w:author"): "Editor"})
    run._r.addprevious(new)
    new.append(run._r)
    header = document.sections[0].header.paragraphs[0].add_run("Running head")
    wrapper = etree.Element(qn("w:ins"), {qn("w:id"): "9"})
    header._r.addprevious(wrapper)
    wrapper.append(header._r)
    path = tmp_path / "Writer - Book 1.docx"
    document.save(path)
    return path


def test_revision_baseline_preserves_original_comments_and_other_parts(tmp_path, monkeypatch):
    source = revised_book(tmp_path)
    before = source.read_bytes()
    baseline, evidence = intake.prepare_source(source, tmp_path / "run")
    assert source.read_bytes() == before
    assert (tmp_path / "run/intake/original" / source.name).read_bytes() == before
    assert paragraph_views(baseline) == paragraph_views(source)
    with ZipFile(source) as original, ZipFile(baseline) as accepted:
        changed = {n for n in original.namelist() if original.read(n) != accepted.read(n)}
        assert changed == {"word/document.xml", "word/header1.xml"}
        assert original.read("word/comments.xml") == accepted.read("word/comments.xml")
    assert Document(baseline).paragraphs[0].runs[1].bold
    corrected = {pid: text.replace("recieved", "received") for pid, text in paragraph_views(baseline).items()}
    tracked, clean, _ = write_manuscripts(baseline, tmp_path / "final", corrected)
    assert paragraph_views(tracked, "reject") == paragraph_views(baseline)
    assert paragraph_views(tracked) == paragraph_views(clean) == corrected
    monkeypatch.setattr(intake, "accept_all_revisions", lambda *a: pytest.fail("Baseline rebuilt on resume"))
    assert intake.prepare_source(source, tmp_path / "run") == (baseline, evidence)


@pytest.mark.parametrize("member", ["original", "accepted", "receipt"])
def test_tampered_intake_cannot_be_certified(tmp_path, member):
    source = revised_book(tmp_path)
    baseline, evidence = intake.prepare_source(source, tmp_path / "run")
    target = (tmp_path / "run/intake/receipt.json" if member == "receipt" else
              tmp_path / "run/intake" / member / source.name)
    target.write_bytes(target.read_bytes() + b"changed")
    with pytest.raises(intake.FixedIntakeError, match="changed"):
        intake.validate_intake(tmp_path / "run", evidence, baseline)


def test_interruption_before_baseline_publication_can_retry(tmp_path, monkeypatch):
    source = revised_book(tmp_path)
    before = source.read_bytes()
    publish = intake.os.replace
    def interrupt(src, dst):
        if str(dst).endswith("/intake"):
            raise OSError("Interrupted intake publication")
        return publish(src, dst)
    monkeypatch.setattr(intake.os, "replace", interrupt)
    with pytest.raises(OSError, match="Interrupted"):
        intake.prepare_source(source, tmp_path / "run")
    assert source.read_bytes() == before
    assert not (tmp_path / "run/intake").exists()
    monkeypatch.setattr(intake.os, "replace", publish)
    baseline, evidence = intake.prepare_source(source, tmp_path / "run")
    assert intake.validate_intake(tmp_path / "run", evidence) == baseline


def test_unsupported_revision_stops_before_workflow_or_model_calls(tmp_path):
    source = revised_book(tmp_path)
    document = Document(source)
    props = document.add_table(rows=1, cols=1).cell(0, 0)._tc.get_or_add_tcPr()
    props.append(etree.Element(qn("w:cellIns"), {qn("w:id"): "10"}))
    document.save(source)
    before = source.read_bytes()
    from galley.fixed_workflow import FixedWorkflow
    with pytest.raises(IngestError, match="Can't resolve"):
        FixedWorkflow(source, tmp_path / "run", calls=object())
    assert source.read_bytes() == before
    assert not (tmp_path / "run/workflow.json").exists()
    assert not (tmp_path / "run/intake").exists()


def test_clean_manuscript_keeps_its_source_and_identity(tmp_path):
    source = tmp_path / "clean.docx"
    document = Document()
    document.add_paragraph("A quiet room.")
    document.save(source)
    assert intake.prepare_source(source, tmp_path / "run") == (source, None)
    assert not (tmp_path / "run/intake").exists()


def test_source_change_refuses_to_rebuild_existing_baseline(tmp_path):
    source = revised_book(tmp_path)
    baseline, evidence = intake.prepare_source(source, tmp_path / "run")
    source.write_bytes(source.read_bytes() + b"changed")
    with pytest.raises(intake.FixedIntakeError, match="incoming manuscript changed"):
        intake.prepare_source(source, tmp_path / "run")
    assert intake.validate_intake(tmp_path / "run", evidence) == baseline


def test_deleted_paragraph_mark_uses_words_accepted_join_and_style(tmp_path):
    from test_ingest import _tracked_doc
    source = _tracked_doc(tmp_path)
    original = source.read_bytes()
    baseline, evidence = intake.prepare_source(source, tmp_path / "run")
    joined = next(p for p in Document(baseline).paragraphs if p.text == "The first half and the second half.")
    assert joined.style.name == "Quote"
    assert source.read_bytes() == original
    assert intake.validate_intake(tmp_path / "run", evidence) == baseline
