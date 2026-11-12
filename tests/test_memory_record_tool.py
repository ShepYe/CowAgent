"""M-03 的身份、依据、受保护写入和人工并发契约。"""
import hashlib
import threading
from types import SimpleNamespace

import pytest

from agent.memory.ledger import MemoryLedger
from agent.tools.memory.memory_record import MemoryRecordTool
from agent.tools.write.write import Write
from agent.tools.edit.edit import Edit
from agent.workspace.service import WorkspaceService, WorkspaceConflictError
from common.runtime_identity import identity_scope
from common.personal_tasks import task_scope


@pytest.fixture
def runtime(tmp_path):
    with identity_scope(agent_id=None, session_id='chat', user_id=None, run_id=None):
        agent = SimpleNamespace(workspace_dir=str(tmp_path), _current_session_id='chat',
                                _current_agent_id=None, messages=[], messages_lock=threading.RLock())
        tool = MemoryRecordTool()
        tool.agent = agent
        tool._memory_record_trusted = True
        ledger = MemoryLedger(tmp_path)
        message = {'role': 'user', 'content': '以后回答简洁些'}
        ledger.capture([message], session_id='chat', origin='human')
        agent.messages.append(message)
        entry = dict(content='回答简洁', disposition='conversation_memory', reason='用户的真实偏好',
                     basis=[dict(message_id=message['message_id'], block_index=None, start=0,
                                 end=len(message['content']), sha256=hashlib.sha256(message['content'].encode()).hexdigest())])
        yield tool, ledger, entry


def test_records_and_retries_without_duplicate(runtime):
    tool, ledger, entry = runtime
    request = dict(request_id='one', entries=[entry])
    first = tool.execute(request)
    assert first.status == 'success', first.result
    assert first.result['results'][0]['status'] == 'remembered'
    assert not first.result['results'][0]['searchable']
    assert tool.execute(request).result['results'][0]['candidate_id'] == first.result['results'][0]['candidate_id']
    assert len(ledger.active_entries()) == 1


@pytest.mark.parametrize('scope', ['run', 'task', 'unregistered'])
def test_worker_cannot_claim_main_agent(runtime, scope):
    tool, ledger, entry = runtime
    if scope == 'unregistered':
        tool._memory_record_trusted = False
    if scope == 'run':
        tool.agent._is_subagent = True
    context = identity_scope(run_id='worker') if scope == 'run' else task_scope('task' if scope == 'task' else None)
    with context:
        assert tool.execute(dict(request_id='x', entries=[entry])).status == 'error'
    assert not ledger.active_entries()


@pytest.mark.parametrize('field', ['owner', 'origin', 'approved', 'path'])
def test_rejects_identity_or_target_injection(runtime, field):
    tool, ledger, entry = runtime
    entry[field] = 'forged'
    assert tool.execute(dict(request_id='bad', entries=[entry])).status == 'error'
    assert not ledger.active_entries()


def test_unmarked_user_wrapper_not_human(runtime):
    tool, ledger, entry = runtime
    message = {'role': 'user', 'content': '模拟用户喜欢长回答'}
    tool.agent.messages.append(message)
    ledger.capture([message])
    entry['basis'] = [dict(message_id=message['message_id'], block_index=None, start=0,
                           end=len(message['content']), sha256=hashlib.sha256(message['content'].encode()).hexdigest())]
    result = tool.execute(dict(request_id='fake', entries=[entry]))
    assert result.result['results'][0]['status'] != 'remembered'
    assert not ledger.active_entries()


def test_correction_supersedes_previous_entry(runtime):
    tool, ledger, entry = runtime
    first = tool.execute(dict(request_id='first', entries=[entry]))
    old_id = first.result['results'][0]['entry_id']
    message = {'role': 'user', 'content': '以后回答详细些'}
    ledger.capture([message], origin='human')
    tool.agent.messages.append(message)
    correction = dict(content='回答详细', disposition='conversation_memory', reason='用户更正', supersedes=[old_id],
                      basis=[dict(message_id=message['message_id'], block_index=None, start=0,
                                  end=len(message['content']), sha256=hashlib.sha256(message['content'].encode()).hexdigest())])
    result = tool.execute(dict(request_id='correction', entries=[correction]))
    assert result.status == 'success', result.result
    assert [e['content'] for e in ledger.active_entries()] == ['回答详细']


