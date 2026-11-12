"""真实 bot/媒体/阅读/发布链路的离线预算回归，仅替换 HTTP 传输。"""
import base64
from copy import deepcopy
from io import BytesIO
import json
import socket
from types import SimpleNamespace

import pytest
from pypdf import PdfReader, PdfWriter
import requests

from agent.knowledge.personal_assistant import SourceService
from agent.knowledge.reading import ReadingService
from agent.knowledge.reading_materials import PdfBudget
from agent.knowledge.reading_publication import publication_state
from agent.knowledge.wiki import WikiService
from agent.protocol.tool_media import MediaDelivery, _pending, _provider
from agent.tools.knowledge_ingest import KnowledgeIngest
from agent.tools.read.read import Read
from common.personal_tasks import OWNER, TaskStore, task_scope
from common.runtime_identity import identity_scope
from models.config_revision import capture_config_revision
from models.openai.openai_http_client import OpenAIHTTPError
from models.openai_compatible_bot import OpenAICompatibleBot
from tests.test_reading_capabilities import ProbeModel
from tests.test_reading_ingest_flow import submission


@pytest.fixture
def transport_flow(tmp_path, monkeypatch, request):
    def no_network(*args, **kwargs):
        pytest.fail("传输预算测试禁止出网")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(requests.Session, "request", no_network)
    monkeypatch.setattr("agent.knowledge.reading_capabilities.ensure_pdf_capability", lambda *a: True)
    workspace = tmp_path / "instance" / "workspace"
    workspace.mkdir(parents=True)
    store = TaskStore(workspace.parent / "personal")
    task = store.submit(OWNER, "transport-budget", {}, {"channel": "web"}, capture_config_revision())
    writer = PdfWriter()
    for page in range(1, getattr(request, "param", 8) + 1):
        writer.add_blank_page(width=200 + page, height=300)
    source = tmp_path / "source.pdf"
    writer.write(source)
    attachment = store.attach(OWNER, task["task_id"], source)
    task = store.claim(OWNER, task["task_id"])
    config = dict(cwd=str(workspace), task_store=store, document_reading_native_pdf_enabled=True,
                  reading_pdf_budget=PdfBudget(max_pages=4))

    class OfflineBot(OpenAICompatibleBot):
        def get_api_config(self):
            return dict(api_type="responses", api_base="https://offline.invalid/v1", model="offline-model")

    bot = OfflineBot()
    read = Read(config)
    read.model = ProbeModel()
    read.model.bot = bot
    from agent.knowledge.reading_capabilities import capability_identity, _key
    capability = capability_identity(read.model, task["config_revision"])
    service = SourceService(workspace)
    with service.transaction() as db:
        db.execute("CREATE TABLE reading_capabilities(id TEXT PRIMARY KEY,record TEXT)")
        db.execute("INSERT INTO reading_capabilities VALUES(?,?)", (_key(capability), json.dumps(
            dict(schema_version=1, state="supported", identity=capability))))
    errors = []
    report_error = MediaDelivery.report_error

    def report(delivery, classified):
        report_error(delivery, classified)
        errors.append(deepcopy(classified))

    monkeypatch.setattr(MediaDelivery, "report_error", report)
    with identity_scope(user_id=OWNER, session_id="transport-session"), task_scope(task["task_id"]):
        tool = KnowledgeIngest(config)
        result = tool.execute(dict(task_id=task["task_id"], purpose="long_term", reason="离线预算测试"))
        assert result.status == "success", result.result
        yield SimpleNamespace(root=workspace, store=store, task=task, entry=attachment, read=read,
                              tool=tool, bot=bot, errors=errors, calls=[], monkeypatch=monkeypatch)


def read_pages(env, pages):
    result = env.read.execute(dict(path=env.entry["path"], pages=pages))
    assert result.status == "success", result.result
    return result


