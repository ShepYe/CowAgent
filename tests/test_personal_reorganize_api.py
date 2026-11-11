"""K-04：重整入口使用真实留档及来源状态，调度和配置由替身隔离。"""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from common.personal_tasks import OWNER
from test_personal_web import api as api_fixture, body, published_task

api = api_fixture


def request(ref, **changes):
    return dict(action='reorganize', attempt=1, document_id=ref['document_id'],
                expected_state=1, request_id='organize-one', **changes)


def test_reorganize_reuses_verified_original_without_touching_parent(api, tmp_path, monkeypatch):
    mod, store, enqueue = api
    svc, parent, ref = published_task(mod, store, tmp_path)
    before = store.get(OWNER, parent['task_id'])
    body(mod, monkeypatch, request(ref))
    response = json.loads(mod.PersonalTaskHandler().POST(parent['task_id']))
    assert response['status'] == 'success', response
    child = response['task']
    assert child['payload']['reorganize'] == dict(document_id=ref['document_id'], revision_id=ref['revision_id'], expected_state=1)
    assert 'source_action' not in child['payload']
    assert 'knowledge_ingest' in child['payload']['text']
    assert child['config_revision'] == 'cfg-test'
    assert [{key: value for key, value in attachment.items()
             if key not in {'source_document_id', 'source_revision_id'}}
            for attachment in child['attachments']] == before['attachments']
    assert child['attachments'][0]['source_document_id'] == ref['document_id']
    assert child['attachments'][0]['source_revision_id'] == ref['revision_id']
    assert child['download_complete']
    assert child['origin']['parent_task_id'] == parent['task_id']
    assert store.get(OWNER, parent['task_id']) == before
    assert svc.search('证据')
    assert json.loads(mod.PersonalTaskHandler().POST(parent['task_id']))['task']['task_id'] == child['task_id']
    assert len(store.list()) == 2
    enqueue.assert_called_with(child['task_id'])


@pytest.mark.parametrize('change', ['withdrawn', 'transient', 'stale', 'corrupt', 'foreign'])
def test_reorganize_does_not_expand_source_permission(api, tmp_path, monkeypatch, change):
    mod, store, enqueue = api
    svc, parent, ref = published_task(mod, store, tmp_path)
    payload = request(ref)
    if change == 'withdrawn':
        svc.change(ref['document_id'], action='withdraw', expected_state=1, reason='撤回')
        payload['expected_state'] = 2
    elif change == 'transient':
        svc.change(ref['document_id'], purpose='transient', expected_state=1, reason='临时')
        payload['expected_state'] = 2
    elif change == 'stale':
        payload['expected_state'] = 0
    elif change == 'foreign':
        payload['document_id'] = 'other'
    else:
        Path(store.get(OWNER, parent['task_id'])['attachments'][0]['path']).write_bytes(b'corrupt')
    body(mod, monkeypatch, payload)
    assert json.loads(mod.PersonalTaskHandler().POST(parent['task_id']))['status'] == 'error'
    assert len(store.list()) == 1
    enqueue.assert_not_called()


def test_parallel_reorganize_only_admits_complete_child(api, tmp_path):
    mod, store, _ = api
    _, parent, ref = published_task(mod, store, tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        children = list(pool.map(lambda _: mod._reorganize(store, parent['task_id'], 1, request(ref)), range(2)))
    assert children[0]['task_id'] == children[1]['task_id']
    assert children[0]['attachments']
    assert len(store.list()) == 2
