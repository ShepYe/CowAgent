"""图文任务的固定目标、跨库回执与最终完成屏障（台账 v1）。

只协调可信 Wiki 候选；图片身份及字节校验由 figures 模块负责。
台账使用相对 workspace，重启先对账稳定 batch_id，再仅补当前缺图。
"""
import json
import os
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import Context
from pathlib import Path

from common.personal_tasks import TaskError, _json, now
from agent.knowledge.reading_publication import _candidate, _current, _wiki, MAX_RECORD_BYTES

MAX_BATCHES = 256
MAX_TARGETS = 4096


def _require_current_config(task):
    from models.config_revision import require_config_revision
    # 新上下文同时排除 use_config_revision 的 pinned 修订和 config_scope 快照。
    Context().run(require_config_revision, task['config_revision'])


@contextmanager
def _current_config_guard(task):
    from config import config_lock
    # config_lock 非重入；持锁后直接比较同一算法计算的真实全局快照，
    # 不再调用内部会重复获取该锁的 require_config_revision。
    with config_lock:
        _check_current_config_locked(task)
        yield


def _check_current_config_locked(task):
    from models.config_revision import ConfigUnavailable, _revision, _snapshot
    if task['config_revision'] != _revision(Context().run(_snapshot)):
        raise ConfigUnavailable()


def publish_candidate(wiki, batch_id, task):
    """发布受信图文候选；调用方已持任务锁并核验候选身份。"""
    _require_current_config(task)
    with _current_config_guard(task):
        return wiki.publish(batch_id)


def _load(db, task):
    if 'figure_publication' not in task:
        return None
    if (task['figure_publication'] != {'schema_version': 1}
            or type(task['figure_publication'].get('schema_version')) is not int) or db.execute('PRAGMA user_version').fetchone()[0] != 3:
        raise TaskError('version_conflict', '不支持的补图任务版本')
    try:
        row = db.execute('SELECT record FROM figure_publications WHERE task_id=?', (task['task_id'],)).fetchone()
        ledger = json.loads(row[0])
        if (type(ledger['schema_version']) is not int or ledger['schema_version'] != 1 or ledger['task_id'] != task['task_id']
                or not isinstance(ledger['targets'], dict) or not isinstance(ledger['batches'], dict)
                or len(ledger['targets']) > MAX_TARGETS or len(ledger['batches']) > MAX_BATCHES):
            raise ValueError()
        for key, target in ledger['targets'].items():
            if key != _key(target['workspace'], target['page_id']) or not isinstance(target['selection'], list):
                raise ValueError()
            _manifest(target['manifest'])
        for batch in ledger['batches'].values():
            if (batch['status'] not in {'prepared', 'published'} or not batch['batch_id']
                    or not batch['workspace'] or not isinstance(batch['refs'], list)):
                raise ValueError()
        return ledger
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise TaskError('version_conflict', '补图台账损坏或版本未知') from exc


def _save(db, ledger):
    body = _json(ledger)
    if len(body.encode('utf-8')) > MAX_RECORD_BYTES:
        raise TaskError('limit_exceeded', '补图台账超过上限')
    db.execute('INSERT OR REPLACE INTO figure_publications VALUES(?,?)', (ledger['task_id'], body))


def _key(workspace, page_id):
    return _json([workspace, page_id])


def _manifest(manifest):
    if (not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int or manifest.get('schema_version') != 1
            or not isinstance(manifest.get('items'), list)
            or any(not isinstance(i, dict) or i.get('state') not in {'ready', 'pending'} for i in manifest['items'])):
        raise TaskError('version_conflict', '图片清单版本未知或损坏')
    return manifest


def figure_required(task):
    requirement = task.get('payload', {}).get('figure_requirement')
    if requirement is None and 'figure_requirement' not in task.get('payload', {}):
        return False
    if (not isinstance(requirement, dict) or type(requirement.get('schema_version')) is not int
            or requirement['schema_version'] != 1):
        raise TaskError('version_conflict', '不支持的图文任务要求版本')
    if 'page_ids' in requirement:
        pages = requirement['page_ids']
        if (not isinstance(pages, list) or not pages or len(pages) > MAX_TARGETS
                or any(not isinstance(p, str) or not p or len(p) > 512 for p in pages)
                or len(set(pages)) != len(pages)):
            raise TaskError('invalid_request', '受信图文页目标无效')
    return True