def transport(env, result, *, failure=None, stream=True, upstream_code=None):
    """只替换 HTTP 客户端；分类、回调、响应落盘由真实 bot 负责。"""
    assert len(result.media) == 1
    number = len(env.calls) + 1
    call_id = "read-" + str(number)
    response = dict(id="response-" + str(number), status="completed", usage={"input_tokens": 20},
                    output=[dict(type="message", role="assistant", content=[
                        dict(type="output_text", text=f"响应 {number}：已阅读 {result.result['target_pages']} 页。")])])

    class Client:
        def responses(self, **kwargs):
            outputs = [item for item in kwargs["input"] if item.get("type") == "function_call_output"]
            assert len(outputs) == 1
            files = [part for part in outputs[0]["output"] if part["type"] == "input_file"]
            assert len(files) == 1
            data = base64.b64decode(files[0]["file_data"].split(",", 1)[1])
            actual_pages = [int(page.mediabox.width) - 200 for page in PdfReader(BytesIO(data)).pages]
            assert actual_pages == result.result["page_map"]
            env.calls.append(dict(reading_id=result.result["reading_id"],
                                  segment_id=result.result["segment_id"], pages=actual_pages))
            if failure is not None:
                status, message = failure
                if upstream_code is not None:
                    assert kwargs["stream"]
                    return iter([dict(type="response.failed", status_code=status,
                                      response=dict(status="failed", error=dict(
                                          code=upstream_code, message=message)))])
                raise OpenAIHTTPError(status, {"error": {"message": message}})
            if kwargs["stream"]:
                return iter([dict(type="response.output_text.delta", delta="本批观察"),
                             dict(type="response.completed", response=deepcopy(response))])
            return deepcopy(response)

    env.monkeypatch.setattr(env.bot, "_get_http_client", lambda: Client())
    messages = [dict(role="assistant", content=[dict(type="tool_use", id=call_id, name="read", input={})]),
                dict(role="user", content=[dict(type="tool_result", tool_use_id=call_id,
                                              content=json.dumps(result.result, ensure_ascii=False))])]
    pending = {call_id: result.media}
    token = _pending.set(pending)
    provider_token = _provider.set("custom:test")
    try:
        outcome = env.bot.call_with_tools(messages, stream=stream)
        outcome = list(outcome) if stream else outcome
    finally:
        _provider.reset(provider_token)
        _pending.reset(token)
    assert not pending
    if failure is None:
        if stream:
            assert not any(item.get("error") for item in outcome)
        else:
            assert not outcome.get("error")
    return outcome, response


def registry(env):
    return ReadingService(env.root, task_store=env.store)


def segment(env, result):
    return next(s for s in registry(env).get(env.task, result.result["reading_id"])["segments"]
                if s["segment_id"] == result.result["segment_id"])


def publish(env, result, submission_id):
    args = submission(env.task, result.result)
    args["reading_submission_id"] = submission_id
    page = result.result["target_pages"][0]
    args["reading_evidence"][0].update(location=dict(kind="pdf", physical_page=page),
                                       observation=f"本批首个物理页为第 {page} 页，页面宽度为 {200 + page}。")
    args["pages"][0].update(title=f"第 {page} 页批次", summary=f"第 {page} 页及本批来源整理。")
    args["pages"][0]["blocks"][0]["content"] = f"第 {page} 页批次的离线观察。"
    published = env.tool.execute(args)
    assert published.status == "success", published.result
    return args, published


@pytest.mark.parametrize("stream", [False, True], ids=["sync", "stream"])
@pytest.mark.parametrize("failure,kind", [((413, "payload too large"), "input_bytes"),
                                       ((400, "context_length_exceeded"), "input_context")])
