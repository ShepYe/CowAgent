"""Saved GPT-6 effort must reach the actual Responses HTTP request."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bridge.agent_bridge import AgentLLMModel
from config import conf
from common.runtime_identity import identity_scope
from models.openai_compatible_bot import OpenAICompatibleBot


@pytest.mark.parametrize("provider", ["chatGPT", "custom:relay"])
@pytest.mark.parametrize("channel", ["weixin", "feishu"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("thinking", [False, True])
@pytest.mark.parametrize("model_name,api_type", [
    ("gpt-6.1-sol", "auto"), ("gpt-6.2-sol", "auto"),
    ("gpt-7-sol", "auto"), ("gateway-next-model", "responses"),
])
def test_saved_effort_reaches_responses_request(monkeypatch, provider, channel, stream, thinking, model_name, api_type):
    config = conf()
    for key, value in {
        "bot_type": provider,
        "model": model_name,
        "use_linkai": False,
        "enable_thinking": thinking,
        "reasoning_effort": "high",
        "reasoning_effort_by_model": {f"{provider}:{model_name}": "xhigh"},
        "open_ai_api_type": api_type,
        "open_ai_api_key": "test-key",
        "open_ai_api_base": "https://relay.invalid/v1",
        "custom_providers": [{
            "id": "relay", "model": model_name,
            "api_key": "test-key", "api_base": "https://relay.invalid/v1",
            "api_type": api_type,
        }],
    }.items():
        monkeypatch.setitem(config, key, value)

    completed = {
        "id": "resp-test", "status": "completed", "model": model_name,
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
        "usage": {"input_tokens": 20, "output_tokens": 2, "total_tokens": 22},
    }
    wire = (
        "data: " + json.dumps({"type": "response.output_text.delta", "delta": "ok"})
        + "\n\ndata: " + json.dumps({"type": "response.completed", "response": completed})
        + "\n\ndata: [DONE]\n\n"
    ).encode()
    response = Mock(status_code=200)
    response.json.return_value = completed
    response.iter_lines.return_value = iter(wire.splitlines())
    response.iter_content.return_value = iter([wire])
    post = Mock(return_value=response)
    monkeypatch.setattr("models.openai.openai_http_client.requests.post", post)

    model = AgentLLMModel(None)
    model.channel_type = channel
    model.session_id = "private-conversation-123"
    request = SimpleNamespace(
        messages=[{"role": "user", "content": "hi"}],
        tools=[], max_tokens=None, system=None,
    )
    if stream:
        chunks = list(model.call_stream(request))
        assert not any(chunk.get("error") for chunk in chunks)
        assert any(chunk["choices"][0]["delta"].get("content") == "ok" for chunk in chunks)
    else:
        result = model.call(request)
        assert result["choices"][0]["message"]["content"] == "ok"

    assert post.call_count == 1
    assert post.call_args.args[0] == "https://relay.invalid/v1/responses"
    if thinking or model_name == "gpt-6.1-sol":
        assert post.call_args.kwargs["json"].get("reasoning") == {"effort": "xhigh"}
    else:
        # Future models' disable semantics are unknown: omit effort and let the
        # upstream apply its default rather than forcing an invented "none".
        assert "reasoning" not in post.call_args.kwargs["json"]
    cache_key = post.call_args.kwargs["json"].get("prompt_cache_key")
    assert cache_key and "private-conversation-123" not in cache_key


def test_cache_key_stays_stable_when_run_changes():
    api = {"api_base": "https://relay.invalid/v1"}
    make_key = OpenAICompatibleBot._responses_prompt_cache_key
    with identity_scope(agent_id="agent-a", session_id="session-a", run_id="run-1"):
        first = make_key("gpt-6.1-sol", api, {})
    with identity_scope(agent_id="agent-a", session_id="session-a", run_id="run-2"):
        assert make_key("gpt-6.1-sol", api, {}) == first
        assert make_key("gpt-6.1-sol", api, {"session_id": "session-b"}) != first
        assert make_key("gpt-6-astra", api, {}) != first
        assert make_key("gpt-6.1-sol", {"api_base": "https://other.invalid/v1"}, {}) != first
    with identity_scope(agent_id="agent-b", session_id="session-a"):
        assert make_key("gpt-6.1-sol", api, {}) != first
    with identity_scope(agent_id="agent-a", session_id="session-a", user_id="other-user"):
        assert make_key("gpt-6.1-sol", api, {}) != first


def test_cache_key_omitted_without_session():
    make_key = OpenAICompatibleBot._responses_prompt_cache_key
    with identity_scope(session_id=None):
        assert make_key("gpt-6.1-sol", {}, {}) is None


def test_responses_cache_routing_can_be_disabled_for_gateway():
    from config import Config, config_scope
    with config_scope(Config({'responses_prompt_cache_enabled': False})), identity_scope(session_id='gateway-session'):
        assert OpenAICompatibleBot._responses_prompt_cache_key('gpt-6.1-sol', {}, {}) is None


@pytest.mark.parametrize("model", ["gpt-5.4", "gpt-6.2-sol", "gpt-7-sol", "gateway-next-model"])
def test_responses_cache_key_does_not_require_known_model(model):
    assert OpenAICompatibleBot._responses_prompt_cache_key(model, {}, {"session_id": "session-a"})
