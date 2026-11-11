"""多来源主题页：不可变发布、正文索引和可追溯的出处目录。"""
import hashlib
import json
import posixpath
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from urllib.parse import unquote, urlsplit

from common.atomic_write import write_text_atomic
from agent.knowledge.parser import SourceError
from agent.knowledge.personal_assistant import SourceService, _hash, _json


def validate_page_summaries(pages):
    """新输入统一校验；历史候选直接发布，不经过此入口。"""
    if not isinstance(pages, list) or not pages or len(pages) > 100:
        raise SourceError('invalid_request', '须提交非空知识页数组（最多100页）')
    summaries = []
    for page in pages:
        summary = page.get('summary') if isinstance(page, dict) else None
        if (not isinstance(summary, str)
                or any(unicodedata.category(c) in {'Cc', 'Cs', 'Zl', 'Zp'} for c in summary)
                or not 1 <= len(summary.strip()) <= 160):
            raise SourceError('invalid_request', '每页 summary 须为1～160个Unicode码点的单行纯文本，禁止换行及控制字符；请补齐后重新提交')
        summaries.append(summary.strip())
    return summaries


def _index_text(text):
    """将纯文本安全地呈现在 Markdown 目录中。"""
    import html
    return re.sub(r'([\\`*_{}\[\]()#!|~])', r'\\\1', html.escape(text, quote=False))


def ensure_schema(db):
    # Additive tables deliberately keep the source/backup v1 contract intact.
    statements = (
        "CREATE TABLE IF NOT EXISTS wiki_pages(id TEXT PRIMARY KEY,current TEXT,status TEXT,reason TEXT)",
        "CREATE TABLE IF NOT EXISTS wiki_batches(id TEXT PRIMARY KEY,task TEXT,fingerprint TEXT,record TEXT,published INTEGER,UNIQUE(task,fingerprint))",
        "CREATE TABLE IF NOT EXISTS wiki_publications(id TEXT PRIMARY KEY,page TEXT,batch TEXT,path TEXT UNIQUE,hash TEXT,verified INTEGER,record TEXT)",
        "CREATE TABLE IF NOT EXISTS wiki_units(publication TEXT,ordinal INTEGER,text TEXT,PRIMARY KEY(publication,ordinal))",
        "CREATE TABLE IF NOT EXISTS wiki_sources(publication TEXT,ordinal INTEGER,document TEXT,revision TEXT,record TEXT,PRIMARY KEY(publication,ordinal))",
        "CREATE TABLE IF NOT EXISTS wiki_dependencies(publication TEXT,page TEXT,version TEXT,PRIMARY KEY(publication,page))",
        "CREATE TABLE IF NOT EXISTS wiki_legacy_retirements(document TEXT PRIMARY KEY,publish_id TEXT)",
        "CREATE TABLE IF NOT EXISTS wiki_source_heads(document TEXT PRIMARY KEY,revision TEXT,generation INTEGER)",
    )
    for statement in statements:
        db.execute(statement)
    db.execute('CREATE INDEX IF NOT EXISTS wiki_sources_document ON wiki_sources(document,revision)')
    db.execute('CREATE INDEX IF NOT EXISTS wiki_dependencies_page ON wiki_dependencies(page)')
    from agent.knowledge.catalog import ensure_schema as ensure_catalog
    ensure_catalog(db)
    # Existing successful wiki publications are sufficient evidence of the
    # ownership transfer, even if those pages have since been paused.
    db.execute('INSERT OR IGNORE INTO wiki_legacy_retirements SELECT s.document,s.publication FROM wiki_sources s JOIN wiki_publications p ON p.id=s.publication WHERE p.verified=1')
    db.execute('INSERT OR IGNORE INTO wiki_source_heads SELECT s.document,s.revision,1 FROM wiki_sources s JOIN wiki_publications p ON p.id=s.publication WHERE p.verified=1 ORDER BY p.rowid DESC')


