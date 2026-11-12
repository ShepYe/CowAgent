"""图片能力独立凭据：真实首轮 tool call 收集、离线 HTTP 边界与 MIME 隔离。"""
import base64
from io import BytesIO
import json

from PIL import Image
import pytest

from agent.knowledge import image_reading_capabilities as cap
from agent.knowledge.personal_assistant import SourceService
from agent.knowledge.reading_capabilities import _key, capability_identity
from models.config_revision import capture_config_revision, ConfigUnavailable
from tests.test_reading_capabilities import ProbeModel


def records(root):
    with SourceService(root).transaction() as db:
        return [json.loads(row[0]) for row in db.execute("SELECT record FROM reading_capabilities")]


@pytest.fixture
def transport(monkeypatch):
    calls = []
    monkeypatch.setattr("secrets.randbelow", lambda n: 372913)
    def send(model, request, pending):
        assert list(pending) == ["call_model_generated"]
        assert request.messages[1]["content"][-1]["id"] == "call_model_generated"
        assert request.messages[2]["content"][0]["tool_use_id"] == "call_model_generated"
        assert "472913" not in str(request.messages)
        assert [t["name"] for t in request.tools] == ["read_probe"]
        ref = next(iter(pending.values()))[0]
        context = dict(protocol="responses", provider=model.bot_type, model=model.model,
                       api_base=model.bot.get_api_config()["api_base"], mime_type="application/pdf",
                       transport="inline", config_revision=capture_config_revision())
        assert ref.supports(context)
        media = ref.loader(context)
        calls.append((request, ref, context, media))
        ref.on_delivery("sent")
        ref.on_delivery("completed")
        yield {"choices": [{"delta": {"content": "472913"}, "finish_reason": "stop"}]}
    monkeypatch.setattr("agent.protocol.tool_media.stream_with_tool_media", send)
    return calls


def test_each_mime_uses_own_fixture_identity_and_cache(tmp_path, transport):
    model, revision = ProbeModel(), capture_config_revision()
    for mime, fmt in cap.MIME_FORMATS.items():
        assert cap.ensure_image_capability(tmp_path, model, revision, mime)
        assert cap.ensure_image_capability(tmp_path, model, revision, mime)
        media = transport[-1][3]
        assert media.mime_type == mime and len(media.data) <= 128 * 1024
        assert Image.open(BytesIO(media.data)).format == fmt
        assert Image.open(BytesIO(media.data)).size == (900, 600)
    assert len(transport) == 4
    saved = records(tmp_path)
    assert len(saved) == 4 and all(r["state"] == "supported" for r in saved)
    assert all(r["identity"]["mime_type"] != "application/pdf" for r in saved)
    assert all("472913" not in json.dumps(r) for r in saved)


def test_pdf_supported_not_inherited_or_modified(tmp_path, transport):
    model, revision = ProbeModel(), capture_config_revision()
    pdf = capability_identity(model, revision)
    old = dict(schema_version=1, probe_version=4, identity=pdf, state="supported", verified_image=True)
    with SourceService(tmp_path).transaction() as db:
        db.execute("CREATE TABLE reading_capabilities(id TEXT PRIMARY KEY, record TEXT)")
        db.execute("INSERT INTO reading_capabilities VALUES(?,?)", (_key(pdf), json.dumps(old)))
    assert cap.ensure_image_capability(tmp_path, model, revision, "image/png")
    assert len(transport) == 1
    saved = records(tmp_path)
    assert old in saved and len(saved) == 2
    assert _key(cap.image_capability_identity(model, revision, "image/png")) != _key(pdf)


@pytest.mark.parametrize("dimension", ["model", "provider", "endpoint", "revision"])
def test_identity_change_requires_new_probe(tmp_path, monkeypatch, transport, dimension):
    from config import Config, config_scope
    with config_scope(Config({"revision_marker": 1})):
        model, revision = ProbeModel(), capture_config_revision()
        assert cap.ensure_image_capability(tmp_path, model, revision, "image/png")
        if dimension == "model":
            model.model = "other-model"
        elif dimension == "provider":
            model.bot_type = "custom:other"
        elif dimension == "endpoint":
            monkeypatch.setattr(model.bot, "get_api_config", lambda: dict(api_type="responses", api_base="https://other.invalid/v1"))
        if dimension == "revision":
            with config_scope(Config({"revision_marker": 2})):
                assert cap.ensure_image_capability(tmp_path, model, capture_config_revision(), "image/png")
        else:
            assert cap.ensure_image_capability(tmp_path, model, revision, "image/png")
    assert len(transport) == 2


@pytest.mark.parametrize("changed", [dict(provider="other"), dict(protocol="chat"), dict(model="other"),
    dict(api_base="https://wrong.invalid/v1"), dict(config_revision="old"), dict(transport="files")])
def test_loader_rejects_wrong_context_but_ignores_context_mime(tmp_path, transport, changed):
    from agent.protocol.tool_media import ToolMediaError
    assert cap.ensure_image_capability(tmp_path, ProbeModel(), capture_config_revision(), "image/png")
    _, ref, context, _ = transport[0]
    for mime in ("application/pdf", "image/jpeg", None):
        assert ref.supports(dict(context, mime_type=mime))
        assert ref.loader(dict(context, mime_type=mime)).mime_type == "image/png"
    assert not ref.supports(dict(context, **changed))
    with pytest.raises(ToolMediaError):
        ref.loader(dict(context, **changed))


