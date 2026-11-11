"""规格5.6/U-01：真实 handler、SQLite 与完整备份的隔离契约验收。

只替换请求载体、认证状态与 Agent 名册，不替换业务服务或发布结果。
资料归并属于前端投影：这里锁住其依赖的 document/attachment/parent 身份，
不在测试中复制归并算法。无模型、无真实账号、无真实工作空间。
"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.knowledge.personal_assistant import SourceService
from agent.knowledge.wiki import WikiService
from agent.registry import AgentProfile, AgentRegistry
from common.personal_tasks import OWNER, TaskStore


@pytest.fixture
def catalog_api(tmp_path, monkeypatch):
    from channel.web.api import knowledge, personal
    from channel.web.core import _common
    roots = {name: tmp_path / name for name in ('alpha', 'beta')}
    for root in roots.values():
        (root / 'knowledge').mkdir(parents=True)
    registry = AgentRegistry([AgentProfile(name, name, str(root)) for name, root in roots.items()], 'alpha')
    monkeypatch.setattr('agent.registry.get_agent_registry', lambda: registry)
    authenticated = {'value': True}
    monkeypatch.setattr(_common, '_check_auth', lambda: authenticated['value'])
    monkeypatch.setattr(knowledge.web, 'header', lambda *a, **kw: None)
    monkeypatch.setattr(knowledge.web, 'ctx', SimpleNamespace(env={}))
    monkeypatch.setattr(knowledge.web, 'cookies', lambda: {})
    monkeypatch.setattr(knowledge.web.webapi.ctx, 'headers', [], raising=False)
    store = TaskStore(tmp_path / 'tasks')
    monkeypatch.setattr(personal, 'TaskStore', lambda: store)
    monkeypatch.setattr(personal, '_get_workspace_root', lambda: str(roots['alpha']))

    def request(handler, *, agent='alpha', method='GET', body=None, **query):
        params = dict(agent_id=agent, **query)
        monkeypatch.setattr(knowledge.web, 'input', lambda **defaults: SimpleNamespace(**dict(defaults, **params)))
        monkeypatch.setattr(knowledge.web, 'data', lambda: json.dumps({} if body is None else body, ensure_ascii=False).encode('utf-8'))
        return json.loads(getattr(getattr(knowledge, handler)(), method)())

    def action(name, ref, category=None, **changes):
        payload = dict(page_id=ref['page_id'], expected_version=ref['page_version'],
                       expected_category_revision=ref['category_revision'])
        if category is not None:
            payload['category'] = category
        payload.update(changes)
        return request('KnowledgeActionHandler', method='POST', body=dict(
            agent_id='alpha', action=name, payload=payload))

    return SimpleNamespace(knowledge=knowledge, personal=personal, roots=roots, store=store,
                           request=request, action=action, authenticated=authenticated)


def publish_topic(root, *, page_id='budget', title='链路预算', category='技术/射频',
                  content='可靠预算为42 dB。', previous=None, sources=None, depends_on=None):
    service = SourceService(root)
    if sources is None:
        original = root / '资料.txt'
        original.write_text('测试原件：预算42 dB。', encoding='utf-8')
        source = service.receive(original, filename=original.name, request_id='source-' + page_id,
                                 origin={'channel': 'web'})
        service.change(source['document_id'], purpose='long_term', expected_state=0, reason='测试长期资料')
        parsed = service.parse(source['revision_id'])
        sources = [dict(document_id=source['document_id'], revision_id=source['revision_id'],
                        parse_id=parsed['parse_id'], ordinal=0)]
    page = dict(page_id=page_id, title=title, category=category,
                blocks=[dict(content=content, sources=sources)], related_page_ids=[], depends_on=depends_on or [])
    if previous:
        page['expected_version'] = previous['page_version']
    wiki = WikiService(root)
    return wiki.publish(wiki.prepare([page])['batch_id'])[0]


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_material_feed_preserves_distinct_documents_even_with_equal_bytes(catalog_api):
    api = catalog_api
    root, store = api.roots['alpha'], api.store
    from agent.tools.knowledge_ingest import KnowledgeIngest
    from common.personal_tasks import task_scope
    from common.runtime_identity import identity_scope
    tool = KnowledgeIngest({'cwd': str(root), 'task_store': store})
    tasks = []
    # 同名同字节的独立接收各有逻辑资料身份；同一任务的两附件也不能丢失。
    path = root / '相同名称.txt'
    path.write_text('可复用预算42 dB。', encoding='utf-8')
    for index in range(2):
        task = store.submit(OWNER, f'independent-{index}', {'text': '分享'}, {'channel': 'web'}, 'offline')
        store.attach(OWNER, task['task_id'], path, attachment_id='first')
        if index == 0:
            store.attach(OWNER, task['task_id'], path, attachment_id='second')
        store.claim(OWNER, task['task_id'])
        with identity_scope(user_id=OWNER), task_scope(task['task_id']):
            result = tool.execute(dict(task_id=task['task_id'], purpose='long_term', reason='真实身份登记'))
        assert result.status == 'success', result.result
        tasks.append(result.result)
    response = json.loads(api.personal.PersonalTasksHandler().GET())
    assert response['status'] == 'success'
    by_id = {task['task_id']: task for task in response['tasks']}
    assert {task['task_id'] for task in tasks} <= by_id.keys()
    decisions = [decision for task in by_id.values() for decision in task['attachment_decisions'].values()]
    assert len(decisions) == 3
    assert len({decision['source']['document_id'] for decision in decisions}) == 3
    assert len({attachment['sha256'] for task in by_id.values() for attachment in task['attachments']}) == 1
    assert {a['attachment_id'] for a in by_id[tasks[0]['task_id']]['attachments']} == {'first', 'second'}


def test_material_feed_keeps_valid_result_separate_from_failed_reorganization(catalog_api):
    api = catalog_api
    root, store = api.roots['alpha'], api.store
    ref = publish_topic(root)
    original = SourceService(root).registered_revision(ref['document_id'], ref['revision_id'])
    parent = store.submit(OWNER, 'published', {'text': '分享'}, {'channel': 'web'}, 'offline')
    store.attach(OWNER, parent['task_id'], original['original'], original['filename'], 'file')
    store.claim(OWNER, parent['task_id'])
    store.complete(OWNER, parent['task_id'], 1, 'published', [ref])
    child = store.submit(OWNER, 'failed-reorganize', {'text': '重整', 'reorganize': dict(
        document_id=ref['document_id'], revision_id=ref['revision_id'], expected_state=1)},
        {'channel': 'web', 'parent_task_id': parent['task_id']}, 'offline')
    store.attach(OWNER, child['task_id'], original['original'], original['filename'], 'file')
    store.claim(OWNER, child['task_id'])
    store.fail(OWNER, child['task_id'], 1, 'index_failed', 'pending_index')
    synthesis = store.submit(OWNER, 'synthesis', {'knowledge_synthesis': True}, {'channel': 'agent'}, 'offline')
    response = json.loads(api.personal.PersonalTasksHandler().GET())
    by_id = {task['task_id']: task for task in response['tasks']}
    assert by_id[parent['task_id']]['result_refs'][0]['current_searchable']
    failed = by_id[child['task_id']]
    assert failed['status'] == 'failed' and failed['error']['code'] == 'index_failed'
    assert failed['origin']['parent_task_id'] == parent['task_id']
    assert failed['payload']['reorganize']['document_id'] == ref['document_id']
    assert by_id[synthesis['task_id']]['attachments'] == []
    assert WikiService(root).current(ref['page_id'])['publish_id'] == ref['publish_id']



@pytest.mark.parametrize('handler,method', [
    ('KnowledgeCatalogHandler', 'GET'), ('KnowledgeHistoryHandler', 'GET'),
    ('KnowledgeVersionHandler', 'GET'), ('KnowledgeActionHandler', 'POST')])
def test_catalog_routes_authenticate_before_workspace_or_storage(catalog_api, monkeypatch, handler, method):
    api = catalog_api
    api.authenticated['value'] = False
    def forbidden_access(*args, **kwargs):
        pytest.fail('未认证请求不应访问工作区或存储')
    monkeypatch.setattr(api.knowledge, '_get_workspace_root', forbidden_access)
    with pytest.raises(api.knowledge.web.HTTPError) as error:
        api.request(handler, method=method)
    assert '401' in str(error.value)


def test_catalog_and_version_use_selected_agent_workspace(catalog_api):
    api = catalog_api
    alpha = publish_topic(api.roots['alpha'], title='甲工作区主题')
    beta = publish_topic(api.roots['beta'], title='乙工作区主题')
    for name, expected, foreign in [('alpha', alpha, beta), ('beta', beta, alpha)]:
        response = api.request('KnowledgeCatalogHandler', agent=name)
        assert response['status'] == 'success', response
        assert [(p['title'], p['page_id']) for p in response['pages']] == [(expected['title'], expected['page_id'])]
        page = response['pages'][0]
        assert page['path'] == expected['path'].removeprefix('knowledge/')
        assert page['category_mode'] == 'auto' and page['category_revision'] == 0
        assert not Path(page['path']).is_absolute() and not page['path'].startswith('knowledge/')
        version = api.request('KnowledgeVersionHandler', agent=name, publish_id=foreign['publish_id'])
        assert version['status'] == 'error' and 'content' not in version.get('version', {})
        history = api.request('KnowledgeHistoryHandler', agent=name)
        assert [e['publish_id'] for e in history['events']] == [expected['publish_id']]
    rejected = api.action('set_page_category', beta, category='跨工作区')
    assert rejected['status'] == 'error'
    assert WikiService(api.roots['beta']).current(beta['page_id'])['category_mode'] == 'auto'


def test_manual_category_changes_metadata_only_and_all_current_views(catalog_api):
    api = catalog_api
    root = api.roots['alpha']
    ref = publish_topic(root)
    wiki = WikiService(root)
    dependent = publish_topic(root, page_id='dependent', sources=ref['sources'],
        depends_on=[dict(page_id=ref['page_id'], page_version=ref['page_version'])])
    evidence = wiki.read_reference(ref['publish_id'])
    originals = [SourceService(root).registered_revision(s['document_id'], s['revision_id'])['original']
                 for s in ref['sources']]
    hashes = {path: file_hash(path) for path in [root / ref['path'], *originals]}
    result = api.action('set_page_category', ref, category='手动/中文分类')
    assert result['status'] == 'success', result
    changed = result['payload']
    assert changed['category'] == '手动/中文分类' and changed['category_mode'] == 'manual'
    assert changed['category_revision'] == ref['category_revision'] + 1
    assert changed['page_version'] == ref['page_version'] and changed['path'] == ref['path']
    assert {path: file_hash(path) for path in hashes} == hashes
    assert wiki.read_reference(ref['publish_id'])['content'] == evidence['content']
    assert wiki.read_reference(ref['publish_id'])['sources'] == evidence['sources']
    assert wiki.current(dependent['page_id'])['searchable']
    pages = api.request('KnowledgeCatalogHandler')['pages']
    assert next(p for p in pages if p['page_id'] == ref['page_id'])['category'] == changed['category']
    graph = api.request('KnowledgeGraphHandler')
    assert next(n for n in graph['nodes'] if n['id'] == ref['path'].removeprefix('knowledge/'))['category'] == changed['category']
    assert WikiService(root).search('预算')[0]['category'] == changed['category']
    assert '手动/中文分类' in (root / 'knowledge/index.md').read_text(encoding='utf-8')
    assert len(api.store.list()) == 0  # 分类无需模型处理任务。


@pytest.mark.parametrize('conflict', ['content_version', 'category_revision'])
def test_category_two_revision_conflicts_cannot_overwrite(catalog_api, conflict):
    api = catalog_api
    ref = publish_topic(api.roots['alpha'])
    first = api.action('set_page_category', ref, category='本人分类')['payload']
    stale = dict(first)
    if conflict == 'category_revision':
        stale['category_revision'] = ref['category_revision']
    else:
        publish_topic(api.roots['alpha'], previous=first, sources=ref['sources'], content='新内容版本')
        stale['page_version'] = ref['page_version']
    history = api.request('KnowledgeHistoryHandler')
    rejected = api.action('set_page_category', stale, category='不应覆盖')
    assert rejected['status'] == 'error', rejected
    assert rejected['code'] == 409
    current = WikiService(api.roots['alpha']).current(ref['page_id'])
    assert current['category'] == '本人分类' and current['category_revision'] == first['category_revision']
    assert api.request('KnowledgeHistoryHandler')['events'] == history['events']


@pytest.mark.parametrize('category', ['', '../escape', '/absolute', '技术//射频', 'bad\\path', None])
def test_invalid_category_is_rejected_without_revision_or_audit(catalog_api, category):
    api = catalog_api
    ref = publish_topic(api.roots['alpha'])
    history = api.request('KnowledgeHistoryHandler')['events']
    rejected = api.action('set_page_category', ref, category=category)
    assert rejected['status'] == 'error'
    current = WikiService(api.roots['alpha']).current(ref['page_id'])
    assert current['category_revision'] == 0 and current['category_mode'] == 'auto'
    assert api.request('KnowledgeHistoryHandler')['events'] == history


def test_manual_lock_survives_publish_reset_keeps_category_until_next_publish(catalog_api):
    api = catalog_api
    root = api.roots['alpha']
    ref = publish_topic(root)
    manual = api.action('set_page_category', ref, category='我的分类')['payload']
    new = publish_topic(root, previous=manual, category='模型建议', content='第二版预算为42 dB。', sources=ref['sources'])
    assert new['category'] == '我的分类' and new['category_mode'] == 'manual'
    assert new['category_revision'] == manual['category_revision']
    reset = api.action('reset_page_category', new)
    assert reset['status'] == 'success', reset
    unlocked = reset['payload']
    assert unlocked['category'] == '我的分类' and unlocked['category_mode'] == 'auto'
    assert unlocked['page_version'] == new['page_version']
    assert unlocked['category_revision'] == manual['category_revision'] + 1
    updated = publish_topic(root, previous=unlocked, category='模型新分类', content='第三版预算为42 dB。', sources=ref['sources'])
    assert updated['category'] == '模型新分类' and updated['category_mode'] == 'auto'
    events = api.request('KnowledgeHistoryHandler')['events']
    assert {e['event_type'] for e in events} == {'published', 'updated', 'category_changed', 'category_auto'}
    assert all(e['created_at'] for e in events)


def test_history_pagination_and_readonly_previous_version(catalog_api):
    api = catalog_api
    root = api.roots['alpha']
    ref = publish_topic(root, content='第一版实际正文42 dB。')
    before = (root / ref['path']).read_bytes()
    manual = api.action('set_page_category', ref, category='手动分类')['payload']
    current = publish_topic(root, previous=manual, content='第二版实际正文42 dB。', sources=ref['sources'])
    api.action('reset_page_category', current)
    pages = [api.request('KnowledgeHistoryHandler', offset=str(i), limit='1') for i in range(4)]
    assert all(p['total'] == 4 for p in pages)
    assert [p['has_more'] for p in pages] == [True, True, True, False]
    events = [p['events'][0] for p in pages]
    assert len({e['id'] for e in events}) == 4
    assert [e['created_at'] for e in events] == sorted((e['created_at'] for e in events), reverse=True)
    response = api.request('KnowledgeVersionHandler', publish_id=ref['publish_id'])
    assert response['status'] == 'success', response
    old = response['version']
    assert old['content'] == before.decode('utf-8')
    assert old['current_status'] == 'superseded' and not old['searchable']
    assert old['current_path'] == current['path'].removeprefix('knowledge/')
    assert old['path'] == ref['path'].removeprefix('knowledge/')
    assert (root / ref['path']).read_bytes() == before
    assert WikiService(root).current(ref['page_id'])['publish_id'] == current['publish_id']


@pytest.mark.parametrize('offset,limit', [('-1', '1'), ('0', '0'), ('0', '201'), ('bad', '2')])
def test_history_rejects_invalid_pagination(catalog_api, offset, limit):
    response = catalog_api.request('KnowledgeHistoryHandler', offset=offset, limit=limit)
    assert response['status'] == 'error' and response['code'] == 'invalid_request'


@pytest.mark.parametrize('damage', ['publication', 'parse', 'original'])
def test_history_version_refuses_damaged_archived_evidence(catalog_api, damage):
    api = catalog_api
    root = api.roots['alpha']
    old = publish_topic(root)
    publish_topic(root, previous=old, content='第二版正文', sources=old['sources'])
    source = old['sources'][0]
    if damage == 'publication':
        path = root / old['path']
    elif damage == 'parse':
        path = root / '.personal_sources/parses' / (source['parse_id'] + '.json')
    else:
        path = Path(SourceService(root).registered_revision(source['document_id'], source['revision_id'])['original'])
    path.write_bytes(b'altered evidence')
    response = api.request('KnowledgeVersionHandler', publish_id=old['publish_id'])
    assert response['status'] == 'error' and response['code'] == 'storage_failed'
    assert 'version' not in response


def test_withdrawn_history_readable_but_current_routes_and_category_reject(catalog_api):
    api = catalog_api
    root = api.roots['alpha']
    ref = publish_topic(root)
    source = ref['sources'][0]
    SourceService(root).change(source['document_id'], action='withdraw', expected_state=1, reason='撤回')
    catalog = api.request('KnowledgeCatalogHandler')
    assert catalog['status'] == 'success' and catalog['pages'] == []
    version = api.request('KnowledgeVersionHandler', publish_id=ref['publish_id'])
    assert version['status'] == 'success', version
    assert version['version']['current_status'] == 'paused' and not version['version']['searchable']
    assert 'current_path' not in version['version']
    assert '可靠预算为42 dB。' in version['version']['content']
    assert api.action('set_page_category', ref, category='无权恢复')['status'] == 'error'
    assert WikiService(root).current(ref['page_id']) is None
    assert not SourceService(root).search('预算')



def test_category_and_audit_survive_reopen_and_full_backup_restore(catalog_api, tmp_path, monkeypatch):
    from cli.commands.backup import create_backup_archive, restore_backup_archive
    api = catalog_api
    root = api.roots['alpha']
    ref = publish_topic(root)
    changed = api.action('set_page_category', ref, category='持久分类')['payload']
    before = api.request('KnowledgeHistoryHandler')
    reopened = WikiService(root).current(ref['page_id'])
    assert reopened['category_mode'] == 'manual' and reopened['category_revision'] == changed['category_revision']
    data = tmp_path / 'backup-data'
    data.mkdir()
    (data / 'config.json').write_text(json.dumps(dict(personal_assistant_enabled=True,
        agent_workspace=str(root))), encoding='utf-8')
    archive = tmp_path / 'full.zip'
    create_backup_archive(archive, data, root, complete=True)
    target = tmp_path / 'restored-workspace'
    restore_backup_archive(archive, tmp_path / 'restored-data', target)
    registry = AgentRegistry([AgentProfile('alpha', 'restored', str(target))], 'alpha')
    monkeypatch.setattr('agent.registry.get_agent_registry', lambda: registry)
    catalog = api.request('KnowledgeCatalogHandler')
    assert catalog['status'] == 'success', catalog
    restored = catalog['pages'][0]
    assert restored['category'] == changed['category'] and restored['category_mode'] == 'manual'
    assert restored['category_revision'] == changed['category_revision']
    assert restored['page_version'] == ref['page_version']
    assert api.request('KnowledgeHistoryHandler')['events'] == before['events']
    version = api.request('KnowledgeVersionHandler', publish_id=ref['publish_id'])
    assert version['status'] == 'success' and version['version']['searchable']
    assert version['version']['content'] == (root / ref['path']).read_text(encoding='utf-8')
    source = SourceService(target).registered_revision(ref['document_id'], ref['revision_id'])
    assert Path(source['original']).is_relative_to(target)
    assert file_hash(source['original']) == file_hash(SourceService(root).registered_revision(
        ref['document_id'], ref['revision_id'])['original'])


def test_pre_metadata_v1_defaults_auto_preserves_legacy_and_invents_no_event(catalog_api):
    api = catalog_api
    root = api.roots['alpha']
    ref = publish_topic(root)
    service = SourceService(root)
    # 升级夹具：保留旧发布、索引与原件，仅移除旧版没有的附加元数据及时间。
    with service.transaction() as db:
        db.execute('DROP TABLE wiki_categories')
        db.execute('DROP TABLE wiki_category_events')
        row = db.execute('SELECT record FROM wiki_publications WHERE id=?', (ref['publish_id'],)).fetchone()
        record = json.loads(row[0])
        record.pop('created_at', None)
        db.execute('UPDATE wiki_publications SET record=? WHERE id=?', (json.dumps(record, ensure_ascii=False), ref['publish_id']))
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
    (root / 'knowledge/旧笔记.md').write_text('# 旧笔记\n未托管内容', encoding='utf-8')
    (root / 'knowledge/log.md').write_text('用户旧日志：没有可证明的事件时间。\n', encoding='utf-8')
    catalog = api.request('KnowledgeCatalogHandler')
    assert catalog['status'] == 'success', catalog
    current = next(p for p in catalog['pages'] if p['page_id'] == ref['page_id'])
    assert current['category_mode'] == 'auto' and current['category_revision'] == 0
    history = api.request('KnowledgeHistoryHandler')
    assert history['total'] == 0 and history['events'] == []
    assert '用户旧日志：没有可证明的事件时间。' in history['legacy_content']
    response = api.request('KnowledgeVersionHandler', publish_id=ref['publish_id'])
    assert response['status'] == 'success' and response['version']['created_at'] is None
    read = api.request('KnowledgeReadHandler', path='旧笔记.md')
    assert read['status'] == 'success' and '未托管内容' in read['content']
    assert (root / 'knowledge/旧笔记.md').read_text(encoding='utf-8') == '# 旧笔记\n未托管内容'
    assert WikiService(root).current(ref['page_id'])['searchable']


def test_projection_failure_does_not_rollback_category_authority(catalog_api, monkeypatch):
    import agent.knowledge.wiki as module
    api = catalog_api
    root = api.roots['alpha']
    ref = publish_topic(root)
    original = module.write_text_atomic
    failures = []
    def fail_projection(path, text, **kwargs):
        if Path(path) == root / 'knowledge/index.md':
            failures.append(str(path))
            raise OSError('测试目录投影写入失败')
        return original(path, text, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(module, 'write_text_atomic', fail_projection)
        api.action('set_page_category', ref, category='故障后保留')
    assert failures
    current = api.request('KnowledgeCatalogHandler')['pages'][0]
    assert current['category'] == '故障后保留' and current['category_mode'] == 'manual'
    assert current['category_revision'] == 1
    assert any(e['event_type'] == 'category_changed' for e in api.request('KnowledgeHistoryHandler')['events'])
    assert '故障后保留' in (root / 'knowledge/index.md').read_text(encoding='utf-8')



@pytest.mark.parametrize('field,value', [('expected_version', None), ('expected_version', 1),
    ('expected_category_revision', True), ('expected_category_revision', '0'),
    ('expected_category_revision', -1), ('page_id', None)])
def test_category_rejects_invalid_revision_types(catalog_api, field, value):
    api = catalog_api
    ref = publish_topic(api.roots['alpha'])
    before = api.request('KnowledgeHistoryHandler')['events']
    result = api.action('set_page_category', ref, category='不应保存', **{field: value})
    assert result['status'] == 'error'
    assert result['code'] == 400 and result['error_code'] == 'invalid_request'
    assert api.request('KnowledgeCatalogHandler')['pages'][0]['category_revision'] == 0
    assert api.request('KnowledgeHistoryHandler')['events'] == before


def test_concurrent_category_actions_have_exactly_one_winner(catalog_api, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, local
    api = catalog_api
    ref = publish_topic(api.roots['alpha'])
    local_request, barrier = local(), Barrier(2)
    monkeypatch.setattr(api.knowledge.web, 'data', lambda: local_request.body)
    def update(category):
        local_request.body = json.dumps(dict(agent_id='alpha', action='set_page_category', payload=dict(
            page_id=ref['page_id'], expected_version=ref['page_version'],
            expected_category_revision=0, category=category))).encode('utf-8')
        barrier.wait(timeout=10)
        return json.loads(api.knowledge.KnowledgeActionHandler().POST())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ['分类甲', '分类乙']))
    assert sorted(result['code'] for result in results) == [200, 409]
    winner = next(r['payload'] for r in results if r['code'] == 200)
    current = api.request('KnowledgeCatalogHandler')['pages'][0]
    assert current['category'] == winner['category'] and current['category_revision'] == 1
    events = api.request('KnowledgeHistoryHandler')['events']
    assert [e['event_type'] for e in events].count('category_changed') == 1


def test_secondary_source_withdrawal_filters_catalog_but_keeps_verified_history(catalog_api):
    api = catalog_api
    root = api.roots['alpha']
    first = publish_topic(root, page_id='first-evidence')
    second = publish_topic(root, page_id='second-evidence')
    merged = publish_topic(root, page_id='merged', sources=first['sources'] + second['sources'])
    source = second['sources'][0]
    SourceService(root).change(source['document_id'], action='withdraw', expected_state=1, reason='仅撤回第二来源')
    catalog = api.request('KnowledgeCatalogHandler')
    assert {p['page_id'] for p in catalog['pages']} == {first['page_id']}
    response = api.request('KnowledgeVersionHandler', publish_id=merged['publish_id'])
    assert response['status'] == 'success', response
    assert response['version']['current_status'] == 'paused'
    assert not response['version']['searchable']
    assert WikiService(root).current(first['page_id'])['searchable']



@pytest.mark.parametrize('body', [[], 'invalid-body', {'action': 'set_page_category', 'payload': []},
    {'action': 'set_page_category', 'payload': 'invalid-payload'}])
def test_classification_action_rejects_nonobject_request(catalog_api, body):
    api = catalog_api
    publish_topic(api.roots['alpha'])
    before = api.request('KnowledgeHistoryHandler')['events']
    response = api.request('KnowledgeActionHandler', method='POST', body=body)
    assert response['status'] == 'error' and response['code'] == 400
    assert api.request('KnowledgeHistoryHandler')['events'] == before


def test_classification_action_honors_query_agent_scope(catalog_api):
    api = catalog_api
    alpha = publish_topic(api.roots['alpha'], title='甲主题')
    beta = publish_topic(api.roots['beta'], title='乙主题')
    api.knowledge.web.ctx.env['QUERY_STRING'] = 'agent_id=beta'
    response = api.request('KnowledgeActionHandler', agent='beta', method='POST', body=dict(
        action='set_page_category', payload=dict(page_id=beta['page_id'], expected_version=beta['page_version'],
            expected_category_revision=beta['category_revision'], category='乙工作区分类')))
    assert response['status'] == 'success', response
    assert WikiService(api.roots['beta']).current(beta['page_id'])['category'] == '乙工作区分类'
    assert WikiService(api.roots['alpha']).current(alpha['page_id'])['category_revision'] == 0



def test_full_backup_history_and_manual_lock_work_with_old_root_offline(catalog_api, tmp_path, monkeypatch):
    """实际完整归档/异根恢复；旧根移走后仍可核验历史正文、出处和分类。"""
    from cli.commands.backup import create_backup_archive, restore_backup_archive
    api = catalog_api
    root = api.roots['alpha']
    first = publish_topic(root, content='历史版预算结论42 dB。')
    locked = api.action('set_page_category', first, category='本人/初始分类')['payload']
    second = publish_topic(root, previous=locked, category='模型建议分类',
                           content='当前版预算结论42 dB，核验通过。', sources=first['sources'])
    unlocked = api.action('reset_page_category', second)['payload']
    current = api.action('set_page_category', unlocked, category='本人/持久锁定')['payload']
    assert current['category_revision'] == 3 and current['category_mode'] == 'manual'
    history_before = api.request('KnowledgeHistoryHandler')
    assert history_before['total'] == 5
    assert {e['event_type'] for e in history_before['events']} == {
        'published', 'updated', 'category_changed', 'category_auto'}
    wiki = WikiService(root)
    versions_before = {ref['publish_id']: api.request('KnowledgeVersionHandler',
        path=ref['path'].removeprefix('knowledge/'))['version'] for ref in (first, second)}
    references_before = {ref['publish_id']: wiki.read_reference(ref['publish_id'], historical=True)
                         for ref in (first, second)}
    originals_before = {source['revision_id']: SourceService(root).registered_revision(
        source['document_id'], source['revision_id']) for source in first['sources']}
    hashes_before = {revision: file_hash(record['original']) for revision, record in originals_before.items()}
    data = tmp_path / 'complete-data'
    data.mkdir()
    (data / 'config.json').write_text(json.dumps(dict(personal_assistant_enabled=True,
        agent_workspace=str(root))), encoding='utf-8')
    archive, target = tmp_path / 'complete-history.zip', tmp_path / 'different-root'
    manifest = create_backup_archive(archive, data, root, complete=True)
    assert manifest['version'] == 3
    restore_backup_archive(archive, tmp_path / 'different-data-root', target)

    # 两路径均明确属于本测试目录；保留旧根原字节，只改变其可访问位置。
    offline = tmp_path / 'offline-original-workspace'
    assert root.resolve().is_relative_to(tmp_path.resolve())
    assert offline.resolve().is_relative_to(tmp_path.resolve()) and not offline.exists()
    root.rename(offline)
    assert not root.exists()
    registry = AgentRegistry([AgentProfile('alpha', 'restored', str(target))], 'alpha')
    monkeypatch.setattr('agent.registry.get_agent_registry', lambda: registry)
    catalog = api.request('KnowledgeCatalogHandler')
    assert catalog['status'] == 'success', catalog
    restored = catalog['pages'][0]
    assert (restored['category'], restored['category_mode'], restored['category_revision']) == (
        current['category'], 'manual', 3)
    assert restored['page_version'] == second['page_version']
    assert api.request('KnowledgeHistoryHandler') == history_before
    restored_service, restored_wiki = SourceService(target), WikiService(target)
    for ref in (first, second):
        publish_id = ref['publish_id']
        path = ref['path'].removeprefix('knowledge/')
        response = api.request('KnowledgeVersionHandler', path=path)
        assert response['status'] == 'success', response
        assert response['version'] == versions_before[publish_id]
        assert response == api.request('KnowledgeVersionHandler', publish_id=publish_id)
        reference = restored_wiki.read_reference(publish_id, historical=True)
        before = references_before[publish_id]
        assert reference['content'] == before['content']
        assert reference['sources'] == before['sources']
        assert reference['current_status'] == before['current_status']
        for field in ('document_id', 'revision_id', 'parse_id', 'ordinal', 'location'):
            assert reference[field] == before[field]
        assert Path(reference['original']).is_relative_to(target)
        assert Path(reference['original']).exists()
    for revision, old in originals_before.items():
        original = restored_service.registered_revision(old['document_id'], revision)
        assert Path(original['original']).is_relative_to(target)
        assert file_hash(original['original']) == hashes_before[revision]
        assert file_hash(offline / Path(old['original']).relative_to(root)) == hashes_before[revision]

    # 恢复后继续发布也服从手动锁；分类修订不因模型建议改变。
    third = publish_topic(target, previous=restored, sources=first['sources'],
                          category='不应覆盖手动分类', content='恢复后新增版本42 dB。')
    assert third['category'] == current['category'] and third['category_mode'] == 'manual'
    assert third['category_revision'] == 3
    source = first['sources'][0]
    restored_service.change(source['document_id'], action='withdraw', expected_state=1, reason='恢复后撤回')
    paused = api.request('KnowledgeVersionHandler', path=third['path'].removeprefix('knowledge/'))
    assert paused['status'] == 'success', paused
    assert paused['version']['current_status'] == 'paused' and not paused['version']['searchable']
    assert 'current_path' not in paused['version']
    assert '恢复后新增版本42 dB。' in paused['version']['content']
    assert api.request('KnowledgeCatalogHandler')['pages'] == []
    assert not restored_service.search('预算')
    assert not root.exists(), '异根恢复后不应重新访问或创建旧工作区'
    for revision, old in originals_before.items():
        assert file_hash(restored_service.registered_revision(old['document_id'], revision)['original']) == hashes_before[revision]
        assert file_hash(offline / Path(old['original']).relative_to(root)) == hashes_before[revision]