def source_head(db, document):
    row = db.execute('SELECT revision,generation FROM wiki_source_heads WHERE document=?', (document,)).fetchone()
    return list(row) if row else None


def replaced(db, document, revision, service):
    # Wiki pause/withdrawal never transfers authority back to legacy writers.
    return legacy_retired(db, document)


def legacy_retired(db, document):
    return db.execute('SELECT 1 FROM wiki_legacy_retirements WHERE document=?', (document,)).fetchone() is not None


def suspend_sources(db, documents, reason):
    pages = set()
    for document in documents:
        pages.update(r[0] for r in db.execute(
            "SELECT p.id FROM wiki_pages p JOIN wiki_sources s ON s.publication=p.current WHERE s.document=?", (document,)))
    suspend_pages(db, pages, reason)


def suspend_pages(db, pages, reason):
    pending, visited = list(pages), set()
    while pending:
        page = pending.pop()
        if page in visited:
            continue
        visited.add(page)
        db.execute("UPDATE wiki_pages SET status='paused',reason=? WHERE id=?", (reason, page))
        pending.extend(r[0] for r in db.execute(
            "SELECT p.id FROM wiki_pages p JOIN wiki_dependencies d ON d.publication=p.current WHERE d.page=?", (page,)))


def _source(db, service, source, *, published=False):
    try:
        document, revision, parse_id, ordinal = (source[k] for k in ("document_id", "revision_id", "parse_id", "ordinal"))
        if not all(isinstance(value, str) and re.fullmatch(r"[\w-]+", value) for value in (document, revision, parse_id)):
            raise ValueError()
        if type(ordinal) is not int or ordinal < 0:
            raise ValueError()
        doc = service._document(db, document)
        row = db.execute("SELECT * FROM revisions WHERE id=? AND document=?", (revision, document)).fetchone()
        latest = db.execute("SELECT id FROM revisions WHERE document=? ORDER BY rowid DESC LIMIT 1", (document,)).fetchone()
        head = source_head(db, document)
        if published:
            allowed = {head[0]} if head else set()
        else:
            allowed = ({latest[0]} if latest else set()) | ({head[0]} if head else set())
        if not row or revision not in allowed or doc['purpose'] != 'long_term' or doc['validity'] != 'active':
            raise SourceError('version_conflict', '来源版本或用途已失效')
        record = json.loads(row['record'])
        if _hash(Path(record['original']).read_bytes()) != record['sha256']:
            raise SourceError('storage_failed', '原件哈希不符')
        raw = (service.root / 'parses' / f'{parse_id}.json').read_bytes()
        parsed = json.loads(raw)
        if any(parsed.get(k) != v for k, v in dict(document_id=document, revision_id=revision, parse_id=parse_id, sha256=record['sha256']).items()):
            raise ValueError()
        payload = {k: v for k, v in parsed.items() if k not in ('parse_id', 'revision_id', 'document_id', 'sha256')}
        if _hash(_json([record['sha256'], payload]).encode()) != parse_id:
            raise SourceError('storage_failed', '解析内容与解析版本不符')
        unit = parsed['units'][ordinal]
        if not unit['text'].strip() or not unit['location']:
            raise ValueError()
        result = dict(document_id=document, revision_id=revision, parse_id=parse_id, ordinal=ordinal,
                      location=unit['location'], filename=record['filename'], source_state_snapshot=doc,
                      text=unit['text'], parse_hash=_hash(raw))
        # Never trust model supplied locations, filenames or snapshots.
        return result
    except SourceError:
        raise
    except (KeyError, TypeError, ValueError, IndexError, OSError) as exc:
        raise SourceError('invalid_request', '出处不是有效的已登记解析单元') from exc


def _check_source(db, service, snapshot, *, published=False):
    current = _source(db, service, snapshot, published=published)
    if current != snapshot:
        raise SourceError('version_conflict', '来源依据快照已变化')


