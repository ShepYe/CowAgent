"""原话身份、用途元数据及真人入口的持久化回归。"""
import copy
import json
import sqlite3

import pytest

from agent.memory.conversation_store import ConversationStore
from agent.memory.message_trace import ensure_trace
from bridge.agent_bridge import AgentBridge
from bridge.context import Context, ContextType


def test_trace_round_trip_without_task_and_content_rewrite(tmp_path):
    store = ConversationStore(tmp_path / 'history.db', agent_id='agent-a')
    message = {'role': 'user', 'content': [{'type': 'text', 'text': '回答简洁些 😀'}],
               'purpose': 'transient', 'archive_only': True,
               'extras': {'custom': {'x': 1}, 'memory_trace': {'origin': 'human', 'decision_id': 'd1'}}}
    original = copy.deepcopy(message['content'])
    store.append_messages('s1', [message])
    restored = ConversationStore(store._db_path, agent_id='agent-a').load_messages('s1')[0]
    assert restored['message_id'] == message['message_id']
    assert restored['extras'] == message['extras']
    assert restored['purpose'] == 'transient'
    assert restored['archive_only'] is True
    assert restored['content'] == original
    assert restored['extras']['memory_trace']['decision_id'] == 'd1'


def test_legacy_identity_persists_and_never_assumes_human(tmp_path):
    store = ConversationStore(tmp_path / 'history.db')
    store.append_messages('s1', [{'role': 'user', 'content': '旧原话'}])
    with sqlite3.connect(store._db_path) as db:
        db.execute("UPDATE messages SET extras = ?", (json.dumps({'purpose': 'transient', 'custom': 1}),))
    first = store.load_messages('s1')[0]
    second = ConversationStore(store._db_path).load_messages('s1')[0]
    assert first == second
    assert first['message_id']
    assert first['extras']['memory_trace']['origin'] == 'internal'
    assert first['purpose'] == 'transient'
    assert first['content'] == '旧原话'


@pytest.mark.parametrize('role,content,origin', [
    ('user', [{'type': 'tool_result', 'tool_use_id': 't1', 'content': '用户喜欢长回答'}], 'tool'),
    ('user', '模拟用户偏好', 'internal'),
    ('assistant', '用户喜欢长回答', 'assistant'),
])
def test_wrapped_messages_not_human(tmp_path, role, content, origin):
    store = ConversationStore(tmp_path / 'history.db')
    message = {'role': role, 'content': content}
    store.append_messages('s1', [message])
    assert store.load_messages('s1')[0]['extras']['memory_trace']['origin'] == origin


def test_helper_keeps_trace_and_rejects_conflicting_ids():
    message = {'role': 'user', 'content': '原文', 'extras': {
        'memory_trace': {'schema_version': 99, 'message_id': 'old', 'origin': 'replay'}}}
    trace = ensure_trace(message, workspace_id='scope-id', agent_id='a', user_id='u', session_id='s', origin='human')
    assert trace['origin'] == 'replay'
    assert trace['schema_version'] == 99
    assert message['message_id'] == message['extras']['message_id'] == 'old'
    message['message_id'] = 'other'
    with pytest.raises(ValueError):
        ensure_trace(message)


@pytest.mark.parametrize('flags,origin', [({}, 'human'), ({'is_delegated_task': True}, 'internal'),
    ({'is_replay': True}, 'internal'), ({'is_scheduled_task': True}, 'internal')])
def test_bridge_pre_persists_verbatim_identity(tmp_path, flags, origin):
    store = ConversationStore(tmp_path / 'history.db')
    bridge = object.__new__(AgentBridge)
    bridge.get_conversation_store = lambda agent_id=None: store
    context = Context(ContextType.TEXT, '原话', {'session_id': 's1', **flags})
    message = bridge._memory_input_message('原话', 's1', context, '')
    assert message['extras']['memory_trace']['origin'] == origin
    if flags.get('is_scheduled_task'):
        assert bridge._pre_persist_user_message('s1', '原话', context, False, message=message) is False
        return
    assert bridge._pre_persist_user_message('s1', '原话', context, False, message=message)
    restored = store.load_messages('s1')[0]
    assert restored['message_id'] == message['message_id']
    assert restored['content'] == [{'type': 'text', 'text': '原话'}]
    assert restored['extras']['memory_trace']['origin'] == origin


def test_ledger_capture_inherits_persisted_human_and_scope(tmp_path):
    from agent.memory.ledger import MemoryLedger
    store = ConversationStore(tmp_path / 'history.db')
    bridge = object.__new__(AgentBridge)
    bridge.get_conversation_store = lambda agent_id=None: store
    context = Context(ContextType.TEXT, '回答简洁些', {'session_id': 's1'})
    message = bridge._memory_input_message('回答简洁些', 's1', context, '')
    assert bridge._pre_persist_user_message('s1', '回答简洁些', context, False, message=message)
    ledger = MemoryLedger(tmp_path, store=store)
    ledger.capture([message], session_id='s1')
    trace = message['extras']['memory_trace']
    assert trace['origin'] == 'human'
    assert trace['workspace_id'] == ledger.workspace_id
    restored = store.load_messages('s1')[0]
    ledger.capture([restored], session_id='s1')
    assert restored['extras']['memory_trace'] == trace
    assert restored['message_id'] == message['message_id']


def test_bridge_hands_pre_persisted_identity_to_executor(bridge_run):  # noqa: F811 -- pytest 夹具
    from test_incremental_persist import _Script, _STEPS

    class InspectInput(_Script):
        def play(self, executor, query, on_event):
            original = executor._memory_input_message
            stored = bridge_run.store.load_messages('s1')[0]
            assert original['message_id'] == stored['message_id']
            assert original['extras']['memory_trace']['origin'] == 'human'
            return super().play(executor, query, on_event)

    reply, _ = bridge_run(InspectInput(_STEPS))
    assert reply.type.name == 'TEXT'


# 复用真实桥接入口夹具，仅替代模型及工具执行。
from test_incremental_persist import bridge_run  # noqa: F401, F811 -- pytest 按夹具名称注入
