"""Versioned source authority and atomic keyword publication, schema v1.

The SQLite commit is the visibility point. Files and index rows are staged first;
interrupted candidates remain invisible and can be retried after restart.
"""
import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from common.atomic_write import write_text_atomic
from agent.knowledge.parser import SourceError, parse_source


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str)


class SourceService:
    def __init__(self, workspace_root, owner_id="local-owner", knowledge_root=None):
        if owner_id != "local-owner":
            raise SourceError("forbidden", "无权访问个人资料")
        self.owner = owner_id
        self.workspace = Path(workspace_root).resolve()
        self.root = self.workspace / ".personal_sources"
        self.knowledge = Path(knowledge_root or self.workspace / "knowledge").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "sources.sqlite3"
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise SourceError("version_conflict", f"不支持来源 schema {version}")
            db.execute("CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, current TEXT, state INTEGER, purpose TEXT, validity TEXT)")
            added_publication = "publication" not in {row[1] for row in db.execute("PRAGMA table_info(documents)")}
            if added_publication:
                db.execute("ALTER TABLE documents ADD COLUMN publication TEXT")
            db.execute("CREATE TABLE IF NOT EXISTS revisions(id TEXT PRIMARY KEY, document TEXT, hash TEXT, filename TEXT, record TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, fingerprint TEXT, record TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY, document TEXT, revision TEXT, state INTEGER, expected TEXT, record TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS publications(id TEXT PRIMARY KEY, document TEXT, revision TEXT, parse TEXT, path TEXT UNIQUE, hash TEXT, record TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS keyword_units(publication TEXT, ordinal INTEGER, text TEXT, location TEXT, PRIMARY KEY(publication,ordinal))")
            db.execute("CREATE TABLE IF NOT EXISTS changes(id TEXT PRIMARY KEY, document TEXT, record TEXT)")
            from agent.knowledge.wiki import ensure_schema
            ensure_schema(db)
            if added_publication:
                # An additive v1 upgrade must retain existing valid evidence.
                # Run only alongside ADD COLUMN, never on ordinary reopen:
                # null pointers after a later withdrawal must stay null.
                for document in db.execute("SELECT * FROM documents WHERE current IS NOT NULL").fetchall():
                    publications = db.execute("SELECT * FROM publications WHERE document=? AND revision=? ORDER BY rowid DESC",
                                              (document["id"], document["current"])).fetchall()
                    for publication in publications:
                        candidate_document = dict(document, publication=publication["id"])
                        try:
                            valid = self._valid(candidate_document, publication, db)
                        except (KeyError, TypeError, ValueError):
                            valid = False
                        if valid:
                            db.execute("UPDATE documents SET publication=? WHERE id=?", (publication["id"], document["id"]))
                            break
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.db, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def receive(self, path, *, filename, request_id, origin, document_id=None):
        """Trusted callers MUST authorize the path against the receiving task first."""
        if not request_id or not origin or not filename:
            raise SourceError("invalid_request", "缺少来源身份")
        path = Path(path)
        if path.stat().st_size > 100 * 1024 * 1024:
            raise SourceError("limit_exceeded", "原件超过 100 MiB")
        data = path.read_bytes()
        digest = _hash(data)
        fingerprint = _hash(_json([digest, filename, origin, document_id]).encode())
        with self.transaction() as db:
            previous = db.execute("SELECT * FROM receipts WHERE id=?", (request_id,)).fetchone()
            if previous:
                receipt = json.loads(previous["record"])
                # Reading may allocate the document before ingest echoes its
                # ID. Omitting that same ID later is also a lookup, not a new
                # association. Keep the original fingerprint/receipt unchanged.
                same_document = not document_id or document_id == receipt["document_id"]
                equivalent = {
                    _hash(_json([digest, filename, origin, target]).encode())
                    for target in (None, receipt["document_id"])
                } if same_document else set()
                if previous["fingerprint"] != fingerprint and previous["fingerprint"] not in equivalent:
                    raise SourceError("idempotency_conflict", "请求标识已用于不同资料")
                return receipt
            if document_id:
                if not db.execute("SELECT 1 FROM documents WHERE id=?", (document_id,)).fetchone():
                    raise SourceError("invalid_request", "逻辑资料不存在")
            else:
                document_id = uuid4().hex
                db.execute("INSERT INTO documents(id,current,state,purpose,validity) VALUES(?,NULL,0,'waiting','active')", (document_id,))
            old = db.execute("SELECT id FROM revisions WHERE document=? AND hash=?", (document_id, digest)).fetchone()
            revision = old[0] if old else uuid4().hex
            blob = self.root / "originals" / digest
            blob.parent.mkdir(parents=True, exist_ok=True)
            if blob.exists():
                if _hash(blob.read_bytes()) != digest:
                    raise SourceError("storage_failed", "原件哈希不符")
            else:
                temp = blob.with_suffix(".tmp-" + uuid4().hex)
                try:
                    with temp.open("xb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temp, blob)
                finally:
                    temp.unlink(missing_ok=True)
            record = dict(schema_version=1, document_id=document_id, revision_id=revision,
                          sha256=digest, filename=filename, origin=origin, original=str(blob),
                          received_at=datetime.now(timezone.utc).isoformat(), duplicate=bool(old))
            if not old:
                db.execute("INSERT INTO revisions VALUES(?,?,?,?,?)", (revision, document_id, digest, filename, _json(record)))
            db.execute("INSERT INTO receipts VALUES(?,?,?)", (request_id, fingerprint, _json(record)))
        self._refresh_wiki_projection()
        return record

    def _refresh_wiki_projection(self):
        from agent.knowledge.wiki import project_indexes
        with self.transaction() as db:
            project_indexes(db, self)

    def parse(self, revision_id, *, visual=False, vision=None, visual_budget=None,
              config_revision=None, base_parse_id=None):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM revisions WHERE id=?", (revision_id,)).fetchone()
            if not row:
                raise SourceError("invalid_request", "内容版本不存在")
            record = json.loads(row["record"])
        blob = Path(record["original"])
        if _hash(blob.read_bytes()) != record["sha256"]:
            raise SourceError("storage_failed", "原件哈希校验失败")
        metadata = {"parse_id", "revision_id", "document_id", "sha256"}
        if base_parse_id is None and Path(record["filename"]).suffix.lower() == ".pdf":
            from agent.knowledge.parser import PARSER_ID, PARSER_VERSION
            source_identity = dict(document_id=record["document_id"], revision_id=revision_id)
            best = None
            for candidate in (self.root / "parses").glob("*.json"):
                if not re.fullmatch(r"[0-9a-f]{64}", candidate.stem):
                    continue
                try:
                    cached = json.loads(candidate.read_text(encoding="utf-8"))
                    if (not isinstance(cached, dict) or cached.get("parse_id") != candidate.stem
                            or cached.get("document_id") != record["document_id"]
                            or cached.get("revision_id") != revision_id or cached.get("sha256") != record["sha256"]
                            or cached.get("source_identity") != source_identity
                            or cached.get("parser_id") != PARSER_ID or cached.get("parser_version") != PARSER_VERSION):
                        continue
                    content = {key: value for key, value in cached.items() if key not in metadata}
                    if _hash(_json([record["sha256"], content]).encode()) != candidate.stem:
                        continue
                    coverage = cached.get("visual_coverage", [])
                    # Append-only continuations win over offline/unavailable
                    # parses; ties retain retry progress without changing ordinals.
                    score = (sum(bool(c.get("ocr") or c.get("visual_interpretation")) for c in coverage),
                             sum(c.get("status") == "processed" for c in coverage),
                             len(cached["units"]), sum(c.get("attempts", 0) for c in coverage),
                             candidate.stat().st_mtime_ns, candidate.stem)
                    if best is None or score > best[0]:
                        best = (score, candidate.stem)
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    continue  # A damaged cache is never promoted to source evidence.
            if best is not None:
                base_parse_id = best[1]
        if base_parse_id is not None:
            if not isinstance(base_parse_id, str) or not re.fullmatch(r"[0-9a-f]{64}", base_parse_id):
                raise SourceError("invalid_request", "解析版本标识无效")
            try:
                base = json.loads((self.root / "parses" / f"{base_parse_id}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise SourceError("invalid_request", "解析版本不存在或已损坏") from exc
            if (not isinstance(base, dict) or base.get("parse_id") != base_parse_id
                    or base.get("revision_id") != revision_id
                    or base.get("document_id") != record["document_id"]
                    or base.get("sha256") != record["sha256"]):
                raise SourceError("version_conflict", "解析版本不属于当前资料原件")
            result = {key: value for key, value in base.items() if key not in metadata}
            if _hash(_json([record["sha256"], result]).encode()) != base_parse_id:
                raise SourceError("storage_failed", "解析稿哈希校验失败")
        else:
            result = parse_source(blob, record["filename"])
        # Equal bytes may belong to distinct logical documents. Bind this
        # immutable parse to its source without changing legacy hash checks.
        result.setdefault("source_identity", dict(document_id=record["document_id"], revision_id=revision_id))
        if type(visual) is not bool:
            raise SourceError("invalid_request", "visual 须为布尔值")
        if visual and Path(record["filename"]).suffix.lower() == ".pdf":
            from agent.knowledge.visual import enrich_pdf_visual
            result = enrich_pdf_visual(blob, result, vision=vision, budget=visual_budget,
                                       config_revision=config_revision)
        parse_id = _hash(_json([record["sha256"], result]).encode())
        result.update(parse_id=parse_id, revision_id=revision_id, document_id=record["document_id"], sha256=record["sha256"])
        (self.root / "parses").mkdir(exist_ok=True)
        target = self.root / "parses" / f"{parse_id}.json"
        if target.exists():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise SourceError("storage_failed", "已有解析稿损坏，禁止覆盖") from exc
            if existing != result:
                raise SourceError("storage_failed", "已有解析稿身份冲突，禁止覆盖")
        else:
            write_text_atomic(target, _json(result), newline="")
        return result

    def change(self, document_id, *, purpose=None, action="purpose", expected_state, reason):
        if type(expected_state) is not int or action not in ("purpose", "withdraw", "correct", "republish") or (action == "purpose" and purpose not in ("long_term", "transient", "waiting")) or not reason:
            raise SourceError("invalid_request", "无效用途或动作")
        with self.transaction() as db:
            doc = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            if not doc or doc["state"] != expected_state:
                raise SourceError("version_conflict", "资料状态已变化")
            purpose = purpose or doc["purpose"]
            validity = {"withdraw": "withdrawn", "correct": "corrected", "republish": "active"}.get(action, doc["validity"])
            # Every purpose/correction/withdrawal invalidates candidates and old evidence.
            db.execute("UPDATE documents SET state=state+1,purpose=?,validity=?,current=NULL,publication=NULL WHERE id=?", (purpose, validity, document_id))
            db.execute("INSERT INTO changes VALUES(?,?,?)", (uuid4().hex, document_id, _json(dict(action=action, purpose=purpose, reason=reason, previous_revision=doc["current"], state=expected_state+1))))
            from agent.knowledge.wiki import suspend_sources
            suspend_sources(db, [document_id], reason)
            result = self._document(db, document_id)
        self._refresh_wiki_projection()
        return result

    @staticmethod
    def _document(db, document_id):
        row = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if not row:
            raise SourceError("invalid_request", "资料不存在")
        return dict(row)

    def get(self, document_id):
        with self.transaction() as db:
            return self._document(db, document_id)

    def evidence_revision(self):
        """Stable fingerprint of current evidence, including external edits."""
        with self.transaction() as db:
            from agent.knowledge.wiki import refresh
            refresh(db, self)
            documents = [tuple(row) for row in db.execute(
                "SELECT id,current,state,purpose,validity,publication FROM documents ORDER BY id")]
            projections = []
            for row in db.execute("SELECT p.id,p.path,p.hash FROM publications p JOIN documents d ON d.publication=p.id ORDER BY p.id"):
                try:
                    actual = _hash(Path(row["path"]).read_bytes())
                except OSError:
                    actual = None
                projections.append((row["id"], row["hash"], actual))
            wiki = [tuple(row) for row in db.execute("SELECT id,current,status,reason FROM wiki_pages ORDER BY id")]
            for row in db.execute("SELECT v.id,v.path,v.hash FROM wiki_publications v JOIN wiki_pages p ON p.current=v.id ORDER BY v.id"):
                try:
                    actual = _hash((self.workspace / row['path']).read_bytes())
                except OSError:
                    actual = None
                projections.append((row['id'], row['hash'], actual))
        return _hash(_json([documents, projections, wiki]).encode())

    def registered_revision(self, document_id, revision_id=None):
        """Resolve only an already registered source, never a model file path."""
        with self.transaction() as db:
            self._document(db, document_id)
            if revision_id:
                row = db.execute("SELECT record FROM revisions WHERE document=? AND id=?", (document_id, revision_id)).fetchone()
            else:
                row = db.execute("SELECT record FROM revisions WHERE document=? ORDER BY rowid DESC LIMIT 1", (document_id,)).fetchone()
            if not row:
                raise SourceError("invalid_request", "资料没有对应的已登记版本")
            return json.loads(row[0])

    def current_publication(self, document_id, revision_id):
        with self.transaction() as db:
            from agent.knowledge.wiki import refresh, _valid as wiki_valid, _ref
            refresh(db, self)
            wiki_pubs = db.execute("SELECT DISTINCT v.* FROM wiki_publications v JOIN wiki_sources s ON s.publication=v.id JOIN wiki_pages p ON p.current=v.id WHERE s.document=? AND s.revision=? ORDER BY v.rowid DESC", (document_id, revision_id)).fetchall()
            for publication in wiki_pubs:
                if wiki_valid(db, self, publication):
                    record = json.loads(publication['record'])
                    ordinal = next(i for i, source in enumerate(record['sources'])
                                   if source['document_id'] == document_id and source['revision_id'] == revision_id)
                    return _ref(record, ordinal)
            doc = self._document(db, document_id)
            pubs = db.execute("SELECT * FROM publications WHERE document=? AND revision=?", (document_id, revision_id)).fetchall()
            for pub in reversed(pubs):
                if self._valid(doc, pub, db):
                    return json.loads(pub["record"])
        return None

    def prepare(self, revision_id, *, expected_current_revision=None, summary=None, task_id=None):
        # Capture the decision revision BEFORE expensive parsing. Withdrawal,
        # correction or a purpose decision during parsing invalidates this work.
        with self.transaction() as db:
            row = db.execute("SELECT document FROM revisions WHERE id=?", (revision_id,)).fetchone()
            if not row:
                raise SourceError("invalid_request", "内容版本不存在")
            baseline = self._document(db, row[0])
            from agent.knowledge.wiki import legacy_retired
            if legacy_retired(db, row[0]):
                raise SourceError('version_conflict', '资料已转由主题知识页维护，禁止旧原文候选')
        parsed = self.parse(revision_id)
        with self.transaction() as db:
            doc = self._document(db, parsed["document_id"])
            from agent.knowledge.wiki import legacy_retired
            if legacy_retired(db, doc['id']):
                raise SourceError('version_conflict', '资料已转由主题知识页维护，禁止旧原文候选')
            if doc["state"] != baseline["state"] or doc["current"] != expected_current_revision or doc["purpose"] != "long_term" or doc["validity"] != "active":
                raise SourceError("version_conflict", "内容版本或用途状态不允许发布")
            candidate_id = uuid4().hex
            record = dict(candidate_id=candidate_id, parse=parsed, summary=summary, stage="pending_index", task_id=task_id)
            db.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?)", (candidate_id, doc["id"], revision_id, doc["state"], expected_current_revision, _json(record)))
        return record

    def publish(self, candidate_id):
        with self.transaction() as db:
            candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            if not candidate:
                raise SourceError("invalid_request", "候选不存在")
            from agent.knowledge.wiki import legacy_retired
            if legacy_retired(db, candidate['document']):
                raise SourceError('version_conflict', '资料已转由主题知识页维护，旧原文候选已退役')
            old = db.execute("SELECT record FROM publications WHERE id=?", (candidate_id,)).fetchone()
            if old:
                pub = db.execute("SELECT * FROM publications WHERE id=?", (candidate_id,)).fetchone()
                if not self._valid(self._document(db, pub["document"]), pub, db):
                    raise SourceError("version_conflict", "历史发布已失效")
                return json.loads(old[0])
            doc = self._document(db, candidate["document"])
            if doc["state"] != candidate["state"] or doc["current"] != candidate["expected"] or doc["purpose"] != "long_term" or doc["validity"] != "active":
                raise SourceError("version_conflict", "候选已因用途、更正或撤回失效")
            record = json.loads(candidate["record"])
            parsed = record["parse"]
            rel = f"managed/{doc['id']}/{candidate_id}.md"
            refs = dict(document_id=doc["id"], revision_id=candidate["revision"], parse_id=parsed["parse_id"], publish_id=candidate_id)
            text = "<!-- personal-source-managed -->\n" + "<!-- " + _json(refs) + " -->\n"
            if record.get("summary"):
                text += record["summary"] + "\n\n"
            for unit in parsed["units"]:
                text += _json(unit["location"]) + "\n" + unit["text"] + "\n\n"
            if len(text.encode()) > 10 * 1024 * 1024:
                raise SourceError("limit_exceeded", "发布投影超过 10 MiB")
            target = self.knowledge / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(target, text, newline="")
            for ordinal, unit in enumerate(parsed["units"]):
                db.execute("INSERT INTO keyword_units VALUES(?,?,?,?)", (candidate_id, ordinal, unit["text"], _json(unit["location"])))
            rows = db.execute("SELECT text,location FROM keyword_units WHERE publication=? ORDER BY ordinal", (candidate_id,)).fetchall()
            if [(r[0], r[1]) for r in rows] != [(u["text"], _json(u["location"])) for u in parsed["units"]]:
                raise SourceError("index_failed", "关键词索引回读不一致")
            result = dict(**refs, task_id=record.get("task_id"), path=f"knowledge/{rel}", outcome="published", searchable=True,
                          index_mode="keyword", indexed_units=len(rows), state_revision=doc["state"], schema_version=1)
            db.execute("INSERT INTO publications VALUES(?,?,?,?,?,?,?)", (candidate_id, doc["id"], candidate["revision"], parsed["parse_id"], str(target), _hash(text.encode()), _json(result)))
            db.execute("UPDATE documents SET current=?,publication=? WHERE id=?", (candidate["revision"], candidate_id, doc["id"]))
            return result

    def read_reference(self, publish_id, ordinal, *, historical=False):
        with self.transaction() as db:
            is_wiki = db.execute('SELECT 1 FROM wiki_publications WHERE id=?', (publish_id,)).fetchone()
        if is_wiki:
            from agent.knowledge.wiki import WikiService
            return WikiService(self.workspace, self.owner).read_reference(publish_id, ordinal, historical)
        with self.transaction() as db:
            pub = db.execute("SELECT * FROM publications WHERE id=?", (publish_id,)).fetchone()
            if not pub:
                raise SourceError("invalid_request", "引用不存在")
            doc = self._document(db, pub["document"])
            valid = self._valid(doc, pub, db)
            if not valid and not historical:
                raise SourceError("version_conflict", "引用已失效或投影存在未发布变更")
            unit = db.execute("SELECT * FROM keyword_units WHERE publication=? AND ordinal=?", (publish_id, ordinal)).fetchone()
            if not unit:
                raise SourceError("invalid_request", "位置不存在")
            parsed = json.loads((self.root / "parses" / f"{pub['parse']}.json").read_text(encoding="utf-8"))
            revision = json.loads(db.execute("SELECT record FROM revisions WHERE id=?", (pub["revision"],)).fetchone()[0])
            if _hash(Path(revision["original"]).read_bytes()) != revision["sha256"]:
                raise SourceError("storage_failed", "原件被修改")
            return dict(**json.loads(pub["record"]), ordinal=ordinal, text=parsed["units"][ordinal]["text"],
                        location=parsed["units"][ordinal]["location"], valid=valid,
                        current_status=doc["validity"] if doc["validity"] != "active" else ("current" if valid else "superseded_or_modified"), original=revision["original"])

    def _valid(self, doc, pub, db=None):
        from agent.knowledge.wiki import replaced
        if db is not None and replaced(db, doc["id"], pub["revision"], self):
            return False
        try:
            return (doc["current"] == pub["revision"] and doc["publication"] == pub["id"] and doc["validity"] == "active" and doc["purpose"] == "long_term"
                    and doc["state"] == json.loads(pub["record"])["state_revision"]
                    and _hash(Path(pub["path"]).read_bytes()) == pub["hash"])
        except OSError:
            return False

    def search(self, query, limit=10):
        if limit <= 0:
            return []
        from agent.knowledge.wiki import WikiService, replaced
        results = WikiService(self.workspace, self.owner).search(query, limit=limit)
        tokens = re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]{2}", query.lower())
        # Chinese overlapping bigrams allow real questions without a tokenizer
        # dependency or an embedding request. Rank by matched keyword count.
        for run in re.findall(r"[\u4e00-\u9fff]+", query):
            tokens.extend(run[i:i+2] for i in range(len(run)-1))
        tokens = sorted(set(tokens)) or [query]
        with self.transaction() as db:
            where = " OR ".join("instr(lower(text),?)>0" for _ in tokens)
            rows = db.execute(f"SELECT publication,ordinal,text FROM keyword_units WHERE {where}", tokens).fetchall()
        rows = sorted(rows, key=lambda row: sum(token in row[2].lower() for token in tokens), reverse=True)
        for row in rows:
            if len(results) >= limit:
                break
            try:
                ref = self.read_reference(row[0], row[1])
                with self.transaction() as db:
                    if replaced(db, ref['document_id'], ref['revision_id'], self):
                        continue
                results.append(ref)
            except SourceError:
                continue
            if len(results) >= limit:
                break
        return results


def guard_path(path, *, write=False):
    """Fail closed for managed pages, including uncommitted/orphan projections."""
    path = Path(path).resolve()
    if ".personal_sources" in path.parts:
        raise SourceError("forbidden", "来源留档通过已授权来源接口读取；禁止直接改写权威记录")
    for knowledge in path.parents:
        if knowledge.name != "knowledge":
            continue
        rel = path.relative_to(knowledge)
        authority = knowledge.parent / ".personal_sources" / "sources.sqlite3"
        if rel.parts[0] not in ("managed", "wiki") and not authority.exists():
            return
        managed = rel.parts[0] in ("managed", "wiki")
        if authority.exists():
            service = SourceService(knowledge.parent, knowledge_root=knowledge)
            with service.transaction() as db:
                from agent.knowledge.wiki import refresh, _valid as wiki_valid
                refresh(db, service)
                wiki_pub = db.execute('SELECT * FROM wiki_publications WHERE path=?', (f'knowledge/{rel.as_posix()}',)).fetchone()
                if wiki_pub:
                    managed = True
                    if not write and wiki_valid(db, service, wiki_pub):
                        return
                pub = db.execute("SELECT * FROM publications WHERE path=?", (str(path),)).fetchone()
                if pub:
                    managed = True
                    if not write and service._valid(service._document(db, pub["document"]), pub, db):
                        return
        if managed:
            raise SourceError("version_conflict", "托管页须通过来源发布入口处理；当前页无有效发布或禁止直接写入")
        return


def readable(path):
    try:
        guard_path(path)
        return True
    except (SourceError, sqlite3.Error):
        return False


def resume_publication(task, store):
    """Resume only staged candidates, with cancellation and validity barriers.

    Worker must claim/reconcile the task to running first, preserving attempt.
    An already committed publication is verified then reused, never reparsed.
    """
    owner, task_id, attempt = task["owner_id"], task["task_id"], task["attempt"]
    candidates = task.get("source_candidates") or []
    if not candidates:
        raise SourceError("invalid_request", "没有待恢复发布候选")
    with store.commit_guard(owner, task_id, attempt):
        refs = list(task.get("source_existing_refs") or [])
        for reference in refs:
            # Check duplicate references as well as staged publications.
            service = SourceService(candidates[0]["workspace"], owner)
            service.read_reference(reference["publish_id"], 0)
        refs += [SourceService(item["workspace"], owner).publish(item["candidate_id"]) for item in candidates]
        return store.complete(owner, task_id, attempt, "published", refs)
