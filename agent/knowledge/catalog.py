"""知识页组织元数据及只读发布历史；与内容版本独立提交。"""
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from agent.knowledge.parser import SourceError
from agent.knowledge.personal_assistant import _hash, _json


def ensure_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS wiki_categories(page TEXT PRIMARY KEY,category TEXT,mode TEXT,revision INTEGER)')
    db.execute('CREATE TABLE IF NOT EXISTS wiki_category_events(id TEXT PRIMARY KEY,record TEXT)')


def metadata(db, record):
    row = db.execute('SELECT category,mode,revision FROM wiki_categories WHERE page=?', (record['page_id'],)).fetchone()
    return dict(category=row[0] if row else record['category'], category_mode=row[1] if row else 'auto',
                category_revision=row[2] if row else 0)


def effective(db, record):
    return dict(record, **metadata(db, record))


def published(db, record):
    """在发布事务内采纳自动建议，手动锁定始终保留。"""
    previous = db.execute('SELECT v.record FROM wiki_pages p JOIN wiki_publications v ON v.id=p.current WHERE p.id=?', (record['page_id'],)).fetchone()
    old = metadata(db, json.loads(previous[0]) if previous else record)
    exists = db.execute('SELECT 1 FROM wiki_categories WHERE page=?', (record['page_id'],)).fetchone()
    if old['category_mode'] == 'manual':
        return
    revision = old['category_revision'] + int(bool(exists or previous) and old['category'] != record['category'])
    db.execute('INSERT INTO wiki_categories VALUES(?,?,?,?) ON CONFLICT(page) DO UPDATE SET category=excluded.category,revision=excluded.revision',
               (record['page_id'], record['category'], 'auto', revision))


def change(wiki, page_id, expected_version, expected_category_revision, category=None, *, reset=False):
    from agent.knowledge.wiki import refresh, _valid, _ref, project_indexes
    if (not isinstance(page_id, str) or not page_id or not isinstance(expected_version, str)
            or type(expected_category_revision) is not int or expected_category_revision < 0):
        raise SourceError('invalid_request', '知识页及双修订参数无效')
    if not reset and (not isinstance(category, str) or len(category) > 256
                      or not re.fullmatch(r'[\w-]+(?:/[\w-]+)*', category)):
        raise SourceError('invalid_request', '知识页分类无效')
    wiki._reconcile()
    with wiki.source.transaction() as db:
        refresh(db, wiki.source)
        pub = db.execute('SELECT v.* FROM wiki_pages p JOIN wiki_publications v ON v.id=p.current WHERE p.id=?', (page_id,)).fetchone()
        if not _valid(db, wiki.source, pub):
            raise SourceError('version_conflict', '知识页不是当前有效对象')
        record = json.loads(pub['record'])
        old = metadata(db, record)
        if pub['id'] != expected_version or old['category_revision'] != expected_category_revision:
            raise SourceError('version_conflict', '知识页版本或分类修订已变化')
        new = dict(category=old['category'] if reset else category, category_mode='auto' if reset else 'manual',
                   category_revision=old['category_revision'] + 1)
        db.execute('INSERT INTO wiki_categories VALUES(?,?,?,?) ON CONFLICT(page) DO UPDATE SET category=excluded.category,mode=excluded.mode,revision=excluded.revision',
                   (page_id, new['category'], new['category_mode'], new['category_revision']))
        event = dict(id=uuid4().hex, event_type='category_auto' if reset else 'category_changed',
                     created_at=datetime.now(timezone.utc).isoformat(), page_id=page_id, page_version=pub['id'],
                     publish_id=pub['id'], title=record['title'], **new)
        db.execute('INSERT INTO wiki_category_events VALUES(?,?)', (event['id'], _json(event)))
        project_indexes(db, wiki.source)
        return _ref(record, db=db)


