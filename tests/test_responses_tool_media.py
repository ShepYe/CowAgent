"""真实类型、作用域验证与请求生命周期的边界回归。"""
import base64
from copy import deepcopy

import pytest

from agent.protocol.tool_media import (
    ToolMediaContent, ToolMediaReference, ToolMediaError, expand_tool_media,
    _pending,
)
from models.openai.responses_adapter import messages_to_input
from models.openai_compatible_bot import OpenAICompatibleBot


PDF = b"%PDF-1.7\nprivate-test-pdf"


def payload():
    return {"model": "gpt-6-test", "input": [
        {"type": "function_call_output", "call_id": "call-7", "output": "reading-7"}]}


def reference(states, loader=None, supports=lambda c: True):
    return ToolMediaReference(loader or (lambda c: ToolMediaContent("application/pdf", PDF)),
                              supports=supports, on_delivery=states.append)


def test_expansion_is_typed_and_consumed_once():
    states = []
    contexts = []
    ref = reference(states, lambda c: (contexts.append(c), ToolMediaContent("application/pdf", PDF))[1])
    pending = {"call-7": [ref]}
    body = payload()
    delivery = expand_tool_media(body, pending, {"protocol": "responses"})
    part = body["input"][0]["output"][1]
    assert part["type"] == "input_file" and part["filename"] == "document.pdf"
    assert base64.b64decode(part["file_data"].split(",")[1]) == PDF
    assert contexts == [{"protocol": "responses"}]
    assert not pending
    delivery.start()
    delivery.finish(True)
    delivery.finish(False)
    assert states == ["sent", "completed"]
    next_body = payload()
    assert not expand_tool_media(next_body, pending, {"protocol": "responses"}).refs
    assert isinstance(next_body["input"][0]["output"], str)


def test_loader_revalidates_each_expansion_and_redacts_errors():
    count = []
    def load(context):
        count.append(context)
        if len(count) > 1:
            raise ValueError("C:/private/source.pdf base64-secret")
        return ToolMediaContent("application/pdf", PDF)
    ref = reference([], load)
    expand_tool_media(payload(), {"call-7": [ref]}, {"protocol": "responses"})
    with pytest.raises(ToolMediaError, match="^media_validation_failed$"):
        expand_tool_media(payload(), {"call-7": [ref]}, {"protocol": "responses"})
    assert len(count) == 2


@pytest.mark.parametrize("protocol,supports", [("chat", True), ("responses", False), ("responses", "unknown")])
def test_unsupported_never_loads(protocol, supports):
    def load(c):
        pytest.fail("unsupported media must never be loaded")
    ref = reference([], load, lambda c: supports)
    with pytest.raises(ToolMediaError, match="attachment_unsupported"):
        expand_tool_media(payload(), {"call-7": [ref]}, {"protocol": protocol})


def test_default_capability_is_closed():
    with pytest.raises(ToolMediaError, match="attachment_unsupported"):
        expand_tool_media(payload(), {"call-7": [ToolMediaReference(lambda c: None)]}, {"protocol": "responses"})


@pytest.mark.parametrize("mime,data,code", [("text/plain", b"text", "attachment_unsupported"),
    ("application/pdf", b"fake-pdf", "invalid_tool_media")])
def test_invalid_content_is_never_sent(mime, data, code):
    with pytest.raises(ToolMediaError, match=code):
        expand_tool_media(payload(), {"call-7": [reference([], lambda c: ToolMediaContent(mime, data))]},
                          {"protocol": "responses"})


def test_compacted_away_pending_result_blocks_the_request():
    with pytest.raises(ToolMediaError, match="media_context_lost"):
        expand_tool_media({"input": []}, {"call-7": [reference([])]}, {"protocol": "responses"})


def test_unsupported_protocol_has_no_http_side_effect(monkeypatch):
    from agent.protocol.tool_media import stream_with_tool_media
    from types import SimpleNamespace
    class Bot(OpenAICompatibleBot):
        def get_api_config(self):
            return {"model": "gpt-6-test", "api_type": "chat"}
    def call(request):
        pytest.fail("unsupported protocol must be rejected before model call")
    model = SimpleNamespace(bot=Bot(), model="gpt-6-test", call_stream=call)
    with pytest.raises(ToolMediaError, match="attachment_unsupported"):
        list(stream_with_tool_media(model, None, {"call-7": [reference([])]}))


def test_network_failure_is_unknown_and_does_not_expose_echo(monkeypatch):
    bot = OpenAICompatibleBot()
    states = []
    class Client:
        def responses(self, **kw):
            raise RuntimeError("C:/private/source.pdf data:application/pdf;base64,SECRET")
    monkeypatch.setattr(bot, "_get_http_client", lambda: Client())
    token = _pending.set({"call-7": [reference(states)]})
    try:
        chunks = list(bot._handle_responses_stream(payload(), "key", "http://localhost", None, "gpt-6-test"))
        assert "SECRET" not in str(chunks)
        assert states == ["sent", "unknown"]
    finally:
        _pending.reset(token)