def _valid(db, service, publication, seen=None):
    if not publication:
        return False
    seen = set() if seen is None else set(seen)
    if publication['id'] in seen:
        return False
    seen.add(publication['id'])
    try:
        page = db.execute('SELECT * FROM wiki_pages WHERE id=?', (publication['page'],)).fetchone()
        if not page or page['current'] != publication['id'] or page['status'] != 'active' or not publication['verified']:
            return False
        record = json.loads(publication['record'])
        path = service.workspace / publication['path']
        if not path.resolve().is_relative_to(service.knowledge.resolve()):
            return False
        if _hash(path.read_bytes()) != publication['hash']:
            return False
        rows = db.execute('SELECT ordinal,text FROM wiki_units WHERE publication=? ORDER BY ordinal', (publication['id'],)).fetchall()
        if [(r[0], r[1]) for r in rows] != list(enumerate(b['content'] for b in record['blocks'])):
            return False
        sources = db.execute('SELECT record FROM wiki_sources WHERE publication=? ORDER BY ordinal', (publication['id'],)).fetchall()
        if [json.loads(r[0]) for r in sources] != record['sources']:
            return False
        for source in record['sources']:
            _check_source(db, service, source, published=True)
        dependencies = [(r[0], r[1]) for r in db.execute('SELECT page,version FROM wiki_dependencies WHERE publication=? ORDER BY page', (publication['id'],))]
        if dependencies != sorted((d['page_id'], d['page_version']) for d in record['depends_on']):
            return False
        for _, version in dependencies:
            upstream = db.execute('SELECT * FROM wiki_publications WHERE id=?', (version,)).fetchone()
            if not _valid(db, service, upstream, seen):
                return False
        return True
    except (SourceError, OSError, ValueError, KeyError, TypeError, IndexError):
        return False


def refresh(db, service, *, project=True):
    for pub in db.execute('SELECT v.* FROM wiki_publications v JOIN wiki_pages p ON p.current=v.id WHERE p.status=\'active\'').fetchall():
        if not _valid(db, service, pub):
            suspend_pages(db, [pub['page']], '来源、上游版本、正文或索引校验失效')
    if project:
        project_indexes(db, service)


def project_indexes(db, service):
    # These files are repairable projections. A read-only/full disk must not
    # roll back authoritative withdrawal or block fail-closed read guards.
    try:
        _project_indexes(db, service)
    except (OSError, UnicodeError, SourceError):
        from common.log import logger
        logger.warning('[KnowledgeWiki] 目录/日志投影刷新失败，权威有效状态仍生效，后续操作将重试')


def _project_indexes(db, service):
    """可重建的 wiki 目录/日志；保留未托管内容，暂停事实不留在投影中。"""
    records = []
    for pub in db.execute('SELECT v.* FROM wiki_publications v JOIN wiki_pages p ON p.current=v.id ORDER BY v.rowid'):
        if _valid(db, service, pub):
            from agent.knowledge.catalog import effective
            records.append(effective(db, json.loads(pub['record'])))
    # Avoid creating wiki artifacts in untouched legacy workspaces.
    if not records and not db.execute('SELECT 1 FROM wiki_publications LIMIT 1').fetchone():
        return
    service.knowledge.mkdir(parents=True, exist_ok=True)
    registered_paths = {row[0].removeprefix('knowledge/') for row in db.execute('SELECT path FROM wiki_publications')}
    for name in ('index.md', 'log.md'):
        path = service.knowledge / name
        if not path.resolve().is_relative_to(service.knowledge.resolve()):
            raise SourceError('forbidden', '知识目录投影路径越界')
        previous = path.read_text(encoding='utf-8') if path.exists() else ''
        start, end = '<!-- wiki-projection:start -->', '<!-- wiki-projection:end -->'
        content = re.sub(re.escape(start) + r'.*?' + re.escape(end) + r'\s*', '', previous, flags=re.S)
        if name == 'index.md':
            content = _strip_registered_index_entries(content, registered_paths)
        content = content.rstrip()
        lines = []
        category = None
        for record in sorted(records, key=lambda r: (r['category'], r['title'], r['page_id'])):
            title = _index_text(record['title'])
            path_ref = record['path'].removeprefix('knowledge/')
            if name == 'index.md':
                if category != record['category']:
                    category = record['category']
                    lines.append(f"## {_index_text(category)}")
                line = f"- [{title}](./{path_ref})"
                if record.get('summary'):
                    line += ' — ' + _index_text(record['summary'])
                lines.append(line)
            else:
                lines.append(f"## [{record.get('created_at', '')[:10]}] ingest | [{title}](./{path_ref})")
        generated = start + '\n' + '\n'.join(lines) + '\n' + end
        text = (content + '\n\n' if content else '') + generated + '\n'
        if text != previous:
            write_text_atomic(path, text, newline='')


