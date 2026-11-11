"""主题知识页的发布、失效与旧来源兼容契约。"""
import asyncio
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from agent.knowledge.parser import SourceError
from agent.knowledge.personal_assistant import SourceService, guard_path, readable
from agent.knowledge.wiki import WikiService


def source(root, name='radio', text='原始依据：射频设备预算42 dB\n第二条证据'):
    service = SourceService(root)
    path = root / (name + '.txt')
    path.write_text(text, encoding='utf-8')
    receipt = service.receive(path, filename=path.name, request_id=name, origin={'channel': 'test'})
    service.change(receipt['document_id'], purpose='long_term', expected_state=0, reason='收录')
    parsed = service.parse(receipt['revision_id'])
    return service, receipt, dict(document_id=receipt['document_id'], revision_id=receipt['revision_id'], parse_id=parsed['parse_id'], ordinal=0)


def page(*sources, title='链路预算', content='整理结论：星际通信需要余量', **kwargs):
    return dict(title=title, category='射频/设计', blocks=[dict(content=content, sources=list(sources))], **kwargs)


def publish(wiki, *pages, task='task'):
    batch = wiki.prepare(list(pages), task_id=task)
    return wiki.publish(batch['batch_id'])


def test_split_merge_update_and_enumerable_sources(tmp_path):
    service, first, s1 = source(tmp_path)
    _, second, s2 = source(tmp_path, 'supply', '电源证据')
    wiki = WikiService(tmp_path)
    refs = publish(wiki, page(s1, s2), page(s1, title='射频器件'))
    assert len({r['page_id'] for r in refs}) == 2
    merged = refs[0]
    assert len(merged['sources']) == 2
    assert [r['document_id'] for r in wiki.references(merged['publish_id'])] == [first['document_id'], second['document_id']]
    ref = wiki.read_reference(merged['publish_id'], 1)
    assert ref['text'] == '电源证据' and ref['document_id'] == second['document_id']
    assert ref['location'] == {'kind': 'text', 'line': 1}
    assert ref['sources'][0]['filename'] == 'radio.txt'
    assert ref['sources'][0]['source_state_snapshot']['purpose'] == 'long_term'
    target = tmp_path / merged['path']
    assert target.read_text(encoding='utf-8') == '# 链路预算\n\n整理结论：星际通信需要余量\n'
    assert service.read_reference(merged['publish_id'], 1)['text'] == '电源证据'
    assert service.current_publication(first['document_id'], first['revision_id'])['page_id'] in {r['page_id'] for r in refs}
    assert service.current_publication(second['document_id'], second['revision_id'])['document_id'] == second['document_id']
    assert '星际通信' in wiki.search('星际通信')[0]['text']
    assert wiki.current(merged['page_id'])['page_version'] == merged['page_version']
    guard_path(target)
    with pytest.raises(SourceError):
        guard_path(target, write=True)
    updated = publish(wiki, page(s1, page_id=merged['page_id'], expected_version=merged['page_version'], content='新版知识结论'), task='update')[0]
    assert updated['page_id'] == merged['page_id'] and updated['page_version'] != merged['page_version']
    assert target.exists() and wiki.read_reference(merged['publish_id'], historical=True)['current_status'] == 'superseded'
    with pytest.raises(SourceError):
        wiki.prepare([page(s1, page_id=merged['page_id'], expected_version=merged['page_version'])])
    with pytest.raises(SourceError):
        wiki.prepare([page(s1, page_id=merged['page_id'])])


def test_prepare_restart_idempotency_and_snapshot_gap(tmp_path):
    _, _, s = source(tmp_path)
    wiki = WikiService(tmp_path)
    pages = [page(s)]
    batch = wiki.prepare(pages, task_id='same-task')
    reordered = [{key: value for key, value in reversed(list(pages[0].items()))}]
    assert WikiService(tmp_path).prepare(reordered, task_id='same-task') == batch
    refs = WikiService(tmp_path).publish(batch['batch_id'])
    assert WikiService(tmp_path).publish(batch['batch_id']) == refs
    assert WikiService(tmp_path).prepare(pages, task_id='same-task') == batch
    with sqlite3.connect(wiki.db) as db:
        assert db.execute('SELECT count(*) FROM wiki_batches').fetchone()[0] == 1
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1