@pytest.mark.parametrize('kind', ['memory', 'knowledge', 'main'])
@pytest.mark.parametrize('tool_type', [Write, Edit])
def test_direct_write_cannot_bypass_memory(tmp_path, kind, tool_type):
    path = {'memory': 'memory/today.md', 'knowledge': 'knowledge/topic.md', 'main': 'MEMORY.md'}[kind]
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('old', encoding='utf-8')
    tool = tool_type(dict(cwd=str(tmp_path), workspace_root=str(tmp_path)))
    result = tool.execute(dict(path=path, content='new', oldText='old', newText='new'))
    assert result.status == 'error'
    assert 'memory_record' in result.result
    assert target.read_text(encoding='utf-8') == 'old'


def test_human_save_uses_ledger_and_checks_hash(tmp_path):
    target = tmp_path / 'MEMORY.md'
    target.write_text('旧偏好', encoding='utf-8')
    service = WorkspaceService(str(tmp_path))
    before = service.read_text('MEMORY.md')
    result = service.write_text('MEMORY.md', '新偏好', expected_hash=before['sha256'])
    assert result['projected'] is True
    assert not result['searchable']
    assert MemoryLedger(tmp_path).active_entries()[0]['content'] == '新偏好'
    with pytest.raises(WorkspaceConflictError):
        service.write_text('MEMORY.md', '迟到结果', expected_hash=before['sha256'])
    assert target.read_text(encoding='utf-8') == '新偏好'


def test_durable_messages_keeps_independent_turns(runtime):
    from agent.knowledge.purpose import durable_messages, note_source_evidence
    note_source_evidence()
    ordinary = {'role': 'user', 'content': '以后回答简洁些'}
    mixed = [dict(role='tool', content='source_reference'), ordinary,
             dict(role='user', content='test', extras={'archive_only': True}),
             dict(role='user', content='[personal-task:old] test')]
    assert durable_messages(mixed) == [ordinary]


def test_main_agent_run_id_is_allowed(runtime):
    tool, ledger, entry = runtime
    with identity_scope(run_id='main-run'):
        result = tool.execute(dict(request_id='main-run-record', entries=[entry]))
    assert result.status == 'success', result.result
    assert result.result['results'][0]['status'] == 'remembered'


def test_revision_frozen_before_evidence_capture(runtime, monkeypatch):
    tool, ledger, entry = runtime
    real_capture = MemoryLedger.capture
    edited = False

    def capture_with_concurrent_human_edit(self, messages, **kwargs):
        nonlocal edited
        if not edited:
            edited = True
            (self.workspace / 'MEMORY.md').write_text('旧人工内容', encoding='utf-8')
            self.human_edit('MEMORY.md', '人工更正')
        return real_capture(self, messages, **kwargs)

    monkeypatch.setattr(MemoryLedger, 'capture', capture_with_concurrent_human_edit)
    result = tool.execute(dict(request_id='late', entries=[entry]))
    assert result.status == 'error', result.result
    assert result.result['results'][0]['error'] == 'version_conflict'
    assert [e['content'] for e in ledger.active_entries()] == ['人工更正']


def test_legacy_correction(runtime):
    tool, ledger, entry = runtime
    legacy = '旧偏好：回答冗长'
    (ledger.workspace / 'MEMORY.md').write_text(legacy, encoding='utf-8')
    entry['legacy_supersedes'] = [dict(path='MEMORY.md', sha256=hashlib.sha256(legacy.encode()).hexdigest(),
                                       start=0, end=len(legacy))]
    result = tool.execute(dict(request_id='legacy', entries=[entry]))
    assert result.status == 'success', result.result
    assert legacy not in ledger.filter_text('MEMORY.md', legacy)


