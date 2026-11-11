"""桌面聊天附件的接收登记，保留原有会话与流式回复。"""
import hashlib
from pathlib import Path

from common.personal_tasks import OWNER, TaskError, TaskStore
from models.config_revision import capture_config_revision


def receive_desktop(request_id, session_id, text, attachments, upload_root, workspace):
    supported = {".pdf", ".docx", ".xlsx", ".md", ".txt"}
    entries = []
    for index, attachment in enumerate(attachments):
        raw = attachment.get("file_path", "")
        if not raw:
            continue
        base = Path(workspace if attachment.get("file_type") == "workspace_ref" else upload_root).resolve()
        source = Path(raw)
        if not source.is_absolute():
            source = base / source
        source = source.resolve(strict=True)
        if not source.is_relative_to(base):
            raise TaskError("forbidden", "附件不属于本次授权工作空间或上传目录")
        if source.is_file() and source.suffix.lower() in supported:
            entries.append((str(index), source, attachment.get("file_name") or source.name))
    if not entries:
        return None
    payload = {"text": text, "files": [{"attachment_id": i, "filename": name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for i, path, name in entries]}
    origin = {"channel": "web", "session_id": session_id}
    store = TaskStore()
    task = store.submit(OWNER, request_id, payload, origin, capture_config_revision())
    for index, path, name in entries:
        store.attach(OWNER, task["task_id"], path, name, index)
    return store.get(OWNER, task["task_id"])


def task_prompt(task):
    import json
    return ("\n[系统接收记录] task_id=" + task["task_id"] + "\n附件原件已保存。"
            "先读取 knowledge-wiki 技能正文和资料，再按上下文自主决定用途。调用 knowledge_ingest 登记用途；"
            "长期资料须继续提交 pages（标题、分类、知识正文块、真实出处及关联）完成整理发布；organization_required 表示尚未完成，"
            "无需逐份询问是否收录。明确临时使用的内容仅留档，禁止写入记忆、日记或摘要。"
            "仅在缺少必要信息时澄清。回复只展示标题、分类、出处及状态，内部资料/版本/发布编号和状态修订只用于工具，不在界面罗列。只有工具确认索引和发布完成才能报告可检索。附件：\n"
            + json.dumps(task["attachments"], ensure_ascii=False))
