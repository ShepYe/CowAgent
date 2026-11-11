from pathlib import Path
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
        assert result.result['status'] == 'running' and result.result['organization_required']
        assert not SourceService(tmp_path).search('antenna')
        material = result.result['source_materials'][0]
        assert material['parse_id']
        assert material['document_id'] == source['document_id']
        assert material['revision_id'] == source['revision_id']
        result = KnowledgeIngest(config).execute(dict(task_id=task['task_id'], purpose='long_term',
            reason='完成主题整理', pages=[dict(title='天线链路预算', category='射频/预算',
                blocks=[dict(content='天线预算 antenna 为 42 dB。', sources=[dict(
                    document_id=material['document_id'], revision_id=material['revision_id'],
                    parse_id=material['parse_id'], ordinal=0)])], related_page_ids=[], depends_on=[])]))
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



def topic_pages(material):
    return [dict(title='天线预算结论', category='射频/预算', blocks=[dict(
        content='天线链路 antenna budget 为42 dB。', sources=[dict(
            document_id=material['document_id'], revision_id=material['revision_id'],
            parse_id=material['parse_id'], ordinal=0)])], related_page_ids=[], depends_on=[])]


@pytest.mark.parametrize('explicit_null', [False, True])
def test_reorganize_defaults_expected_revision_after_trusted_state_check(tmp_path, explicit_null):
    from agent.knowledge.wiki import WikiService
    service = SourceService(tmp_path)
    original = tmp_path / 'source.txt'
    original.write_text('antenna budget 42 dB', encoding='utf-8')
    source = service.receive(original, filename=original.name, request_id='existing', origin={'channel': 'web'})
    service.change(source['document_id'], purpose='long_term', expected_state=0, reason='已有长期资料')
    old = service.publish(service.prepare(source['revision_id'])['candidate_id'])
    before = service.get(source['document_id'])
    assert before['current'] == source['revision_id']
    store = TaskStore(tmp_path / 'tasks')
    directive = dict(document_id=source['document_id'], revision_id=source['revision_id'], expected_state=before['state'])
    task = store.submit(OWNER, 'reorganize', {'text': '重新整理已登记资料', 'reorganize': directive},
                        {'channel': 'web'}, 'cfg')
    attachment = store.attach(OWNER, task['task_id'], original)
    store.claim(OWNER, task['task_id'])
    config = {'cwd': str(tmp_path), 'task_store': store}
    with identity_scope(user_id=OWNER), task_scope(task['task_id']):
        read = Read(config).execute({'path': attachment['path']})
        assert read.status == 'success', read.result
        assert read.result['expected_current_revision'] == before['current']
        args = dict(task_id=task['task_id'], purpose='long_term', reason='依据已验证原件重整')
        if explicit_null:
            args['expected_current_revision'] = None
        tool = KnowledgeIngest(config)
        registered = tool.execute(args)
        assert registered.status == 'success', registered.result
        assert registered.result['organization_required'] and registered.result['status'] == 'running'
        decision = next(iter(registered.result['attachment_decisions'].values()))
        assert decision['expected_current_revision'] == before['current']
        assert decision['source']['document_id'] == source['document_id']
        assert service.read_reference(old['publish_id'], 0)['valid']
        finished = tool.execute(dict(args, pages=topic_pages(registered.result['source_materials'][0])))
    assert finished.status == 'success', finished.result
    assert finished.result['status'] == 'succeeded' and finished.result['attempt'] == 1
    ref = finished.result['result_refs'][0]
    assert ref['document_id'] == source['document_id'] and ref['revision_id'] == source['revision_id']
    assert WikiService(tmp_path).read_reference(ref['publish_id'])['valid']
    assert service.search('antenna')