@pytest.mark.parametrize('failure', ['index', 'file'])
def test_atomic_failed_batch_preserves_old_and_retry(tmp_path, monkeypatch, failure):
    import agent.knowledge.wiki as module
    service, receipt, s = source(tmp_path)
    old = service.publish(service.prepare(receipt['revision_id'])['candidate_id'])
    wiki = WikiService(tmp_path)
    batch = wiki.prepare([page(s), page(s, title='第二页')])
    if failure == 'index':
        original = wiki._verify_index
        calls = []

        def fail_second(db, record):
            calls.append(record)
            original(db, record)
            if len(calls) == 2:
                raise SourceError('index_failed', '模拟索引故障')

        monkeypatch.setattr(wiki, '_verify_index', fail_second)
    else:
        original_write = module.write_text_atomic
        calls = []

        def fail_second(path, text, **kwargs):
            calls.append(path)
            if len(calls) == 2:
                raise OSError('模拟磁盘故障')
            original_write(path, text, **kwargs)

        monkeypatch.setattr(module, 'write_text_atomic', fail_second)
    with pytest.raises((SourceError, OSError)):
        wiki.publish(batch['batch_id'])
    assert not wiki.search('星际通信')
    assert service.search('射频设备')[0]['publish_id'] == old['publish_id']
    for record in batch['pages']:
        assert wiki.current(record['page_id']) is None
        assert not readable(tmp_path / record['path'])
    if failure == 'index':
        monkeypatch.setattr(wiki, '_verify_index', original)
    else:
        monkeypatch.setattr(module, 'write_text_atomic', original_write)
    refs = WikiService(tmp_path).publish(batch['batch_id'])
    assert len(refs) == 2 and not any(r['publish_id'] == old['publish_id'] for r in service.search('射频设备'))


@pytest.mark.parametrize('action', ['withdraw', 'correct', 'purpose', 'revision'])
def test_source_invalidates_whole_page_downstream_and_late_batch(tmp_path, action):
    service, receipt, s = source(tmp_path)
    _, _, independent = source(tmp_path, 'independent', '无关来源')
    wiki = WikiService(tmp_path)
    parent, unrelated = publish(wiki, page(s), page(independent, title='无关页面'))
    child = publish(wiki, page(independent, title='派生结论', depends_on=[dict(page_id=parent['page_id'], page_version=parent['page_version'])]), task='child')[0]
    late = wiki.prepare([page(s, title='迟到候选')])
    if action == 'revision':
        path = tmp_path / 'new.txt'
        path.write_text('原件新版', encoding='utf-8')
        newer = service.receive(path, filename='new.txt', request_id='replacement', origin={'channel': 'test'}, document_id=s['document_id'])
        assert wiki.current(parent['page_id']) and wiki.current(child['page_id'])
        parsed = service.parse(newer['revision_id'])
        new_source = dict(document_id=s['document_id'], revision_id=newer['revision_id'], parse_id=parsed['parse_id'], ordinal=0)
        publish(wiki, page(new_source, title='已发布新版主题'), task='replacement')
    else:
        service.change(s['document_id'], action=action, purpose='transient' if action == 'purpose' else None, expected_state=1, reason='依据失效')
    assert WikiService(tmp_path).current(parent['page_id']) is None
    assert wiki.current(child['page_id']) is None
    assert wiki.current(unrelated['page_id'])
    assert not readable(tmp_path / child['path'])
    assert wiki.read_reference(parent['publish_id'], historical=True)['current_status'] == 'paused'
    with pytest.raises(SourceError):
        WikiService(tmp_path).publish(late['batch_id'])
    if action != 'revision':
        service.change(s['document_id'], action='republish', purpose='long_term', expected_state=2, reason='恢复资料')
        assert wiki.current(parent['page_id']) is None
        recovered = publish(wiki, page(s, page_id=parent['page_id'], expected_version=parent['page_version']), task='reorganize')[0]
        assert recovered['page_id'] == parent['page_id'] and wiki.current(child['page_id']) is None


