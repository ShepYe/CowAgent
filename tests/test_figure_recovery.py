"""启动离线屏障：PID、版本、恢复点及两库预备。"""

import json
import os
from pathlib import Path
import sqlite3
import zipfile
import pytest
from common.figure_recovery import prepare_startup_recovery
from agent.knowledge.personal_assistant import SourceService


def instance(tmp_path):
    root = tmp_path / "data"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    (root / "config.json").write_text(
        json.dumps(dict(personal_assistant_enabled=True, agent_workspace=str(workspace))), encoding="utf-8"
    )
    SourceService(workspace)
    return root, workspace


def test_startup_own_pid_prepares_both_versions_and_restart_skips_backup(tmp_path):
    root, workspace = instance(tmp_path)
    (root / ".personal-backend.pid").write_text(str(os.getpid()), encoding="utf-8")
    result = prepare_startup_recovery(root, workspace)
    for path in (workspace / ".personal_sources/sources.sqlite3", root / "personal/tasks.sqlite3"):
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    assert prepare_startup_recovery(root, workspace) is None
    assert len(list((root / "recovery").glob("*.zip"))) == 1
    assert Path(result["archive"]).is_file()


def test_other_live_pid_blocks_before_any_migration(tmp_path, monkeypatch):
    root, workspace = instance(tmp_path)
    (root / ".personal-backend.pid").write_text(str(os.getpid() + 1), encoding="utf-8")
    monkeypatch.setattr("cli.commands.process._is_pid_alive", lambda pid: True)
    with pytest.raises(ValueError):
        prepare_startup_recovery(root, workspace)
    with sqlite3.connect(workspace / ".personal_sources/sources.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_unknown_task_version_blocks_before_backup(tmp_path):
    root, workspace = instance(tmp_path)
    (root / "personal").mkdir()
    with sqlite3.connect(root / "personal/tasks.sqlite3") as db:
        db.execute("PRAGMA user_version=4")
    with pytest.raises(ValueError):
        prepare_startup_recovery(root, workspace)
    assert not list(root.rglob("*.zip"))


def test_second_recovery_does_not_embed_previous_archives(tmp_path):
    from cli.commands.backup import create_backup_archive

    root, workspace = instance(tmp_path)
    first = prepare_startup_recovery(root, workspace)
    archive = root / "recovery/second.zip"
    create_backup_archive(archive, root, workspace, complete=True)
    with zipfile.ZipFile(archive) as saved:
        assert not any(n.endswith(".zip") for n in saved.namelist())
    assert Path(first["archive"]).is_file()


@pytest.mark.skipif(os.name != 'nt', reason='Windows venv 启动器专有行为')
def test_windows_venv_launcher_recognized_only_for_own_startup(tmp_path):
    from cli.commands.backup import _is_startup_launcher, _require_personal_stopped
    import sys
    if sys.prefix == sys.base_prefix:
        pytest.skip('需要 venv 启动器')
    assert _is_startup_launcher(os.getppid(), os.getpid())
    assert not _is_startup_launcher(os.getppid(), None)
    assert not _is_startup_launcher(os.getpid(), os.getpid())
    (tmp_path / '.personal-backend.pid').write_text(str(os.getppid()), encoding='utf-8')
    _require_personal_stopped(tmp_path, startup_pid=os.getpid())
    with pytest.raises(ValueError):
        _require_personal_stopped(tmp_path)
