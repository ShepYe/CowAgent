"""K-04：用真实 Wiki 发布验证界面 API，不调用模型或账号。"""
import json
from types import SimpleNamespace
import pytest

from common.personal_tasks import OWNER
from agent.knowledge.wiki import WikiService
from agent.knowledge.service import KnowledgeService
from test_personal_web import api as api_fixture
from test_personal_web import body

api = api_fixture


def publish_wiki(mod, tmp_path, title='模型整理的主题'):
    svc = mod._source_service()
    sources = []
    for index in range(2):
        path = tmp_path / f'原件{index}.txt'
        path.write_text(f'来源{index}第一行\n来源{index}第二行\n来源{index}第三行', encoding='utf-8')
        receipt = svc.receive(path, filename=path.name, request_id=f'source-{index}', origin={'channel': 'web'})
        svc.change(receipt['document_id'], purpose='long_term', expected_state=0, reason='分享')
        parsed = svc.parse(receipt['revision_id'])
        sources.append(dict(receipt, parse_id=parsed['parse_id'], ordinal=index + 1))
    wiki = WikiService(mod._get_workspace_root())
    batch = wiki.prepare([
        dict(page_id='topic-one', title=title, category='技术', blocks=[dict(content='经证据支撑的整理结论', sources=sources)]),
        dict(page_id='topic-two', title='相关主题', category='技术', blocks=[dict(content='另一个结论', sources=[sources[0]])], related_page_ids=['topic-one'])])
    refs = wiki.publish(batch['batch_id'])
    return svc, wiki, refs


def task_with_refs(store, refs):
    task = store.submit(OWNER, 'wiki-result', {}, {'channel': 'web'}, 'cfg')
    store.claim(OWNER, task['task_id'])
    return store.complete(OWNER, task['task_id'], 1, 'published', refs)


@pytest.mark.parametrize('normalized_contract', [False, True])
def test_wiki_reference_uses_multisource_directory_not_primary_parse(api, tmp_path, monkeypatch, normalized_contract):
    mod, store, _ = api
    svc, wiki, refs = publish_wiki(mod, tmp_path)
    task = task_with_refs(store, refs)
    if normalized_contract:
        references = wiki.references
        monkeypatch.setattr(wiki, 'references', lambda publish_id: [dict(unit, ordinal=i, source_ordinal=unit['ordinal']) for i, unit in enumerate(references(publish_id))])
        monkeypatch.setattr(mod, '_wiki_service', lambda: wiki)
    monkeypatch.setattr(mod.web, 'input', lambda **kw: SimpleNamespace(ordinal='1'))
    response = json.loads(mod.PersonalReferenceHandler().GET(task['task_id'], refs[0]['publish_id']))
    assert response['status'] == 'success', response
    reference = response['reference']
    assert reference['unit_count'] == 2
    assert reference['ordinal'] == 1
    assert reference['source_ordinal'] == 2
    assert [(u['ordinal'], u['source_ordinal']) for u in reference['units']] == [(0, 1), (1, 2)]
    assert reference['text'] == '来源1第三行'
    assert reference['location']['line'] == 3
    assert len({unit['document_id'] for unit in reference['units']}) == 2
    assert reference['units'][1]['filename'] == '原件1.txt'
    assert mod._decorate(task)['result_refs'][0]['current_searchable']
    svc.change(refs[0]['sources'][1]['document_id'], action='withdraw', expected_state=1, reason='撤回第二来源')
    assert not mod._decorate(task)['result_refs'][0]['current_searchable']
    assert json.loads(mod.PersonalReferenceHandler().GET(task['task_id'], refs[0]['publish_id']))['reference']['text'] == '来源1第三行'


def test_knowledge_graph_uses_title_category_and_page_relations(api, tmp_path):
    mod, _, _ = api
    _, wiki, refs = publish_wiki(mod, tmp_path)
    service = KnowledgeService(mod._get_workspace_root())
    graph = service.build_graph()
    assert {n['label'] for n in graph['nodes']} == {'模型整理的主题', '相关主题'}
    assert {n['category'] for n in graph['nodes']} == {'技术'}
    assert len(graph['links']) == 1
    page = service.read_file(refs[0]['path'].removeprefix('knowledge/'))
    assert page['content'] == '# 模型整理的主题\n\n经证据支撑的整理结论\n'
    assert not page['editable']
    with wiki.source.transaction() as db:
        db.execute('UPDATE wiki_publications SET verified=0 WHERE id=?', (refs[0]['publish_id'],))
    assert not service.build_graph()['links']