def test_page_update_pauses_downstream_cycle_and_late_candidate(tmp_path):
    _, _, s = source(tmp_path)
    wiki = WikiService(tmp_path)
    parent = publish(wiki, page(s))[0]
    child_page = page(s, title='下游', depends_on=[dict(page_id=parent['page_id'], page_version=parent['page_version'])])
    child = publish(wiki, child_page, task='child')[0]
    with pytest.raises(SourceError, match='循环'):
        wiki.prepare([page(s, page_id=parent['page_id'], expected_version=parent['page_version'], depends_on=[dict(page_id=child['page_id'], page_version=child['page_version'])])])
    late = wiki.prepare([child_page])
    publish(wiki, page(s, page_id=parent['page_id'], expected_version=parent['page_version']), task='update')
    assert wiki.current(child['page_id']) is None
    with pytest.raises(SourceError):
        wiki.publish(late['batch_id'])


@pytest.mark.parametrize('damage', ['file', 'index', 'parse', 'original', 'sources'])
def test_tamper_fail_closed_and_evidence_refresh(tmp_path, damage):
    service, receipt, s = source(tmp_path)
    wiki = WikiService(tmp_path)
    ref = publish(wiki, page(s))[0]
    before = service.evidence_revision()
    if damage == 'file':
        path = tmp_path / ref['path']
        previous = path.read_bytes()
        path.write_text('篡改正文', encoding='utf-8')
    elif damage == 'index':
        with service.transaction() as db:
            db.execute('DELETE FROM wiki_units')
    elif damage == 'parse':
        (service.root / 'parses' / (s['parse_id'] + '.json')).write_text('{}', encoding='utf-8')
    elif damage == 'sources':
        with service.transaction() as db:
            db.execute('DELETE FROM wiki_sources')
    else:
        Path(receipt['original']).write_text('原件篡改', encoding='utf-8')
    with pytest.raises(SourceError):
        wiki.read_reference(ref['publish_id'])
    assert service.evidence_revision() != before
    assert wiki.current(ref['page_id']) is None
    assert not wiki.search('星际通信') and not readable(tmp_path / ref['path'])
    if damage == 'file':
        path.write_bytes(previous)
        assert WikiService(tmp_path).current(ref['page_id']) is None


def test_retired_original_never_revives_when_wiki_pauses(tmp_path):
    service, receipt, s = source(tmp_path)
    legacy = service.publish(service.prepare(receipt['revision_id'])['candidate_id'])
    wiki = WikiService(tmp_path)
    ref = publish(wiki, page(s))[0]
    assert not service.search('射频设备')
    assert not readable(tmp_path / legacy['path'])
    (tmp_path / ref['path']).write_text('损坏知识页', encoding='utf-8')
    assert not service.search('射频设备')
    assert not readable(tmp_path / legacy['path'])
    service.change(s['document_id'], action='correct', expected_state=1, reason='更正旧依据')
    assert not service.search('射频设备')


def test_relative_storage_relocation_and_memory_bridge(tmp_path, monkeypatch):
    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager
    from common import state_dir
    original, restored = tmp_path / 'original', tmp_path / 'restored'
    original.mkdir()
    service, _, s = source(original)
    wiki = WikiService(original)
    ref = publish(wiki, page(s))[0]
    with service.transaction() as db:
        for table in ('wiki_batches', 'wiki_publications', 'wiki_sources'):
            for row in db.execute(f'SELECT record FROM {table}'):
                assert str(original) not in row[0] and 'original"' not in row[0]
    shutil.copytree(original, restored)
    # The existing backup relocates only source revisions/receipts and legacy
    # publications. Wiki records need no absolute path relocation.
    db_path = restored / '.personal_sources/sources.sqlite3'
    with sqlite3.connect(db_path) as db:
        for table in ('revisions', 'receipts'):
            for rowid, body in db.execute(f'SELECT rowid,record FROM {table}').fetchall():
                record = json.loads(body)
                record['original'] = str(restored / '.personal_sources/originals' / record['sha256'])
                db.execute(f'UPDATE {table} SET record=? WHERE rowid=?', (json.dumps(record), rowid))
    assert WikiService(restored).current(ref['page_id'])
    monkeypatch.setattr(state_dir, 'knowledge_dir', lambda **kwargs: restored / 'knowledge')
    manager = MemoryManager(MemoryConfig(workspace_root=str(restored)), embedding_provider=None)
    try:
        asyncio.run(manager.sync(force=True))
        results = asyncio.run(manager.search('星际通信', user_id='local-owner', min_score=0))
        assert results and all('[source_reference]' in r.snippet for r in results if r.path == ref['path'])
        assert '星际通信' in results[0].snippet
        metadata = json.loads(results[0].snippet.split('[source_reference] ')[1])
        assert metadata['page_id'] == ref['page_id'] and metadata['sources'][0]['document_id'] == s['document_id']
    finally:
        manager.close()


