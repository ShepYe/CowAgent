"""隔离生产 PDF Agent 入库实测；只写新建 tmp 子根，绝不打开真实知识库。

运行 --config 指向个人配置；请求报告只有类型、哈希、大小、时延和 usage。
异常写入失败检查点后继续抛出；题库答案不进入模型提示。
"""
import argparse
import base64
import hashlib
import json
import os
import re
from pathlib import Path
import sys
import time
from datetime import datetime, timezone


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_probe_error(error, secrets=()):
    """仅无敏感能力probe诊断；不向报告写请求或原始错误对象。"""
    error = error if isinstance(error, dict) else {"message": str(error)}
    message = str(error.get("message") or "")
    for secret in secrets:
        if isinstance(secret, str) and len(secret) >= 6:
            message = message.replace(secret, "[redacted]")
    message = re.sub(r"data:[^\s\"']+", "[data-uri]", message, flags=re.I)
    message = re.sub(r"(?:Bearer\s+|sk-)[A-Za-z0-9_.-]+", "[token]", message, flags=re.I)
    message = re.sub(r"[A-Za-z0-9+/=_-]{64,}", "[encoded]", message)
    message = re.sub(r"[A-Za-z]:[\\/][^\s\"']+|(?:/[^/\s]+){2,}", "[path]", message)
    safe = {"message": message[:500]}
    for key in ("code", "type"):
        value = str(error.get(key) or "")
        safe[key] = value if re.fullmatch(r"[A-Za-z0-9_.-]{0,80}", value) else "redacted"
    return safe


