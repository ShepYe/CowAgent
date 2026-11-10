"""T06：真实临时 TaskStore/SourceService，模型调度替身。"""
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from common.personal_tasks import TaskStore, OWNER


@pytest.fixture
def api(tmp_path, monkeypatch):
    from channel.web.api import personal
    store = TaskStore(tmp_path / 'personal')
    monkeypatch.setattr(personal, 'TaskStore', lambda: store)
    monkeypatch.setattr(personal, '_require_auth', lambda: None)
    monkeypatch.setattr(personal.web, 'header', lambda *a, **kw: None)
    monkeypatch.setattr(personal.web, 'ctx', SimpleNamespace(env={}))
    monkeypatch.setattr(personal.web.webapi.ctx, 'headers', [], raising=False)
    monkeypatch.setattr(personal, '_get_workspace_root', lambda: str(tmp_path / 'workspace'))
    monkeypatch.setattr('models.config_revision.capture_config_revision', lambda: 'cfg-test')
    enqueue = Mock()
    monkeypatch.setattr('common.personal_tasks.enqueue', enqueue)
    return personal, store, enqueue


def body(api, monkeypatch, value):
    monkeypatch.setattr(api.web, 'data', lambda: json.dumps(value).encode())


def submit(api, monkeypatch, data=b'hello', request='r1'):
    monkeypatch.setattr(api, '_multipart_lists', lambda _: {
        'file': [SimpleNamespace(filename='资料.txt', file=io.BytesIO(data))],
        'request_id': [request], 'text': ['整理资料'],
    })
    return json.loads(api.PersonalTasksHandler().POST())


def test_upload_archives_before_dispatch_and_deduplicates(api, monkeypatch):
    mod, store, enqueue = api
    first = submit(mod, monkeypatch)
    second = submit(mod, monkeypatch)
    assert first['status'] == 'success'
    task = first['task']
    assert task['status'] == 'queued'
    assert task['owner_id'] == OWNER
    assert second['task']['task_id'] == task['task_id']
    assert len(store.list(OWNER)) == 1
    assert len(store.get(OWNER, task['task_id'])['attachments']) == 1
    assert store.attachment(OWNER, task['task_id'], 'file-0')['filename'] == '资料.txt'
    enqueue.assert_called_with(task['task_id'])
    conflict = submit(mod, monkeypatch, data=b'changed')
    assert conflict['code'] == 'idempotency_conflict'


def test_attempt_conflict_returns_latest_and_no_dispatch(api, monkeypatch):
    mod, store, enqueue = api
    task = store.submit(OWNER, 'r', {}, {'channel': 'web'}, 'cfg')
    body(mod, monkeypatch, {'action': 'cancel', 'attempt': 2})
    response = json.loads(mod.PersonalTaskHandler().POST(task['task_id']))
    assert response['code'] == 'version_conflict'
    assert response['task']['status'] == 'queued'
    enqueue.assert_not_called()


@pytest.mark.parametrize('spoof', [{'owner': 'intruder'}, {'owner_id': 'intruder'}])
def test_owner_is_never_client_controlled(api, monkeypatch, spoof):
    mod, store, _ = api
    task = store.submit(OWNER, 'r', {}, {'channel': 'web'}, 'cfg')
    body(mod, monkeypatch, {'action': 'cancel', 'attempt': 1, **spoof})
    assert json.loads(mod.PersonalTaskHandler().POST(task['task_id']))['code'] == 'invalid_request'
    assert store.get(OWNER, task['task_id'])['status'] == 'queued'


def test_cancel_wait_input_and_retry(api, monkeypatch):
    mod, store, enqueue = api
    task = store.submit(OWNER, 'r', {}, {'channel': 'web'}, 'cfg')
    store.claim(OWNER, task['task_id'])
    store.wait_input(OWNER, task['task_id'], 1, '用途？')
    body(mod, monkeypatch, {'action': 'update_input', 'attempt': 1, 'input': '长期资料'})
    assert json.loads(mod.PersonalTaskHandler().POST(task['task_id']))['task']['status'] == 'queued'
    store.claim(OWNER, task['task_id'])
    store.fail(OWNER, task['task_id'], 1, 'index_failed', 'pending_index')
    body(mod, monkeypatch, {'action': 'retry', 'attempt': 1})
    assert json.loads(mod.PersonalTaskHandler().POST(task['task_id']))['task']['attempt'] == 2
    body(mod, monkeypatch, {'action': 'cancel', 'attempt': 2})
    assert json.loads(mod.PersonalTaskHandler().POST(task['task_id']))['task']['status'] == 'cancelled'
    assert enqueue.call_count == 2


