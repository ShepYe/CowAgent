import shutil
import subprocess
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "docker/docker-compose.agent-mail.yml"
WRAPPER = ROOT / "docker/agent-mail/entrypoint.sh"

requires_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not available")


def test_overlay_keeps_agent_mail_data_out_of_the_cow_data_mount():
    """Credentials must not also surface inside the agent's data directory."""
    overlay = OVERLAY.read_text(encoding="utf-8")

    for mount in (
        "./agent-mail-data/config:/home/agent/.agently-cli",
        "./agent-mail-data/share:/home/agent/.local/share/agently-cli",
        "./agent-mail-data/npm-global:/home/agent/.npm-global",
    ):
        assert mount in overlay
    assert "cow-data" not in overlay
    assert "./agent-mail/entrypoint.sh:/docker-entrypoint-init.d/agent-mail-entrypoint.sh:ro" in overlay


def test_wrapper_never_blocks_cowagent_startup():
    """An install failure must fall through to the original entrypoint."""
    wrapper = WRAPPER.read_text(encoding="utf-8")
    code_lines = [line for line in wrapper.splitlines() if line.strip() and not line.lstrip().startswith("#")]

    assert not any(line.startswith("set ") and "e" in line.split()[1] for line in code_lines)
    assert code_lines[-1] == "exec /entrypoint.sh"
    assert "auth login" not in wrapper
    assert "skills add" not in wrapper


@requires_bash
def test_wrapper_is_valid_bash():
    subprocess.run(["bash", "-n"], input=WRAPPER.read_text(encoding="utf-8").encode("utf-8"),
                   check=True, timeout=10)


@requires_bash
@pytest.mark.parametrize(
    "installed, pinned, expected",
    [
        ("1.0.18", "1.0.18", True),
        ("1.0.20", "1.0.18", True),
        ("1.1.0", "1.0.18", True),
        ("1.0.9", "1.0.18", False),
        ("22.23.3", "22.23.3", True),
        ("20.19.0", "22.23.3", False),
        ("", "1.0.18", False),
    ],
)
def test_newer_versions_are_kept_and_older_ones_reinstalled(installed, pinned, expected):
    """A CLI upgraded by the user must not be downgraded on the next restart."""
    # Send the real function over stdin: Bash may be Git Bash or WSL and
    # cannot necessarily resolve the host Python's Windows paths.
    function = re.search(r"^version_at_least\(\) \{.*?^\}",
                         WRAPPER.read_text(encoding="utf-8"), re.M | re.S)
    assert function is not None
    script = function.group() + f"\nversion_at_least '{installed}' '{pinned}'\n"
    result = subprocess.run(["bash"], input=script.encode("utf-8"),
                            capture_output=True, timeout=10)

    assert result.returncode in (0, 1), result.stderr
    assert (result.returncode == 0) is expected


def test_guides_are_listed_in_the_extensions_navigation():
    docs_config = (ROOT / "docs/docs.json").read_text(encoding="utf-8")

    for page in ("extensions/agent-mail", "zh/extensions/agent-mail", "ja/extensions/agent-mail"):
        assert f'"{page}"' in docs_config
        assert (ROOT / "docs" / f"{page}.mdx").is_file()


def test_agent_mail_credentials_are_ignored_by_git():
    assert "docker/agent-mail-data/" in (ROOT / ".gitignore").read_text(encoding="utf-8")
