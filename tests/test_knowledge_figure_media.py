"""受控图片工具边界：隔离材料、真实媒体展开、迟到及旧文本调用兼容。"""
import base64
from copy import deepcopy
from io import BytesIO
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from agent.knowledge.parser import SourceError
from agent.protocol.tool_media import ToolMediaError, expand_tool_media
from agent.tools.knowledge_ingest import KnowledgeIngest, _figure_pages
from agent.tools.read.read import Read
from common.runtime_identity import current_identity, identity_scope
from common.personal_tasks import current_task_id, task_scope, TaskStore, OWNER
from models.config_revision import capture_config_revision
from tests.test_reading_capabilities import ProbeModel


def source():
    return dict(document_id='doc', revision_id='rev', parse_id='parse', ordinal=0)


def pages():
    return [dict(title='图', category='测试', summary='图示', blocks=[dict(content='关键图',
        sources=[source()], figures=[dict(figure_key=' key ', caption=' 图注 ', source=source())])])]


def context(model):
    return dict(protocol='responses', model=model.model, provider=model.bot_type,
        api_base=model.bot.get_api_config()['api_base'], identity=current_identity(),
        config_revision=capture_config_revision(), task_id=current_task_id(), transport='inline')


@pytest.fixture
def media(tmp_path, monkeypatch):
    image = BytesIO()
    Image.new('RGB', (16, 16), 'blue').save(image, 'PNG')
    state = dict(data=image.getvalue(), mime_type='image/png', item={'figure_id': 'fig', 'caption': '图'},
                 path=tmp_path / 'private.png')
    calls = []

    def resolve(workspace, publish_id, figure_id, view='current'):
        calls.append((workspace, publish_id, figure_id, view))
        if state.get('error'):
            raise SourceError('storage_failed', '不得泄露本地路径')
        return deepcopy(state)

    # 资产模块的真实来源/哈希验证由资产测试负责；此处固定其公开接口。
    monkeypatch.setitem(sys.modules, 'agent.knowledge.figures', SimpleNamespace(resolve_figure=resolve))
    monkeypatch.setattr('common.state_dir.knowledge_dir', lambda **kw: tmp_path / 'knowledge')
    tool = Read(dict(cwd=str(tmp_path)))
    tool.model = ProbeModel()
    tool.model.catalog_model_meta = lambda: {'capabilities': ['vision']}
    monkeypatch.setattr(tool, '_resolve_path', lambda *a: pytest.fail('逻辑URI不得进入通用路径'))
    with identity_scope(user_id=OWNER, session_id='figures'):
        yield tool, state, calls


def test_png_expands_to_actual_main_model_image(media):
    tool, state, calls = media
    result = tool.execute(dict(path='knowledge-figure:pub/fig'))
    assert result.status == 'success', result.result
    assert str(state['path']) not in str(result.result)
    payload = dict(input=[dict(type='function_call_output', call_id='figure', output='关键图')])
    delivery = expand_tool_media(payload, {'figure': result.media}, context(tool.model))
    assert len(delivery.refs) == 1 and len(calls) == 2
    parts = payload['input'][0]['output']
    assert parts[1]['type'] == 'input_image'
    assert base64.b64decode(parts[1]['image_url'].split(',', 1)[1]) == state['data']


def test_history_is_explicit_and_preserved_at_load(media):
    tool, _, calls = media
    result = tool.execute(dict(path='knowledge-figure:pub/fig?view=history'))
    assert result.status == 'success', result.result
    result.media[0].loader(context(tool.model))
    assert [c[-1] for c in calls] == ['history', 'history']
    assert result.result['view'] == 'history'


@pytest.mark.parametrize('suffix', ['?view=current', '?view=other', '?view=history&x=1', '?x=1',
    '?view=history&view=history', '/extra', '/../fig', '%2Fextra', '#anchor', '?view=history#x'])
def test_invalid_uri_never_falls_back_or_resolves(media, suffix):
    tool, _, calls = media
    result = tool.execute(dict(path='knowledge-figure:pub/fig' + suffix))
    assert result.status == 'error' and result.result['code'] == 'invalid_request'
    assert not calls


@pytest.mark.parametrize('argument', ['pages', 'offset', 'limit', 'region', 'parse_id', 'worksheet', 'objects'])
def test_figure_rejects_material_scope_overrides(media, argument):
    tool, _, calls = media
    assert tool.execute(dict(path='knowledge-figure:pub/fig', **{argument: '1'})).status == 'error'
    assert not calls


def test_visual_false_does_not_resolve_or_probe(media, monkeypatch):
    tool, _, calls = media
    monkeypatch.setattr('agent.knowledge.image_reading_capabilities.ensure_image_capability',
                        lambda *a: pytest.fail('visual=false不得探测'))
    result = tool.execute(dict(path='knowledge-figure:pub/fig', visual=False))
    assert result.status == 'error' and not result.media and not calls
    assert '未看图' in result.result['message']