def _strip_registered_index_entries(content, registered_paths):
    """接管旧生成器遗留的完整登记条目，保留普通说明及未托管链接。"""
    entry = re.compile(r'^\s*[-*+]\s+\[.*\]\(\s*<?([^\n()<>]+?)>?\s*\)(?:\s*(?:—|–|--?).*)?\s*$')
    lines = []
    for line in content.splitlines(keepends=True):
        match = entry.fullmatch(line.rstrip('\r\n'))
        if match:
            try:
                link = urlsplit(match[1])
                target = unquote(link.path).replace('\\', '/')
                if target.startswith('knowledge/'):
                    target = target[len('knowledge/'):]
                target = posixpath.normpath(target)
                if not link.scheme and not link.netloc and target in registered_paths:
                    continue
            except ValueError:
                pass
        lines.append(line)
    return ''.join(lines)


def _public_source(source):
    return {k: v for k, v in source.items() if k not in ('parse_hash',)}


def _ref(record, source_ordinal=0, db=None):
    if db is not None:
        from agent.knowledge.catalog import effective
        record = effective(db, record)
    sources = [dict(_public_source(s), source_ordinal=i) for i, s in enumerate(record['sources'])]
    main = sources[source_ordinal]
    return dict(**{k: main[k] for k in ('document_id', 'revision_id', 'parse_id', 'ordinal', 'location', 'filename', 'source_state_snapshot')},
                title=record['title'], summary=record.get('summary', ''), category=record['category'], category_mode=record.get('category_mode', 'auto'),
                category_revision=record.get('category_revision', 0), page_id=record['page_id'],
                page_version=record['page_version'], publish_id=record['page_version'], path=record['path'],
                sources=sources, related_page_ids=record['related_page_ids'], depends_on=record['depends_on'],
                task_id=record['task_id'], outcome='published', searchable=True, index_mode='keyword',
                indexed_units=len(record['blocks']), source_ordinal=source_ordinal, schema_version=1)


