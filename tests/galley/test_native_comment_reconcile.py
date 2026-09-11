"""Comment cleanup must never replay or normalize previously verified edits."""
import json

import docx
import pytest

from docproof.attribution import PROOFREADER_AUTHOR
from docproof.utils.xml_helpers import DocxPackage, qn
from galley import comment_reconcile as cr
from galley.verify import applied_edits, deliverable_docx, paragraph_views
from tests.galley.test_settle import _build, _para_ids, _walk, _replay_config


def _prepared(tmp_path):
    document = docx.Document()
    paragraph = document.add_paragraph('Its mine.')
    document.add_comment(paragraph.runs, text='Keep this author note.', author=PROOFREADER_AUTHOR)
    source = tmp_path / 'book.docx'
    document.save(source)
    ids, _ = _para_ids(source)
    run = _build(tmp_path, source, [
        dict(para_id=ids[0], original_text='Its', corrected_text="It's"),
        dict(para_id=ids[0], original_text='Its mine.', corrected_text="It's mine.",
             force_query=True, explanation='Missing apostrophe')])
    # A prior validated writer may legitimately retain an ASCII apostrophe.
    # Replay curls it; native comment cleanup must preserve its actual bytes.
    path = deliverable_docx(run)
    package = DocxPackage(path)
    for text in package.tree('word/document.xml').iter(qn('w:t')):
        if text.text and '\u2019' in text.text:
            text.text = text.text.replace('\u2019', "'")
    package.mark_modified('word/document.xml')
    package.save(path)
    payload = json.loads((run / 'findings.json').read_text())
    for row in payload['findings']:
        row['corrected_text'] = row['corrected_text'].replace('\u2019', "'")
        if row.get('applied'):
            row['anchor']['insert_text'] = row['anchor']['insert_text'].replace('\u2019', "'")
    (run / 'findings.json').write_text(json.dumps(payload))
    query = next(row for row in payload['findings'] if row.get('queried'))
    _walk(run, [])
    assert paragraph_views(run)[1][ids[0]] == "It's mine."
    return run, source, query['finding_id']


def test_comment_cleanup_preserves_ascii_revision_and_all_reading_evidence(tmp_path, monkeypatch):
    from galley.settle import Settler
    run, source, key = _prepared(tmp_path)
    views, edits = paragraph_views(run), applied_edits(run)
    monkeypatch.setattr(Settler, '_rebuild', lambda *a, **k: pytest.fail('Comment cleanup replayed edits'))
    from docproof.config import load_config
    Settler(run, cfg=load_config(_replay_config(tmp_path)), manuscript=source,
            error_dir='config/error_types')._reconcile_comments()
    assert paragraph_views(run) == views
    assert applied_edits(run) == edits
    comments = cr.actual_comments(deliverable_docx(run))
    assert len(comments) == 1 and comments[0]['explanation'] == 'Keep this author note.'
    transition = next((run / 'settle' / 'comment-transitions').iterdir())
    receipt = json.loads((transition / 'receipt.json').read_text())
    cr.prove_comment_only(transition / 'before' / deliverable_docx(run).name,
                          deliverable_docx(run), receipt['removed_comment_ids'])
    assert receipt['before']['build_sha256'] != receipt['after']['build_sha256']
    for name in ('accepted_sha256', 'source_sha256', 'paragraph_sha256'):
        assert receipt['before'][name] == receipt['after'][name]
    # Settlement may project its rows, but native cleanup itself preserves the
    # original artifacts and its separately identified package transition.
    assert (run / 'reading-input-transitions.json').exists()
    assert (transition / 'before' / 'finished_walk.json').read_bytes() == (run / 'finished_walk.json').read_bytes()


