"""M-04：候选先存、实际主 Agent 工具调用、修订屏障与全文读取。"""
import asyncio
import copy
import json

import pytest

from agent.memory.config import MemoryConfig
from agent.memory.ledger import MemoryLedger
from agent.memory.manager import MemoryManager
from agent.memory.summarizer import MemoryFlushManager
from agent.protocol.agent import Agent
from agent.protocol.agent_stream import AgentStreamExecutor
from common.runtime_identity import identity_scope


def basis(evidence):
    return {key: evidence[key] for key in ("message_id", "block_index", "start", "end", "sha256")}


@pytest.fixture
def routed(tmp_path):
    with identity_scope(agent_id=None, user_id=None, session_id="summary-chat", run_id="summary-run"):
        ledger = MemoryLedger(tmp_path)
        messages = [{"role": "user", "content": "以后多用表格，这次测试42 dB不要记"}]
        ledger.capture(messages, origin="human")
        entry = dict(content="多用表格", disposition="conversation_memory", reason="真实偏好",
                     basis=[dict(basis(ledger.evidence(messages)[0]), end=6)])
        yield ledger, messages, entry


class SummaryModel:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def call(self, request):
        self.calls += 1
        assert "evidence" in request.messages[0]["content"]
        value = self.response(request) if callable(self.response) else self.response
        if isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False)
        return {"content": [{"type": "text", "text": value}]}


def test_summary_keeps_context_but_only_proposes_untrusted_candidates(routed):
    ledger, messages, entry = routed
    manager = MemoryFlushManager(ledger.workspace, SummaryModel(dict(context_summary="测试42 dB，偏好表格", entries=[entry])))
    summaries = []
    assert manager.flush_from_messages(messages, context_summary_callback=summaries.append)
    assert summaries == ["测试42 dB，偏好表格"]
    assert not ledger.active_entries()
    pending = ledger.pending_candidates()
    assert pending[0]["candidate"]["content"] == entry["content"]
    assert pending[0]["evidence"][0]["message_id"] == messages[0]["message_id"]
    assert not manager.get_today_memory_file().exists()
    agent = Agent("", workspace_dir=str(ledger.workspace), enable_skills=False)
    prompt = agent.memory_request_prompt([])
    assert pending[0]["candidate"]["candidate_id"] in prompt
    assert messages[0]["content"] in prompt


def test_summary_reuses_main_decision_and_does_not_write_arbitrary_text(routed):
    ledger, messages, entry = routed
    first = ledger.record("main", [entry], trusted=True)["results"][0]
    manager = MemoryFlushManager(ledger.workspace, SummaryModel(dict(context_summary="临时42 dB", entries=[entry])))
    assert manager.flush_from_messages(messages)
    assert manager.last_receipt["results"][0]["entry_id"] == first["entry_id"]
    assert len(ledger.active_entries()) == 1
    assert not manager.write_daily_summary("资料42 dB")
    assert not manager.get_today_memory_file().exists()


@pytest.mark.parametrize("response", [None, "plain summary"])
def test_unavailable_or_plain_model_only_preserves_context(routed, response):
    ledger, messages, _ = routed
    manager = MemoryFlushManager(ledger.workspace, SummaryModel(response) if response else None)
    summaries = []
    assert manager.flush_from_messages(messages, context_summary_callback=summaries.append)
    assert summaries
    assert not ledger.active_entries()
    assert not manager.get_today_memory_file().exists()


def test_empty_entries_summary_cannot_cross_concurrent_correction(routed):
    ledger, messages, _ = routed
    def concurrent_edit(request):
        ledger.human_edit("MEMORY.md", "新决定")
        return dict(context_summary="旧摘要", entries=[])
    manager = MemoryFlushManager(ledger.workspace, SummaryModel(concurrent_edit))
    summaries = []
    assert not manager.flush_from_messages(messages, context_summary_callback=summaries.append)
    assert not summaries


def test_callback_drops_history_switch(routed):
    ledger, messages, _ = routed
    agent = Agent("", workspace_dir=str(ledger.workspace), enable_skills=False)
    executor = AgentStreamExecutor(agent, None, "", [], messages=copy.deepcopy(messages))
    turns = [{"messages": executor.messages}]
    # Callback requires Claude-format text blocks.
    executor.messages[0]["content"] = [{"type": "text", "text": "still here"}]
    ready = executor._build_context_summary_callback([{}], turns)
    agent.clear_history()
    ready("late")
    assert executor.messages[0]["content"][0]["text"] == "still here"


