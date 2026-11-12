"""可恢复的个人任务执行器，复用现有主 Agent 与渠道发送。"""
from concurrent.futures import ThreadPoolExecutor
import threading

from common.personal_tasks import OWNER, TaskError, TaskStore

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="personal")
_active = set()
_lock = threading.Lock()
_channels = {}
_reconciled = False


def register_channel(instance_id, download, notify):
    _channels[instance_id] = (download, notify)
    recover()


def enqueue(task_id):
    with _lock:
        if task_id in _active:
            return
        _active.add(task_id)
    _pool.submit(_run, task_id)


def recover():
    global _reconciled
    store = TaskStore()
    with _lock:
        records = store.list() if _reconciled else store.reconcile()
        _reconciled = True
    for r in records:
        if r["status"] == "needs_reconcile":
            # 知识发布的恢复由知识登记核对；不盲目重新调用模型。
            if "reading_publication" in r or r.get("wiki_batch_id") or r.get("source_candidates") or r.get("attachment_decisions") or r["stage"] in {"receive", "download", "decide"}:
                r = store._change(OWNER, r["task_id"], r["attempt"], {"needs_reconcile"}, status="queued")
            else:
                store.fail(OWNER, r["task_id"], r["attempt"], "needs_reconcile", r["stage"])
        if r["status"] == "queued" or (r["status"] == "succeeded" and r["notification"] in {"pending", "failed", "unknown"}):
            origin = r["origin"]
            if origin.get("channel") != "feishu" or origin.get("channel_instance_id") in _channels:
                enqueue(r["task_id"])


def _notify(store, task, text):
    origin = task["origin"]
    if origin.get("channel") != "feishu":
        store.notification(OWNER, task["task_id"], "sent")
        return
    channel = _channels.get(origin.get("channel_instance_id"))
    if not channel:
        return
    try:
        # 标识稳定，发送结果未知时可复用飞书幂等键。
        status = channel[1](task, text)
    except Exception:
        status = "unknown"
    store.notification(OWNER, task["task_id"], status)


def _run(task_id):
    store = TaskStore()
    running_attempt = None
    try:
        task = store.get(OWNER, task_id)
        if task["status"] == "succeeded":
            _notify(store, task, task.get("response") or "处理已完成，请在桌面查看结果。")
            return
        task = store.claim(OWNER, task_id)
        running_attempt = task["attempt"]
        from models.config_revision import require_config_revision
        require_config_revision(task["config_revision"])
        decisions = task.get("attachment_decisions") or {}
        fully_decided = bool(decisions) and len(decisions) == len(task["attachments"]) and all(d.get("ready") for d in decisions.values())
        if "reading_publication" in task:
            # 只从受信持久检查点恢复阅读登记，不能采用模型工具参数中的目录。
            from agent.knowledge.reading import ReadingService
            workspaces = {d["workspace"] for d in decisions.values() if d.get("workspace")}
            if not workspaces and task.get("wiki_workspace"):
                workspaces.add(task["wiki_workspace"])
            for workspace in sorted(workspaces):
                ReadingService(workspace, task_store=store).resume_all(task)
            from agent.knowledge.reading_publication import resume_publication as resume_reading
            task = resume_reading(store, task)
            if task["status"] == "succeeded":
                _notify(store, task, "资料索引与发布已完成，可检索。")
                return
        elif task.get("wiki_batch_id") or task.get("source_candidates") or fully_decided:
            from agent.tools.knowledge_ingest import resume_publication
            store.stage(OWNER, task_id, task["attempt"], "pending_index")
            resume_publication(task, store)
            result = store.get(OWNER, task_id)
            _notify(store, result, "资料索引与发布已完成，可检索。" if result["outcome"] in {"published", "duplicate"} else "资料已按临时用途处理并留档。")
            return
        if task["payload"].get("source_action"):
            execute_source_action(store, task)
            task = store.get(OWNER, task_id)
        origin = task["origin"]
        if origin.get("channel") != "feishu":
            expected = len(task["payload"].get("files", [])) or (1 if task["payload"].get("filename") else 0)
            if len(task["attachments"]) < expected:
                raise TaskError("storage_failed", "附件保存未完成，请重新提交原件")
        if origin.get("channel") == "feishu":
            channel = _channels.get(origin.get("channel_instance_id"))
            if not channel:
                raise TaskError("config_unavailable")
            _notify(store, task, f"已接收，任务 {task_id}。")
            channel[0](store, task)
        task = store.get(OWNER, task_id)
        if task["attachments"] and origin.get("channel") == "feishu":
            _notify(store, task, f"原件已保存，正在整理。任务 {task_id}。")
        store.stage(OWNER, task_id, task["attempt"], "decide")
        response = run_agent(task)
        task = store.get(OWNER, task_id)
        if task["attempt"] != running_attempt:
            return
        if task["status"] == "cancelled":
            _notify(store, task, "任务已取消。原始记录与附件保留。")
            return
        if task["status"] == "running":
            if "reading_publication" in task:
                from agent.knowledge.reading_publication import resume_publication as resume_reading
                task = resume_reading(store, task)
                if task["status"] == "running":
                    store.fail(OWNER, task_id, running_attempt, "reading_incomplete", "parse")
                    response = "阅读范围尚未全部完成，已发布成果保留，可继续重试。"
            elif task["attachments"]:
                # 只有 Agent 明确调用 waiting 才向用户追问；缺少整理成品属于处理未完成。
                store.fail(OWNER, task_id, task["attempt"], "organization_incomplete", "publish")
            else:
                store.complete(OWNER, task_id, task["attempt"], "transient", [])
        with store._transaction() as db:
            task = store._read(db, OWNER, task_id)
            task["response"] = response
            store._save(db, task)
        _notify(store, task, response or "处理记录已保存，请在桌面查看。")
    except Exception as exc:
        task = store.get(OWNER, task_id)
        if task["status"] == "running" and task["attempt"] == running_attempt:
            code = getattr(exc, "code", "processing_failed")
            from common.log import logger
            logger.warning('[PersonalTask] task=%s attempt=%s stage=%s failed code=%s',
                           task_id, task['attempt'], task['stage'], code)
            store.fail(OWNER, task_id, task["attempt"], code, task["stage"])
            _notify(store, store.get(OWNER, task_id), f"处理未完成：{code}。任务 {task_id} 可在桌面查看或重试。")
    finally:
        with _lock:
            _active.discard(task_id)