def test_unsupported_capability_reports_unseen(media, monkeypatch):
    tool, _, _ = media
    tool.model.catalog_model_meta = lambda: {}
    monkeypatch.setattr('agent.knowledge.image_reading_capabilities.ensure_image_capability', lambda *a: False)
    result = tool.execute(dict(path='knowledge-figure:pub/fig'))
    assert result.result['code'] == 'attachment_unsupported' and not result.media
    assert '未看图' in result.result['message']


@pytest.mark.parametrize('field', ['identity', 'task_id', 'config_revision', 'provider', 'model', 'api_base', 'protocol'])
def test_request_binding_is_rechecked(media, field):
    tool, _, _ = media
    result = tool.execute(dict(path='knowledge-figure:pub/fig'))
    ctx = context(tool.model)
    ctx[field] = 'other'
    assert not result.media[0].supports(ctx)
    with pytest.raises(ToolMediaError):
        result.media[0].loader(ctx)


@pytest.mark.parametrize('change', ['data', 'item', 'withdrawn', 'session', 'config'])
def test_loader_rechecks_source_bytes_and_live_identity(media, change, monkeypatch):
    tool, state, _ = media
    result = tool.execute(dict(path='knowledge-figure:pub/fig'))
    ctx = context(tool.model)
    if change == 'data':
        state['data'] += b'tampered'
    elif change == 'item':
        state['item']['caption'] = '换绑'
    elif change == 'withdrawn':
        state['error'] = True
    elif change == 'config':
        monkeypatch.setattr('models.config_revision.capture_config_revision', lambda: 'changed')
    elif change == 'session':
        with identity_scope(session_id='other'):
            with pytest.raises(ToolMediaError):
                result.media[0].loader(ctx)
        return
    with pytest.raises(ToolMediaError):
        result.media[0].loader(ctx)


def test_task_cancellation_rejects_late_media(media, tmp_path):
    tool, _, _ = media
    store = TaskStore(tmp_path / 'tasks')
    task = store.submit(OWNER, 'figure', {'text': '图'}, {'channel': 'web'}, capture_config_revision())
    task = store.claim(OWNER, task['task_id'])
    tool.config['task_store'] = store
    with task_scope(task['task_id']):
        result = tool.execute(dict(path='knowledge-figure:pub/fig'))
        assert result.status == 'success', result.result
        ctx = context(tool.model)
        store.cancel(OWNER, task['task_id'], task['attempt'])
        with pytest.raises(ToolMediaError):
            result.media[0].loader(ctx)


def test_request_budget_is_enforced_before_media(media):
    tool, _, _ = media
    tool.config['reading_pdf_budget'] = {'max_request_bytes': 1}
    result = tool.execute(dict(path='knowledge-figure:pub/fig'))
    assert result.result['code'] == 'reading_budget_exceeded' and not result.media


def test_shared_knowledge_scope_is_bound_and_rechecked(media, tmp_path, monkeypatch):
    tool, _, calls = media
    shared = tmp_path / 'shared'
    monkeypatch.setattr('common.state_dir.knowledge_dir', lambda **kw: shared / 'knowledge')
    result = tool.execute(dict(path='knowledge-figure:pub/fig'))
    assert result.status == 'success', result.result
    assert calls[0][0] == shared
    ctx = context(tool.model)
    result.media[0].loader(ctx)
    with identity_scope(agent_id='another-agent'):
        with pytest.raises(ToolMediaError):
            result.media[0].loader(ctx)
    monkeypatch.setattr('common.state_dir.knowledge_dir', lambda **kw: tmp_path / 'other' / 'knowledge')
    with pytest.raises(ToolMediaError):
        result.media[0].loader(ctx)


def test_schema_requires_explicit_figures_but_runtime_preserves_legacy():
    block = KnowledgeIngest().params['properties']['pages']['items']['properties']['blocks']['items']
    assert 'figures' in block['required']
    assert len(block['properties']['figures']['items']['properties']['source']['oneOf']) == 2
    old = pages()
    del old[0]['blocks'][0]['figures']
    assert _figure_pages(old) == old and 'figures' not in _figure_pages(old)[0]['blocks'][0]
    assert _figure_pages(pages())[0]['blocks'][0]['figures'][0]['figure_key'] == 'key'


@pytest.mark.parametrize('defect', ['duplicate', 'extra', 'source', 'bool', 'nan', 'reverse', 'line', 'uri'])
def test_figure_input_rejects_bypass_and_invalid_regions(defect):
    value = pages()
    block = value[0]['blocks'][0]
    figure = block['figures'][0]
    if defect == 'duplicate':
        block['figures'].append(deepcopy(figure))
    elif defect == 'extra':
        figure['path'] = 'arbitrary.png'
    elif defect == 'source':
        figure['source']['ordinal'] = 99
    elif defect == 'bool':
        figure['region'] = [False, 0, 1, 1]
    elif defect == 'nan':
        figure['region'] = [0, float('nan'), 1, 1]
    elif defect == 'reverse':
        figure['region'] = [1, 0, 0, 1]
    elif defect == 'line':
        figure['caption'] = '两\n行'
    elif defect == 'uri':
        block['content'] = '![绕过](knowledge-figure:pub/fig)'
    with pytest.raises(SourceError):
        _figure_pages(value)


