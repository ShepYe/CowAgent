"""消息身份帮助函数；不访问存储或记忆台账。"""

from __future__ import annotations

from uuid import uuid4


def ensure_trace(message, *, workspace_id="", agent_id="", user_id="",
                 session_id="", origin="internal"):
    """原位补齐身份并返回 trace；已有身份、扩展字段和版本保持不变。

    只有受信入口可以传 human。role=user 本身不能证明是真人原话。
    workspace_id 可暂空，由台账从持久 scope 补齐。
    """
    extras = message.get("extras")
    if not isinstance(extras, dict):
        extras = {}
        message["extras"] = extras
    trace = extras.get("memory_trace")
    if trace is None:
        trace = {}
        extras["memory_trace"] = trace
    if not isinstance(trace, dict):
        raise ValueError("memory_trace must be an object")
    ids = [value for value in (message.get("message_id"), extras.get("message_id"),
                               trace.get("message_id")) if value]
    if any(not isinstance(value, str) for value in ids) or len(set(ids)) > 1:
        raise ValueError("conflicting message_id")
    message_id = ids[0] if ids else str(uuid4())
    message["message_id"] = extras["message_id"] = trace["message_id"] = message_id
    trace.setdefault("schema_version", 1)
    for key, value in (("workspace_id", workspace_id), ("agent_id", agent_id),
                       ("user_id", user_id), ("session_id", session_id)):
        if not trace.get(key):
            trace[key] = value or ""
    trace.setdefault("origin", origin)
    # 兼容原有用途字段，以 extras 作为留档载体，不触碰正文。
    for key in ("personal_task_id", "purpose", "archive_only", "metadata"):
        if key in message:
            extras.setdefault(key, message[key])
    return trace