def require_explicit_figures(task, pages):
    """旧调用保持兼容；首次显式 figures 后，同任务所有新提交均须显式。"""
    active = figure_required(task) or 'figure_publication' in task or any(
        'figures' in block for page in pages for block in page.get('blocks', []))
    if active and any('figures' not in block for page in pages for block in page.get('blocks', [])):
        raise TaskError('invalid_request', '图文任务每个正文块须显式提交 figures')


def _selection(record):
    # 保留模型固定输入，不把派生文件身份和 ready/pending 纳入选图身份。
    selection = [dict(block_ordinal=i, figures=[]) for i in range(len(record['blocks']))]
    for item in _manifest(record['figure_manifest'])['items']:
        figure = {k: item[k] for k in ('figure_key', 'caption', 'source', 'region')}
        source = figure['source']
        snapshot = next((s for s in record['sources'] if all(
            s.get(k) == source.get(k) for k in ('document_id', 'revision_id', 'parse_id', 'ordinal'))), None)
        if snapshot and snapshot.get('location') is not None:
            # 真实重核可更新解析身份，原件修订与实际位置保持固定。
            figure['source'] = {k: snapshot[k] for k in ('document_id', 'revision_id', 'location')}
        selection[item['block_ordinal']]['figures'].append(figure)
    return selection


def register_batch(store, task, submission_id, batch_id, workspace):
    """Wiki.prepare 后、publish 前调用；显式空清单同样启用契约。"""
    from agent.knowledge.wiki import WikiService
    if not isinstance(submission_id, str) or not submission_id or len(submission_id) > 256:
        raise TaskError('invalid_request')
    with store.commit_guard(task['owner_id'], task['task_id'], task['attempt']):
        wiki = WikiService(workspace, task['owner_id'])
        candidate, _ = _candidate(wiki, task, batch_id)
        location = os.path.relpath(Path(workspace).resolve(), store.root)
        if any('figure_manifest' in r for r in candidate['pages']):
            with wiki.source.transaction() as source_db:
                if source_db.execute('PRAGMA user_version').fetchone()[0] != 3:
                    raise TaskError('version_conflict', '来源与任务库均须就绪后登记补图任务')
        with store._transaction() as db:
            current = _current(store, db, task)
            ledger = _load(db, current)
            explicit = any('figure_manifest' in r for r in candidate['pages'])
            if ledger is None and not explicit:
                if figure_required(current):
                    raise TaskError('invalid_request', '受信图文任务须显式提供 figures')
                return None
            # 已持久化的旧候选可恢复，新图文候选不可漏交清单。
            if any('figure_manifest' not in r for r in candidate['pages']):
                raise TaskError('invalid_request', '图文任务须显式提供 figures')
            if ledger is None:
                ledger = dict(schema_version=1, task_id=task['task_id'], created_at=now(), targets={}, batches={})
            old = ledger['batches'].get(submission_id)
            if old:
                if old['batch_id'] != batch_id or old['workspace'] != location or old['fingerprint'] != candidate['fingerprint']:
                    raise TaskError('idempotency_conflict')
                return old
            if len(ledger['batches']) >= MAX_BATCHES:
                raise TaskError('limit_exceeded')
            for record in candidate['pages']:
                manifest = _manifest(record['figure_manifest'])
                key = _key(location, record['page_id'])
                target = ledger['targets'].get(key)
                selection = _selection(record)
                if target and target['selection'] != selection:
                    raise TaskError('idempotency_conflict', '已固定的选图目标不可缩小或替换')
                if not target:
                    if len(ledger['targets']) >= MAX_TARGETS:
                        raise TaskError('limit_exceeded')
                    ledger['targets'][key] = dict(workspace=location, page_id=record['page_id'],
                        selection=selection, manifest=manifest, preparation=record,
                        published_version=None, gaps=['unpublished'])
            batch = dict(batch_id=batch_id, workspace=location, fingerprint=candidate['fingerprint'],
                         attempt=task['attempt'], status='prepared', refs=[], created_at=now())
            ledger['batches'][submission_id] = batch
            db.execute('PRAGMA user_version=3')
            current['figure_publication'] = {'schema_version': 1}
            _save(db, ledger)
            store._save(db, current)
            return batch


