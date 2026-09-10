from datetime import datetime
import json
import pytest

from docproof.interior import daily_digest as digest


def at(value):
    return datetime.fromisoformat(value)


def setup(home, monkeypatch, armed='2026-09-10T18:00:00+00:00'):
    config = {'enabled':True,'recipient':'Quinton@Atmospherepress.com','time':'17:00',
              'timezone':'America/New_York','armed_at':armed}
    digest.save_json(home/'daily-digest/config.json',config)
    monkeypatch.setattr(digest,'access_token',lambda *a,**k:'access-secret')
    return config


def empty(home=None):
    return {'books':[],'batches':[],'events':[],'jobs':{},'worker':{},'error':None}


def test_no_send_when_disabled_or_before_five(tmp_path,monkeypatch):
    calls=[]
    sender=lambda *a,**k:calls.append(a)
    assert digest.poll(tmp_path,now=at('2026-09-10T22:00:00+00:00'),sender=sender)['state']=='disabled'
    setup(tmp_path,monkeypatch)
    result=digest.poll(tmp_path,now=at('2026-09-10T20:59:59+00:00'),sender=sender,snapshotter=empty)
    assert result['state']=='scheduled' and result['next_at']=='2026-09-10T17:00:00-04:00'
    assert calls==[]


def test_one_email_per_day_including_quiet_days_and_restarts(tmp_path,monkeypatch):
    setup(tmp_path,monkeypatch)
    calls=[]
    def sender(token,recipient,message,**kwargs):
        calls.append(message)
        assert recipient=='Quinton@Atmospherepress.com'
        return 'gmail-receipt'
    for now in ['2026-09-10T21:00:00+00:00','2026-09-10T21:05:00+00:00',
                '2026-09-10T23:59:00+00:00','2026-09-11T21:00:00+00:00']:
        digest.poll(tmp_path,now=at(now),sender=sender,snapshotter=empty)
    assert len(calls)==2
    assert 'Completed (0)\nNone.' in calls[0]['body']


@pytest.mark.parametrize('now,expected',[('2026-07-10T20:59:00+00:00',False),
    ('2026-07-10T21:00:00+00:00',True),('2026-01-10T21:00:00+00:00',False),
    ('2026-01-10T22:00:00+00:00',True)])
def test_five_pm_tracks_daylight_saving(now,expected):
    day=now[:10]
    cfg={'time':'17:00','timezone':'America/New_York','armed_at':day+'T12:00:00+00:00'}
    assert digest.slot(cfg,{},at(now))['due']==expected


def test_sleep_gap_sends_one_catchup_then_returns_to_five(tmp_path,monkeypatch):
    setup(tmp_path,monkeypatch)
    calls=[]
    def sender(*a,**k): calls.append(a); return 'receipt'
    for now in ['2026-09-13T14:00:00+00:00','2026-09-13T21:00:00+00:00',
                '2026-09-14T14:00:00+00:00','2026-09-14T21:00:00+00:00']:
        digest.poll(tmp_path,now=at(now),sender=sender,snapshotter=empty)
    assert len(calls)==2


def test_timeout_does_not_retry_or_write_exception_secrets(tmp_path,monkeypatch):
    setup(tmp_path,monkeypatch)
    calls=[]
    def sender(*a,**k): calls.append(a); raise TimeoutError('access-secret')
    first=digest.poll(tmp_path,now=at('2026-09-10T21:00:00+00:00'),sender=sender,snapshotter=empty)
    second=digest.poll(tmp_path,now=at('2026-09-10T21:05:00+00:00'),sender=sender,snapshotter=empty)
    assert first['state']==second['state']=='delivery_uncertain' and len(calls)==1
    assert all('access-secret' not in p.read_text('utf-8') for p in (tmp_path/'daily-digest').rglob('*.json'))


def test_process_death_at_send_boundary_is_never_replayed(tmp_path,monkeypatch):
    setup(tmp_path,monkeypatch)
    def sender(*a,**k): raise SystemExit()
    with pytest.raises(SystemExit):
        digest.poll(tmp_path,now=at('2026-09-10T21:00:00+00:00'),sender=sender,snapshotter=empty)
    result=digest.poll(tmp_path,now=at('2026-09-10T21:05:00+00:00'),
                       sender=lambda *a,**k:pytest.fail('duplicate send'),snapshotter=empty)
    assert result['state']=='delivery_uncertain'


