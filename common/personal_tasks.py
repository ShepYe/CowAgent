"""个人实例的可靠接收、处理轨迹和提交屏障；原件永不自动清理。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from uuid import uuid4

OWNER = "local-owner"
TERMINAL = {"succeeded", "failed", "cancelled"}
_locks = {}
_locks_guard = threading.Lock()
_task_context = ContextVar("personal_task_id", default=None)


def current_task_id():
    return _task_context.get()


@contextmanager
def task_scope(task_id):
    token = _task_context.set(task_id)
    try:
        yield
    finally:
        _task_context.reset(token)


class TaskError(ValueError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


def now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def personal_enabled():
    from config import conf
    return os.environ.get("COW_PERSONAL") == "1" or bool(conf().get("personal_assistant_enabled", False))


class TaskStore:
    def __init__(self, root=None):
        from config import get_data_root
        self.root = Path(root or Path(get_data_root()) / "personal").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "tasks.sqlite3"
        with _locks_guard:
            self.lock = _locks.setdefault(str(self.db), threading.RLock())
        with self._transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise TaskError("version_conflict", f"任务库需要 schema {version}，当前支持 1/2")
            db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, record TEXT NOT NULL, UNIQUE(owner,request_id))")
            db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, at TEXT NOT NULL, record TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS reading_publications (task_id TEXT PRIMARY KEY, record TEXT NOT NULL)")
            db.execute("PRAGMA user_version=" + str(max(1, version)))

    @contextmanager
    def _transaction(self):
        with self.lock:
            db = sqlite3.connect(str(self.db), timeout=30)
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    @staticmethod
    def _owner(owner):
        if owner != OWNER:
            raise TaskError("forbidden")

    def _read(self, db, owner, task_id):
        self._owner(owner)
        row = db.execute("SELECT record FROM tasks WHERE id=? AND owner=?", (task_id, owner)).fetchone()
        if not row:
            raise TaskError("forbidden", "任务不存在或不属于当前身份")
        return json.loads(row[0])

    def _save(self, db, record):
        record["updated_at"] = now()
        body = _json(record)
        db.execute("UPDATE tasks SET record=? WHERE id=?", (body, record["task_id"]))
        db.execute("INSERT INTO events(task_id,at,record) VALUES(?,?,?)", (record["task_id"], record["updated_at"], body))
        return record

    def submit(self, owner, request_id, payload, origin, config_revision):
        self._owner(owner)
        if not isinstance(request_id, str) or not request_id or not isinstance(payload, dict) or not origin or not config_revision:
            raise TaskError("invalid_request")
        fingerprint = hashlib.sha256(_json({"payload": payload, "origin": origin}).encode()).hexdigest()
        with self._transaction() as db:
            old = db.execute("SELECT fingerprint,record FROM tasks WHERE owner=? AND request_id=?", (owner, request_id)).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise TaskError("idempotency_conflict")
                return json.loads(old[1])
            task_id = uuid4().hex
            record = dict(schema_version=1, receipt_id=uuid4().hex, task_id=task_id, request_id=request_id,
                          owner_id=owner, origin=origin, payload=payload, status="queued", stage="receive", attempt=1,
                          config_revision=config_revision, attachments=[], result_refs=[], outcome=None, error=None,
                          notification="pending", created_at=now(), updated_at=now())
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?)", (task_id, owner, request_id, fingerprint, _json(record)))
            return self._save(db, record)

    def get(self, owner, task_id):
        with self._transaction() as db:
            return self._read(db, owner, task_id)

    def list(self, owner=OWNER):
        self._owner(owner)
        with self._transaction() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT record FROM tasks WHERE owner=? ORDER BY rowid DESC", (owner,))]

    def history(self, owner, task_id):
        with self._transaction() as db:
            self._read(db, owner, task_id)
            return [json.loads(r[0]) for r in db.execute("SELECT record FROM events WHERE task_id=? ORDER BY seq", (task_id,))]

    def attach(self, owner, task_id, source_path, filename=None, attachment_id=None):
        """仅受信下载/上传入口调用；Agent 使用已登记的附件 ID。"""
        self.get(owner, task_id)
        source = Path(source_path).resolve(strict=True)
        if not source.is_file() or source.stat().st_size > 100 * 1024 * 1024:
            raise TaskError("limit_exceeded")
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        blob_dir = self.root / "originals"
        blob_dir.mkdir(exist_ok=True)
        dest = blob_dir / digest
        if not dest.exists():
            temp = blob_dir / (uuid4().hex + ".part")
            try:
                with temp.open("xb") as out:
                    out.write(content)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temp, dest)
            finally:
                temp.unlink(missing_ok=True)
        item = dict(attachment_id=attachment_id or uuid4().hex, filename=Path(filename or source.name).name,
                    path=str(dest), sha256=digest, bytes=len(content))
        with self._transaction() as db:
            record = self._read(db, owner, task_id)
            for old in record["attachments"]:
                if old["attachment_id"] == item["attachment_id"]:
                    if old["sha256"] != digest:
                        raise TaskError("idempotency_conflict")
                    return old
            record["attachments"].append(item)
            self._save(db, record)
        return item

    def attachment(self, owner, task_id, attachment_id):
        record = self.get(owner, task_id)
        item = next((a for a in record["attachments"] if a["attachment_id"] == attachment_id), None)
        if not item:
            raise TaskError("forbidden", "附件未登记在本次接收记录")
        path = Path(item["path"]).resolve()
        if path.parent != (self.root / "originals").resolve() or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise TaskError("storage_failed", "原件校验失败")
        return item

    def _change(self, owner, task_id, expected_attempt, allowed, **updates):
        with self._transaction() as db:
            r = self._read(db, owner, task_id)
            if r["attempt"] != expected_attempt or r["status"] not in allowed:
                raise TaskError("version_conflict", "任务状态已改变，请刷新")
            r.update(updates)
            return self._save(db, r)

    def claim(self, owner, task_id):
        r = self.get(owner, task_id)
        return self._change(owner, task_id, r["attempt"], {"queued"}, status="running", stage="download")

    def stage(self, owner, task_id, attempt, stage):
        if stage not in {"download", "decide", "parse", "publish", "pending_index", "done"}:
            raise TaskError("invalid_request")
        return self._change(owner, task_id, attempt, {"running"}, stage=stage)

    @contextmanager
    def commit_guard(self, owner, task_id, attempt):
        """与取消共用写锁；发布在此屏障内完成，先提交者获胜。"""
        with self.lock:
            r = self.get(owner, task_id)
            if r["attempt"] != attempt or r["status"] != "running":
                raise TaskError("version_conflict")
            yield r

    def complete(self, owner, task_id, attempt, outcome, result_refs):
        if outcome not in {"published", "transient", "duplicate"}:
            raise TaskError("invalid_request")
        with self._transaction() as db:
            r = self._read(db, owner, task_id)
            if r["attempt"] != attempt or r["status"] != "running":
                raise TaskError("version_conflict")
            if "reading_publication" in r:
                from agent.knowledge.reading_publication import _load, _satisfied
                ledger = _load(db, r)
                if outcome != "published" or not _satisfied(ledger):
                    raise TaskError("reading_incomplete")
                # 调用方不能用最后一批回执覆盖之前的成果。
                result_refs = r["result_refs"]
            r.update(status="succeeded", stage="done", outcome=outcome, result_refs=result_refs, error=None)
            return self._save(db, r)

    def wait_input(self, owner, task_id, attempt, question):
        return self._change(owner, task_id, attempt, {"running"}, status="waiting_input", stage="decide", question=question)

    def fail(self, owner, task_id, attempt, code, stage, retryable=True):
        return self._change(owner, task_id, attempt, {"running", "needs_reconcile"}, status="failed", stage=stage,
                            error={"code": code, "stage": stage, "message": code, "retryable": retryable})

    def cancel(self, owner, task_id, attempt):
        with self.lock:
            r = self.get(owner, task_id)
            if r["attempt"] != attempt or r["status"] not in {"queued", "running", "waiting_input"}:
                raise TaskError("version_conflict")
            if "reading_publication" in r:
                from agent.knowledge.reading_publication import reconcile_committed
                reconcile_committed(self, r)
            return self._change(owner, task_id, attempt, {"queued", "running", "waiting_input"}, status="cancelled")

    def retry(self, owner, task_id, attempt):
        r = self.get(owner, task_id)
        if not (r.get("error") or {}).get("retryable"):
            raise TaskError("invalid_request")
        return self._change(owner, task_id, attempt, {"failed"}, status="queued", attempt=attempt + 1, error=None)

    def update_input(self, owner, task_id, attempt, input):
        if not isinstance(input, str) or not input.strip():
            raise TaskError("invalid_request")
        return self._change(owner, task_id, attempt, {"waiting_input"}, status="queued", clarification=input)

    def notification(self, owner, task_id, status):
        if status not in {"pending", "sent", "failed", "unknown"}:
            raise TaskError("invalid_request")
        with self._transaction() as db:
            r = self._read(db, owner, task_id)
            r["notification"] = status
            return self._save(db, r)

    def reconcile(self):
        """本地下载/决策可幂等重做；发布阶段须查知识登记后明确接续。"""
        for r in self.list():
            if r["status"] == "running":
                updates = {"status": "needs_reconcile"}
                if "reading_publication" in r:
                    # 重启隔离旧回调；持久候选由可信恢复器关联新 attempt。
                    updates["attempt"] = r["attempt"] + 1
                self._change(OWNER, r["task_id"], r["attempt"], {"running"}, **updates)
        return self.list()


def enqueue(task_id):
    """调度到同一主 Agent，进程退出后的记录由启动对账恢复。"""
    from common.personal_worker import enqueue as submit
    return submit(task_id)
