import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_personal_web import api as api_fixture, body
from common.personal_tasks import OWNER

api = api_fixture

def failed_attachment(store, tmp_path):
    file = tmp_path / 'source.txt'
    file.write_text('antenna original', encoding='utf-8')
    task = store.submit(OWNER, 'original', {'text': '整理附件'},
                        {'channel': 'feishu', 'channel_instance_id': 'feishu', 'sender_open_id': 'fixture'}, 'cfg-old')
    store.attach(OWNER, task['task_id'], file, attachment_id='0')
    store.claim(OWNER, task['task_id'])
    store._change(OWNER, task['task_id'], 1, {'running'}, download_complete=True)
    return store.fail(OWNER, task['task_id'], 1, 'config_unavailable', 'download')


def test_explicit_resubmit_reuses_original_and_current_config(api, tmp_path, monkeypatch):
    mod, store, enqueue = api
    old = failed_attachment(store, tmp_path)
    body(mod, monkeypatch, {'action': 'resubmit', 'attempt': 1, 'request_id': 'new-request',
                            'config_revision': 'spoof', 'local_path': 'spoof'})
    response = json.loads(mod.PersonalTaskHandler().POST(old['task_id']))
    assert response['status'] == 'success', response
    new = response['task']
    assert new['task_id'] != old['task_id']
    assert new['config_revision'] == 'cfg-test'
    assert new['attachments'] == old['attachments']
    assert new['origin']['parent_task_id'] == old['task_id']
    assert new['origin']['sender_open_id'] == 'fixture'
    assert new['download_complete'] is True
    assert store.get(OWNER, old['task_id']) == old
    assert enqueue.call_args.args == (new['task_id'],)
    repeated = json.loads(mod.PersonalTaskHandler().POST(old['task_id']))
    assert repeated['task']['task_id'] == new['task_id']
    assert len(store.list()) == 2


@pytest.mark.parametrize('change', [
    'stale', 'running', 'source_action', 'candidates', 'published', 'other_error', 'corrupt'])
def test_resubmit_rejects_invalid_parent_without_admission(api, tmp_path, monkeypatch, change):
    mod, store, enqueue = api
    old = failed_attachment(store, tmp_path)
    if change == 'corrupt':
        from pathlib import Path
        Path(old['attachments'][0]['path']).write_text('corrupt', encoding='utf-8')
    elif change != 'stale':
        updates = {'running': {'status': 'running'}, 'source_action': {'payload': {'source_action': {}}},
                   'candidates': {'source_candidates': [{'candidate_id': 'old'}]},
                   'published': {'result_refs': [{'publish_id': 'old'}]},
                   'other_error': {'error': {'code': 'parse_failed'}}}[change]
        store._change(OWNER, old['task_id'], 1, {'failed'}, **updates)
    body(mod, monkeypatch, {'action': 'resubmit', 'attempt': 2 if change == 'stale' else 1, 'request_id': 'new'})
    response = json.loads(mod.PersonalTaskHandler().POST(old['task_id']))
    assert response['status'] == 'error'
    assert len(store.list()) == 1
    enqueue.assert_not_called()


def test_parallel_resubmissions_admit_one_complete_child(api, tmp_path):
    mod, store, _ = api
    old = failed_attachment(store, tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        children = list(pool.map(lambda _: mod._resubmit(store, old['task_id'], 1, 'same'), range(2)))
    assert children[0]['task_id'] == children[1]['task_id']
    assert children[0]['attachments'] == old['attachments']
    assert len(store.list()) == 2


def test_resubmitted_child_runs_with_current_revision_without_redownload(api, tmp_path, monkeypatch):
    from common import personal_worker as worker
    from agent.tools.knowledge_ingest import KnowledgeIngest
    from common.personal_tasks import task_scope
    from common.runtime_identity import identity_scope
    mod, store, _ = api
    old = failed_attachment(store, tmp_path)
    child = mod._resubmit(store, old['task_id'], 1, 'run-new')
    monkeypatch.setattr(worker, 'TaskStore', lambda: store)
    revisions = []
    monkeypatch.setattr('models.config_revision.require_config_revision', lambda value: revisions.append(value))
    monkeypatch.setattr(worker, '_notify', lambda *args: None)
    def download(task_store, task):
        assert task['download_complete']
        assert task['attachments'] == old['attachments']
    monkeypatch.setattr(worker, '_channels', {'feishu': (download, lambda *args: 'sent')})
    def agent(task):
        assert task['config_revision'] == 'cfg-test'
        with task_scope(task['task_id']), identity_scope(user_id=OWNER):
            result = KnowledgeIngest({'cwd': str(tmp_path), 'task_store': store}).execute({
                'task_id': task['task_id'], 'purpose': 'transient', 'reason': '隔离工具链验证'})
        assert result.status == 'success', result.result
        return '处理完成'
    monkeypatch.setattr(worker, 'run_agent', agent)
    worker._run(child['task_id'])
    assert revisions == ['cfg-test']
    assert store.get(OWNER, child['task_id'])['status'] == 'succeeded'
    assert store.get(OWNER, old['task_id']) == old


def test_config_unavailable_saved_wiki_candidate_is_kept_on_explicit_resubmit(api, tmp_path):
    from agent.knowledge.wiki import WikiService
    mod, store, _ = api
    old = failed_attachment(store, tmp_path)
    service = mod._source_service()
    source = service.receive(old['attachments'][0]['path'], filename='source.txt', request_id='saved', origin={'channel': 'test'})
    service.change(source['document_id'], purpose='long_term', expected_state=0, reason='分享')
    parsed = service.parse(source['revision_id'])
    wiki = WikiService(service.workspace)
    batch = wiki.prepare([dict(title='候选知识', category='工程', summary='说明保存的原始依据。',
                              blocks=[dict(content='antenna original', sources=[dict(document_id=source['document_id'],
                                  revision_id=source['revision_id'], parse_id=parsed['parse_id'], ordinal=0)])])], task_id=old['task_id'])
    store._change(OWNER, old['task_id'], 1, {'failed'},
                  wiki_batch_id=batch['batch_id'], wiki_workspace=str(service.workspace),
                  attachment_decisions={'0': {'purpose': 'long_term', 'ready': True}})
    before = store.get(OWNER, old['task_id'])
    new = mod._resubmit(store, old['task_id'], 1, 'new-current-config')
    assert new['config_revision'] == 'cfg-test'
    assert new['attachments'] == before['attachments']
    assert 'wiki_batch_id' not in new and 'attachment_decisions' not in new
    assert store.get(OWNER, old['task_id']) == before
    with service.transaction() as db:
        saved = db.execute('SELECT record,published FROM wiki_batches WHERE id=?', (batch['batch_id'],)).fetchone()
    assert saved[1] == 0 and json.loads(saved[0])['pages'][0]['summary'] == '说明保存的原始依据。'
