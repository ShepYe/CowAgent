"""普通来源会话的派生边界；真实工具和持久化，模型及外网不参与。"""
import asyncio
import copy
import socket
import threading
from types import SimpleNamespace

import pytest

from agent.knowledge import purpose
from agent.knowledge.parser import SourceError
from agent.knowledge.personal_assistant import SourceService, readable
from agent.knowledge.wiki import WikiService
from agent.memory.config import MemoryConfig
from agent.memory.manager import MemoryManager
from agent.tools.edit.edit import Edit
from agent.tools.knowledge_ingest import KnowledgeIngest, resume_publication
from agent.tools.memory.memory_get import MemoryGetTool
from agent.tools.memory.memory_search import MemorySearchTool
from agent.tools.read.read import Read
from agent.tools.write.write import Write
from common.personal_tasks import OWNER, TaskStore, task_scope
from common.runtime_identity import identity_scope
from config import Config, conf, config_scope
from models.config_revision import capture_config_revision, require_config_revision, use_config_revision


@pytest.fixture
def derivation(tmp_path, monkeypatch):
    from common import state_dir

    monkeypatch.setattr(state_dir, 'knowledge_dir', lambda **kw: tmp_path / 'knowledge')
    monkeypatch.setattr(state_dir, 'memory_dir', lambda **kw: tmp_path / 'memory')
    for name, value in (('_sourced_sessions', set()), ('_evidence_tokens', {}), ('_discarded_evidence', {})):
        monkeypatch.setattr(purpose, name, value)
    connect = socket.socket.connect

    def offline_connect(sock, address):
        # Windows asyncio 用回环 socketpair 唤醒；其他连接均拒绝。
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1', 'localhost'):
            raise AssertionError('离线派生验证禁止外网')
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, 'connect', offline_connect)
    with config_scope(Config({'knowledge': True})), task_scope(None):
        source = SourceService(tmp_path)
        original = tmp_path / 'source.txt'
        original.write_text('source_derivation_budget_4719 = 42 dB', encoding='utf-8')
        receipt = source.receive(original, filename=original.name, request_id='source', origin={'channel': 'test'})
        source.change(receipt['document_id'], purpose='long_term', expected_state=0, reason='分享')
        parsed = source.parse(receipt['revision_id'])
        evidence = {k: receipt[k] for k in ('document_id', 'revision_id')}
        evidence.update(parse_id=parsed['parse_id'], ordinal=0)
        wiki = WikiService(tmp_path)
        upstream = dict(summary='说明本页知识的主题及适用范围。', title='预算依据', category='工程', blocks=[dict(
            content='source_derivation_budget_4719 的预算为42 dB', sources=[evidence])])
        ref = wiki.publish(wiki.prepare([upstream])['batch_id'])[0]
        manager = MemoryManager(MemoryConfig(workspace_root=str(tmp_path)), embedding_provider=None)
        store = TaskStore(tmp_path / 'tasks')
        config = dict(cwd=str(tmp_path), workspace_root=str(tmp_path), task_store=store)
        env = SimpleNamespace(root=tmp_path, source=source, evidence=evidence, wiki=wiki,
                              ref=ref, manager=manager, store=store, config=config)
        try:
            yield env
        finally:
            manager.close()


def read_evidence(env, reader='read'):
    if reader == 'read':
        result = Read(env.config).execute({'path': env.ref['path']})
    elif reader == 'get':
        result = MemoryGetTool(env.manager).execute({'path': env.ref['path']})
    else:
        result = MemorySearchTool(env.manager, OWNER).execute({'query': 'source_derivation_budget_4719'})
        assert 'source_reference' in str(result.result)
    assert result.status == 'success', result.result
    return result


def draft(env, **changes):
    page = dict(summary='说明本页知识的主题及适用范围。', title='预算派生结论', category='工程', blocks=[dict(
        content='derived_budget_4720：设计预算为42 dB', sources=[copy.deepcopy(env.evidence)])],
        depends_on=[dict(page_id=env.ref['page_id'], page_version=env.ref['page_version'])])
    page.update(changes)
    return page


@pytest.mark.parametrize('reader', ['read', 'get', 'search'])
def test_verified_read_blocks_untracked_write_edit_and_direct_summary(derivation, reader):
    env = derivation
    target = env.root / 'knowledge/concepts/existing.md'
    target.parent.mkdir(parents=True)
    target.write_text('原有笔记', encoding='utf-8')
    with identity_scope(user_id=OWNER, session_id='source-session'):
        read_evidence(env, reader)
        assert Write(env.config).execute({'path': 'knowledge/concepts/copy.md', 'content': '派生副本'}).status == 'error'
        assert Edit(env.config).execute({'path': str(target), 'oldText': '', 'newText': '派生副本'}).status == 'error'
        assert not env.manager.flush_manager.write_daily_summary('预算为42 dB')
        assert not env.manager.flush_manager.get_today_memory_file().exists()
        assert not (env.root / 'knowledge/concepts/copy.md').exists()
        assert target.read_text(encoding='utf-8') == '原有笔记'
        # 工程输出仍可写；限制针对无出处的长期知识与记忆。
        assert Write(env.config).execute({'path': 'output/result.txt', 'content': '预算为42 dB'}).status == 'success'