def test_confirmed_mail_recovers_watermark_after_state_write_failure(tmp_path,monkeypatch):
    setup(tmp_path,monkeypatch)
    save=digest.save_json
    def fail_state(path,value):
        if path.name=='state.json' and value.get('last_sent_at'): raise OSError('disk interruption')
        return save(path,value)
    monkeypatch.setattr(digest,'save_json',fail_state)
    with pytest.raises(OSError):
        digest.poll(tmp_path,now=at('2026-09-10T21:00:00+00:00'),sender=lambda *a,**k:'receipt',snapshotter=empty)
    monkeypatch.setattr(digest,'save_json',save)
    result=digest.poll(tmp_path,now=at('2026-09-10T21:05:00+00:00'),
        sender=lambda *a,**k:pytest.fail('duplicate send'),snapshotter=empty)
    assert result['state']=='scheduled'
    assert digest._read(tmp_path/'daily-digest/state.json')['last_sent_at']


def test_preparation_failure_can_retry_without_counting_an_email(tmp_path,monkeypatch):
    setup(tmp_path,monkeypatch)
    def broken(*a,**k): raise ValueError('private credential detail')
    monkeypatch.setattr(digest,'access_token',broken)
    result=digest.poll(tmp_path,now=at('2026-09-10T21:00:00+00:00'),snapshotter=empty)
    assert result['state']=='preparation_failed'
    assert not (tmp_path/'daily-digest/state.json').exists()
    assert 'private credential detail' not in json.dumps(result)


def test_complete_hold_waiting_and_failed_rows_are_distinct():
    data=empty()
    data.update(books=[{'project_id':'p','author':'Author Name','title':'The Book'}],
        batches=[{'project_id':'p','batch_id':'done','state':'delivered','job_id':'j'},
                 {'project_id':'p','batch_id':'hold','state':'held','job_id':'h'},
                 {'project_id':'p','batch_id':'running','state':'delivery','job_id':'r'}],
        events=[{'project_id':'p','batch_id':None,'reason':'','history_state':None},
                {'project_id':None,'batch_id':None,'reason':'ambiguous','history_state':None}],
        jobs={'j':{'result':{'counts':{'edits':7,'unresolved':0}},'uploaded':{
             'Name - Book 4.5.indd':'book-id','Name - Book 4.5.corrections.xlsx':'sheet-id'}}},
        worker={'state':'error','error':'SECRET_PROVIDER_BODY'},error='SECRET_TOKEN')
    cfg={'timezone':'America/New_York'}
    message=digest.compose(data,set(),at('2026-09-10T21:00:00+00:00'),cfg)
    assert message['counts']=={'completed':1,'review':2,'in_progress':1,'waiting_books':1,'system_issues':3}
    assert message['batch_ids']==['done']
    assert 'https://drive.google.com/file/d/book-id/view' in message['body']
    assert 'SECRET_' not in message['body']
    next_message=digest.compose(data,{'done'},at('2026-09-11T21:00:00+00:00'),cfg)
    assert next_message['counts']['completed']==0 and next_message['counts']['review']==2


def test_recipient_header_injection_is_rejected():
    with pytest.raises(ValueError,match='one valid'):
        digest.validate_config({'enabled':True,'recipient':'a@b.com\r\nBcc:evil@b.com'})


def test_gmail_transport_sends_only_the_configured_recipient_and_requires_receipt():
    import base64
    from email import policy
    from email.parser import BytesParser
    import io
    calls=[]
    def opener(request):
        calls.append(request)
        raw=json.loads(request.data)['raw']
        message=BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
        assert message['To']=='Quinton@Atmospherepress.com'
        assert message['Cc'] is None and message['Bcc'] is None
        assert message['Subject']=='Daily summary'
        assert message.get_content().strip()=='No new books.'
        return io.BytesIO(b'{"id":"gmail-message-id"}')
    result=digest.send('test-access','Quinton@Atmospherepress.com',
        {'subject':'Daily summary','body':'No new books.'},opener=opener)
    assert result=='gmail-message-id' and len(calls)==1
    assert calls[0].full_url=='https://gmail.googleapis.com/gmail/v1/users/me/messages/send'
    assert calls[0].get_header('Authorization')=='Bearer test-access'
    with pytest.raises(ValueError,match='message receipt'):
        digest.send('test-access','Quinton@Atmospherepress.com',
            {'subject':'Daily summary','body':'No new books.'},opener=lambda req:io.BytesIO(b'{}'))


def test_digest_launcher_does_not_restart_or_run_indesign(tmp_path,monkeypatch):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'tools'))
    from install_native_digest_windows import launcher_source, TASK_NAME
    source=launcher_source(tmp_path/'user-home',tmp_path/'chosen-repo')
    compile(source,'digest-launch.py','exec')
    assert TASK_NAME=='DocProof Interior Daily Digest'
    assert "sys.path.insert(0,str(repo))" in source
    assert "daily_digest import main" in source
    assert "'--continuous'" in source
    assert 'rehearsal' not in source and 'schtasks' not in source
