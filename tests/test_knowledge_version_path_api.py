"""历史链接按完整登记路径回读，复用独立 API 验收的隔离载体。"""
from pathlib import Path

import pytest

from test_knowledge_catalog_api import catalog_api as _catalog_api_fixture, publish_topic
from agent.knowledge.wiki import WikiService

catalog_api = _catalog_api_fixture


def test_path_handler_preserves_current_and_historical_contract(catalog_api):
    api = catalog_api
    first = publish_topic(api.roots['alpha'])
    second = publish_topic(api.roots['alpha'], previous=first, sources=first['sources'], content='新内容')
    rel = first['path'].removeprefix('knowledge/')
    result = api.request('KnowledgeVersionHandler', path='./wiki/../' + rel)
    assert result['status'] == 'success'
    assert result['version']['publish_id'] == first['publish_id']
    assert result['version']['path'] == rel
    assert result['version']['current_status'] == 'superseded'
    assert not result['version']['searchable']
    assert result['version']['current_path'] == second['path'].removeprefix('knowledge/')
    assert api.request('KnowledgeVersionHandler', publish_id=first['publish_id']) == result
    assert api.request('KnowledgeVersionHandler', agent='beta', path=rel)['status'] == 'error'
    Path(api.roots['alpha'] / first['path']).write_text('篡改正文', encoding='utf-8')
    assert api.request('KnowledgeVersionHandler', path=rel)['status'] == 'error'


@pytest.mark.parametrize('query', [
    {}, {'publish_id': 'id', 'path': 'wiki/a.md'}, {'path': '../wiki/a.md'},
    {'path': '/wiki/a.md'}, {'path': 'C:/wiki/a.md'}, {'path': 'wiki/../../wiki/a.md'},
])
def test_path_handler_rejects_invalid_selectors(catalog_api, query):
    response = catalog_api.request('KnowledgeVersionHandler', **query)
    assert response['status'] == 'error' and response['code'] == 'invalid_request'


def test_path_handler_never_guesses_basename_or_unregistered_file(catalog_api):
    api = catalog_api
    ref = publish_topic(api.roots['alpha'])
    basename = ref['path'].rsplit('/', 1)[-1]
    for path in (basename, 'wiki/错误/' + basename, 'wiki/未登记.md'):
        file = api.roots['alpha'] / 'knowledge' / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('# 同名或未登记文件', encoding='utf-8')
        response = api.request('KnowledgeVersionHandler', path=path)
        assert response['status'] == 'error'


def test_path_handler_authenticates_before_service_access(catalog_api, monkeypatch):
    api = catalog_api
    api.authenticated['value'] = False
    def unexpected(*args, **kwargs):
        pytest.fail('未授权不得访问历史服务')
    monkeypatch.setattr(WikiService, 'version', unexpected)
    with pytest.raises(api.knowledge.web.HTTPError):
        api.request('KnowledgeVersionHandler', path='wiki/any.md')


@pytest.mark.parametrize('legacy', ['', '# 我的说明\n\n旧文字与 [旧笔记](./notes/旧.md)'])
def test_catalog_handler_returns_only_legacy_index_content(catalog_api, legacy):
    api = catalog_api
    index = api.roots['alpha'] / 'knowledge/index.md'
    content = legacy + '\n<!-- wiki-projection:start -->\n自动投影\n<!-- wiki-projection:end -->\n'
    index.write_text(content, encoding='utf-8')
    before = index.read_bytes(), index.stat().st_mtime_ns
    response = api.request('KnowledgeCatalogHandler')
    assert response['status'] == 'success'
    assert response['legacy_content'] == legacy
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