def test_transport_budget_replans_then_persists_and_publishes_without_repeating_old_products(
        transport_flow, failure, kind, stream):
    env = transport_flow
    first = read_pages(env, "1-8")
    _, first_response = transport(env, first, stream=stream)
    old_args, _ = publish(env, first, "old-published")
    old_segment = deepcopy(segment(env, first))
    assert old_segment["full_response_saved"] and old_segment["state"] == "completed"
    assert old_segment["saved_model_output"] == first_response["output"]
    old_refs = deepcopy(env.store.get(OWNER, env.task["task_id"])["result_refs"])
    old_body = WikiService(env.root).read_reference(old_refs[0]["publish_id"])
    ledger_before = publication_state(env.store, env.task)

    failed = read_pages(env, "1-8")
    assert failed.result["target_pages"] == [5, 6, 7, 8]
    outcome, _ = transport(env, failed, failure=failure, stream=stream)
    error = outcome[0] if stream else outcome
    assert error["error"]["code"] == "reading_budget_exceeded"
    assert env.errors[-1]["error"]["budget_kind"] == kind
    failed_state = segment(env, failed)
    assert failed_state["state"] == "unknown" and failed_state["budget_kind"] == kind
    assert failed_state["error_code"] == "reading_budget_exceeded"
    assert not failed_state.get("full_response_saved") and not failed_state.get("delivery_receipt_id")
    parent_after_failure = deepcopy(registry(env).get(env.task, first.result["reading_id"]))
    assert publication_state(env.store, env.task) == ledger_before

    child_first = read_pages(env, "1-8")
    assert child_first.result["reading_id"] != first.result["reading_id"]
    child = registry(env).get(env.task, child_first.result["reading_id"])
    assert child["continuation_from"]["reading_id"] == first.result["reading_id"]
    assert [s["target_pages"] for s in child["segments"]] == [[5, 6], [7, 8]]
    assert child_first.result["target_pages"] == [5, 6]
    assert registry(env).get(env.task, first.result["reading_id"]) == parent_after_failure
    _, saved = transport(env, child_first, stream=stream)
    saved_first = deepcopy(segment(env, child_first))
    assert saved_first["state"] == "completed" and saved_first["full_response_saved"]
    assert saved_first["response_id"] == saved["id"]
    assert saved_first["saved_model_output"] == saved["output"]
    assert saved_first["usage"] == saved["usage"] and saved_first["delivery_receipt_id"]
    assert saved_first["response_checkpoint"] == "response_saved"
    assert saved_first["response_hash"]
    child_args, _ = publish(env, child_first, "child-first")

    child_second = read_pages(env, "1-8")
    assert child_second.result["reading_id"] == child_first.result["reading_id"]
    assert child_second.result["target_pages"] == [7, 8]
    assert segment(env, child_first) == saved_first
    transport(env, child_second, stream=stream)
    # 子段全完成时，原始页窗继续 read 也不能重复展开已完成媒体。
    cached = read_pages(env, "1-8")
    assert not cached.media
    assert cached.result["reading_id"] == child_first.result["reading_id"]
    assert cached.result["segment_id"] == child_first.result["segment_id"]
    assert cached.result["reading_state"] == "completed"
    assert cached.result["saved_model_output"] == saved_first["saved_model_output"]
    publish(env, child_second, "child-second")
    current = env.store.get(OWNER, env.task["task_id"])
    assert current["status"] == "succeeded" and len(current["result_refs"]) == 3
    assert current["result_refs"][:1] == old_refs
    assert WikiService(env.root).read_reference(old_refs[0]["publish_id"]) == old_body
    with SourceService(env.root).transaction() as db:
        assert db.execute("SELECT count(*) FROM wiki_publications").fetchone()[0] == 3
    assert env.tool.execute(deepcopy(old_args)).status == "success"
    assert env.tool.execute(deepcopy(child_args)).status == "success"
    assert env.store.get(OWNER, env.task["task_id"])["result_refs"] == current["result_refs"]
    assert not read_pages(env, "1-8").media
    state = publication_state(env.store, env.task)
    assert state["targets"] == ledger_before["targets"] and not state["remaining"]
    assert len(state["batches"]) == 3
    assert len(env.errors) == 1
    with SourceService(env.root).transaction() as db:
        assert db.execute("SELECT count(*) FROM wiki_publications").fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM reading_records").fetchone()[0] == 2
    assert env.calls == [dict(reading_id=first.result["reading_id"], segment_id=first.result["segment_id"], pages=[1, 2, 3, 4]),
                         dict(reading_id=failed.result["reading_id"], segment_id=failed.result["segment_id"], pages=[5, 6, 7, 8]),
                         dict(reading_id=child_first.result["reading_id"], segment_id=child_first.result["segment_id"], pages=[5, 6]),
                         dict(reading_id=child_second.result["reading_id"], segment_id=child_second.result["segment_id"], pages=[7, 8])]