@pytest.mark.parametrize('mutate', [
    lambda p: p['blocks'][0].update(sources=[]),
    lambda p: p['blocks'][0]['sources'][0].update(ordinal=999),
    lambda p: p.update(category='../escape'),
    lambda p: p.update(related_page_ids=['missing']),
])
def test_invalid_evidence_and_relationships_rejected(tmp_path, mutate):
    _, _, s = source(tmp_path)
    candidate = page(s)
    mutate(candidate)
    with pytest.raises(SourceError):
        WikiService(tmp_path).prepare([candidate])


def test_failed_update_and_competing_candidates_preserve_current(tmp_path, monkeypatch):
    _, _, s = source(tmp_path)
    wiki = WikiService(tmp_path)
    old = publish(wiki, page(s))[0]
    draft = page(s, page_id=old['page_id'], expected_version=old['page_version'], content='修订的正文')
    a = wiki.prepare([draft], task_id='a')
    b = wiki.prepare([draft], task_id='b')
    original = wiki._verify_index
    monkeypatch.setattr(wiki, '_verify_index', lambda *args: (_ for _ in ()).throw(SourceError('index_failed', '注入故障')))
    with pytest.raises(SourceError):
        wiki.publish(a['batch_id'])
    assert wiki.current(old['page_id'])['page_version'] == old['page_version']
    assert readable(tmp_path / old['path'])
    monkeypatch.setattr(wiki, '_verify_index', original)
    new = wiki.publish(a['batch_id'])[0]
    with pytest.raises(SourceError):
        wiki.publish(b['batch_id'])
    assert wiki.current(old['page_id'])['page_version'] == new['page_version']


