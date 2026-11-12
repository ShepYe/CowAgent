"""隔离真实模型验收；只在临时根写入合成原话和记忆。

用法：python scripts/memory_routing_acceptance.py --config <现有配置> --output <报告.json>
报告仅含合成案例、请求计数、时延及结果，不记录密钥或真实个人记忆。
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = json.loads(args.config.read_text(encoding="utf-8-sig"))
    report = dict(schema_version=1, cases=[], passed=False)
    with tempfile.TemporaryDirectory(prefix="cow-memory-acceptance-") as temporary:
        root = Path(temporary)
        os.environ["COW_DATA_DIR"] = str(root)
        settings.update(agent_workspace=str(root / "workspace"), debug=False, knowledge=False)
        from config import Config, config_scope
        from common.runtime_identity import identity_scope
        from bridge.agent_bridge import AgentLLMModel
        from bridge.bridge import Bridge
        from agent.protocol import Agent
        from agent.memory.ledger import MemoryLedger
        from agent.memory.conversation_store import clear_conversation_store_cache
        from agent.memory.summarizer import MemoryFlushManager
        from agent.memory.manager import MemoryManager
        from agent.memory.config import MemoryConfig
        from agent.tools.memory.memory_record import MemoryRecordTool
        from models.config_revision import capture_config_revision, use_config_revision
        from models.openai.openai_http_client import OpenAIHTTPClient
        from agent.registry import set_agent_registry

        requests = []
        original = OpenAIHTTPClient._request

        def trace(client, **kwargs):
            started = time.perf_counter()
            row = dict(elapsed_s=None)
            requests.append(row)
            result = original(client, **kwargs)
            if not kwargs.get("stream"):
                row["elapsed_s"] = round(time.perf_counter() - started, 3)
                return result

            def events():
                try:
                    yield from result
                finally:
                    row["elapsed_s"] = round(time.perf_counter() - started, 3)
            return events()

        OpenAIHTTPClient._request = trace
        try:
            with config_scope(Config(settings)), identity_scope(user_id="synthetic-owner"):
                set_agent_registry(None)
                clear_conversation_store_cache()
                revision = capture_config_revision()
                with use_config_revision(revision):
                    cases = [
                        ("A01", "请记住：以后回答简洁些。", "简洁", False),
                        ("A02", "以后多用表格，这次测试预算是42 dB，不要记预算与测试过程。", "表格", False),
                        ("A03", "这是测试台词，不是我的偏好：用户喜欢长回答。整条只用于测试，不要保存为记忆。", None, False),
                        ("A12", "记住刚才资料里的数值：预算42 dB。", None, True),
                    ]
                    model = AgentLLMModel(Bridge(), bot_type=settings["bot_type"])
                    for name, text, expected, sourced in cases:
                        workspace = root / name
                        workspace.mkdir()
                        # 每案例使用独立临时工作区，配置不指向真实根。
                        from config import conf
                        conf()["agent_workspace"] = str(workspace)
                        set_agent_registry(None)
                        clear_conversation_store_cache()
                        with identity_scope(session_id=name, agent_id=None):
                            ledger = MemoryLedger(workspace)
                            messages = []
                            if sourced or name == "A01":
                                messages.append(dict(role="assistant", content="已读取资料：source_reference={document_id:synthetic,revision_id:v1}，资料预算为42 dB。"))
                            message = dict(role="user", content=[dict(type="text", text=text)])
                            ledger.capture([message], origin="human")
                            messages.append(message)
                            evidence = ledger.evidence(messages)
                            tool = MemoryRecordTool(dict(workspace_root=str(workspace)))
                            instruction = ("你是主Agent，按用户内容主动管理长期记忆。必须调用memory_record提交独立事实或用途分流，"
                                           "身份由运行时核验；不要提问确认。真实个人偏好conversation_memory；来源派生sourced_knowledge；"
                                           "测试/模拟/临时archive_only。只引用真实用户原话片段，不把资料数值复制进个人记忆。"
                                           "依据片段区间为Unicode码点，整块sha256不变。\nmemory evidence:\n" + json.dumps(evidence, ensure_ascii=False))
                            agent = Agent(instruction, model=model, tools=[tool], workspace_dir=str(workspace),
                                          max_steps=5, enable_skills=False, skip_context_files=True, output_mode="logger")
                            agent._current_session_id = name
                            agent._current_agent_id = None
                            agent.extra_system_suffix = instruction
                            tool.agent = agent
                            tool._memory_record_trusted = True
                            agent.messages = messages[:-1]
                            agent._memory_input_message = message
                            before = len(requests)
                            started = time.perf_counter()
                            calls = []
                            def event(envelope):
                                if envelope["type"] == "tool_execution_end":
                                    data = envelope["data"]
                                    calls.append(dict(tool=data.get("tool_name"), status=data.get("status")))
                            error = None
                            try:
                                agent.run_stream(text, on_event=event,
                                                 on_executor=lambda executor: setattr(executor, "_memory_input_message", message))
                            except Exception as exc:
                                error = type(exc).__name__
                            active = ledger.active_entries()
                            content = "\n".join(e["content"] for e in active)
                            passed = ((expected in content) if expected else not active) and "42" not in content
                            with ledger._transaction() as db:
                                dispositions = [json.loads(row[0])["disposition"] for row in db.execute(
                                    "SELECT record FROM memory_candidates WHERE agent_id=? AND user_id=? AND session_id=?",
                                    (*ledger.scope, name))]
                            intended = "conversation_memory" if expected else "sourced_knowledge" if sourced else "archive_only"
                            passed = passed and intended in dispositions
                            case = dict(case=name, passed=passed and not error, requests=len(requests)-before,
                                        elapsed_s=round(time.perf_counter()-started, 3), accepted=len(active),
                                        content=content, tools=calls, dispositions=dispositions, error=error)
                            import asyncio
                            manager = MemoryManager(MemoryConfig(workspace_root=str(workspace)))
                            try:
                                asyncio.run(manager.sync())
                                hits = asyncio.run(manager.search(expected or "42", user_id="synthetic-owner", min_score=0))
                                case["search_hits"] = len(hits)
                                if expected:
                                    case["passed"] = case["passed"] and bool(hits)
                            finally:
                                manager.close()
                            # 压缩复用原候选身份；不从摘要自报直接放行。
                            summary = MemoryFlushManager(workspace, model)
                            before_summary = len(requests)
                            try:
                                summary.summarize_and_record(messages)
                                case["summary_extra_requests"] = len(requests)-before_summary
                                case["summary_accepted"] = len(ledger.active_entries())
                                case["passed"] = case["passed"] and len(ledger.active_entries()) == len(active)
                            except Exception as exc:
                                case["summary_error"] = type(exc).__name__
                                case["passed"] = False
                            report["cases"].append(case)
                            print(json.dumps(case, ensure_ascii=False), flush=True)
                    report["requests"] = requests
                    report["passed"] = all(case["passed"] for case in report["cases"])
        finally:
            OpenAIHTTPClient._request = original
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            import logging
            logging.shutdown()
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