def test_summary_excludes_derived_answer_after_tool_messages_are_trimmed(derivation):
    env = derivation
    with identity_scope(user_id=OWNER, session_id='trimmed-session'):
        read_evidence(env)
        messages = [{'role': 'user', 'content': '解释预算'}, {'role': 'assistant', 'content': '预算为42 dB'}]
        assert purpose.durable_messages(messages) == []
        assert env.manager.flush_manager._format_conversation_for_summary(messages) == ''
        assert not env.manager.flush_manager.write_daily_summary('预算为42 dB')


@pytest.mark.parametrize('marker', ['[source_reference] {}', 'knowledge/wiki/工程/topic.md', 'knowledge/managed/source/topic.md'])
def test_persisted_evidence_excludes_background_summary_and_restores_write_guard(derivation, marker):
    env = derivation
    messages = [{'role': 'tool', 'content': marker}, {'role': 'assistant', 'content': '预算为42 dB'}]
    with identity_scope(user_id=OWNER, session_id='restored-session'):
        # 不依赖进程内已读标记，模拟重启后加载的持久消息。
        assert purpose.durable_messages(messages) == []
        agent = SimpleNamespace(workspace_dir=str(env.root), messages=messages, messages_lock=threading.RLock())
        purpose.refresh_evidence_context(agent)
        assert Write(env.config).execute({'path': 'MEMORY.md', 'content': '预算为42 dB'}).status == 'error'
        assert not env.manager.flush_manager.write_daily_summary('预算为42 dB')


def test_other_ordinary_session_remains_writable_and_summarizable(derivation):
    env = derivation
    with identity_scope(user_id=OWNER, session_id='source-session'):
        read_evidence(env)
    with identity_scope(user_id=OWNER, session_id='independent-session'):
        messages = [{'role': 'user', 'content': '喜欢简洁回答'}]
        assert purpose.durable_messages(messages) == messages
        assert Write(env.config).execute({'path': 'knowledge/concepts/ordinary.md', 'content': '普通笔记'}).status == 'success'
        assert env.manager.flush_manager.write_daily_summary('用户喜欢简洁回答')
        assert '用户喜欢简洁回答' in env.manager.flush_manager.get_today_memory_file().read_text(encoding='utf-8')
    with identity_scope(user_id=OWNER, session_id='source-session'):
        assert not env.manager.flush_manager.write_daily_summary('预算为42 dB')


def test_controlled_synthesis_persists_task_and_dependency_then_withdraws(derivation):
    env = derivation
    with identity_scope(user_id=OWNER, session_id='synthesis-session'):
        read_evidence(env, 'search')
        args = dict(purpose='long_term', reason='从预算依据提炼', pages=[draft(env)])
        result = KnowledgeIngest(env.config).execute(args)
        assert result.status == 'success', result.result
        task = result.result
        assert task['status'] == 'succeeded' and not task['attachments']
        assert task['payload']['knowledge_synthesis'] is True
        assert task['config_revision'] == capture_config_revision()
        assert task['origin']['session_id'] == 'synthesis-session'
        ref = task['result_refs'][0]
        assert ref['depends_on'][0]['page_version'] == env.ref['page_version']
        assert env.wiki.search('derived_budget_4720')[0]['publish_id'] == ref['publish_id']
        assert KnowledgeIngest(env.config).execute(args).result['task_id'] == task['task_id']
        assert len(env.store.list()) == 1
        env.source.change(env.evidence['document_id'], action='withdraw', expected_state=1, reason='撤回依据')
        assert env.wiki.current(ref['page_id']) is None
        assert not env.wiki.search('derived_budget_4720')
        assert not readable(env.root / ref['path'])
        asyncio.run(env.manager.sync(force=True))
        assert not asyncio.run(env.manager.search('derived_budget_4720', min_score=0))
        assert KnowledgeIngest(env.config).execute(args).status == 'error'


