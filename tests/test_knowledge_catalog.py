"""组织元数据与不可变证据的独立性，全部使用临时资料。"""
import json
import sqlite3

import pytest

from agent.knowledge.parser import SourceError
from agent.knowledge.wiki import WikiService
from agent.knowledge.service import KnowledgeService
from test_knowledge_wiki_publication import source, page, publish


def test_category_lock_reset_and_publication_keep_content_authority(tmp_path):
    svc, receipt, s = source(tmp_path)
    wiki = WikiService(tmp_path)
    first = publish(wiki, page(s))[0]
    dependent = publish(wiki, page(s, title='派生', depends_on=[dict(page_id=first['page_id'], page_version=first['page_version'])]), task='dependent')[0]
    original = tmp_path / first['path']
    before = original.read_bytes(), original.stat().st_mtime_ns
    changed = wiki.set_page_category(first['page_id'], first['page_version'], 0, '本人/收藏')
    assert (original.read_bytes(), original.stat().st_mtime_ns) == before
    assert changed['page_version'] == first['page_version'] and changed['path'] == first['path']
    assert changed['category_mode'] == 'manual' and changed['category_revision'] == 1
    assert wiki.current(dependent['page_id'])
    assert svc.current_publication(receipt['document_id'], receipt['revision_id'])
    assert wiki.read_reference(first['publish_id'])['category'] == '本人/收藏'
    assert next(r for r in wiki.search('星际通信') if r['page_id'] == first['page_id'])['category'] == '本人/收藏'
    catalog = KnowledgeService(tmp_path).catalog()
    assert next(r for r in catalog['pages'] if r['page_id'] == first['page_id'])['category'] == '本人/收藏'
    assert next(n for n in KnowledgeService(tmp_path).build_graph()['nodes'] if n['id'] == first['path'].removeprefix('knowledge/'))['category'] == '本人/收藏'
    newer = page(s, page_id=first['page_id'], expected_version=first['page_version'])
    newer['category'] = '模型/建议'
    second = publish(wiki, newer, task='new')[0]
    assert second['category'] == '本人/收藏' and second['category_revision'] == 1
    reset = wiki.reset_page_category(second['page_id'], second['page_version'], 1)
    assert reset['category'] == '本人/收藏' and reset['category_mode'] == 'auto' and reset['category_revision'] == 2
    newer['expected_version'] = second['page_version']
    third = publish(WikiService(tmp_path), newer, task='after-reset')[0]
    assert third['category'] == '模型/建议' and third['category_revision'] == 3


@pytest.mark.parametrize('category', ['', '../x', 'a//b', ' a', 'a\\b', None, 1])
def test_illegal_categories_and_conflicts_do_not_append_events(tmp_path, category):
    _, _, s = source(tmp_path)
    wiki = WikiService(tmp_path)
    ref = publish(wiki, page(s))[0]
    with pytest.raises(SourceError):
        wiki.set_page_category(ref['page_id'], ref['page_version'], 0, category)
    wiki.set_page_category(ref['page_id'], ref['page_version'], 0, '收藏')
    with pytest.raises(SourceError, match='修订'):
        wiki.reset_page_category(ref['page_id'], ref['page_version'], 0)
    assert wiki.history()['total'] == 2


def test_history_readonly_invalidated_sources_and_hashes(tmp_path):
    svc, receipt, s = source(tmp_path)
    wiki = WikiService(tmp_path)
    first = publish(wiki, page(s))[0]
    log = tmp_path / 'knowledge/log.md'
    log.write_text('手工日志 [原链接](./wiki/example.md)\n' + log.read_text(encoding='utf-8'), encoding='utf-8')
    wiki.set_page_category(first['page_id'], first['page_version'], 0, '收藏')
    svc.change(receipt['document_id'], action='withdraw', expected_state=1, reason='撤回')
    history = wiki.history(0, 1)
    assert history['total'] == 2 and history['has_more']
    assert history['events'][0]['event_type'] == 'category_changed'
    assert '手工日志 [原链接]' in history['legacy_content']
    version = wiki.version(first['publish_id'])
    assert version['current_status'] == 'paused' and not version['searchable'] and 'current_path' not in version
    assert '整理结论' in version['content']
    assert not wiki.catalog()['pages'] and not wiki.search('星际通信')
    with pytest.raises(SourceError):
        wiki.reset_page_category(first['page_id'], first['page_version'], 1)
    original = svc.registered_revision(receipt['document_id'], receipt['revision_id'])
    from pathlib import Path
    Path(original['original']).write_text('被篡改', encoding='utf-8')
    with pytest.raises(SourceError, match='核验'):
        wiki.version(first['publish_id'])


