"""旧版本恢复点必须早于记忆台账启用。"""
import json
from pathlib import Path
import sqlite3

import pytest

from common.memory_recovery import prepare_startup_recovery
from agent.memory.conversation_store import ConversationStore


def test_first_startup_preserves_old_complete_point_and_reuses_it(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (tmp_path / 'config.json').write_text(json.dumps(dict(agent_workspace=str(workspace), personal_instance=True)), encoding='utf-8')
    (workspace / 'MEMORY.md').write_text('既有记忆', encoding='utf-8')
    store = ConversationStore(workspace / 'memory/long-term/index.db')
    point = prepare_startup_recovery(tmp_path, workspace)
    assert point['backup_version'] == 3
    assert (tmp_path / 'recovery' / point['archive']).is_file()
    with sqlite3.connect(store._db_path) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_schema'").fetchone()
    assert prepare_startup_recovery(tmp_path, workspace) == point
    from agent.memory.ledger import MemoryLedger
    MemoryLedger(workspace, store)
    assert prepare_startup_recovery(tmp_path, workspace) is None


def test_damaged_recovery_point_prevents_enable(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    point = prepare_startup_recovery(tmp_path, workspace)
    (tmp_path / 'recovery' / point['archive']).write_bytes(b'broken')
    with pytest.raises(ValueError, match='恢复点缺失或损坏'):
        prepare_startup_recovery(tmp_path, workspace)


def test_unknown_ledger_version_stops_before_new_backup(tmp_path):
    from agent.memory.ledger import MemoryLedger
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    store = ConversationStore(workspace / 'memory/long-term/index.db')
    MemoryLedger(workspace, store)
    with sqlite3.connect(store._db_path) as db:
        db.execute('UPDATE memory_schema SET version=999')
    with pytest.raises(ValueError, match='不受支持'):
        prepare_startup_recovery(tmp_path, workspace)
    assert not (Path(tmp_path) / 'recovery').exists()


def test_desktop_runtime_lock_is_outside_authoritative_backup(tmp_path):
    import zipfile
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (tmp_path / 'electron').mkdir()
    (tmp_path / 'electron/lockfile').write_bytes(b'locked-runtime')
    (workspace / 'MEMORY.md').write_text('记忆原文', encoding='utf-8')
    point = prepare_startup_recovery(tmp_path, workspace)
    with zipfile.ZipFile(tmp_path / 'recovery' / point['archive']) as archive:
        assert not any('/electron/' in name for name in archive.namelist())
        assert 'workspace/MEMORY.md' in archive.namelist()
