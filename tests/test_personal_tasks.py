"""持久接收、故障、权限、取消屏障和恢复的契约测试。"""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from common.personal_tasks import OWNER, TaskError, TaskStore


def submit(store, request="request-1", text="共享资料"):
    return store.submit(OWNER, request, {"text": text}, {"channel": "web"}, "cfg1-test")


def test_concurrent_receipt_survives_restart_and_conflict(tmp_path):
    def receive(_):
        return submit(TaskStore(tmp_path))["task_id"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(receive, range(20)))
    assert len(set(ids)) == 1
    store = TaskStore(tmp_path)
    assert len(store.list()) == 1
    with pytest.raises(TaskError, match="idempotency_conflict"):
        submit(store, text="不同请求")
    with pytest.raises(TaskError, match="forbidden"):
        store.get("stranger", ids[0])


def test_original_bytes_and_association_are_preserved(tmp_path):
    store = TaskStore(tmp_path / "state")
    task = submit(store)
    source = tmp_path / "sample.docx"
    source.write_bytes(b"source-version-1")
    item = store.attach(OWNER, task["task_id"], source, attachment_id="file-key")
    source.write_bytes(b"version-2")
    assert store.attachment(OWNER, task["task_id"], "file-key") == item
    with pytest.raises(TaskError, match="idempotency_conflict"):
        store.attach(OWNER, task["task_id"], source, attachment_id="file-key")
    with pytest.raises(TaskError, match="forbidden"):
        store.attachment(OWNER, task["task_id"], "arbitrary-path")


def test_cancel_blocks_late_completion_and_publish_wins(tmp_path):
    store = TaskStore(tmp_path)
    t = submit(store)
    store.claim(OWNER, t["task_id"])
    store.cancel(OWNER, t["task_id"], 1)
    with pytest.raises(TaskError, match="version_conflict"):
        with store.commit_guard(OWNER, t["task_id"], 1):
            pytest.fail("取消后不能进入发布")
    t2 = submit(store, "second")
    store.claim(OWNER, t2["task_id"])
    with store.commit_guard(OWNER, t2["task_id"], 1):
        store.complete(OWNER, t2["task_id"], 1, "transient", [])
    with pytest.raises(TaskError, match="version_conflict"):
        store.cancel(OWNER, t2["task_id"], 1)


def test_pending_index_retries_same_task_and_notifications_are_independent(tmp_path):
    store = TaskStore(tmp_path)
    t = submit(store)
    store.claim(OWNER, t["task_id"])
    store.fail(OWNER, t["task_id"], 1, "index_failed", "pending_index")
    task = store.retry(OWNER, t["task_id"], 1)
    assert task["attempt"] == 2
    store.claim(OWNER, task["task_id"])
    store.complete(OWNER, task["task_id"], 2, "published", [{"publish_id": "pub-1"}])
    store.notification(OWNER, task["task_id"], "failed")
    after = TaskStore(tmp_path).get(OWNER, t["task_id"])
    assert after["status"] == "succeeded" and after["notification"] == "failed"
    assert len(store.history(OWNER, t["task_id"])) >= 7


def test_unknown_schema_is_not_overwritten(tmp_path):
    store = TaskStore(tmp_path)
    submit(store)
    with sqlite3.connect(str(store.db)) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(TaskError, match="schema 99"):
        TaskStore(tmp_path)
    with sqlite3.connect(str(store.db)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99
        assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_restart_running_enters_reconciliation(tmp_path):
    store = TaskStore(tmp_path)
    t = submit(store)
    store.claim(OWNER, t["task_id"])
    assert TaskStore(tmp_path).reconcile()[0]["status"] == "needs_reconcile"


def test_disk_failure_never_returns_accepted(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(store, "_save", fail)
    with pytest.raises(OSError):
        submit(store)
    assert TaskStore(tmp_path).list() == []
