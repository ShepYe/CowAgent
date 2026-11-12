"""离线验证 CowAgent 聊天凭据不会隐式启用其他能力。"""

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from agent.memory.embedding.factory import create_default_embedding_provider
from agent.tools.vision.vision import Vision
from agent.tools.web_search import web_search
from bridge.bridge import Bridge
from config import Config, config_scope


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    def reject_network(*args, **kwargs):
        pytest.fail("离线测试不得发送真实请求")

    monkeypatch.setattr("requests.sessions.Session.request", reject_network)
    with patch.dict(os.environ, {}, clear=True):
        with config_scope(Config({
            "cowagent_api_key": "offline-cowagent-key",
            "cowagent_api_base": "https://cowagent.invalid/v1",
        })) as cfg:
            yield cfg


@pytest.fixture
def image_script():
    path = Path(__file__).parents[1] / "skills/image-generation/scripts/generate.py"
    spec = importlib.util.spec_from_file_location("explicit_image_routing", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chat_key_does_not_initialize_embedding(monkeypatch):
    create = Mock(side_effect=AssertionError("不得自动创建向量客户端"))
    monkeypatch.setattr("agent.memory.embedding.provider.create_embedding_provider", create)
    assert create_default_embedding_provider() is None
    create.assert_not_called()


def test_explicit_embedding_is_preserved(isolated_config, monkeypatch):
    isolated_config["embedding_provider"] = "cowagent"
    create = Mock(return_value=SimpleNamespace(dimensions=1536))
    monkeypatch.setattr("agent.memory.embedding.provider.create_embedding_provider", create)
    assert create_default_embedding_provider() is create.return_value
    assert create.call_args.kwargs["provider"] == "cowagent"
    assert create.call_args.kwargs["api_base"] == "https://cowagent.invalid/v1"
    isolated_config["embedding_enabled"] = False
    create.reset_mock()
    assert create_default_embedding_provider() is None
    create.assert_not_called()


@pytest.mark.parametrize("use_linkai", [False, True])
def test_vision_auto_chain_excludes_cowagent(isolated_config, use_linkai):
    isolated_config.update({"use_linkai": use_linkai, "linkai_api_key": "offline-linkai"})
    assert "CowAgent" not in [p.name for p in Vision()._resolve_providers()]


def test_explicit_vision_keeps_selected_model(isolated_config):
    isolated_config["tools"] = {"vision": {"provider": "cowagent", "model": "chosen-vision"}}
    providers = Vision()._resolve_providers()
    assert len(providers) == 1
    assert providers[0].name == "CowAgent"
    assert providers[0].model_override == "chosen-vision"
    assert providers[0].api_key == "offline-cowagent-key"


def test_asr_auto_excludes_chat_key_but_explicit_selection_survives(isolated_config, monkeypatch):
    bridge = Bridge.new_instance()
    assert bridge.btype["voice_to_text"] == "openai"
    isolated_config["voice_to_text"] = "cowagent"
    bridge.refresh_voice()
    create = Mock(return_value=object())
    monkeypatch.setattr("bridge.bridge.create_voice", create)
    assert bridge.get_bot("voice_to_text") is create.return_value
    create.assert_called_once_with("cowagent")


def test_search_chat_key_does_not_enable_tool_or_auto_candidate(isolated_config):
    assert not web_search.WebSearch.is_available()
    assert web_search.WebSearch()._resolve_provider(None) is None
    isolated_config["deepseek_api_key"] = "offline-deepseek"
    assert web_search.configured_providers() == ["deepseek"]
    assert web_search.WebSearch()._resolve_provider(None) == "deepseek"


@pytest.mark.parametrize("binding", ["fixed", "requested"])
def test_explicit_search_dispatches_to_cowagent(isolated_config, monkeypatch, binding):
    isolated_config["tools"] = {"web_search": {"strategy": "fixed", "provider": "cowagent"}}
    assert web_search.WebSearch.is_available()
    post = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"results": []}))
    monkeypatch.setattr(web_search.requests, "post", post)
    args = {"query": "offline query"}
    if binding == "requested":
        args["provider"] = "cowagent"
    result = web_search.WebSearch().execute(args)
    assert result.status == "success"
    assert result.result["backend"] == "cowagent"
    assert post.call_args.args == ("https://cowagent.invalid/v1/search",)
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer offline-cowagent-key"


