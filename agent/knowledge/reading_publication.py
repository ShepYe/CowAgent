"""有界多批发布台账。目标由可信阅读服务登记，工具只提交已核验覆盖。

task schema 仍为 v1；本表独立版本。workspace 相对任务库根保存，整根迁移
后保持关联。resume_publication 是跨 attempt 接续入口，模型回调不得调用。
"""
import hashlib
import json
import os
from pathlib import Path

from common.personal_tasks import TaskError, _json, now

MAX_TARGETS = 4096
MAX_BATCHES = 256
MAX_RECORD_BYTES = 2 * 1024 * 1024


def _scope(values):
    if (not isinstance(values, list) or not values or len(values) > MAX_TARGETS
            or any(not isinstance(v, str) or not v or len(v) > 512 for v in values)
            or len(set(values)) != len(values)):
        raise TaskError("invalid_request", "阅读范围无效或超过上限")
    return sorted(values)


def _load(db, task):
    if "reading_publication" not in task:
        return None
    if task["reading_publication"] != {"schema_version": 1}:
        raise TaskError("version_conflict", "不支持的阅读发布版本")
    row = db.execute("SELECT record FROM reading_publications WHERE task_id=?", (task["task_id"],)).fetchone()
    try:
        ledger = json.loads(row[0]) if row else None
        if (not isinstance(ledger, dict) or ledger.get("schema_version") != 1
                or ledger.get("task_id") != task["task_id"]
                or not isinstance(ledger.get("batches"), dict)
                or len(ledger["batches"]) > MAX_BATCHES):
            raise ValueError()
        _scope(ledger["targets"])
        for key, batch in ledger["batches"].items():
            if (not key or not set(_scope(batch["coverage"])).issubset(ledger["targets"])
                    or batch["status"] not in {"prepared", "published"}
                    or not isinstance(batch["refs"], list)
                    or (batch["status"] == "published" and not batch["refs"])
                    or not batch["batch_id"] or not batch["workspace"]
                    or not isinstance(batch["attempt"], int)):
                raise ValueError()
        return ledger
    except (ValueError, KeyError, TypeError, TaskError) as exc:
        raise TaskError("version_conflict", "阅读发布台账损坏或版本未知") from exc


def _encode(ledger):
    body = _json(ledger)
    if len(body.encode("utf-8")) > MAX_RECORD_BYTES:
        raise TaskError("limit_exceeded", "阅读发布台账超过上限")
    return body


def _save(db, ledger):
    body = _encode(ledger)
    db.execute("INSERT OR REPLACE INTO reading_publications VALUES(?,?)", (ledger["task_id"], body))


def _current(store, db, task, allowed=("running",)):
    current = store._read(db, task["owner_id"], task["task_id"])
    if current["attempt"] != task["attempt"] or current["status"] not in allowed:
        raise TaskError("version_conflict")
    return current


def register_target(store, task, targets):
    """可信程序在首次阅读前登记全部长期目标，不从模型页窗推导目标。"""
    targets = _scope(targets)
    with store._transaction() as db:
        current = _current(store, db, task, ("queued", "running"))
        ledger = _load(db, current)
        if ledger is not None:
            if ledger["targets"] != targets:
                raise TaskError("idempotency_conflict", "任务目标不可缩小或替换")
            db.execute("PRAGMA user_version=2")
            return current
        if current.get("wiki_batch_id") or current.get("result_refs"):
            raise TaskError("version_conflict", "已有旧路径发布，不可中途改为阅读任务")
        ledger = dict(schema_version=1, task_id=task["task_id"], targets=targets,
                      registered_attempt=task["attempt"], created_at=now(), batches={})
        current["reading_publication"] = {"schema_version": 1}
        _save(db, ledger)
        # 库级写屏障阻止不识别阅读目标的旧 worker 把部分批次误 complete。
        # task record 本身继续 schema v1；无阅读记录的旧库不升级。
        db.execute("PRAGMA user_version=2")
        return store._save(db, current)


def publication_state(store, task):
    with store._transaction() as db:
        current = store._read(db, task["owner_id"], task["task_id"])
        ledger = _load(db, current)
    if ledger is None:
        return None
    covered = {v for b in ledger["batches"].values() if b["status"] == "published" for v in b["coverage"]}
    return dict(ledger, remaining=sorted(set(ledger["targets"]) - covered))