def catalog(wiki):
    from agent.knowledge.wiki import refresh
    with wiki.source.transaction() as db:
        refresh(db, wiki.source, project=False)
        pages = _current_pages(wiki, db)
        return dict(pages=pages, categories=sorted({p['category'] for p in pages}),
                    legacy_content=_index_legacy_content(wiki, db))


def _current_pages(wiki, db):
    from agent.knowledge.wiki import _valid, _ref
    pages = []
    for pub in db.execute('SELECT v.* FROM wiki_pages p JOIN wiki_publications v ON v.id=p.current'):
        if _valid(db, wiki.source, pub):
            ref = _ref(json.loads(pub['record']), db=db)
            ref['path'] = Path(wiki.workspace / pub['path']).relative_to(wiki.knowledge).as_posix()
            pages.append(ref)
    pages.sort(key=lambda p: (p['category'], p['title'], p['page_id']))
    return pages


@contextmanager
def browse_snapshot(knowledge_root):
    """一次树/图遍历共享权威事务；不跨请求缓存来源有效性。"""
    root = Path(knowledge_root).resolve()
    if not (root.parent / '.personal_sources' / 'sources.sqlite3').exists():
        yield None
        return
    from agent.knowledge.wiki import WikiService, refresh
    wiki = WikiService(root.parent)
    with wiki.source.transaction() as db:
        # 保留原浏览守卫的失效检测及目录恢复，但每次遍历只做一次。
        refresh(db, wiki.source)
        yield _BrowseSnapshot(wiki, db)


class _BrowseSnapshot:
    def __init__(self, wiki, db):
        self.wiki, self.db = wiki, db
        self.pages = {p['path']: p for p in _current_pages(wiki, db)}

    def guard(self, path):
        """与单文件守卫相同的发布资格，复用本次事务及已核验目录。"""
        from agent.knowledge.personal_assistant import guard_path
        path = Path(path).resolve()
        root = self.wiki.knowledge.resolve()
        if ('.personal_sources' in path.parts or not path.is_relative_to(root)
                or any(p.name == 'knowledge' for p in path.parents if p != root and p.is_relative_to(root))):
            return guard_path(path)
        rel = path.relative_to(root).as_posix()
        if rel in self.pages:
            return
        registered = self.db.execute(
            'SELECT 1 FROM wiki_publications WHERE path=?', ('knowledge/' + rel,)).fetchone()
        pub = self.db.execute('SELECT * FROM publications WHERE path=?', (str(path),)).fetchone()
        if pub and self.wiki.source._valid(self.wiki.source._document(self.db, pub['document']), pub, self.db):
            return
        if registered or pub or rel.split('/')[0] in {'wiki', 'managed'}:
            raise SourceError('version_conflict', '托管页须通过来源发布入口处理；当前页无有效发布或禁止直接写入')

    def presentation(self, path):
        path = Path(path).resolve()
        rel = path.relative_to(self.wiki.knowledge.resolve()).as_posix()
        if rel in self.pages:
            page = self.pages[rel]
            return page['title'], page['category']
        if rel.startswith('managed/'):
            rows = self.db.execute(
                'SELECT p.path,r.filename FROM publications p JOIN revisions r ON r.id=p.revision')
            for row in rows:
                if Path(row[0]).resolve() == path:
                    return Path(row[1]).stem, '资料原文'
            return '资料原文', '资料原文'
        return None


def _index_legacy_content(wiki, db):
    """只读提取未托管说明，不把可重建目录或空模板当作旧内容。"""
    from agent.knowledge.wiki import _strip_registered_index_entries
    index = wiki.knowledge / 'index.md'
    if not index.exists():
        return ''
    if not index.resolve().is_relative_to(wiki.knowledge.resolve()):
        raise SourceError('forbidden', '知识索引路径越界')
    content = index.read_text(encoding='utf-8')
    for marker in ('wiki-projection', 'knowledge-index'):
        content = re.sub(r'<!--\s*' + marker + r':start\s*-->.*?<!--\s*' + marker + r':end\s*-->\s*', '', content, flags=re.S)
    paths = {row[0].removeprefix('knowledge/') for row in db.execute('SELECT path FROM wiki_publications')}
    content = _strip_registered_index_entries(content, paths).strip()
    if all(line.strip() in {'', '# 知识库目录', '# 知识索引', '# 知识目录', '# Knowledge Index', '## wiki'}
           for line in content.splitlines()):
        return ''
    return content