def test_synthesis_dependency_is_invalidated_by_upstream_page_update(derivation):
    env = derivation
    with identity_scope(user_id=OWNER, session_id='dependency-session'):
        result = KnowledgeIngest(env.config).execute(dict(purpose='long_term', reason='派生', pages=[draft(env)]))
        assert result.status == 'success', result.result
        derived = result.result['result_refs'][0]
        update = draft(env, page_id=env.ref['page_id'], expected_version=env.ref['page_version'], depends_on=[])
        env.wiki.publish(env.wiki.prepare([update])['batch_id'])
        assert env.wiki.current(derived['page_id']) is None


def test_synthesis_index_failure_recovers_saved_batch_without_model(derivation, monkeypatch):
    env = derivation
    with identity_scope(user_id=OWNER, session_id='recovery-session'):
        with monkeypatch.context() as patch:
            patch.setattr(WikiService, '_verify_index', staticmethod(lambda *a: (_ for _ in ()).throw(SourceError('index_failed', '离线故障'))))
            result = KnowledgeIngest(env.config).execute(dict(purpose='long_term', reason='派生', pages=[draft(env)]))
        assert result.status == 'error'
        failed = env.store.list()[0]
        assert failed['status'] == 'failed' and failed['wiki_batch_id']
        env.store.retry(OWNER, failed['task_id'], failed['attempt'])
        recovered = env.store.claim(OWNER, failed['task_id'])
        require_config_revision(recovered['config_revision'])
        complete = resume_publication(recovered, env.store)
        assert complete['status'] == 'succeeded'
        assert complete['wiki_batch_id'] == failed['wiki_batch_id']
        assert env.wiki.current(complete['result_refs'][0]['page_id'])


@pytest.mark.parametrize('invalid', ['transient', 'withdrawn', 'unknown_parse'])
def test_synthesis_rejects_unusable_registered_evidence(derivation, invalid):
    env = derivation
    page = draft(env)
    if invalid == 'unknown_parse':
        page['blocks'][0]['sources'][0]['parse_id'] = '0' * 64
    else:
        env.source.change(env.evidence['document_id'], expected_state=1, reason='来源失效',
                          **({'purpose': 'transient'} if invalid == 'transient' else {'action': 'withdraw'}))
    with identity_scope(user_id=OWNER, session_id='invalid-session'):
        assert KnowledgeIngest(env.config).execute(dict(purpose='long_term', reason='派生', pages=[page])).status == 'error'
    assert not env.wiki.search('derived_budget_4720')


@pytest.mark.parametrize('task_id', [None, 'another-task'])
def test_attachment_scope_cannot_be_bypassed_with_synthesis(derivation, task_id):
    env = derivation
    with identity_scope(user_id=OWNER, session_id='attachment-session'), task_scope('current-attachment-task'):
        args = dict(purpose='long_term', reason='派生', pages=[draft(env)])
        if task_id is not None:
            args['task_id'] = task_id
        result = KnowledgeIngest(env.config).execute(args)
        assert result.status == 'error' and result.result == 'forbidden'
    assert not env.store.list()


def test_same_synthesis_in_another_session_does_not_conflict(derivation):
    env = derivation
    args = dict(purpose='long_term', reason='从相同证据提炼', pages=[draft(env)])
    with identity_scope(user_id=OWNER, session_id='first-session'):
        assert KnowledgeIngest(env.config).execute(args).status == 'success'
    with identity_scope(user_id=OWNER, session_id='second-session'):
        result = KnowledgeIngest(env.config).execute(args)
        assert result.status == 'success', result.result


def test_sessionless_read_does_not_mark_other_ordinary_sessions(derivation):
    env = derivation
    with identity_scope(user_id=OWNER, session_id=None):
        read_evidence(env)
        assert not purpose.source_derived_context()
    with identity_scope(user_id=OWNER, session_id='later-independent-session'):
        assert not purpose.source_derived_context()
        assert env.manager.flush_manager.write_daily_summary('普通会话笔记')


def test_synthesis_recovery_keeps_verified_revision_after_runtime_normalization(derivation, monkeypatch):
    env = derivation
    revision = capture_config_revision()
    with identity_scope(user_id=OWNER, session_id='pinned-synthesis-session'):
        with use_config_revision(revision):
            # 只模拟已知的运行时补齐现象，不假定真实 mutator 或具体键。
            conf()['test_runtime_normalization'] = True
            with monkeypatch.context() as patch:
                patch.setattr(WikiService, '_verify_index', staticmethod(lambda *a: (_ for _ in ()).throw(SourceError('index_failed', '离线故障'))))
                result = KnowledgeIngest(env.config).execute(dict(purpose='long_term', reason='派生', pages=[draft(env)]))
            assert result.status == 'error'
        failed = env.store.list()[0]
        assert failed['error']['code'] == 'index_failed'
        # 离开模型运行时作用域后仍应能以已核验的配置修订恢复。
        require_config_revision(failed['config_revision'])
