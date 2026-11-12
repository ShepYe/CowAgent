"""KF-05：认证、参数和范围在资产解析前检查，PNG 不走任意文件路径。"""
import json
from types import SimpleNamespace
import pytest
from test_knowledge_catalog_api import catalog_api as catalog_api_fixture

catalog_api = catalog_api_fixture


def call(api, monkeypatch, **query):
    params = dict(agent_id='alpha', publish_id='publication', figure_id='figure', view='current')
    params.update(query)
    monkeypatch.setattr(api.knowledge.web, 'input', lambda **defaults: SimpleNamespace(**dict(defaults, **params)))
    return api.knowledge.KnowledgeFigureHandler().GET()


def test_figure_auth_precedes_storage(catalog_api, monkeypatch):
    api = catalog_api
    api.authenticated['value'] = False
    monkeypatch.setattr(api.knowledge, '_get_workspace_root', lambda **kw: pytest.fail('未登录不能访问工作区'))
    with pytest.raises(api.knowledge.web.HTTPError):
        call(api, monkeypatch)


@pytest.mark.parametrize('query', [dict(view='other'), dict(view='../history'), dict(publish_id='../p'),
    dict(figure_id='/tmp/a.png'), dict(figure_id=''), dict(publish_id='a' * 129)])
def test_figure_invalid_input_precedes_workspace(catalog_api, monkeypatch, query):
    monkeypatch.setattr(catalog_api.knowledge, '_get_workspace_root', lambda **kw: pytest.fail('参数须先验证'))
    result = json.loads(call(catalog_api, monkeypatch, **query))
    assert result['code'] == 'invalid_request'


@pytest.mark.parametrize('view', ['current', 'history'])
def test_figure_scoped_png_no_cache(catalog_api, monkeypatch, view):
    api = catalog_api
    from agent.knowledge import figures
    calls, headers = [], []
    monkeypatch.setattr(figures, 'resolve_figure', lambda workspace, publication, figure, **kw:
        calls.append((workspace, publication, figure, kw)) or dict(data=b'PNG', mime_type='image/png'))
    monkeypatch.setattr(api.knowledge.web, 'header', lambda *a, **kw: headers.append(a))
    assert call(api, monkeypatch, agent_id='beta', view=view) == b'PNG'
    assert calls == [(str(api.roots['beta']), 'publication', 'figure', dict(view=view))]
    assert ('Cache-Control', 'no-store') in headers and ('Content-Type', 'image/png') in headers


def test_figure_unknown_agent_never_resolves(catalog_api, monkeypatch):
    from agent.knowledge import figures
    monkeypatch.setattr(figures, 'resolve_figure', lambda *a, **kw: pytest.fail('越权不得解析'))
    assert json.loads(call(catalog_api, monkeypatch, agent_id='unknown-agent'))['code'] == 'forbidden'


@pytest.mark.parametrize('code', ['storage_failed', 'version_conflict', 'forbidden'])
def test_figure_resolver_failure_is_structured(catalog_api, monkeypatch, code):
    from agent.knowledge import figures
    from agent.knowledge.parser import SourceError
    def fail(*args, **kwargs): raise SourceError(code, 'internal path secret')
    monkeypatch.setattr(figures, 'resolve_figure', fail)
    result = json.loads(call(catalog_api, monkeypatch))
    assert result['code'] == code and 'secret' not in str(result)


from tests.test_knowledge_figures import flow as flow_fixture, draft, publish

flow = flow_fixture


def test_real_resolver_single_missing_png_and_original(flow, monkeypatch):
    from pathlib import Path
    from channel.web.api import knowledge
    from agent.knowledge.figures import resolve_figure
    wiki, ref, item = publish(flow, draft(flow))
    monkeypatch.setattr(knowledge, '_require_auth', lambda: None)
    monkeypatch.setattr(knowledge, '_get_workspace_root', lambda **kw: str(wiki.workspace))
    monkeypatch.setattr(knowledge.web, 'header', lambda *a, **kw: None)
    monkeypatch.setattr(knowledge.web, 'ctx', SimpleNamespace(env={}))
    params = dict(agent_id='', publish_id=ref['publish_id'], figure_id=item['figure_id'],
                  figure_key=item['figure_key'], view='current')
    monkeypatch.setattr(knowledge.web, 'input', lambda **defaults: SimpleNamespace(**dict(defaults, **params)))
    png = knowledge.KnowledgeFigureHandler().GET()
    assert png.startswith(b'\x89PNG')
    original = knowledge.KnowledgeFigureOriginalHandler().GET()
    assert original.startswith(b'%PDF-')
    params['material'] = 'reading-copy'
    assert knowledge.KnowledgeFigureOriginalHandler().GET().startswith(b'%PDF-')
    resolved = resolve_figure(wiki.workspace, ref['publish_id'], item['figure_id'])
    Path(resolved['path']).unlink()  # 单张真实 PNG 缺失，正文和原件仍可读。
    result = json.loads(knowledge.KnowledgeFigureHandler().GET())
    assert result['code'] == 'storage_failed'
    assert knowledge.KnowledgeFigureOriginalHandler().GET().startswith(b'%PDF-')
    assert wiki.current(ref['page_id'])['searchable']
    params['view'] = 'history'
    assert json.loads(knowledge.KnowledgeFigureHandler().GET())['code'] == 'storage_failed'
