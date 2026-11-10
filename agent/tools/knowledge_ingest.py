"""The main Agent decides purpose; trusted task records authorize attachments."""
from pathlib import Path
from agent.tools.base_tool import BaseTool, ToolResult
from agent.knowledge.personal_assistant import SourceService, resume_publication as resume_legacy_publication
from agent.knowledge.parser import SourceError
from common.personal_tasks import TaskStore
from common.runtime_identity import current_identity

MAX_BATCH_FILES = 100
MAX_BATCH_TEXT_BYTES = 200 * 1024 * 1024


def _check_batch_limits(attachments, decisions):
    if len(attachments) > MAX_BATCH_FILES or sum(d.get("parsed_bytes", 0) for d in decisions.values()) > MAX_BATCH_TEXT_BYTES:
        raise SourceError("limit_exceeded", "资料发布批次超过 100 件或 200 MiB 文本预算")


class KnowledgeIngest(BaseTool):
    name = "knowledge_ingest"
    description = "自主整理接收任务中的资料。可按 attachment_id 分别决定用途，省略时同批同用途。long_term 长期知识；transient 仅留档；waiting 提出必要问题。返回 running 时继续决定其余附件；全部明确才统一发布并完成。无需逐份询问收录。"
    params = {"type": "object", "properties": {
        "task_id": {"type": "string"}, "attachment_id": {"type": "string"},
        "path": {"type": "string"}, "purpose": {"type": "string", "enum": ["long_term", "transient", "waiting"]},
        "document_id": {"type": "string"}, "expected_current_revision": {"type": ["string", "null"]},
        "reason": {"type": "string"}, "summary": {"type": "string"}},
        "required": ["task_id", "purpose", "reason"]}

    def __init__(self, config=None):
        self.config = config or {}

    def execute(self, args):
        owner = current_identity().user_id
        if owner != "local-owner":
            return ToolResult.fail("forbidden")
        store = self.config.get("task_store") or TaskStore()
        task_id = args.get("task_id")
        from common.personal_tasks import current_task_id
        if current_task_id() and task_id != current_task_id():
            return ToolResult.fail("forbidden")
        attempt, stage = None, "decide"
        try:
            task = store.get(owner, task_id)
            if task["status"] == "succeeded":
                return ToolResult.success(task)
            attempt = task["attempt"]
            purpose = args.get("purpose")
            if purpose not in ("long_term", "transient", "waiting") or not args.get("reason"):
                raise ValueError("invalid_request")
            attachments = task["attachments"]
            if not attachments:
                raise ValueError("invalid_request: 任务尚无已保存附件")
            _check_batch_limits(attachments, task.get("attachment_decisions") or {})
            if args.get("attachment_id"):
                selected = [store.attachment(owner, task_id, args["attachment_id"])]
            elif args.get("path"):
                selected = [a for a in attachments if Path(a["path"]).resolve() == Path(args["path"]).resolve()]
                if not selected:
                    raise ValueError("forbidden")
            else:
                selected = attachments
            workspace = self.config.get("workspace_root") or self.config.get("cwd")
            if not workspace:
                from common import state_dir
                workspace = state_dir.state_root()
            service = SourceService(workspace, owner)
            # Serialize decision merges with cancellation and concurrent subset
            # calls. Candidates remain private until every attachment is decided.
            with store.commit_guard(owner, task_id, attempt):
                task = store.get(owner, task_id)
                decisions = dict(task.get("attachment_decisions") or {})
                if task.get("source_candidates"):
                    stage = "pending_index"
                    return ToolResult.success(resume_publication(task, store))
                for entry in selected:
                    attachment_id = entry["attachment_id"]
                    previous = decisions.get(attachment_id)
                    if previous and previous["purpose"] == purpose and previous.get("ready"):
                        continue
                    attachment = store.attachment(owner, task_id, attachment_id)
                    # Reuse the immutable source association across clarification,
                    # retries and purpose changes; never rebind an old receipt.
                    source = previous.get("source") if previous else None
                    if source is None:
                        source = service.receive(attachment["path"], filename=attachment["filename"],
                            request_id=f"{task_id}:{attachment_id}", origin=task["origin"], document_id=args.get("document_id"))
                    elif args.get("document_id") and args["document_id"] != source["document_id"]:
                        raise SourceError("version_conflict", "附件已关联另一资料，不能改写已登记来源")
                    doc = service.get(source["document_id"])
                    existing = service.current_publication(doc["id"], source["revision_id"])
                    decision = dict(attachment_id=attachment_id, purpose=purpose, reason=args["reason"],
                                    source=source, workspace=str(service.workspace), ready=False)
                    if source["duplicate"] and purpose == "long_term" and existing:
                        decision.update(result_ref=dict(existing, outcome="duplicate", attachment_id=attachment_id,
                                                       purpose=purpose), ready=True)
                    else:
                        if doc["purpose"] != purpose:
                            service.change(doc["id"], purpose=purpose, expected_state=doc["state"], reason=args["reason"])
                        decision["state_revision"] = service.get(doc["id"])["state"]
                        decision["expected_current_revision"] = args.get("expected_current_revision")
                        decision["summary"] = args.get("summary")
                        if purpose == "transient":
                            decision.update(result_ref=dict(source, outcome="transient", purpose=purpose,
                                                            attachment_id=attachment_id, searchable=False), ready=True)
                    decisions[attachment_id] = decision
                    # Preserve the chosen purpose even if parsing fails next.
                    store._change(owner, task_id, attempt, {"running"}, attachment_decisions=decisions,
                                  source_candidates=[])
                    if purpose == "long_term" and not decision["ready"]:
                        stage = "parse"
                        store.stage(owner, task_id, attempt, stage)
                        candidate = service.prepare(source["revision_id"],
                            expected_current_revision=decision["expected_current_revision"],
                            summary=decision["summary"], task_id=task_id)
                        decision.update(candidate_id=candidate["candidate_id"], ready=True)
                        decision["parsed_bytes"] = sum(len(unit["text"].encode("utf-8")) for unit in candidate["parse"]["units"])
                        store._change(owner, task_id, attempt, {"running"}, attachment_decisions=decisions)
                        _check_batch_limits(attachments, decisions)
                task = store.get(owner, task_id)
                undecided = [a["attachment_id"] for a in attachments
                             if not decisions.get(a["attachment_id"], {}).get("ready")]
                waiting = [d for d in decisions.values() if d["purpose"] == "waiting"]
                if waiting:
                    store._change(owner, task_id, attempt, {"running"}, source_candidates=[])
                    return ToolResult.success(store.wait_input(owner, task_id, attempt,
                        "；".join(d["reason"] for d in waiting)))
                if undecided:
                    return ToolResult.success(store._change(owner, task_id, attempt, {"running"},
                        stage="decide", source_candidates=[], pending_attachment_ids=undecided))
                candidates = [dict(candidate_id=d["candidate_id"], workspace=d["workspace"], attachment_id=d["attachment_id"])
                              for d in decisions.values() if d.get("candidate_id")]
                stage = "pending_index"
                task = store._change(owner, task_id, attempt, {"running"}, stage=stage,
                    source_candidates=candidates, pending_attachment_ids=[])
                return ToolResult.success(resume_publication(task, store))
        except Exception as exc:
            code = getattr(exc, "code", "storage_failed" if isinstance(exc, OSError) else "invalid_request")
            if attempt is not None:
                try:
                    store.fail(owner, task_id, attempt, code, stage)
                except ValueError:
                    pass
            return ToolResult.fail({"code": code, "stage": stage, "message": str(exc)})


