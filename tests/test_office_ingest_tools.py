"""通过实际read/ingest工具完成Office图文主题发布；只替换LibreOffice进程。"""
import json

import pytest
from docx import Document

from agent.knowledge.personal_assistant import SourceService
from agent.knowledge.reading_capabilities import capability_identity, _key
from agent.knowledge.wiki import WikiService
from agent.tools.knowledge_ingest import KnowledgeIngest
from agent.tools.read.read import Read
from common.personal_tasks import OWNER, task_scope
from common.runtime_identity import identity_scope
from tests.test_office_reading_flow import env as office_environment, powerpoint
from tests.test_reading_capabilities import ProbeModel

env = office_environment


@pytest.mark.parametrize('suffix', ['docx', 'pptx'])
def test_office_tool_to_topic_and_source_copy(env, monkeypatch, suffix):
    root, store, task, executable, calls = env
    path = root.parent / ('input.' + suffix)
    if suffix == 'docx':
        document = Document()
        document.add_paragraph('Word 资料原文')
        document.save(path)
    else:
        powerpoint(path)
    entry = store.attach(OWNER, task['task_id'], path)
    task = store.claim(OWNER, task['task_id'])
    config = dict(cwd=str(root), workspace_root=str(root), task_store=store,
                  document_reading_native_pdf_enabled=True, document_reading_soffice=str(executable))
    config[f'document_reading_native_{suffix}_enabled'] = True
    read, ingest = Read(config), KnowledgeIngest(config)
    read.model = ProbeModel()
    monkeypatch.setattr('agent.knowledge.reading_capabilities.ensure_pdf_capability', lambda *a: True)
    identity = capability_identity(read.model, task['config_revision'])
    source = SourceService(root)
    with source.transaction() as db:
        db.execute('CREATE TABLE reading_capabilities(id TEXT PRIMARY KEY,record TEXT)')
        db.execute('INSERT INTO reading_capabilities VALUES(?,?)', (_key(identity), json.dumps(
            dict(schema_version=1, state='supported', identity=identity))))
    with identity_scope(user_id=OWNER, session_id='office-tools'), task_scope(task['task_id']):
        decided = ingest.execute(dict(task_id=task['task_id'], purpose='long_term', reason='整理图文'))
        assert decided.status == 'success', decided.result
        assert not calls, '用途登记不应转换或调用模型'
        result = read.execute(dict(path=entry['path'], visual=True))
        assert result.status == 'success', result.result
        data = result.result
        assert data['source_format'] == suffix
        assert data['source_locations'][0]['location']['kind'] == ('slide' if suffix == 'pptx' else 'word_reading_copy_page')
        ref = result.media[0]
        ref.on_delivery('sent')
        ref.on_response(dict(id='office-response', status='completed', output=[]))
        ref.on_delivery('completed')
        location = data['source_locations'][0]['location']
        args = dict(task_id=task['task_id'], purpose='long_term', reason='图文主题',
            reading_submission_id='office-submit', reading_coverage=[dict(reading_id=data['reading_id'], segment_id=data['segment_id'])],
            reading_evidence=[dict(reading_id=data['reading_id'], segment_id=data['segment_id'], evidence_key='figure',
                location=location, observation='图中显示业务流程与连接关系。', type='visual_interpretation')],
            pages=[dict(title='Office图文主题', category='测试', summary='说明原件图示中的业务流程。',
                blocks=[dict(content='图示通过连接线表达业务流程。', sources=[dict(reading_id=data['reading_id'], evidence_key='figure')])])])
        published = ingest.execute(args)
        assert published.status == 'success', published.result
        assert published.result['status'] == 'succeeded'
        body = WikiService(root).read_reference(published.result['result_refs'][0]['publish_id'])
        assert body['sources'][0]['location']['kind'] == location['kind']
        assert body['sources'][0]['origin'] == 'model'
        assert (root / body['sources'][0]['reading_copy']['path']).is_file()
        assert WikiService(root).search('业务')
        assert len(ingest.execute(args).result['result_refs']) == 1
        assert not read.execute(dict(path=entry['path'])).media


def test_office_visual_false_keeps_text_without_conversion(env, monkeypatch):
    root, store, task, executable, calls = env
    path = root.parent / 'input.docx'
    document = Document()
    document.add_paragraph('Word 资料原文')
    document.save(path)
    entry = store.attach(OWNER, task['task_id'], path)
    task = store.claim(OWNER, task['task_id'])
    read = Read(dict(cwd=str(root), task_store=store, document_reading_native_docx_enabled=True,
                     document_reading_soffice=str(executable)))
    monkeypatch.setattr('agent.knowledge.reading_capabilities.ensure_pdf_capability',
                        lambda *a: pytest.fail('visual=false不得探测或外发'))
    with identity_scope(user_id=OWNER, session_id='office-text'), task_scope(task['task_id']):
        result = read.execute(dict(path=entry['path'], visual=False))
    assert result.status == 'success', result.result
    assert result.result['units'][0]['text'] == 'Word 资料原文'
    assert not result.media and not calls


def test_general_office_media_rechecks_original_without_registering_source(env, monkeypatch):
    from common.runtime_identity import current_identity
    from models.config_revision import capture_config_revision
    root, store, task, executable, calls = env
    path = root / 'general.docx'
    document = Document()
    document.add_paragraph('Word 资料原文')
    document.save(path)
    tool = Read(dict(cwd=str(root), document_reading_native_docx_enabled=True,
                     document_reading_soffice=str(executable)))
    tool.model = ProbeModel()
    monkeypatch.setattr('agent.knowledge.reading_capabilities.ensure_pdf_capability', lambda *a: True)
    with identity_scope(user_id=OWNER, session_id='general-office'):
        result = tool.execute(dict(path=str(path), visual=True))
        assert result.status == 'success', result.result
        assert result.result['filename'] == path.name
        assert result.result['source_locations'][0]['location']['kind'] == 'word_reading_copy_page'
        assert 'reading_id' not in result.result and result.media
        context = dict(protocol='responses', model=tool.model.model, api_base='https://offline.invalid/v1',
                       identity=current_identity(), config_revision=capture_config_revision(), task_id=None)
        assert result.media[0].loader(context).data.startswith(b'%PDF-')
        path.write_bytes(b'changed')
        with pytest.raises(ValueError, match='version_conflict'):
            result.media[0].loader(context)
    assert not (root / '.personal_sources/sources.sqlite3').exists()
