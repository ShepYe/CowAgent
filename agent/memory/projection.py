"""按当前身份读取记忆全文；标记核验只能在切片、分块之前执行。"""
from pathlib import Path

from common.log import logger


def mark_context_message(message, revision=None, snapshot=None):
    """显示层改写取得新 internal 身份，不能冒用已保存的真人原话。"""
    from agent.memory.message_trace import ensure_trace
    import copy
    if snapshot is not None:
        if revision is not None and revision != snapshot["revision"]:
            raise ValueError("摘要修订与依赖快照不一致")
        revision = snapshot["revision"]
    previous = message.get("message_id")
    original = copy.deepcopy((message.get("extras") or {}).get("context_original") or message)
    extras = copy.deepcopy(message.get("extras") or {})
    extras.pop("memory_trace", None)
    extras.pop("message_id", None)
    if previous:
        extras["context_input_id"] = previous
        extras["context_original"] = original
    if revision is not None:
        extras["context_memory_revision"] = revision
    if snapshot is not None:
        extras["context_memory_snapshot"] = copy.deepcopy(snapshot)
    message.pop("message_id", None)
    message["extras"] = extras
    ensure_trace(message, origin="internal")


def refresh_context_messages(messages, ledger):
    """过期压缩摘要退出当前请求；保留原真人消息的可核验身份。"""
    import copy
    revision = None
    kept = []
    for message in messages:
        extras = message.get("extras") or {}
        stamp = extras.get("context_memory_revision")
        if "context_memory_snapshot" in extras:
            snapshot = extras["context_memory_snapshot"]
            valid = (isinstance(snapshot, dict) and stamp == snapshot.get("revision")
                     and ledger.context_valid(snapshot))
        else:
            # 旧缓存没有依赖信息，只能沿用作用域修订的保守失效策略。
            if stamp is not None and revision is None:
                revision = ledger.revision()
            valid = stamp is None or stamp == revision
        if valid:
            kept.append(message)
            continue
        original = extras.get("context_original")
        if isinstance(original, dict):
            original = copy.deepcopy(original)
            while isinstance((original.get("extras") or {}).get("context_original"), dict):
                original = original["extras"]["context_original"]
            kept.append(original)
    messages[:] = kept
    return messages


def is_memory_path(workspace, path):
    root = Path(workspace).resolve()
    target = Path(path)
    target = (root / target).resolve() if not target.is_absolute() else target.resolve()
    try:
        relative = target.relative_to(root)
    except ValueError:
        return False
    return relative.as_posix() == "MEMORY.md" or relative.parts[:1] == ("memory",)


def projection_owner(workspace, path):
    """只从当前 Agent 的权威文件登记解析 owner，未知投影不进入索引。"""
    import sqlite3
    from contextlib import closing
    from agent.memory.ledger import MemoryLedger
    ledger = MemoryLedger(workspace)
    target = Path(path)
    if not target.is_absolute():
        target = Path(workspace) / target
    relative = target.resolve().relative_to(Path(workspace).resolve()).as_posix()
    with closing(sqlite3.connect(str(ledger.db_path))) as db:
        rows = db.execute("SELECT user_id FROM memory_files WHERE agent_id=? AND path=?",
                          (ledger.agent_id, relative)).fetchall()
    if len(rows) > 1:
        raise ValueError("记忆文件存在多个权威 owner，暂不加载")
    return rows[0][0] if rows else None


def filter_memory_text(workspace, path, text):
    if not is_memory_path(workspace, path):
        return text
    try:
        from agent.memory.ledger import MemoryLedger
        from common.runtime_identity import current_identity, identity_scope
        relative = Path(path)
        if relative.is_absolute():
            relative = relative.resolve().relative_to(Path(workspace).resolve())
        user_id = current_identity().user_id or ""
        owner = projection_owner(workspace, path)
        if owner is not None:
            if owner not in ("", user_id):
                return ""
            user_id = owner
        elif relative.name.startswith("routed"):
            return ""
        elif relative.parts[:2] == ("memory", "users") and len(relative.parts) > 2:
            if relative.parts[2] != user_id:
                return ""
        with identity_scope(user_id=user_id):
            return MemoryLedger(workspace).filter_text(path, text)
    except Exception as exc:
        logger.warning(f"[Memory] 无法核验当前记忆，暂不加载 {path}: {exc}")
        return ""


def read_memory_text(workspace, path):
    target = Path(path)
    if not target.is_absolute():
        target = Path(workspace) / target
    if not target.is_file():
        return ""
    return filter_memory_text(workspace, target, target.read_text(encoding="utf-8"))