def test_additive_v1_no_fabricated_events_and_restart(tmp_path):
    _, _, s = source(tmp_path)
    wiki = WikiService(tmp_path)
    first = publish(wiki, page(s))[0]
    with sqlite3.connect(wiki.db) as db:
        db.execute('DROP TABLE wiki_categories')
        db.execute('DROP TABLE wiki_category_events')
    reopened = WikiService(tmp_path)
    assert reopened.current(first['page_id'])['category_mode'] == 'auto'
    assert reopened.history()['total'] == 1
    reopened.set_page_category(first['page_id'], first['page_version'], 0, '收藏')
    assert WikiService(tmp_path).current(first['page_id'])['category_revision'] == 1
    with sqlite3.connect(wiki.db) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
        assert json.loads(db.execute('SELECT record FROM wiki_publications').fetchone()[0])['category'] == first['category']


def test_version_exact_path_resolves_superseded_publication(tmp_path):
    _, _, s = source(tmp_path)
    wiki = WikiService(tmp_path)
    first = publish(wiki, page(s))[0]
    second = publish(wiki, page(s, page_id=first['page_id'], expected_version=first['page_version'], content='新版结论'), task='new')[0]
    rel = first['path'].removeprefix('knowledge/')
    result = KnowledgeService(tmp_path).version(path='./wiki/无关/../' + rel.removeprefix('wiki/'))
    assert result == wiki.version(first['publish_id'])
    assert result['current_status'] == 'superseded' and not result['searchable']
    assert result['path'] == rel and result['current_path'] == second['path'].removeprefix('knowledge/')
    assert wiki.version(path=rel.replace('/', '\\')) == result
    for wrong_path in (rel.rsplit('/', 1)[-1], 'wiki/错误分类/' + rel.rsplit('/', 1)[-1]):
        with pytest.raises(SourceError, match='不存在'):
            wiki.version(path=wrong_path)
    (tmp_path / first['path']).write_text('历史正文被修改', encoding='utf-8')
    with pytest.raises(SourceError, match='核验'):
        wiki.version(path=rel)


@pytest.mark.parametrize('args', [
    {}, {'publish_id': 'id', 'path': 'wiki/a.md'}, {'path': '../wiki/a.md'},
    {'path': '/wiki/a.md'}, {'path': 'C:/wiki/a.md'}, {'path': '\\\\server\\wiki\\a.md'},
    {'path': 'wiki/../../wiki/a.md'}, {'path': 'wiki/../'}, {'path': 'wiki/\x00a.md'},
    {'path': ['wiki/a.md']}, {'publish_id': 42},
])
def test_version_rejects_invalid_selector_before_reading_storage(tmp_path, monkeypatch, args):
    wiki = WikiService(tmp_path)
    def unexpected():
        pytest.fail('非法路径不应开始历史回读')
    monkeypatch.setattr(wiki, '_reconcile', unexpected)
    with pytest.raises(SourceError) as error:
        wiki.version(**args)
    assert error.value.code == 'invalid_request'