def run_agent(task):
    from bridge.bridge import bridge_for_config_revision
    from bridge.context import Context, ContextType
    from common.runtime_identity import identity_scope
    query = task["payload"].get("text", "")
    query += "\n" + task.get("clarification", "")
    if "reading_publication" in task:
        import json
        from agent.knowledge.reading_publication import publication_state
        state = publication_state(TaskStore(), task)
        query += ("\n[系统阅读目标] 继续未完成范围；已成功发布的批次不得重复提交。目标不可缩小。\n"
                  + json.dumps({"targets": state["targets"], "remaining": state["remaining"],
                                "result_refs": task["result_refs"]}, ensure_ascii=False))
    if task["attachments"]:
        import json
        query += ("\n[系统接收记录] task_id=" + task["task_id"] +
                  "\n附件已可靠保存。根据用户上下文自主判断用途，分享资料主动整理，无需逐份询问。"
                  "先读取 knowledge-wiki 技能正文及资料，调用 knowledge_ingest 登记用途；长期资料须继续提炼、分类、命名，"
                  "以 pages 正文块、每页必填的一句话 summary 与真实出处完成受控发布，支持拆页、跨来源和更新已有页。organization_required 表示须继续整理。"
                  "临时资料只留档，缺少必要信息时具体澄清。"
                  "面向用户只展示标题、分类、出处和真实处理状态；document_id、revision_id、parse_id、publish_id及状态修订仅用于工具参数，不在回复中罗列。"
                  "禁止将未完成索引称为可检索。附件：\n" + json.dumps(task["attachments"], ensure_ascii=False))
    context = Context(ContextType.TEXT, query)
    context["session_id"] = task["origin"].get("session_id") or "personal:" + task["task_id"]
    context["channel_type"] = task["origin"].get("channel", "web")
    context["personal_task_id"] = task["task_id"]
    context["request_id"] = task["request_id"]
    from common.personal_tasks import task_scope
    with task_scope(task["task_id"]), identity_scope(user_id=OWNER, session_id=context["session_id"]):
        with bridge_for_config_revision(task["config_revision"]) as bridge:
            reply = bridge.fetch_agent_reply(query, context)
    return str(getattr(reply, "content", "") or "")


def execute_source_action(store, task):
    """本机明确授权的恢复任务由程序核验，不交模型改写期望修订。"""
    from agent.tools.knowledge_source_action import KnowledgeSourceAction
    from common.personal_tasks import task_scope
    from common.runtime_identity import identity_scope
    directive = task["payload"]["source_action"]
    if directive.get("action") != "republish" or directive.get("purpose") != "long_term":
        raise TaskError("invalid_request")
    if not task["origin"].get("parent_task_id"):
        raise TaskError("forbidden")
    parent = store.get(OWNER, task["origin"]["parent_task_id"])
    if not any(r.get("document_id") == directive.get("document_id") and r.get("revision_id") == directive.get("revision_id") for r in parent["result_refs"]):
        raise TaskError("forbidden")
    with task_scope(task["task_id"]), identity_scope(user_id=OWNER):
        with store.commit_guard(OWNER, task["task_id"], task["attempt"]):
            outcome = KnowledgeSourceAction({"task_store": store}).execute(directive)
            if outcome.status != "success":
                raise TaskError(outcome.result.get("code", "index_failed"))
            # 恢复来源资格后仍由主 Agent 重新整理；来源资格不是可检索回执。
