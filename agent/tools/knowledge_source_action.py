"""Natural-language corrections use the same authority as desktop actions."""
from agent.tools.base_tool import BaseTool, ToolResult
from common.runtime_identity import current_identity
from agent.knowledge.personal_assistant import SourceService
from agent.knowledge.parser import SourceError
from contextlib import nullcontext


class KnowledgeSourceAction(BaseTool):
    name = "knowledge_source_action"
    description = "用户更正、撤回、改变资料用途或明确重新发布时调用。必须给出已登记 document_id、当前 expected_state 和具体 reason；通过知识查询取得版本，不猜资料关系。"
    params = {"type": "object", "properties": {
        "document_id": {"type": "string"}, "expected_state": {"type": "integer"},
        "action": {"type": "string", "enum": ["purpose", "withdraw", "correct", "republish"]},
        "purpose": {"type": "string", "enum": ["long_term", "transient", "waiting"]},
        "revision_id": {"type": "string", "description": "可选已登记版本；省略时恢复该资料最新登记版本"},
        "candidate_id": {"type": "string", "description": "仅用于接续索引失败回执中的恢复候选，expected_state 使用该回执的新状态修订"},
        "reason": {"type": "string"}}, "required": ["document_id", "expected_state", "action", "reason"]}

    def __init__(self, config=None):
        self.config = config or {}

    def execute(self, args):
        if current_identity().user_id != "local-owner":
            return ToolResult.fail("forbidden")
        store, task_id, attempt = None, None, None
        stage, candidate_id, state = "decide", None, None
        try:
            from common import state_dir
            from common.personal_tasks import current_task_id, TaskStore
            workspace = self.config.get("workspace_root") or self.config.get("cwd") or state_dir.state_root()
            service = SourceService(workspace)
            task_id = current_task_id()
            task = None
            if task_id:
                store = self.config.get("task_store") or TaskStore()
                task = store.get("local-owner", task_id)
                attempt = task["attempt"]
            action, document_id = args["action"], args["document_id"]
            if type(args["expected_state"]) is not int or not args.get("reason"):
                raise SourceError("invalid_request", "缺少有效状态修订或操作理由")
            restore = action == "republish" or (action == "purpose" and args.get("purpose") == "long_term")
            if args.get("candidate_id") and not restore:
                raise SourceError("invalid_request", "只有恢复发布可接续候选")
            with store.commit_guard("local-owner", task_id, attempt) if store else nullcontext():
                checkpoint = (task or {}).get("source_action_checkpoint")
                if restore:
                    # Select before changing validity, so a bad revision cannot
                    # invalidate any currently published evidence.
                    source = service.registered_revision(document_id, args.get("revision_id"))
                    if checkpoint:
                        if checkpoint["document_id"] != document_id or checkpoint["expected_state"] != args["expected_state"]:
                            raise SourceError("version_conflict", "恢复操作与任务检查点不一致")
                        state = checkpoint["state"]
                        source = service.registered_revision(document_id, checkpoint["revision_id"])
                    elif args.get("candidate_id"):
                        state = args["expected_state"]
                    else:
                        changed = service.change(document_id, action="republish", purpose="long_term",
                            expected_state=args["expected_state"], reason=args["reason"])
                        state = changed["state"]
                        if store:
                            checkpoint = dict(document_id=document_id, expected_state=args["expected_state"],
                                              state=state, revision_id=source["revision_id"])
                            task = store._change("local-owner", task_id, attempt, {"running"},
                                source_action_checkpoint=checkpoint, stage="parse")
                    doc = service.get(document_id)
                    if doc["state"] != state or doc["purpose"] != "long_term" or doc["validity"] != "active":
                        raise SourceError("version_conflict", "恢复操作已因新用途或撤回失效")
                    candidate_id = args.get("candidate_id") or (checkpoint or {}).get("candidate_id")
                    if candidate_id:
                        with service.transaction() as db:
                            row = db.execute("SELECT document,state,revision FROM candidates WHERE id=?", (candidate_id,)).fetchone()
                            if not row or row["document"] != document_id or row["state"] != state:
                                raise SourceError("version_conflict", "恢复候选不属于当前资料状态")
                    else:
                        stage = "parse"
                        candidate_id = service.prepare(source["revision_id"], expected_current_revision=doc["current"],
                                                       task_id=task_id)["candidate_id"]
                    stage = "pending_index"
                    if store:
                        checkpoint["candidate_id"] = candidate_id
                        store._change("local-owner", task_id, attempt, {"running"}, stage=stage,
                            source_action_checkpoint=checkpoint,
                            source_candidates=[dict(candidate_id=candidate_id, workspace=str(service.workspace))])
                    result = service.publish(candidate_id)
                    if store and not task["attachments"]:
                        store.complete("local-owner", task_id, attempt, "published", [result])
                    return ToolResult.success(result)
                result = service.change(document_id, expected_state=args["expected_state"],
                    action=action, purpose=args.get("purpose"), reason=args["reason"])
                return ToolResult.success(result)
        except Exception as exc:
            code = getattr(exc, "code", "storage_failed" if isinstance(exc, OSError) else "invalid_request")
            if store and attempt is not None:
                try:
                    store.fail("local-owner", task_id, attempt, code, stage)
                except ValueError:
                    pass
            return ToolResult.fail({"code": code, "stage": stage, "message": str(exc),
                                    "candidate_id": candidate_id, "expected_state": state,
                                    "searchable": False, "retryable": stage == "pending_index"})
