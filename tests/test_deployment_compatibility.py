"""部署标识兼容旧桌面启动，托管模式必须显式选择。"""
import pytest

from common import deployment


@pytest.mark.parametrize('explicit,desktop,docker,expected', [
    ('', '', '', 'source'),
    ('', '1', '', 'desktop'),
    ('', '', '1', 'docker'),
    ('invalid', '1', '', 'desktop'),
    ('desktop', '', '', 'desktop'),
    (' MANAGED ', '1', '', 'managed'),
    ('source', '1', '1', 'source'),
])
def test_deployment_selection(monkeypatch, explicit, desktop, docker, expected):
    monkeypatch.setenv('COW_DEPLOYMENT', explicit)
    monkeypatch.setenv('COW_DESKTOP', desktop)
    monkeypatch.setenv('COW_DOCKER', docker)
    assert deployment.deployment() == expected
    assert deployment.is_desktop() == (expected == 'desktop')
    assert deployment.is_managed() == (expected == 'managed')


@pytest.mark.parametrize('value,expected', [
    (' https://manage.example.invalid/site ', 'https://manage.example.invalid/site'),
    ('javascript:fixture', ''),
    ('', ''),
])
def test_management_link(monkeypatch, value, expected):
    monkeypatch.setenv('COW_MANAGE_URL', value)
    assert deployment.manage_url() == expected
