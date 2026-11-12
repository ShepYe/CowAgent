"""隔离根贯通生产PDF准备、媒体、观察、主题发布及检索回读。"""
import json
from copy import deepcopy
from io import BytesIO

import pytest
from PIL import Image

from agent.tools.read.read import Read
from agent.tools.knowledge_ingest import KnowledgeIngest
from agent.knowledge.personal_assistant import SourceService
from agent.knowledge.wiki import WikiService
from common.personal_tasks import TaskStore, OWNER, task_scope
from common.runtime_identity import identity_scope
from models.config_revision import capture_config_revision


@pytest.fixture
def flow(tmp_path, monkeypatch):
    from tests.test_reading_capabilities import ProbeModel
    monkeypatch.setattr('agent.knowledge.reading_capabilities.ensure_pdf_capability', lambda *a: True)
    data = tmp_path / 'instance'
    workspace = data / 'workspace'
    workspace.mkdir(parents=True)
    (data / 'config.json').write_text(json.dumps({'personal_assistant_enabled': True,
        'agent_workspace': str(workspace)}), encoding='utf-8')
    store = TaskStore(data / 'personal')
    revision = capture_config_revision()
    task = store.submit(OWNER, 'native-pdf', {'text': '整理图形'}, {'channel': 'web'}, revision)
    image = BytesIO()
    Image.new('RGB', (100, 100), 'blue').save(image, 'PDF')
    pdf = tmp_path / 'image.pdf'
    pdf.write_bytes(image.getvalue())
    entry = store.attach(OWNER, task['task_id'], pdf)
    task = store.claim(OWNER, task['task_id'])
    config = dict(cwd=str(workspace), task_store=store, document_reading_native_pdf_enabled=True)
    read = Read(config)
    read.model = ProbeModel()
    from agent.knowledge.reading_capabilities import capability_identity, _key
    capability = capability_identity(read.model, revision)
    source = SourceService(workspace)
    with source.transaction() as db:
        db.execute('CREATE TABLE reading_capabilities(id TEXT PRIMARY KEY,record TEXT)')
        db.execute('INSERT INTO reading_capabilities VALUES(?,?)', (_key(capability), json.dumps(
            dict(schema_version=1, state='supported', identity=capability))))
    with identity_scope(user_id=OWNER, session_id='session'), task_scope(task['task_id']):
        tool = KnowledgeIngest(config)
        decision = tool.execute(dict(task_id=task['task_id'], purpose='long_term', reason='整理'))
        assert decision.status == 'success', decision.result
        yield workspace, store, task, entry, read, tool


def deliver(result, task):
    from common.runtime_identity import current_identity
    context = dict(protocol='responses', model='offline-model', api_base='https://offline.invalid/v1',
                   identity=current_identity(), task_id=task['task_id'], config_revision=task['config_revision'],
                   provider='custom:test', mime_type='application/pdf', transport='inline')
    assert len(result.media) == 1
    ref = result.media[0]
    assert ref.supports(context)
    content = ref.loader(context)
    assert content.mime_type == 'application/pdf' and content.data.startswith(b'%PDF-')
    ref.on_delivery('sent')
    ref.on_delivery('completed')


def test_ingest_attachment_relative_path_uses_tool_workspace(flow):
    root, store, task, entry, read, tool = flow
    import os
    relative = os.path.relpath(entry['path'], root)
    result = tool.execute(dict(task_id=task['task_id'], path=relative,
        purpose='long_term', reason='继续整理同一附件'))
    assert result.status in ('success', 'running'), result.result
    assert len(store.get(OWNER, task['task_id'])['attachment_decisions']) == 1