def test_index_rebuild_has_one_controlled_entry_and_keeps_unmanaged_content(tmp_path):
    from urllib.parse import quote, unquote
    import re
    _, _, s = source(tmp_path)
    wiki = WikiService(tmp_path)
    ref = publish(wiki, page(s))[0]
    index = tmp_path / 'knowledge/index.md'
    note = '用户手写说明与 [未托管链接](./wiki/未托管.md)'
    index.write_text(note + '\n\n' + index.read_text(encoding='utf-8'), encoding='utf-8')
    wiki.set_page_category(ref['page_id'], ref['page_version'], 0, '分类A')
    service = KnowledgeService(tmp_path)
    assert service.rebuild_index_md()
    rel = ref['path'].removeprefix('knowledge/')
    # 兼容旧生成器留下的无标记、编码路径条目，只清理完整登记路径。
    with index.open('a', encoding='utf-8') as output:
        output.write(f'\n- [旧目录项](./{quote(rel, safe="/")}) — 分类A\n')
    wiki.set_page_category(ref['page_id'], ref['page_version'], 1, '分类B')
    changed = index.read_text(encoding='utf-8')
    assert sum(unquote(link).removeprefix('./') == rel for link in re.findall(r'\]\(([^)]+)\)', changed)) == 1
    assert '分类B' in changed and '分类A' not in changed
    for _ in range(2):
        assert service.rebuild_index_md()
    content = index.read_text(encoding='utf-8')
    links = re.findall(r'\]\(([^)]+)\)', content)
    assert sum(unquote(link).removeprefix('./') == rel for link in links) == 1
    assert '分类B' in content and '分类A' not in content
    assert note in content


def test_succeeded_ingest_receipt_uses_current_category_without_changing_task_history(tmp_path):
    from agent.tools.knowledge_ingest import KnowledgeIngest
    from common.personal_tasks import OWNER, TaskStore, task_scope
    from common.runtime_identity import identity_scope
    _, _, s = source(tmp_path)
    wiki = WikiService(tmp_path)
    ref = publish(wiki, page(s))[0]
    store = TaskStore(tmp_path / 'tasks')
    task = store.submit(OWNER, 'completed', {}, {'channel': 'test'}, 'cfg')
    store.claim(OWNER, task['task_id'])
    saved = store.complete(OWNER, task['task_id'], 1, 'published', [ref])
    history = store.history(OWNER, task['task_id'])
    wiki.set_page_category(ref['page_id'], ref['page_version'], 0, '分类A')
    wiki.set_page_category(ref['page_id'], ref['page_version'], 1, '分类B')
    tool = KnowledgeIngest({'cwd': str(tmp_path), 'task_store': store})
    with identity_scope(user_id=OWNER), task_scope(task['task_id']):
        result = tool.execute(dict(task_id=task['task_id'], purpose='long_term', reason='幂等重试'))
    assert result.status == 'success', result.result
    returned = result.result['result_refs'][0]
    assert (returned['category'], returned['category_mode'], returned['category_revision']) == ('分类B', 'manual', 2)
    assert {k: v for k, v in returned.items() if k not in ('category', 'category_mode', 'category_revision')} == {
        k: v for k, v in ref.items() if k not in ('category', 'category_mode', 'category_revision')}
    assert store.get(OWNER, task['task_id']) == saved
    assert store.history(OWNER, task['task_id']) == history


@pytest.mark.parametrize('content', [
    '', '# 知识库目录\n\n', '# Knowledge Index\n', '# 知识库目录\n\n## wiki\n',
    '# 知识索引\n<!-- wiki-projection:start -->\n自动目录\n<!-- wiki-projection:end -->\n',
    '<!-- knowledge-index:start -->\n# 知识库目录\n自动目录\n<!-- knowledge-index:end -->\n',
])
def test_catalog_legacy_content_omits_projections_and_empty_templates_without_writing(tmp_path, content):
    wiki = WikiService(tmp_path)
    wiki.knowledge.mkdir(exist_ok=True)
    index = wiki.knowledge / 'index.md'
    index.write_text(content, encoding='utf-8')
    before = index.read_bytes(), index.stat().st_mtime_ns
    assert wiki.catalog()['legacy_content'] == ''
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


def test_catalog_legacy_content_preserves_unmanaged_text_and_links_without_writing(tmp_path):
    _, _, s = source(tmp_path)
    wiki = WikiService(tmp_path)
    publish(wiki, page(s))
    index = wiki.knowledge / 'index.md'
    note = '# 我的索引\n\n手写说明与 [旧笔记](./notes/旧笔记.md)\n'
    content = note + index.read_text(encoding='utf-8') + '\n<!-- knowledge-index:start -->\n自动目录\n<!-- knowledge-index:end -->\n'
    index.write_text(content, encoding='utf-8')
    before = index.read_bytes(), index.stat().st_mtime_ns
    result = KnowledgeService(tmp_path).catalog()
    assert result['legacy_content'] == note.strip()
    assert result['pages']
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