def test_user_projection_is_scoped_and_stale_chunks_do_not_recall(tmp_path):
    manager = MemoryManager(MemoryConfig(workspace_root=str(tmp_path)))
    try:
        with identity_scope(session_id="a", user_id="alice"):
            ledger = MemoryLedger(tmp_path)
            msg = {"role": "user", "content": "ALICEQUARTZ use tables"}
            ledger.capture([msg], origin="human")
            entry = dict(content=msg["content"], disposition="conversation_memory", reason="preference",
                         basis=[basis(ledger.evidence([msg])[0])])
            receipt = ledger.record("alice", [entry], trusted=True)
            # Bob's sync still assigns Alice's projection its registered owner.
        with identity_scope(session_id="b", user_id="bob"):
            asyncio.run(manager.sync())
            assert not asyncio.run(manager.search("ALICEQUARTZ", user_id="bob", min_score=0))
        with identity_scope(session_id="a", user_id="alice"):
            assert asyncio.run(manager.search("ALICEQUARTZ", user_id="alice", min_score=0))
            ledger.exclude(receipt["results"][0]["entry_id"])
            assert not asyncio.run(manager.search("ALICEQUARTZ", user_id="alice", min_score=0))
    finally:
        manager.close()


class StreamModel:
    model = "fake-memory-stream"
    def __init__(self, original_id):
        self.original_id = original_id
        self.requests = []

    def call_stream(self, request):
        self.requests.append(request)
        data = json.loads(request.system.rsplit("\n", 1)[-1])
        original = next(e for e in data["evidence"] if e["message_id"] == self.original_id)
        assert original["origin"] == "human"
        assert original["text"] == "以后回答简洁些，也多用表格"
        assert any(t.get("function", t)["name"] == "memory_record" for t in request.tools)
        if len(self.requests) == 1:
            for i, (start, end, content) in enumerate([(0, 7, "简洁回答"), (8, len(original["text"]), "多用表格")]):
                entry = dict(content=content, disposition="conversation_memory", reason="用户偏好",
                             basis=[dict(basis(original), start=start, end=end)])
                args = dict(request_id=f"stream-{i}", entries=[entry])
                yield {"choices": [{"delta": {"tool_calls": [{"index": i, "id": f"call-{i}",
                       "type": "function", "function": {"name": "memory_record", "arguments": json.dumps(args)}}]}}]}
            yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
        else:
            yield {"choices": [{"delta": {"content": "已记住"}, "finish_reason": "stop"}]}


def test_actual_agent_stream_uses_current_evidence_and_registered_trusted_tool(tmp_path, monkeypatch):
    from agent.tools.tool_manager import ToolManager
    monkeypatch.setattr("common.state_dir.real_state_root", lambda: str(tmp_path))
    with identity_scope(agent_id=None, session_id="stream-chat", user_id=None, run_id="stream-run"):
        registry = ToolManager()
        assert registry._load_tools_from_init()
        tool = registry.create_tool("memory_record")
        ledger = MemoryLedger(tmp_path)
        original = {"role": "user", "content": [{"type": "text", "text": "以后回答简洁些，也多用表格"}]}
        ledger.capture([original], origin="human")
        model = StreamModel(original["message_id"])
        agent = Agent("", workspace_dir=str(tmp_path), model=model, tools=[tool], enable_skills=False,
                      max_steps=4, output_mode="logger")
        agent._current_session_id = "stream-chat"
        agent._current_agent_id = None
        assert agent.messages == []  # 当前真人只在 executor，旧 agent.messages 看不到。
        events = []
        def bind(executor):
            executor._memory_input_message = original
        response = agent.run_stream("以后回答简洁些，也多用表格", on_executor=bind, on_event=events.append)
        assert response == "已记住"
        assert len(model.requests) == 2
        assert tool.agent is agent and tool._memory_record_trusted is True
        assert len(ledger.active_entries()) == 2
        ends = [e["data"] for e in events if e["type"] == "tool_execution_end"]
        assert len(ends) == 2
        assert all(e["status"] == "success" for e in ends), ends
        assert agent._memory_request_revisions["stream-run"]["revision"] == ledger.revision()


def test_compact_saves_pending_before_pruning_and_retains_current_human_evidence(routed):
    ledger, messages, entry = routed
    current = {"role": "user", "content": [{"type": "text", "text": "also remember current request"}]}
    ledger.capture([current], origin="human")
    original_id = current["message_id"]
    model = SummaryModel(dict(context_summary="task context 42 dB", entries=[entry]))
    manager = MemoryFlushManager(ledger.workspace, model)
    agent = Agent("", workspace_dir=str(ledger.workspace), enable_skills=False)
    agent.memory_manager = type("Manager", (), {"flush_manager": manager})()
    agent.messages = copy.deepcopy(messages) + [{"role": "assistant", "content": "done"}, current,
                                              {"role": "assistant", "content": "understood"}]
    result = agent.compact_context(keep_recent_turns=1)
    assert result["ok"]
    assert len(agent.messages) == 2
    assert ledger.pending_candidates()[0]["evidence"][0]["message_id"] == messages[0]["message_id"]
    wrapped = agent.messages[0]
    assert wrapped["message_id"] != original_id
    assert wrapped["extras"]["memory_trace"]["origin"] == "internal"
    evidence = json.loads(agent.memory_request_prompt(agent.messages).rsplit("\n", 1)[-1])["evidence"]
    original = next(e for e in evidence if e["message_id"] == original_id)
    assert original["origin"] == "human"
    assert original["text"] == "also remember current request"