def submission(task, reading):
    rid, segment = reading['reading_id'], reading['segment_id']
    return dict(task_id=task['task_id'], purpose='long_term', reason='图文主题整理',
        reading_submission_id='submission-1', reading_coverage=[dict(reading_id=rid, segment_id=segment)],
        reading_evidence=[dict(reading_id=rid, evidence_key='figure', segment_id=segment,
            location=dict(kind='pdf', physical_page=1), observation='整页显示蓝色正方形。', type='visual_interpretation')],
        pages=[dict(title='蓝色图形', category='测试', summary='说明该页蓝色图形的形状与出处。',
            blocks=[dict(content='本页图形为蓝色正方形。', sources=[dict(reading_id=rid, evidence_key='figure')])])])


def test_pdf_native_to_topic_search_readback_and_no_repeat_media(flow):
    root, store, task, entry, read, tool = flow
    result = read.execute(dict(path=entry['path'], pages='1'))
    assert result.status == 'success', result.result
    assert result.result['media_attached']
    deliver(result, task)
    cached = read.execute(dict(path=entry['path'], pages='1'))
    assert cached.status == 'success', cached.result
    assert cached.result['reading_id'] == result.result['reading_id']
    assert not cached.media
    args = submission(task, result.result)
    published = tool.execute(args)
    assert published.status == 'success', published.result
    assert published.result['status'] == 'succeeded'
    assert len(published.result['result_refs']) == 1
    wiki = WikiService(root)
    hits = wiki.search('蓝色')
    assert hits
    body = wiki.read_reference(published.result['result_refs'][0]['publish_id'])
    assert body['summary'] == args['pages'][0]['summary']
    assert body['sources'][0]['text'] == '整页显示蓝色正方形。'
    assert body['sources'][0]['location']['physical_page'] == 1
    source = SourceService(root)
    parse_id = body['sources'][0]['parse_id']
    parsed = json.loads((source.root / 'parses' / (parse_id + '.json')).read_text(encoding='utf-8'))
    unit = parsed['units'][body['sources'][0]['ordinal']]
    assert unit['type'] == 'visual_interpretation' and unit['origin'] == 'model'
    assert tool.execute(deepcopy(args)).status == 'success'
    assert len(store.get(OWNER, task['task_id'])['result_refs']) == 1


def test_prepared_pdf_cannot_publish_observations(flow):
    root, store, task, entry, read, tool = flow
    result = read.execute(dict(path=entry['path'], pages='1'))
    assert result.status == 'success', result.result
    published = tool.execute(submission(task, result.result))
    assert published.status == 'error'
    assert not WikiService(root).search('蓝色')


def test_published_native_sources_survive_real_backup_new_root(flow, tmp_path):
    from cli.commands.backup import create_backup_archive, restore_backup_archive
    root, store, task, entry, read, tool = flow
    result = read.execute(dict(path=entry['path'], pages='1'))
    assert result.status == 'success', result.result
    deliver(result, task)
    published = tool.execute(submission(task, result.result))
    assert published.status == 'success', published.result
    reference = published.result['result_refs'][0]
    before = WikiService(root).read_reference(reference['publish_id'])
    archive = tmp_path / 'native-backup.zip'
    create_backup_archive(archive, root.parent, root, complete=True)
    restored = tmp_path / 'new-instance'
    restore_backup_archive(archive, restored, restored / 'workspace')
    after = WikiService(restored / 'workspace').read_reference(reference['publish_id'])
    assert {k: v for k, v in after.items() if k != 'original'} == {k: v for k, v in before.items() if k != 'original'}
    from pathlib import Path
    assert Path(after['original']).is_relative_to(restored)
    assert WikiService(restored / 'workspace').search('蓝色')
    copied = after['sources'][0]['reading_copy']
    assert (restored / 'workspace' / copied['path']).is_file()
    # 材料损坏必须使读取失败，不能继续采用模型观察文本。
    (restored / 'workspace' / copied['path']).write_bytes(b'damaged')
    from agent.knowledge.parser import SourceError
    with pytest.raises(SourceError):
        WikiService(restored / 'workspace').read_reference(reference['publish_id'])