def test_original_only_resolves_registered_attachment(api, monkeypatch):
    mod, _, _ = api
    task = submit(mod, monkeypatch)['task']
    headers = Mock()
    monkeypatch.setattr(mod.web, 'header', headers)
    assert mod.PersonalOriginalHandler().GET(task['task_id'], 'file-0') == b'hello'
    disposition = next(c.args[1] for c in headers.call_args_list if c.args[0] == 'Content-Disposition')
    assert "filename*=UTF-8''" in disposition
    assert '%E8%B5%84' in disposition
    assert json.loads(mod.PersonalOriginalHandler().GET(task['task_id'], '../../config.json'))['code'] == 'forbidden'


def test_binding_requires_desktop_token_even_with_web_auth(api, monkeypatch):
    mod, _, _ = api
    monkeypatch.setattr(mod, '_desktop_token_matches', lambda: False)
    with pytest.raises(mod.web.HTTPError):
        mod.PersonalBindingHandler().GET()
    with pytest.raises(mod.web.HTTPError):
        mod.PersonalBindingHandler().POST()


def test_binding_writes_only_trusted_fields(api, monkeypatch):
    mod, _, _ = api
    monkeypatch.setattr(mod, '_desktop_token_matches', lambda: True)
    save = Mock()
    monkeypatch.setattr('channel.web.api.integrations.save_config_values', save)
    binding = dict(channel_instance_id='feishu-main', app_id='app', tenant_id='tenant', sender_open_id='open')
    body(mod, monkeypatch, {'binding': {**binding, 'owner_id': 'other'}})
    assert json.loads(mod.PersonalBindingHandler().POST())['code'] == 'invalid_request'
    save.assert_not_called()
    body(mod, monkeypatch, {'binding': binding})
    assert json.loads(mod.PersonalBindingHandler().POST())['status'] == 'success'
    save.assert_called_once_with({'personal_feishu_binding': binding})


def test_withdraw_is_bound_to_task_and_expected_source_state(api, monkeypatch, tmp_path):
    mod, store, _ = api
    source = tmp_path / 'source.txt'
    source.write_text('知识原文', encoding='utf-8')
    svc = mod._source_service()
    receipt = svc.receive(source, filename='source.txt', request_id='source1', origin={'channel': 'web'})
    task = store.submit(OWNER, 'r', {}, {'channel': 'web'}, 'cfg')
    store.claim(OWNER, task['task_id'])
    store.complete(OWNER, task['task_id'], 1, 'published', [receipt])
    body(mod, monkeypatch, {'action': 'withdraw', 'attempt': 1, 'document_id': receipt['document_id'], 'expected_state': 0})
    response = json.loads(mod.PersonalTaskHandler().POST(task['task_id']))
    assert response['status'] == 'success'
    assert response['task']['result_refs'][0]['source_state']['validity'] == 'withdrawn'
    assert json.loads(mod.PersonalTaskHandler().POST(task['task_id']))['code'] == 'version_conflict'
    body(mod, monkeypatch, {'action': 'withdraw', 'attempt': 1, 'document_id': 'unrelated', 'expected_state': 0})
    assert json.loads(mod.PersonalTaskHandler().POST(task['task_id']))['code'] == 'forbidden'


@pytest.mark.parametrize('handler,method,args', [
    ('PersonalTasksHandler', 'GET', ()), ('PersonalTasksHandler', 'POST', ()),
    ('PersonalTaskHandler', 'GET', ('id',)), ('PersonalTaskHandler', 'POST', ('id',)),
    ('PersonalOriginalHandler', 'GET', ('id', 'attachment')),
    ('PersonalReferenceHandler', 'GET', ('id', 'publish')),
])
def test_every_task_route_authenticates_before_storage(api, monkeypatch, handler, method, args):
    mod, _, _ = api
    def deny():
        raise mod.web.unauthorized()
    monkeypatch.setattr(mod, '_require_auth', deny)
    storage = Mock(side_effect=AssertionError('unauthenticated storage access'))
    monkeypatch.setattr(mod, 'TaskStore', storage)
    with pytest.raises(mod.web.HTTPError):
        getattr(getattr(mod, handler)(), method)(*args)
    storage.assert_not_called()