def test_failed_candidate_commit_keeps_compact_history_intact(routed, monkeypatch):
    ledger, messages, entry = routed
    manager = MemoryFlushManager(ledger.workspace, SummaryModel(dict(context_summary="summary", entries=[entry])))
    agent = Agent("", workspace_dir=str(ledger.workspace), enable_skills=False)
    agent.memory_manager = type("Manager", (), {"flush_manager": manager})()
    agent.messages = copy.deepcopy(messages) + [{"role": "assistant", "content": "reply"},
                                              {"role": "user", "content": "current"}]
    before = copy.deepcopy(agent.messages)
    monkeypatch.setattr(MemoryLedger, "record", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert not agent.compact_context(keep_recent_turns=1)["ok"]
    assert [m["content"] for m in agent.messages] == [m["content"] for m in before]


def test_excluded_fact_leaves_old_compact_summary_before_next_request(routed):
    ledger, messages, entry = routed
    accepted = ledger.record("accept", [entry], trusted=True)["results"][0]
    manager = MemoryFlushManager(ledger.workspace, SummaryModel(dict(context_summary="旧偏好表格", entries=[])))
    agent = Agent("", workspace_dir=str(ledger.workspace), enable_skills=False)
    agent.memory_manager = type("Manager", (), {"flush_manager": manager})()
    current = {"role": "user", "content": [{"type": "text", "text": "当前新问题"}]}
    ledger.capture([current], origin="human")
    original_id = current["message_id"]
    agent.messages = messages + [{"role": "assistant", "content": "done"}, current,
                                 {"role": "assistant", "content": "reply"}]
    assert agent.compact_context(keep_recent_turns=1)["ok"]
    assert "旧偏好表格" in str(agent.messages)
    ledger.exclude(accepted["entry_id"])
    executor = AgentStreamExecutor(agent, None, "", [], messages=agent.messages)
    request_messages = executor._prepare_messages()
    assert "旧偏好表格" not in str(request_messages)
    assert "当前新问题" in str(request_messages)
    assert any(message.get("message_id") == original_id for message in request_messages)


@pytest.fixture(params=["compact", "trim"])
def compacted_task_context(routed, request):
    ledger, messages, entry = routed
    accepted = ledger.record("accept-context-dependency", [entry], trusted=True)["results"][0]
    model = SummaryModel(dict(context_summary="临时任务预算42 dB，偏好表格", entries=[]))
    manager = MemoryFlushManager(ledger.workspace, model)
    agent = Agent("", workspace_dir=str(ledger.workspace), enable_skills=False)
    agent.memory_manager = type("Manager", (), {"flush_manager": manager})()
    current = {"role": "user", "content": [{"type": "text", "text": "按之前参数继续计算"}]}
    ledger.capture([current], origin="human")
    original_id = current["message_id"]
    agent.messages = copy.deepcopy(messages) + [{"role": "assistant", "content": "done"}, current,
                                              {"role": "assistant", "content": "reply"}]
    if request.param == "compact":
        assert agent.compact_context(keep_recent_turns=1)["ok"]
        executor = AgentStreamExecutor(agent, None, "", [], messages=agent.messages)
    else:
        executor = AgentStreamExecutor(agent, None, "", [], messages=agent.messages)
        discarded = [{"messages": executor.messages[:2]}]
        kept = [{"messages": executor.messages[2:]}]
        ready = executor._build_context_summary_callback(discarded, kept)
        assert manager.flush_from_messages(discarded[0]["messages"], context_summary_callback=ready)
        executor.messages = kept[0]["messages"]
    assert "临时任务预算42 dB" in str(executor.messages)
    extras = executor.messages[0]["extras"]
    assert extras["context_memory_snapshot"]["revision"] == extras["context_memory_revision"]
    return ledger, accepted, model, executor, original_id


def test_unrelated_memory_addition_preserves_temporary_task_summary(compacted_task_context):
    ledger, _, model, executor, _ = compacted_task_context
    message = {"role": "user", "content": "以后称呼我老王"}
    ledger.capture([message], origin="human")
    proposal = dict(content=message["content"], disposition="conversation_memory", reason="独立称呼偏好",
                    basis=[basis(ledger.evidence([message])[0])])
    assert ledger.record("unrelated-preference", [proposal], trusted=True)["results"][0]["status"] == "remembered"
    assert "临时任务预算42 dB" in str(executor._prepare_messages())
    assert model.calls == 1


def test_exclusion_still_strips_dependent_task_summary(compacted_task_context):
    ledger, accepted, model, executor, original_id = compacted_task_context
    ledger.exclude(accepted["entry_id"])
    prepared = executor._prepare_messages()
    assert "临时任务预算42 dB" not in str(prepared)
    assert "按之前参数继续计算" in str(prepared)
    assert any(message.get("message_id") == original_id for message in prepared)
    assert model.calls == 1


def test_legacy_exclusion_strips_task_summary(compacted_task_context):
    import hashlib
    ledger, _, model, executor, _ = compacted_task_context
    legacy = ledger.workspace / "MEMORY.md"
    legacy.write_text("旧偏好：详细回答", encoding="utf-8")
    message = {"role": "user", "content": "把旧偏好改成简洁回答"}
    ledger.capture([message], origin="human")
    proposal = dict(content="简洁回答", disposition="conversation_memory", reason="更正旧记忆",
                    basis=[basis(ledger.evidence([message])[0])],
                    legacy_supersedes=[dict(path="MEMORY.md", sha256=hashlib.sha256(legacy.read_bytes()).hexdigest(),
                                            start=0, end=len("旧偏好：详细回答"))])
    assert ledger.record("legacy-context-correction", [proposal], trusted=True)["results"][0]["status"] == "remembered"
    assert "临时任务预算42 dB" not in str(executor._prepare_messages())
    assert model.calls == 1


def test_legacy_revision_only_summary_keeps_conservative_invalidation(compacted_task_context):
    ledger, _, _, executor, _ = compacted_task_context
    executor.messages[0]["extras"].pop("context_memory_snapshot")
    assert "临时任务预算42 dB" in str(executor._prepare_messages())
    ledger.human_edit("memory/unrelated.md", "独立偏好")
    assert "临时任务预算42 dB" not in str(executor._prepare_messages())


def test_overflow_plain_compression_retains_real_basis(routed, monkeypatch):
    ledger, messages, entry = routed
    # 工具链足够长时实际 overflow 会转为纯文本。
    agent = Agent("", workspace_dir=str(ledger.workspace), enable_skills=False)
    chain = copy.deepcopy(messages) + [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call", "name": "read", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call", "content": "x" * 20000}]},
        {"role": "assistant", "content": "完成"},
    ]
    executor = AgentStreamExecutor(agent, None, "", [], messages=chain)
    monkeypatch.setattr(executor, "_overflow_input_budget", lambda error: 100)
    assert executor._smart_compact_to_budget("too long")
    prompt = json.loads(agent.memory_request_prompt(executor.messages).rsplit("\n", 1)[-1])
    original = next(e for e in prompt["evidence"] if e["message_id"] == messages[0]["message_id"])
    assert original["origin"] == "human"
    assert ledger.record("after-overflow", [entry], trusted=True)["results"][0]["status"] == "remembered"