def status(db, source, pub):
    from agent.knowledge.wiki import _valid
    valid = _valid(db, source, pub)
    page = db.execute('SELECT current FROM wiki_pages WHERE id=?', (pub['page'],)).fetchone()
    return dict(current_status='current' if valid else ('superseded' if page and page[0] != pub['id'] else 'paused'), searchable=valid)


def verify_archive(db, source, pub):
    """核验不可变证据，不要求历史来源仍有当前召回资格。"""
    try:
        if not pub or not pub['verified']:
            raise ValueError()
        record = json.loads(pub['record'])
        from agent.knowledge.wiki import publication_markdown
        if _hash(publication_markdown(record).encode()) != pub['hash']:
            raise ValueError()
        path = source.workspace / pub['path']
        if not path.resolve().is_relative_to(source.knowledge.resolve()) or _hash(path.read_bytes()) != pub['hash']:
            raise ValueError()
        if record['page_id'] != pub['page'] or record['page_version'] != pub['id'] or record['path'] != pub['path']:
            raise ValueError()
        sources = [json.loads(r[0]) for r in db.execute('SELECT record FROM wiki_sources WHERE publication=? ORDER BY ordinal', (pub['id'],))]
        if not sources or sources != record['sources']:
            raise ValueError()
        for snap in sources:
            row = db.execute('SELECT record FROM revisions WHERE id=? AND document=?', (snap['revision_id'], snap['document_id'])).fetchone()
            revision = json.loads(row[0])
            if _hash(Path(revision['original']).read_bytes()) != revision['sha256']:
                raise ValueError()
            if not re.fullmatch(r'[\w-]+', snap['parse_id']):
                raise ValueError()
            raw = (source.root / 'parses' / (snap['parse_id'] + '.json')).read_bytes()
            parsed = json.loads(raw)
            payload = {k: v for k, v in parsed.items() if k not in ('parse_id', 'revision_id', 'document_id', 'sha256')}
            if (_hash(raw) != snap['parse_hash'] or _hash(_json([revision['sha256'], payload]).encode()) != snap['parse_id']
                    or any(parsed[k] != value for k, value in dict(document_id=snap['document_id'], revision_id=snap['revision_id'], parse_id=snap['parse_id'], sha256=revision['sha256']).items())):
                raise ValueError()
            unit = parsed['units'][snap['ordinal']]
            if unit['text'] != snap['text'] or unit['location'] != snap['location'] or revision['filename'] != snap['filename']:
                raise ValueError()
            if 'reading_provenance' in unit:
                from agent.knowledge.reading import verify_reading_unit
                metadata = verify_reading_unit(db, source, unit, snap)
                if any(snap.get(key) != value for key, value in metadata.items()):
                    raise ValueError()
        from agent.knowledge.figures import validate_manifest
        validate_manifest(db, source, record, check_bytes=False)
        return record, path.read_text(encoding='utf-8')
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise SourceError('storage_failed', '历史发布正文或出处核验失败') from exc


