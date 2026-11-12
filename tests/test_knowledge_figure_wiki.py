"""图文发布沿用正文证据、不可变历史和依赖失效边界。"""
import json

import pytest

from agent.knowledge.parser import SourceError
from agent.knowledge.wiki import WikiService
from tests.test_reading_ingest_flow import flow as flow_fixture, deliver, submission

flow = flow_fixture


def publish_figure(flow):
    root, store, task, entry, read, tool = flow
    result = read.execute(dict(path=entry['path'], pages='1'))
    deliver(result, task)
    args = submission(task, result.result)
    args['pages'][0]['blocks'][0]['figures'] = [dict(figure_key='blue', caption='蓝色正方形的整页原图',
        source=args['pages'][0]['blocks'][0]['sources'][0])]
    result = tool.execute(args)
    assert result.status == 'success', result.result
    return WikiService(root), result.result['result_refs'][0]


def test_caption_index_and_archive_survive_missing_png(flow):
    wiki, ref = publish_figure(flow)
    version = wiki.version(ref['publish_id'])
    assert version['figure_state'] == 'ready'
    assert 'knowledge-figure:' in version['content']
    assert wiki.search('整页原图')[0]['publish_id'] == ref['publish_id']
    from agent.knowledge.figures import resolve_figure
    item = version['figure_manifest']['items'][0]
    image = resolve_figure(wiki.workspace, ref['publish_id'], item['figure_id'])
    assert image['data'].startswith(b'\x89PNG')
    from pathlib import Path
    Path(image['path']).unlink()
    with pytest.raises(SourceError) as error:
        resolve_figure(wiki.workspace, ref['publish_id'], item['figure_id'])
    assert error.value.code == 'storage_failed'
    # 图片失效与正文失效独立，目录、正文、历史和文字检索继续可用。
    assert wiki.current(ref['page_id'])
    assert wiki.version(ref['publish_id'])['content'] == version['content']
    assert wiki.search('整页原图')


def test_figure_update_keeps_summary_category_and_suspends_dependents(flow):
    wiki, ref = publish_figure(flow)
    original = wiki.read_reference(ref['publish_id'])
    source = {key: original['sources'][0][key] for key in ('document_id', 'revision_id', 'parse_id', 'ordinal')}
    dependent = wiki.prepare([dict(title='下游蓝色', summary='依赖图形结论。', category='测试',
        blocks=[dict(content='依据上游理解图形。', sources=[source])],
        depends_on=[dict(page_id=ref['page_id'], page_version=ref['publish_id'])])])
    downstream = wiki.publish(dependent['batch_id'])[0]
    wiki.set_page_category(ref['page_id'], ref['publish_id'], 0, '手动分类')
    candidate = wiki.prepare_figure_update(ref['page_id'], ref['publish_id'], task_id='figure-retry')
    updated = wiki.publish(candidate['batch_id'])[0]
    assert updated['page_id'] == ref['page_id'] and updated['publish_id'] != ref['publish_id']
    assert updated['category'] == '手动分类' and updated['category_mode'] == 'manual'
    assert updated['summary'] == ref['summary']
    assert wiki.current(downstream['page_id']) is None
    assert not wiki.version(downstream['publish_id'])['searchable']
    assert wiki.version(ref['publish_id'])['content'] != wiki.version(updated['publish_id'])['content']
    assert wiki.read_reference(ref['publish_id'], historical=True)['content'] == original['content']
    assert not any(hit['page_id'] == downstream['page_id'] for hit in wiki.search('依据上游'))
    with pytest.raises(SourceError):
        wiki.prepare_figure_update(ref['page_id'], ref['publish_id'], task_id='stale')


def test_new_inputs_cannot_bypass_manifest(flow):
    wiki, ref = publish_figure(flow)
    with wiki.source.transaction() as db:
        record = json.loads(db.execute('SELECT record FROM wiki_publications WHERE id=?', (ref['publish_id'],)).fetchone()[0])
    source = {key: record['sources'][0][key] for key in ('document_id', 'revision_id', 'parse_id', 'ordinal')}
    with pytest.raises(SourceError):
        wiki.prepare([dict(title='绕过图片', summary='非法受控图片。', category='测试',
            blocks=[dict(content='![伪造](knowledge-figure:untrusted/file)', sources=[source])])])


def test_browse_does_not_read_png_bytes(flow, monkeypatch):
    from pathlib import Path
    from agent.knowledge.service import KnowledgeService
    wiki, ref = publish_figure(flow)
    original = Path.read_bytes

    def no_png_reads(path):
        if 'figures' in path.parts and path.suffix == '.png':
            pytest.fail('目录和图谱不应读取 PNG 字节')
        return original(path)

    monkeypatch.setattr(Path, 'read_bytes', no_png_reads)
    service = KnowledgeService(wiki.workspace)
    assert service.catalog()['pages']
    assert service.list_tree()
    assert service.build_graph()
    assert wiki.current(ref['page_id'])['figure_state'] == 'ready'


def test_pending_text_publishes_then_retry_creates_new_version(flow, monkeypatch):
    from agent.knowledge import reading_materials
    root, store, task, entry, read, tool = flow
    result = read.execute(dict(path=entry['path'], pages='1'))
    deliver(result, task)
    args = submission(task, result.result)
    args['pages'][0]['blocks'][0]['figures'] = [dict(figure_key='blue', caption='待补图形',
        source=args['pages'][0]['blocks'][0]['sources'][0])]
    original = reading_materials._plan_region

    def unavailable(*args, **kwargs):
        raise reading_materials.ReadingMaterialError('render_failed', '模拟渲染失败')

    monkeypatch.setattr(reading_materials, '_plan_region', unavailable)
    published = tool.execute(args)
    assert published.status == 'success', published.result
    assert published.result['status'] == 'running'
    ref = published.result['result_refs'][0]
    wiki = WikiService(root)
    assert wiki.current(ref['page_id'])['figure_state'] == 'pending'
    text = wiki.version(ref['publish_id'])['content']
    assert '待补图形' in text and 'knowledge-figure:' not in text
    from common.personal_tasks import TaskError, OWNER
    with pytest.raises(TaskError, match='figure_incomplete'):
        store.complete(OWNER, task['task_id'], task['attempt'], 'published', [])
    monkeypatch.setattr(reading_materials, '_plan_region', original)
    from agent.knowledge.figure_publication import resume_publication
    resume_publication(store, task)
    updated = wiki.current(ref['page_id'])
    assert updated['figure_state'] == 'ready'
    assert updated['publish_id'] != ref['publish_id']
    assert wiki.version(ref['publish_id'])['content'] == text
    assert store.complete(OWNER, task['task_id'], task['attempt'], 'published', [])['status'] == 'succeeded'