def run(config_path, output, pdf_path, golden_path, *, resume=False, output_budget=None, max_steps=12,
        omit_prompt_cache_key=False, soffice=None, pdf_max_pages=None):
    output = Path(output).resolve()
    workspace = output / "runtime"
    if workspace.exists() and not resume:
        raise ValueError("隔离输出已存在，请使用新的输出目录")
    workspace.mkdir(parents=True, exist_ok=resume)
    os.environ["COW_DATA_DIR"] = str(workspace)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from config import Config, config_scope
    from common.log import logger
    from common.personal_tasks import TaskStore, OWNER, task_scope
    from common.runtime_identity import identity_scope
    from models.config_revision import capture_config_revision, use_config_revision
    from models.openai.openai_http_client import OpenAIHTTPClient
    from agent.protocol import Agent
    from agent.tools.read.read import Read
    from agent.tools.knowledge_ingest import KnowledgeIngest
    from agent.tools.memory.memory_search import MemorySearchTool
    from agent.tools.tool_manager import ToolManager
    from agent.memory.manager import MemoryManager
    from agent.memory.config import MemoryConfig
    from agent.knowledge.wiki import WikiService
    from agent.knowledge.personal_assistant import SourceService
    from bridge.agent_bridge import AgentLLMModel
    from bridge.bridge import Bridge
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    # 隔离知识数据不应丢失实际模型预算；只复制无密钥的模型目录配置。
    catalog = Path(os.path.expanduser(cfg.get('agent_workspace') or '~/cow')) / 'system/models.json'
    if catalog.is_file():
        (workspace / 'system').mkdir(exist_ok=True)
        (workspace / 'system/models.json').write_text(catalog.read_text(encoding='utf-8'), encoding='utf-8')
    if output_budget is not None:
        if type(output_budget) is not int or output_budget <= 0:
            raise ValueError('output_budget 须为正整数')
        local_catalog = workspace / 'system/models.json'
        catalog_data = json.loads(local_catalog.read_text(encoding='utf-8'))
        overrides = catalog_data['providers'][cfg['bot_type']]['overrides']
        entry = next(item for item in overrides if item['name'] == cfg['model'])
        entry['max_output_tokens'] = output_budget
        local_catalog.write_text(json.dumps(catalog_data, ensure_ascii=False), encoding='utf-8')
        # 此诊断使用不同处理配置，不沿用原预算的能力身份。
        cfg['document_reading_probe_output_budget'] = output_budget
    cfg.update(agent_workspace=str(workspace), chat_fallback={"chain": []}, debug=False,
               document_reading_native_pdf_enabled=True, knowledge=True,
               embedding_enabled=False, knowledge_semantic_enabled=False)
    if omit_prompt_cache_key:
        cfg['responses_prompt_cache_enabled'] = False
    if pdf_max_pages is not None:
        cfg['document_reading_probe_pdf_max_pages'] = pdf_max_pages
    suffix = Path(pdf_path).suffix.lower()
    if suffix in ('.docx', '.pptx'):
        cfg[f'document_reading_native_{suffix[1:]}_enabled'] = True
        if soffice:
            cfg['document_reading_soffice'] = str(Path(soffice).resolve())
    # Presence prevents shared-root fallback for every relevant storage family.
    for folder in ("knowledge", "skills", "memory"):
        (workspace / folder).mkdir(exist_ok=True)
    installed_skill = workspace / 'skills/knowledge-wiki/SKILL.md'
    installed_skill.parent.mkdir(exist_ok=True)
    installed_skill.write_text(Path('skills/knowledge-wiki/SKILL.md').read_text(encoding='utf-8'), encoding='utf-8')
    logger.setLevel("WARNING")
    golden = json.loads(Path(golden_path).read_text(encoding="utf-8"))
    samples = golden["samples"]
    questions = [dict(id=s["id"], page=s["sample_pdf_page"], question=s["question"]) for s in samples]
    report = dict(schema_version=1, started_at=datetime.now(timezone.utc).isoformat(),
        input_sha256=digest(Path(pdf_path).read_bytes()), model=cfg["model"],
        requests=[], calls=[], failures=[], production_library_touched=False,
        golden_item_count=sum(len(s["required_answer_items"]) for s in samples),
        golden_coverage_gaps=golden["coverage_gaps"], v26_passed=False)
    if output_budget is not None:
        report['isolated_output_budget_override'] = output_budget
    if omit_prompt_cache_key:
        report['isolated_omit_prompt_cache_key'] = True
    if resume:
        previous = output / 'report.json'
        report = json.loads(previous.read_text(encoding='utf-8'))
        if report['input_sha256'] != digest(Path(pdf_path).read_bytes()):
            raise ValueError('续接原件不一致')
        report.setdefault('resumed_at', []).append(datetime.now(timezone.utc).isoformat())
        for key in ('finished_at', 'exception_type', 'failure_code'):
            report.pop(key, None)
    phase = "setup"
    def save_report():
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    save_report()
    from agent.protocol.tool_media import media_request_error
    probe_secrets = [v for k,v in cfg.items() if any(word in k.lower() for word in ("key", "token", "secret")) and isinstance(v,str)]
    probe_secrets.extend(p.get("api_key") for p in cfg.get("custom_providers", []))
    original_http = OpenAIHTTPClient.responses
    original_sync = ToolManager.sync_mcp_into_agent

    def traced_http(client, **kwargs):
        row = dict(phase=phase, protocol="responses", streaming=bool(kwargs.get("stream")),
            input_types=[i.get("type", i.get("role")) for i in kwargs.get("input", [])],
            function_names=[i.get("name") for i in kwargs.get("input", []) if i.get("type") == "function_call"],
            media=[], usage=None,
            tool_definition_count=len(kwargs.get("tools") or []),
            max_output_tokens=kwargs.get("max_output_tokens"), reasoning=kwargs.get("reasoning"))
        row['tool_schema_keys'] = {t.get('name'): sorted((t.get('parameters') or {}).get('properties') or {})
                                   for t in kwargs.get('tools') or []}
        for item in kwargs.get("input", []):
            for part in item.get("output", []) if isinstance(item.get("output"), list) else []:
                if part.get("type") == "input_file":
                    raw = base64.b64decode(part["file_data"].split(",", 1)[1])
                    row["media"].append(dict(type="input_file", sha256=digest(raw), bytes=len(raw), call_id=item.get("call_id")))
                elif part.get("type") == "input_image":
                    row["media"].append(dict(type="input_image"))
        safe_body = {k:v for k,v in kwargs.items() if k not in ("api_key", "api_base", "timeout")}
        encoded = json.dumps(safe_body, ensure_ascii=False, sort_keys=True).encode()
        row.update(request_sha256=digest(encoded), request_bytes=len(encoded))
        report["requests"].append(row)
        save_report()
        start = time.perf_counter()
        def stream():
            try:
                for event in original_http(client, **kwargs):
                    response = event.get("response") or {}
                    if event.get("error") or response.get("error"):
                        safe_error = media_request_error(event.get("error") or response.get("error"), event.get("status_code"))
                        row["safe_error"] = dict(code=safe_error["error"]["code"], status_code=safe_error["status_code"])
                        row["probe_error"] = safe_probe_error(event.get("error") or response.get("error"), probe_secrets)
                    if event.get("type") == "response.output_text.delta":
                        row["output_text_chars"] = row.get("output_text_chars", 0) + len(event.get("delta") or "")
                    if response:
                        row["response_status"] = response.get("status")
                        row["response_id_present"] = bool(response.get("id"))
                        row["output_types"] = [i.get("type") for i in response.get("output", [])]
                    if event.get("type") == "response.output_item.added" and event.get("item", {}).get("type") == "function_call":
                        row.setdefault("requested_tools", []).append(event["item"].get("name"))
                    if response.get("usage"):
                        row["usage"] = response["usage"]
                    if event.get("type") in ("response.completed", "response.failed", "response.incomplete", "error"):
                        row["terminal"] = event["type"]
                    yield event
            except GeneratorExit:
                row["stream_closed"] = True
                raise
            except BaseException as exc:
                row["exception_type"] = type(exc).__name__
                row["exception_module"] = type(exc).__module__
                if phase == "capability":
                    safe = media_request_error(exc)
                    row["safe_error"] = dict(code=safe["error"]["code"], status_code=safe["status_code"])
                raise
            finally:
                row["elapsed_s"] = round(time.perf_counter() - start, 3)
                save_report()
        return stream()

    def event(envelope):
        kind, data = envelope["type"], envelope["data"]
        if kind == "tool_execution_start":
            # 探针仅固定无敏感夹具；保留实际结构参数，诊断工具调用而非猜测。
            args = data.get('arguments') or {}
            report.setdefault('tool_inputs', []).append(dict(phase=phase, name=data.get('tool_name'),
                arguments={k:v for k,v in args.items() if k in (
                    'task_id', 'attachment_id', 'path', 'purpose', 'reason', 'pages', 'visual',
                    'offset', 'limit', 'parse_id') and not isinstance(v, (list, dict))}))
            save_report()
        if kind == "tool_execution_end":
            result = data.get("result")
            row = dict(phase=phase, name=data.get("tool_name"), status=data.get("status"), elapsed_s=data.get("execution_time"))
            if isinstance(result, dict):
                for key in ("code", "reading_id", "segment_id", "reading_state", "media_attached", "status"):
                    if key in result:
                        row[key] = result[key]
            if data.get("status") not in ("success", "running"):
                # Failure details are retained as safe codes; never raw server echoes.
                report["failures"].append(dict(phase=phase, tool=data.get("tool_name"), code=result.get("code") if isinstance(result, dict) else "tool_error"))
                row['diagnostic'] = safe_probe_error(result.get('message') if isinstance(result, dict) else result, probe_secrets)
            report["calls"].append(row)
            save_report()

    OpenAIHTTPClient.responses = traced_http
    ToolManager.sync_mcp_into_agent = lambda *a: None
    try:
        with config_scope(Config(cfg)):
            revision = capture_config_revision()
            report["config_revision"] = revision
            store = TaskStore(workspace / "personal")
            task = store.submit(OWNER, "native-ingest-probe", {"text": "整理六页混合PDF为可检索主题知识"}, {"channel": "web"}, revision)
            entry = (task['attachments'][0] if resume and task['attachments'] else
                     store.attach(OWNER, task["task_id"], Path(pdf_path).resolve()))
            if task['status'] == 'queued':
                task = store.claim(OWNER, task["task_id"])
            elif task['status'] not in ('running', 'succeeded'):
                raise RuntimeError('隔离任务状态不允许直接接续')
            report.update(task_id=task["task_id"], attempt=task["attempt"])
            config = dict(cwd=str(workspace), workspace_root=str(workspace), task_store=store,
                          document_reading_native_pdf_enabled=True)
            if pdf_max_pages is not None:
                from agent.knowledge.reading_materials import PdfBudget
                config['reading_pdf_budget'] = PdfBudget(max_pages=pdf_max_pages)
            with use_config_revision(revision), identity_scope(user_id=OWNER, session_id="ingest-probe"), task_scope(task["task_id"]):
                model = AgentLLMModel(Bridge(), bot_type=cfg["bot_type"])
                phase = "capability"
                from agent.knowledge.reading_capabilities import ensure_pdf_capability, capability_identity
                report["provider_identity"] = capability_identity(model, revision)
                report["bot_class"] = type(model.bot).__module__ + "." + type(model.bot).__qualname__
                save_report()
                verified = ensure_pdf_capability(workspace, model, revision)
                report["capability_verified"] = verified
                save_report()
                if not verified:
                    raise RuntimeError("native_pdf_capability_unknown")
                phase = "ingest"
                read = Read(config)
                ingest = KnowledgeIngest(config)
                memory = MemoryManager(MemoryConfig(workspace_root=str(workspace)))
                search = MemorySearchTool(memory, OWNER)
                skill = Path("skills/knowledge-wiki/SKILL.md").read_text(encoding="utf-8")
                prompt = "你在隔离验收中。按真实工具结果完成任务，任何错误如实反馈。" + skill
                agent = Agent(prompt, model=model, tools=[read, ingest, search], workspace_dir=str(workspace),
                    max_steps=max_steps, enable_skills=False, skip_context_files=True, output_mode="logger")
                # Agent 会重建通用系统提示；任务指令须走受支持的 suffix 接口。
                agent.extra_system_suffix = prompt
                attachment = Path(entry["path"]).relative_to(workspace).as_posix()
                decide_args = dict(task_id=task['task_id'], attachment_id=entry['attachment_id'],
                                   purpose='long_term', reason='整理用户指定资料为可检索的主题知识')
                read_args = dict(path=attachment, pages='1-', visual=True)
                query = (f"当前接收任务task_id={task['task_id']}，唯一附件path={attachment}。"
                    f"先调用knowledge_ingest，参数为{json.dumps(decide_args, ensure_ascii=False)}，仅登记用途；"
                    f"然后调用read，参数为{json.dumps(read_args, ensure_ascii=False)}，读取真实资料的图文阅读副本。"
                    "当前隔离知识库为空，技能已完整加载在系统提示，无需再读取索引或技能。"
                    "禁止附parse_id/offset/limit。PDF紧接的首个响应就整理并提交knowledge_ingest，"
                    "或先写下带具体数值、公式、图形关系及出处的主题发现，再调用其他工具；"
                    "不要只声明已读就先查索引，后续轮不会重复附图。"
                    "按next_pages继续全部剩余批次；跨批先保存具体发现，最后同主题合并。"
                    "按主题而非逐页整理，至少3个主题页，每页summary。"
                    "必须完整保留下列问题涉及的具体参数、公式、图间关系，在reading_evidence中按实际物理页提交"
                    "具体观察（type=ocr或visual_interpretation），Office必须使用source_locations中的完整location；"
                    "notes中修订及批注仅作附加层，当前正文以阅读副本为准。正文sources用reading_id/evidence_key；"
                    "reading_submission_id固定probe-submit-1，reading_coverage覆盖read返回的全部reading_id/segment_id。"
                    "不要只提交占位，完成入库后memory_search按题目关键词检索并read命中知识页正文。"
                    "最后回答各问题的答案和输入PDF物理页，JSON对象answers数组（id,answer,page）。问题：" + json.dumps(questions, ensure_ascii=False))
                started = time.perf_counter()
                answer = agent.run_stream(query, on_event=event)
                report["ingest_elapsed_s"] = round(time.perf_counter() - started, 3)
                (output / "answers.txt").write_text(answer, encoding="utf-8")
                current = store.get(OWNER, task["task_id"])
                report["task_status"] = current["status"]
                report["result_refs"] = current.get("result_refs", [])
                report["search_hits"] = {}
                wiki = WikiService(workspace)
                for keyword in golden.get('search_keywords', ("Directivity", "去嵌", "校准")):
                    report["search_hits"][keyword] = len(wiki.search(keyword))
                refs = current.get("result_refs", [])
                bodies = [wiki.read_reference(ref["publish_id"]) for ref in refs]
                report["pages"] = [dict(title=b["title"], summary=b["summary"], source_count=len(b["sources"]),
                    locations=[s["location"] for s in b["sources"]]) for b in bodies]
                (output / "published.json").write_text(json.dumps(bodies, ensure_ascii=False, indent=2), encoding="utf-8")
                # 终态仅只读检查持久记录，不绕过运行期guard。
                import sqlite3
                source_service = SourceService(workspace)
                with sqlite3.connect(source_service.db.resolve().as_uri()+"?mode=ro", uri=True) as db:
                    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reading_records'").fetchone()
                    records = ([json.loads(row[0]) for row in db.execute("SELECT record FROM reading_records").fetchall()]
                               if exists else [])
                records = [r for r in records if r["task_id"] == current["task_id"]]
                report["reading_checkpoints"] = [dict(reading_id=r["reading_id"], segments=[{
                    k:s.get(k) for k in ("segment_id", "state", "full_response_saved", "response_id", "output_hash")} for s in r["segments"]]) for r in records]
                # 已完成任务的正常read若不再允许，明确记录错误，不伪造缓存命中。
                phase = "cache"
                read.model = model
                before = len(report["requests"])
                cached = read.execute(dict(path=entry["path"], pages="1-6"))
                report["cache"] = dict(status=cached.status, media_count=len(cached.media),
                    model_requests=len(report["requests"])-before,
                    code=cached.result.get("code") if isinstance(cached.result, dict) else None)
                report["ingest_passed"] = current["status"] == "succeeded" and bool(refs) and all(b.get("summary") and b.get("sources") for b in bodies)
                save_report()
                if not report["ingest_passed"]:
                    raise RuntimeError("native_ingest_not_completed")
                phase = "cached_answers"
                cache_agent = Agent("仅从已发布知识回答问题，先memory_search再read知识正文。禁止读取原始附件。",
                    model=model, tools=[read, search], workspace_dir=str(workspace), max_steps=6,
                    enable_skills=False, skip_context_files=True, output_mode="logger")
                cache_agent.extra_system_suffix = '仅从已发布知识回答问题，先memory_search再read知识正文。禁止读取原始附件。'
                before = len(report["requests"])
                cached_answer = cache_agent.run_stream("从知识正文回答以下问题，每项给id/answer/page：" + json.dumps(questions, ensure_ascii=False), on_event=event)
                (output / "cached-answers.txt").write_text(cached_answer, encoding="utf-8")
                report["cached_answers"] = dict(model_requests=len(report["requests"])-before,
                    media_count=sum(len(r["media"]) for r in report["requests"][before:]),
                    quality="待人工逐项核验23点，不能以零媒体推断质量通过")
            # 普通无task读取，仅新建无敏感的单页图片PDF。
            phase = "general_read"
            from PIL import Image, ImageDraw
            image = Image.new("RGB", (500, 180), "white")
            ImageDraw.Draw(image).text((20, 30), "SAFE GENERAL PDF: 314159", fill="black", font_size=24)
            general_file = workspace / "general-safe.pdf"
            image.save(general_file, "PDF")
            with use_config_revision(revision), identity_scope(user_id=OWNER, session_id="general-probe"):
                general_model = AgentLLMModel(Bridge(), bot_type=cfg["bot_type"])
                general_read = Read(config)
                general_agent = Agent("调用read读取指定PDF第一页，回答图中六位数字，不做入库。", model=general_model,
                    tools=[general_read], workspace_dir=str(workspace), max_steps=3,
                    enable_skills=False, skip_context_files=True, output_mode="logger")
                before = len(report["requests"])
                general_answer = general_agent.run_stream("read path=general-safe.pdf,pages=1,visual=true；不附parse_id/offset/limit。", on_event=event)
                report["general_read"] = dict(correct="314159" in general_answer,
                    model_requests=len(report["requests"])-before,
                    media_count=sum(len(r["media"]) for r in report["requests"][before:]))
    except BaseException as exc:
        report["exception_type"] = type(exc).__name__
        if isinstance(exc, RuntimeError) and str(exc) in ("native_pdf_capability_unknown", "native_ingest_not_completed"):
            report["failure_code"] = str(exc)
        raise
    finally:
        OpenAIHTTPClient.responses = original_http
        ToolManager.sync_mcp_into_agent = original_sync
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pdf", type=Path, default=Path("tmp/pdf-compare/mixed-sample.pdf"))
    parser.add_argument("--golden", type=Path, default=Path("tmp/pdf-compare/golden.json"))
    parser.add_argument('--resume', action='store_true', help='接续同一隔离任务及其成功产物')
    parser.add_argument('--output-budget', type=int, help='仅覆盖隔离模型目录的输出预算，用于参数对照')
    parser.add_argument('--max-steps', type=int, default=12)
    parser.add_argument('--omit-prompt-cache-key', action='store_true', help='仅隔离诊断不发送缓存路由提示')
    parser.add_argument('--soffice', type=Path, help='Office隔离验收使用的本地LibreOffice路径')
    parser.add_argument('--pdf-max-pages', type=int, help='隔离验收中每个PDF媒体批次的最大页数')
    args = parser.parse_args()
    result = run(args.config, args.output, args.pdf, args.golden, resume=args.resume,
                 output_budget=args.output_budget, max_steps=args.max_steps,
                 omit_prompt_cache_key=args.omit_prompt_cache_key, soffice=args.soffice, pdf_max_pages=args.pdf_max_pages)
    print(json.dumps(dict(output=str(args.output.resolve()), passed=result.get("ingest_passed"),
        task_status=result.get("task_status"), request_count=len(result["requests"])), ensure_ascii=False))
    sys.exit(0 if result.get("ingest_passed") else 1)
