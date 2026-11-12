"""新包镜像可供 MCP 安装使用，显式服务环境仍优先。"""
import pytest

from agent.tools.mcp.mcp_client import McpClient


@pytest.mark.parametrize('name', [
    'NPM_CONFIG_REGISTRY', 'npm_config_registry', 'PIP_INDEX_URL',
    'PIP_EXTRA_INDEX_URL', 'UV_INDEX_URL', 'UV_DEFAULT_INDEX',
])
def test_registry_environment_and_explicit_override(monkeypatch, name):
    monkeypatch.setenv(name, 'https://mirror.example.invalid/simple')
    client = McpClient({'name': 'fixture', 'type': 'stdio', 'command': 'fixture'})
    assert client._build_stdio_env({})[name] == 'https://mirror.example.invalid/simple'
    assert client._build_stdio_env({name: 'https://override.example.invalid'})[name] == 'https://override.example.invalid'


def test_new_passthrough_does_not_inherit_unrelated_credentials(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'unrelated-fixture-key')
    client = McpClient({'name': 'fixture', 'type': 'stdio', 'command': 'fixture'})
    assert 'OPENAI_API_KEY' not in client._build_stdio_env({})
    assert client._build_stdio_env({'OPENAI_API_KEY': 'explicit-fixture'})['OPENAI_API_KEY'] == 'explicit-fixture'
