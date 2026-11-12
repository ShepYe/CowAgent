"""版本化阅读凭据；TaskStore 屏障授权，来源 SQLite 提交观察和别名。

模型输入只进入 resolve_evidence。prepare/生命周期/resume 是受信运行时 API。
支持 PDF 及有可信 Office 转换关联的阅读 PDF；不发起模型请求。
"""
import copy
import json
import re
from contextlib import contextmanager
from uuid import uuid4

from agent.knowledge.parser import SourceError
from agent.knowledge.personal_assistant import SourceService, _hash, _json
from agent.knowledge.reading_materials import PreparedPdf, load_pdf_materials, replan_pdf_segment, retry_pdf_segment, _relative
from common.atomic_write import write_text_atomic


META = {"parse_id", "document_id", "revision_id", "sha256"}
EVIDENCE_FIELDS = {"reading_id", "evidence_key", "segment_id", "location", "observation", "type"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def reject(code="invalid_request", message="阅读证据校验失败"):
    raise SourceError(code, message)


def text(value):
    if not isinstance(value, str) or not value.strip():
        reject()
    return value


def _read(db, reading_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reading_records'").fetchone():
        reject("storage_failed", "缺少阅读台账")
    row = db.execute("SELECT record FROM reading_records WHERE id=?", (text(reading_id),)).fetchone()
    if not row:
        reject("invalid_request", "阅读记录不存在")
    record = json.loads(row[0])
    if record.get("schema_version") != 1:
        reject("version_conflict", "未知阅读记录版本")
    return record


def _save(db, record):
    db.execute("UPDATE reading_records SET record=? WHERE id=?", (canonical(record), record["reading_id"]))


def _materials(workspace, record):
    materials = load_pdf_materials(workspace, record["manifest"], expected_manifest_hash=record["manifest_hash"])
    if "segments" in record:
        expected = [dict(segment_id=s.segment_id, page_map=list(s.page_map), target_pages=list(s.target_pages))
                    for s in materials.segments]
        actual = [{k: s[k] for k in ("segment_id", "page_map", "target_pages")} for s in record["segments"]]
        if canonical(actual) != canonical(expected):
            reject("storage_failed", "阅读记录与不可变材料位置映射不符")
    return materials


def _source_hash(record):
    manifest = record["manifest"]
    derived = manifest.get("derived_source")
    return manifest["original_hash"] if derived is None else derived["original_hash"]


def _validate_location(material, segment, location):
    if not isinstance(location, dict):
        reject("invalid_request", "观察位置须为对象")
    # canonical 区分 true/1；只接受程序映射出的完整源位置，不信任自报页码。
    encoded = canonical(location)
    if not any(encoded == canonical(material.location(p)) for p in segment["page_map"]):
        reject("invalid_request", "观察位置超出成功送达的源范围")


def _office_details(material, pages):
    derived = material.derived_source
    office = derived["office_manifest"]
    return dict(office_manifest_hash=derived["office_manifest_hash"],
                source_page_map=[material.location(p) for p in pages],
                source_format=office["source_format"], notes=copy.deepcopy(office["notes"]),
                coverage_gaps=copy.deepcopy(office["coverage_gaps"]))


def _verify_model_products(db, record):
    for segment in record.get("segments", []):
        if segment.get("full_response_saved"):
            response = dict(id=segment["response_id"], output=segment["saved_model_output"],
                            usage=segment.get("usage"), status="completed")
            if _hash(canonical(response).encode()) != segment.get("response_hash"):
                reject("storage_failed", "模型响应检查点哈希不符")
    if record.get("reused_from"):
        origin = _read(db, record["reused_from"]["reading_id"])
        expected = {k: origin[k] for k in ("reading_id", "task_id", "attempt", "model_identity")}
        if (record["reused_from"] != expected or origin.get("reused_from")
                or origin["task_id"] == record["task_id"]
                or any(origin[k] != record[k] for k in ("owner_id", "document_id", "revision_id", "base_parse_id",
                                                      "manifest_hash", "config_revision", "model_identity"))):
            reject("storage_failed", "跨任务缓存来源关联不符")
        for segment in record["segments"]:
            original = next((s for s in origin["segments"] if s["segment_id"] == segment["segment_id"]), None)
            if (not original or original["state"] != "completed" or not original.get("full_response_saved")
                    or segment.get("response_hash") != original.get("response_hash")
                    or segment.get("saved_model_output") != original.get("saved_model_output")
                    or segment.get("response_id") != original.get("response_id")
                    or segment.get("usage") != original.get("usage")):
                reject("storage_failed", "缓存模型产物与原产物不符")


def verify_reading_unit(db, service, unit, ref):
    """Wiki 来源守卫调用；不借用当前 task/attempt，已发布知识可独立核验。"""
    proof = unit["reading_provenance"]
    record = _read(db, proof["reading_id"])
    material = _materials(service.workspace, record)
    _verify_model_products(db, record)
    if (record["document_id"] != ref["document_id"] or record["revision_id"] != ref["revision_id"]
            or material.manifest_hash != proof["manifest_hash"]):
        reject("storage_failed")
    row = db.execute("SELECT record FROM reading_aliases WHERE scope=?", (proof["alias_scope"],)).fetchone()
    if not row:
        reject("storage_failed", "缺少已提交观察别名")
    alias = json.loads(row[0])
    evidence = alias["payload"]["evidence"]
    segment = next((s for s in record["segments"] if s["segment_id"] == evidence["segment_id"]), None)
    expected_ref = {k: ref[k] for k in ("document_id", "revision_id", "parse_id", "ordinal")}
    # 追加解析版本保留旧 ordinal，旧单元也可以经新 parse 回查。
    same_unit = all(alias["ref"][k] == expected_ref[k] for k in ("document_id", "revision_id", "ordinal"))
    if (not same_unit or segment is None or segment["state"] != "completed"
            or not segment.get("delivery_receipt_id") or unit["type"] != evidence["type"]
            or unit["origin"] != "model" or unit["text"] != evidence["observation"]
            or unit["location"] != dict(evidence["location"], type=evidence["type"])
            or unit["model_identity"] != record["model_identity"]):
        reject("storage_failed")
    source_segment = next(s for s in material.segments if s.segment_id == segment["segment_id"])
    _validate_location(material, segment, evidence["location"])
    result = dict(type=unit["type"], origin="model", model_identity=unit["model_identity"],
                  reading_id=record["reading_id"], reading_copy=dict(
                      path=source_segment.material_relpath, sha256=source_segment.material_hash,
                      page_map=list(source_segment.page_map), manifest_hash=material.manifest_hash))
    if material.derived_source is not None:
        result["reading_copy"].update(_office_details(material, source_segment.page_map))
    if material.region_supplement is not None:
        result["reading_copy"]["region"] = copy.deepcopy(material.region_supplement["region"])
        result["reading_copy"]["supplemental"] = True
    if record.get("reused_from"):
        result["reused_from"] = copy.deepcopy(record["reused_from"])
    return result


class ReadingService:
    def __init__(self, workspace_root, *, task_store):
        self.source = SourceService(workspace_root)
        self.workspace = self.source.workspace
        self.store = task_store
        with self.source.transaction() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reading_records'").fetchone():
                for row in db.execute("SELECT record FROM reading_records").fetchall():
                    try:
                        version = json.loads(row[0]).get("schema_version")
                    except (ValueError, AttributeError) as exc:
                        raise SourceError("version_conflict", "非法阅读记录，禁止写入") from exc
                    if version != 1:
                        reject("version_conflict", "未知阅读记录版本，禁止写入")
            db.execute("CREATE TABLE IF NOT EXISTS reading_records(id TEXT PRIMARY KEY,fingerprint TEXT UNIQUE,record TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS reading_aliases(scope TEXT PRIMARY KEY,record TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS reading_submissions(scope TEXT PRIMARY KEY,payload TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS reading_parse_heads(revision TEXT PRIMARY KEY,parse TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS reading_resumes(reading TEXT,attempt INTEGER,PRIMARY KEY(reading,attempt))")
            db.execute("PRAGMA user_version=2")

    @contextmanager
    def _guard(self, task):
        if not isinstance(task, dict) or task.get("owner_id") != self.source.owner:
            reject("forbidden")
        with self.store.commit_guard(task["owner_id"], task["task_id"], task["attempt"]) as current:
            if current["config_revision"] != task.get("config_revision"):
                reject("version_conflict")
            yield current

    def _authorize(self, db, task, record, *, resumed=True):
        if (record["owner_id"] != task["owner_id"] or record["task_id"] != task["task_id"]
                or record["config_revision"] != task["config_revision"]):
            reject("forbidden")
        if record["attempt"] != task["attempt"]:
            if not resumed or not db.execute("SELECT 1 FROM reading_resumes WHERE reading=? AND attempt=?",
                                            (record["reading_id"], task["attempt"])).fetchone():
                reject("version_conflict", "旧 attempt 产物尚未受信接续")

    def _source_check(self, db, record):
        _verify_model_products(db, record)
        doc = self.source._document(db, record["document_id"])
        row = db.execute("SELECT record FROM revisions WHERE document=? AND id=?",
                         (record["document_id"], record["revision_id"])).fetchone()
        latest = db.execute("SELECT id FROM revisions WHERE document=? ORDER BY rowid DESC LIMIT 1",
                            (record["document_id"],)).fetchone()
        if not row or not latest or latest[0] != record["revision_id"] or doc["purpose"] != "long_term" or doc["validity"] != "active":
            reject("version_conflict", "阅读来源已失效")
        source = json.loads(row[0])
        materials = _materials(self.workspace, record)
        if source["sha256"] != materials.source_hash:
            reject("storage_failed")
        expected = ".personal_sources/originals/" + source["sha256"]
        if materials.source_relpath != expected:
            reject("forbidden")
        return materials

    def _parse(self, parse_id, record):
        if not isinstance(parse_id, str) or not re.fullmatch(r"[0-9a-f]{64}", parse_id):
            reject()
        try:
            parsed = json.loads((self.source.root / "parses" / (parse_id + ".json")).read_text(encoding="utf-8"))
            payload = {k: v for k, v in parsed.items() if k not in META}
            if (parsed.get("parse_id") != parse_id or parsed.get("document_id") != record["document_id"]
                    or parsed.get("revision_id") != record["revision_id"]
                    or parsed.get("sha256") != _source_hash(record)
                    or _hash(_json([parsed["sha256"], payload]).encode()) != parse_id
                    or payload.get("source_identity") != dict(document_id=record["document_id"], revision_id=record["revision_id"])):
                reject("storage_failed")
            return payload
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, SourceError):
                raise
            raise SourceError("storage_failed", "解析稿不存在或已损坏") from exc

    def prepare(self, task, receipt, base_parse, materials):
        manifest = materials.to_dict() if isinstance(materials, PreparedPdf) else copy.deepcopy(materials)
        parse_id = base_parse["parse_id"] if isinstance(base_parse, dict) else base_parse
        with self._guard(task) as task, self.source.transaction() as db:
            record = dict(schema_version=1, reading_id=uuid4().hex, owner_id=task["owner_id"],
                          task_id=task["task_id"], attempt=task["attempt"], config_revision=task["config_revision"],
                          document_id=receipt["document_id"], revision_id=receipt["revision_id"],
                          base_parse_id=parse_id, manifest=manifest, manifest_hash=manifest["manifest_hash"],
                          model_identity={"config_revision": task["config_revision"]})
            verified = self._source_check(db, record)
            if (verified.document_id != record["document_id"] or verified.revision_id != record["revision_id"]
                    or verified.parse_id != parse_id or verified.config_revision != task["config_revision"]):
                reject("version_conflict")
            self._parse(parse_id, record)
            record["segments"] = [dict(segment_id=s.segment_id, state="prepared", page_map=list(s.page_map),
                                       target_pages=list(s.target_pages)) for s in verified.segments]
            fingerprint = _hash(canonical({k: v for k, v in record.items() if k != "reading_id"}).encode())
            old = db.execute("SELECT id FROM reading_records WHERE fingerprint=?", (fingerprint,)).fetchone()
            if old:
                previous = _read(db, old[0])
                self._source_check(db, previous)
                self._recover_checkpoint(previous)
                _save(db, previous)
                return previous
            # 同一逻辑材料的成功阅读跨重试接续，不为 attempt 改变再次付费。
            for previous_row in db.execute("SELECT id FROM reading_records ORDER BY rowid DESC").fetchall():
                previous = _read(db, previous_row[0])
                if (all(previous[k] == record[k] for k in ("owner_id", "task_id", "config_revision",
                                                          "document_id", "revision_id", "base_parse_id", "manifest_hash"))
                        and previous["attempt"] < task["attempt"]
                        and any((s["state"] == "completed" and s.get("delivery_receipt_id"))
                                or (s["state"] == "sent" and s.get("full_response_saved")) for s in previous["segments"])
                        and all(s["state"] in ("prepared", "completed") or s.get("full_response_saved")
                                for s in previous["segments"])):
                    self._source_check(db, previous)
                    self._parse(previous["base_parse_id"], previous)
                    self._recover_checkpoint(previous)
                    _save(db, previous)
                    db.execute("INSERT OR IGNORE INTO reading_resumes VALUES(?,?)", (previous["reading_id"], task["attempt"]))
                    return previous
            # 跨任务只复用完整成功响应；新 reading 身份不继承旧任务授权。
            for previous_row in db.execute("SELECT id FROM reading_records ORDER BY rowid DESC").fetchall():
                previous = _read(db, previous_row[0])
                if (previous["task_id"] == record["task_id"] or previous.get("reused_from")
                        or any(previous[k] != record[k] for k in ("owner_id", "config_revision", "document_id",
                                                                 "revision_id", "base_parse_id", "manifest_hash"))
                        or not previous["segments"]
                        or not all(s["state"] == "completed" and s.get("full_response_saved")
                                   and s.get("delivery_receipt_id") for s in previous["segments"])):
                    continue
                self._source_check(db, previous)
                self._parse(previous["base_parse_id"], previous)
                record["reused_from"] = {k: copy.deepcopy(previous[k]) for k in ("reading_id", "task_id", "attempt", "model_identity")}
                record["model_identity"] = copy.deepcopy(previous["model_identity"])
                record["segments"] = copy.deepcopy(previous["segments"])
                for segment in record["segments"]:
                    segment["delivery_receipt_id"] = uuid4().hex
                    segment["saved_model_output_context"] = dict(
                        original_reading_id=previous["reading_id"], original_task_id=previous["task_id"],
                        original_attempt=previous["attempt"], current_reading_id=record["reading_id"],
                        current_task_id=record["task_id"], current_attempt=record["attempt"])
                break
            db.execute("INSERT INTO reading_records VALUES(?,?,?)", (record["reading_id"], fingerprint, canonical(record)))
            return record

    def get(self, task, reading_id):
        with self._guard(task) as task, self.source.transaction() as db:
            record = _read(db, reading_id)
            self._authorize(db, task, record)
            self._source_check(db, record)
            return record

    def mark_budget_exceeded(self, task, reading_id, segment_id, *, error_code, budget_kind):
        """受信运行时显式记录实际预算错误；普通 unknown/超时不得推断为超预算。

        budget_kind 只允许 input_bytes/input_context；输出上限、stream_break
        不属于缩批条件。在失败回调之后调用亦可；已有完整响应或完成凭据一律拒绝。
        未发送段记 failed，已发送段保留 unknown，不制造成功回执。
        """
        if error_code != "reading_budget_exceeded" or budget_kind not in ("input_bytes", "input_context"):
            reject("invalid_request", "只接受明确的输入字节或输入上下文超预算")
        with self._guard(task) as task, self.source.transaction() as db:
            record = _read(db, reading_id)
            self._authorize(db, task, record)
            self._source_check(db, record)
            segment = next((s for s in record["segments"] if s["segment_id"] == segment_id), None)
            if (segment is None or segment["state"] not in ("prepared", "sent", "unknown", "failed")
                    or segment.get("full_response_saved") or segment.get("delivery_receipt_id")
                    or segment.get("delivery_attempt", record["attempt"]) != task["attempt"]):
                reject("version_conflict", "阅读段已有成功产物或不属于当前失败回调")
            if segment["state"] == "failed" and segment.get("error_code") != error_code:
                reject("version_conflict")
            if segment.get("budget_kind") and segment["budget_kind"] != budget_kind:
                reject("idempotency_conflict")
            segment.update(state="failed" if segment["state"] in ("prepared", "failed") else "unknown",
                           error_code=error_code, budget_kind=budget_kind)
            _save(db, record)
            return record

    def retry_unknown_segment(self, task, reading_id, segment_id, *, max_retries=2):
        """显式再次读取时建立新身份；保留旧 unknown，重试次数沿接续链有界。"""
        if type(max_retries) is not int or not 0 <= max_retries <= 2:
            reject('invalid_request', '未知结果重试上限须在0至2之间')
        with self._guard(task) as task, self.source.transaction() as db:
            parent = _read(db, reading_id)
            self._authorize(db, task, parent)
            materials = self._source_check(db, parent)
            self._parse(parent['base_parse_id'], parent)
            segment = next((s for s in parent['segments'] if s['segment_id'] == segment_id), None)
            if (segment is None or segment['state'] != 'unknown' or segment.get('full_response_saved')
                    or segment.get('delivery_receipt_id') or segment.get('error_code') == 'reading_budget_exceeded'):
                reject('reading_incomplete', '只有没有成功产物的未知段可显式重试')
            link = dict(reading_id=reading_id, segment_id=segment_id, manifest_hash=parent['manifest_hash'],
                        target_pages=segment['target_pages'], reason='unknown_retry')
            for row in db.execute('SELECT id FROM reading_records').fetchall():
                child = _read(db, row[0])
                if child.get('continuation_from') == link:
                    self._authorize(db, task, child)
                    self._source_check(db, child)
                    return child
            number = parent.get('unknown_retry_number', 0) + 1
            if number > max_retries:
                reject('reading_incomplete', '未知结果的补读次数已达上限；保留失败记录，可重试任务')
            prepared = retry_pdf_segment(self.workspace, materials, segment_id)
            child = dict(schema_version=1, reading_id=uuid4().hex, owner_id=task['owner_id'],
                task_id=task['task_id'], attempt=task['attempt'], config_revision=task['config_revision'],
                document_id=parent['document_id'], revision_id=parent['revision_id'],
                base_parse_id=parent['base_parse_id'], manifest=prepared.to_dict(), manifest_hash=prepared.manifest_hash,
                model_identity={'config_revision': task['config_revision']}, continuation_from=link,
                unknown_retry_number=number,
                segments=[dict(segment_id=s.segment_id, state='prepared', page_map=list(s.page_map),
                               target_pages=list(s.target_pages)) for s in prepared.segments])
            fingerprint = _hash(canonical({k: v for k, v in child.items() if k != 'reading_id'}).encode())
            db.execute('INSERT INTO reading_records VALUES(?,?,?)', (child['reading_id'], fingerprint, canonical(child)))
            return child

    def replan_budget_exceeded(self, task, reading_id, segment_id):
        """受信接续入口：每个明确超预算父段只创建一个子 reading，不发送请求。

        返回的 continuation_from 固定父 ID/清单/范围；子段完成后仍返回同一
        reading，调用方只发送其中 prepared 段。新 attempt 可显式接续，原
        记录身份、清单、完成段和观察均不改写，整体任务目标不在此处修改。
        普通 prepare 不会自动调用本接口；read/flow 仍需显式整合错误分类。
        """
        with self._guard(task) as task, self.source.transaction() as db:
            parent = _read(db, reading_id)
            if any(parent[k] != task[k] for k in ("owner_id", "task_id", "config_revision")):
                reject("forbidden")
            if parent["attempt"] > task["attempt"]:
                reject("version_conflict")
            materials = self._source_check(db, parent)
            self._parse(parent["base_parse_id"], parent)
            segment = next((s for s in parent["segments"] if s["segment_id"] == segment_id), None)
            if (segment is None or segment["state"] not in ("failed", "unknown")
                    or segment.get("error_code") != "reading_budget_exceeded"
                    or segment.get("budget_kind") not in ("input_bytes", "input_context")
                    or segment.get("full_response_saved") or segment.get("delivery_receipt_id")):
                reject("reading_incomplete", "仅明确超预算且无成功产物的失败段可缩批")
            link = dict(reading_id=reading_id, segment_id=segment_id,
                        manifest_hash=parent["manifest_hash"], target_pages=segment["target_pages"],
                        reason="reading_budget_exceeded", budget_kind=segment["budget_kind"])
            records = [_read(db, row[0]) for row in db.execute("SELECT id FROM reading_records").fetchall()]
            child = next((r for r in records if r.get("continuation_from") == link), None)
            if child is None:
                # 不从其他已成功页自动重发；存在重叠时交由调用方对账，不能缩小父目标。
                targets = set(segment["target_pages"])
                for other in records:
                    if (other["task_id"] != task["task_id"] or other["revision_id"] != parent["revision_id"]):
                        continue
                    if any((s["state"] == "completed" or s.get("full_response_saved"))
                           and targets.intersection(s["page_map"]) for s in other["segments"]):
                        reject("version_conflict", "父段范围已有成功阅读页，须先对账")
                prepared = replan_pdf_segment(self.workspace, materials, segment_id)
                child = dict(schema_version=1, reading_id=uuid4().hex, owner_id=task["owner_id"],
                    task_id=task["task_id"], attempt=task["attempt"], config_revision=task["config_revision"],
                    document_id=parent["document_id"], revision_id=parent["revision_id"],
                    base_parse_id=parent["base_parse_id"], manifest=prepared.to_dict(),
                    manifest_hash=prepared.manifest_hash, model_identity={"config_revision": task["config_revision"]},
                    continuation_from=copy.deepcopy(link),
                    segments=[dict(segment_id=s.segment_id, state="prepared", page_map=list(s.page_map),
                                   target_pages=list(s.target_pages)) for s in prepared.segments])
                fingerprint = _hash(canonical({k: v for k, v in child.items() if k != "reading_id"}).encode())
                db.execute("INSERT INTO reading_records VALUES(?,?,?)",
                           (child["reading_id"], fingerprint, canonical(child)))
            self._source_check(db, child)
            if child["attempt"] > task["attempt"]:
                reject("version_conflict")
            for record in (parent, child):
                if record["attempt"] < task["attempt"]:
                    db.execute("INSERT OR IGNORE INTO reading_resumes VALUES(?,?)",
                               (record["reading_id"], task["attempt"]))
            return child

    def for_task(self, task):
        with self._guard(task) as task, self.source.transaction() as db:
            results = []
            for row in db.execute("SELECT id FROM reading_records ORDER BY rowid").fetchall():
                record = _read(db, row[0])
                if record["task_id"] != task["task_id"]:
                    continue
                self._authorize(db, task, record)
                self._source_check(db, record)
                results.append(record)
            return results

    def resume_all(self, task):
        """受信 worker 恢复入口；不要求先用 get/for_task 发现旧记录。"""
        with self._guard(task) as current, self.source.transaction() as db:
            identifiers = [_read(db, row[0]) for row in db.execute("SELECT id FROM reading_records").fetchall()]
            identifiers = [r["reading_id"] for r in identifiers if r["task_id"] == current["task_id"]
                           and r["attempt"] < current["attempt"]
                           and any((s["state"] == "completed" and s.get("delivery_receipt_id"))
                                   or (s["state"] == "sent" and s.get("full_response_saved")) for s in r["segments"])]
        return [self.resume(task, reading_id) for reading_id in identifiers]

    @staticmethod
    def _recover_checkpoint(record):
        for segment in record["segments"]:
            if segment["state"] == "sent" and segment.get("full_response_saved"):
                segment.update(state="completed", delivery_receipt_id=uuid4().hex, recovered_completion=True)

    def resume(self, task, reading_id):
        with self._guard(task) as task, self.source.transaction() as db:
            record = _read(db, reading_id)
            if (record["owner_id"], record["task_id"], record["config_revision"]) != (task["owner_id"], task["task_id"], task["config_revision"]):
                reject("forbidden")
            if task["attempt"] <= record["attempt"]:
                reject("version_conflict")
            self._source_check(db, record)
            self._parse(record["base_parse_id"], record)
            self._recover_checkpoint(record)
            if not any(s["state"] == "completed" and s.get("delivery_receipt_id") for s in record["segments"]):
                reject("reading_incomplete", "没有已保存的成功响应可接续")
            db.execute("INSERT OR IGNORE INTO reading_resumes VALUES(?,?)", (reading_id, task["attempt"]))
            _save(db, record)
            return record  # 不改原 attempt 或原模型身份

    def load(self, task, reading_id, segment_id):
        with self._guard(task) as task, self.source.transaction() as db:
            record = _read(db, reading_id)
            self._authorize(db, task, record)
            materials = self._source_check(db, record)
            segment = next((s for s in record["segments"] if s["segment_id"] == segment_id), None)
            if segment is None or segment["state"] != "prepared":
                reject("reading_incomplete", "材料不在可发送状态")
            source = next(s for s in materials.segments if s.segment_id == segment_id)
            result = dict(path=_relative(self.workspace, source.material_relpath), sha256=source.material_hash,
                          media_type="application/pdf", page_map=list(source.page_map))
            if materials.derived_source is not None:
                result.update(_office_details(materials, source.page_map))
            return result

    def coverage(self, task, reading_id, segment_id):
        """供 ingest 对成功段计算可信覆盖；Office 不冒充原件 PDF 页码。"""
        with self._guard(task) as task, self.source.transaction() as db:
            record = _read(db, reading_id)
            self._authorize(db, task, record)
            materials = self._source_check(db, record)
            segment = next((s for s in record["segments"] if s["segment_id"] == segment_id), None)
            if not segment or segment["state"] != "completed" or not segment.get("delivery_receipt_id"):
                reject("reading_incomplete")
            return materials.coverage_keys(segment["target_pages"])

    def _delivery(self, task, reading_id, segment_id, state, response_id=None):
        with self._guard(task) as task, self.source.transaction() as db:
            record = _read(db, reading_id)
            self._authorize(db, task, record)
            self._source_check(db, record)
            segment = next((s for s in record["segments"] if s["segment_id"] == segment_id), None)
            if segment is None:
                reject()
            if segment.get("delivery_attempt", record["attempt"]) != task["attempt"]:
                reject("version_conflict", "旧 callback 不得借接续授权")
            previous = segment["state"]
            if state == previous:
                if state == "completed" and response_id and response_id != segment["response_id"]:
                    reject("idempotency_conflict")
                return record
            if (previous, state) not in (("prepared", "sent"), ("sent", "completed"), ("sent", "unknown")):
                reject("version_conflict", "阅读生命周期不能跳步或倒退")
            segment["state"] = state
            if state == "completed":
                segment["response_id"] = segment.get("response_id") or response_id
                segment["delivery_receipt_id"] = uuid4().hex
                segment["response_checkpoint"] = "response_saved" if segment.get("full_response_saved") else "completion_only"
                segment.setdefault("full_response_saved", False)
            _save(db, record)
            return record

    def mark_sent(self, task, reading_id, segment_id):
        return self._delivery(task, reading_id, segment_id, "sent")

    def mark_completed(self, task, reading_id, segment_id, response_id=None):
        return self._delivery(task, reading_id, segment_id, "completed", response_id)

    def mark_unknown(self, task, reading_id, segment_id):
        return self._delivery(task, reading_id, segment_id, "unknown")

    def save_response(self, task, reading_id, segment_id, response):
        """先保存模型完整 output，再允许 completed；不接受请求、媒体字节或 data URI。"""
        if (not isinstance(response, dict) or set(response) - {"id", "output", "usage", "status"}
                or response.get("status") != "completed" or not isinstance(response.get("output"), list)):
            reject("invalid_request", "非法模型响应检查点")
        text(response.get("id"))
        response = dict(response, usage=response.get("usage"))
        try:
            encoded = canonical(response)
        except (ValueError, TypeError) as exc:
            raise SourceError("invalid_request", "响应检查点必须为 JSON 模型产物") from exc
        if len(encoded.encode()) > 20 * 1024 * 1024 or re.search(r"data:[^;\s]+;base64,", encoded, re.I):
            reject("limit_exceeded", "响应检查点包含超限产物或媒体编码")
        for output in response["output"]:
            if not isinstance(output, dict) or output.get("type") not in ("message", "function_call", "reasoning"):
                reject("invalid_request", "不支持的模型产物类型")
        with self._guard(task) as task, self.source.transaction() as db:
            record = _read(db, reading_id)
            self._authorize(db, task, record)
            self._source_check(db, record)
            segment = next((s for s in record["segments"] if s["segment_id"] == segment_id), None)
            if segment is None or segment["state"] != "sent":
                reject("version_conflict")
            if segment.get("delivery_attempt", record["attempt"]) != task["attempt"]:
                reject("version_conflict")
            digest = _hash(encoded.encode())
            if segment.get("response_hash") and segment["response_hash"] != digest:
                reject("idempotency_conflict")
            segment.update(response_id=response["id"], saved_model_output=copy.deepcopy(response["output"]),
                           usage=copy.deepcopy(response.get("usage")), response_hash=digest,
                           response_checkpoint="response_saved", full_response_saved=True)
            _save(db, record)
            return record

    def media_reference(self, task, reading_id, segment_id, *, identity, provider_identity):
        from agent.protocol.tool_media import ToolMediaReference, ToolMediaContent
        from agent.knowledge.reading_capabilities import _key
        from models.config_revision import require_config_revision

        provider = copy.deepcopy(provider_identity)
        if not isinstance(provider, dict) or provider.get("config_revision") != task["config_revision"]:
            reject("attachment_unsupported")
        if identity.user_id != task["owner_id"]:
            reject("forbidden")
        self.load(task, reading_id, segment_id)
        with self._guard(task) as current, self.source.transaction() as db:
            record = _read(db, reading_id)
            self._authorize(db, current, record)
            if record["model_identity"] != {"config_revision": task["config_revision"]} and record["model_identity"] != provider:
                reject("version_conflict")
            record["model_identity"] = provider
            segment = next(s for s in record["segments"] if s["segment_id"] == segment_id)
            segment["delivery_attempt"] = current["attempt"]
            _save(db, record)

        def supports(context):
            if (context.get("protocol") != "responses" or context.get("model") != provider.get("model")
                    or context.get("provider") != provider.get("provider")
                    or context.get("mime_type") != provider.get("mime_type")
                    or context.get("transport") != provider.get("transport")
                    or context.get("config_revision") != task["config_revision"]
                    or context.get("task_id") != task["task_id"] or context.get("identity") != identity
                    or _hash(str(context.get("api_base") or "").rstrip("/").encode()) != provider.get("endpoint_hash")):
                return False
            with self.source.transaction() as db:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reading_capabilities'").fetchone()
                row = db.execute("SELECT record FROM reading_capabilities WHERE id=?", (_key(provider),)).fetchone() if exists else None
                capability = json.loads(row[0]) if row else {}
                return capability.get("state") == "supported" and capability.get("identity") == provider

        def loader(context):
            if not supports(context):
                reject("attachment_unsupported")
            require_config_revision(task["config_revision"])
            media = self.load(task, reading_id, segment_id)
            data = media["path"].read_bytes()
            if _hash(data) != media["sha256"]:
                reject("storage_failed")
            return ToolMediaContent("application/pdf", data)

        def on_delivery(state):
            return self._delivery(task, reading_id, segment_id, state)

        def on_response(response):
            return self.save_response(task, reading_id, segment_id, response)

        return ToolMediaReference(loader, supports, on_delivery, on_response=on_response)

    def resolve_evidence(self, task, submission_id, evidence, *, submission_payload=None):
        text(submission_id)
        if submission_payload is not None:
            text(submission_payload)
        if not isinstance(evidence, list) or len(evidence) > 100:
            reject()
        with self._guard(task) as task, self.source.transaction() as db:
            entries, seen = [], set()
            for item in evidence:
                if not isinstance(item, dict) or set(item) != EVIDENCE_FIELDS:
                    reject()
                for key in ("reading_id", "evidence_key", "segment_id", "observation"):
                    text(item[key])
                if (item["type"] not in ("ocr", "visual_interpretation")
                        or item["observation"].strip() in ("已读取该文件", "附件已读", "已读取")
                        or len(item["observation"].encode()) > 100000):
                    reject()
                alias = (item["reading_id"], item["evidence_key"])
                if alias in seen:
                    reject("invalid_request", "同一 reading 的证据键重复")
                seen.add(alias)
                record = _read(db, item["reading_id"])
                self._authorize(db, task, record)
                material = self._source_check(db, record)
                self._parse(record["base_parse_id"], record)
                segment = next((s for s in record["segments"] if s["segment_id"] == item["segment_id"]), None)
                if not segment or segment["state"] != "completed" or not segment.get("delivery_receipt_id"):
                    reject("reading_incomplete")
                loc = item["location"]
                _validate_location(material, segment, loc)
                scope = canonical([task["owner_id"], task["task_id"], submission_id, *alias])
                payload = dict(evidence=copy.deepcopy(item), material=dict(
                    manifest_hash=material.manifest_hash, document_id=record["document_id"],
                    revision_id=record["revision_id"], base_parse_id=record["base_parse_id"],
                    model_identity=record["model_identity"], attempt=record["attempt"],
                    delivery_receipt_id=segment["delivery_receipt_id"], response_id=segment["response_id"]))
                entries.append((alias, scope, payload, record))
            entries.sort(key=lambda e: e[0])
            submission_scope = canonical([task["owner_id"], task["task_id"], submission_id])
            complete_payload = canonical(dict(evidence=[e[2] for e in entries], submission_payload=submission_payload))
            old = db.execute("SELECT payload FROM reading_submissions WHERE scope=?", (submission_scope,)).fetchone()
            if old and old[0] != complete_payload:
                reject("idempotency_conflict")
            existing = {}
            for alias, scope, payload, record in entries:
                row = db.execute("SELECT record FROM reading_aliases WHERE scope=?", (scope,)).fetchone()
                if row:
                    previous = json.loads(row[0])
                    if canonical(previous["payload"]) != canonical(payload):
                        reject("idempotency_conflict")
                    parsed = self._parse(previous["ref"]["parse_id"], record)
                    verify_reading_unit(db, self.source, parsed["units"][previous["ref"]["ordinal"]], previous["ref"])
                    existing[alias] = previous["ref"]
            # 冲突检查完毕后才写新文件。事务失败可留内容寻址孤儿文件；重试不覆盖。
            result = dict(existing)
            for alias, scope, payload, record in entries:
                if alias in existing:
                    continue
                row = db.execute("SELECT parse FROM reading_parse_heads WHERE revision=?", (record["revision_id"],)).fetchone()
                parsed = self._parse(row[0] if row else record["base_parse_id"], record)
                item = payload["evidence"]
                ordinal = len(parsed["units"])
                parsed["units"].append(dict(text=item["observation"], type=item["type"], origin="model",
                    location=dict(item["location"], type=item["type"]), model_identity=record["model_identity"],
                    reading_provenance=dict(reading_id=record["reading_id"], manifest_hash=record["manifest_hash"], alias_scope=scope)))
                digest = _source_hash(record)
                parse_id = _hash(_json([digest, parsed]).encode())
                parsed.update(parse_id=parse_id, document_id=record["document_id"], revision_id=record["revision_id"], sha256=digest)
                target = self.source.root / "parses" / (parse_id + ".json")
                if target.exists():
                    if json.loads(target.read_text(encoding="utf-8")) != parsed:
                        reject("storage_failed")
                else:
                    write_text_atomic(target, _json(parsed), newline="")
                ref = dict(document_id=record["document_id"], revision_id=record["revision_id"], parse_id=parse_id, ordinal=ordinal)
                db.execute("INSERT INTO reading_aliases VALUES(?,?)", (scope, canonical(dict(payload=payload, ref=ref))))
                db.execute("INSERT INTO reading_parse_heads VALUES(?,?) ON CONFLICT(revision) DO UPDATE SET parse=excluded.parse",
                           (record["revision_id"], parse_id))
                result[alias] = ref
            if not old:
                db.execute("INSERT INTO reading_submissions VALUES(?,?)", (submission_scope, complete_payload))
            return result


ReadingRegistry = ReadingService
