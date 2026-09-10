import asyncio
import json
import os
import sys

import pytest

from docproof.interior.evidence_server import EvidenceStore, write_manifest


def fixture_registry(tmp_path):
    evidence = tmp_path / 'native.json'
    evidence.write_text(json.dumps({'stories': [{'id': '42', 'text': 'One teh. Two teh.'}]}))
    manifest = write_manifest(tmp_path, {'baseline': evidence}, {})
    return manifest, evidence


def test_frozen_registry_rejects_paths_and_changed_content(tmp_path):
    manifest, evidence = fixture_registry(tmp_path)
    store = EvidenceStore(manifest)
    assert 'path' not in store.index()['baseline']
    with pytest.raises(ValueError, match='Unknown evidence'):
        store.data(str(evidence))
    assert store.find('baseline', 'teh')['total_matches'] == 2
    evidence.write_text('{}')
    with pytest.raises(ValueError, match='changed'):
        store.data('baseline')


def test_registry_cannot_expose_files_outside_job(tmp_path):
    job = tmp_path / 'job'
    job.mkdir()
    outside = tmp_path / 'private.json'
    outside.write_text('{}')
    with pytest.raises(ValueError, match='inside its job'):
        write_manifest(job, {'other': outside}, {})


def test_evidence_pagination_is_complete(tmp_path):
    manifest, evidence = fixture_registry(tmp_path)
    store = EvidenceStore(manifest)
    first = store.read_text('baseline', characters=8)
    rest = store.read_text('baseline', start=first['next_start'])
    assert first['text'] + rest['text'] == evidence.read_text()
    assert store.field('baseline', '/stories/0/text', start=4, limit=3)['value'] == 'teh'


def test_batched_anchors_keep_exact_counts_and_bounded_context(tmp_path):
    evidence = tmp_path / 'native.json'
    evidence.write_text(json.dumps({'stories': [{'id': '42', 'text': 'teh ' * 27}]}))
    store = EvidenceStore(write_manifest(tmp_path, {'baseline': evidence}, {}))
    results = store.find_many('baseline', ['teh', 'missing'])
    assert results[0]['total_matches'] == 27
    assert len(results[0]['matches']) == 3 and results[0]['next_start'] == 3
    assert store.find('baseline', 'teh', start=3)['matches'][0]['offset'] == 12
    assert results[1]['total_matches'] == 0
    with pytest.raises(ValueError, match='between 1 and 20'):
        store.find_many('baseline', ['teh'] * 21)


def test_stdio_server_can_read_registered_evidence(tmp_path):
    import base64
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    manifest, evidence = fixture_registry(tmp_path)
    picture = tmp_path / 'page.png'
    picture.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aR0cAAAAASUVORK5CYII='))
    manifest = write_manifest(tmp_path, {'baseline': evidence}, {'page-1': picture})

    async def check():
        params = StdioServerParameters(command=sys.executable,
            args=['-m', 'docproof.interior.evidence_server', '--manifest', str(manifest)],
            env={key: value for key, value in os.environ.items() if key in {'PATH', 'SYSTEMROOT', 'TEMP', 'TMP'}})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert names == {'list_evidence', 'read_evidence', 'read_json_field', 'find_in_stories',
                                 'find_story_anchors', 'view_evidence_image', 'view_evidence_images'}
                result = await session.call_tool('find_in_stories', {'evidence_id': 'baseline', 'text': 'teh'})
                assert not result.isError
                assert json.loads(result.content[0].text)['total_matches'] == 2
                viewed = await session.call_tool('view_evidence_image', {'evidence_id': 'page-1'})
                assert not viewed.isError
                assert viewed.content[0].type == 'image'
                assert base64.b64decode(viewed.content[0].data) == picture.read_bytes()
                batched = await session.call_tool('view_evidence_images', {'evidence_ids': ['page-1']})
                assert not batched.isError
                assert batched.content[0].type == 'text' and batched.content[0].text == 'page-1'
                assert batched.content[1].type == 'image'
                assert base64.b64decode(batched.content[1].data) == picture.read_bytes()
                denied_batch = await session.call_tool('view_evidence_images', {'evidence_ids': ['page-1', '../private.png']})
                assert denied_batch.isError
                denied = await session.call_tool('read_evidence', {'evidence_id': '../private.json'})
                assert denied.isError
    asyncio.run(check())