def test_shared_human_file_does_not_become_legacy_in_user_scope(tmp_path):
    from agent.memory.projection import read_memory_text
    with identity_scope(user_id=None, session_id="shared"):
        ledger = MemoryLedger(tmp_path)
        receipt = ledger.human_edit("MEMORY.md", "旧的共享约定")
        ledger.exclude(receipt["entry_id"])
    with identity_scope(user_id="owner", session_id="private"):
        assert read_memory_text(tmp_path, "MEMORY.md") == ""


def test_repeated_compaction_keeps_multiblock_real_evidence(routed):
    from agent.protocol.agent_stream import compress_turn_to_text_only
    from agent.memory.projection import mark_context_message
    ledger, _, _ = routed
    original = dict(role="user", content=[dict(type="text", text="偏好表格"), dict(type="text", text="另一个真实约定")])
    ledger.capture([original], origin="human")
    original_id = original["message_id"]
    wrapped = copy.deepcopy(original)
    mark_context_message(wrapped, revision=ledger.revision())
    wrapped["content"][0]["text"] = "上下文摘要\n偏好表格"
    compressed = compress_turn_to_text_only(dict(messages=[wrapped, dict(role="assistant", content="完成")]))
    agent = Agent("", workspace_dir=str(ledger.workspace), enable_skills=False)
    prompt = json.loads(agent.memory_request_prompt(compressed["messages"]).rsplit("\n", 1)[-1])
    human = [e for e in prompt["evidence"] if e["message_id"] == original_id]
    assert len(human) == 2 and all(e["origin"] == "human" for e in human)
