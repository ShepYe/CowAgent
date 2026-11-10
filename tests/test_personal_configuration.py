"""P-04 local probes: no live credentials or provider network calls."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import config
from config import Config
from models.config_revision import (
    ConfigUnavailable, capture_config_revision, require_config_revision,
    use_config_revision,
)
from channel.web.api.models import ModelsHandler


@pytest.fixture
def cfg(monkeypatch):
    value = Config({"bot_type": "custom:p", "model": "m", "embedding_enabled": False,
                    "custom_providers": [{"id": "p", "name": "probe", "api_key": "fake-a",
                                          "api_base": "https://a.invalid/v1"}]})
    monkeypatch.setattr(config, "config", value)
    return value


@pytest.mark.parametrize("field,new", [("api_key", "fake-b"), ("api_base", "https://b.invalid/v1"),
                                       ("model", "new-model")])
def test_queue_restart_and_retry_never_switch_revision(cfg, field, new):
    revision = capture_config_revision()
    persisted_task = json.loads(json.dumps({"config_revision": revision}))
    assert "fake-a" not in json.dumps(persisted_task)
    require_config_revision(revision)
    if field == "model":
        cfg[field] = new
    else:
        cfg["custom_providers"][0][field] = new
    for _phase in ("queued", "restart", "retry"):
        with pytest.raises(ConfigUnavailable) as err:
            require_config_revision(persisted_task["config_revision"])
        assert err.value.code == "config_unavailable"
        assert "fake-" not in str(err.value)
    require_config_revision(capture_config_revision())


def test_running_attempt_reads_original_config_while_next_uses_new(cfg):
    revision = capture_config_revision()
    with use_config_revision(revision):
        cfg["custom_providers"][0]["api_base"] = "https://b.invalid/v1"
        assert config.conf()["custom_providers"][0]["api_base"] == "https://a.invalid/v1"
        with ThreadPoolExecutor(max_workers=1) as pool:
            current = pool.submit(capture_config_revision).result()
        assert current != revision
    with pytest.raises(ConfigUnavailable):
        require_config_revision(revision)


@pytest.mark.parametrize("provider", ["", "openai", "linkai", "custom:p"])
def test_keyword_mode_creates_no_provider(cfg, monkeypatch, provider):
    from agent.memory.embedding.factory import create_default_embedding_provider
    cfg.update(embedding_provider=provider, open_ai_api_key="fake-key", linkai_api_key="fake-key")
    build = Mock(side_effect=AssertionError("unexpected embedding client"))
    monkeypatch.setattr("agent.memory.embedding.provider.create_embedding_provider", build)
    assert create_default_embedding_provider() is None
    build.assert_not_called()


def test_legacy_absent_enabled_keeps_auto_selection(cfg, monkeypatch):
    from agent.memory.embedding.factory import create_default_embedding_provider
    del cfg["embedding_enabled"]
    cfg["open_ai_api_key"] = "fake-key"
    build = Mock(return_value=Mock(dimensions=3))
    monkeypatch.setattr("agent.memory.embedding.provider.create_embedding_provider", build)
    assert create_default_embedding_provider() is build.return_value
    assert build.call_args.kwargs["api_key"] == "fake-key"


@pytest.fixture
def handler(cfg):
    obj = ModelsHandler()
    obj._read_file_config = lambda: copy.deepcopy(dict(cfg))
    obj._write_file_config = Mock()
    obj._reset_bridge = Mock()
    return obj


def test_failed_disk_save_leaves_config_unchanged(cfg, handler):
    before = copy.deepcopy(dict(cfg))
    handler._write_file_config.side_effect = OSError("disk full")
    with pytest.raises(OSError):
        handler._handle_set_custom_provider({"id": "p", "name": "probe", "api_key": "fake-b"})
    assert dict(cfg) == before


def test_masks_and_base_only_update_keep_original_key(cfg, handler):
    result = json.loads(handler._handle_set_custom_provider(
        {"id": "p", "name": "probe", "api_base": "https://b.invalid/v1", "api_key": "fa***a"}))
    assert result["status"] == "success"
    assert cfg["custom_providers"][0]["api_key"] == "fake-a"
    handler._reset_bridge.assert_called_once()


@pytest.mark.parametrize("path", ["embedding_provider", "voice_to_text", "text_to_voice"])
def test_referenced_custom_provider_delete_rejected(cfg, handler, path):
    cfg["bot_type"] = "openai"
    cfg[path] = "custom:p"
    result = json.loads(handler._handle_delete_custom_provider({"id": "p"}))
    assert result["code"] == "provider_in_use"
    assert path in result["references"]
    handler._write_file_config.assert_not_called()


def test_fresh_bridge_isolated_from_global_cache(cfg):
    from bridge.bridge import Bridge, bridge_for_config_revision
    revision = capture_config_revision()
    with bridge_for_config_revision(revision) as first:
        first.bots["chat"] = object()
        with bridge_for_config_revision(revision) as second:
            assert first is not second
            assert second.bots == {}
            assert second is not Bridge()


def test_personal_chat_rejects_queued_revision_before_generate(cfg):
    from bridge.context import Context, ContextType
    from channel.chat_channel import ChatChannel
    cfg["personal_assistant_enabled"] = True
    ctx = Context(ContextType.TEXT, "probe", {"session_id": "probe"})
    ctx["config_revision"] = capture_config_revision()
    cfg["model"] = "new"
    ch = ChatChannel.__new__(ChatChannel)
    ch._generate_reply = Mock(side_effect=AssertionError("must reject before model"))
    ch._send_reply = Mock()
    ch._handle(ctx)
    ch._generate_reply.assert_not_called()
    assert "config_unavailable" in ch._send_reply.call_args.args[1].content


def test_channel_uses_fresh_bridge_inside_attempt(cfg):
    from bridge.bridge import bridge_for_config_revision
    from bridge.context import Context, ContextType
    from channel.channel import Channel
    with bridge_for_config_revision(capture_config_revision()) as scoped:
        scoped.fetch_agent_reply = Mock(return_value="scoped result")
        assert Channel().build_reply_content("probe", Context(ContextType.TEXT, "probe")) == "scoped result"
        scoped.fetch_agent_reply.assert_called_once()


def test_personal_identity_is_owner(cfg):
    from bridge.context import Context, ContextType
    from channel.chat_channel import ChatChannel
    cfg["personal_assistant_enabled"] = True
    ctx = Context(ContextType.TEXT, "probe", {"session_id": "s"})
    assert ChatChannel.__new__(ChatChannel)._identity_for(ctx).user_id == "local-owner"