def test_gateway_error_preserves_safe_cause_without_private_message():
    from agent.protocol.tool_media import media_request_error
    classified = media_request_error({'code': 'upstream_stream_break',
        'message': 'private path and document content'})
    assert classified['error']['code'] == 'media_request_failed'
    assert classified['error']['upstream_code'] == 'upstream_stream_break'
    assert 'private' not in str(classified)
    assert 'budget_kind' not in classified['error']


def test_error_receipt_failure_is_redacted_and_not_replayed():
    from agent.protocol.tool_media import MediaDelivery, media_request_error
    calls = []
    states = []
    def fail(error):
        calls.append(error)
        raise ValueError('private error from checkpoint')
    delivery = MediaDelivery()
    delivery.refs = [ToolMediaReference(lambda c: None, on_delivery=states.append, on_error=fail)]
    delivery.start()
    error = media_request_error({'message': 'context_length_exceeded'}, 400)
    with pytest.raises(ToolMediaError, match='^media_receipt_failed$'):
        delivery.report_error(error)
    delivery.report_error(error)
    delivery.finish(True)
    assert states == ['sent', 'unknown']
    assert len(calls) == 1


@pytest.mark.parametrize("fail_save", [False, True])
def test_response_checkpoint_precedes_completion(monkeypatch, fail_save):
    states = []
    saved = []
    def save(response):
        saved.append(response)
        states.append("response_saved")
        if fail_save:
            raise RuntimeError("private-checkpoint-error")
    ref = ToolMediaReference(lambda c: ToolMediaContent("application/pdf", PDF), lambda c: True, states.append, save)
    response = {"id": "resp-7", "status": "completed", "output": [{"type": "message", "content": []}],
                "usage": {"input_tokens": 12}, "input": "never-save-input"}
    class Client:
        def responses(self, **kw):
            yield {"type": "response.completed", "response": response}
    bot = OpenAICompatibleBot()
    monkeypatch.setattr(bot, "_get_http_client", lambda: Client())
    token = _pending.set({"call-7": [ref]})
    try:
        chunks = list(bot._handle_responses_stream(payload(), "key", "http://localhost", None, "gpt-6-test"))
    finally:
        _pending.reset(token)
    assert states == ["sent", "response_saved", "unknown" if fail_save else "completed"]
    assert saved[0] == {k: response[k] for k in ("id", "status", "output", "usage")}
    assert "private-checkpoint-error" not in str(chunks)
    assert any(c.get("error") for c in chunks) == fail_save


@pytest.mark.parametrize("status,detail,expected", [(429, "private", "rate_limited"),
    (408, "private", "media_request_timeout"), (413, "private", "reading_budget_exceeded"),
    (400, "context_length_exceeded private", "reading_budget_exceeded"),
    (400, "private", "media_request_rejected")])
def test_media_http_status_timeout_and_safe_category(monkeypatch, status, detail, expected):
    from models.openai.openai_http_client import OpenAIHTTPError
    observed = []
    class Client:
        def responses(self, **kwargs):
            observed.append(kwargs["timeout"])
            raise OpenAIHTTPError(status, {}, detail)
    bot = OpenAICompatibleBot()
    monkeypatch.setattr(bot, "_get_http_client", lambda: Client())
    token = _pending.set({"call-7": [reference([])]})
    try:
        chunks = list(bot._handle_responses_stream(payload(), "key", "http://localhost", None, "gpt-6-test"))
    finally:
        _pending.reset(token)
    assert observed == [(90, 240)]
    assert chunks[0]["status_code"] == status and chunks[0]["error"]["code"] == expected
    assert "private" not in str(chunks)


@pytest.mark.parametrize("kind", ["image_url", "input_image", "image"])
def test_legacy_tool_images_keep_their_real_type(kind):
    url = "data:image/png;base64,aGVsbG8="
    block = {"type": kind, "image_url": {"url": url} if kind == "image_url" else url}
    if kind == "image":
        block = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="}}
    bot = OpenAICompatibleBot()
    messages = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-7",
                "content": [{"type": "text", "text": "image"}, block]}]}]
    messages.insert(0, {"role": "assistant", "content": [{"type": "tool_use", "id": "call-7", "name": "read", "input": {}}]})
    original = deepcopy(messages)
    inputs = messages_to_input(bot._convert_messages_to_openai_format(messages))
    inputs = [i for i in inputs if i.get("type") == "function_call_output"]
    assert inputs[0]["call_id"] == "call-7"
    assert inputs[0]["output"] == [{"type": "input_text", "text": "image"}, {"type": "input_image", "image_url": url}]
    assert messages == original


@pytest.mark.parametrize("terminal,expected", [("response.completed", "completed"),
    ("response.incomplete", "unknown"), ("response.failed", "unknown"), (None, "unknown")])
def test_stream_requires_explicit_completion(monkeypatch, terminal, expected):
    states = []
    bot = OpenAICompatibleBot()
    def events(**kwargs):
        if terminal:
            yield {"type": terminal, "response": {"status": terminal.split(".")[1]}}
    class Client:
        responses = staticmethod(events)
    monkeypatch.setattr(bot, "_get_http_client", lambda: Client())
    token = _pending.set({"call-7": [reference(states)]})
    try:
        list(bot._handle_responses_stream(payload(), "key", "http://localhost", None, "gpt-6-test"))
    finally:
        _pending.reset(token)
    assert states == ["sent", expected]