def test_historical_reference_remains_readable_after_withdraw(api, monkeypatch, tmp_path):
    mod, store, _ = api
    source = tmp_path / 'source.txt'
    source.write_text('可核验原文', encoding='utf-8')
    svc = mod._source_service()
    receipt = svc.receive(source, filename='source.txt', request_id='source-ref', origin={'channel': 'web'})
    doc = svc.change(receipt['document_id'], purpose='long_term', expected_state=0, reason='分享资料')
    candidate = svc.prepare(receipt['revision_id'])
    published = svc.publish(candidate['candidate_id'])
    task = store.submit(OWNER, 'r-ref', {}, {'channel': 'web'}, 'cfg')
    store.claim(OWNER, task['task_id'])
    store.complete(OWNER, task['task_id'], 1, 'published', [published])
    monkeypatch.setattr(mod.web, 'input', lambda **kw: SimpleNamespace(ordinal='0'))
    result = json.loads(mod.PersonalReferenceHandler().GET(task['task_id'], published['publish_id']))
    assert result['reference']['text'] == '可核验原文'
    assert result['reference']['current_status'] == 'current'
    assert 'original' not in result['reference']
    svc.change(receipt['document_id'], action='withdraw', expected_state=doc['state'], reason='撤回')
    result = json.loads(mod.PersonalReferenceHandler().GET(task['task_id'], published['publish_id']))
    assert result['reference']['current_status'] == 'withdrawn'
    assert json.loads(mod.PersonalReferenceHandler().GET(task['task_id'], 'unrelated'))['code'] == 'forbidden'


def published_task(mod, store, tmp_path):
    source = tmp_path / 'source.txt'
    source.write_text('第一行证据\n第二行证据\n第三行证据', encoding='utf-8')
    svc = mod._source_service()
    receipt = svc.receive(source, filename='source.txt', request_id='src', origin={'channel': 'web'})
    svc.change(receipt['document_id'], purpose='long_term', expected_state=0, reason='分享资料')
    published = svc.publish(svc.prepare(receipt['revision_id'])['candidate_id'])
    task = store.submit(OWNER, 'parent', {}, {'channel': 'web'}, 'cfg')
    store.attach(OWNER, task['task_id'], source, 'source.txt', 'source')
    store.claim(OWNER, task['task_id'])
    store.complete(OWNER, task['task_id'], 1, 'published', [published])
    return svc, task, published


@pytest.mark.parametrize('action,purpose', [('correct', None), ('change_purpose', 'transient')])
def test_correction_or_purpose_immediately_excludes_old_evidence(api, monkeypatch, tmp_path, action, purpose):
    mod, store, enqueue = api
    svc, task, ref = published_task(mod, store, tmp_path)
    assert svc.search('证据')
    request = dict(action=action, attempt=1, document_id=ref['document_id'], expected_state=1, reason='第二行数据过期', purpose=purpose)
    body(mod, monkeypatch, request)
    response = json.loads(mod.PersonalTaskHandler().POST(task['task_id']))
    assert response['status'] == 'success'
    assert response['task']['result_refs'][0]['current_searchable'] is False
    assert svc.search('证据') == []
    doc = svc.get(ref['document_id'])
    assert doc['current'] is None
    assert doc['state'] == 2
    assert doc['validity'] == ('corrected' if action == 'correct' else 'active')
    assert doc['purpose'] == (purpose or 'long_term')
    assert json.loads(mod.PersonalTaskHandler().POST(task['task_id']))['code'] == 'version_conflict'
    enqueue.assert_not_called()


