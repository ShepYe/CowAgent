"""真实 Agent/AgentLLMModel/HTTP/SSE 链路，不替换生产媒体边界。"""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent.protocol import Agent
from agent.protocol.tool_media import ToolMediaContent, ToolMediaReference
from agent.tools.base_tool import BaseTool, ToolResult
from bridge.agent_bridge import AgentLLMModel
from bridge.bridge import Bridge
from config import Config, config_scope
from models.openai_compatible_bot import OpenAICompatibleBot


@pytest.fixture
def http_peer():
    requests = []
    mode = {"value": "ok"}
    cancel = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            def send(event):
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
            try:
                index = len(requests)
                if index == 2 and mode["value"] == "fail":
                    send({"type": "response.failed", "response": {"status": "failed", "error": {
                        "message": "C:/private/source.pdf data:application/pdf;base64,SECRET"}}})
                    return
                if index == 2 and mode["value"] == "cancel":
                    cancel.set()
                    for _ in range(16):
                        send({"type": "response.output_text.delta", "delta": "partial"})
                    return
                if index <= 2:
                    name = "read_media" if index == 1 else "observe"
                    send({"type": "response.output_item.added", "output_index": 0, "item": {
                        "type": "function_call", "id": "item-" + str(index),
                        "call_id": "call-" + str(index), "name": name}})
                    send({"type": "response.function_call_arguments.delta", "output_index": 0, "delta": "{}"})
                else:
                    send({"type": "response.output_text.delta", "delta": "receipt-ok"})
                output = [{"type": "function_call", "call_id": "call-" + str(index), "name": name, "arguments": "{}"}] if index <= 2 else [{"type": "message", "content": [{"type": "output_text", "text": "receipt-ok"}]}]
                send({"type": "response.completed", "response": {"status": "completed", "id": "resp-" + str(index),
                      "output": output, "usage": {"input_tokens": 42, "output_tokens": 5}}})
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield requests, mode, cancel, "http://127.0.0.1:" + str(server.server_port) + "/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("mode_name", ["ok", "cancel", "fail"])
def test_real_agent_media_round_trip(tmp_path, monkeypatch, http_peer, mode_name, caplog):
    requests, mode, cancel, base = http_peer
    mode["value"] = mode_name
    states = []
    contexts = []
    saved_responses = []
    # 真实PDF（不含文字层）与PNG字节；检查HTTP实际收到的字节。
    from PIL import Image
    from io import BytesIO
    pdf = BytesIO()
    png = BytesIO()
    Image.new("RGB", (20, 20), "blue").save(pdf, "PDF")
    Image.new("RGB", (20, 20), "red").save(png, "PNG")
    raw = pdf.getvalue()
    class Bot(OpenAICompatibleBot):
        def get_api_config(self):
            return {"model": "gpt-6-test", "api_type": "responses", "api_key": "test", "api_base": base}
    monkeypatch.setattr("models.bot_factory.create_bot", lambda *a, **kw: Bot())
    monkeypatch.setattr("agent.tools.tool_manager.ToolManager.sync_mcp_into_agent", lambda *a: None)
    def load(context):
        contexts.append(context)
        assert context["protocol"] == "responses" and context["model"] == "gpt-6-test"
        assert context["api_base"] == base
        assert context["config_revision"].startswith("cfg1-")
        assert context["provider"] == model._resolve_bot_type(model.model)
        assert context["mime_type"] == "application/pdf" and context["transport"] == "inline"
        return ToolMediaContent("application/pdf", raw)
    class Read(BaseTool):
        name = "read_media"
        description = "读取测试资料"
        params = {"type": "object", "properties": {}}
        def execute(self, args):
            return ToolResult.success({"reading_id": "reading-safe", "text": "x" * 51000}, media=[
                ToolMediaReference(load, supports=lambda c: True, on_delivery=states.append, on_response=saved_responses.append),
                ToolMediaReference(lambda c: ToolMediaContent("image/png", png.getvalue()), supports=lambda c: True)])
    class Observe(BaseTool):
        name = "observe"
        description = "提交观察"
        params = {"type": "object", "properties": {}}
        def execute(self, args):
            return ToolResult.success("receipt-ok")
    events = []
    executors = []
    cfg = Config({"model": "gpt-6-test", "bot_type": "chat", "chat_fallback": {"chain": []}})
    with config_scope(cfg):
        model = AgentLLMModel(Bridge(), bot_type="chat")
        agent = Agent("测试工具媒体往返", model=model, tools=[Read(), Observe()],
            workspace_dir=str(tmp_path), max_steps=4, enable_skills=False,
            skip_context_files=True, output_mode="logger")
        try:
            result = agent.run_stream("读测试资料", cancel_event=cancel, on_event=lambda *args: events.append(args), on_executor=executors.append)
        except Exception:
            if mode_name != "fail":
                raise
            result = "failed"
    history = executors[0].messages
    serialized = json.dumps(history)
    assert "file_data" not in serialized and "base64," not in serialized
    assert "reading-safe" in serialized
    from agent.protocol.agent_stream import compress_turn_to_text_only
    if mode_name == "ok":
        compacted = compress_turn_to_text_only({"messages": history})
        assert "reading-safe" in json.dumps(compacted)
        assert "receipt-ok" in json.dumps(compacted)
    assert not executors[0]._pending_tool_media
    assert len(contexts) == 1
    assert states == ["sent", "completed" if mode_name == "ok" else "unknown"]
    if mode_name == "ok":
        assert saved_responses == [{"id": "resp-2", "status": "completed", "usage": {"input_tokens": 42, "output_tokens": 5},
            "output": [{"type": "function_call", "call_id": "call-2", "name": "observe", "arguments": "{}"}]}]
    else:
        assert saved_responses == []
    outputs = [i for i in requests[1]["input"] if i.get("type") == "function_call_output"]
    media = next(i for i in outputs if isinstance(i["output"], list))
    assert media["call_id"] == "call-1"
    assert "Output truncated" in media["output"][0]["text"]
    assert base64.b64decode(media["output"][1]["file_data"].split(",")[1]) == raw
    assert media["output"][2]["type"] == "input_image"
    assert base64.b64decode(media["output"][2]["image_url"].split(",")[1]) == png.getvalue()
    for req in requests[2:]:
        assert "file_data" not in json.dumps(req)
    assert "file_data" not in json.dumps(events, default=str)
    assert "base64," not in json.dumps(events, default=str)
    assert "SECRET" not in caplog.text and "C:/private/source.pdf" not in caplog.text
    if mode_name == "ok":
        assert result == "receipt-ok" and len(requests) == 3


def test_compression_keeps_reading_reference_and_result():
    from agent.protocol.agent_stream import compress_turn_to_text_only
    turn = {"messages": [
        {"role": "user", "content": "读资料"},
        {"role": "assistant", "content": [{"type": "tool_use", "name": "read", "id": "c", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c", "content":
            json.dumps({"reading_id": "reading-safe", "status": "prepared"})}]},
        {"role": "assistant", "content": "蓝色图形"}]}
    result = compress_turn_to_text_only(turn)
    text = json.dumps(result, ensure_ascii=False)
    assert "reading-safe" in text and "蓝色图形" in text
    assert "tool_result" not in text and "base64" not in text