def resume_publication(task, store):
    """Resume a fully decided batch without parsing or losing transient receipts.

    Incomplete/waiting batches cannot publish, even after restart. Older
    task checkpoints without per-attachment decisions retain their old path.
    """
    if "attachment_decisions" not in task:
        return resume_legacy_publication(task, store)
    owner, task_id, attempt = task["owner_id"], task["task_id"], task["attempt"]
    with store.commit_guard(owner, task_id, attempt):
        current = store.get(owner, task_id)
        decisions = current.get("attachment_decisions") or {}
        _check_batch_limits(current["attachments"], decisions)
        entries = [decisions.get(a["attachment_id"]) for a in current["attachments"]]
        if not entries or any(not d or not d.get("ready") or d["purpose"] not in ("long_term", "transient") for d in entries):
            raise SourceError("version_conflict", "附件用途尚未全部明确，禁止发布候选")
        # Validate all decisions before the first publication. In particular,
        # a cancelled/withdrawn stale transient decision cannot mask a change.
        for decision in entries:
            service = SourceService(decision["workspace"], owner)
            doc = service.get(decision["source"]["document_id"])
            if doc["purpose"] != decision["purpose"]:
                raise SourceError("version_conflict", "附件用途状态已变化")
            if "state_revision" in decision and doc["state"] != decision["state_revision"]:
                raise SourceError("version_conflict", "附件状态修订已变化")
            if decision.get("result_ref", {}).get("outcome") == "duplicate":
                service.read_reference(decision["result_ref"]["publish_id"], 0)
        refs = []
        for decision in entries:
            if decision.get("candidate_id"):
                ref = SourceService(decision["workspace"], owner).publish(decision["candidate_id"])
                ref = dict(ref, attachment_id=decision["attachment_id"], purpose=decision["purpose"])
            else:
                ref = decision["result_ref"]
            refs.append(ref)
        outcome = "published" if any(r["outcome"] == "published" for r in refs) else (
            "duplicate" if all(r["outcome"] == "duplicate" for r in refs) else "transient")
        return store.complete(owner, task_id, attempt, outcome, refs)
