import asyncio
from pathlib import Path
import pytest


def write_test_pdf(path, pages):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    for text in pages:
        page = writer.add_blank_page(width=600, height=800)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(("BT /F1 12 Tf 70 700 Td (" + text + ") Tj ET").encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(path)

from agent.knowledge.parser import parse_source, SourceError
from agent.knowledge.personal_assistant import SourceService, guard_path


def setup_source(tmp_path, text="射频预算为42 dB，设备型号RF100"):
    p = tmp_path / "input.txt"
    p.write_text(text, encoding="utf-8")
    service = SourceService(tmp_path)
    source = service.receive(p, filename=p.name, request_id="first", origin={"channel": "test"})
    service.change(source["document_id"], purpose="long_term", expected_state=0, reason="主动整理")
    return service, source


def organized_pages(task, content="设备预算资料已按主题整理。", title="设备预算"):
    """确定性成品夹具；出处来自首轮用途登记，不冒充模型判断验收。"""
    decisions = task["attachment_decisions"].values()
    sources = [dict(document_id=d["source"]["document_id"], revision_id=d["source"]["revision_id"],
                    parse_id=d["parse_id"], ordinal=0) for d in decisions if d["purpose"] == "long_term"]
    return [dict(summary='说明本页知识的主题及适用范围。', title=title, category="工程/预算", blocks=[dict(content=content, sources=sources)],
                 related_page_ids=[], depends_on=[])]


def test_three_formats_full_and_locations(tmp_path):
    from docx import Document
    from openpyxl import Workbook
    pdf = tmp_path / "sample.pdf"
    write_test_pdf(pdf, [f"page {n+1} source" for n in range(3)])
    units = parse_source(pdf)["units"]
    assert units[-1]["location"]["physical_page"] == 3
    doc = Document()
    for n in range(2200):
        doc.add_paragraph(f"paragraph {n}")
    doc.add_table(rows=1, cols=2).cell(0, 1).text = "tail evidence"
    doc.save(tmp_path / "sample.docx")
    units = parse_source(tmp_path / "sample.docx")["units"]
    assert "table[1]/row[1]/cell[2]" in units[-1]["location"]["path"]
    assert units[2199]["text"] == "paragraph 2199"
    book = Workbook()
    book.active.title = "预算"
    book.active["B3"] = "=1+1"
    book.save(tmp_path / "sample.xlsx")
    formula = parse_source(tmp_path / "sample.xlsx")["units"][0]
    assert formula["location"] == {"kind": "xlsx", "sheet": "预算", "cell": "B3"}
    assert formula["formula"] == "=1+1" and formula["cached_value"] is None
    service = SourceService(tmp_path)
    for filename in ("sample.pdf", "sample.docx", "sample.xlsx"):
        receipt = service.receive(tmp_path / filename, filename=filename, request_id=filename, origin={"test": "three formats"})
        assert Path(receipt["original"]).suffix == ""
        service.change(receipt["document_id"], purpose="long_term", expected_state=0, reason="三格式来源发布探针")
        publication = service.publish(service.prepare(receipt["revision_id"])["candidate_id"])
        assert publication["searchable"]
        assert service.read_reference(publication["publish_id"], 0)["valid"]
    with pytest.raises(SourceError, match="超限"):
        parse_source(tmp_path / "sample.docx", max_bytes=100)
    (tmp_path / "broken.pdf").write_bytes(b"invalid")
    with pytest.raises(SourceError) as err:
        parse_source(tmp_path / "broken.pdf")
    assert err.value.code == "parse_failed"


def test_publication_search_tamper_withdraw_restart(tmp_path):
    service, source = setup_source(tmp_path)
    candidate = service.prepare(source["revision_id"], task_id="task-test")
    result = service.publish(candidate["candidate_id"])
    assert result["searchable"] and result["task_id"] == "task-test"
    assert service.search("请问射频预算是多少？")[0]["location"]["line"] == 1
    path = tmp_path / result["path"]
    guard_path(path)
    with pytest.raises(SourceError):
        guard_path(path, write=True)
    original = path.read_bytes()
    path.write_text("tampered", encoding="utf-8")
    assert not service.search("射频预算")
    with pytest.raises(SourceError):
        guard_path(path)
    path.write_bytes(original)
    service.change(source["document_id"], action="withdraw", expected_state=1, reason="用户撤回")
    service = SourceService(tmp_path)
    assert not service.search("预算")
    assert service.read_reference(result["publish_id"], 0, historical=True)["current_status"] == "withdrawn"
    with pytest.raises(SourceError):
        service.publish(candidate["candidate_id"])


def test_parse_decision_barrier_and_failed_replacement(tmp_path, monkeypatch):
    service, source = setup_source(tmp_path)
    service.publish(service.prepare(source["revision_id"])["candidate_id"])
    p = tmp_path / "input.txt"
    p.write_text("new budget 99", encoding="utf-8")
    new = service.receive(p, filename=p.name, request_id="new", origin={"test": 1}, document_id=source["document_id"])
    original_parse = service.parse
    def interrupt(revision):
        result = original_parse(revision)
        service.change(source["document_id"], action="correct", expected_state=1, reason="更正")
        service.change(source["document_id"], action="republish", expected_state=2, reason="新决定")
        return result
    monkeypatch.setattr(service, "parse", interrupt)
    with pytest.raises(SourceError):
        service.prepare(new["revision_id"], expected_current_revision=source["revision_id"])
    assert not service.search("预算")


def test_duplicate_identity_and_schema(tmp_path):
    service, source = setup_source(tmp_path)
    same = service.receive(tmp_path / "input.txt", filename="input.txt", request_id="second", origin={"test": 2}, document_id=source["document_id"])
    assert same["duplicate"] and same["revision_id"] == source["revision_id"]
    independent = service.receive(tmp_path / "input.txt", filename="input.txt", request_id="third", origin={"test": 3})
    assert independent["document_id"] != source["document_id"]
    with service.transaction() as db:
        db.execute("PRAGMA user_version=2")
    with pytest.raises(SourceError):
        SourceService(tmp_path)


def test_v1_publication_column_upgrade_is_additive(tmp_path):
    import sqlite3
    root = tmp_path / ".personal_sources"
    root.mkdir()
    with sqlite3.connect(root / "sources.sqlite3") as db:
        db.execute("CREATE TABLE documents(id TEXT PRIMARY KEY,current TEXT,state INTEGER,purpose TEXT,validity TEXT)")
        db.execute("INSERT INTO documents VALUES('legacy','original-revision',7,'long_term','active')")
        db.execute("PRAGMA user_version=1")
    service = SourceService(tmp_path)
    assert service.get("legacy") == dict(id="legacy", current="original-revision", state=7,
                                         purpose="long_term", validity="active", publication=None)
    # A second open neither repeats ALTER nor mutates old authoritative values.
    assert SourceService(tmp_path).get("legacy") == service.get("legacy")
    with sqlite3.connect(service.db) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_memory_search_bridge(tmp_path, monkeypatch):
    from agent.memory.manager import MemoryManager
    from agent.memory.config import MemoryConfig
    from common import state_dir
    service, source = setup_source(tmp_path)
    service.publish(service.prepare(source["revision_id"])["candidate_id"])
    monkeypatch.setattr(state_dir, "knowledge_dir", lambda **kw: tmp_path / "knowledge")
    manager = MemoryManager(MemoryConfig(workspace_root=str(tmp_path)), embedding_provider=None)
    results = asyncio.run(manager.search("请问射频预算是多少？", user_id="local-owner", min_score=0))
    assert any("source_reference" in result.snippet for result in results)
    manager.close()


def test_main_agent_registry_prompt_skill(tmp_path, monkeypatch):
    from common import state_dir
    from agent.tools.tool_manager import ToolManager
    from agent.skills.manager import SkillManager
    from agent.prompt.builder import PromptBuilder
    monkeypatch.setattr(state_dir, "real_state_root", lambda: str(tmp_path))
    monkeypatch.setattr(state_dir, "knowledge_dir", lambda **kw: tmp_path / "knowledge")
    manager = ToolManager()
    manager.load_tools()
    assert "knowledge_ingest" in manager.tool_classes
    assert "knowledge_source_action" in manager.tool_classes
    tool = manager.create_tool("knowledge_ingest")
    assert tool.name == "knowledge_ingest"
    skills = SkillManager(custom_dir=str(tmp_path / "skills"))
    skill = skills.get_skill("knowledge-wiki")
    assert skill is not None
    prompt = PromptBuilder(workspace_dir=str(tmp_path), language="zh").build(tools=[tool], skill_manager=skills)
    assert "knowledge_ingest" in prompt and "knowledge-wiki" in prompt


def test_ingest_multi_recovery_and_no_reparse(tmp_path, monkeypatch):
    from common.personal_tasks import TaskStore, OWNER, task_scope
    from common.runtime_identity import identity_scope
    from agent.tools.knowledge_ingest import KnowledgeIngest
    from agent.tools.knowledge_ingest import resume_publication
    store = TaskStore(tmp_path / "tasks")
    task = store.submit(OWNER, "upload", {"text": "整理"}, {"channel": "web"}, "cfg")
    for i in range(2):
        path = tmp_path / f"{i}.txt"
        path.write_text(f"设备预算资料 {i}", encoding="utf-8")
        store.attach(OWNER, task["task_id"], path)
    task = store.claim(OWNER, task["task_id"])
    tool = KnowledgeIngest({"cwd": str(tmp_path), "task_store": store})
    old = store.complete
    def crash(*args, **kwargs):
        raise OSError("complete 前退出")
    monkeypatch.setattr(store, "complete", crash)
    with identity_scope(user_id=OWNER, session_id="ordinary-desktop"), task_scope(task["task_id"]):
        args = {"task_id": task["task_id"], "purpose": "long_term", "reason": "有价值"}
        registered = tool.execute(args)
        assert registered.status == "success" and registered.result["organization_required"]
        assert not SourceService(tmp_path).search("设备预算")
        assert tool.execute(dict(args, pages=organized_pages(registered.result))).status == "error"
    task = store.get(OWNER, task["task_id"])
    assert task["wiki_batch_id"] and task["wiki_workspace"] == str(tmp_path) and task["stage"] == "pending_index"
    monkeypatch.setattr(store, "complete", old)
    store.retry(OWNER, task["task_id"], 1)
    task = store.claim(OWNER, task["task_id"])
    monkeypatch.setattr(SourceService, "parse", lambda *a: pytest.fail("恢复不能重新解析"))
    result = resume_publication(task, store)
    assert result["status"] == "succeeded" and len(result["result_refs"]) == 1
    assert len(result["result_refs"][0]["sources"]) == 2


def test_purpose_context_and_history_round_trip(tmp_path):
    from common.personal_tasks import task_scope
    from agent.knowledge.purpose import durable_messages, mark_messages, guard_memory_write
    from agent.memory.conversation_store import ConversationStore
    messages = [{"role": "user", "content": "临时预算"}, {"role": "assistant", "content": "仅这次使用"}]
    with task_scope("t1"):
        assert durable_messages(messages) == []
        with pytest.raises(ValueError):
            guard_memory_write(tmp_path / "memory" / "today.md")
    marked = mark_messages(messages, "t1")
    assert durable_messages(marked) == []
    store = ConversationStore(tmp_path / "conversation.db")
    store.append_messages("desktop", marked)
    loaded = store.load_messages("desktop")
    assert loaded[0]["personal_task_id"] == "t1"
    assert durable_messages(loaded) == []


def test_failed_new_candidate_keeps_previous_version(tmp_path, monkeypatch):
    service, source = setup_source(tmp_path)
    old = service.publish(service.prepare(source["revision_id"])["candidate_id"])
    path = tmp_path / "new.txt"
    path.write_text("new budget", encoding="utf-8")
    revision = service.receive(path, filename=path.name, request_id="new", origin={"test": 1}, document_id=source["document_id"])
    candidate = service.prepare(revision["revision_id"], expected_current_revision=source["revision_id"])
    import agent.knowledge.personal_assistant as module
    monkeypatch.setattr(module, "write_text_atomic", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        service.publish(candidate["candidate_id"])
    assert service.search("预算")[0]["publish_id"] == old["publish_id"]


def test_ingest_duplicate_and_action_barrier(tmp_path):
    from common.personal_tasks import TaskStore, OWNER, task_scope
    from common.runtime_identity import identity_scope
    from agent.tools.knowledge_ingest import KnowledgeIngest
    from agent.tools.knowledge_source_action import KnowledgeSourceAction
    store = TaskStore(tmp_path / "tasks")
    tool = KnowledgeIngest({"cwd": str(tmp_path), "task_store": store})
    path = tmp_path / "source.txt"
    path.write_text("同资料同版本", encoding="utf-8")
    document, ref = None, None
    with identity_scope(user_id=OWNER):
        for n in range(2):
            task = store.submit(OWNER, str(n), {"text": "整理"}, {"channel": "web"}, "cfg")
            store.attach(OWNER, task["task_id"], path)
            store.claim(OWNER, task["task_id"])
            args = {"task_id": task["task_id"], "purpose": "long_term", "reason": "共享"}
            if document:
                args["document_id"] = document
            with task_scope(task["task_id"]):
                registered = tool.execute(args)
                assert registered.status == "success" and registered.result["organization_required"]
                pages = organized_pages(registered.result, "同资料同版本的整理结论")
                if ref is not None:
                    # 同源重复提交仍须携带成品；更新同一页而非自动原文发布。
                    pages[0].update(page_id=ref["page_id"], expected_version=ref["page_version"])
                result = tool.execute(dict(args, pages=pages))
                replay = tool.execute(dict(args, pages=pages))
                assert replay.result["result_refs"] == result.result["result_refs"]
            assert result.status == "success", result.result
            ref = result.result["result_refs"][0]
            document = ref["document_id"]
            assert result.result["outcome"] == "published"
        action = KnowledgeSourceAction({"cwd": str(tmp_path)})
        assert action.execute({"document_id": document, "expected_state": 1, "action": "withdraw", "reason": "撤回"}).status == "success"
        assert action.execute({"document_id": document, "expected_state": 1, "action": "republish", "reason": "迟到"}).status == "error"
    with identity_scope(user_id="stranger"):
        assert action.execute({"document_id": document}).result == "forbidden"


def test_cancel_winner_cannot_change_existing_purpose(tmp_path):
    from common.personal_tasks import TaskStore, OWNER, task_scope
    from common.runtime_identity import identity_scope
    from agent.tools.knowledge_ingest import KnowledgeIngest
    from agent.tools.knowledge_source_action import KnowledgeSourceAction
    service, source = setup_source(tmp_path)
    old = service.publish(service.prepare(source["revision_id"])["candidate_id"])
    store = TaskStore(tmp_path / "tasks")
    task = store.submit(OWNER, "cancel", {"text": "暂用"}, {"channel": "web"}, "cfg")
    store.attach(OWNER, task["task_id"], tmp_path / "input.txt")
    store.claim(OWNER, task["task_id"])
    store.cancel(OWNER, task["task_id"], 1)
    with identity_scope(user_id=OWNER), task_scope(task["task_id"]):
        tool = KnowledgeIngest({"cwd": str(tmp_path), "task_store": store})
        result = tool.execute({"task_id": task["task_id"], "document_id": source["document_id"], "purpose": "transient", "reason": "暂用"})
        assert result.status == "error"
        action = KnowledgeSourceAction({"cwd": str(tmp_path), "task_store": store})
        assert action.execute({"document_id": source["document_id"], "expected_state": 1, "action": "withdraw", "reason": "撤回"}).status == "error"
    assert service.search("预算")[0]["publish_id"] == old["publish_id"]


@pytest.mark.parametrize("interrupt", [False, True])
def test_attachment_subsets_mixed_purposes(tmp_path, monkeypatch, interrupt):
    from common.personal_tasks import TaskStore, OWNER, task_scope
    from common.runtime_identity import identity_scope
    from agent.tools.knowledge_ingest import KnowledgeIngest, resume_publication
    store = TaskStore(tmp_path / "tasks")
    task = store.submit(OWNER, "mixed", {"text": "第一份长期，第二份临时"}, {"channel": "web"}, "cfg")
    for number, text in enumerate(("长期资料证据", "临时机密素材")):
        path = tmp_path / f"mixed-{number}.txt"
        path.write_text(text, encoding="utf-8")
        store.attach(OWNER, task["task_id"], path, attachment_id=f"a{number}")
    store.claim(OWNER, task["task_id"])
    tool = KnowledgeIngest({"cwd": str(tmp_path), "task_store": store})
    service = SourceService(tmp_path)
    with identity_scope(user_id=OWNER), task_scope(task["task_id"]):
        first = tool.execute(dict(task_id=task["task_id"], attachment_id="a0", purpose="long_term", reason="可复用"))
        assert first.status == "success" and first.result["status"] == "running"
        assert not first.result["source_candidates"]
        assert first.result["attachment_decisions"]["a0"]["parse_id"]
        assert not service.search("长期资料")
        with pytest.raises(SourceError):
            resume_publication(store.get(OWNER, task["task_id"]), store)
        # 重复子集决定复用已有解析；可靠性断言继续保留。
        monkeypatch.setattr(SourceService, "parse", lambda *a, **kw: pytest.fail("已有解析不可重新解析"))
        repeated = tool.execute(dict(task_id=task["task_id"], attachment_id="a0", purpose="long_term", reason="重试"))
        assert repeated.result["attachment_decisions"]["a0"]["parse_id"] == first.result["attachment_decisions"]["a0"]["parse_id"]
        last = tool.execute(dict(task_id=task["task_id"], attachment_id="a1", purpose="transient", reason="仅本次"))
        assert last.status == "success" and last.result["organization_required"]
        assert last.result["attachment_decisions"]["a1"]["purpose"] == "transient"
        complete = store.complete
        if interrupt:
            monkeypatch.setattr(store, "complete", lambda *a, **kw: (_ for _ in ()).throw(OSError("模拟发布后退出")))
        last = tool.execute(dict(task_id=task["task_id"], purpose="long_term", reason="完成整理",
                                 pages=organized_pages(last.result, "长期资料的可靠结论")))
        monkeypatch.setattr(SourceService, "parse", lambda *a: pytest.fail("恢复不能重新解析"))
        if interrupt:
            assert last.status == "error"
            monkeypatch.setattr(store, "complete", complete)
            store.retry(OWNER, task["task_id"], 1)
            claimed = store.claim(OWNER, task["task_id"])
            finished = resume_publication(claimed, store)
        else:
            assert last.status == "success"
            finished = last.result
    assert finished["status"] == "succeeded" and len(finished["result_refs"]) == 2
    assert finished["attachment_decisions"]["a0"]["purpose"] == "long_term"
    assert finished["result_refs"][1]["purpose"] == "transient"
    assert finished["result_refs"][1]["searchable"] is False
    assert service.search("长期资料") and not service.search("临时机密")
    assert all(Path(ref["original"]).exists() for ref in (finished["attachment_decisions"][key]["source"] for key in ("a0", "a1")))


def test_waiting_and_cancel_block_partial_batch(tmp_path):
    from common.personal_tasks import TaskStore, OWNER, task_scope
    from common.runtime_identity import identity_scope
    from agent.tools.knowledge_ingest import KnowledgeIngest, resume_publication
    store = TaskStore(tmp_path / "tasks")
    task = store.submit(OWNER, "waiting", {"text": "整理"}, {"channel": "web"}, "cfg")
    for number in range(2):
        path = tmp_path / f"source-{number}.txt"
        path.write_text(f"候选证据 {number}", encoding="utf-8")
        store.attach(OWNER, task["task_id"], path, attachment_id=str(number))
    store.claim(OWNER, task["task_id"])
    tool = KnowledgeIngest({"cwd": str(tmp_path), "task_store": store})
    with identity_scope(user_id=OWNER), task_scope(task["task_id"]):
        tool.execute(dict(task_id=task["task_id"], attachment_id="0", purpose="long_term", reason="可复用"))
        result = tool.execute(dict(task_id=task["task_id"], attachment_id="1", purpose="waiting", reason="应替代哪份资料？"))
        assert result.result["status"] == "waiting_input"
        assert result.result["question"] == "应替代哪份资料？"
        assert not result.result["source_candidates"]
        store.cancel(OWNER, task["task_id"], 1)
        assert tool.execute(dict(task_id=task["task_id"], attachment_id="1", purpose="transient", reason="临时")).status == "error"
        with pytest.raises(ValueError):
            resume_publication(store.get(OWNER, task["task_id"]), store)
    assert not SourceService(tmp_path).search("候选证据")


@pytest.mark.parametrize("action", ["republish", "purpose"])
@pytest.mark.parametrize("other_purpose", ["undecided", "transient"])
def test_source_action_pages_cannot_complete_attachment_batch(tmp_path, action, other_purpose):
    from agent.tools.knowledge_ingest import KnowledgeIngest
    from agent.tools.knowledge_source_action import KnowledgeSourceAction
    from agent.tools.read.read import Read
    from common.personal_tasks import TaskStore, OWNER, task_scope
    from common.runtime_identity import identity_scope

    service, source = setup_source(tmp_path)
    parsed = service.parse(source["revision_id"])
    service.change(source["document_id"], action="withdraw", expected_state=1, reason="已撤回")
    store = TaskStore(tmp_path / "tasks")
    task = store.submit(OWNER, "source-action-batch", {"text": "第一份恢复整理，第二份另行决定用途"},
                        {"channel": "web"}, "cfg")
    store.attach(OWNER, task["task_id"], tmp_path / "input.txt", attachment_id="first")
    other = tmp_path / "other.txt"
    other.write_text("临时机密：不得随第一份资料长期发布", encoding="utf-8")
    attachment = store.attach(OWNER, task["task_id"], other, attachment_id="second")
    store.claim(OWNER, task["task_id"])
    config = {"cwd": str(tmp_path), "task_store": store}
    args = dict(document_id=source["document_id"], revision_id=source["revision_id"],
                expected_state=2, action=action, purpose="long_term", reason="恢复并提交第一份成品")
    pages = [dict(summary='说明本页知识的主题及适用范围。', title="射频预算", category="工程", blocks=[dict(content="射频预算为42 dB。", sources=[dict(
        document_id=source["document_id"], revision_id=source["revision_id"],
        parse_id=parsed["parse_id"], ordinal=0)])], related_page_ids=[], depends_on=[])]
    with identity_scope(user_id=OWNER), task_scope(task["task_id"]):
        read = Read(config).execute({"path": attachment["path"]})
        assert read.status == "success", read.result
        if other_purpose == "transient":
            decision = KnowledgeIngest(config).execute(dict(task_id=task["task_id"], attachment_id="second",
                purpose="transient", reason="仅供当前任务使用"))
            assert decision.status == "success" and decision.result["status"] == "running"
        before = store.get(OWNER, task["task_id"])
        sources_before = {document: service.get(document) for document in
                          (source["document_id"], read.result["document_id"])}
        with service.transaction() as db:
            counts_before = [db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                             for table in ("changes", "wiki_batches", "wiki_publications")]
        tool = KnowledgeSourceAction(config)
        result = tool.execute(dict(args, pages=pages))
        assert result.status == "error" and result.result["code"] == "invalid_request"
        assert not result.result["searchable"]
        after = store.get(OWNER, task["task_id"])
        assert after["status"] == "failed" and after["outcome"] is None
        assert after["error"]["code"] == "invalid_request"
        assert not after["result_refs"] and len(after["attachments"]) == 2
        assert after.get("attachment_decisions") == before.get("attachment_decisions")
        assert not after.get("wiki_batch_id") and not after.get("source_action_checkpoint")
        assert {document: service.get(document) for document in sources_before} == sources_before
        with service.transaction() as db:
            assert [db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                    for table in ("changes", "wiki_batches", "wiki_publications")] == counts_before
        assert not service.search("预算") and not service.search("临时机密")

        # 不携带成品仍可恢复资格，之后必须由 knowledge_ingest 统一处理附件。
        store.retry(OWNER, task["task_id"], 1)
        store.claim(OWNER, task["task_id"])
        restored = tool.execute(args)
        assert restored.status == "success" and restored.result["organization_required"]
        assert not restored.result["searchable"]
        after = store.get(OWNER, task["task_id"])
        assert after["status"] == "running" and not after["result_refs"]
        assert after.get("attachment_decisions") == before.get("attachment_decisions")
        assert not after.get("wiki_batch_id")
        assert service.get(read.result["document_id"]) == sources_before[read.result["document_id"]]
        assert service.get(source["document_id"])["state"] == 3
        assert not service.search("预算")


@pytest.mark.parametrize("action", ["republish", "purpose"])
def test_restore_registered_source_without_attachment(tmp_path, action):
    from common.runtime_identity import identity_scope
    from agent.tools.knowledge_source_action import KnowledgeSourceAction
    service, source = setup_source(tmp_path)
    previous = service.publish(service.prepare(source["revision_id"])["candidate_id"])
    service.change(source["document_id"], action="withdraw", expected_state=1, reason="撤回")
    assert not service.search("预算")
    tool = KnowledgeSourceAction({"cwd": str(tmp_path)})
    with identity_scope(user_id="local-owner"):
        result = tool.execute(dict(document_id=source["document_id"], expected_state=2,
                                   action=action, purpose="long_term", reason="恢复该资料"))
        assert result.status == "success" and result.result["organization_required"]
        assert not result.result["searchable"] and not service.search("预算")
        assert result.result["revision_id"] == source["revision_id"]
        material = result.result
        pages = [dict(summary='说明本页知识的主题及适用范围。', title="射频预算", category="工程/预算", blocks=[dict(content="射频预算为42 dB。",
            sources=[dict(document_id=material["document_id"], revision_id=material["revision_id"],
                          parse_id=material["parse_id"], ordinal=0)])], related_page_ids=[], depends_on=[])]
        result = tool.execute(dict(document_id=source["document_id"], expected_state=material["state_revision"],
                                  action=action, purpose="long_term", reason="重新整理", pages=pages))
    assert result.status == "success" and result.result["searchable"]
    ref = result.result["result_refs"][0]
    assert ref["publish_id"] != previous["publish_id"]
    assert service.search("预算")[0]["publish_id"] == ref["publish_id"]
    with pytest.raises(SourceError):
        service.read_reference(previous["publish_id"], 0)


def test_restore_pending_index_retries_without_reparse(tmp_path, monkeypatch):
    from agent.knowledge.wiki import WikiService
    from agent.tools.knowledge_ingest import resume_publication
    from agent.tools.knowledge_source_action import KnowledgeSourceAction
    from common.runtime_identity import identity_scope
    from common.personal_tasks import TaskStore, OWNER, task_scope
    service, source = setup_source(tmp_path)
    service.change(source["document_id"], purpose="transient", expected_state=1, reason="暂用")
    store = TaskStore(tmp_path / "tasks")
    task = store.submit(OWNER, "restore-index", {"text": "恢复"}, {"channel": "web"}, "cfg")
    store.claim(OWNER, task["task_id"])
    tool = KnowledgeSourceAction({"cwd": str(tmp_path), "task_store": store})
    publish = WikiService.publish
    with identity_scope(user_id=OWNER), task_scope(task["task_id"]):
        args = dict(document_id=source["document_id"], expected_state=2, action="republish", reason="恢复")
        material = tool.execute(args).result
        assert material["organization_required"] and not service.search("预算")
        pages = [dict(summary='说明本页知识的主题及适用范围。', title="恢复预算", category="工程", blocks=[dict(content="射频预算42 dB。", sources=[dict(
            document_id=source["document_id"], revision_id=source["revision_id"], parse_id=material["parse_id"], ordinal=0)])])]
        monkeypatch.setattr(WikiService, "publish", lambda *a: (_ for _ in ()).throw(SourceError("index_failed", "索引暂不可用")))
        result = tool.execute(dict(args, pages=pages))
        assert result.status == "error" and result.result["stage"] == "pending_index"
        assert not result.result["searchable"] and not service.search("预算")
        checkpoint = store.get(OWNER, task["task_id"])
        assert checkpoint["wiki_batch_id"] and checkpoint["wiki_workspace"]
        monkeypatch.setattr(WikiService, "publish", publish)
        monkeypatch.setattr(SourceService, "parse", lambda *a: pytest.fail("索引重试不能重新解析"))
        store.retry(OWNER, task["task_id"], 1)
        reopened = TaskStore(tmp_path / "tasks")
        result = resume_publication(reopened.claim(OWNER, task["task_id"]), reopened)
        assert result["status"] == "succeeded" and service.search("预算")


def test_restore_text_task_checkpoint_and_cancel(tmp_path, monkeypatch):
    from common.runtime_identity import identity_scope
    from common.personal_tasks import TaskStore, OWNER, task_scope
    from agent.tools.knowledge_source_action import KnowledgeSourceAction
    service, source = setup_source(tmp_path)
    service.change(source["document_id"], action="withdraw", expected_state=1, reason="撤回")
    store = TaskStore(tmp_path / "tasks")
    task = store.submit(OWNER, "restore", {"text": "恢复该资料"}, {"channel": "feishu"}, "cfg")
    store.claim(OWNER, task["task_id"])
    tool = KnowledgeSourceAction({"cwd": str(tmp_path), "task_store": store})
    complete = store.complete
    monkeypatch.setattr(store, "complete", lambda *a, **kw: (_ for _ in ()).throw(OSError("退出")))
    args = dict(document_id=source["document_id"], expected_state=2, action="republish", reason="恢复")
    with identity_scope(user_id=OWNER), task_scope(task["task_id"]):
        material = tool.execute(args).result
        assert material["organization_required"]
        pages = [dict(summary='说明本页知识的主题及适用范围。', title="恢复资料", category="工程", blocks=[dict(content="预算42 dB。", sources=[dict(
            document_id=source["document_id"], revision_id=source["revision_id"], parse_id=material["parse_id"], ordinal=0)])])]
        assert tool.execute(dict(args, pages=pages)).status == "error"
        checkpoint = store.get(OWNER, task["task_id"])
        assert checkpoint["source_action_checkpoint"]["revision_id"] == source["revision_id"]
        assert checkpoint["wiki_batch_id"] and not checkpoint["attachments"]
        monkeypatch.setattr(store, "complete", complete)
        store.retry(OWNER, task["task_id"], 1)
        store.claim(OWNER, task["task_id"])
        monkeypatch.setattr(SourceService, "parse", lambda *a: pytest.fail("恢复不得重解析"))
        from agent.tools.knowledge_ingest import resume_publication
        result = resume_publication(store.get(OWNER, task["task_id"]), store)
        assert result["status"] == "succeeded" and service.search("预算")
        assert store.get(OWNER, task["task_id"])["status"] == "succeeded"
        cancelled = store.submit(OWNER, "cancel-restore", {"text": "恢复"}, {"channel": "web"}, "cfg")
        store.claim(OWNER, cancelled["task_id"])
        store.cancel(OWNER, cancelled["task_id"], 1)
        with task_scope(cancelled["task_id"]):
            assert tool.execute(dict(args, expected_state=3)).status == "error"
        assert service.get(source["document_id"])["state"] == 3


def test_read_authorized_extensionless_pdf_without_publishing(tmp_path):
    from common.personal_tasks import TaskStore, OWNER, task_scope
    from common.runtime_identity import identity_scope
    from agent.tools.read.read import Read
    from agent.tools.knowledge_ingest import KnowledgeIngest
    path = tmp_path / "temporary.pdf"
    write_test_pdf(path, [f"temporary evidence page {page+1}" for page in range(2)])
    store = TaskStore(tmp_path / "tasks")
    task = store.submit(OWNER, "read-pdf", {"text": "仅本次分析"}, {"channel": "web"}, "cfg")
    entry = store.attach(OWNER, task["task_id"], path, attachment_id="pdf")
    assert not Path(entry["path"]).suffix
    store.claim(OWNER, task["task_id"])
    read = Read({"cwd": str(tmp_path), "task_store": store})
    with identity_scope(user_id=OWNER), task_scope(task["task_id"]):
        first = read.execute(dict(path=entry["path"], limit=1))
        assert first.status == "success"
        assert "page 1" in first.result["units"][0]["text"]
        assert first.result["units"][0]["location"]["physical_page"] == 1
        assert first.result["has_more"] and first.result["next_offset"] == 2
        second = read.execute(dict(path=entry["path"], offset=2, limit=1))
        assert "page 2" in second.result["units"][0]["text"] and not second.result["has_more"]
        assert all(first.result[key] == second.result[key] for key in ("document_id", "revision_id", "parse_id"))
        assert not first.result["published"] and not first.result["searchable"]
        ingest = KnowledgeIngest({"cwd": str(tmp_path), "task_store": store})
        result = ingest.execute(dict(task_id=task["task_id"], purpose="transient", reason="仅本次分析"))
        assert result.status == "success" and result.result["outcome"] == "transient"
        assert result.result["result_refs"][0]["document_id"] == first.result["document_id"]
    assert not SourceService(tmp_path).search("temporary evidence")
    other = store.submit(OWNER, "other", {"text": "别的资料"}, {"channel": "web"}, "cfg")
    with identity_scope(user_id=OWNER), task_scope(other["task_id"]):
        assert read.execute(dict(path=entry["path"])).result["code"] == "forbidden"


def test_v1_migration_backfills_valid_publication_only(tmp_path):
    import sqlite3
    service, source = setup_source(tmp_path)
    first = service.publish(service.prepare(source["revision_id"])["candidate_id"])
    # Same bytes can have multiple projections; select the latest valid one.
    newest = service.publish(service.prepare(source["revision_id"], expected_current_revision=source["revision_id"])["candidate_id"])
    for key in ("withdrawn", "changed_state", "tampered"):
        receipt = service.receive(tmp_path / "input.txt", filename="input.txt", request_id=key, origin={"test": key})
        service.change(receipt["document_id"], purpose="long_term", expected_state=0, reason="整理")
        ref = service.publish(service.prepare(receipt["revision_id"])["candidate_id"])
        if key == "withdrawn":
            service.change(receipt["document_id"], action="withdraw", expected_state=1, reason="撤回")
        elif key == "changed_state":
            with service.transaction() as db:
                db.execute("UPDATE documents SET state=state+1 WHERE id=?", (receipt["document_id"],))
        else:
            (tmp_path / ref["path"]).write_text("modified outside publisher", encoding="utf-8")
    # Recreate only the old five-column v1 table; keep all original records,
    # publications, indexes and source files as an actual upgrade fixture.
    with sqlite3.connect(service.db) as db:
        db.execute("ALTER TABLE documents RENAME TO old_documents")
        db.execute("CREATE TABLE documents(id TEXT PRIMARY KEY,current TEXT,state INTEGER,purpose TEXT,validity TEXT)")
        db.execute("INSERT INTO documents SELECT id,current,state,purpose,validity FROM old_documents")
        db.execute("DROP TABLE old_documents")
    upgraded = SourceService(tmp_path)
    assert upgraded.get(source["document_id"])["publication"] == newest["publish_id"]
    assert upgraded.read_reference(newest["publish_id"], 0)["valid"]
    with pytest.raises(SourceError):
        upgraded.read_reference(first["publish_id"], 0)
    with upgraded.transaction() as db:
        docs = db.execute("SELECT id,publication FROM documents WHERE id<>?", (source["document_id"],)).fetchall()
        assert all(row["publication"] is None for row in docs)
        db.execute("UPDATE documents SET publication=NULL WHERE id=?", (source["document_id"],))
    # Subsequent startup is not a migration and must not resurrect a null pointer.
    assert SourceService(tmp_path).get(source["document_id"])["publication"] is None


def test_personal_context_preserves_normal_chat_across_fresh_agents(tmp_path, monkeypatch):
    from threading import RLock
    from types import SimpleNamespace
    from common.runtime_identity import identity_scope
    from common import state_dir
    from agent.knowledge.purpose import refresh_evidence_context
    service, source = setup_source(tmp_path)
    service.publish(service.prepare(source["revision_id"])["candidate_id"])
    monkeypatch.setattr(state_dir, "knowledge_dir", lambda **kw: tmp_path / "knowledge")
    ordinary = [{"role": "user", "content": "我刚才说了什么？"},
                {"role": "assistant", "content": "保留正常对话"},
                {"role": "user", "content": [{"type": "image", "source": {"data": "image-bytes"}}, {"type": "text", "text": "看这个"}]}]
    def agent(messages):
        return SimpleNamespace(workspace_dir=str(tmp_path), messages=list(messages), messages_lock=RLock())
    with identity_scope(session_id="same-session", agent_id="same-agent"):
        first = agent(ordinary)
        refresh_evidence_context(first)
        assert first.messages == ordinary
        fresh = agent(ordinary)
        refresh_evidence_context(fresh)
        assert fresh.messages == ordinary
        evidence = [{"role": "assistant", "content": [{"type": "tool_use", "name": "memory_search", "id": "r"}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "r", "content": "[source_reference] document_id old"}]},
                    {"role": "assistant", "content": "旧来源推导结论"}]
        fresh.messages.extend(evidence)
        refresh_evidence_context(fresh)
        assert fresh.messages == ordinary + evidence
        service.change(source["document_id"], action="withdraw", expected_state=1, reason="撤回")
        restored = agent(ordinary + evidence)
        refresh_evidence_context(restored)
        # Tool-call partner preceding the source result is also discarded.
        assert restored.messages == ordinary
        restored_again = agent(ordinary + evidence)
        refresh_evidence_context(restored_again)
        assert restored_again.messages == ordinary


def test_source_task_cannot_write_unmanaged_knowledge(tmp_path, monkeypatch):
    from common.personal_tasks import task_scope
    from common import state_dir
    from agent.tools.write.write import Write
    from agent.tools.edit.edit import Edit
    knowledge = tmp_path / "actual" / "knowledge"
    knowledge.mkdir(parents=True)
    page = knowledge / "note.md"
    page.write_text("original", encoding="utf-8")
    monkeypatch.setattr(state_dir, "knowledge_dir", lambda **kw: knowledge)
    write = Write({"cwd": str(tmp_path / "actual")})
    edit = Edit({"cwd": str(tmp_path / "actual")})
    with task_scope("temporary-source"):
        assert write.execute(dict(path=str(page), content="temporary derivation")).status == "error"
        assert edit.execute(dict(path=str(page), oldText="original", newText="temporary derivation")).status == "error"
        unrelated = tmp_path / "other-project" / "knowledge" / "note.md"
        assert write.execute(dict(path=str(unrelated), content="project document")).status == "success"
    assert page.read_text(encoding="utf-8") == "original"
    assert write.execute(dict(path=str(page), content="legacy unmanaged edit")).status == "success"


@pytest.mark.parametrize("budget", ["files", "text"])
def test_ingest_enforces_batch_limits(tmp_path, monkeypatch, budget):
    from common.personal_tasks import TaskStore, OWNER, task_scope
    from common.runtime_identity import identity_scope
    import agent.tools.knowledge_ingest as module
    assert module.MAX_BATCH_FILES == 100
    assert module.MAX_BATCH_TEXT_BYTES == 200 * 1024 * 1024
    store = TaskStore(tmp_path / "tasks")
    task = store.submit(OWNER, "budget", {"text": "整理"}, {"channel": "web"}, "cfg")
    for number in range(2):
        path = tmp_path / f"budget-{number}.txt"
        path.write_text("budget-data", encoding="utf-8")
        store.attach(OWNER, task["task_id"], path)
    store.claim(OWNER, task["task_id"])
    if budget == "files":
        monkeypatch.setattr(module, "MAX_BATCH_FILES", 1)
    else:
        monkeypatch.setattr(module, "MAX_BATCH_TEXT_BYTES", 15)
    with identity_scope(user_id=OWNER), task_scope(task["task_id"]):
        result = module.KnowledgeIngest({"cwd": str(tmp_path), "task_store": store}).execute(
            dict(task_id=task["task_id"], purpose="long_term", reason="发布预算测试"))
    assert result.status == "error" and result.result["code"] == "limit_exceeded"
    assert store.get(OWNER, task["task_id"])["status"] == "failed"
    assert not SourceService(tmp_path).search("budget-data")
