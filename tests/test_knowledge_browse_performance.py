"""临时小知识库的真实浏览路径，锁住重复目录与来源初始化。"""
from time import perf_counter
from pathlib import Path

import pytest

from agent.knowledge.service import KnowledgeService
from agent.knowledge.wiki import WikiService
from agent.knowledge.personal_assistant import SourceService
from test_knowledge_wiki_publication import source, page, publish


@pytest.fixture
def small_knowledge(tmp_path):
    _, _, evidence = source(tmp_path)
    wiki = WikiService(tmp_path)
    refs = publish(wiki, *(page(evidence, title=f'知识{i}', content='依据与结论。' * 180)
                           for i in range(4)))
    return tmp_path, refs


@pytest.mark.parametrize('operation', ['list_tree', 'catalog', 'build_graph'])
def test_small_browse_initializes_authority_once(small_knowledge, monkeypatch, operation):
    root, refs = small_knowledge
    calls = []
    refreshes = []
    projections = []
    original = SourceService.__init__
    from agent.knowledge import wiki as wiki_module
    refresh = wiki_module.refresh
    project = wiki_module.project_indexes

    def counted(self, *args, **kwargs):
        calls.append(1)
        original(self, *args, **kwargs)

    def counted_refresh(*args, **kwargs):
        refreshes.append(1)
        return refresh(*args, **kwargs)

    def counted_projection(*args, **kwargs):
        projections.append(1)
        return project(*args, **kwargs)

    def unexpected_model(*args, **kwargs):
        pytest.fail('浏览知识不应初始化检索或向量模型')

    monkeypatch.setattr(SourceService, '__init__', counted)
    monkeypatch.setattr(wiki_module, 'refresh', counted_refresh)
    monkeypatch.setattr(wiki_module, 'project_indexes', counted_projection)
    monkeypatch.setattr(KnowledgeService, '_manager', unexpected_model)
    monkeypatch.setattr('agent.memory.embedding.create_default_embedding_provider', unexpected_model)
    start = perf_counter()
    result = getattr(KnowledgeService(root), operation)()
    elapsed = perf_counter() - start
    size = sum((root / ref['path']).stat().st_size for ref in refs)
    print(f'{operation}: {elapsed:.4f}s; bytes={size}; '
          f'authority_initializations={len(calls)}; refreshes={len(refreshes)}; '
          f'projections={len(projections)}')
    if operation == 'catalog':
        assert len(result['pages']) == len(refs)
    elif operation == 'build_graph':
        assert len(result['nodes']) == len(refs)
    else:
        assert result['stats']['pages'] == len(refs)
    assert len(calls) <= 1, '一次浏览不应为每篇知识重新初始化权威来源'
    assert len(refreshes) <= 1, '一次浏览不应为每篇知识恢复整库投影'
    assert len(projections) <= (0 if operation == 'catalog' else 1)


@pytest.mark.parametrize('operation', ['list_tree', 'build_graph'])
@pytest.mark.parametrize('invalidate', ['withdraw', 'content', 'original'])
def test_browse_snapshot_does_not_cache_invalidated_pages(small_knowledge, operation, invalidate):
    root, refs = small_knowledge
    service = KnowledgeService(root)
    assert getattr(service, operation)()
    if invalidate == 'withdraw':
        src = SourceService(root)
        src.change(refs[0]['sources'][0]['document_id'], action='withdraw',
                   expected_state=1, reason='测试撤回')
    elif invalidate == 'content':
        (root / refs[0]['path']).write_text('# 被改写的知识', encoding='utf-8')
    else:
        src = SourceService(root)
        evidence = refs[0]['sources'][0]
        revision = src.registered_revision(evidence['document_id'], evidence['revision_id'])
        Path(revision['original']).write_text('被篡改的原件', encoding='utf-8')
    result = getattr(service, operation)()
    expected = 3 if invalidate == 'content' else 0
    if operation == 'list_tree':
        assert result['stats']['pages'] == expected
    else:
        assert len(result['nodes']) == expected


def test_shared_knowledge_browse_uses_shared_authority(small_knowledge, tmp_path, monkeypatch):
    root, refs = small_knowledge
    agent = tmp_path / 'agent-without-own-knowledge'
    agent.mkdir()
    monkeypatch.setattr('common.state_dir.knowledge_dir', lambda **kwargs: root / 'knowledge')
    service = KnowledgeService(agent)
    assert service.list_tree()['stats']['pages'] == len(refs)
    assert len(service.build_graph()['nodes']) == len(refs)
    assert not (agent / '.personal_sources').exists()


def test_browse_guards_match_single_file_authority(small_knowledge):
    from agent.knowledge.catalog import browse_snapshot
    from agent.knowledge.personal_assistant import readable
    root, refs = small_knowledge
    legacy, receipt, _ = source(root, name='legacy')
    candidate = legacy.prepare(receipt['revision_id'])
    legacy_ref = legacy.publish(candidate['candidate_id'])
    paths = [root / ref['path'] for ref in [*refs, legacy_ref]]
    for rel in ['notes/free.md', 'wiki/orphan.md', 'managed/orphan.md']:
        path = root / 'knowledge' / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# 测试知识', encoding='utf-8')
        paths.append(path)
    paths[0].write_text('# 被篡改的知识', encoding='utf-8')
    expected = [readable(path) for path in paths]
    with browse_snapshot(root / 'knowledge') as snapshot:
        assert [KnowledgeService._browse_readable(path, snapshot) for path in paths] == expected
    service = KnowledgeService(root)
    graph = service.build_graph()
    node = next(n for n in graph['nodes'] if n['id'] == legacy_ref['path'].removeprefix('knowledge/'))
    assert node['label'] == 'legacy' and node['category'] == '资料原文'
