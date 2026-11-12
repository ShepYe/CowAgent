"""Portable local backup and restore commands for CowAgent user data."""

import errno
import hashlib
import sqlite3
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable, Optional, Set

import click

from cli.config_json import merge_duplicate_keys
from cli.utils import _ensure_project_on_path, get_project_root


def _team():
    """The team file module.

    Imported on use, not at module load: the CLI is installed as its own
    package and only puts the project root on ``sys.path`` when it needs
    something from it.
    """
    _ensure_project_on_path()
    from agent import team

    return team


BACKUP_FORMAT = "cowagent-backup"
# v2 only describes the multi-Agent layout. A single-workspace archive is
# structurally identical to what v1 produced, so it keeps declaring v1 and
# stays restorable by older CowAgent versions.
BACKUP_VERSION = 2
_SINGLE_WORKSPACE_VERSION = 1
_SUPPORTED_BACKUP_VERSIONS = {_SINGLE_WORKSPACE_VERSION, BACKUP_VERSION, 3}
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SKIP_DIRS = {".git", "__pycache__", "tmp"}
_SKIP_FILES = {".DS_Store", ".personal-backend.pid", ".cow.pid"}


def _data_root() -> Path:
    configured = os.environ.get("COW_DATA_DIR")
    return Path(configured).expanduser().resolve() if configured else Path(get_project_root()).resolve()


def _read_config(data_root: Path) -> dict:
    path = data_root / "config.json"
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            value = json.load(handle, object_pairs_hook=merge_duplicate_keys)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _workspace_from_config(config: dict) -> Path:
    return Path(config.get("agent_workspace") or (_data_root() / "workspace" if os.environ.get("COW_PERSONAL") == "1" else "~/cow")).expanduser().resolve()


def _configured_workspaces(config: dict, fallback: Path):
    """Return archive profiles and whether config uses the explicit registry."""
    if config.get("agents"):
        from agent.registry import AgentRegistry

        registry = AgentRegistry.from_config(config)
        return registry.list(), True, registry.default_agent_id
    from agent.registry import AgentProfile

    return [
        AgentProfile("default", "Default", str(Path(fallback).resolve()))
    ], False, "default"


def _legacy_user_data_path(data_root: Path, config: dict) -> Path:
    appdata_dir = config.get("appdata_dir") or ""
    return (data_root / appdata_dir / "user_datas.pkl").resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([str(path.resolve()), str(root.resolve())]) == str(root.resolve())
    except (OSError, ValueError):
        return False


def _iter_workspace_files(
    workspace: Path,
    excluded: Set[Path],
    pruned_dirs: Optional[Set[Path]] = None,
    complete: bool = False,
):
    pruned = {Path(path).resolve() for path in (pruned_dirs or ())}
    if not workspace.is_dir():
        return
    for current, dirnames, filenames in os.walk(str(workspace), followlinks=False):
        current_path = Path(current)
        dirnames[:] = [
            name for name in dirnames
            if (name not in _SKIP_DIRS or (complete and name == "tmp"))
            and not (current_path / name).is_symlink()
            and (current_path / name).resolve() not in pruned
        ]
        for name in filenames:
            path = current_path / name
            if name in _SKIP_FILES or name.endswith((".pyc", ".pyo")):
                continue
            if path.is_symlink() or path.resolve() in excluded:
                continue
            if path.is_file():
                yield path


def _require_personal_stopped(data_root: Path) -> None:
    """完整实例备份仅允许离线运行；逐库 backup API 不是跨库事务。"""
    from cli.commands.process import _is_pid_alive

    for name in (".personal-backend.pid", ".cow.pid"):
        path = data_root / name
        if not path.exists():
            continue
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError) as exc:
            raise ValueError("个人实例 PID 记录不可核验，请先退出后端再备份/恢复") from exc
        if pid > 0 and _is_pid_alive(pid):
            raise ValueError(f"个人实例后端仍在运行（PID {pid}），请先退出桌面后端再执行完整备份/恢复；不支持跨数据库在线快照")