def _wiki(store, batch, owner):
    from agent.knowledge.wiki import WikiService
    return WikiService((store.root / batch["workspace"]).resolve(), owner)


def _candidate(wiki, task, batch_id):
    with wiki.source.transaction() as db:
        row = db.execute("SELECT record,published FROM wiki_batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            raise TaskError("version_conflict", "知识候选不存在")
        candidate = json.loads(row[0])
        if candidate.get("task_id") != task["task_id"]:
            raise TaskError("forbidden", "知识候选不属于本任务")
        return candidate, bool(row[1])


def _payload_digest(payload):
    try:
        return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
    except (ValueError, TypeError) as exc:
        raise TaskError("invalid_request", "知识载荷无法规范化") from exc


def prepare_publication(store, task, submission_id, coverage, pages, workspace):
    """可信知识提交入口；重放比较先于 Wiki.prepare 的任何副作用。

    对象键排序，所有数组保留顺序。返回 Wiki.prepare 的 candidate（含 batch_id）。
    """
    coverage = _scope(coverage)
    if not isinstance(submission_id, str) or not submission_id or len(submission_id) > 256:
        raise TaskError("invalid_request")
    digest = _payload_digest(pages)
    location = os.path.relpath(Path(workspace).resolve(), store.root)
    with store.commit_guard(task["owner_id"], task["task_id"], task["attempt"]):
        state = publication_state(store, task)
        if state is None or not set(coverage).issubset(state["targets"]):
            raise TaskError("invalid_request", "提交范围超出任务目标")
        old = state["batches"].get(submission_id)
        if old and (old.get("payload_digest") != digest or old["coverage"] != coverage
                    or old["workspace"] != location):
            raise TaskError("idempotency_conflict")
        if not old and len(state["batches"]) >= MAX_BATCHES:
            raise TaskError("limit_exceeded")
        from agent.knowledge.wiki import WikiService
        wiki = WikiService(workspace, task["owner_id"])
        if old:
            candidate, _ = _candidate(wiki, task, old["batch_id"])
            if candidate["fingerprint"] != old["fingerprint"]:
                raise TaskError("version_conflict")
            return candidate
        candidate = wiki.prepare(pages, task_id=task["task_id"])
        register_batch(store, task, submission_id, coverage, candidate["batch_id"], workspace, pages)
        return candidate


def register_batch(store, task, submission_id, coverage, batch_id, workspace, payload=None):
    """Wiki.prepare 后、publish 前登记；coverage 须先经阅读服务核验。"""
    coverage = _scope(coverage)
    if not isinstance(submission_id, str) or not submission_id or len(submission_id) > 256:
        raise TaskError("invalid_request")
    from agent.knowledge.wiki import WikiService
    with store.commit_guard(task["owner_id"], task["task_id"], task["attempt"]):
        state = publication_state(store, task)
        if state is None or not set(coverage).issubset(state["targets"]):
            raise TaskError("invalid_request", "提交范围超出任务目标")
        candidate, _ = _candidate(WikiService(workspace, task["owner_id"]), task, batch_id)
        location = os.path.relpath(Path(workspace).resolve(), store.root)
        digest = hashlib.sha256(_json(dict(coverage=coverage, batch_id=batch_id,
                    workspace=location, fingerprint=candidate["fingerprint"], payload=payload)).encode()).hexdigest()
        with store._transaction() as db:
            current = _current(store, db, task)
            ledger = _load(db, current)
            if ledger is None or not set(coverage).issubset(ledger["targets"]):
                raise TaskError("invalid_request", "提交范围超出任务目标")
            old = ledger["batches"].get(submission_id)
            if old:
                if old["digest"] != digest:
                    raise TaskError("idempotency_conflict")
                return old
            if any(b["batch_id"] == batch_id for b in ledger["batches"].values()):
                raise TaskError("idempotency_conflict", "知识批次已登记其他提交身份")
            if len(ledger["batches"]) >= MAX_BATCHES:
                raise TaskError("limit_exceeded")
            batch = dict(batch_id=batch_id, coverage=coverage, workspace=location,
                         digest=digest, payload_digest=_payload_digest(payload),
                         fingerprint=candidate["fingerprint"], attempt=task["attempt"],
                         status="prepared", refs=[], created_at=now())
            ledger["batches"][submission_id] = batch
            _save(db, ledger)
            return batch


def _receipt(store, task, submission_id, refs):
    with store._transaction() as db:
        current = _current(store, db, task, ("queued", "running", "waiting_input"))
        ledger = _load(db, current)
        batch = ledger["batches"][submission_id]
        batch.update(status="published", refs=refs)
        for ref in refs:
            if ref not in current["result_refs"]:
                current["result_refs"].append(ref)
        _save(db, ledger)
        return store._save(db, current)


def publish_batch(store, task, submission_id):
    with store.commit_guard(task["owner_id"], task["task_id"], task["attempt"]):
        state = publication_state(store, task)
        if state is None or submission_id not in state["batches"]:
            raise TaskError("invalid_request")
        batch = state["batches"][submission_id]
        if batch["attempt"] != task["attempt"] and batch.get("adopted_attempt") != task["attempt"]:
            raise TaskError("version_conflict", "旧候选须经可信恢复器接续")
        if batch["status"] == "published":
            return store.get(task["owner_id"], task["task_id"])
        wiki = _wiki(store, batch, task["owner_id"])
        candidate, committed = _candidate(wiki, task, batch["batch_id"])
        if candidate["fingerprint"] != batch["fingerprint"]:
            raise TaskError("version_conflict")
        # 在 wiki 提交前确认回执可完整落盘，避免大小上限制造不可恢复窗口。
        preview = json.loads(_json({k: v for k, v in state.items() if k != "remaining"}))
        preview["batches"][submission_id].update(status="published", refs=_committed_refs(wiki, candidate))
        _encode(preview)
        refs = _committed_refs(wiki, candidate, validate=True) if committed else wiki.publish(batch["batch_id"])
        # 此处崩溃时 wiki 已提交，重启按稳定 batch_id 补回执。
        return _receipt(store, task, submission_id, refs)


def _committed_refs(wiki, candidate, validate=False):
    from agent.knowledge.wiki import _ref, _valid
    with wiki.source.transaction() as db:
        if validate and any(not _valid(db, wiki.source, db.execute(
                'SELECT * FROM wiki_publications WHERE id=?', (r['page_version'],)).fetchone())
                for r in candidate['pages']):
            raise TaskError("version_conflict", "已发布知识或来源已失效")
        return [_ref(r, db=db) for r in candidate["pages"]]


def reconcile_committed(store, task):
    """取消前只读取已提交结果，不发布尚未提交的候选。调用方持任务锁。"""
    state = publication_state(store, task)
    if state is None:
        return
    for key, batch in state["batches"].items():
        if batch["status"] == "published":
            continue
        wiki = _wiki(store, batch, task["owner_id"])
        candidate, committed = _candidate(wiki, task, batch["batch_id"])
        if committed:
            if candidate["fingerprint"] != batch["fingerprint"]:
                raise TaskError("version_conflict")
            _receipt(store, task, key, _committed_refs(wiki, candidate))


def _satisfied(ledger):
    return set(ledger["targets"]).issubset({v for b in ledger["batches"].values()
                                         if b["status"] == "published" for v in b["coverage"]})


def finish_publication(store, task):
    with store.commit_guard(task["owner_id"], task["task_id"], task["attempt"]):
        state = publication_state(store, task)
        if state is not None and not state["remaining"]:
            return store.complete(task["owner_id"], task["task_id"], task["attempt"], "published", [])
        return store.get(task["owner_id"], task["task_id"])


def resume_publication(store, task):
    """可信恢复入口：核验持久候选、关联当前 attempt，再接续剩余发布。"""
    state = publication_state(store, task)
    if state is None:
        return store.get(task["owner_id"], task["task_id"])
    for key, batch in state["batches"].items():
        with store.commit_guard(task["owner_id"], task["task_id"], task["attempt"]):
            if batch["status"] == "published":
                continue
            wiki = _wiki(store, batch, task["owner_id"])
            candidate, _ = _candidate(wiki, task, batch["batch_id"])
            if candidate["fingerprint"] != batch["fingerprint"]:
                raise TaskError("version_conflict")
            with store._transaction() as db:
                current = _current(store, db, task)
                ledger = _load(db, current)
                ledger["batches"][key]["adopted_attempt"] = task["attempt"]
                _save(db, ledger)
            publish_batch(store, task, key)
    return finish_publication(store, task)