def test_legacy_display_uses_real_filename_and_human_location(api, tmp_path):
    from test_personal_web import published_task
    mod, store, _ = api
    _, _, ref = published_task(mod, store, tmp_path)
    service = KnowledgeService(mod._get_workspace_root())
    graph = service.build_graph()
    assert graph['nodes'][0]['label'] == 'source'
    assert graph['nodes'][0]['category'] == '资料原文'
    content = service.read_file(ref['path'].replace('knowledge/', '', 1))['content']
    assert '"kind"' not in content
    assert '行 1' in content


def test_legacy_receipt_title_comes_from_registered_filename(api, tmp_path):
    from test_personal_web import published_task
    mod, store, _ = api
    _, parent, ref = published_task(mod, store, tmp_path)
    for old_title in (None, '', ref['publish_id'], 'obsolete.pdf'):
        decorated = mod._decorate(dict(parent, result_refs=[dict(ref, title=old_title, category='managed')]))['result_refs'][0]
        assert decorated['title'] == 'source'
        assert decorated['filename'] == 'source.txt'
        assert decorated['category'] == '资料原文'


def test_wiki_title_remains_model_title_even_when_it_is_a_filename(api, tmp_path):
    mod, store, _ = api
    _, _, refs = publish_wiki(mod, tmp_path, title='模型选定的标题.pdf')
    task = task_with_refs(store, [refs[0]])
    assert mod._decorate(task)['result_refs'][0]['title'] == '模型选定的标题.pdf'