@pytest.mark.parametrize('changes', [
    {'reason': ''}, {'reason': '   '}, {'reason': None}, {'reason': {}},
    {'purpose': 'waiting'}, {'purpose': 'unknown'}, {'document_id': 'unrelated'},
    {'expected_state': True}, {'attempt': 9},
])
def test_source_edit_validation_before_side_effects(api, monkeypatch, tmp_path, changes):
    mod, store, _ = api
    svc, task, ref = published_task(mod, store, tmp_path)
    request = dict(action='change_purpose', attempt=1, document_id=ref['document_id'], expected_state=1, reason='只用于临时任务', purpose='transient')
    body(mod, monkeypatch, {**request, **changes})
    assert json.loads(mod.PersonalTaskHandler().POST(task['task_id']))['status'] == 'error'
    assert svc.get(ref['document_id'])['state'] == 1
    assert svc.search('证据')


def test_explicit_republication_creates_idempotent_task_without_restoring_visibility(api, monkeypatch, tmp_path):
    mod, store, enqueue = api
    svc, task, ref = published_task(mod, store, tmp_path)
    svc.change(ref['document_id'], action='correct', expected_state=1, reason='旧结论错误')
    request = dict(action='republish', attempt=1, document_id=ref['document_id'], expected_state=2, reason='已核对原文，更正后重新整理', request_id='republish-1')
    body(mod, monkeypatch, request)
    response = json.loads(mod.PersonalTaskHandler().POST(task['task_id']))
    new = response['task']
    assert response['status'] == 'success'
    assert new['task_id'] != task['task_id']
    assert new['status'] == 'queued'
    assert new['payload']['source_action']['expected_state'] == 2
    assert new['payload']['source_action']['document_id'] == ref['document_id']
    assert store.attachment(OWNER, new['task_id'], 'file-0')['sha256'] == store.get(OWNER, task['task_id'])['attachments'][0]['sha256']
    assert svc.search('证据') == []
    assert svc.get(ref['document_id'])['validity'] == 'corrected'
    enqueue.assert_called_with(new['task_id'])
    again = json.loads(mod.PersonalTaskHandler().POST(task['task_id']))
    assert again['task']['task_id'] == new['task_id']
    assert len(store.list(OWNER)) == 2


def test_arbitrary_reference_units_and_invalid_ordinals(api, monkeypatch, tmp_path):
    mod, store, _ = api
    svc, task, ref = published_task(mod, store, tmp_path)
    monkeypatch.setattr(mod.web, 'input', lambda **kw: SimpleNamespace(ordinal='2'))
    response = json.loads(mod.PersonalReferenceHandler().GET(task['task_id'], ref['publish_id']))
    assert response['reference']['text'] == '第三行证据'
    assert response['reference']['ordinal'] == 2
    assert response['reference']['unit_count'] == 3
    assert len(response['reference']['units']) == 3
    svc.change(ref['document_id'], action='correct', expected_state=1, reason='更正依据')
    response = json.loads(mod.PersonalReferenceHandler().GET(task['task_id'], ref['publish_id']))
    assert response['reference']['current_status'] == 'corrected'
    assert response['reference']['text'] == '第三行证据'
    for value in ('-1', '3', 'abc', '1.5'):
        monkeypatch.setattr(mod.web, 'input', lambda **kw: SimpleNamespace(ordinal=value))
        assert json.loads(mod.PersonalReferenceHandler().GET(task['task_id'], ref['publish_id']))['status'] == 'error'


def test_return_to_long_term_does_not_implicitly_republish(api, monkeypatch, tmp_path):
    mod, store, enqueue = api
    svc, task, ref = published_task(mod, store, tmp_path)
    svc.change(ref['document_id'], purpose='transient', expected_state=1, reason='临时用途')
    body(mod, monkeypatch, dict(action='change_purpose', attempt=1, document_id=ref['document_id'],
                               expected_state=2, reason='改为长期资料', purpose='long_term'))
    response = json.loads(mod.PersonalTaskHandler().POST(task['task_id']))
    assert response['status'] == 'success'
    assert response['task']['result_refs'][0]['source_state']['purpose'] == 'long_term'
    assert response['task']['result_refs'][0]['current_searchable'] is False
    assert svc.search('证据') == []
    assert len(store.list(OWNER)) == 1
    enqueue.assert_not_called()
