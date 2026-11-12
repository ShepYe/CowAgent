"""Keep attachment task conversations in the archive, out of diary/dream inputs."""
from common.runtime_identity import current_identity
import threading

_evidence_lock = threading.RLock()
_evidence_tokens = {}
_discarded_evidence = {}
_sourced_sessions = set()


def _session_key():
    identity = current_identity()
    return (identity.agent_id, identity.user_id, identity.session_id) if identity.session_id else None


def note_source_evidence():
    """已核验来源进入会话后，派生持久化须继续走来源发布。"""
    key = _session_key()
    if key is not None:
        with _evidence_lock:
            _sourced_sessions.add(key)


def _source_bearing(message):
    text = str(message.get('content', ''))
    return any(marker in text for marker in ('source_reference', 'knowledge/managed/', 'knowledge/wiki/'))


def source_task_context():
    from common.personal_tasks import current_task_id
    return bool(current_task_id()) or (current_identity().session_id or "").startswith("personal:")


def source_derived_context():
    with _evidence_lock:
        sourced = _session_key() in _sourced_sessions
    return source_task_context() or sourced


def durable_messages(messages):
    if source_task_context():
        return []
    # 这里只缩小候选输入；最终来源／用途准入由台账按片段校验。
    # 不再用读过来源这一会话提示否决独立的普通消息。
    def eligible(message):
        containers = [message] + [message.get(key) or {} for key in ("metadata", "extras")]
        if any(item.get("personal_task_id") or item.get("archive_only")
               or item.get("purpose") in ("transient", "waiting") for item in containers):
            return False
        return "[personal-task:" not in str(message.get("content", "")) and not _source_bearing(message)
    return [message for message in messages if eligible(message)]


def mark_messages(messages, task_id):
    if not task_id:
        return messages
    result = []
    for message in messages:
        marked = dict(message)
        marked["personal_task_id"] = task_id
        marked["extras"] = dict(marked.get("extras") or {}, personal_task_id=task_id, archive_only=True)
        content = marked.get("content", "")
        marker = f"[personal-task:{task_id};archive-only]\n"
        if isinstance(content, str) and not content.startswith(marker):
            marked["content"] = marker + content
        elif isinstance(content, list):
            marked["content"] = [dict(block, text=marker + block["text"])
                if isinstance(block, dict) and block.get("type") == "text" and not block.get("text", "").startswith(marker)
                else block for block in content]
        result.append(marked)
    return result


def guard_memory_write(path, workspace_root=None):
    from pathlib import Path
    from common import state_dir
    target = Path(path).resolve()
    workspace = Path(workspace_root or state_dir.state_root()).resolve()
    # 工作区根及共享知识根均须守卫，不能通过普通 knowledge 副本绕开依据核验。
    protected = (target == workspace / "MEMORY.md"
                 or target.is_relative_to(workspace / "memory")
                 or target.is_relative_to(workspace / "knowledge")
                 or target.is_relative_to(Path(state_dir.knowledge_dir(base=workspace)).resolve()))
    if protected:
        raise ValueError("受保护的记忆／知识目标不能通过 edit/write 修改。真实会话记忆及普通交流形成的独立主题知识请使用 memory_record 并引用 basis，准入后保存到 memory/routed.md；资料原文及派生结论请使用 knowledge_ingest。")


def refresh_evidence_context(agent):
    """Keep ordinary context; discard archived tasks and stale source evidence."""
    from pathlib import Path
    from common import state_dir
    from agent.knowledge.personal_assistant import SourceService
    workspace = getattr(agent, "workspace_dir", None) or state_dir.state_root()
    root = Path(state_dir.knowledge_dir(base=workspace)).parent
    exists = (root / ".personal_sources" / "sources.sqlite3").exists()
    token = SourceService(root).evidence_revision() if exists else "no-sources"
    identity = current_identity()
    key = (str(Path(workspace).resolve()), identity.session_id or getattr(agent, "_current_session_id", None) or id(agent),
           identity.agent_id or getattr(agent, "_current_agent_id", ""))
    with _evidence_lock:
        previous = _evidence_tokens.get(key, getattr(agent, "_personal_evidence_revision", None))
        discarded = set(_discarded_evidence.get(key, set()))
    changed = previous is not None and previous != token
    unknown = previous is None and exists

    def archive(message):
        return bool(message.get("personal_task_id") or (message.get("extras") or {}).get("archive_only")
                    or "[personal-task:" in str(message.get("content", "")))

    def tool_result(message):
        return message.get("role") == "tool" or (isinstance(message.get("content"), list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in message["content"]))

    def source_evidence(message):
        text = str(message.get("content", ""))
        return any(marker in text for marker in ("source_reference", "knowledge/managed/", "knowledge/wiki/", "document_id", "publish_id", "page_version"))

    with agent.messages_lock:
        if any(_source_bearing(message) for message in agent.messages):
            note_source_evidence()
        messages = [message for message in agent.messages if not archive(message) and str(message) not in discarded]
        if unknown or changed:
            # Drop source-bearing turns and their assistant/tool follow-ups.
            # Ordinary earlier exchanges and all genuine user input survive.
            start = next((i for i, m in enumerate(messages) if source_evidence(m)), None)
            if start is not None:
                if start > 0 and tool_result(messages[start]) and messages[start-1].get("role") == "assistant":
                    start -= 1
                kept = []
                for i, message in enumerate(messages):
                    if i < start or (message.get("role") not in ("assistant", "tool") and not tool_result(message)):
                        kept.append(message)
                    else:
                        discarded.add(str(message))
                messages = kept
        agent.messages[:] = messages
    agent._personal_evidence_revision = token
    with _evidence_lock:
        _evidence_tokens[key] = token
        _discarded_evidence[key] = discarded
