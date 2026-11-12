"""个人实例首次启用记忆台账前，保留可由旧版本恢复的完整离线快照。"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4


def prepare_startup_recovery(data_root=None, workspace=None):
    from config import conf, get_data_root
    from common.personal_tasks import personal_enabled
    from cli.commands.backup import create_backup_archive, _configured_workspaces, _read_config, _team
    if data_root is None and not personal_enabled():
        return None
    root = Path(data_root or get_data_root()).resolve()
    workspace = Path(workspace or conf().get('agent_workspace', '~/cow')).expanduser().resolve()
    settings = _read_config(root)
    if settings:
        settings = _team().resolve({**settings, 'agent_workspace': str(workspace)})
    profiles, _, _ = _configured_workspaces(settings, workspace)
    from agent.memory.ledger import _TABLES
    missing = False
    for profile in profiles:
        path = Path(profile.workspace).expanduser().resolve() / 'memory/long-term/index.db'
        if not path.exists():
            missing = True
            continue
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            present = tables.intersection(_TABLES)
            if not present:
                missing = True
                continue
            if present != set(_TABLES) or [row[0] for row in db.execute('SELECT version FROM memory_schema')] != [1]:
                raise ValueError('记忆台账版本或结构不受支持，停止启动并保留原数据')
    if not missing:
        return None
    marker = root / 'recovery/memory-ledger-v1.json'
    if marker.exists():
        point = json.loads(marker.read_text(encoding='utf-8'))
        archive = root / 'recovery' / point['archive']
        if (archive.resolve().parent != (root / 'recovery').resolve() or not archive.is_file()
                or hashlib.sha256(archive.read_bytes()).hexdigest() != point['sha256']):
            raise ValueError('记忆升级前恢复点缺失或损坏，停止启动')
        return point
    archive = root / 'recovery' / ('pre-memory-' + uuid4().hex + '.zip')
    # Electron 自身仍运行；其缓存/锁文件不是业务权威数据，不能阻止后端离线快照。
    result = create_backup_archive(archive, root, workspace, complete=True, _startup_pid=os.getpid(),
                                   excluded_paths={root / 'electron'})
    point = dict(schema_version=1, archive=archive.name,
                 sha256=hashlib.sha256(archive.read_bytes()).hexdigest(), backup_version=result['version'])
    # 完整快照已经落盘；marker失败只会让下次启动再建快照，不会启用旧恢复点。
    from common.atomic_write import write_text_atomic
    write_text_atomic(str(marker), json.dumps(point, ensure_ascii=False, indent=2), newline='')
    return point