def _state(store, task):
    with store._transaction() as db:
        current = store._read(db, task['owner_id'], task['task_id'])
        return _load(db, current)


def reconcile_committed(store, task):
    """仅对账已提交结果，取消时也安全；调用方持任务锁。"""
    from agent.knowledge.wiki import _ref
    state = _state(store, task)
    if state is None:
        return
    for sid, batch in state['batches'].items():
        if batch['status'] == 'published':
            continue
        wiki = _wiki(store, batch, task['owner_id'])
        candidate, committed = _candidate(wiki, task, batch['batch_id'])
        if candidate['fingerprint'] != batch['fingerprint']:
            raise TaskError('version_conflict')
        if not committed:
            continue
        with wiki.source.transaction() as source_db:
            refs = [_ref(r, db=source_db) for r in candidate['pages']]
        with store._transaction() as db:
            current = _current(store, db, task, ('queued', 'running', 'waiting_input'))
            ledger = _load(db, current)
            ledger['batches'][sid].update(status='published', refs=refs)
            for record in candidate['pages']:
                target = ledger['targets'][_key(batch['workspace'], record['page_id'])]
                target.update(published_version=record['page_version'], manifest=record['figure_manifest'],
                              preparation=record, gaps=[])
            for ref in refs:
                if ref not in current['result_refs']:
                    current['result_refs'].append(ref)
            _save(db, ledger)
            store._save(db, current)


def _inspect(store, task, ledger, sources=None):
    from agent.knowledge.wiki import _valid
    from agent.knowledge.figures import validate_manifest
    from agent.knowledge.personal_assistant import SourceError
    remaining, paused = [], []
    for key, target in ledger['targets'].items():
        held = sources[target['workspace']] if sources is not None else None
        wiki = held[0] if held else _wiki(store, target, task['owner_id'])
        gaps = []
        transaction = nullcontext(held[1]) if held else wiki.source.transaction()
        with transaction as db:
            row = db.execute('SELECT v.* FROM wiki_pages p JOIN wiki_publications v ON v.id=p.current WHERE p.id=?',
                             (target['page_id'],)).fetchone()
            if not row or row['id'] != target['published_version'] or not _valid(db, wiki.source, row):
                gaps.append('publication_invalid')
            else:
                record = json.loads(row['record'])
                manifest = _manifest(record.get('figure_manifest'))
                if _selection(record) != target['selection'] or manifest != target['manifest']:
                    gaps.append('manifest_changed')
                else:
                    try:
                        validate_manifest(db, wiki.source, record, check_bytes=True)
                    except SourceError as exc:
                        if exc.code == 'version_conflict':
                            raise TaskError('version_conflict', str(exc)) from exc
                        gaps.append('unavailable')
                    if any(i['state'] == 'pending' for i in manifest['items']):
                        gaps.append('pending')
            downstream, frontier = set(), [target['page_id']]
            while frontier:
                upstream = frontier.pop()
                for dep in db.execute('SELECT p.id FROM wiki_pages p JOIN wiki_dependencies d ON d.publication=p.current WHERE d.page=?', (upstream,)):
                    if dep[0] not in downstream:
                        downstream.add(dep[0])
                        frontier.append(dep[0])
            for dep in db.execute("SELECT id,current,reason FROM wiki_pages WHERE status!='active'"):
                if dep['id'] in downstream and _key(target['workspace'], dep['id']) not in ledger['targets']:
                    item = dict(workspace=target['workspace'], page_id=dep['id'], page_version=dep['current'], reason=dep['reason'])
                    if item not in paused:
                        paused.append(item)
        target['gaps'] = gaps
        if gaps:
            remaining.append(key)
    ledger['paused_outside_targets'] = paused
    return remaining