def test_upstream_stream_break_does_not_automatically_replan(transport_flow):
    env = transport_flow
    result = read_pages(env, "1-8")
    outcome, _ = transport(env, result, failure=(502, "upstream_stream_break"))
    assert outcome[0]["error"]["code"] != "reading_budget_exceeded"
    assert env.errors[-1]["error"]["code"] != "reading_budget_exceeded"
    state = segment(env, result)
    assert state["state"] == "unknown"
    assert not state.get("budget_kind") and not state.get("delivery_receipt_id")
    # 错误回调自身不缩批或重发；后续显式同页 read 的恢复契约另测。
    records = registry(env).for_task(env.task)
    assert len(records) == 1 and len(env.calls) == 1
    assert all(not record.get("continuation_from") for record in records)
    assert not env.store.get(OWNER, env.task["task_id"]).get("result_refs")


def test_explicit_same_page_read_after_unknown_creates_new_reading_and_preserves_old_unknown(transport_flow):
    """缺陷回归：同页显式补读应创建新交付身份，不能重置旧 unknown。"""
    env = transport_flow
    first = read_pages(env, "1-8")
    outcome, _ = transport(env, first, failure=(502, "上游流已中断"), upstream_code="upstream_stream_break")
    assert outcome[0]["error"]["upstream_code"] == "upstream_stream_break"
    assert outcome[0]["error"]["code"] != "reading_budget_exceeded"
    old_record = deepcopy(registry(env).get(env.task, first.result["reading_id"]))
    old_segment = segment(env, first)
    assert old_segment["state"] == "unknown"
    assert not old_segment.get("full_response_saved") and not old_segment.get("delivery_receipt_id")
    assert not old_segment.get("budget_kind")
    assert len(env.calls) == 1 and len(registry(env).for_task(env.task)) == 1
    ledger_before = deepcopy(publication_state(env.store, env.task))

    # 新请求来自显式 read；错误回调不能自动重试或把 unknown 改回 prepared。
    retry = read_pages(env, "1-8")
    assert retry.result["reading_id"] != first.result["reading_id"]
    assert retry.result["target_pages"] == first.result["target_pages"] == [1, 2, 3, 4]
    assert retry.result["page_map"] == first.result["page_map"]
    assert retry.result["reading_state"] == "prepared" and len(retry.media) == 1
    assert registry(env).get(env.task, first.result["reading_id"]) == old_record
    assert len(registry(env).for_task(env.task)) == 2
    assert publication_state(env.store, env.task) == ledger_before
    assert len(env.calls) == 1  # read 只准备媒体，尚未发起补读传输。

    _, response = transport(env, retry)
    completed = segment(env, retry)
    assert completed["state"] == "completed" and completed["full_response_saved"]
    assert completed["response_id"] == response["id"]
    assert completed["saved_model_output"] == response["output"]
    assert completed["delivery_receipt_id"]
    saved_record = deepcopy(registry(env).get(env.task, retry.result["reading_id"]))
    for _ in range(3):
        cached = read_pages(env, "1-8")
        assert not cached.media
        assert cached.result["reading_id"] == retry.result["reading_id"]
        assert cached.result["reading_state"] == "completed"
        assert cached.result["saved_model_output"] == response["output"]
    assert registry(env).get(env.task, retry.result["reading_id"]) == saved_record
    assert len(registry(env).for_task(env.task)) == 2
    assert registry(env).get(env.task, first.result["reading_id"]) == old_record
    assert [call["pages"] for call in env.calls] == [[1, 2, 3, 4], [1, 2, 3, 4]]
    assert not env.store.get(OWNER, env.task["task_id"]).get("result_refs")


