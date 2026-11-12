"""O-06：隔离资料、实际发布与授权阅读副本，不接触真实库或模型。"""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from common.personal_tasks import OWNER
from test_reading_ingest_flow import flow as flow_fixture, submission

flow = flow_fixture


@pytest.fixture
def native_api(flow, monkeypatch):
    from channel.web.api import personal
    root, store, task, entry, read, tool = flow
    monkeypatch.setattr(personal, 'TaskStore', lambda: store)
    monkeypatch.setattr(personal, '_get_workspace_root', lambda: str(root))
    monkeypatch.setattr(personal, '_require_auth', lambda: None)
    monkeypatch.setattr(personal.web, 'header', Mock())
    monkeypatch.setattr(personal.web, 'input', lambda **kw: personal.web.storage(ordinal='0'))
    result = read.execute(dict(path=entry['path'], pages='1'))
    assert result.status == 'success', result.result
    assert result.result['reading_state'] == 'prepared'
    ref = result.media[0]
    ref.on_delivery('sent')
    ref.on_response(dict(id='response-offline', status='completed', output=[dict(
        type='message', content=[dict(type='output_text', text='整页为蓝色正方形。')])]))
    ref.on_delivery('completed')
    published = tool.execute(submission(task, result.result))
    assert published.status == 'success', published.result
    current = store.get(OWNER, task['task_id'])
    return personal, store, current, current['result_refs'][0], root


def test_native_display_only_immutable_parse_and_cache_by_parse_id(tmp_path):
    from channel.web.api.personal import _display_parse
    directory = tmp_path / 'parses'
    directory.mkdir()
    service = SimpleNamespace(root=tmp_path, parse=Mock(side_effect=AssertionError('禁止触发全文渲染')))
    cache = {}
    for parse_id in ('parse-one', 'parse-two'):
        payload = dict(units=[dict(text=parse_id)], parse_id=parse_id)
        (directory / (parse_id + '.json')).write_text(json.dumps(payload), encoding='utf-8')
        source = dict(document_id='doc', revision_id='rev', reading_id='reading', parse_id=parse_id)
        assert _display_parse(source, service, cache) == payload
    assert len(cache) == 2
    service.parse.assert_not_called()


def test_reference_has_model_type_and_copy_without_local_path(native_api, monkeypatch):
    mod, store, task, ref, root = native_api
    service = mod._source_service()
    monkeypatch.setattr(service, 'parse', Mock(side_effect=AssertionError('不应调用旧解析')))
    monkeypatch.setattr(mod, '_source_service', lambda: service)
    response = json.loads(mod.PersonalReferenceHandler().GET(task['task_id'], ref['publish_id']))
    assert response['status'] == 'success', response
    reference = response['reference']
    assert reference['type'] == 'visual_interpretation' and reference['origin'] == 'model'
    assert reference['location']['physical_page'] == 1
    assert reference['reading_copy']['page_map'] == [1]
    assert 'path' not in reference['reading_copy']
    assert str(root) not in json.dumps(reference)
    displayed = mod._decorate(task)
    assert displayed['reading_progress']['task_complete']
    assert len(displayed['reading_progress']['published']) == 1
    assert 'path' not in displayed['result_refs'][0]['sources'][0]['reading_copy']
    service.parse.assert_not_called()


def test_copy_authorization_tamper_and_client_path(native_api, monkeypatch):
    mod, store, task, ref, root = native_api
    handler = mod.PersonalReadingCopyHandler()
    data = handler.GET(task['task_id'], ref['publish_id'])
    assert data.startswith(b'%PDF-')
    assert any(call.args == ('Cache-Control', 'no-store') for call in mod.web.header.call_args_list)
    other = store.submit(OWNER, 'unrelated', {}, {'channel': 'web'}, 'cfg')
    assert json.loads(handler.GET(other['task_id'], ref['publish_id']))['code'] == 'forbidden'
    assert json.loads(handler.GET(task['task_id'], 'foreign'))['code'] == 'forbidden'
    for ordinal in ('-1', '1', 'bad'):
        monkeypatch.setattr(mod.web, 'input', lambda **kw: mod.web.storage(ordinal=ordinal))
        assert json.loads(handler.GET(task['task_id'], ref['publish_id']))['status'] == 'error'
    monkeypatch.setattr(mod.web, 'input', lambda **kw: mod.web.storage(ordinal='0', path='C:/private.pdf'))
    assert json.loads(handler.GET(task['task_id'], ref['publish_id']))['code'] == 'invalid_request'
    monkeypatch.setattr(mod.web, 'input', lambda **kw: mod.web.storage(ordinal='0'))
    reading = mod._reference_data(task['task_id'], ref['publish_id'], 0)
    target = root / reading['reading_copy']['path']
    target.write_bytes(target.read_bytes() + b'tampered')
    assert json.loads(handler.GET(task['task_id'], ref['publish_id']))['status'] == 'error'


def test_copy_requires_auth_before_lookup(native_api, monkeypatch):
    mod, _, task, ref, _ = native_api
    def deny():
        raise PermissionError('unauthenticated')
    monkeypatch.setattr(mod, '_require_auth', deny)
    with pytest.raises(PermissionError):
        mod.PersonalReadingCopyHandler().GET(task['task_id'], ref['publish_id'])


def test_missing_native_parse_never_falls_back_to_rendering(tmp_path):
    from channel.web.api.personal import _display_parse
    service = SimpleNamespace(root=tmp_path, parse=Mock(side_effect=AssertionError('不能回退渲染')))
    with pytest.raises(OSError):
        _display_parse(dict(document_id='d', revision_id='r', reading_id='reading', parse_id='missing'), service, {})
    service.parse.assert_not_called()


def test_progress_partial_publication_retains_task_status(tmp_path, monkeypatch):
    from test_reading_publication import env as publication_fixture
    from channel.web.api import personal
    from agent.knowledge import reading_publication as pub
    # 复用真实隔离台账夹具生成器，不模拟发布成功。
    store, task, wiki, targets, prepare = publication_fixture.__wrapped__(tmp_path)
    prepare()
    current = pub.publish_batch(store, task, 's0')
    monkeypatch.setattr(personal, 'TaskStore', lambda: store)
    monkeypatch.setattr(personal, '_get_workspace_root', lambda: str(wiki.source.workspace))
    progress = personal._decorate(current)
    assert progress['status'] == 'running'
    assert progress['reading_progress']['published'] == [targets[0]]
    assert progress['reading_progress']['remaining'] == [targets[1]]
    assert not progress['reading_progress']['task_complete']


def test_progress_prepared_and_unknown_are_pending(flow, monkeypatch):
    from channel.web.api import personal
    root, store, task, entry, read, _ = flow
    result = read.execute(dict(path=entry['path'], pages='1'))
    monkeypatch.setattr(personal, 'TaskStore', lambda: store)
    monkeypatch.setattr(personal, '_get_workspace_root', lambda: str(root))
    current = store.get(OWNER, task['task_id'])
    prepared = personal._reading_progress(current)
    assert len(prepared['prepared']) == len(prepared['pending']) == 1
    assert not prepared['delivered']
    result.media[0].on_delivery('sent')
    result.media[0].on_delivery('unknown')
    unknown = personal._reading_progress(current)
    assert len(unknown['unknown']) == len(unknown['pending']) == 1
    assert not unknown['delivered'] and not unknown['task_complete']
