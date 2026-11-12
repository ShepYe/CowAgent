"""The main Agent decides purpose; trusted task records authorize attachments."""
from pathlib import Path
from agent.tools.base_tool import BaseTool, ToolResult
from agent.knowledge.personal_assistant import SourceService, resume_publication as resume_legacy_publication
from agent.knowledge.parser import SourceError
from common.personal_tasks import TaskStore
from common.runtime_identity import current_identity

MAX_BATCH_FILES = 100
MAX_BATCH_TEXT_BYTES = 200 * 1024 * 1024

READING_SOURCE_SCHEMA = {'type': 'object', 'properties': {
    'reading_id': {'type': 'string'}, 'evidence_key': {'type': 'string'}},
    'required': ['reading_id', 'evidence_key'], 'additionalProperties': False}


def _check_batch_limits(attachments, decisions):
    if len(attachments) > MAX_BATCH_FILES or sum(d.get("parsed_bytes", 0) for d in decisions.values()) > MAX_BATCH_TEXT_BYTES:
        raise SourceError("limit_exceeded", "资料发布批次超过 100 件或 200 MiB 文本预算")


class KnowledgeIngest(BaseTool):
    name = "knowledge_ingest"
    description = "受控收录资料并发布模型整理的主题知识页。接收任务须给 task_id；long_term 无 pages 时仅登记用途，须继续读取资料、提炼并提交 pages。普通会话基于已登记有效来源继续提炼时可省略 task_id，提交带出处的 long_term pages。transient 仅留档，waiting 仅必要澄清。支持跨来源、拆页与更新已有页；只有有效且索引核验通过才能报告可检索。"
    params = {"type": "object", "properties": {
        "task_id": {"type": "string"}, "attachment_id": {"type": "string"},
        "path": {"type": "string"}, "purpose": {"type": "string", "enum": ["long_term", "transient", "waiting"]},
        "document_id": {"type": "string", "description": "沿用 read 返回的资料 ID；不能改绑已有附件。"},
        "expected_current_revision": {"type": ["string", "null"], "description": "使用 read 返回的 expected_current_revision（当前已发布版本），不是内容 revision_id；首次发布为空。"},
        "reason": {"type": "string"}, "summary": {"type": "string", "description": "兼容旧参数；不能替代整理后的 pages。"},
        "pages": {"type": "array", "description": "完整整理成品；每个正文块带已读取原文出处。更新页须附 page_id 和 expected_version。", "items": {
            "type": "object", "properties": {
                "page_id": {"type": "string"}, "expected_version": {"type": ["string", "null"]},
                "title": {"type": "string"}, "category": {"type": "string"},
                "summary": {"type": "string", "minLength": 1, "maxLength": 160, "description": "必填：本页正文的一句话摘要，1～160个Unicode码点，单行纯文本，无控制字符。"},
                "blocks": {"type": "array", "items": {"type": "object", "properties": {
                    "content": {"type": "string"}, "sources": {"type": "array", "items": {
                        "type": "object", "properties": {"document_id": {"type": "string"},
                            "revision_id": {"type": "string"}, "parse_id": {"type": "string"},
                            "ordinal": {"type": "integer"}}, "required": ["document_id", "revision_id", "parse_id", "ordinal"]}}},
                    "required": ["content", "sources"]}},
                "related_page_ids": {"type": "array", "items": {"type": "string"}},
                "depends_on": {"type": "array", "items": {"type": "object", "properties": {
                    "page_id": {"type": "string"}, "page_version": {"type": "string"}}, "required": ["page_id", "page_version"]}}},
            "required": ["title", "category", "summary", "blocks"]}}},
        "required": ["purpose", "reason"]}

    def __init__(self, config=None):
        self.config = config or {}
        import copy
        self.params = copy.deepcopy(type(self).params)
        properties = self.params['properties']
        properties.update(reading_submission_id={'type': 'string'}, reading_evidence={
            'type': 'array', 'items': {'type': 'object', 'properties': {
                'reading_id': {'type': 'string'}, 'evidence_key': {'type': 'string'},
                'segment_id': {'type': 'string'}, 'location': {'type': 'object'},
                'observation': {'type': 'string'},
                'type': {'type': 'string', 'enum': ['ocr', 'visual_interpretation']}},
                'required': ['reading_id', 'evidence_key', 'segment_id', 'location', 'observation', 'type'],
                'additionalProperties': False}}, reading_coverage={
            'type': 'array', 'items': {'type': 'object', 'properties': {
                'reading_id': {'type': 'string'}, 'segment_id': {'type': 'string'}},
                'required': ['reading_id', 'segment_id'], 'additionalProperties': False}})
        source = properties['pages']['items']['properties']['blocks']['items']['properties']['sources']
        source['items'] = {'oneOf': [dict(source['items'], additionalProperties=False), READING_SOURCE_SCHEMA]}

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
            if 'pages' in args:
                from agent.knowledge.wiki import validate_page_summaries
                validate_page_summaries(args['pages'])
            if not task_id:
                if args.get("purpose") != "long_term" or not args.get("pages"):
                    raise SourceError("invalid_request", "没有接收任务时须提交带已登记有效出处的长期知识 pages")
                import hashlib
                import json
                from models.config_revision import capture_config_revision
                origin = dict(channel="agent", session_id=current_identity().session_id)
                fingerprint = hashlib.sha256(json.dumps([args["pages"], origin], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                task = store.submit(owner, "wiki-synthesis:" + fingerprint,
                    {"text": args.get("reason", "知识提炼"), "knowledge_synthesis": True}, origin, capture_config_revision())
                task_id = task["task_id"]
                if task["status"] == "queued":
                    task = store.claim(owner, task_id)
            task = store.get(owner, task_id)
            if task["status"] == "succeeded":
                if task.get('reading_publication') and args.get('pages'):
                    submission = args.get('reading_submission_id')
                    expected = (task.get('reading_tool_submissions') or {}).get(submission)
                    if expected != _reading_payload(args):
                        raise SourceError('idempotency_conflict', '已完成任务的图文提交与原载荷不符')
                refs = []
                for ref in task.get("result_refs", []):
                    display = dict(ref)
                    if ref.get('page_id'):
                        display.setdefault('summary', '')
                    if ref.get("publish_id"):
                        workspace = self.config.get("workspace_root") or self.config.get("cwd")
                        if not workspace:
                            from common import state_dir
                            workspace = state_dir.state_root()
                        current = SourceService(workspace, owner).read_reference(ref["publish_id"], ref.get("source_ordinal", 0))
                        if ref.get('page_id'):
                            display.update({key: current[key] for key in ('category', 'category_mode', 'category_revision')})
                    refs.append(display)
                return ToolResult.success(dict(task, result_refs=refs))
            attempt = task["attempt"]
            purpose = args.get("purpose")
            if purpose not in ("long_term", "transient", "waiting") or not args.get("reason"):
                raise ValueError("invalid_request")
            if task.get('reading_publication') and args.get('pages'):
                return self._reading_submission(store, task, args)
            attachments = task["attachments"]
            if not attachments:
                if not args.get("pages") or not task.get("payload", {}).get("knowledge_synthesis"):
                    raise ValueError("invalid_request: 任务尚无已保存附件")
                from agent.knowledge.wiki import WikiService
                from common import state_dir
                workspace = self.config.get("workspace_root") or self.config.get("cwd") or state_dir.state_root()
                with store.commit_guard(owner, task_id, attempt):
                    batch = WikiService(workspace, owner).prepare(args["pages"], task_id=task_id)
                    task = store._change(owner, task_id, attempt, {"running"}, stage="pending_index",
                        wiki_batch_id=batch["batch_id"], wiki_workspace=str(workspace))
                    stage = "pending_index"
                    return ToolResult.success(resume_publication(task, store))
            _check_batch_limits(attachments, task.get("attachment_decisions") or {})
            if args.get("attachment_id"):
                selected = [store.attachment(owner, task_id, args["attachment_id"])]
            elif args.get("path"):
                selected_path = Path(args['path']).expanduser()
                if not selected_path.is_absolute():
                    selected_path = Path(self.config.get('cwd') or self.config.get('workspace_root') or '.') / selected_path
                selected = [a for a in attachments if Path(a["path"]).resolve() == selected_path.resolve()]
                if not selected:
                    raise ValueError("forbidden")
            else:
                selected = attachments
            workspace = self.config.get("workspace_root") or self.config.get("cwd")
            if not workspace:
                from common import state_dir
                workspace = state_dir.state_root()
            service = SourceService(workspace, owner)
            from agent.knowledge.reading_flow import enabled, office_enabled
            # Serialize decision merges with cancellation and concurrent subset
            # calls. Candidates remain private until every attachment is decided.
            with store.commit_guard(owner, task_id, attempt):
                task = store.get(owner, task_id)
                decisions = dict(task.get("attachment_decisions") or {})
                if task.get("wiki_batch_id") or task.get("source_candidates"):
                    stage = "pending_index"
                    return ToolResult.success(resume_publication(task, store))
                if args.get("pages") and len(decisions) == len(attachments):
                    # 提交整批成品时沿用各附件用途，避免混合批次中的临时附件被改为长期。
                    selected = []
                for entry in selected:
                    attachment_id = entry["attachment_id"]
                    previous = decisions.get(attachment_id)
                    if previous and previous["purpose"] == purpose and (previous.get("ready") or previous.get("parse_id")
                                                                         or previous.get('native_preparation_pending')):
                        doc = service.get(previous["source"]["document_id"])
                        if doc["state"] != previous["state_revision"] or doc["purpose"] != purpose:
                            raise SourceError("version_conflict", "附件用途或状态已变化")
                        continue
                    attachment = store.attachment(owner, task_id, attachment_id)
                    # Reuse the immutable source association across clarification,
                    # retries and purpose changes; never rebind an old receipt.
                    source = previous.get("source") if previous else None
                    if source is None:
                        target_document = args.get("document_id")
                        reorganize = task.get("payload", {}).get("reorganize") or task.get("payload", {}).get("source_action")
                        if reorganize:
                            target_document = reorganize["document_id"]
                            current = service.get(target_document)
                            registered = service.registered_revision(target_document, reorganize["revision_id"])
                            expected_state = (task.get("source_action_checkpoint") or {}).get("state", reorganize["expected_state"])
                            if (current["state"] != expected_state or
                                    current["purpose"] != "long_term" or current["validity"] != "active" or
                                    registered["sha256"] != attachment["sha256"] or
                                    (args.get("document_id") and args["document_id"] != target_document)):
                                raise SourceError("version_conflict", "重新整理的原件或来源状态已改变")
                        source = service.receive(attachment["path"], filename=attachment["filename"],
                            request_id=f"{task_id}:{attachment_id}", origin=task["origin"], document_id=target_document)
                    elif args.get("document_id") and args["document_id"] != source["document_id"]:
                        raise SourceError("version_conflict", "附件已关联另一资料，不能改写已登记来源")
                    doc = service.get(source["document_id"])
                    expected = args.get("expected_current_revision")
                    if (task.get("payload", {}).get("reorganize") or task.get("payload", {}).get("source_action")) and expected is None:
                        # 受信重新整理指令已核验不可变原件和 expected_state。
                        expected = doc["current"]
                    # Older read results exposed only the content revision.
                    # Accept that echo solely for a pristine, unpublished doc;
                    # never relax checks after publication or a state change.
                    if (doc["state"] == 0 and doc["purpose"] == "waiting" and
                            doc["validity"] == "active" and doc["current"] is None and
                            expected == source["revision_id"]):
                        expected = None
                    if purpose == "long_term" and (doc["validity"] != "active" or doc["current"] != expected):
                        raise SourceError("version_conflict", "来源已失效或当前版本已改变，须核对后重新整理")
                    decision = dict(attachment_id=attachment_id, purpose=purpose, reason=args["reason"],
                                    source=source, workspace=str(service.workspace), ready=False)
                    if doc["purpose"] != purpose:
                        service.change(doc["id"], purpose=purpose, expected_state=doc["state"], reason=args["reason"])
                    decision["state_revision"] = service.get(doc["id"])["state"]
                    decision["expected_current_revision"] = expected
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
                        if office_enabled(self.config, source['filename']):
                            # 先登记用途；在 read 中转换并固定阅读副本后建立解析身份。
                            decision.update(native_preparation_pending=True, parsed_bytes=0)
                        else:
                            pure_pdf = (enabled(self.config) and Path(source['filename']).suffix.lower() == '.pdf')
                            parsed = service.parse(source["revision_id"], prepare_only=pure_pdf)
                            decision.update(parse_id=parsed["parse_id"], visual_coverage=parsed.get("visual_coverage"))
                            decision["parsed_bytes"] = sum(len(unit["text"].encode("utf-8")) for unit in parsed["units"])
                        store._change(owner, task_id, attempt, {"running"}, attachment_decisions=decisions)
                        _check_batch_limits(attachments, decisions)
                task = store.get(owner, task_id)
                undecided = [a["attachment_id"] for a in attachments if a["attachment_id"] not in decisions]
                waiting = [d for d in decisions.values() if d["purpose"] == "waiting"]
                if waiting:
                    store._change(owner, task_id, attempt, {"running"}, source_candidates=[])
                    return ToolResult.success(store.wait_input(owner, task_id, attempt,
                        "；".join(dict.fromkeys(d["reason"] for d in waiting))))
                if undecided:
                    return ToolResult.success(store._change(owner, task_id, attempt, {"running"},
                        stage="decide", source_candidates=[], pending_attachment_ids=undecided))
                long_term = [d for d in decisions.values() if d["purpose"] == "long_term"]
                if long_term:
                    pages = args.get("pages")
                    if not pages:
                        task = store._change(owner, task_id, attempt, {"running"}, stage="decide", pending_attachment_ids=[])
                        return ToolResult.success(dict(task, organization_required=True,
                            source_materials=[dict(d["source"], parse_id=d.get("parse_id"), state_revision=d["state_revision"],
                                visual_coverage=d.get("visual_coverage")) for d in long_term],
                            next_action="实际读取 knowledge-wiki 和原文，按主题提炼后携带完整 pages 再调用本工具；无需向用户询问是否收录。"
                                + ("PDF原生首次读取只传path/pages/visual=true，不传parse_id、offset或limit；"
                                   "返回reading_id后按segment和真实观察提交reading_evidence及reading_coverage。"
                                   if enabled(self.config) or any(office_enabled(self.config, d['source']['filename'])
                                                                 for d in long_term) else '')))
                    cited = {s.get("document_id") for p in pages for b in p.get("blocks", []) for s in b.get("sources", [])}
                    if any(d["source"]["document_id"] not in cited for d in long_term):
                        raise SourceError("invalid_request", "整理成品缺少本次长期资料的出处")
                    from agent.knowledge.wiki import WikiService
                    batch = WikiService(workspace, owner).prepare(pages, task_id=task_id)
                    for decision in long_term:
                        decision["ready"] = True
                    task = store._change(owner, task_id, attempt, {"running"}, stage="pending_index",
                        attachment_decisions=decisions, wiki_batch_id=batch["batch_id"], wiki_workspace=str(workspace),
                        source_candidates=[], pending_attachment_ids=[])
                    stage = "pending_index"
                    return ToolResult.success(resume_publication(task, store))
                stage = "pending_index"
                task = store._change(owner, task_id, attempt, {"running"}, stage=stage,
                    source_candidates=[], pending_attachment_ids=[])
                return ToolResult.success(resume_publication(task, store))
        except Exception as exc:
            code = getattr(exc, "code", "storage_failed" if isinstance(exc, OSError) else "invalid_request")
            if attempt is not None and not (stage == "decide" and code in {
                    "invalid_request", "version_conflict", "forbidden", "reading_incomplete", "idempotency_conflict"}):
                try:
                    store.fail(owner, task_id, attempt, code, stage)
                except ValueError:
                    pass
            return ToolResult.fail({"code": code, "stage": stage, "message": str(exc)})

    def _reading_submission(self, store, task, args):
        """媒体出处先还原成既有四元身份，再走现有Wiki校验及发布。"""
        import copy
        from agent.knowledge.reading import ReadingService
        from agent.knowledge.reading_publication import prepare_publication, publish_batch, finish_publication
        from common import state_dir
        if args['purpose'] != 'long_term':
            raise SourceError('invalid_request', '图文主题发布须沿用长期用途')
        workspace = self.config.get('workspace_root') or self.config.get('cwd') or state_dir.state_root()
        submission = args.get('reading_submission_id')
        if not isinstance(submission, str) or not submission.strip():
            raise SourceError('invalid_request', '图文发布须有reading_submission_id')
        registry = ReadingService(workspace, task_store=store)
        coverage_items = args.get('reading_coverage')
        if not isinstance(coverage_items, list) or not coverage_items:
            raise SourceError('invalid_request', '须明确本批已整理范围reading_coverage')
        coverage = set()
        for item in coverage_items:
            if not isinstance(item, dict) or set(item) != {'reading_id', 'segment_id'}:
                raise SourceError('invalid_request', '阅读覆盖形状无效')
            record = registry.get(task, item['reading_id'])
            segment = next((s for s in record['segments'] if s['segment_id'] == item['segment_id']), None)
            if segment is None or segment['state'] != 'completed':
                raise SourceError('reading_incomplete', '阅读范围尚未成功交付主模型')
            coverage.update(registry.coverage(task, item['reading_id'], item['segment_id']))
        pages = copy.deepcopy(args['pages'])
        evidence = args.get('reading_evidence', [])
        wanted = set()
        for page in pages:
            for block in page.get('blocks', []):
                for source in block.get('sources', []):
                    if isinstance(source, dict) and set(source) == {'reading_id', 'evidence_key'}:
                        wanted.add((source['reading_id'], source['evidence_key']))
                    elif not isinstance(source, dict) or set(source) != {'document_id', 'revision_id', 'parse_id', 'ordinal'}:
                        raise SourceError('invalid_request', '出处须为四元身份或互斥阅读别名')
        if not isinstance(evidence, list) or any(not isinstance(e, dict) for e in evidence):
            raise SourceError('invalid_request', '阅读观察数组无效')
        if wanted != {(e.get('reading_id'), e.get('evidence_key')) for e in evidence}:
            raise SourceError('invalid_request', '图文观察与正文出处不对应')
        with store.commit_guard(task['owner_id'], task['task_id'], task['attempt']):
            current = store.get(task['owner_id'], task['task_id'])
            submissions = dict(current.get('reading_tool_submissions') or {})
            payload = _reading_payload(args)
            if submission in submissions and submissions[submission] != payload:
                raise SourceError('idempotency_conflict', '同一图文提交标识载荷不同')
            aliases = registry.resolve_evidence(task, submission, evidence, submission_payload=payload)
            used = set()
            for page in pages:
                for block in page.get('blocks', []):
                    sources = []
                    for source in block.get('sources', []):
                        if isinstance(source, dict) and set(source) == {'reading_id', 'evidence_key'}:
                            key = (source['reading_id'], source['evidence_key'])
                            if key not in aliases:
                                raise SourceError('invalid_request', '图文别名未提供同提交证据')
                            sources.append(aliases[key])
                            used.add(key)
                        elif isinstance(source, dict) and set(source) == {'document_id', 'revision_id', 'parse_id', 'ordinal'}:
                            sources.append(source)
                        else:
                            raise SourceError('invalid_request', '出处须为四元身份或互斥阅读别名')
                    block['sources'] = sources
            if used != set(aliases):
                raise SourceError('invalid_request', '图文观察与正文出处不对应')
            prepare_publication(store, task, submission, sorted(coverage), pages, workspace)
            submissions[submission] = payload
            store._change(task['owner_id'], task['task_id'], task['attempt'], {'running'},
                          reading_tool_submissions=submissions)
            publish_batch(store, task, submission)
            result = finish_publication(store, task)
            return ToolResult.success(result)


def _reading_payload(args):
    import hashlib
    import json
    payload = {k: args.get(k) for k in ('pages', 'reading_evidence', 'reading_coverage', 'purpose')}
    for key in ('reading_evidence', 'reading_coverage'):
        if isinstance(payload[key], list):
            payload[key] = sorted(payload[key], key=lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False))
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def resume_publication(task, store):
    """Resume a fully decided batch without parsing or losing transient receipts.

    Incomplete/waiting batches cannot publish, even after restart. Older
    task checkpoints without per-attachment decisions retain their old path.
    """
    if task.get("wiki_batch_id"):
        from agent.knowledge.wiki import WikiService
        owner, task_id, attempt = task["owner_id"], task["task_id"], task["attempt"]
        with store.commit_guard(owner, task_id, attempt):
            current = store.get(owner, task_id)
            decisions = current.get("attachment_decisions") or {}
            for decision in decisions.values():
                doc = SourceService(decision["workspace"], owner).get(decision["source"]["document_id"])
                if doc["state"] != decision["state_revision"] or doc["purpose"] != decision["purpose"]:
                    raise SourceError("version_conflict", "来源用途或有效状态已变化")
            refs = WikiService(current["wiki_workspace"], owner).publish(current["wiki_batch_id"])
            refs.extend(d["result_ref"] for d in decisions.values() if d["purpose"] == "transient")
            return store.complete(owner, task_id, attempt, "published", refs)
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