@pytest.mark.parametrize("other_search", [False, True])
def test_search_request_cannot_enable_unbound_cowagent(isolated_config, monkeypatch, other_search):
    if other_search:
        isolated_config["deepseek_api_key"] = "offline-deepseek"
        assert web_search.WebSearch.is_available()
    post, get = Mock(), Mock()
    monkeypatch.setattr(web_search.requests, "post", post)
    monkeypatch.setattr(web_search.requests, "get", get)
    assert "cowagent" not in web_search.configured_providers()
    result = web_search.WebSearch().execute({"query": "offline query", "provider": "cowagent"})
    assert result.status == "error"
    post.assert_not_called()
    get.assert_not_called()


def test_chat_model_does_not_implicitly_enable_cowagent_vision():
    tool = Vision()
    tool.model = SimpleNamespace(
        model="gpt-6-luna",
        bot=SimpleNamespace(_bot_type="cowagent", call_vision=Mock(), supports_vision=True),
    )
    assert tool._resolve_providers() == []


def test_disabled_search_cannot_be_reenabled_by_request(isolated_config):
    isolated_config["tools"] = {"web_search": {
        "strategy": "fixed", "provider": "cowagent", "disabled_providers": ["cowagent"],
    }}
    assert web_search.WebSearch()._resolve_provider("cowagent") is None


@pytest.mark.parametrize("provider", ["openai", "gemini", "doubao", "dashscope", "minimax", "linkai", "unknown"])
def test_missing_explicit_image_key_never_borrows_cowagent(image_script, monkeypatch, provider):
    monkeypatch.setenv("COWAGENT_API_KEY", "offline-cowagent-key")
    assert image_script._build_providers("gpt-image-2.5-flare", provider) == []


def test_missing_explicit_cowagent_image_key_never_borrows_other_provider(image_script, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-openai-key")
    assert image_script._build_providers("gpt-image-2.5-flare", "cowagent") == []


@pytest.mark.parametrize("model", ["", "gpt-image-2.5-flare", "gemini-3.1-flash-image", "seedream-5.0-lite"])
def test_unbound_images_never_use_cowagent_chat_key(image_script, monkeypatch, model):
    monkeypatch.setenv("COWAGENT_API_KEY", "offline-cowagent-key")
    assert image_script._build_providers(model) == []


@pytest.mark.parametrize("model", ["gpt-image-2.5-flare", "gemini-3.1-flash-image", "seedream-5.0-lite"])
def test_explicit_cowagent_images_preserve_model(image_script, monkeypatch, model):
    monkeypatch.setenv("COWAGENT_API_KEY", "offline-cowagent-key")
    monkeypatch.setenv("COWAGENT_API_BASE", "https://cowagent.invalid/v1")
    providers = image_script._build_providers(model, "cowagent")
    assert len(providers) == 1
    assert providers[0][0] == "CowAgent"
    assert providers[0][1].model == model
    assert providers[0][1].api_base == "https://cowagent.invalid/v1"


def test_custom_image_provider_remains_independent(image_script, monkeypatch):
    monkeypatch.setenv("COWAGENT_API_KEY", "offline-cowagent-key")
    monkeypatch.setenv("SKILL_IMAGE_GENERATION_CUSTOM_PROVIDER", json.dumps({
        "id": "image", "name": "custom-image", "api_key": "offline-custom-key",
        "api_base": "https://custom.invalid/v1", "model": "custom-model",
    }))
    providers = image_script._build_providers("", "custom:image")
    assert len(providers) == 1
    assert providers[0][1].api_key == "offline-custom-key"
    assert providers[0][1].model == "custom-model"
    assert image_script._build_providers("custom-model", "custom:missing") == []