def test_concurrent_prepare_shares_task_draft_and_publishes_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    _, _, s = source(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        batches = list(pool.map(lambda _: WikiService(tmp_path).prepare([page(s)], task_id='concurrent'), range(2)))
        assert batches[0] == batches[1]
        results = list(pool.map(lambda _: WikiService(tmp_path).publish(batches[0]['batch_id']), range(2)))
    assert results[0] == results[1]


def test_block_search_preserves_its_own_multisource_mapping(tmp_path):
    _, _, first = source(tmp_path)
    _, _, second = source(tmp_path, 'other', '其他原件\n末尾证据')
    second['ordinal'] = 1
    draft = page(first, content='第一主题正文')
    draft['blocks'].append(dict(content='量子器件独立结论', sources=[second]))
    wiki = WikiService(tmp_path)
    ref = publish(wiki, draft)[0]
    hit = wiki.search('量子器件')[0]
    assert hit['source_ordinal'] == 1 and hit['ordinal'] == 1
    assert hit['document_id'] == second['document_id'] and hit['source_ordinals'] == [1]
    assert wiki.read_reference(ref['publish_id'], 1)['text'] == '末尾证据'
    with pytest.raises(SourceError):
        wiki.read_reference(ref['publish_id'], 2)


def test_invalid_initial_parse_rejected_and_unmanaged_page_survives(tmp_path, monkeypatch):
    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager
    from common import state_dir
    service, _, s = source(tmp_path)
    parsed_path = service.root / 'parses' / (s['parse_id'] + '.json')
    parsed = json.loads(parsed_path.read_text(encoding='utf-8'))
    parsed['units'][0]['text'] = '伪造解析正文'
    parsed_path.write_text(json.dumps(parsed), encoding='utf-8')
    with pytest.raises(SourceError):
        WikiService(tmp_path).prepare([page(s)])
    knowledge = tmp_path / 'knowledge'
    knowledge.mkdir()
    old = knowledge / 'old.md'
    old.write_text('# 旧知识\n未托管知识仍可检索', encoding='utf-8')
    assert readable(old)
    monkeypatch.setattr(state_dir, 'knowledge_dir', lambda **kwargs: knowledge)
    manager = MemoryManager(MemoryConfig(workspace_root=str(tmp_path)), embedding_provider=None)
    try:
        asyncio.run(manager.sync(force=True))
        results = asyncio.run(manager.search('未托管知识', user_id='local-owner', min_score=0))
        assert any(r.path == 'knowledge/old.md' for r in results)
    finally:
        manager.close()


def test_index_log_projection_preserves_unmanaged_and_removes_paused_facts(tmp_path):
    service, _, s = source(tmp_path)
    knowledge = tmp_path / 'knowledge'
    knowledge.mkdir()
    (knowledge / 'index.md').write_text('# 个人知识\n\n- [旧笔记](notes/old.md) — 用户自有摘要\n', encoding='utf-8')
    (knowledge / 'log.md').write_text('## 手动日志保留\n', encoding='utf-8')
    wiki = WikiService(tmp_path)
    ref = publish(wiki, page(s, title='机密主题标题'))[0]
    for name in ('index.md', 'log.md'):
        content = (knowledge / name).read_text(encoding='utf-8')
        assert '机密主题标题' in content and ref['path'].removeprefix('knowledge/') in content
        assert content.count('wiki-projection:start') == 1
    assert '用户自有摘要' in (knowledge / 'index.md').read_text(encoding='utf-8')
    service.change(s['document_id'], action='withdraw', expected_state=1, reason='撤回')
    for name in ('index.md', 'log.md'):
        assert '机密主题标题' not in (knowledge / name).read_text(encoding='utf-8')
    assert '用户自有摘要' in (knowledge / 'index.md').read_text(encoding='utf-8')
    assert '手动日志保留' in (knowledge / 'log.md').read_text(encoding='utf-8')


def test_projection_repairs_after_restart_and_tamper_drops_title(tmp_path):
    service, _, s = source(tmp_path)
    wiki = WikiService(tmp_path)
    batch = wiki.prepare([page(s, title='待验证主题')])
    ref = wiki.publish(batch['batch_id'])[0]
    index = tmp_path / 'knowledge/index.md'
    index.unlink()
    assert WikiService(tmp_path).publish(batch['batch_id'])
    assert '待验证主题' in index.read_text(encoding='utf-8')
    (tmp_path / ref['path']).write_text('损坏页', encoding='utf-8')
    service.evidence_revision()
    assert '待验证主题' not in index.read_text(encoding='utf-8')


def test_new_source_revision_keeps_legacy_retired_after_restart(tmp_path):
    service, receipt, s = source(tmp_path)
    legacy = service.publish(service.prepare(receipt['revision_id'])['candidate_id'])
    wiki = WikiService(tmp_path)
    ref = publish(wiki, page(s))[0]
    replacement = tmp_path / 'replacement.txt'
    replacement.write_text('新版依据99dB', encoding='utf-8')
    newer = service.receive(replacement, filename=replacement.name, request_id='new-version',
                            origin={'channel': 'test'}, document_id=s['document_id'])
    reopened = SourceService(tmp_path)
    assert newer['revision_id'] != s['revision_id']
    assert WikiService(tmp_path).current(ref['page_id'])
    assert reopened.current_publication(s['document_id'], s['revision_id'])['page_id'] == ref['page_id']
    assert not reopened.search('射频设备')
    assert not readable(tmp_path / legacy['path'])
    assert not reopened.read_reference(legacy['publish_id'], 0, historical=True)['valid']
    with pytest.raises(SourceError):
        reopened.read_reference(legacy['publish_id'], 0)
    with pytest.raises(SourceError):
        reopened.publish(legacy['publish_id'])


def test_late_legacy_candidate_cannot_change_wiki_source_snapshot(tmp_path):
    service, receipt, s = source(tmp_path)
    legacy = service.publish(service.prepare(receipt['revision_id'])['candidate_id'])
    late = service.prepare(receipt['revision_id'], expected_current_revision=receipt['revision_id'])
    before = service.get(s['document_id'])
    wiki = WikiService(tmp_path)
    ref = publish(wiki, page(s))[0]
    assert service.get(s['document_id']) == before
    for candidate in (late['candidate_id'], legacy['publish_id']):
        with pytest.raises(SourceError, match='退役'):
            SourceService(tmp_path).publish(candidate)
    with pytest.raises(SourceError):
        service.prepare(receipt['revision_id'], expected_current_revision=receipt['revision_id'])
    assert service.get(s['document_id']) == before
    assert wiki.current(ref['page_id'])['publish_id'] == ref['publish_id']
    # Upgrade an already-used wiki database without rebuilding pages or data.
    with service.transaction() as db:
        db.execute('DROP TABLE wiki_legacy_retirements')
    reopened = SourceService(tmp_path)
    with pytest.raises(SourceError):
        reopened.publish(late['candidate_id'])
    assert WikiService(tmp_path).current(ref['page_id'])


@pytest.mark.parametrize('action', ['withdraw', 'correct', 'purpose'])
def test_projection_io_failure_cannot_rollback_invalidation(tmp_path, monkeypatch, action):
    import agent.knowledge.wiki as module
    from agent.prompt.builder import _build_knowledge_section
    from common import state_dir
    service, receipt, s = source(tmp_path)
    legacy = service.publish(service.prepare(receipt['revision_id'])['candidate_id'])
    _, _, independent = source(tmp_path, 'other', '无关依据')
    wiki = WikiService(tmp_path)
    ref = publish(wiki, page(s, title='应从提示消失的主题'))[0]
    child = publish(wiki, page(independent, title='暂停的派生主题', depends_on=[dict(page_id=ref['page_id'], page_version=ref['page_version'])]), task='child')[0]
    before = service.evidence_revision()
    observed = []

    def fail_projection(path, text, **kwargs):
        assert Path(path).name in ('index.md', 'log.md')
        # A separate connection must already see committed invalidation before
        # any optional filesystem projection is attempted.
        with sqlite3.connect(service.db) as db:
            doc = db.execute('SELECT validity,purpose,state FROM documents WHERE id=?', (s['document_id'],)).fetchone()
            assert doc[2] == 2
            assert db.execute("SELECT count(*) FROM wiki_pages WHERE status='paused'").fetchone()[0] == 2
            observed.append(doc)
        raise OSError('投影磁盘只读')

    monkeypatch.setattr(module, 'write_text_atomic', fail_projection)
    result = service.change(s['document_id'], action=action, expected_state=1,
                            purpose='transient' if action == 'purpose' else None, reason='失效')
    assert result['state'] == 2 and observed
    assert service.evidence_revision() != before
    assert not wiki.current(ref['page_id']) and not wiki.current(child['page_id'])
    assert not service.search('星际通信') and not service.search('射频设备')
    for path in (ref['path'], child['path'], legacy['path']):
        assert not readable(tmp_path / path)
        with pytest.raises(SourceError):
            guard_path(tmp_path / path)
    monkeypatch.setattr(state_dir, 'knowledge_dir', lambda **kwargs: tmp_path / 'knowledge')
    prompt = '\n'.join(_build_knowledge_section(str(tmp_path), 'zh'))
    assert '应从提示消失的主题' not in prompt and '暂停的派生主题' not in prompt


def test_receive_is_candidate_until_atomic_new_revision_publication(tmp_path, monkeypatch):
    service, receipt, s = source(tmp_path)
    legacy = service.publish(service.prepare(receipt['revision_id'])['candidate_id'])
    wiki = WikiService(tmp_path)
    old = publish(wiki, page(s, title='旧有效主题'))[0]
    _, _, independent = source(tmp_path, 'independent', '无关原件')
    child = publish(wiki, page(independent, title='旧主题派生页', depends_on=[dict(page_id=old['page_id'], page_version=old['page_version'])]), task='child')[0]
    stale = wiki.prepare([page(s, title='基于旧发布版本整理')])
    before = service.evidence_revision()
    path = tmp_path / 'candidate.txt'
    path.write_text('新原件的依据99dB', encoding='utf-8')
    newer = service.receive(path, filename=path.name, request_id='new-candidate', origin={'channel': 'test'}, document_id=s['document_id'])
    assert service.evidence_revision() == before
    assert WikiService(tmp_path).current(old['page_id']) and wiki.current(child['page_id'])
    assert wiki.read_reference(old['publish_id'])['valid']
    parsed = service.parse(newer['revision_id'])
    new_source = dict(document_id=s['document_id'], revision_id=newer['revision_id'], parse_id=parsed['parse_id'], ordinal=0)
    batch = wiki.prepare([page(new_source, title='新有效主题')], task_id='new-publish')
    original = wiki._verify_index
    monkeypatch.setattr(wiki, '_verify_index', lambda *args: (_ for _ in ()).throw(SourceError('index_failed', '新候选失败')))
    with pytest.raises(SourceError):
        wiki.publish(batch['batch_id'])
    assert WikiService(tmp_path).current(old['page_id']) and wiki.current(child['page_id'])
    assert readable(tmp_path / old['path']) and not readable(tmp_path / legacy['path'])
    with service.transaction() as db:
        assert db.execute('SELECT revision FROM wiki_source_heads WHERE document=?', (s['document_id'],)).fetchone()[0] == s['revision_id']
    monkeypatch.setattr(wiki, '_verify_index', original)
    current = WikiService(tmp_path).publish(batch['batch_id'])[0]
    assert wiki.current(current['page_id']) and not wiki.current(old['page_id']) and not wiki.current(child['page_id'])
    assert wiki.references(old['publish_id'])[0]['source_status'] == 'superseded_or_changed'
    assert not readable(tmp_path / old['path']) and not readable(tmp_path / legacy['path'])
    with pytest.raises(SourceError):
        wiki.publish(stale['batch_id'])
    with service.transaction() as db:
        assert db.execute('SELECT revision FROM wiki_source_heads WHERE document=?', (s['document_id'],)).fetchone()[0] == newer['revision_id']
    service.change(s['document_id'], action='withdraw', expected_state=1, reason='撤回新来源')
    reopened = WikiService(tmp_path)
    assert not reopened.current(current['page_id']) and not reopened.current(old['page_id'])
    assert not SourceService(tmp_path).search('射频设备') and not readable(tmp_path / legacy['path'])
    with pytest.raises(SourceError):
        reopened.publish(batch['batch_id'])


def test_failed_first_wiki_publication_retains_previous_legacy_revision(tmp_path, monkeypatch):
    service, receipt, s = source(tmp_path)
    old = service.publish(service.prepare(receipt['revision_id'])['candidate_id'])
    path = tmp_path / 'new.txt'
    path.write_text('新版依据', encoding='utf-8')
    newer = service.receive(path, filename=path.name, request_id='new', origin={'channel': 'test'}, document_id=s['document_id'])
    wiki = WikiService(tmp_path)
    parsed = service.parse(newer['revision_id'])
    ns = dict(document_id=s['document_id'], revision_id=newer['revision_id'], parse_id=parsed['parse_id'], ordinal=0)
    batch = wiki.prepare([page(ns)])
    monkeypatch.setattr(wiki, '_verify_index', lambda *args: (_ for _ in ()).throw(SourceError('index_failed', '故障')))
    with pytest.raises(SourceError):
        wiki.publish(batch['batch_id'])
    assert readable(tmp_path / old['path'])
    assert service.current_publication(s['document_id'], s['revision_id'])['publish_id'] == old['publish_id']
    assert service.search('射频设备')[0]['publish_id'] == old['publish_id']