def test_searchability_requires_verified_index_independently_of_visual_coverage(api, tmp_path, monkeypatch):
    from test_personal_web import published_task
    mod, store, _ = api
    svc, parent, ref = published_task(mod, store, tmp_path)
    parse_path = svc.root / 'parses' / (ref['parse_id'] + '.json')
    parsed = json.loads(parse_path.read_text(encoding='utf-8'))
    parsed['visual_coverage'] = [dict(physical_page=2, status='pending', unprocessed_reason='vision_unavailable')]
    parse_path.write_text(json.dumps(parsed, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(svc, 'parse', lambda revision: parsed)
    monkeypatch.setattr(mod, '_source_service', lambda: svc)
    task = store.get(OWNER, parent['task_id'])
    decorated = mod._decorate(task)['result_refs'][0]
    assert decorated['current_searchable']
    assert decorated['visual_coverage'][0]['physical_page'] == 2
    with svc.transaction() as db:
        db.execute('DELETE FROM keyword_units WHERE publication=? AND ordinal=0', (ref['publish_id'],))
    assert not mod._decorate(task)['result_refs'][0]['current_searchable']


@pytest.mark.parametrize('action', ['withdraw', 'correct', 'change_purpose'])
def test_secondary_source_action_authorized_by_parent_ref_only(api, tmp_path, monkeypatch, action):
    mod, store, _ = api
    svc, _, refs = publish_wiki(mod, tmp_path)
    merged = task_with_refs(store, [refs[0]])
    secondary = refs[0]['sources'][1]
    # 原件单独来自其他任务，原件持有本身不授权修改；合并页回执中的来源才授权。
    original_task = store.submit(OWNER, 'other-original-task', {}, {'channel': 'web'}, 'cfg')
    original_path = svc.registered_revision(secondary['document_id'], secondary['revision_id'])['original']
    store.attach(OWNER, original_task['task_id'], original_path, '原件1.txt', 'file-b')
    payload = dict(action=action, attempt=1, document_id=secondary['document_id'], expected_state=1,
                   reason='本人修订第二来源', purpose='transient')
    body(mod, monkeypatch, payload)
    assert json.loads(mod.PersonalTaskHandler().POST(original_task['task_id']))['code'] == 'forbidden'
    # 同为当前身份、另有知识页回执，也不能借用其他页的B来源关联。
    unrelated = store.submit(OWNER, 'a-only-page', {}, {'channel': 'web'}, 'cfg')
    store.claim(OWNER, unrelated['task_id'])
    store.complete(OWNER, unrelated['task_id'], 1, 'published', [refs[1]])
    body(mod, monkeypatch, dict(payload, sources=[secondary], result_refs=[refs[0]]))
    assert json.loads(mod.PersonalTaskHandler().POST(unrelated['task_id']))['code'] == 'forbidden'
    body(mod, monkeypatch, payload)
    assert svc.get(secondary['document_id'])['state'] == 1
    result = json.loads(mod.PersonalTaskHandler().POST(merged['task_id']))
    assert result['status'] == 'success', result
    sources = result['task']['result_refs'][0]['sources']
    assert sources[0]['source_state']['state'] == 1
    assert sources[0]['source_status'] == 'current'
    assert sources[1]['source_state']['state'] == 2
    assert sources[1]['source_status'] == {'withdraw': 'withdrawn', 'correct': 'corrected', 'change_purpose': 'transient'}[action]
    assert sources[1]['filename'] == '原件1.txt'
    assert not result['task']['result_refs'][0]['current_searchable']
    assert json.loads(mod.PersonalTaskHandler().POST(merged['task_id']))['code'] == 'version_conflict'
    assert store.get(OWNER, merged['task_id'])['result_refs'] == [refs[0]]


def test_visual_warnings_aggregate_per_source_without_inventing_pages(api, tmp_path, monkeypatch):
    mod, store, _ = api
    svc, _, refs = publish_wiki(mod, tmp_path)
    parses = {}
    for source in refs[0]['sources']:
        path = svc.root / 'parses' / (source['parse_id'] + '.json')
        parsed = json.loads(path.read_text(encoding='utf-8'))
        parsed['visual_warnings'] = [dict(kind='chart', part=f"xl/charts/{source['document_id']}.xml", status='pending', reason='visual_not_supported')]
        path.write_text(json.dumps(parsed, ensure_ascii=False), encoding='utf-8')
        parses[source['revision_id']] = parsed
    monkeypatch.setattr(svc, 'parse', lambda revision: parses[revision])
    monkeypatch.setattr(mod, '_source_service', lambda: svc)
    task = task_with_refs(store, [refs[0]])
    decorated = mod._decorate(task)['result_refs'][0]
    assert len(decorated['visual_warnings']) == 2
    assert len(decorated['sources'][1]['visual_warnings']) == 1
    assert decorated['sources'][1]['visual_warnings'][0]['kind'] == 'chart'
    assert decorated['sources'][1]['visual_coverage'] == []
    assert decorated['visual_coverage'] == []


def test_visual_display_uses_best_revision_parse_once_preserving_reference_identity(api, tmp_path, monkeypatch):
    from unittest.mock import Mock
    mod, store, _ = api
    svc, _, refs = publish_wiki(mod, tmp_path)
    source = refs[0]['sources'][0]
    best = dict(parse_id='best-parse', visual_coverage=[dict(physical_page=i + 1,
                status='processed' if i < 20 else 'partial') for i in range(29)])
    parse = Mock(return_value=best)
    monkeypatch.setattr(svc, 'parse', parse)
    monkeypatch.setattr(mod, '_source_service', lambda: svc)
    # 同修订多个历史解析及多个知识页：展示只使用最佳解析一次。
    first = dict(refs[0], sources=[source, dict(source, parse_id='old-pending-parse')])
    second = dict(refs[1], sources=[source])
    task = task_with_refs(store, [first, second])
    decorated = mod._decorate(task)
    parse.assert_called_once_with(source['revision_id'])
    for ref in decorated['result_refs']:
        assert len(ref['visual_coverage']) == 29
        assert sum(p['status'] == 'processed' for p in ref['visual_coverage']) == 20
        assert sum(p['status'] == 'partial' for p in ref['visual_coverage']) == 9
        assert ref['sources'][0]['parse_id'] == source['parse_id']
        assert len(ref['sources'][0]['visual_coverage']) == 29
    assert decorated['result_refs'][0]['sources'][1]['parse_id'] == 'old-pending-parse'
    assert store.get(OWNER, task['task_id'])['result_refs'] == [first, second]


@pytest.mark.parametrize('filename,title', [('index.md', '知识索引'), ('log.md', '整理记录')])
def test_root_view_hides_only_system_projection_markers(tmp_path, filename, title):
    root = tmp_path / 'knowledge'
    root.mkdir()
    original = '# 原标题\n<!-- user: keep -->\n<div>用户 HTML</div>\n<!-- wiki-projection:start -->\n[主题](wiki/a.md)\n<!-- wiki-projection:end -->\n<!-- wiki-projection:custom -->\n'
    path = root / filename
    path.write_text(original, encoding='utf-8')
    service = KnowledgeService(str(tmp_path))
    result = service.read_file(filename)
    assert '<!-- wiki-projection:start -->' not in result['content']
    assert '<!-- wiki-projection:end -->' not in result['content']
    assert '[主题](wiki/a.md)' in result['content']
    assert '<!-- user: keep -->' in result['content']
    assert '<div>用户 HTML</div>' in result['content']
    assert '<!-- wiki-projection:custom -->' in result['content']
    assert path.read_text(encoding='utf-8') == original
    assert result['path'] == filename
    entry = service.list_tree()['root_files'][0]
    assert entry['name'] == filename
    assert entry['title'] == title
    # 普通用户文件中的同名注释不进行清理。
    (root / '普通笔记.md').write_text(original, encoding='utf-8')
    assert service.read_file('普通笔记.md')['content'] == original
