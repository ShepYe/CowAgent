"""桌面站点原值及自动能力预测兼容；只读替身，不调用网络模型。"""
import json
import socket

import pytest

from channel.web.api import models
from config import Config


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('禁止出网或写入配置')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(models.ModelsHandler, '_write_file_config', forbidden)
    monkeypatch.setattr(models.ModelsHandler, '_custom_provider_cards', lambda *args: [])
    monkeypatch.setattr(models.model_catalog, 'get_catalog_map', lambda: {})
    monkeypatch.setattr(models.model_catalog, 'get_hidden_map', lambda: {})
    for key in ('COWAGENT_API_KEY', 'LINKAI_API_KEY', 'DEEPSEEK_API_KEY', 'ZHIPU_API_KEY',
                'QIANFAN_API_KEY', 'BOCHA_API_KEY', 'ANYSEARCH_API_KEY', 'KEENABLE_API_KEY',
                'TAVILY_API_KEY', 'SERPLY_API_KEY', 'OUTCRAWL_API_KEY', 'FIRECRAWL_API_KEY',
                'SPECSTREAM_API_KEY', 'SEARXNG_URL'):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize('raw,key', [('', ''), ('https://api.cowagent.cn/v1', ''),
                                    ('https://api.cowagent.ai/v1', 'sk-private-fixture')])
def test_overview_distinguishes_configured_base_and_effective_default(monkeypatch, raw, key):
    config = Config({'cowagent_api_base': raw, 'cowagent_api_key': key})
    effective = raw or 'https://api.cowagent.ai/v1'
    monkeypatch.setattr(models, 'conf', lambda: config)
    monkeypatch.setattr(models, 'resolve_cowagent_credentials', lambda: (key, effective))
    p = next(p for p in models.ModelsHandler._provider_overview() if p['id'] == 'cowagent')
    assert p['configured_api_base'] == raw
    assert p['api_base'] == effective
    assert p['configured'] == bool(key)
    assert not key or key not in json.dumps(p)


def capabilities(monkeypatch, **extra):
    config = Config({'cowagent_api_key': 'sk-cowagent-fixture', 'bot_type': 'cowagent',
                     'model': 'gpt-5.5', **extra})
    monkeypatch.setattr(models, 'conf', lambda: config)
    return models.ModelsHandler._capabilities(config)


def test_chat_key_does_not_predict_cowagent_for_unbound_capabilities(monkeypatch):
    caps = capabilities(monkeypatch)
    for name in ('vision', 'image'):
        assert caps[name]['fallback_provider'] == ''
        assert 'cowagent' in caps[name]['providers']
    for name in ('asr', 'embedding'):
        assert caps[name]['suggested_provider'] == ''
        assert 'cowagent' in caps[name]['providers']
    assert caps['search']['current_provider'] == ''
    assert any(p['id'] == 'cowagent' and p['configured'] for p in caps['search']['providers'])


def test_explicit_cowagent_bindings_and_selectable_catalog_are_retained(monkeypatch):
    caps = capabilities(monkeypatch, voice_to_text='cowagent', embedding_provider='cowagent',
        tools={'vision': {'provider': 'cowagent', 'model': 'gpt-5.5'},
               'web_search': {'strategy': 'fixed', 'provider': 'cowagent'}},
        skills={'image-generation': {'provider': 'cowagent', 'model': 'gpt-image-2.5-flare'}})
    for name in ('vision', 'image', 'asr', 'embedding', 'search'):
        assert caps[name]['current_provider'] == 'cowagent'


def test_existing_linkai_auto_order_still_wins_without_cowagent_insertion(monkeypatch):
    caps = capabilities(monkeypatch, linkai_api_key='sk-linkai-fixture')
    for name in ('vision', 'image'):
        assert caps[name]['fallback_provider'] == 'linkai'
    for name in ('asr', 'embedding'):
        assert caps[name]['suggested_provider'] == 'linkai'
    assert caps['search']['current_provider'] == 'linkai'


def test_existing_openai_priority_is_unchanged(monkeypatch):
    caps = capabilities(monkeypatch, open_ai_api_key='sk-openai-fixture', linkai_api_key='sk-linkai-fixture')
    for name in ('vision', 'image'):
        assert caps[name]['fallback_provider'] == 'openai'
    for name in ('asr', 'embedding'):
        assert caps[name]['suggested_provider'] == 'openai'