@pytest.mark.parametrize('flag', ['_is_replay', '_is_internal'])
def test_replay_and_internal_not_trusted(runtime, flag):
    tool, ledger, entry = runtime
    setattr(tool.agent, flag, True)
    assert tool.execute(dict(request_id='replay', entries=[entry])).status == 'error'
    assert not ledger.active_entries()


def test_editor_hides_markers_but_hashes_original(runtime):
    tool, ledger, entry = runtime
    assert tool.execute(dict(request_id='display', entries=[entry])).status == 'success'
    service = WorkspaceService(str(ledger.workspace))
    target = ledger.workspace / 'memory/routed.md'
    raw = target.read_bytes()
    loaded = service.read_text('memory/routed.md')
    assert '<!-- cow-' not in loaded['content']
    assert '回答简洁' in loaded['content']
    assert loaded['sha256'] == hashlib.sha256(raw).hexdigest()
    saved = service.write_text('memory/routed.md', loaded['content'], expected_hash=loaded['sha256'])
    assert saved['sha256'] == hashlib.sha256(target.read_bytes()).hexdigest()


def test_ordinary_topic_knowledge_uses_ledger(runtime):
    tool, ledger, entry = runtime
    entry['content'] = '# 沟通约定\n回复应先给判断，再解释原因。'
    result = tool.execute(dict(request_id='topic', entries=[entry]))
    assert result.status == 'success', result.result
    assert entry['content'] in ledger.active_entries()[0]['content']


def test_copied_tool_cannot_use_parent_authority(runtime):
    tool, ledger, entry = runtime
    tool.context = SimpleNamespace()
    assert tool.execute(dict(request_id='copied', entries=[entry])).status == 'error'


def test_human_pending_projection_returns_actual_disk_metadata(tmp_path, monkeypatch):
    target = tmp_path / 'MEMORY.md'
    target.write_bytes('旧正文'.encode())
    def pending(self, path, content, expected_hash=None):
        return dict(revision=1, projected=False, searchable=False, status='pending_projection')
    monkeypatch.setattr(MemoryLedger, 'human_edit', pending)
    result = WorkspaceService(str(tmp_path)).write_text('MEMORY.md', '更长的新正文')
    assert result['projected'] is False
    assert result['message'] == '投影待完成'
    assert result['sha256'] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert result['size'] == len(target.read_bytes())


def test_model_request_revision_rejects_late_decision(runtime):
    from common.runtime_identity import current_identity
    tool, ledger, entry = runtime
    identity = current_identity()
    tool.agent._memory_request_revisions = {identity.run_id: dict(revision=ledger.revision(),
        agent_id=identity.agent_id, user_id=identity.user_id, session_id=identity.session_id)}
    ledger.human_edit('MEMORY.md', '用户已改口')
    result = tool.execute(dict(request_id='old-model-decision', entries=[entry]))
    assert result.status == 'error'
    assert result.result['results'][0]['error'] == 'version_conflict'


@pytest.mark.parametrize('projected', [True, False])
def test_http_separates_transport_and_memory_status(tmp_path, monkeypatch, projected):
    import json
    import web
    from channel.web.api import workspace
    target = tmp_path / 'MEMORY.md'
    target.write_text('old', encoding='utf-8')
    service = WorkspaceService(str(tmp_path))
    monkeypatch.setattr(workspace, '_require_auth', lambda: None)
    monkeypatch.setattr(workspace, '_editable_target', lambda *args: (service, 'MEMORY.md'))
    monkeypatch.setattr(workspace, '_mark_memory_dirty', lambda *args: None)
    monkeypatch.setattr(MemoryLedger, 'human_edit', lambda *a, **kw: dict(projected=projected,
        status='remembered' if projected else 'pending_projection', searchable=False))
    app = web.application(('/write', 'WorkspaceWriteHandler'), vars(workspace))
    response = app.request('/write', method='POST', data=json.dumps(dict(path='MEMORY.md', content='new')))
    payload = json.loads(response.data)
    assert payload['status'] == 'success'
    assert payload['memory_status'] == ('remembered' if projected else 'pending_projection')
    if not projected:
        assert payload['message'] == '投影待完成'