@pytest.mark.parametrize('crash,existing_ledger', [(False, False), (True, True)])
def test_partial_install_rolls_back_doc_findings_and_transition_ledger(tmp_path, monkeypatch,
                                                                     crash, existing_ledger):
    run, source, key = _prepared(tmp_path)
    ledger = run / 'reading-input-transitions.json'
    if existing_ledger:
        ledger.write_text('{"schema_version":1,"transitions":[]}')
    from galley.verify import build_fingerprints
    for name in ('finished_walk.json', 'change_verify.json'):
        artifact = json.loads((run / name).read_text())
        artifact.update(build_fingerprints(run))
        (run / name).write_text(json.dumps(artifact))
    document = deliverable_docx(run)
    names = [document.name, 'findings.json', 'finished_walk.json', 'change_verify.json', ledger.name]
    original = {name: (run / name).read_bytes() if (run / name).exists() else None for name in names}
    real, calls = cr._atomic_copy, []

    def interrupted(src, target):
        calls.append(target)
        if len(calls) == (5 if crash else 2):
            if crash:
                raise KeyboardInterrupt('simulated process interruption')
            raise OSError('simulated install failure')
        return real(src, target)

    monkeypatch.setattr(cr, '_atomic_copy', interrupted)
    with pytest.raises(KeyboardInterrupt if crash else OSError):
        cr.remove_queries(run, source, {key: 'already_corrected'}, reason='test cleanup')
    if crash:
        assert document.read_bytes() != original[document.name]
        assert (run / 'change_verify.json').read_bytes() != original['change_verify.json']
        cr.recover_comment_transactions(run)
    for name, data in original.items():
        assert ((run / name).read_bytes() if (run / name).exists() else None) == data
    state = next((run / 'settle' / 'comment-transitions').glob('*/transaction.json'))
    assert json.loads(state.read_text())['status'] == 'rolled_back'


def test_unowned_comment_is_rejected_before_live_mutation(tmp_path):
    run, source, key = _prepared(tmp_path)
    payload = json.loads((run / 'findings.json').read_text())
    next(r for r in payload['findings'] if r['finding_id'] == key)['explanation'] = 'Different question'
    (run / 'findings.json').write_text(json.dumps(payload))
    before = deliverable_docx(run).read_bytes()
    with pytest.raises(ValueError, match='uniquely own'):
        cr.remove_queries(run, source, {key: 'already_corrected'}, reason='test cleanup')
    assert deliverable_docx(run).read_bytes() == before


def test_package_proof_rejects_changed_revision_or_format(tmp_path):
    run, source, key = _prepared(tmp_path)
    cr.remove_queries(run, source, {key: 'already_corrected'}, reason='test cleanup')
    transition = next((run / 'settle' / 'comment-transitions').iterdir())
    receipt = json.loads((transition / 'receipt.json').read_text())
    path = deliverable_docx(run)
    pkg = DocxPackage(path)
    revision = next(pkg.tree('word/document.xml').iter(qn('w:ins')))
    revision.set(qn('w:author'), 'Changed revision author')
    pkg.mark_modified('word/document.xml')
    pkg.save(path)
    with pytest.raises(ValueError, match='other manuscript content'):
        cr.prove_comment_only(transition / 'before' / path.name, path,
                              receipt['removed_comment_ids'])


def test_native_cleanup_keeps_full_read_artifacts_byte_identical(tmp_path):
    run, source, key = _prepared(tmp_path)
    before = {name: (run / name).read_bytes() for name in ('finished_walk.json', 'change_verify.json')}
    edits = applied_edits(run)
    cr.remove_queries(run, source, {key: 'already_corrected'}, reason='test cleanup')
    assert applied_edits(run) == edits
    assert before == {name: (run / name).read_bytes() for name in before}


def test_query_intake_survives_interruption_after_transaction_commit(tmp_path, monkeypatch):
    from docproof.config import load_config
    from galley.settle import Settler
    run, source, key = _prepared(tmp_path)
    # This question needs correction intake, rather than stale-query removal.
    payload = json.loads((run / 'findings.json').read_text())
    next(r for r in payload['findings'] if r['finding_id'] == key)['corrected_text'] = "It is mine."
    (run / 'findings.json').write_text(json.dumps(payload))
    real = cr.remove_queries

    def commit_then_interrupt(*args, **kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt('after commit, before settlement save')

    def settler():
        return Settler(run, cfg=load_config(_replay_config(tmp_path)), manuscript=source,
                       error_dir='config/error_types')
    monkeypatch.setattr(cr, 'remove_queries', commit_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        settler()._intake_corrections()
    assert not (run / 'settlement.json').exists()
    monkeypatch.setattr(cr, 'remove_queries', real)
    recovered = settler()._intake_corrections()
    assert len(recovered) == 1
    assert recovered[0].suggestion == 'It is mine.'
    assert recovered[0].quote == 'Its mine.'
    from galley.settle import SettlementRecord
    resumed = settler()
    record = SettlementRecord(recovered[0].id, 1, 'drop', None, '', '',
                              'exact reviewed input', 'test',
                              input_evidence=recovered[0].to_json())
    resumed.settlement.records.append(record)
    assert resumed._intake_corrections() == []
    record.action = 'internal_repair'
    assert len(resumed._intake_corrections()) == 1
    record.action = 'drop'
    record.input_evidence = {**record.input_evidence, 'suggestion': 'A different correction'}
    assert len(resumed._intake_corrections()) == 1