def _publication_path(wiki, path):
    """规范化知识根相对路径；不做 URL 二次解码或文件名回退。"""
    path = path.replace('\\', '/')
    if path.startswith('/') or re.search(r'[:\x00-\x1f]', path):
        raise SourceError('invalid_request', '历史路径必须位于知识根内')
    parts = []
    for part in path.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if not parts:
                raise SourceError('invalid_request', '历史路径越界')
            parts.pop()
        else:
            parts.append(part)
    if not parts:
        raise SourceError('invalid_request', '历史路径无效')
    target = wiki.knowledge.joinpath(*parts)
    if not target.resolve().is_relative_to(wiki.knowledge.resolve()):
        raise SourceError('invalid_request', '历史路径越界')
    return (wiki.knowledge.relative_to(wiki.workspace) / '/'.join(parts)).as_posix()


def version(wiki, publish_id=None, *, path=None):
    from agent.knowledge.wiki import refresh, figure_state
    if (any(value is not None and not isinstance(value, str) for value in (publish_id, path))
            or bool(publish_id) == bool(path)):
        raise SourceError('invalid_request', 'publish_id 与 path 必须二选一')
    registered_path = _publication_path(wiki, path) if path else None
    wiki._reconcile()
    with wiki.source.transaction() as db:
        refresh(db, wiki.source)
        if registered_path is not None:
            pub = db.execute('SELECT * FROM wiki_publications WHERE path=?', (registered_path,)).fetchone()
        else:
            pub = db.execute('SELECT * FROM wiki_publications WHERE id=?', (publish_id,)).fetchone()
        if not pub:
            raise SourceError('invalid_request', '知识发布不存在')
        record, content = verify_archive(db, wiki.source, pub)
        result = dict(page_id=record['page_id'], page_version=pub['id'], publish_id=pub['id'], title=record['title'], summary=record.get('summary', ''),
                      category=record['category'], path=Path(wiki.workspace / pub['path']).relative_to(wiki.knowledge).as_posix(), content=content, created_at=record.get('created_at'), **status(db, wiki.source, pub))
        result['figure_state'] = figure_state(record)
        if 'figure_manifest' in record:
            result['figure_manifest'] = record['figure_manifest']
        if result['searchable']:
            result.update(metadata(db, record))
        current = db.execute('SELECT v.* FROM wiki_pages p JOIN wiki_publications v ON v.id=p.current WHERE p.id=?', (pub['page'],)).fetchone()
        if current and status(db, wiki.source, current)['searchable']:
            result['current_path'] = Path(wiki.workspace / current['path']).relative_to(wiki.knowledge).as_posix()
        return result


def history(wiki, offset=0, limit=50):
    from agent.knowledge.wiki import refresh
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 200:
        raise SourceError('invalid_request', '分页参数无效')
    with wiki.source.transaction() as db:
        refresh(db, wiki.source)
        events, seen = [], set()
        publications = {p['id']: p for p in db.execute('SELECT * FROM wiki_publications WHERE verified=1 ORDER BY rowid')}
        for pub in publications.values():
            record = json.loads(pub['record'])
            kind = 'updated' if pub['page'] in seen else 'published'
            seen.add(pub['page'])
            if record.get('created_at'):
                events.append(dict(id=pub['id'], event_type=kind, created_at=record['created_at'], page_id=pub['page'],
                                   page_version=pub['id'], publish_id=pub['id'], title=record['title'], summary=record.get('summary', ''), category=record['category'], **status(db, wiki.source, pub)))
        for row in db.execute('SELECT record FROM wiki_category_events ORDER BY rowid'):
            event = json.loads(row[0])
            pub = publications.get(event['publish_id'])
            if pub:
                events.append(dict(event, summary=json.loads(pub['record']).get('summary', ''), **status(db, wiki.source, pub)))
        events.sort(key=lambda e: e['created_at'], reverse=True)
        log = wiki.knowledge / 'log.md'
        legacy = log.read_text(encoding='utf-8') if log.exists() else ''
        legacy = re.sub(r'<!-- wiki-projection:start -->.*?<!-- wiki-projection:end -->\s*', '', legacy, flags=re.S)
        return dict(events=events[offset:offset + limit], total=len(events), has_more=offset + limit < len(events), legacy_content=legacy)