@pytest.mark.parametrize("failure", ["error", "exception", "wrong_code", "no_receipt", "late_unknown", "no_load", "truncated"])
def test_failure_unknown_no_internal_retry_and_later_explicit_probe(tmp_path, monkeypatch, transport, failure):
    good = __import__("agent.protocol.tool_media", fromlist=["stream_with_tool_media"]).stream_with_tool_media
    calls = []
    def fail(model, request, pending):
        calls.append(1)
        ref = next(iter(pending.values()))[0]
        if failure == "error":
            yield {"error": {"code": "rate_limit_exceeded"}}
            return
        if failure == "exception":
            raise TimeoutError("private endpoint details")
        context = dict(protocol="responses", model=model.model, api_base=model.bot.get_api_config()["api_base"],
                       config_revision=capture_config_revision())
        if failure != "no_load":
            ref.loader(context)
        if failure != "no_receipt":
            ref.on_delivery("sent")
            ref.on_delivery("completed")
        if failure == "late_unknown":
            ref.on_delivery("unknown")
        yield {"choices": [{"delta": {"content": "000000" if failure == "wrong_code" else "472913"},
                            "finish_reason": "length" if failure == "truncated" else "stop"}]}
    monkeypatch.setattr("agent.protocol.tool_media.stream_with_tool_media", fail)
    model, revision = ProbeModel(), capture_config_revision()
    assert not cap.ensure_image_capability(tmp_path, model, revision, "image/png")
    assert len(calls) == 1 and records(tmp_path)[0]["state"] == "unknown"
    assert "private endpoint" not in json.dumps(records(tmp_path))
    monkeypatch.setattr("agent.protocol.tool_media.stream_with_tool_media", good)
    assert cap.ensure_image_capability(tmp_path, model, revision, "image/png")


@pytest.mark.parametrize("case", ["no_call", "wrong_name", "bad_arguments", "missing_id", "error"])
def test_first_round_failure_never_sends_image(tmp_path, monkeypatch, case):
    class Model(ProbeModel):
        def call_stream(self, request):
            if case == "error":
                yield {"error": True}
                return
            if case != "no_call":
                yield {"choices": [{"delta": {"tool_calls": [dict(index=0, type="function",
                    id="" if case == "missing_id" else "real-call", function=dict(
                        name="wrong" if case == "wrong_name" else "read_probe",
                        arguments="{" if case == "bad_arguments" else "{}"))]}}]}
            yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
    monkeypatch.setattr("agent.protocol.tool_media.stream_with_tool_media", lambda *a: pytest.fail("首轮失败不得第二轮"))
    assert not cap.ensure_image_capability(tmp_path, Model(), capture_config_revision(), "image/png")
    assert records(tmp_path)[0]["state"] == "unknown"


def test_fixture_font_failure_before_any_paid_call(tmp_path, monkeypatch):
    monkeypatch.setattr("PIL.ImageFont.truetype", lambda *a, **k: (_ for _ in ()).throw(OSError("no font")))
    model = ProbeModel()
    monkeypatch.setattr(model, "call_stream", lambda *a: pytest.fail("缺字体不得付费"))
    assert not cap.ensure_image_capability(tmp_path, model, capture_config_revision(), "image/png")
    assert records(tmp_path)[0]["state"] == "unknown"


def test_unsupported_protocol_mime_and_stale_revision_no_probe(tmp_path, monkeypatch):
    from config import Config, config_scope
    model = ProbeModel()
    monkeypatch.setattr(model, "call_stream", lambda *a: pytest.fail("不允许调用"))
    with pytest.raises(ValueError, match="unsupported_image_mime"):
        cap.ensure_image_capability(tmp_path, model, capture_config_revision(), "image/emf")
    monkeypatch.setattr(model.bot, "get_api_config", lambda: dict(api_type="chat"))
    assert not cap.ensure_image_capability(tmp_path, model, capture_config_revision(), "image/png")
    with config_scope(Config({"revision_marker": 1})):
        old = capture_config_revision()
    with config_scope(Config({"revision_marker": 2})):
        with pytest.raises(ConfigUnavailable):
            cap.ensure_image_capability(tmp_path, model, old, "image/png")


def test_two_round_real_adapter_http_boundary(tmp_path, monkeypatch):
    from models.openai_compatible_bot import OpenAICompatibleBot
    class Bot(OpenAICompatibleBot):
        def get_api_config(self):
            return dict(api_type="responses", api_base="https://offline.invalid/v1")
    class Model:
        model, bot_type = "offline-model", "custom:test"
        bot = Bot()
        def call_stream(self, request):
            assistant = next((m for m in request.messages if m.get("role") == "assistant"), None)
            messages = [dict(role="user", content="请调用read_probe。")] if assistant is None else request.messages
            yield from self.bot.call_with_tools(messages=messages, tools=request.tools, model=self.model,
                                                stream=True, max_tokens=request.max_tokens, system=request.system)
    calls = []
    class Http:
        def responses(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                yield {"type": "response.output_item.added", "output_index": 0,
                       "item": {"type": "function_call", "id": "fc", "call_id": "real-http-call", "name": "read_probe", "arguments": ""}}
                yield {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": "{}"}
            else:
                parts = [p for i in kwargs["input"] if i.get("type") == "function_call_output" for p in i["output"]]
                image = next(p for p in parts if p["type"] == "input_image")
                raw = base64.b64decode(image["image_url"].split(",", 1)[1])
                assert Image.open(BytesIO(raw)).format == "PNG"
                assert not any(p["type"] == "input_file" for p in parts)
                assert "472913" not in str(kwargs["input"])
                yield {"type": "response.output_text.delta", "delta": "472913"}
            yield {"type": "response.completed", "response": {"status": "completed", "output": []}}
    model = Model()
    monkeypatch.setattr(model.bot, "_get_http_client", lambda: Http())
    monkeypatch.setattr("secrets.randbelow", lambda n: 372913)
    assert cap.ensure_image_capability(tmp_path, model, capture_config_revision(), "image/png")
    assert len(calls) == 2
