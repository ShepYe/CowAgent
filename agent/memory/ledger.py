"""会话记忆权威台账。SQLite 提交先于可重建的 Markdown 投影。"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from common.runtime_identity import current_identity


SCHEMA_VERSION = 1
DISPOSITIONS = {"conversation_memory", "sourced_knowledge", "archive_only", "undecided"}
_TABLES = {
    "memory_schema": "version INTEGER NOT NULL",
    "memory_scopes": "agent_id TEXT, user_id TEXT, workspace_id TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(agent_id,user_id)",
    "memory_evidence": "message_id TEXT PRIMARY KEY, agent_id TEXT, user_id TEXT, session_id TEXT, record TEXT NOT NULL",
    "memory_candidates": "candidate_id TEXT PRIMARY KEY, agent_id TEXT, user_id TEXT, session_id TEXT, fact_key TEXT, record TEXT NOT NULL, UNIQUE(agent_id,user_id,session_id,fact_key)",
    "memory_decisions": "decision_id TEXT PRIMARY KEY, candidate_id TEXT, revision INTEGER, record TEXT NOT NULL, UNIQUE(candidate_id,revision)",
    "memory_entries": "entry_id TEXT PRIMARY KEY, agent_id TEXT, user_id TEXT, revision INTEGER, state TEXT, record TEXT NOT NULL",
    "memory_operations": "agent_id TEXT, user_id TEXT, session_id TEXT, request_id TEXT, payload TEXT, record TEXT NOT NULL, PRIMARY KEY(agent_id,user_id,session_id,request_id)",
    "memory_files": "agent_id TEXT, user_id TEXT, path TEXT, hash TEXT, record TEXT NOT NULL, PRIMARY KEY(agent_id,user_id,path)",
    "memory_history": "id INTEGER PRIMARY KEY, entry_id TEXT, record TEXT NOT NULL",
    "memory_legacy_exclusions": "agent_id TEXT, user_id TEXT, path TEXT, hash TEXT, start INTEGER, end INTEGER, text TEXT",
}
_BLOCK = re.compile(r"<!-- cow-entry (\{[^\n]*\}) -->\n(.*?)\n<!-- /cow-entry -->", re.S)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


class MemoryError(ValueError):
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


class MemoryLedger:
    def __init__(self, workspace_dir, store=None):
        from agent.memory.conversation_store import get_conversation_store
        self.workspace = Path(workspace_dir).resolve()
        self.store = store or get_conversation_store(self.workspace)
        self.db_path = self.store._db_path
        self.agent_id = self.store._agent_id
        identity = current_identity()
        self.user_id = identity.user_id or ""
        self.session_id = identity.session_id or ""
        with self._transaction() as db:
            existing = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            present = existing.intersection(_TABLES)
            if present and present != set(_TABLES):
                raise MemoryError("unsupported_schema", "记忆表结构不完整，已停止写入")
            if not present:
                for table, columns in _TABLES.items():
                    db.execute(f"CREATE TABLE {table} ({columns})")
                db.execute("INSERT INTO memory_schema VALUES (?)", (SCHEMA_VERSION,))
            self._check_schema(db)
            db.execute("INSERT OR IGNORE INTO memory_scopes VALUES (?,?,?,0)",
                       (self.agent_id, self.user_id, str(uuid.uuid4())))
            self.workspace_id = db.execute("SELECT workspace_id FROM memory_scopes WHERE agent_id=? AND user_id=?",
                                           self.scope).fetchone()[0]

    @property
    def scope(self):
        return self.agent_id, self.user_id

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(str(self.db_path), timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _check_schema(self, db):
        try:
            versions = [r[0] for r in db.execute("SELECT version FROM memory_schema")]
            if versions != [SCHEMA_VERSION]:
                raise MemoryError("unsupported_schema", "未知记忆格式版本，已保留原数据")
            for table, definition in _TABLES.items():
                # 检查所有持久字段，避免半次迁移被 CREATE IF NOT EXISTS 掩盖。
                required = set(re.findall(r"(?:^|,)\s*(\w+)\s+(?:TEXT|INTEGER)\b", definition))
                columns = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
                if not required.issubset(columns):
                    raise MemoryError("unsupported_schema", f"记忆表 {table} 不完整")
        except sqlite3.DatabaseError as exc:
            raise MemoryError("unsupported_schema", str(exc)) from exc

    def _revision(self, db):
        return db.execute("SELECT revision FROM memory_scopes WHERE agent_id=? AND user_id=?", self.scope).fetchone()[0]

    def revision(self):
        with self._transaction() as db:
            self._check_schema(db)
            return self._revision(db)

    def context_snapshot(self):
        """摘要依赖当前事实的有效修订；单纯新增事实不使旧任务摘要失效。"""
        with self._transaction() as db:
            self._check_schema(db)
            entries = [dict(entry_id=row[0], revision=row[1]) for row in db.execute(
                "SELECT entry_id,revision FROM memory_entries WHERE agent_id=? AND user_id=? AND state='active' ORDER BY entry_id", self.scope)]
            legacy = [list(row) for row in db.execute(
                "SELECT path,hash,start,end FROM memory_legacy_exclusions WHERE agent_id=? AND user_id=? ORDER BY path,hash,start,end", self.scope)]
            return dict(schema_version=1, workspace_id=self.workspace_id, agent_id=self.agent_id,
                        user_id=self.user_id, revision=self._revision(db), entries=entries, legacy_sha256=_hash(_json(legacy)))

    def context_valid(self, snapshot):
        if not isinstance(snapshot, dict) or any(snapshot.get(key) != value for key, value in (
                ("schema_version", 1), ("workspace_id", self.workspace_id), ("agent_id", self.agent_id), ("user_id", self.user_id))):
            return False
        with self._transaction() as db:
            self._check_schema(db)
            for ref in snapshot.get("entries", []):
                row = db.execute("SELECT state,revision FROM memory_entries WHERE entry_id=? AND agent_id=? AND user_id=?",
                                 (ref.get("entry_id"), *self.scope)).fetchone()
                if not row or row[0] != "active" or row[1] != ref.get("revision"):
                    return False
            legacy = [list(row) for row in db.execute(
                "SELECT path,hash,start,end FROM memory_legacy_exclusions WHERE agent_id=? AND user_id=? ORDER BY path,hash,start,end", self.scope)]
            return snapshot.get("legacy_sha256") == _hash(_json(legacy))

    def _bump(self, db):
        db.execute("UPDATE memory_scopes SET revision=revision+1 WHERE agent_id=? AND user_id=?", self.scope)
        return self._revision(db)

    def capture(self, messages, session_id=None, origin=None):
        """冻结依据；未知/重放的 user 包装保持 internal，不推断为真人。"""
        session = session_id or self.session_id
        from common.personal_tasks import current_task_id
        with self._transaction() as db:
            self._check_schema(db)
            for message in messages:
                extras = message.setdefault("extras", {})
                trace = copy.deepcopy(extras.get("memory_trace") or {})
                if trace.get("schema_version", 1) != 1:
                    raise MemoryError("unsupported_schema", "消息记忆元数据版本未知，保留原记录")
                message_id = trace.get("message_id") or message.get("message_id") or str(uuid.uuid4())
                previous = db.execute("SELECT * FROM memory_evidence WHERE message_id=?", (message_id,)).fetchone()
                if previous:
                    saved = json.loads(previous["record"])
                    if (previous["agent_id"], previous["user_id"], previous["session_id"]) != (*self.scope, session):
                        raise MemoryError("invalid_evidence", "原话身份不属于当前会话")
                    if saved["content"] != message.get("content", ""):
                        if saved.get("role") == "user":
                            raise MemoryError("invalid_evidence", "同一原话身份不能修改正文")
                        # 裁剪/显示层可以改消息，但同一原话身份不能改正文。
                        message["message_id"] = message_id
                        extras["memory_trace"] = saved["extras"]["memory_trace"]
                        continue
                    extras["memory_trace"] = saved["extras"]["memory_trace"]
                    message["message_id"] = message_id
                    continue
                kind = origin or trace.get("origin") or ("assistant" if message.get("role") == "assistant" else "internal")
                if message.get("role") == "tool" or (isinstance(message.get("content"), list) and any(
                        b.get("type") == "tool_result" for b in message["content"] if isinstance(b, dict))):
                    kind = "tool"
                # 只有已由会话留档入口盖章的 human 才能继续以真人身份使用。
                if kind == "human" and origin != "human":
                    row = db.execute("SELECT role,content,extras FROM messages WHERE agent_id=? AND session_id=? AND json_valid(extras) AND json_extract(extras,'$.memory_trace.message_id')=?",
                                     (self.agent_id, session, message_id)).fetchone()
                    if not row:
                        kind = "internal"
                    else:
                        stored_trace = (json.loads(row["extras"]).get("memory_trace") or {})
                        if (stored_trace.get("origin") != "human" or row["role"] != "user"
                                or json.loads(row["content"]) != message.get("content", "")
                                or (stored_trace.get("user_id") or "") != self.user_id
                                or stored_trace.get("schema_version") != 1
                                or (stored_trace.get("workspace_id") and stored_trace["workspace_id"] != self.workspace_id)):
                            raise MemoryError("invalid_evidence", "原话与受信留档的正文、身份或来源不一致")
                        # 用途约束来自受信留档；调用方删除 extras 不能解除临时用途。
                        stored_extras = json.loads(row["extras"])
                        for key in ("purpose", "archive_only", "personal_task_id", "metadata"):
                            if key in stored_extras:
                                extras[key] = copy.deepcopy(stored_extras[key])
                        trace = copy.deepcopy(stored_trace)
                trace.update(schema_version=1, message_id=message_id, workspace_id=self.workspace_id,
                             agent_id=self.agent_id, user_id=self.user_id, session_id=session, origin=kind)
                if current_task_id():
                    trace["task_id"] = current_task_id()
                    extras["archive_only"] = True
                extras["memory_trace"] = trace
                message["message_id"] = message_id
                db.execute("INSERT INTO memory_evidence VALUES (?,?,?,?,?)",
                           (message_id, *self.scope, session, _json(copy.deepcopy(message))))
        return messages

    def evidence(self, messages):
        self.capture(messages)
        result = []
        for message in messages:
            trace = (message.get("extras") or {}).get("memory_trace") or {}
            content = message.get("content", "")
            blocks = [(None, content)] if isinstance(content, str) else [
                (i, b["text"]) for i, b in enumerate(content) if isinstance(b, dict) and b.get("type") == "text"]
            for index, text in blocks:
                if text:
                    result.append(dict(message_id=trace.get("message_id"), block_index=index,
                                       start=0, end=len(text), sha256=_hash(text), text=text,
                                       origin=trace.get("origin")))
        return result

    def _entry(self, db, entry_id):
        row = db.execute("SELECT * FROM memory_entries WHERE entry_id=? AND agent_id=? AND user_id=?",
                         (entry_id, *self.scope)).fetchone()
        if not row:
            raise MemoryError("invalid_evidence", "记忆条目不属于当前身份")
        return json.loads(row["record"])

    def _validate_basis(self, db, basis, disposition):
        if not isinstance(basis, list) or not basis:
            raise MemoryError("invalid_evidence", "依据不能为空")
        inputs = []
        for ref in basis:
            if not isinstance(ref, dict):
                raise MemoryError("invalid_evidence")
            if "entry_id" in ref:
                entry = self._entry(db, ref["entry_id"])
                if entry["state"] != "active" or entry["revision"] != ref.get("revision"):
                    raise MemoryError("version_conflict", "引用的记忆修订已失效")
                inputs.append(entry["entry_id"])
                continue
            row = db.execute("SELECT * FROM memory_evidence WHERE message_id=?", (ref.get("message_id"),)).fetchone()
            if not row or (row["agent_id"], row["user_id"], row["session_id"]) != (*self.scope, self.session_id):
                raise MemoryError("invalid_evidence", "原话不存在或归属不符")
            message = json.loads(row["record"])
            content = message["content"]
            index = ref.get("block_index")
            if isinstance(content, str) and index is None:
                text = content
            elif isinstance(content, list) and type(index) is int and 0 <= index < len(content):
                block = content[index]
                if not isinstance(block, dict) or block.get("type") != "text":
                    raise MemoryError("invalid_evidence")
                text = block["text"]
            else:
                raise MemoryError("invalid_evidence", "原始文本块位置无效")
            start, end = ref.get("start"), ref.get("end")
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text) or ref.get("sha256") != _hash(text):
                raise MemoryError("invalid_evidence", "原话区间或 SHA-256 不匹配")
            extras = message.get("extras") or {}
            trace = extras.get("memory_trace") or {}
            metadata = message.get("metadata") or extras.get("metadata") or {}
            if disposition == "conversation_memory":
                if message.get("role") != "user" or trace.get("origin") != "human":
                    raise MemoryError("invalid_evidence", "个人记忆必须引用真人原话或有效条目")
                if (extras.get("archive_only") or extras.get("personal_task_id") or trace.get("task_id")
                        or message.get("personal_task_id") or metadata.get("personal_task_id")
                        or message.get("purpose") in ("transient", "waiting", "archive_only")
                        or extras.get("purpose") in ("transient", "waiting", "archive_only")
                        or metadata.get("purpose") in ("transient", "waiting", "archive_only")
                        or "[personal-task:" in text):
                    raise MemoryError("purpose_conflict", "测试或临时原话只留档")
                fragment = text[start:end]
                if any(s in fragment for s in ("source_reference", "knowledge/managed/", "knowledge/wiki/")):
                    raise MemoryError("purpose_conflict", "来源结论需 knowledge_ingest")
        return inputs

    def record(self, request_id, entries, *, trusted=False, expected_revision=None):
        if not isinstance(request_id, str) or not request_id.strip() or not isinstance(entries, list) or not entries:
            raise MemoryError("invalid_evidence", "request_id 与非空 entries 必填")
        payload = _json(entries)
        key = (*self.scope, self.session_id, request_id)
        with self._transaction() as db:
            self._check_schema(db)
            previous = db.execute("SELECT * FROM memory_operations WHERE agent_id=? AND user_id=? AND session_id=? AND request_id=?", key).fetchone()
            if previous:
                if previous["payload"] != payload:
                    return {"request_id": request_id, "results": [{"status": "failed", "error": "idempotency_conflict"}], "revision": self._revision(db)}
                receipt = json.loads(previous["record"])
                retry_stale = expected_revision is not None and expected_revision != self._revision(db)
                for index, result in enumerate(receipt["results"]):
                    if result.get("status") != "failed":
                        continue
                    db.execute("SAVEPOINT retry_candidate")
                    try:
                        if retry_stale:
                            raise MemoryError("version_conflict")
                        receipt["results"][index] = self._record_one(db, entries[index], trusted, request_id)
                        db.execute("RELEASE retry_candidate")
                    except (MemoryError, KeyError, TypeError, ValueError) as exc:
                        db.execute("ROLLBACK TO retry_candidate")
                        db.execute("RELEASE retry_candidate")
                        receipt["results"][index] = dict(index=index, status="failed", error=getattr(exc, "code", "invalid_evidence"), reason=str(exc))
                receipt["revision"] = self._revision(db)
                db.execute("UPDATE memory_operations SET record=? WHERE agent_id=? AND user_id=? AND session_id=? AND request_id=?",
                           (_json(receipt), *key))
            else:
                results = []
                stale = expected_revision is not None and expected_revision != self._revision(db)
                for index, proposal in enumerate(entries):
                    db.execute("SAVEPOINT candidate")
                    try:
                        if stale:
                            raise MemoryError("version_conflict", "记忆已变更，请重新核验依据")
                        result = self._record_one(db, proposal, trusted, request_id)
                        results.append(result)
                        db.execute("RELEASE candidate")
                    except (MemoryError, KeyError, TypeError, ValueError) as exc:
                        db.execute("ROLLBACK TO candidate")
                        db.execute("RELEASE candidate")
                        results.append(dict(index=index, status="failed", error=getattr(exc, "code", "invalid_evidence"), reason=str(exc)))
                receipt = dict(request_id=request_id, results=results, revision=self._revision(db))
                # 全失败不占用重试身份；部分成功的原操作完整保留。
                if any(r["status"] != "failed" for r in results):
                    db.execute("INSERT INTO memory_operations VALUES (?,?,?,?,?,?)", (*key, payload, _json(receipt)))
        projected = self.project()
        for result in receipt["results"]:
            if result.get("entry_id"):
                with self._transaction() as db:
                    entry = self._entry(db, result["entry_id"])
                result["status"] = ("remembered" if projected else "pending_projection") if entry["state"] == "active" else "archive_only"
            result["searchable"] = False
        return receipt

    def _record_one(self, db, proposal, trusted, operation):
        allowed = {"candidate_id", "content", "disposition", "basis", "reason", "supersedes", "legacy_supersedes"}
        if not isinstance(proposal, dict) or set(proposal) - allowed:
            raise MemoryError("invalid_evidence", "不接受调用方指定身份、文件或批准状态")
        disposition = proposal["disposition"]
        if disposition not in DISPOSITIONS or not isinstance(proposal["content"], str) or not proposal["content"].strip() or not proposal.get("reason"):
            raise MemoryError("invalid_evidence")
        inputs = self._validate_basis(db, proposal["basis"], disposition)
        if trusted:
            self._check_span_purpose(db, proposal["basis"], disposition)
        if trusted and disposition == "conversation_memory" and proposal.get("supersedes"):
            replacing = set(proposal["supersedes"])
            pending = list(inputs)
            checked = set()
            while pending:
                dependency = pending.pop()
                if dependency in replacing:
                    raise MemoryError("purpose_conflict", "更正不能依赖将被替代的旧条目；请仅引用新的真实原话，并以 supersedes 指明旧条目")
                if dependency not in checked:
                    checked.add(dependency)
                    pending.extend(self._entry(db, dependency).get("inputs", []))
        if not trusted:
            # 背景候选不能用新的依据组合继承一个尚未批准的用途。
            if proposal.get("supersedes") or proposal.get("legacy_supersedes"):
                raise MemoryError("purpose_conflict", "更正必须由受信主 Agent 判定")
        fact_key = _hash(_json(proposal["basis"]))
        row = db.execute("SELECT record FROM memory_candidates WHERE agent_id=? AND user_id=? AND session_id=? AND fact_key=?",
                         (*self.scope, self.session_id, fact_key)).fetchone()
        candidate = json.loads(row[0]) if row else dict(candidate_id=str(uuid.uuid4()), basis=proposal["basis"], decision_revision=0)
        if proposal.get("candidate_id") and proposal["candidate_id"] != candidate["candidate_id"]:
            raise MemoryError("invalid_evidence", "候选身份与原话事实不符")
        cid = candidate["candidate_id"]
        decision = candidate.get("decision")
        if (row and decision and disposition == "conversation_memory"
                and proposal["content"] != candidate.get("content") and not proposal.get("candidate_id")):
            pending = candidate.setdefault("pending_proposals", [])
            if proposal not in pending:
                pending.append(proposal)
            db.execute("UPDATE memory_candidates SET record=? WHERE candidate_id=?", (_json(candidate), cid))
            return dict(candidate_id=cid, decision_revision=candidate["decision_revision"], status="undecided",
                        reason="同一片段存在另一事实表述；请沿用候选身份确认改写，或缩小片段区分独立事实")
        if decision and decision.get("actor") == "correction" and disposition == "conversation_memory":
            raise MemoryError("purpose_conflict", "原事实已被更正或排除；请引用新的真实原话")
        if trusted:
            if decision and decision["disposition"] in ("archive_only", "sourced_knowledge") and disposition == "conversation_memory":
                raise MemoryError("purpose_conflict", "既有受限片段不能直接重标；请引用新的独立原话")
            rewrite = bool(proposal.get("candidate_id") and proposal["content"] != candidate.get("content"))
            if not decision or decision["disposition"] != disposition or rewrite:
                candidate["decision_revision"] += 1
                decision = dict(decision_id=str(uuid.uuid4()), decision_revision=candidate["decision_revision"],
                                disposition=disposition, basis=proposal["basis"], actor="main_agent")
                db.execute("INSERT INTO memory_decisions VALUES (?,?,?,?)", (decision["decision_id"], cid, candidate["decision_revision"], _json(decision)))
                self._bump(db)
                if candidate.get("entry_id"):
                    self._invalidate(db, candidate["entry_id"], "excluded", update_candidate=False)
                    candidate.pop("entry_id", None)
                candidate["decision"] = decision
                candidate.pop("pending_proposals", None)
        effective = disposition if decision and decision["disposition"] == disposition else "undecided"
        # 待处理不自报批准；后续主 Agent 可引用原候选完成判定。
        candidate.update(content=proposal["content"], reason=proposal["reason"], disposition=effective)
        if effective == "conversation_memory":
            if not candidate.get("entry_id"):
                entry_id = str(uuid.uuid4())
                revision = self._bump(db)
                entry = dict(schema_version=1, entry_id=entry_id, candidate_id=cid, content=proposal["content"],
                             basis=proposal["basis"], decision=decision, revision=revision, state="active",
                             inputs=inputs, candidate_ids=[cid], operation_ids=[dict(session_id=self.session_id, request_id=operation)])
                for input_id in inputs:
                    source = self._entry(db, input_id)
                    entry["candidate_ids"] += source.get("candidate_ids", [])
                    entry["operation_ids"] += source.get("operation_ids", [])
                db.execute("INSERT INTO memory_entries VALUES (?,?,?,?,?,?)", (entry_id, *self.scope, revision, "active", _json(entry)))
                candidate["entry_id"] = entry_id
            if trusted:
                for old in proposal.get("supersedes", []):
                    if old != candidate["entry_id"]:
                        self._invalidate(db, old, "superseded")
                self._exclude_legacy(db, proposal.get("legacy_supersedes", []))
        db.execute("INSERT OR REPLACE INTO memory_candidates VALUES (?,?,?,?,?,?)", (cid, *self.scope, self.session_id, fact_key, _json(candidate)))
        for ref in proposal["basis"]:
            if "message_id" in ref:
                row = db.execute("SELECT record FROM memory_evidence WHERE message_id=?", (ref["message_id"],)).fetchone()
                message = json.loads(row[0])
                trace = message["extras"]["memory_trace"]
                trace.setdefault("candidates", {})[cid] = decision
                db.execute("UPDATE memory_evidence SET record=? WHERE message_id=?", (_json(message), ref["message_id"]))
                db.execute("UPDATE messages SET extras=json_set(CASE WHEN extras='' THEN '{}' ELSE extras END,'$.memory_trace',json(?)) WHERE agent_id=? AND session_id=? AND json_extract(extras,'$.memory_trace.message_id')=?",
                           (_json(trace), self.agent_id, self.session_id, ref["message_id"]))
        result = dict(candidate_id=cid, decision_revision=candidate["decision_revision"],
                      status={"conversation_memory": "pending_projection", "sourced_knowledge": "requires_knowledge_ingest", "archive_only": "archive_only", "undecided": "undecided"}[effective])
        if candidate.get("entry_id"):
            result["entry_id"] = candidate["entry_id"]
        return result

    @staticmethod
    def _overlapping_basis(left, right):
        return any(a.get("message_id") and all(a.get(key) == b.get(key) for key in ("message_id", "block_index", "sha256"))
                   and max(a["start"], b["start"]) < min(a["end"], b["end"])
                   for a in left for b in right if "message_id" in a and "message_id" in b)

    def _check_span_purpose(self, db, basis, disposition):
        restricted = {"archive_only", "sourced_knowledge"}
        if disposition != "conversation_memory" and disposition not in restricted:
            return
        rows = db.execute("SELECT record FROM memory_candidates WHERE agent_id=? AND user_id=? AND session_id=?",
                          (*self.scope, self.session_id)).fetchall()
        for row in rows:
            candidate = json.loads(row[0])
            decision = candidate.get("decision") or {}
            previous = decision.get("disposition")
            if not self._overlapping_basis(basis, decision.get("basis") or candidate["basis"]):
                continue
            if disposition == "conversation_memory" and previous in restricted:
                raise MemoryError("purpose_conflict", "原话片段已受测试／临时或来源用途限制；缩小区间不能解除限制，请引用新的独立原话")
            if disposition in restricted and previous == "conversation_memory" and candidate.get("entry_id"):
                self._invalidate(db, candidate["entry_id"], "excluded")

    def _invalidate(self, db, entry_id, state, seen=None, update_candidate=True):
        seen = seen if seen is not None else set()
        if entry_id in seen:
            return
        seen.add(entry_id)
        entry = self._entry(db, entry_id)
        if entry["state"] != "active":
            return
        db.execute("INSERT INTO memory_history(entry_id,record) VALUES (?,?)", (entry_id, _json(entry)))
        entry.update(state=state, revision=self._bump(db))
        db.execute("UPDATE memory_entries SET state=?,revision=?,record=? WHERE entry_id=?", (state, entry["revision"], _json(entry), entry_id))
        # 候选保留排除判定，重启或再次提炼不能复活同一事实。
        cid = entry.get("candidate_id")
        if cid and update_candidate:
            row = db.execute("SELECT record FROM memory_candidates WHERE candidate_id=?", (cid,)).fetchone()
            if row:
                candidate = json.loads(row[0])
                candidate["decision_revision"] += 1
                candidate["decision"] = dict(decision_id=str(uuid.uuid4()), decision_revision=candidate["decision_revision"], disposition="archive_only", basis=candidate["basis"], actor="correction")
                candidate["disposition"] = "archive_only"
                db.execute("INSERT INTO memory_decisions VALUES (?,?,?,?)", (candidate["decision"]["decision_id"], cid, candidate["decision_revision"], _json(candidate["decision"])))
                db.execute("UPDATE memory_candidates SET record=? WHERE candidate_id=?", (_json(candidate), cid))
        rows = db.execute("SELECT record FROM memory_entries WHERE agent_id=? AND user_id=? AND state='active'", self.scope).fetchall()
        for row in rows:
            child = json.loads(row[0])
            if entry_id in child.get("inputs", []):
                self._invalidate(db, child["entry_id"], "excluded", seen)

    def exclude(self, entry_id, expected_revision=None):
        with self._transaction() as db:
            self._check_schema(db)
            if expected_revision is not None and expected_revision != self._revision(db):
                raise MemoryError("version_conflict")
            self._invalidate(db, entry_id, "excluded")
        return dict(revision=self.revision(), projected=self.project())

    def active_entries(self):
        with self._transaction() as db:
            self._check_schema(db)
            return self._current_entries(db)

    def _current_entries(self, db):
        entries = [json.loads(r[0]) for r in db.execute("SELECT record FROM memory_entries WHERE agent_id=? AND user_id=? AND state='active' ORDER BY rowid", self.scope)]
        # 输入保留原有依据修订供重试/失效传播，读取只显示尚未被整理覆盖的末端。
        merged = {eid for entry in entries for eid in entry.get("inputs", [])}
        return [entry for entry in entries if entry["entry_id"] not in merged]

    def pending_candidates(self):
        """主 Agent 可处理裁剪前留下的候选，连同必要的原话快照。"""
        with self._transaction() as db:
            result = []
            for row in db.execute("SELECT record FROM memory_candidates WHERE agent_id=? AND user_id=? AND session_id=?", (*self.scope, self.session_id)):
                candidate = json.loads(row[0])
                if candidate["disposition"] != "undecided" and not candidate.get("pending_proposals"):
                    continue
                records = []
                for ref in candidate["basis"]:
                    if "message_id" in ref:
                        saved = db.execute("SELECT record FROM memory_evidence WHERE message_id=?", (ref["message_id"],)).fetchone()
                        if saved:
                            records.append(json.loads(saved[0]))
                proposals = candidate.get("pending_proposals") or [candidate]
                for pending in proposals:
                    item = dict(pending, candidate_id=candidate["candidate_id"], disposition="undecided")
                    result.append(dict(candidate=item, evidence=records))
            return result

    def merge(self, request_id, content, input_ids, expected_revision):
        with self._transaction() as db:
            inputs = [self._entry(db, eid) for eid in input_ids]
        basis = [dict(entry_id=e["entry_id"], revision=e["revision"]) for e in inputs]
        if not basis:
            raise MemoryError("invalid_evidence")
        # 派生归类沿用全部已准入输入，不允许以 Dream 自报批准。
        return self.record(request_id, [dict(content=content, disposition="conversation_memory", basis=basis,
                                             reason="整理已准入记忆，保留所有输入")], trusted=True, expected_revision=expected_revision)

    def _relative(self, path):
        target = Path(path)
        if not target.is_absolute():
            target = self.workspace / target
        target = target.resolve()
        try:
            rel = target.relative_to(self.workspace).as_posix()
        except ValueError as exc:
            raise MemoryError("invalid_evidence", "记忆路径超出工作区") from exc
        return target, rel

    def _projection_path(self):
        # 用户身份不参与磁盘路径拼接，避免路径注入和不同用户覆盖。
        suffix = "" if not self.user_id else "-" + _hash(self.user_id)[:20]
        return self.workspace / "memory" / ("routed" + suffix + ".md")

    def _strict_replace(self, path, data, baseline):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".cow-memory-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            actual = _hash(path.read_bytes()) if path.exists() else None
            if actual != baseline:
                raise MemoryError("version_conflict", "记忆文件已被外部修改，保留原文件")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _render(self, db):
        revision = self._revision(db)
        metadata = dict(schema_version=1, workspace_id=self.workspace_id, revision=revision)
        chunks = ["<!-- cow-memory " + _json(metadata) + " -->\n"]
        entries = self._current_entries(db)
        for entry in entries:
            block = dict(entry_id=entry["entry_id"], revision=entry["revision"], sha256=_hash(entry["content"]),
                         candidate_ids=entry.get("candidate_ids", []), operation_ids=entry.get("operation_ids", []))
            chunks.append("<!-- cow-entry " + _json(block) + " -->\n" + entry["content"] + "\n<!-- /cow-entry -->\n")
        return "\n".join(chunks), [entry["entry_id"] for entry in entries]

    def project(self):
        try:
            # 替换前先持久化生成意图。中断后即使权威修订继续前进，仍能识别自己的旧生成物。
            with self._transaction() as db:
                self._check_schema(db)
                _, rel = self._relative(self._projection_path())
                row = db.execute("SELECT * FROM memory_files WHERE agent_id=? AND user_id=? AND path=?", (*self.scope, rel)).fetchone()
                if not row or json.loads(row["record"]).get("kind") == "projection":
                    record = json.loads(row["record"]) if row else dict(kind="projection")
                    text, ids = self._render(db)
                    pending = record.setdefault("pending_hashes", [])
                    if _hash(text) not in pending:
                        pending.append(_hash(text))
                    record["planned_entry_ids"] = ids
                    db.execute("INSERT OR REPLACE INTO memory_files VALUES (?,?,?,?,?)",
                               (*self.scope, rel, row["hash"] if row else "", _json(record)))
            # 跨实例/进程共享 SQLite 写屏障，检查、原子替换与投影回执串行。
            with self._transaction() as db:
                self._check_schema(db)
                for pending in db.execute("SELECT * FROM memory_files WHERE agent_id=? AND user_id=?", self.scope).fetchall():
                    record = json.loads(pending["record"])
                    if record.get("kind") != "human_edit" or not record.get("pending"):
                        continue
                    target, _ = self._relative(pending["path"])
                    actual = _hash(target.read_bytes()) if target.exists() else None
                    if actual != pending["hash"]:
                        clean = "\n\n".join(self._entry(db, eid)["content"] for eid in record["entry_ids"]
                                            if self._entry(db, eid)["state"] == "active")
                        self._strict_replace(target, clean.encode("utf-8"), record["previous_hash"])
                    record["pending"] = False
                    db.execute("UPDATE memory_files SET record=? WHERE agent_id=? AND user_id=? AND path=?",
                               (_json(record), *self.scope, pending["path"]))
                path, rel = self._relative(self._projection_path())
                row = db.execute("SELECT * FROM memory_files WHERE agent_id=? AND user_id=? AND path=?", (*self.scope, rel)).fetchone()
                baseline = _hash(path.read_bytes()) if path.exists() else None
                revision = self._revision(db)
                text, ids = self._render(db)
                pending_hashes = json.loads(row["record"]).get("pending_hashes", []) if row else []
                # 文件替换后、投影回执提交前中断：精确匹配权威生成物即可对账。
                if baseline not in pending_hashes and baseline != _hash(text) and ((row and baseline not in (None, row["hash"])) or (not row and baseline is not None)):
                    return False
                self._strict_replace(path, text.encode("utf-8"), baseline)
                db.execute("INSERT OR REPLACE INTO memory_files VALUES (?,?,?,?,?)", (*self.scope, rel, _hash(text), _json(dict(revision=revision, kind="projection", entry_ids=ids))))
            return True
        except (OSError, sqlite3.DatabaseError, MemoryError):
            return False

    def filter_text(self, path, text):
        """读投影时重新核权威状态；旧内容不经此路径升级成新条目。"""
        try:
            target, rel = self._relative(path)
            with self._transaction() as db:
                self._check_schema(db)
                row = db.execute("SELECT * FROM memory_files WHERE agent_id=? AND user_id=? AND path=?", (*self.scope, rel)).fetchone()
                matches = bool(row and row["hash"] == _hash(text))
                if row and not matches and target.exists():
                    raw = target.read_bytes()
                    matches = (_hash(raw) == row["hash"] and
                               raw.decode("utf-8").replace("\r\n", "\n") == text.replace("\r\n", "\n"))
                    if matches:
                        text = raw.decode("utf-8")
                if "<!-- cow-memory" in text or "<!-- cow-entry" in text:
                    if not matches:
                        return ""
                    clean = []
                    for match in _BLOCK.finditer(text):
                        meta = json.loads(match[1])
                        entry = self._entry(db, meta["entry_id"])
                        if entry["state"] == "active" and entry["revision"] == meta["revision"] and _hash(match[2]) == meta["sha256"]:
                            clean.append(entry["content"])
                    return "\n\n".join(clean)
                if row and json.loads(row["record"]).get("kind") == "human_edit":
                    if not matches:
                        return ""
                    ids = json.loads(row["record"]).get("entry_ids", [])
                    return "\n\n".join(self._entry(db, eid)["content"] for eid in ids if self._entry(db, eid)["state"] == "active")
                if row:
                    return ""
                # 旧任务标记仍排除；legacy 只维持既有读取能力。
                if "[personal-task:" in text:
                    return ""
                for excluded in db.execute("SELECT * FROM memory_legacy_exclusions WHERE agent_id=? AND user_id=? AND path=?", (*self.scope, rel)):
                    text = text.replace(excluded["text"], "")
                    text = text.replace(excluded["text"].replace("\r\n", "\n").rstrip("\r"), "")
                return text
        except (MemoryError, sqlite3.DatabaseError, ValueError, KeyError):
            return ""

    def file_status(self, path, raw):
        """供人工编辑器诊断；不将外部正文反向导入台账。"""
        _, rel = self._relative(path)
        with self._transaction() as db:
            self._check_schema(db)
            row = db.execute("SELECT hash FROM memory_files WHERE agent_id=? AND user_id=? AND path=?", (*self.scope, rel)).fetchone()
            if row:
                return "current" if row[0] == _hash(raw) else "unverified"
            return "unverified" if b"<!-- cow-" in raw else "legacy"

    def _exclude_legacy(self, db, refs):
        for ref in refs:
            target, rel = self._relative(ref["path"])
            try:
                text = target.read_bytes().decode("utf-8")
            except FileNotFoundError as exc:
                raise MemoryError("invalid_evidence", "被更正的旧记忆文件不存在，请重新核对引用") from exc
            except OSError as exc:
                raise MemoryError("storage_failed", "无法读取被更正的旧记忆文件，请重试") from exc
            start, end = ref["start"], ref["end"]
            if _hash(text) != ref["sha256"] or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
                raise MemoryError("version_conflict", "旧记忆定位已变化")
            db.execute("INSERT INTO memory_legacy_exclusions VALUES (?,?,?,?,?,?,?)", (*self.scope, rel, ref["sha256"], start, end, text[start:end]))
            self._bump(db)

    def human_edit(self, path, content, expected_hash=None):
        target, rel = self._relative(path)
        if target.name != "MEMORY.md" and not rel.startswith("memory/"):
            raise MemoryError("invalid_evidence", "不是记忆目标")
        with self._transaction() as db:
            self._check_schema(db)
            baseline = _hash(target.read_bytes()) if target.exists() else None
            if expected_hash is not None and baseline != expected_hash:
                raise MemoryError("version_conflict", "记忆已被修改，请重新加载")
            # 旧受控块中的条目都失效；人类新文本具有独立 human_edit 依据。
            old_text = target.read_text(encoding="utf-8") if target.exists() else ""
            if old_text:
                db.execute("INSERT INTO memory_history(entry_id,record) VALUES (?,?)",
                           ("human_edit:" + rel, _json(dict(path=rel, content=old_text, sha256=baseline))))
            old_row = db.execute("SELECT record FROM memory_files WHERE agent_id=? AND user_id=? AND path=?", (*self.scope, rel)).fetchone()
            if old_row:
                previous = json.loads(old_row[0])
                if previous.get("kind") == "projection":
                    visible_ids = previous.get("entry_ids", [])
                    current_ids = [entry["entry_id"] for entry in self._current_entries(db)]
                    if visible_ids != current_ids:
                        raise MemoryError("version_conflict", "权威记忆已更新且尚未完整显示，请先完成投影并重新加载后编辑")
                elif previous.get("pending"):
                    raise MemoryError("version_conflict", "上次人工修订尚未投影，请先完成投影并重新加载")
            old_ids = [json.loads(m[1])["entry_id"] for m in _BLOCK.finditer(old_text)]
            if old_row:
                old_ids += json.loads(old_row[0]).get("entry_ids", [])
                old_ids += json.loads(old_row[0]).get("planned_entry_ids", [])
            if "<!-- cow-" in old_text and not old_ids:
                raise MemoryError("unsupported_schema", "旧记忆标记损坏，保留原文件")
            # 人工替代一个合并块时，其原输入也必须退出当前使用。
            stack = list(old_ids)
            while stack:
                parent = self._entry(db, stack.pop())
                for input_id in parent.get("inputs", []):
                    if input_id not in old_ids:
                        old_ids.append(input_id)
                        stack.append(input_id)
            for eid in set(old_ids):
                self._invalidate(db, eid, "superseded")
            clean = _BLOCK.sub(lambda m: m[2], content)
            clean = re.sub(r"<!-- cow-memory [^\n]* -->\n?", "", clean)
            if "<!-- cow-" in clean:
                raise MemoryError("unsupported_schema", "记忆标记损坏，保留原文件")
            revision = self._bump(db)
            eid = str(uuid.uuid4())
            entry = dict(schema_version=1, entry_id=eid, content=clean, basis=[dict(type="human_edit", path=rel, previous_sha256=baseline)],
                         revision=revision, state="active", inputs=[], candidate_ids=[], operation_ids=[])
            db.execute("INSERT INTO memory_entries VALUES (?,?,?,?,?,?)", (eid, *self.scope, revision, "active", _json(entry)))
            db.execute("INSERT OR REPLACE INTO memory_files VALUES (?,?,?,?,?)", (*self.scope, rel, _hash(clean),
                       _json(dict(revision=revision, kind="human_edit", entry_ids=[eid], pending=True, previous_hash=baseline))))
        # 权威提交成功才更新文件；掉电或投影失败可由 project 重建。
        projected = self.project()
        return dict(revision=revision, entry_id=eid, projected=projected,
                    status="remembered" if projected else "pending_projection", searchable=False)