class WikiService:
    def __init__(self, workspace_root, owner_id='local-owner'):
        self.source = SourceService(workspace_root, owner_id)
        self.workspace = self.source.workspace
        self.root, self.db, self.knowledge = self.source.root, self.source.db, self.source.knowledge

    def _reconcile(self):
        # Persist detected invalidation even when the following operation fails.
        with self.source.transaction() as db:
            refresh(db, self.source)

    def prepare(self, pages, task_id=None):
        summaries = validate_page_summaries(pages)
        if task_id is not None and (not isinstance(task_id, str) or not task_id):
            raise SourceError('invalid_request', '任务身份无效')
        try:
            fingerprint = hashlib.sha256(json.dumps(pages, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        except (ValueError, TypeError) as exc:
            raise SourceError('invalid_request', '知识页不是可序列化的草稿') from exc
        self._reconcile()
        with self.source.transaction() as db:
            refresh(db, self.source)
            if task_id is not None:
                old = db.execute('SELECT record FROM wiki_batches WHERE task=? AND fingerprint=?', (task_id, fingerprint)).fetchone()
                if old:
                    return json.loads(old[0])
            batch_id, records, ids = uuid4().hex, [], set()
            for page, summary in zip(pages, summaries):
                if not isinstance(page, dict):
                    raise SourceError('invalid_request', '知识页必须为对象')
                page_id = page.get('page_id') or uuid4().hex
                if not isinstance(page_id, str) or not re.fullmatch(r'[\w-]+', page_id) or page_id in ids:
                    raise SourceError('invalid_request', '知识页身份无效或重复')
                ids.add(page_id)
                existing = db.execute('SELECT current FROM wiki_pages WHERE id=?', (page_id,)).fetchone()
                expected = page.get('expected_version')
                if (existing and ('expected_version' not in page or expected != existing[0])) or (not existing and expected is not None):
                    raise SourceError('version_conflict', '预期知识页版本不符')
                title, category, blocks = page.get('title'), page.get('category'), page.get('blocks')
                if not isinstance(title, str) or not title.strip() or '\n' in title or '\r' in title:
                    raise SourceError('invalid_request', '知识页标题无效')
                if not isinstance(category, str) or not re.fullmatch(r'[\w-]+(?:/[\w-]+)*', category):
                    raise SourceError('invalid_request', '知识页分类无效')
                if not isinstance(blocks, list) or not blocks:
                    raise SourceError('invalid_request', '知识页正文不能为空')
                source_map, normalized = {}, []
                for block in blocks:
                    if not isinstance(block, dict) or not isinstance(block.get('content'), str) or not block['content'].strip() or not isinstance(block.get('sources'), list) or not block['sources']:
                        raise SourceError('invalid_request', '每个正文块必须有内容及出处')
                    indices = []
                    for source in block['sources']:
                        snap = _source(db, self.source, source)
                        key = tuple(snap[k] for k in ('document_id', 'revision_id', 'parse_id', 'ordinal'))
                        if key not in source_map:
                            source_map[key] = (len(source_map), snap)
                        indices.append(source_map[key][0])
                    normalized.append(dict(content=block['content'], source_ordinals=sorted(set(indices))))
                related = page.get('related_page_ids', [])
                dependencies = page.get('depends_on', [])
                if not isinstance(related, list) or not all(isinstance(p, str) for p in related) or not isinstance(dependencies, list):
                    raise SourceError('invalid_request', '页面关系无效')
                deps = {}
                for dep in dependencies:
                    if not isinstance(dep, dict) or not isinstance(dep.get('page_id'), str) or not isinstance(dep.get('page_version'), str):
                        raise SourceError('invalid_request', '派生依赖无效')
                    if dep['page_id'] in deps:
                        raise SourceError('invalid_request', '派生依赖重复')
                    upstream = db.execute('SELECT * FROM wiki_publications WHERE id=? AND page=?', (dep['page_version'], dep['page_id'])).fetchone()
                    if not _valid(db, self.source, upstream):
                        raise SourceError('version_conflict', '上游页版本无效')
                    deps[dep['page_id']] = dep['page_version']
                version = uuid4().hex
                slug = re.sub(r'[^\w-]+', '-', title.strip().lower()).strip('-')[:60] or 'page'
                record = dict(page_id=page_id, page_version=version, expected_version=expected, title=title.strip(), summary=summary, category=category,
                              path=f'knowledge/wiki/{category}/{slug}-{version}.md', blocks=normalized,
                              sources=[s for _, s in source_map.values()], related_page_ids=list(dict.fromkeys(related)),
                              depends_on=[dict(page_id=p, page_version=v) for p, v in sorted(deps.items())], task_id=task_id,
                              created_at=datetime.now(timezone.utc).isoformat())
                record['source_heads'] = {s['document_id']: source_head(db, s['document_id']) for s in record['sources']}
                records.append(record)
            known = ids | {r[0] for r in db.execute('SELECT id FROM wiki_pages')}
            if any(p not in known for r in records for p in r['related_page_ids']):
                raise SourceError('invalid_request', '相关页面不存在')
            self._check_graph(db, records)
            result = dict(batch_id=batch_id, task_id=task_id, fingerprint=fingerprint, pages=records, stage='pending_index')
            if len(_json(result).encode()) > 20 * 1024 * 1024:
                raise SourceError('limit_exceeded', '整理成品超过20 MiB')
            db.execute('INSERT INTO wiki_batches VALUES(?,?,?,?,0)', (batch_id, task_id, fingerprint, _json(result)))
            return result

    @staticmethod
    def _check_graph(db, records):
        graph = {}
        for row in db.execute('SELECT p.id,d.page FROM wiki_pages p JOIN wiki_dependencies d ON d.publication=p.current'):
            graph.setdefault(row[0], set()).add(row[1])
        for record in records:
            graph[record['page_id']] = {d['page_id'] for d in record['depends_on']}
        visited, active = set(), set()

        def visit(page):
            if page in active:
                raise SourceError('invalid_request', '派生依赖形成循环')
            if page in visited:
                return
            active.add(page)
            for upstream in graph.get(page, ()):
                visit(upstream)
            active.remove(page)
            visited.add(page)

        for page in graph:
            visit(page)

    @staticmethod
    def _verify_index(db, record):
        rows = db.execute('SELECT ordinal,text FROM wiki_units WHERE publication=? ORDER BY ordinal', (record['page_version'],)).fetchall()
        if [(r[0], r[1]) for r in rows] != list(enumerate(b['content'] for b in record['blocks'])):
            raise SourceError('index_failed', '知识页关键词索引回读不一致')

    def publish(self, batch_id):
        self._reconcile()
        with self.source.transaction() as db:
            refresh(db, self.source)
            batch = db.execute('SELECT * FROM wiki_batches WHERE id=?', (batch_id,)).fetchone()
            if not batch:
                raise SourceError('invalid_request', '整理批次不存在')
            records = json.loads(batch['record'])['pages']
            if batch['published']:
                if any(not _valid(db, self.source, db.execute('SELECT * FROM wiki_publications WHERE id=?', (r['page_version'],)).fetchone()) for r in records):
                    raise SourceError('version_conflict', '该批次历史发布已失效')
                return [_ref(r, db=db) for r in records]
            self._check_graph(db, records)
            changed_ids = {r['page_id'] for r in records}
            chosen_revisions = {}
            for record in records:
                page = db.execute('SELECT current FROM wiki_pages WHERE id=?', (record['page_id'],)).fetchone()
                if (page[0] if page else None) != record['expected_version']:
                    raise SourceError('version_conflict', '知识页版本已变化')
                for source in record['sources']:
                    _check_source(db, self.source, source)
                    document, revision = source['document_id'], source['revision_id']
                    if document in record.get('source_heads', {}) and source_head(db, document) != record['source_heads'][document]:
                        raise SourceError('version_conflict', '来源已发布版本在整理期间变化')
                    if document in chosen_revisions and chosen_revisions[document] != revision:
                        raise SourceError('version_conflict', '同批次不能发布同一来源的不同版本')
                    chosen_revisions[document] = revision
                for dep in record['depends_on']:
                    if dep['page_id'] in changed_ids or not _valid(db, self.source, db.execute('SELECT * FROM wiki_publications WHERE id=? AND page=?', (dep['page_version'], dep['page_id'])).fetchone()):
                        raise SourceError('version_conflict', '上游页版本已变化；须基于新版本重新核验')
            # Immutable paths mean failed/crashed candidates never overwrite an
            # old effective page. Uncommitted files are rejected by guard_path.
            for record in records:
                from agent.knowledge.catalog import metadata
                organization = metadata(db, record)
                if organization['category_mode'] == 'manual':
                    record['category'] = organization['category']
                record['created_at'] = datetime.now(timezone.utc).isoformat()
                text = '# ' + record['title'] + '\n\n' + '\n\n'.join(b['content'] for b in record['blocks']) + '\n'
                target = self.workspace / record['path']
                if (not target.resolve().is_relative_to((self.knowledge / 'wiki').resolve())
                        or not target.resolve().is_relative_to(self.knowledge.resolve())):
                    raise SourceError('forbidden', '知识页路径越界')
                target.parent.mkdir(parents=True, exist_ok=True)
                write_text_atomic(target, text, newline='')
                digest = _hash(text.encode())
                if _hash(target.read_bytes()) != digest:
                    raise SourceError('storage_failed', '知识页落盘校验失败')
                version = record['page_version']
                for ordinal, block in enumerate(record['blocks']):
                    db.execute('INSERT INTO wiki_units VALUES(?,?,?)', (version, ordinal, block['content']))
                self._verify_index(db, record)
                db.execute('INSERT INTO wiki_publications VALUES(?,?,?,?,?,1,?)',
                           (version, record['page_id'], batch_id, record['path'], digest, _json(record)))
                for ordinal, source in enumerate(record['sources']):
                    db.execute('INSERT INTO wiki_sources VALUES(?,?,?,?,?)', (version, ordinal, source['document_id'], source['revision_id'], _json(source)))
                    db.execute('INSERT OR IGNORE INTO wiki_legacy_retirements VALUES(?,?)', (source['document_id'], version))
                for dep in record['depends_on']:
                    db.execute('INSERT INTO wiki_dependencies VALUES(?,?,?)', (version, dep['page_id'], dep['page_version']))
            for document, revision in chosen_revisions.items():
                head = source_head(db, document)
                if head and head[0] != revision:
                    suspend_sources(db, [document], '来源新版已成功发布')
                if not head or head[0] != revision:
                    db.execute('INSERT INTO wiki_source_heads VALUES(?,?,?) ON CONFLICT(document) DO UPDATE SET revision=excluded.revision,generation=excluded.generation',
                               (document, revision, head[1] + 1 if head else 1))
            suspend_pages(db, changed_ids, '上游知识页版本已更新')
            for record in records:
                from agent.knowledge.catalog import published
                published(db, record)
                db.execute("INSERT INTO wiki_pages VALUES(?,?,'active',NULL) ON CONFLICT(id) DO UPDATE SET current=excluded.current,status='active',reason=NULL", (record['page_id'], record['page_version']))
            for record in records:
                pub = db.execute('SELECT * FROM wiki_publications WHERE id=?', (record['page_version'],)).fetchone()
                if not _valid(db, self.source, pub):
                    raise SourceError('index_failed', '知识页发布提交前回读核验失败')
            db.execute('UPDATE wiki_batches SET published=1 WHERE id=?', (batch_id,))
            project_indexes(db, self.source)
            return [_ref(r, db=db) for r in records]

    def current(self, page_id):
        with self.source.transaction() as db:
            refresh(db, self.source)
            pub = db.execute('SELECT v.* FROM wiki_pages p JOIN wiki_publications v ON v.id=p.current WHERE p.id=?', (page_id,)).fetchone()
            return _ref(json.loads(pub['record']), db=db) if _valid(db, self.source, pub) else None

    def catalog(self):
        from agent.knowledge.catalog import catalog
        return catalog(self)

    def history(self, offset=0, limit=50):
        from agent.knowledge.catalog import history
        return history(self, offset, limit)

    def version(self, publish_id=None, *, path=None):
        from agent.knowledge.catalog import version
        return version(self, publish_id, path=path)

    def set_page_category(self, page_id, expected_version, expected_category_revision, category):
        from agent.knowledge.catalog import change
        return change(self, page_id, expected_version, expected_category_revision, category)

    def reset_page_category(self, page_id, expected_version, expected_category_revision):
        from agent.knowledge.catalog import change
        return change(self, page_id, expected_version, expected_category_revision, reset=True)

    def read_reference(self, publish_id, ordinal=0, historical=False):
        if type(ordinal) is not int or ordinal < 0:
            raise SourceError('invalid_request', '出处下标无效')
        self._reconcile()
        with self.source.transaction() as db:
            refresh(db, self.source)
            pub = db.execute('SELECT * FROM wiki_publications WHERE id=?', (publish_id,)).fetchone()
            if not pub:
                raise SourceError('invalid_request', '知识页引用不存在')
            from agent.knowledge.catalog import verify_archive
            record, _ = verify_archive(db, self.source, pub)
            valid = _valid(db, self.source, pub)
            if not valid and not historical:
                raise SourceError('version_conflict', '知识页已暂停、替代或校验失效')
            if ordinal >= len(record['sources']):
                raise SourceError('invalid_request', '出处下标不存在')
            source = _public_source(record['sources'][ordinal])
            revision = json.loads(db.execute('SELECT record FROM revisions WHERE id=?', (source['revision_id'],)).fetchone()[0])
            original = revision['original']
            if _hash(Path(original).read_bytes()) != revision['sha256']:
                raise SourceError('storage_failed', '原件哈希不符')
            page = db.execute('SELECT * FROM wiki_pages WHERE id=?', (record['page_id'],)).fetchone()
            result = _ref(record, db=db) if valid else _ref(record)
            result.update(source)
            result.update(source_ordinal=ordinal, original=original, valid=valid, searchable=valid,
                          current_status='current' if valid else ('superseded' if page['current'] != publish_id else 'paused'),
                          pause_reason=page['reason'], blocks=record['blocks'], content='\n\n'.join(b['content'] for b in record['blocks']))
            return result

    def references(self, publish_id):
        ref = self.read_reference(publish_id, historical=True)
        with self.source.transaction() as db:
            sources = []
            for i, source in enumerate(ref['sources']):
                current_state = self.source._document(db, source['document_id'])
                head = source_head(db, source['document_id'])
                status = current_state['validity'] if current_state['validity'] != 'active' else (
                    'current' if current_state == source['source_state_snapshot'] and head and head[0] == source['revision_id'] else 'superseded_or_changed')
                sources.append(dict(source, source_ordinal=i, valid=ref['valid'], current_status=ref['current_status'],
                                    source_state=current_state, source_status=status))
            return sources

    def search(self, query, limit=10):
        if limit <= 0:
            return []
        tokens = re.findall(r'[a-zA-Z0-9_]+', query.lower())
        for run in re.findall(r'[\u4e00-\u9fff]+', query):
            tokens.extend(run[i:i+2] for i in range(len(run)-1))
        tokens = sorted(set(tokens)) or [query.lower()]
        with self.source.transaction() as db:
            refresh(db, self.source)
            rows = db.execute("SELECT u.*,v.record FROM wiki_units u JOIN wiki_publications v ON v.id=u.publication JOIN wiki_pages p ON p.current=v.id WHERE p.status='active'").fetchall()
            matches = []
            for row in rows:
                score = sum(t in row['text'].lower() for t in tokens)
                if not score:
                    continue
                record = json.loads(row['record'])
                ref = _ref(record, db=db)
                source_ordinals = record['blocks'][row['ordinal']]['source_ordinals']
                main = record['sources'][source_ordinals[0]]
                ref.update({k: main[k] for k in ('document_id', 'revision_id', 'parse_id', 'ordinal', 'location', 'filename', 'source_state_snapshot')})
                ref.update(text=row['text'], block_ordinal=row['ordinal'], source_ordinals=source_ordinals, source_ordinal=source_ordinals[0])
                matches.append((score, ref))
            return [ref for _, ref in sorted(matches, key=lambda m: -m[0])[:limit]]