@pytest.mark.parametrize("status,upstream_code", [(500, None), (502, "upstream_stream_break")],
                         ids=["ordinary-500", "upstream-stream-break"])
def test_unknown_same_page_retry_chain_stops_after_two_retries_without_shrinking(
        transport_flow, status, upstream_code):
    env = transport_flow
    first = read_pages(env, "1-8")
    current = first
    snapshots = {}
    ledger_before = deepcopy(publication_state(env.store, env.task))
    for number in range(3):  # 初次发送 + 两次显式补读，三次都返回未知结果。
        transport(env, current, failure=(status, "上游请求失败"), upstream_code=upstream_code)
        assert env.errors[-1]["error"]["code"] != "reading_budget_exceeded"
        state = segment(env, current)
        assert state["state"] == "unknown"
        assert not state.get("budget_kind") and not state.get("full_response_saved")
        assert not state.get("delivery_receipt_id")
        rid = current.result["reading_id"]
        snapshots[rid] = deepcopy(registry(env).get(env.task, rid))
        if number == 2:
            break
        retry = read_pages(env, "1-8")
        assert retry.result["reading_id"] not in snapshots
        assert retry.result["target_pages"] == first.result["target_pages"] == [1, 2, 3, 4]
        assert retry.result["page_map"] == first.result["page_map"]
        child = registry(env).get(env.task, retry.result["reading_id"])
        assert child["unknown_retry_number"] == number + 1
        assert child["continuation_from"]["reading_id"] == rid
        assert child["continuation_from"]["reason"] == "unknown_retry"
        assert child["manifest"]["budget"] == snapshots[rid]["manifest"]["budget"]
        assert len(child["segments"]) == 1  # 普通 500/断流不进行预算缩页。
        repeated = read_pages(env, "1-8")
        assert repeated.result["reading_id"] == retry.result["reading_id"]
        assert len(env.calls) == number + 1  # 准备新媒体不触发 HTTP。
        current = retry

    for _ in range(3):
        exhausted = env.read.execute(dict(path=env.entry["path"], pages="1-8"))
        assert exhausted.status == "error" and not exhausted.media
        assert exhausted.result["code"] == "reading_incomplete"
    assert len(env.calls) == 3
    assert [call["pages"] for call in env.calls] == [[1, 2, 3, 4]] * 3
    assert {record["reading_id"]: record for record in registry(env).for_task(env.task)} == snapshots
    assert publication_state(env.store, env.task) == ledger_before
    assert not env.store.get(OWNER, env.task["task_id"]).get("result_refs")


@pytest.mark.parametrize("transport_flow", [1], indirect=True)
def test_single_page_budget_failure_stops_without_infinite_children(transport_flow):
    env = transport_flow
    result = read_pages(env, "1")
    transport(env, result, failure=(413, "payload too large"))
    before = deepcopy(registry(env).for_task(env.task))
    for _ in range(3):
        retry = env.read.execute(dict(path=env.entry["path"], pages="1"))
        assert retry.status == "error" and not retry.media
        assert retry.result["code"] == "reading_budget_exceeded"
    assert registry(env).for_task(env.task) == before
    assert len(before) == 1 and len(env.calls) == 1
    assert segment(env, result)["state"] == "unknown"
    assert not env.store.get(OWNER, env.task["task_id"]).get("result_refs")