def test_reading_alias_keeps_four_part_figure_source(tmp_path, monkeypatch):
    from tests import test_reading_ingest_flow as fixtures
    from tests.test_reading_ingest_flow import deliver, submission
    fixture = fixtures.flow.__wrapped__(tmp_path, monkeypatch)
    root, store, task, entry, read, tool = next(fixture)
    try:
        result = read.execute(dict(path=entry['path'], pages='1'))
        deliver(result, task)
        args = submission(task, result.result)
        block = args['pages'][0]['blocks'][0]
        block['figures'] = [dict(figure_key='blue', caption='蓝色图', source=deepcopy(block['sources'][0]))]
        captured = []

        class Captured(BaseException):
            pass

        def prepare(store, task, submission, coverage, pages, workspace):
            captured.extend(pages)
            raise Captured()

        monkeypatch.setattr('agent.knowledge.reading_publication.prepare_publication', prepare)
        with pytest.raises(Captured):
            tool.execute(args)
        normalized = captured[0]['blocks'][0]
        assert normalized['figures'][0]['source'] == normalized['sources'][0]
        assert set(normalized['figures'][0]['source']) == set(source())
        assert 'reading_id' in args['pages'][0]['blocks'][0]['figures'][0]['source']
    finally:
        fixture.close()


def test_real_published_png_readback(tmp_path, monkeypatch):
    from tests import test_reading_ingest_flow as fixtures
    from tests.test_knowledge_figure_wiki import publish_figure
    fixture = fixtures.flow.__wrapped__(tmp_path, monkeypatch)
    flow = next(fixture)
    try:
        root, _, _, _, read, _ = flow
        wiki, ref = publish_figure(flow)
        item = ref['figure_manifest']['items'][0]
        read.model.catalog_model_meta = lambda: {'capabilities': ['vision']}
        monkeypatch.setattr('common.state_dir.knowledge_dir', lambda **kw: root / 'knowledge')
        with task_scope(None):
            result = read.execute(dict(path=f"knowledge-figure:{ref['publish_id']}/{item['figure_id']}"))
            assert result.status == 'success', result.result
            media = result.media[0].loader(context(read.model))
            import hashlib
            assert hashlib.sha256(media.data).hexdigest() == item['png_hash']
            from pathlib import Path
            from agent.knowledge.figures import resolve_figure
            Path(resolve_figure(root, ref['publish_id'], item['figure_id'])['path']).write_bytes(b'corrupt')
            with pytest.raises(ToolMediaError):
                result.media[0].loader(context(read.model))
    finally:
        fixture.close()


def test_legacy_synthesis_registers_figures_and_pending_stays_running(tmp_path, monkeypatch):
    from tests import test_reading_ingest_flow as fixtures
    from tests.test_reading_ingest_flow import deliver, submission
    from agent.knowledge.wiki import WikiService
    from agent.knowledge import reading_materials
    fixture = fixtures.flow.__wrapped__(tmp_path, monkeypatch)
    root, store, task, entry, read, tool = next(fixture)
    try:
        result = read.execute(dict(path=entry['path'], pages='1'))
        deliver(result, task)
        first = tool.execute(submission(task, result.result))
        body = WikiService(root).read_reference(first.result['result_refs'][0]['publish_id'])
        ref = {k: body['sources'][0][k] for k in source()}
        value = pages()
        block = value[0]['blocks'][0]
        block['sources'] = [ref]
        block['figures'][0]['source'] = ref
        monkeypatch.setattr(reading_materials, '_plan_region', lambda *a, **kw: (
            _ for _ in ()).throw(SourceError('storage_failed', '模拟缺图')))
        with task_scope(None):
            published = tool.execute(dict(purpose='long_term', reason='旧解析重用', pages=value))
            assert published.status == 'success', published.result
            current = published.result
            assert current['status'] == 'running' and current['stage'] == 'publish'
            assert current['figure_publication'] == {'schema_version': 1}
            assert 'reading_publication' not in current
            assert current['result_refs']
            missing = deepcopy(value)
            del missing[0]['blocks'][0]['figures']
            rejected = tool.execute(dict(task_id=current['task_id'], purpose='long_term', reason='漏交', pages=missing))
            assert rejected.status == 'error' and rejected.result['code'] == 'invalid_request'
            assert store.get(OWNER, current['task_id'])['status'] == 'running'
            # 同一任务接续复用已发布正文，补图可以生成新版本但不能创建第二主题。
            original_prepare = WikiService.prepare

            def only_update(self, value, *args, **kwargs):
                assert all(p.get('page_id') for p in value), '接续不得重建无page_id正文'
                return original_prepare(self, value, *args, **kwargs)

            monkeypatch.setattr(WikiService, 'prepare', only_update)
            resumed = tool.execute(dict(task_id=current['task_id'], purpose='long_term', reason='接续', pages=value))
            assert resumed.status == 'success', resumed.result
            assert resumed.result['status'] == 'running'
    finally:
        fixture.close()
