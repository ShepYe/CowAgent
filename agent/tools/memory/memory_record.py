"""依据真实会话片段提交记忆；身份及投影目标由运行时确定。"""
from contextlib import nullcontext
import re

from agent.tools.base_tool import BaseTool, ToolResult
from common.runtime_identity import current_identity


_MESSAGE_BASIS = {
    "type": "object", "additionalProperties": False,
    "properties": {"message_id": {"type": "string", "minLength": 1},
                   "block_index": {"type": ["integer", "null"]},
                   "start": {"type": "integer", "minimum": 0},
                   "end": {"type": "integer", "minimum": 1},
                   "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
    "required": ["message_id", "block_index", "start", "end", "sha256"],
}
_ENTRY_BASIS = {
    "type": "object", "additionalProperties": False,
    "properties": {"entry_id": {"type": "string", "minLength": 1},
                   "revision": {"type": "integer", "minimum": 1}},
    "required": ["entry_id", "revision"],
}
_ENTRY = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "candidate_id": {"type": "string", "minLength": 1},
        "content": {"type": "string", "minLength": 1},
        "disposition": {"type": "string", "enum": ["conversation_memory", "sourced_knowledge", "archive_only", "undecided"]},
        "basis": {"type": "array", "minItems": 1, "items": {"oneOf": [_MESSAGE_BASIS, _ENTRY_BASIS]}},
        "reason": {"type": "string", "minLength": 1},
        "supersedes": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "legacy_supersedes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"path": {"type": "string", "minLength": 1},
                           "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                           "start": {"type": "integer", "minimum": 0},
                           "end": {"type": "integer", "minimum": 1}},
            "required": ["path", "sha256", "start", "end"]}},
    },
    "required": ["content", "disposition", "basis", "reason"],
}


def _validate(value, schema):
    """执行工具的封闭参数 schema，不依赖可选的第三方校验包。"""
    if "oneOf" in schema:
        for option in schema["oneOf"]:
            try:
                _validate(value, option)
                return
            except ValueError:
                pass
        raise ValueError("basis 必须为完整消息片段或有效条目修订引用")
    kind = schema["type"]
    kinds = kind if isinstance(kind, list) else [kind]
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "integer": type(value) is int, "null": value is None}
    if not any(matches[k] for k in kinds):
        raise ValueError("参数类型不匹配")
    if isinstance(value, dict):
        properties = schema["properties"]
        if set(value) - set(properties) or set(schema.get("required", [])) - set(value):
            raise ValueError("参数含未知字段或缺少必填字段")
        for key, item in value.items():
            _validate(item, properties[key])
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValueError("数组不能为空")
        for item in value:
            _validate(item, schema["items"])
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or ("pattern" in schema and not re.fullmatch(schema["pattern"], value)):
            raise ValueError("文本或哈希格式无效")
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError("未知用途")
    elif type(value) is int and value < schema.get("minimum", value):
        raise ValueError("区间或修订无效")


class MemoryRecordTool(BaseTool):
    name = "memory_record"
    description = ("按独立事实提交长期记忆及用途决定。普通交流形成的独立主题知识也用本工具，content 可含主题及整理正文，准入后保存到 memory/routed.md。引用提示中的 memory evidence 原话片段，"
                   "sha256 为完整原始文本块哈希，区间按 Unicode 码点计算。资料结论走 knowledge_ingest；"
                   "测试及临时内容用 archive_only。更正用 supersedes，排除已有候选沿用 candidate_id 和依据。"
                   "回执区分已记住、投影待完成、仅留档、来源发布和待判断；searchable 单独报告。")
    params = {"type": "object", "additionalProperties": False,
              "properties": {"request_id": {"type": "string", "minLength": 1},
                             "entries": {"type": "array", "minItems": 1, "items": _ENTRY}},
              "required": ["request_id", "entries"]}

    def __init__(self, config=None):
        self.config = config or {}
        self.agent = None
        self._memory_record_trusted = False

    def execute(self, args):
        from common.personal_tasks import current_task_id
        from common import state_dir
        identity = current_identity()
        agent = self.agent
        if (not self._memory_record_trusted or agent is None or not identity.session_id or current_task_id()
                or getattr(agent, "_is_subagent", False)
                or (getattr(self, "context", None) is not None and self.context is not agent)
                or getattr(agent, "_is_replay", False) or getattr(agent, "_is_internal", False)
                or identity.session_id.startswith("personal:")
                or getattr(agent, "_current_session_id", None) != identity.session_id
                or getattr(agent, "_current_agent_id", None) != identity.agent_id):
            return ToolResult.fail("memory_record 仅供当前会话的主 Agent 使用，worker/task 不能提交用途决定。")
        try:
            # execute 也校验 schema，防止跳过 Provider 参数校验而注入 owner/origin/approved。
            _validate(args, self.params)
            if not args["request_id"].strip():
                raise ValueError("request_id 不能为空")
            from agent.memory.ledger import MemoryLedger
            from agent.memory.conversation_store import get_conversation_store
            workspace = getattr(agent, "workspace_dir", None) or self.config.get("workspace_root") or state_dir.state_root()
            store = get_conversation_store(workspace)
            ledger = MemoryLedger(workspace, store=store)
            expected_revision = ledger.revision()
            snapshot = getattr(agent, "_memory_request_revisions", {}).get(identity.run_id)
            if snapshot is not None:
                if any(snapshot.get(key) != getattr(identity, key) for key in ("agent_id", "user_id", "session_id")):
                    raise ValueError("记忆判定快照不属于当前身份")
                expected_revision = snapshot["revision"]
            ledger.capture(store.load_messages(identity.session_id), session_id=identity.session_id)
            with getattr(agent, "messages_lock", None) or nullcontext():
                ledger.capture(agent.messages, session_id=identity.session_id)
            receipt = ledger.record(args["request_id"], args["entries"], trusted=True, expected_revision=expected_revision)
            labels = {"remembered": "已记住", "pending_projection": "投影待完成", "archive_only": "仅留档",
                      "requires_knowledge_ingest": "需按来源发布", "undecided": "待判断", "failed": "写入失败"}
            statuses = [item.get("status", "failed") for item in receipt.get("results", [])]
            display = "；".join(labels.get(status, status) for status in statuses) or "写入失败"
            if "failed" in statuses or not statuses:
                return ToolResult.fail(receipt, display=display)
            manager = getattr(agent, "memory_manager", None)
            if manager and "remembered" in statuses:
                try:
                    manager.mark_dirty()
                except Exception:
                    pass  # 台账已接受；可检索状态仍由回执独立报告。
            return ToolResult.success(receipt, display=display)
        except Exception as exc:
            code = getattr(exc, "code", "invalid_evidence" if isinstance(exc, ValueError) else "storage_failed")
            receipt = dict(request_id=args.get("request_id") if isinstance(args, dict) else None,
                           results=[dict(status="failed", error=code, reason=str(exc), searchable=False)])
            return ToolResult.fail(receipt, display="写入失败")
