"""接收任务前建立图片契约升级的完整离线恢复点。"""

import os
from pathlib import Path
import sqlite3
from contextlib import closing

# 仅当前进程启动阶段填充，运行时不接受模型路径或磁盘标记授权。
_points = {}


def prepare_startup_recovery(data_root=None, workspace=None):
    from config import conf, get_data_root
    from common.personal_tasks import personal_enabled
    from cli.commands.backup import create_backup_archive, _configured_workspaces
    from uuid import uuid4
    from agent.knowledge.personal_assistant import _hash

    explicit = data_root is not None
    if not explicit and not personal_enabled():
        return None
    data_root = Path(data_root or get_data_root()).resolve()
    workspace = Path(workspace or conf().get("agent_workspace", "~/cow")).expanduser().resolve()
    from cli.commands.backup import _read_config, _team

    settings = _read_config(data_root)
    if settings:
        settings = _team().resolve({**settings, "agent_workspace": str(workspace)})
    profiles, _, _ = _configured_workspaces(settings, workspace)
    roots = [Path(p.workspace).expanduser().resolve() for p in profiles]
    # 只检查版本，不创建或迁移旧来源库。
    old = []
    for root in roots:
        path = root / ".personal_sources/sources.sqlite3"
        if not path.exists():
            old.append(root)
            continue
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (1, 2, 3):
            raise ValueError("未知来源库版本，停止启动")
        if version < 3:
            old.append(root)
    # 任务库尚未升级时也需要相同的恢复点。
    task_path = data_root / "personal/tasks.sqlite3"
    task_old = not task_path.exists()
    if task_path.exists():
        with closing(sqlite3.connect(task_path.as_uri() + "?mode=ro", uri=True)) as db:
            task_version = db.execute("PRAGMA user_version").fetchone()[0]
            if task_version not in (1, 2, 3):
                raise ValueError("unknown task database version")
            task_old = task_version < 3
    if not old and not task_old:
        return None
    archive = data_root / "recovery" / ("pre-figures-" + uuid4().hex + ".zip")
    # 此入口位于 app.run 的 PID/渠道/worker 初始化之前；只允许启动者自己的
    # PID（桌面父进程可能已登记），任何其他存活实例仍阻止快照。
    result = create_backup_archive(archive, data_root, workspace, complete=True, _startup_pid=os.getpid())
    point = dict(archive=str(archive), sha256=_hash(archive.read_bytes()), result=result)
    for root in roots:
        _points[str(root)] = point
    # 此次部署启用图片契约：在接收任何新原件/任务之前预备两库。
    # 各库独立提交；任一失败向上传播，app 不启动渠道，原恢复点保留。
    from agent.knowledge.personal_assistant import SourceService
    from agent.knowledge.figures import ensure_schema
    from common.personal_tasks import TaskStore

    for root in roots:
        service = SourceService(root)
        with service.transaction() as db:
            ensure_schema(db, service)
    store = TaskStore(data_root / "personal")
    with store._transaction() as db:
        db.execute("PRAGMA user_version=3")
    return result


def startup_recovery(workspace):
    from agent.knowledge.personal_assistant import _hash

    point = _points.get(str(Path(workspace).resolve()))
    if point is None:
        return None
    archive = Path(point["archive"])
    if _hash(archive.read_bytes()) != point["sha256"]:
        raise ValueError("升级恢复点缺失或哈希不符")
    return point["result"]