def create_backup_archive(
    output: Path,
    data_root: Path,
    workspace: Path,
    excluded_paths: Optional[Iterable[Path]] = None,
    complete: Optional[bool] = None,
) -> dict:
    """Create a portable archive containing config and every agent workspace."""
    output = Path(output).expanduser().resolve()
    data_root = Path(data_root).expanduser().resolve()
    workspace = Path(workspace).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    excluded = {Path(path).expanduser().resolve() for path in (excluded_paths or [])}
    excluded.add(output)

    config_path = data_root / "config.json"
    config = _read_config(data_root)
    if config:
        # The roster lives beside the workspaces, so read it from the tree
        # being archived rather than from wherever config.json happens to point.
        config = _team().resolve({**config, "agent_workspace": str(workspace)})
    if complete is None:
        complete = config.get("personal_instance") is True or config.get("personal_assistant_enabled") is True
    if complete:
        _require_personal_stopped(data_root)
    legacy_path = _legacy_user_data_path(data_root, config)
    profiles, explicit_registry, default_agent_id = _configured_workspaces(config, workspace)
    sources = {
        profile.id: Path(profile.workspace).expanduser().resolve()
        for profile in profiles
    }
    workspace_entries = []
    total_files = 0
    total_bytes = 0
    for profile in profiles:
        source = sources[profile.id]
        # The registry only rejects workspaces that are exactly equal, and the
        # default layout nests on purpose: the default Agent owns the instance
        # root while every other Agent lives in `<root>/agents/<id>`. Without
        # pruning, walking the default Agent packs every other Agent a second
        # time under its archive root.
        nested = {
            other
            for other_id, other in sources.items()
            if other_id != profile.id and other != source and _is_within(other, source)
        }
        files = list(_iter_workspace_files(source, excluded, nested, complete=complete))
        size = sum(path.stat().st_size for path in files)
        archive_root = (
            f"agents/{profile.id}/workspace"
            if explicit_registry
            else "workspace"
        )
        workspace_entries.append((profile, source, archive_root, files, size))
        total_files += len(files)
        total_bytes += size

    manifest = {
        "format": BACKUP_FORMAT,
        "version": 3 if complete else (BACKUP_VERSION if explicit_registry else _SINGLE_WORKSPACE_VERSION),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "layout": "agents" if explicit_registry else "workspace",
        "data_root_source": str(data_root),
        "workspace_source": str(
            next(
                source
                for profile, source, _, _, _ in workspace_entries
                if profile.id
                == default_agent_id
            )
        ),
        "agents": [
            {
                "id": profile.id,
                "name": profile.name,
                "enabled": profile.enabled,
                "workspace_source": str(source),
                "archive_root": archive_root,
                "workspace_files": len(files),
                "workspace_bytes": size,
            }
            for profile, source, archive_root, files, size in workspace_entries
        ],
        # Reserved for the user dimension. User assets live under
        # `<shared root>/users/<user_id>/`, so today they travel inside the
        # default Agent's workspace and this list stays empty. Declaring the
        # slot now means adding per-user archive roots later is an additive
        # change to the v2 manifest instead of a v3 format break.
        "users": [],
        "contents": {
            "config": config_path.is_file(),
            "legacy_user_data": legacy_path.is_file(),
            "agent_workspaces": len(workspace_entries),
            "workspace_files": total_files,
            "workspace_bytes": total_bytes,
        },
    }

    # os.replace requires the staging archive and output to share a filesystem.
    temp_dir = Path(tempfile.mkdtemp(prefix="cowagent-backup-", dir=str(output.parent)))
    temp_archive = temp_dir / "backup.zip"
    try:
        with zipfile.ZipFile(
            str(temp_archive), "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
        ) as archive:
            def write_file(path, name):
                # Backup API includes committed WAL pages and produces one standalone DB.
                with path.open("rb") as source:
                    is_sqlite = source.read(16) == b"SQLite format 3\0"
                if is_sqlite:
                    snapshot = temp_dir / "sqlite-snapshot"
                    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as src:
                        with closing(sqlite3.connect(snapshot)) as dst:
                            src.backup(dst)
                    try:
                        archive.write(snapshot, name)
                    finally:
                        snapshot.unlink()
                else:
                    archive.write(path, name)

            if config_path.is_file():
                # The archive keeps the roster inside config.json even though a
                # live install stores it separately. An archive is a snapshot,
                # not a layout, and folding it in here keeps one format that
                # every version can both write and read.
                if config:
                    archive.writestr(
                        "data/config.json",
                        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                    )
                else:
                    # Unparseable, so there is nothing to fold in and no way to
                    # rewrite it without losing whatever it does hold.
                    archive.write(str(config_path), "data/config.json")
            if legacy_path.is_file():
                write_file(legacy_path, "data/user_datas.pkl")
            for _, source, archive_root, files, _ in workspace_entries:
                for path in files:
                    relative = path.relative_to(source).as_posix()
                    if complete and path.name.endswith(("-wal", "-shm")):
                        continue
                    write_file(path, f"{archive_root}/{relative}")
            if complete:
                # Data-root files include session caches, attachment associations,
                # credentials and processing state outside the workspace.
                for path in _iter_workspace_files(data_root, excluded, set(sources.values()) | {temp_dir}, complete=True):
                    if path == config_path or path.name.endswith(("-wal", "-shm")):
                        continue
                    write_file(path, "data/instance/" + path.relative_to(data_root).as_posix())
                manifest["schema_version"] = 1
            if not complete:
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        if complete:
            manifest["files"] = {}
            with zipfile.ZipFile(temp_archive) as staged:
                for info in staged.infolist():
                    digest = hashlib.sha256()
                    with staged.open(info) as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            digest.update(chunk)
                    manifest["files"][info.filename] = {"size": info.file_size, "sha256": digest.hexdigest()}
            with zipfile.ZipFile(temp_archive, "a", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        try:
            os.replace(str(temp_archive), str(output))
        except OSError as exc:
            # os.replace is atomic but cannot cross filesystems; shutil.move
            # falls back to copy + remove when the staging dir lands elsewhere.
            if exc.errno != errno.EXDEV:
                raise
            shutil.move(str(temp_archive), str(output))
        try:
            os.chmod(str(output), stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    finally:
        shutil.rmtree(str(temp_dir), ignore_errors=True)

    manifest["archive"] = str(output)
    return manifest


def _validate_archive(archive: zipfile.ZipFile) -> dict:
    names = {info.filename for info in archive.infolist()}
    if len(names) != len(archive.infolist()):
        raise ValueError("duplicate archive entries")
    if "manifest.json" not in names:
        raise ValueError("archive is missing manifest.json")
    try:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("archive manifest is invalid") from exc
    version = manifest.get("version")
    if (
        manifest.get("format") != BACKUP_FORMAT
        or version not in _SUPPORTED_BACKUP_VERSIONS
    ):
        raise ValueError("unsupported CowAgent backup format or version")

    if version == 3:
        if manifest.get("schema_version") != 1:
            raise ValueError("unsupported backup schema_version")
        files = manifest.get("files")
        if not isinstance(files, dict) or set(files) != names - {"manifest.json"}:
            raise ValueError("backup file manifest does not match archive")
        for name, metadata in files.items():
            digest = hashlib.sha256()
            size = 0
            with archive.open(name) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
            if not isinstance(metadata, dict) or metadata.get("size") != size or metadata.get("sha256") != digest.hexdigest():
                raise ValueError(f"backup integrity mismatch: {name}")
    elif archive.testzip() is not None:
        raise ValueError("backup CRC mismatch")

    # The user dimension is declared but not yet produced. Checked at any
    # version, because the slot travels on single-workspace archives too.
    # Refuse an archive that carries per-user roots instead of silently
    # dropping them: a restore that quietly loses one user's memory is worse
    # than one that stops.
    users = manifest.get("users", [])
    if not isinstance(users, list):
        raise ValueError("archive manifest has an invalid users list")
    if users:
        raise ValueError(
            "archive contains user-scoped workspaces, which this CowAgent "
            "version cannot restore; upgrade before restoring"
        )

    allowed_agent_roots = set()
    if version >= 2 and manifest.get("layout") == "agents":
        agents = manifest.get("agents")
        if not isinstance(agents, list) or not agents:
            raise ValueError("multi-agent archive manifest is missing agents")
        seen_ids = set()
        for item in agents:
            if not isinstance(item, dict):
                raise ValueError("multi-agent archive manifest is invalid")
            agent_id = item.get("id")
            root = item.get("archive_root")
            if not isinstance(agent_id, str) or not _AGENT_ID_RE.fullmatch(agent_id):
                raise ValueError("multi-agent archive contains an invalid Agent ID")
            if agent_id in seen_ids or root != f"agents/{agent_id}/workspace":
                raise ValueError("multi-agent archive contains duplicate or invalid roots")
            seen_ids.add(agent_id)
            allowed_agent_roots.add(root + "/")

    for info in archive.infolist():
        name = info.filename
        path = PurePosixPath(name)
        if not name or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name or any(part.endswith((".", " ")) for part in path.parts):
            raise ValueError(f"unsafe archive path: {name!r}")
        mode = (info.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise ValueError(f"symbolic links are not allowed in backups: {name!r}")
        if allowed_agent_roots:
            allowed = name.startswith("data/") or any(
                name.startswith(root) for root in allowed_agent_roots
            )
        else:
            allowed = name.startswith(("data/", "workspace/"))
        if name != "manifest.json" and not allowed:
            raise ValueError(f"unexpected archive entry: {name!r}")
    return manifest


def _extract_validated(archive: zipfile.ZipFile, destination: Path) -> dict:
    file_modes = {}
    for info in archive.infolist():
        target = destination.joinpath(*PurePosixPath(info.filename).parts)
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info, "r") as source, target.open("wb") as output:
            shutil.copyfileobj(source, output)
        # Keep staging files readable for config parsing and copying. Apply
        # archived rwx bits only when publishing the destination's sibling.
        # DOS archives may have no Unix mode; keep their existing defaults.
        mode = (info.external_attr >> 16) & 0o777
        if os.name != "nt" and info.create_system == 3 and mode:
            file_modes[target] = mode
        else:
            file_modes.pop(target, None)
    return file_modes


def _atomic_copy(
    source: Path, destination: Path, private: bool = False, mode: Optional[int] = None
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=destination.name + ".", dir=str(destination.parent))
    os.close(fd)
    try:
        shutil.copy2(str(source), temp_name)
        if mode is not None and not private:
            os.chmod(temp_name, mode)
        os.replace(temp_name, str(destination))
        if private:
            try:
                os.chmod(str(destination), stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)


def _multi_agent_destinations(
    manifest: dict,
    archived_config: dict,
    current_config: dict,
    workspace_root: Optional[Path],
):
    """Resolve safe restore destinations without trusting archived paths."""
    from agent.registry import AgentRegistry

    archived_registry = AgentRegistry.from_config(archived_config)
    manifest_ids = [item["id"] for item in manifest.get("agents", [])]
    configured_ids = [profile.id for profile in archived_registry.list()]
    if sorted(manifest_ids) != sorted(configured_ids):
        raise ValueError("archive Agent registry does not match its workspace manifest")

    current_registry = None
    if current_config.get("agents"):
        try:
            current_registry = AgentRegistry.from_config(current_config)
        except ValueError:
            current_registry = None

    # An explicit --workspace wins; otherwise reuse the instance root this
    # machine already runs on, and only fall back to the single-Agent default
    # on a fresh machine.
    base = (
        Path(workspace_root).expanduser().resolve()
        if workspace_root is not None
        else _workspace_from_config(current_config)
    )
    default_agent_id = archived_registry.default_agent_id
    destinations = {}
    for agent_id in manifest_ids:
        current = None
        if workspace_root is None and current_registry is not None:
            try:
                current = current_registry.get(agent_id, require_enabled=False)
            except KeyError:
                pass
        if current is not None:
            destinations[agent_id] = current.workspace_path.resolve()
            continue
        # Mirror AgentRegistry.from_config: the default Agent owns the instance
        # root, every other Agent lands in `agents/<id>`. Restoring into the
        # same layout the registry derives keeps the workspace keys in the
        # restored config redundant rather than load-bearing, so hand-removing
        # one later cannot relocate an Agent away from its restored data.
        destination = (
            base if agent_id == default_agent_id else base / "agents" / agent_id
        )
        if not _is_within(destination, base):
            raise ValueError(f"unsafe Agent workspace destination: {agent_id}")
        destinations[agent_id] = destination
    if len({str(path) for path in destinations.values()}) != len(destinations):
        raise ValueError("multiple Agents resolve to the same workspace destination")
    return archived_registry, destinations


def _validate_schemas(root: Path) -> None:
    for path in _iter_workspace_files(root, set(), complete=True):
        if path.suffix == ".json":
            try:
                record = json.loads(path.read_text(encoding="utf-8-sig"))
            except (ValueError, UnicodeError):
                continue
            if isinstance(record, dict) and "schema_version" in record and record["schema_version"] != 1:
                raise ValueError(f"unsupported schema_version: {path.name}")
        with path.open("rb") as handle:
            is_sqlite = handle.read(16) == b"SQLite format 3\0"
        if is_sqlite:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                schema_version = db.execute("PRAGMA user_version").fetchone()[0]
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                known_reading = (schema_version == 2 and path.name == 'sources.sqlite3'
                    and path.parent.name == '.personal_sources'
                    and {'reading_records', 'reading_aliases', 'reading_submissions',
                         'reading_parse_heads', 'reading_resumes'}.issubset(tables))
                known_tasks = (schema_version == 2 and path.name == 'tasks.sqlite3'
                    and path.parent.name == 'personal'
                    and {'tasks', 'events', 'reading_publications'}.issubset(tables))
                if schema_version > 1 and not (known_reading or known_tasks):
                    raise ValueError(f"unsupported SQLite schema: {path.name}")
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError(f"invalid SQLite snapshot: {path.name}")
                for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                    quoted = '"' + table.replace('"', '""') + '"'
                    columns = [row[1] for row in db.execute(f"PRAGMA table_info({quoted})")]
                    if "record" in columns:
                        for (body,) in db.execute(f"SELECT record FROM {quoted}"):
                            try:
                                record = json.loads(body)
                            except (ValueError, TypeError):
                                continue
                            if isinstance(record, dict) and "schema_version" in record and record["schema_version"] != 1:
                                raise ValueError(f"unsupported record schema: {path.name}")
                    if "schema_version" in columns and db.execute(f"SELECT 1 FROM {quoted} WHERE schema_version != 1 LIMIT 1").fetchone():
                        raise ValueError(f"unsupported record schema: {path.name}")


def _relocate_personal_databases(staging: Path, manifest: dict, data_root: Path,
                                 target_workspace: Path, destinations: dict) -> None:
    """只改已校验 staging 的 v1 路径槽位；正文、摘要、定位和指纹原样保留。"""
    mappings = []
    if manifest.get("data_root_source"):
        mappings.append((manifest["data_root_source"], data_root))
    for item in manifest.get("agents", []):
        mappings.append((item["workspace_source"], destinations.get(item["id"], target_workspace)))
    if manifest.get("workspace_source"):
        mappings.append((manifest["workspace_source"], target_workspace))

    def pure(value):
        return PureWindowsPath(value) if PureWindowsPath(value).drive or "\\" in value else PurePosixPath(value)

    mappings.sort(key=lambda pair: len(pure(pair[0]).parts), reverse=True)

    def relocate(value):
        if not isinstance(value, str) or not pure(value).is_absolute():
            return value  # Publication result paths are deliberately relative.
        path = pure(value)
        for old, new in mappings:
            try:
                relative = path.relative_to(pure(old))
            except ValueError:
                continue
            if ".." in relative.parts:
                raise ValueError("unsafe persisted source path")
            result = new.joinpath(*relative.parts)
            if not _is_within(result, new):
                raise ValueError("unsafe relocated source path")
            return str(result)
        raise ValueError("persisted source path has no archived root mapping")

    def source_ref(record):
        if isinstance(record, dict):
            for key in ("original", "path"):
                if key in record:
                    record[key] = relocate(record[key])

    def task_record(record):
        if "wiki_workspace" in record:
            record["wiki_workspace"] = relocate(record["wiki_workspace"])
        for item in record.get("attachments", []):
            if "path" in item:
                item["path"] = relocate(item["path"])
        for item in record.get("source_candidates", []):
            if "workspace" in item:
                item["workspace"] = relocate(item["workspace"])
        for key in ("result_refs", "source_existing_refs"):
            for item in record.get(key, []):
                source_ref(item)
        for decision in record.get("attachment_decisions", {}).values():
            if "workspace" in decision:
                decision["workspace"] = relocate(decision["workspace"])
            source_ref(decision.get("source"))
            source_ref(decision.get("result_ref"))

    databases = [(staging / "data/instance/personal/tasks.sqlite3", True)]
    for item in manifest.get("agents", []):
        databases.append((staging / item["archive_root"] / ".personal_sources/sources.sqlite3", False))
    if not manifest.get("agents"):
        databases.append((staging / "workspace/.personal_sources/sources.sqlite3", False))
    for path, is_tasks in databases:
        if not path.is_file():
            continue
        with closing(sqlite3.connect(path)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            with db:
                for table in (("tasks", "events") if is_tasks else ("revisions", "receipts")):
                    if table not in tables:
                        continue
                    for rowid, body in db.execute(f"SELECT rowid, record FROM {table}").fetchall():
                        record = json.loads(body)
                        if is_tasks:
                            task_record(record)
                        elif "original" in record:
                            record["original"] = relocate(record["original"])
                        db.execute(f"UPDATE {table} SET record=? WHERE rowid=?",
                                   (json.dumps(record, ensure_ascii=False), rowid))
                if not is_tasks and "publications" in tables:
                    for rowid, value in db.execute("SELECT rowid, path FROM publications").fetchall():
                        db.execute("UPDATE publications SET path=? WHERE rowid=?", (relocate(value), rowid))
            # Close with no sidecars: publishing copies only the standalone DB.
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("PRAGMA journal_mode=DELETE")


def restore_backup_archive(
    archive_path: Path,
    data_root: Path,
    workspace: Optional[Path] = None,
) -> dict:
    """Merge a validated backup into the selected data root and workspace."""
    archive_path = Path(archive_path).expanduser().resolve()
    data_root = Path(data_root).expanduser().resolve()
    current_config = _read_config(data_root)
    if current_config:
        # A live install keeps its roster beside the workspaces rather than in
        # config.json, so the layout this machine runs on only shows up once
        # team.json is overlaid. Without it every Agent lands on the layout the
        # archive implies and its real workspace is left behind.
        current_config = _team().resolve(current_config)

    temp_dir = Path(tempfile.mkdtemp(prefix="cowagent-restore-"))
    undo = []
    paused_loggers = []
    def preserve(destination):
        saved = temp_dir / "undo" / str(len(undo))
        if destination.exists():
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, saved)
        undo.append((destination, saved if saved.exists() else None))

    def publish(source, destination, **kwargs):
        destination = Path(destination)
        # CLI imports can open the destination run.log before restore begins.
        # Windows cannot replace that file; POSIX would leave the logger writing
        # to its unlinked old inode. Pause only our matching file handlers until
        # publication or rollback has finished, then continue the restored log.
        loggers = [logging.getLogger(), *list(logging.Logger.manager.loggerDict.values())]
        for log in loggers:
            if not isinstance(log, logging.Logger):
                continue
            for handler in list(log.handlers):
                if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).resolve() == destination.resolve():
                    log.removeHandler(handler)
                    paused_loggers.append((log, handler))
                    handler.close()
        # Reject pre-existing symlinks/junction escapes before touching files.
        preserve(destination)
        with Path(source).open("rb") as handle:
            if handle.read(16) == b"SQLite format 3\0":
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(str(destination) + suffix)
                    if sidecar.exists():
                        preserve(sidecar)
                        sidecar.unlink()
        _atomic_copy(source, destination, **kwargs)
    try:
        with zipfile.ZipFile(str(archive_path), "r") as archive:
            manifest = _validate_archive(archive)
            file_modes = _extract_validated(archive, temp_dir)

        archived_config_path = temp_dir / "data" / "config.json"
        archived_config = {}
        if archived_config_path.is_file():
            with archived_config_path.open("r", encoding="utf-8-sig") as handle:
                value = json.load(handle, object_pairs_hook=merge_duplicate_keys)
            if not isinstance(value, dict):
                raise ValueError("archived config.json must contain an object")
            archived_config = value

        if manifest.get("version") == 3:
            _require_personal_stopped(data_root)
            _validate_schemas(temp_dir)
        restored_config = dict(archived_config)
        multi_agent = (
            manifest.get("version", 1) >= 2
            and manifest.get("layout") == "agents"
        )
        destinations = {}
        if multi_agent:
            if not restored_config.get("agents"):
                raise ValueError("multi-agent archive is missing its Agent registry")
            archived_registry, destinations = _multi_agent_destinations(
                manifest, restored_config, current_config, workspace
            )
            restored_agents = []
            for raw in restored_config["agents"]:
                item = dict(raw)
                item["workspace"] = str(destinations[item["id"]])
                restored_agents.append(item)
            restored_config["agents"] = restored_agents
            default_agent_id = archived_registry.default_agent_id
            restored_config["default_agent_id"] = default_agent_id
            target_workspace = destinations[default_agent_id]
            # Keep the singular key aligned for older extensions that still
            # inspect it even when an explicit registry is configured.
            restored_config["agent_workspace"] = str(target_workspace)
            from agent.registry import AgentRegistry

            AgentRegistry.from_config(restored_config)
        else:
            if workspace is not None:
                target_workspace = Path(workspace).expanduser().resolve()
            elif current_config.get("agent_workspace"):
                target_workspace = _workspace_from_config(current_config)
            else:
                # Do not trust an archive-controlled absolute destination on a
                # fresh machine. Portable restores default to ~/cow unless the
                # operator supplies --workspace.
                target_workspace = data_root / "workspace" if manifest.get("version") == 3 else Path("~/cow").expanduser().resolve()
            if restored_config:
                restored_config["agent_workspace"] = str(target_workspace)

        if restored_config:
            appdata_dir = restored_config.get("appdata_dir") or ""
            if appdata_dir:
                archived_appdata = (data_root / appdata_dir).resolve()
                if not _is_within(archived_appdata, data_root):
                    # Keep legacy user data under the selected data root
                    # instead of writing to an archive-controlled path.
                    restored_config["appdata_dir"] = ""
        if manifest.get("version") == 3:
            _validate_schemas(data_root)
            for target in (list(destinations.values()) if multi_agent else [target_workspace]):
                _validate_schemas(target)
            # Resolve every destination before publication, including symlink ancestors.
            roots = {"data/instance/": data_root}
            if multi_agent:
                for item in manifest["agents"]:
                    roots[item["archive_root"] + "/"] = destinations[item["id"]]
            else:
                roots["workspace/"] = target_workspace
            for name in manifest["files"]:
                for prefix, target in roots.items():
                    if name.startswith(prefix) and not _is_within(target / name[len(prefix):], target):
                        raise ValueError("unsafe restore destination")

        if manifest.get("version") == 3:
            _relocate_personal_databases(temp_dir, manifest, data_root, target_workspace, destinations)
            _validate_schemas(temp_dir)

        recovery_point = None
        if manifest.get("version") == 3:
            # Stop the backend before calling restore. Preserve every current byte
            # before a downgrade/restore, including data a compatible old point lacks.
            if data_root.exists() or target_workspace.exists():
                recovery_path = archive_path.parent / ("cow-recovery-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".zip")
                recovery_point = create_backup_archive(recovery_path, data_root, target_workspace, excluded_paths={archive_path}, complete=True)
        restored_files = 0
        restored_agents = []
        if multi_agent:
            manifest_agents = {item["id"]: item for item in manifest["agents"]}
            for agent_id, target in destinations.items():
                archive_root = manifest_agents[agent_id]["archive_root"]
                source_root = temp_dir.joinpath(*PurePosixPath(archive_root).parts)
                agent_files = 0
                if source_root.is_dir():
                    for source in _iter_workspace_files(source_root, set(), complete=manifest.get("version") == 3):
                        relative = source.relative_to(source_root)
                        destination = target / relative
                        if not _is_within(destination, target):
                            raise ValueError(
                                f"unsafe Agent workspace destination: {agent_id}/{relative}"
                            )
                        publish(source, destination, mode=file_modes.get(source))
                        restored_files += 1
                        agent_files += 1
                restored_agents.append(
                    {"id": agent_id, "workspace": str(target), "files": agent_files}
                )
        else:
            source_root = temp_dir / "workspace"
            if source_root.is_dir():
                for source in _iter_workspace_files(source_root, set(), complete=manifest.get("version") == 3):
                    relative = source.relative_to(source_root)
                    destination = target_workspace / relative
                    if not _is_within(destination, target_workspace):
                        raise ValueError(f"unsafe workspace destination: {relative}")
                    publish(source, destination, mode=file_modes.get(source))
                    restored_files += 1

        instance_source = temp_dir / "data" / "instance"
        if manifest.get("version") == 3 and instance_source.is_dir():
            for source in _iter_workspace_files(instance_source, set(), complete=True):
                destination = data_root / source.relative_to(instance_source)
                if not _is_within(destination, data_root):
                    raise ValueError("unsafe instance destination")
                publish(source, destination, private=True)

        # Publish config only after every workspace file has been validated
        # and copied. A copy failure cannot leave config pointing at a partial
        # multi-agent restore.
        if restored_config:
            # Split the archive's single blob back into the two files a live
            # install keeps: the roster travels inside config.json, but it is
            # not read from there any more.
            team = _team()
            published = {
                key: value
                for key, value in restored_config.items()
                if key not in team.TEAM_KEYS
            }
            if multi_agent:
                preserve(team.team_file(published))
                team.write(
                    published,
                    {
                        "agents": team.compact(
                            restored_config["agents"], published, default_agent_id
                        ),
                        "default_agent_id": default_agent_id,
                        "channel_instances": restored_config.get("channel_instances") or [],
                    },
                )
            config_temp = temp_dir / "restored-config.json"
            with config_temp.open("w", encoding="utf-8") as handle:
                json.dump(published, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            publish(config_temp, data_root / "config.json", private=True)

        legacy_source = temp_dir / "data" / "user_datas.pkl"
        if legacy_source.is_file():
            effective_config = restored_config or current_config
            legacy_destination = _legacy_user_data_path(data_root, effective_config)
            publish(legacy_source, legacy_destination, private=True)

        if recovery_point:
            point = temp_dir / "recovery-point.json"
            point.write_text(json.dumps({"schema_version": 1, "created_at": recovery_point["created_at"], "archive": recovery_point["archive"], "restored_from": str(archive_path)}, ensure_ascii=False, indent=2), encoding="utf-8")
            publish(point, data_root / "recovery-point.json", private=True)
        return {
            "manifest": manifest,
            "recovery_point": recovery_point,
            "workspace": str(target_workspace),
            "workspace_files": restored_files,
            "agents": restored_agents,
            "config_restored": bool(restored_config),
            "legacy_user_data_restored": legacy_source.is_file(),
        }
    except Exception:
        for destination, saved in reversed(undo):
            if saved is None:
                destination.unlink(missing_ok=True)
            else:
                shutil.copy2(saved, destination)
        raise
    finally:
        shutil.rmtree(str(temp_dir), ignore_errors=True)
        for log, handler in paused_loggers:
            handler.mode = "a"
            log.addHandler(handler)


@click.command("backup")
@click.option(
    "--output",
    "-o",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Output .zip path (default: ./cow-backup-<timestamp>.zip).",
)
def backup_command(output: Optional[Path]):
    """Back up config, persona, memory, skills, knowledge, and schedules."""
    data_root = _data_root()
    config = _read_config(data_root)
    workspace = _workspace_from_config(config)
    if config.get("personal_assistant_enabled") or config.get("personal_instance"):
        try:
            from cli.commands.process import _read_pid
            pid = _read_pid()
            if pid:
                raise ValueError(f"后端仍在运行（PID {pid}），请先停止后端再执行完整备份")
            _require_personal_stopped(data_root)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    if output is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output = Path.cwd() / f"cow-backup-{stamp}.zip"
    try:
        result = create_backup_archive(output, data_root, workspace)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(click.style("✓ Backup created", fg="green"))
    click.echo(f"  Archive: {result['archive']}")
    click.echo(f"  Agent workspaces: {result['contents']['agent_workspaces']}")
    click.echo(f"  Workspace files: {result['contents']['workspace_files']}")
    click.echo("  Keep this archive private: it may contain API keys and personal data.")


@click.command("restore")
@click.argument("archive", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--workspace",
    type=click.Path(file_okay=False, path_type=Path),
    help="Restore a single workspace here, or use this as the root for Agent subdirectories.",
)
@click.option("--yes", is_flag=True, help="Confirm overwriting matching files.")
def restore_command(archive: Path, workspace: Optional[Path], yes: bool):
    """Restore a backup without deleting unrelated destination files."""
    from cli.commands.process import _read_pid

    pid = _read_pid()
    if pid:
        raise click.ClickException(
            f"CowAgent is running (PID: {pid}). Run 'cow stop' before restoring."
        )
    if not yes:
        click.confirm(
            "Restore this archive and overwrite matching config/workspace files?",
            abort=True,
        )

    data_root = _data_root()
    current_config = _read_config(data_root)
    current_workspace = _workspace_from_config(current_config)
    has_current_data = (data_root / "config.json").is_file() or current_workspace.is_dir()
    if has_current_data:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        rollback = archive.resolve().parent / f"cow-pre-restore-{stamp}.zip"
        create_backup_archive(
            rollback,
            data_root,
            current_workspace,
            excluded_paths={archive.resolve()},
        )
        click.echo(f"Rollback backup: {rollback}")

    result = restore_backup_archive(archive, data_root, workspace)
    click.echo(click.style("✓ Backup restored", fg="green"))
    click.echo(f"  Workspace: {result['workspace']}")
    if result["agents"]:
        click.echo(f"  Agent workspaces: {len(result['agents'])}")
    click.echo(f"  Restored files: {result['workspace_files']}")
