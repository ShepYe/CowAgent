"""新渠道的站点和环境凭据纳入任务修订，旧渠道修订兼容。"""
from unittest.mock import Mock

import pytest

from common import const
from config import Config, config_scope
from models.config_revision import (
    ConfigUnavailable, _revision, capture_config_revision, require_config_revision, use_config_revision,
)
from models.cowagent_provider import resolve_cowagent_credentials


def test_unrelated_provider_retains_existing_revision(monkeypatch):
    monkeypatch.setenv('COWAGENT_API_KEY', 'unused-fixture-key')
    settings = Config({'bot_type': 'custom:p', 'model': 'fixture'})
    with config_scope(settings):
        assert capture_config_revision() == _revision(settings)
        assert '_cowagent_credentials_bound' not in settings


def test_running_task_pins_environment_and_skill_subprocess(monkeypatch, tmp_path):
    from agent.tools.bash.bash import Bash
    settings = Config({'bot_type': const.COWAGENT, 'model': 'fixture'})
    monkeypatch.setenv('COWAGENT_API_KEY', 'old-fixture-key')
    monkeypatch.setenv('COWAGENT_API_BASE', 'https://old.example.invalid/v1/')
    with config_scope(settings):
        revision = capture_config_revision()
        assert 'old-fixture-key' not in revision
        with use_config_revision(revision):
            monkeypatch.setenv('COWAGENT_API_KEY', 'new-fixture-key')
            monkeypatch.setenv('COWAGENT_API_BASE', 'https://new.example.invalid/v1')
            assert resolve_cowagent_credentials() == ('old-fixture-key', 'https://old.example.invalid/v1')
            assert capture_config_revision() == revision
            runner = Mock(return_value={'stdout': 'ok', 'stderr': '', 'returncode': 0})
            tool = Bash({'cwd': str(tmp_path)})
            monkeypatch.setattr(tool, '_run_streaming', runner)
            monkeypatch.setattr(tool, '_snapshot_files', lambda *_: {})
            tool.execute({'command': 'echo fixture'})
            env = runner.call_args.args[2]
            assert env['COWAGENT_API_KEY'] == 'old-fixture-key'
            assert env['COWAGENT_API_BASE'] == 'https://old.example.invalid/v1'
            with use_config_revision(revision):
                assert resolve_cowagent_credentials()[0] == 'old-fixture-key'
        with pytest.raises(ConfigUnavailable):
            require_config_revision(revision)
        assert settings == {'bot_type': const.COWAGENT, 'model': 'fixture'}


def test_language_default_is_bound_but_explicit_site_wins(monkeypatch):
    monkeypatch.delenv('COWAGENT_API_BASE', raising=False)
    language = {'value': 'zh'}
    monkeypatch.setattr('models.cowagent_provider.i18n.get_language', lambda: language['value'])
    with config_scope(Config({'bot_type': const.COWAGENT, 'cowagent_api_key': 'fixture'})):
        revision = capture_config_revision()
        with use_config_revision(revision):
            language['value'] = 'en'
            assert resolve_cowagent_credentials()[1] == const.COWAGENT_API_BASE_CN
        with pytest.raises(ConfigUnavailable):
            require_config_revision(revision)
    with config_scope(Config({'bot_type': const.COWAGENT, 'cowagent_api_key': 'fixture',
                              'cowagent_api_base': const.COWAGENT_API_BASE_CN})):
        revision = capture_config_revision()
        language['value'] = 'zh'
        require_config_revision(revision)
        assert resolve_cowagent_credentials()[1] == const.COWAGENT_API_BASE_CN


@pytest.mark.parametrize('provider', ['CowAgent', ' cowagent '])
def test_normalized_search_binding_pins_environment(monkeypatch, provider):
    from agent.tools.web_search.web_search import configured_providers
    monkeypatch.setenv('COWAGENT_API_KEY', 'old-fixture-key')
    monkeypatch.setenv('COWAGENT_API_BASE', 'https://old.example.invalid/v1')
    with config_scope(Config({'bot_type': 'openai', 'tools': {
        'web_search': {'strategy': 'fixed', 'provider': provider},
    }})):
        assert 'cowagent' in configured_providers()
        revision = capture_config_revision()
        with use_config_revision(revision):
            monkeypatch.setenv('COWAGENT_API_KEY', 'new-fixture-key')
            assert resolve_cowagent_credentials()[0] == 'old-fixture-key'
        with pytest.raises(ConfigUnavailable):
            require_config_revision(revision)


@pytest.mark.parametrize('api_type', ['auto', 'chat', 'responses'])
def test_cowagent_chat_does_not_change_custom_protocol(monkeypatch, api_type):
    from models.chatgpt.chat_gpt_bot import ChatGPTBot
    settings = Config({'model': 'gpt-6.1-sol', 'open_ai_api_type': 'responses',
                       'custom_providers': [{'id': 'p', 'api_type': api_type}]})
    with config_scope(settings):
        custom = ChatGPTBot.__new__(ChatGPTBot)
        custom._bot_type = 'custom:p'
        builtin = ChatGPTBot.__new__(ChatGPTBot)
        builtin._bot_type = const.COWAGENT
        assert custom._openai_api_type() == api_type
        assert builtin._openai_api_type() == 'chat'