def publication_state(store, task, sources=None):
    with store.lock:
        ledger = _state(store, task)
        if ledger is None:
            if figure_required(task):
                return dict(schema_version=1, task_id=task['task_id'], targets={}, batches={},
                            remaining=['selection_required'], paused_outside_targets=[])
            return None
        remaining = _inspect(store, task, ledger, sources)
        if figure_required(task):
            required = set(task['payload']['figure_requirement'].get('page_ids', []))
            covered = {t['page_id'] for t in ledger['targets'].values()}
            remaining.extend('page_required:' + p for p in sorted(required - covered))
        with store._transaction() as db:
            _save(db, ledger)
        return dict(ledger, remaining=remaining)


@contextmanager
def completion_guard(store, task):
    """任务锁先于源库；最终核验到调用方提交完成之间保留全部 SQLite 写锁。"""
    with store.commit_guard(task['owner_id'], task['task_id'], task['attempt']) as current:
        _require_current_config(current)
        reconcile_committed(store, current)
        ledger = _state(store, current)
        # WikiService 初始化也会打开源事务，须在取得任何源库写锁前全部完成。
        workspaces = sorted({t['workspace'] for t in ledger['targets'].values()}) if ledger else []
        wikis = {workspace: _wiki(store, {'workspace': workspace}, current['owner_id'])
                 for workspace in workspaces}
        with ExitStack() as stack:
            # 与 _publish 一致：任务锁 → 配置锁 → 按序源库写锁。
            stack.enter_context(_current_config_guard(current))
            sources = {workspace: (wikis[workspace], stack.enter_context(wikis[workspace].source.transaction()))
                       for workspace in workspaces}
            state = publication_state(store, current, sources)
            if state is not None and (state['remaining'] or any(
                    b['status'] != 'published' for b in state['batches'].values())):
                raise TaskError('figure_incomplete', '正文已发布，待补图或重核依赖')
            _check_current_config_locked(current)
            yield


def check_complete(store, task):
    """兼容预检入口；提交成功终态必须放在 completion_guard 内。"""
    with completion_guard(store, task):
        pass


def resume_publication(store, task):
    """有界恢复：对账、接续持久候选，再按依赖顺序只补缺图。"""
    from agent.knowledge.wiki import _valid
    with store.commit_guard(task['owner_id'], task['task_id'], task['attempt']):
        _require_current_config(task)
        reconcile_committed(store, task)
        state = _state(store, task)
        if state is None:
            return store.get(task['owner_id'], task['task_id'])
        for sid, batch in state['batches'].items():
            if batch['status'] == 'prepared':
                _publish(store, task, sid, batch)
        state = publication_state(store, task)
        # remaining 也含 page_required 缺页标记；缺页须由主 Agent 整理，
        # 恢复器只对台账已有目标补图，不能把缺口标记当成目标键。
        pending = {key for key in state['targets'] if key in state['remaining']}
        ordered = []
        def visit(key, visiting):
            if key in ordered:
                return
            if key in visiting:
                raise TaskError('version_conflict', '补图依赖成环')
            target = state['targets'][key]
            for dep in target['preparation'].get('depends_on', []):
                upstream = _key(target['workspace'], dep['page_id'])
                if upstream in pending:
                    visit(upstream, visiting | {key})
            ordered.append(key)
        for key in sorted(pending):
            visit(key, set())
        for key in ordered:
            target = state['targets'][key]
            wiki = _wiki(store, target, task['owner_id'])
            with wiki.source.transaction() as db:
                row = db.execute('SELECT * FROM wiki_publications WHERE id=?', (target['published_version'],)).fetchone()
                valid = _valid(db, wiki.source, row)
            # 失效/暂停下游由主 Agent 重核，不能程序盲改 depends_on。
            if not valid:
                continue
            candidate = wiki.prepare_figure_update(target['page_id'], target['published_version'], task_id=task['task_id'])
            sid = 'figure:' + candidate['batch_id']
            batch = register_batch(store, task, sid, candidate['batch_id'], wiki.workspace)
            _publish(store, task, sid, batch)
        return store.get(task['owner_id'], task['task_id'])


def _publish(store, task, sid, batch):
    wiki = _wiki(store, batch, task['owner_id'])
    candidate, committed = _candidate(wiki, task, batch['batch_id'])
    if candidate['fingerprint'] != batch['fingerprint']:
        raise TaskError('version_conflict')
    if not committed:
        publish_candidate(wiki, batch['batch_id'], task)
    reconcile_committed(store, task)
