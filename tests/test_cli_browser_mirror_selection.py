"""镜像选择回归：所有安装调用均由桩替代。"""

import os
from unittest.mock import Mock

import pytest

from agent.tools.browser import browser_env
from cli import utils
from cli.commands import install


@pytest.mark.parametrize("name", ["NPM_CONFIG_REGISTRY", "npm_config_registry"])
def test_npm_mirror_detection_without_pip(monkeypatch, name):
    # A plain mapping also exercises case sensitivity on Windows.
    monkeypatch.setattr(install.os, "environ", {name: "https://registry.npmmirror.com"})
    pip = Mock(side_effect=AssertionError("must not invoke pip"))
    monkeypatch.setattr(install.subprocess, "check_output", pip)
    assert install._is_china_network() is True
    pip.assert_not_called()


@pytest.fixture
def offline_install(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "get_cli_language", lambda: "zh")
    monkeypatch.setattr(install, "_is_frozen", lambda: False)
    monkeypatch.setattr(install, "_get_glibc_version", lambda: None)
    monkeypatch.setattr(install, "_pip_install", lambda *a: 0)
    monkeypatch.setattr(install, "_get_installed_version", lambda: "1.52.0")
    monkeypatch.setattr(install, "_is_headless_linux", lambda: False)
    monkeypatch.setattr(install.sys, "platform", "win32")
    monkeypatch.setattr(install.subprocess, "call", lambda *a, **k: 0)
    monkeypatch.setattr(install.subprocess, "check_output", Mock(side_effect=AssertionError("unexpected subprocess")))
    monkeypatch.setattr(browser_env, "capability_summary", lambda: {"ready": False})
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
    monkeypatch.setattr(install, "_is_china_network", lambda: True)
    return str(tmp_path / "browsers")


@pytest.mark.parametrize("result", [0, 1])
def test_explicit_download_host_is_preserved_even_on_failure(monkeypatch, offline_install, result):
    host = "https://custom.example/playwright"
    monkeypatch.setenv("PLAYWRIGHT_DOWNLOAD_HOST", host)
    cli = Mock(return_value=result)
    monkeypatch.setattr(install, "_playwright_cli", cli)

    assert install.run_install_browser(stream=lambda *a: None) == result

    cli.assert_called_once()
    env = cli.call_args.kwargs["env"]
    assert env["PLAYWRIGHT_DOWNLOAD_HOST"] == host
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == offline_install
    assert os.environ["PLAYWRIGHT_DOWNLOAD_HOST"] == host


def test_automatic_mirror_failure_retries_official_cdn(monkeypatch, offline_install):
    monkeypatch.delenv("PLAYWRIGHT_DOWNLOAD_HOST", raising=False)
    cli = Mock(side_effect=[1, 0])
    monkeypatch.setattr(install, "_playwright_cli", cli)

    assert install.run_install_browser(stream=lambda *a: None) == 0

    assert cli.call_count == 2
    first, second = [call.kwargs["env"] for call in cli.call_args_list]
    assert first["PLAYWRIGHT_DOWNLOAD_HOST"] == install.CHINA_MIRROR
    assert "PLAYWRIGHT_DOWNLOAD_HOST" not in second
    assert first["PLAYWRIGHT_BROWSERS_PATH"] == second["PLAYWRIGHT_BROWSERS_PATH"] == offline_install