@pytest.mark.parametrize('code', ['invalid_request', 'version_conflict', 'forbidden'])
def test_decide_validation_error_keeps_running_and_accepts_correction(tmp_path, code):
    original = tmp_path / 'source.txt'
    original.write_text('antenna budget 42 dB', encoding='utf-8')
    store = TaskStore(tmp_path / 'tasks')
    task = store.submit(OWNER, 'correct-arguments', {'text': '分享资料'}, {'channel': 'web'}, 'cfg')
    attachment = store.attach(OWNER, task['task_id'], original)
    store.claim(OWNER, task['task_id'])
    config = {'cwd': str(tmp_path), 'task_store': store}
    service = SourceService(tmp_path)
    with identity_scope(user_id=OWNER), task_scope(task['task_id']):
        read = Read(config).execute({'path': attachment['path']})
        assert read.status == 'success', read.result
        before = service.get(read.result['document_id'])
        args = dict(task_id=task['task_id'], purpose='long_term', reason='主动整理',
                    document_id=read.result['document_id'],
                    expected_current_revision=read.result['expected_current_revision'])
        invalid = dict(args)
        if code == 'invalid_request':
            invalid['purpose'] = 'unsupported-purpose'
        elif code == 'forbidden':
            invalid['attachment_id'] = 'unregistered-attachment'
        else:
            invalid['expected_current_revision'] = 'stale-revision'
        tool = KnowledgeIngest(config)
        rejected = tool.execute(invalid)
        assert rejected.status == 'error' and rejected.result['code'] == code
        assert rejected.result['stage'] == 'decide'
        pending = store.get(OWNER, task['task_id'])
        assert pending['status'] == 'running' and pending['attempt'] == 1
        assert pending['error'] is None and not pending['result_refs']
        assert service.get(read.result['document_id']) == before
        assert not service.search('antenna')
        registered = tool.execute(args)
        assert registered.status == 'success' and registered.result['organization_required']
        finished = tool.execute(dict(args, pages=topic_pages(registered.result['source_materials'][0])))
    assert finished.status == 'success', finished.result
    assert finished.result['status'] == 'succeeded' and finished.result['attempt'] == 1
    assert service.search('antenna')


@pytest.mark.parametrize('stage,code', [('parse', 'parse_failed'), ('pending_index', 'index_failed')])
def test_processing_failure_still_fails_task(tmp_path, monkeypatch, stage, code):
    from agent.knowledge.parser import SourceError
    from agent.knowledge.wiki import WikiService
    original = tmp_path / 'source.txt'
    original.write_text('antenna budget 42 dB', encoding='utf-8')
    store = TaskStore(tmp_path / 'tasks')
    task = store.submit(OWNER, 'processing-failure', {'text': '分享资料'}, {'channel': 'web'}, 'cfg')
    attachment = store.attach(OWNER, task['task_id'], original)
    store.claim(OWNER, task['task_id'])
    tool = KnowledgeIngest({'cwd': str(tmp_path), 'task_store': store})
    def fail(*args, **kwargs):
        raise SourceError(code, '确定性故障注入')
    with identity_scope(user_id=OWNER), task_scope(task['task_id']):
        args = dict(task_id=task['task_id'], purpose='long_term', reason='主动整理')
        if stage == 'parse':
            monkeypatch.setattr(SourceService, 'parse', fail)
        else:
            registered = tool.execute(args)
            assert registered.status == 'success' and registered.result['organization_required']
            args['pages'] = topic_pages(registered.result['source_materials'][0])
            monkeypatch.setattr(WikiService, '_verify_index', fail)
        result = tool.execute(args)
    assert result.status == 'error' and result.result['code'] == code
    assert result.result['stage'] == stage
    after = store.get(OWNER, task['task_id'])
    assert after['status'] == 'failed' and after['attempt'] == 1
    assert after['error']['code'] == code and after['error']['stage'] == stage
    assert after['error']['retryable'] and not after['result_refs']
    assert Path(attachment['path']).read_bytes() == original.read_bytes()
    assert not SourceService(tmp_path).search('antenna')
    if stage == 'pending_index':
        assert after['wiki_batch_id'] and after['wiki_workspace'] == str(tmp_path)
