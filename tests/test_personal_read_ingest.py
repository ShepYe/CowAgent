from agent.knowledge.personal_assistant import SourceService
from agent.tools.knowledge_ingest import KnowledgeIngest
from agent.tools.read.read import Read
from common.personal_tasks import OWNER, TaskStore, task_scope
from common.runtime_identity import identity_scope
import pytest


@pytest.mark.parametrize('extension', ['txt', 'pdf', 'docx', 'xlsx'])
@pytest.mark.parametrize('retry', [False, True])
@pytest.mark.parametrize('legacy_echo', [False, True])
def test_read_then_ingest_uses_returned_source_identity(tmp_path, extension, retry, legacy_echo):
    original = tmp_path / ('source.' + extension)
    text = 'fixture antenna budget 42 dB'
    if extension == 'pdf':
        from test_personal_sources import write_test_pdf
        write_test_pdf(original, [text])
    elif extension == 'docx':
        from docx import Document
        document = Document()
        document.add_paragraph(text)
        document.save(original)
    elif extension == 'xlsx':
        from openpyxl import Workbook
        workbook = Workbook()
        workbook.active['A1'] = text
        workbook.save(original)
        workbook.close()
    else:
        original.write_text(text, encoding='utf-8')
    store = TaskStore(tmp_path / 'tasks')
    task = store.submit(OWNER, 'read-ingest', {'text': '分享资料'}, {'channel': 'feishu'}, 'cfg')
    entry = store.attach(OWNER, task['task_id'], original, attachment_id='0')
    store.claim(OWNER, task['task_id'])
    config = {'cwd': str(tmp_path), 'task_store': store}
    with identity_scope(user_id=OWNER), task_scope(task['task_id']):
        read = Read(config).execute({'path': entry['path']})
        assert read.status == 'success', read.result
        source = read.result
        if retry:
            store.fail(OWNER, task['task_id'], 1, 'idempotency_conflict', 'decide')
            store.retry(OWNER, task['task_id'], 1)
            store = TaskStore(tmp_path / 'tasks')
            store.claim(OWNER, task['task_id'])
            config['task_store'] = store
        result = KnowledgeIngest(config).execute(dict(
            task_id=task['task_id'], attachment_id='0', purpose='long_term', reason='主动整理',
            document_id=source['document_id'], expected_current_revision=(
                source['revision_id'] if legacy_echo else source['expected_current_revision'])))
    assert result.status == 'success', result.result
    assert result.result['outcome'] == 'published'
    assert result.result['result_refs'][0]['document_id'] == source['document_id']
    assert SourceService(tmp_path).search('antenna')
    assert len(store.get(OWNER, task['task_id'])['attachments']) == 1


@pytest.mark.parametrize('first_explicit', [False, True])
def test_receipt_accepts_only_same_document_echo(tmp_path, first_explicit):
    service = SourceService(tmp_path)
    original = tmp_path / 'source.txt'
    original.write_text('immutable evidence', encoding='utf-8')
    origin = {'channel': 'feishu'}
    seed = service.receive(original, filename='source.txt', request_id='seed', origin=origin)
    receipt = service.receive(original, filename='source.txt', request_id='receipt', origin=origin,
                              document_id=seed['document_id'] if first_explicit else None)
    for document in (None, receipt['document_id']):
        assert service.receive(original, filename='source.txt', request_id='receipt', origin=origin,
                               document_id=document) == receipt
    for overrides in ({'filename': 'other.txt'}, {'origin': {'channel': 'web'}},
                      {'document_id': 'other-document'}):
        arguments = dict(filename='source.txt', request_id='receipt', origin=origin,
                         document_id=receipt['document_id'])
        arguments.update(overrides)
        with pytest.raises(ValueError, match='请求标识已用于不同资料'):
            service.receive(original, **arguments)
    original.write_text('different evidence', encoding='utf-8')
    with pytest.raises(ValueError, match='请求标识已用于不同资料'):
        service.receive(original, filename='source.txt', request_id='receipt', origin=origin)


@pytest.mark.parametrize('action', ['withdraw', 'correct', 'purpose'])
def test_content_revision_echo_cannot_bypass_later_source_state(tmp_path, action):
    original = tmp_path / 'source.txt'
    original.write_text('guarded antenna source', encoding='utf-8')
    store = TaskStore(tmp_path / 'tasks')
    task = store.submit(OWNER, 'guarded', {'text': '分享'}, {'channel': 'web'}, 'cfg')
    attachment = store.attach(OWNER, task['task_id'], original)
    store.claim(OWNER, task['task_id'])
    config = {'cwd': str(tmp_path), 'task_store': store}
    service = SourceService(tmp_path)
    with identity_scope(user_id=OWNER), task_scope(task['task_id']):
        read = Read(config).execute({'path': attachment['path']}).result
        service.change(read['document_id'], action=action, purpose='long_term',
                       expected_state=0, reason='后来决定')
        result = KnowledgeIngest(config).execute(dict(task_id=task['task_id'], purpose='long_term',
            reason='迟到回传', document_id=read['document_id'], expected_current_revision=read['revision_id']))
    assert result.status == 'error'
    assert result.result['code'] == 'version_conflict'
    assert not service.search('antenna')
