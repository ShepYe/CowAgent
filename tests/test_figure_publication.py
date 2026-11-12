"""真实隔离任务／Wiki 的补图、完成屏障、恢复和依赖测试。"""
import json
import sqlite3
from pathlib import Path

import pytest

from agent.knowledge import figure_publication as figures_pub
from agent.knowledge import reading_publication as reading_pub
from agent.knowledge import figures, reading_materials
from agent.knowledge.parser import SourceError
from agent.knowledge.wiki import WikiService
from common.personal_tasks import OWNER, TaskError, TaskStore
from common import personal_worker as worker
from models.config_revision import capture_config_revision
from tests.test_reading_ingest_flow import flow as flow_fixture, deliver, submission  # noqa: F401


@pytest.fixture
def published(request, monkeypatch):
    root, store, task, entry, read, tool = request.getfixturevalue('flow_fixture')
    # 停在可靠正文已发布、最终 complete 之前，故障注入真实 PNG。
    monkeypatch.setattr(reading_pub, 'finish_publication', lambda s, t: s.get(OWNER, t['task_id']))
    def publish(pending=False, empty=False):
        result = read.execute(dict(path=entry['path'], pages='1'))
        deliver(result, task)
        args = submission(task, result.result)
        args['pages'][0]['blocks'][0]['figures'] = [] if empty else [dict(
            figure_key='blue', caption='蓝色方块', source=args['pages'][0]['blocks'][0]['sources'][0])]
        render = reading_materials._plan_region
        if pending:
            monkeypatch.setattr(reading_materials, '_plan_region', lambda *a, **kw: (_ for _ in ()).throw(SourceError('storage_failed', '模拟渲染失败')))
        result = tool.execute(args)
        monkeypatch.setattr(reading_materials, '_plan_region', render)
        assert result.status == 'success', result.result
        current = store.get(OWNER, task['task_id'])
        return store, current, WikiService(root), result.result['result_refs'][0]
    return publish


def test_explicit_empty_and_both_register_target_branches_preserve_v3(published):
    store, task, wiki, ref = published(empty=True)
    assert task['figure_publication'] == {'schema_version': 1}
    assert figures_pub.publication_state(store, task)['remaining'] == []
    with sqlite3.connect(store.db) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 3
    original = reading_pub.publication_state(store, task)['targets']
    reading_pub.register_target(store, task, original)
    other = store.submit(OWNER, 'other', {'text': '新任务'}, {'channel': 'web'}, 'cfg')
    reading_pub.register_target(store, other, ['revision:1'])
    reopened = TaskStore(store.root)
    with sqlite3.connect(reopened.db) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 3
    assert reopened.complete(OWNER, task['task_id'], task['attempt'], 'published', [])['status'] == 'succeeded'


@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
def test_complete_rechecks_actual_png_and_keeps_reliable_text(published, damage):
    store, task, wiki, ref = published()
    item = ref['figure_manifest']['items'][0]
    path = figures.resolve_figure(wiki.workspace, ref['publish_id'], item['figure_id'])['path']
    if damage == 'missing':
        Path(path).unlink()
    else:
        Path(path).write_bytes(b'corrupt PNG')
    with pytest.raises(TaskError, match='figure_incomplete'):
        store.complete(OWNER, task['task_id'], task['attempt'], 'published', [])
    current = store.get(OWNER, task['task_id'])
    assert current['status'] == 'running' and current['result_refs'] == task['result_refs']
    assert wiki.current(ref['page_id']) and wiki.search('蓝色方块')
    state = figures_pub.publication_state(store, current)
    assert state['targets'][state['remaining'][0]]['gaps'] == ['unavailable']


def test_pending_then_resume_new_version_without_rereading(published, monkeypatch):
    store, task, wiki, ref = published(pending=True)
    assert ref['figure_state'] == 'pending'
    with pytest.raises(TaskError, match='figure_incomplete'):
        store.complete(OWNER, task['task_id'], task['attempt'], 'published', [])
    monkeypatch.setattr('agent.knowledge.reading.ReadingService.resume_all', lambda *a: pytest.fail('不得重读'))
    result = figures_pub.resume_publication(store, task)
    current = wiki.current(ref['page_id'])
    assert current['publish_id'] != ref['publish_id'] and current['figure_state'] == 'ready'
    assert wiki.version(ref['publish_id'])['figure_state'] == 'pending'
    assert result['result_refs'][0] == ref
    assert figures_pub.publication_state(store, task)['remaining'] == []
    count = len(figures_pub.publication_state(store, task)['batches'])
    figures_pub.resume_publication(store, task)
    assert len(figures_pub.publication_state(store, task)['batches']) == count
    assert store.complete(OWNER, task['task_id'], task['attempt'], 'published', [])['status'] == 'succeeded'


def test_target_cannot_be_dropped_and_new_submissions_require_figures(published):
    store, task, wiki, ref = published(pending=True)
    with pytest.raises(TaskError, match='invalid_request'):
        figures_pub.require_explicit_figures(task, [{'blocks': [{'content': '漏图'}]}])
    with wiki.source.transaction() as db:
        original = json.loads(db.execute('SELECT record FROM wiki_publications WHERE id=?', (ref['publish_id'],)).fetchone()[0])
    source = {k: original['sources'][0][k] for k in ('document_id', 'revision_id', 'parse_id', 'ordinal')}
    candidate = wiki.prepare([dict(page_id=ref['page_id'], expected_version=ref['publish_id'], title=ref['title'],
        summary=ref['summary'], category=ref['category'], blocks=[dict(content='漏交选图', sources=[source], figures=[])])], task_id=task['task_id'])
    with pytest.raises(TaskError, match='idempotency_conflict'):
        figures_pub.register_batch(store, task, 'drop', candidate['batch_id'], wiki.workspace)
    assert wiki.current(ref['page_id'])['publish_id'] == ref['publish_id']


def test_restart_and_cancel_reconcile_committed_figure_batch(published, monkeypatch):
    store, task, wiki, ref = published(pending=True)
    candidate = wiki.prepare_figure_update(ref['page_id'], ref['publish_id'], task_id=task['task_id'])
    figures_pub.register_batch(store, task, 'repair', candidate['batch_id'], wiki.workspace)
    wiki.publish(candidate['batch_id'])  # 来源已提交、任务回执未落盘。
    store.reconcile()
    current = store.get(OWNER, task['task_id'])
    assert current['attempt'] == task['attempt'] + 1
    store._change(OWNER, task['task_id'], current['attempt'], {'needs_reconcile'}, status='queued')
    current = store.claim(OWNER, task['task_id'])
    monkeypatch.setattr(WikiService, 'publish', lambda *a: pytest.fail('不得重发已提交批次'))
    cancelled = store.cancel(OWNER, task['task_id'], current['attempt'])
    assert cancelled['status'] == 'cancelled' and len(cancelled['result_refs']) == 2
    with pytest.raises(TaskError, match='version_conflict'):
        figures_pub.resume_publication(store, task)


def test_unknown_marker_and_ledger_versions_fail_closed(published):
    store, task, wiki, ref = published(empty=True)
    with store._transaction() as db:
        db.execute("UPDATE figure_publications SET record=? WHERE task_id=?", (json.dumps(dict(schema_version=2)), task['task_id']))
    with pytest.raises(TaskError, match='version_conflict'):
        store.complete(OWNER, task['task_id'], task['attempt'], 'published', [])


def test_dependency_inside_blocks_completion_outside_is_reported(published):
    store, task, wiki, ref = published(pending=True)
    with wiki.source.transaction() as db:
        original = json.loads(db.execute('SELECT record FROM wiki_publications WHERE id=?', (ref['publish_id'],)).fetchone()[0])
    source = {k: original['sources'][0][k] for k in ('document_id', 'revision_id', 'parse_id', 'ordinal')}
    def dependent(title, tracked):
        batch = wiki.prepare([dict(title=title, summary='依赖上游。', category='测试', blocks=[dict(
            content=title, sources=[source], figures=[])], depends_on=[dict(page_id=ref['page_id'], page_version=ref['publish_id'])])], task_id=task['task_id'])
        if tracked:
            figures_pub.register_batch(store, task, title, batch['batch_id'], wiki.workspace)
        return wiki.publish(batch['batch_id'])[0]
    inside = dependent('目标内下游', True)
    outside = dependent('目标外下游', False)
    figures_pub.reconcile_committed(store, task)
    figures_pub.resume_publication(store, task)
    state = figures_pub.publication_state(store, task)
    assert any(t['page_id'] == inside['page_id'] and t['gaps'] == ['publication_invalid'] for t in state['targets'].values())
    assert {t['page_id'] for t in state['paused_outside_targets']} == {outside['page_id']}
    assert wiki.current(inside['page_id']) is None and wiki.version(inside['publish_id'])
    with pytest.raises(TaskError, match='figure_incomplete'):
        store.complete(OWNER, task['task_id'], task['attempt'], 'published', [])
    # 主 Agent 已真实重核后显式提交新的下游依赖；程序不自行换版本。
    upstream = wiki.current(ref['page_id'])
    verified = wiki.prepare([dict(page_id=inside['page_id'], expected_version=inside['publish_id'],
        title='目标内下游', summary='已重核依赖上游。', category='测试', blocks=[dict(content='目标内下游',
        sources=[source], figures=[])], depends_on=[dict(page_id=ref['page_id'], page_version=upstream['publish_id'])])], task_id=task['task_id'])
    figures_pub.register_batch(store, task, 'reverified', verified['batch_id'], wiki.workspace)
    wiki.publish(verified['batch_id'])
    figures_pub.reconcile_committed(store, task)
    assert figures_pub.publication_state(store, task)['remaining'] == []
    assert store.complete(OWNER, task['task_id'], task['attempt'], 'published', [])['status'] == 'succeeded'
    assert wiki.current(outside['page_id']) is None


def test_worker_budget_exhaustion_preserves_text_and_reports_publish_failure(published, monkeypatch):
    store, task, wiki, ref = published(pending=True)
    store.fail(OWNER, task['task_id'], task['attempt'], 'figure_incomplete', 'publish')
    task = store.retry(OWNER, task['task_id'], task['attempt'])
    monkeypatch.setattr(worker, 'TaskStore', lambda: store)
    monkeypatch.setattr('models.config_revision.require_config_revision', lambda value: None)
    monkeypatch.setattr(reading_materials, '_plan_region', lambda *a, **kw: (_ for _ in ()).throw(SourceError('storage_failed', '仍失败')))
    monkeypatch.setattr(worker, 'run_agent', lambda t: '错误地声称全部完成')
    notices = []
    monkeypatch.setattr(worker, '_notify', lambda s, t, text: notices.append(text))
    worker._run(task['task_id'])
    result = store.get(OWNER, task['task_id'])
    assert result['status'] == 'failed' and result['stage'] == 'publish'
    assert result['error']['code'] == 'figure_incomplete' and result['error']['retryable'] is True
    assert result['result_refs'] and wiki.current(ref['page_id'])
    assert all('错误地声称全部完成' not in n for n in notices)


def test_trusted_requirement_cannot_complete_via_legacy_without_marker(tmp_path):
    store = TaskStore(tmp_path)
    task = store.submit(OWNER, 'required', dict(text='图文整理', figure_requirement={'schema_version': 1}), {'channel': 'web'}, capture_config_revision())
    task = store.claim(OWNER, task['task_id'])
    assert figures_pub.publication_state(store, task)['remaining'] == ['selection_required']
    with pytest.raises(TaskError, match='invalid_request'):
        figures_pub.require_explicit_figures(task, [{'blocks': [{'content': '漏图'}]}])
    with pytest.raises(TaskError, match='figure_incomplete'):
        store.complete(OWNER, task['task_id'], 1, 'published', [{'publish_id': 'legacy'}])
    assert 'figure_publication' not in store.get(OWNER, task['task_id'])
    figures_pub.require_explicit_figures(task, [{'blocks': [{'figures': []}]}])
    with pytest.raises(TaskError, match='version_conflict'):
        figures_pub.figure_required({'payload': {'figure_requirement': {'schema_version': 2}}})


def test_nonreading_task_register_reconcile_and_completion(published):
    store, task, wiki, ref = published(empty=True)
    other = store.submit(OWNER, 'legacy-parse', {'text': '已有解析重新整理', 'figure_requirement': {'schema_version': 1}}, {'channel': 'web'}, capture_config_revision())
    other = store.claim(OWNER, other['task_id'])
    candidate = wiki.prepare_figure_update(ref['page_id'], ref['publish_id'], task_id=other['task_id'])
    figures_pub.register_batch(store, other, 'legacy-parse', candidate['batch_id'], wiki.workspace)
    wiki.publish(candidate['batch_id'])
    figures_pub.reconcile_committed(store, other)
    current = store.get(OWNER, other['task_id'])
    assert 'reading_publication' not in current and current['result_refs']
    assert store.complete(OWNER, other['task_id'], 1, 'published', [])['status'] == 'succeeded'


@pytest.mark.parametrize('marker', ['figure', 'reading', 'both'])
def test_complete_merges_only_trusted_transient_receipt(published, marker):
    from agent.tools.knowledge_ingest import KnowledgeIngest
    store, task, wiki, ref = published(empty=True)
    path = wiki.workspace / 'temporary.txt'
    path.write_text('仅供本次参考', encoding='utf-8')
    entry = store.attach(OWNER, task['task_id'], path)
    tool = KnowledgeIngest(dict(cwd=str(wiki.workspace), task_store=store))
    result = tool.execute(dict(task_id=task['task_id'], attachment_id=entry['attachment_id'],
                               purpose='transient', reason='仅供本次参考'))
    assert result.status == 'success', result.result
    task = store.get(OWNER, task['task_id'])
    receipt = task['attachment_decisions'][entry['attachment_id']]['result_ref']
    with store._transaction() as db:
        current = store._read(db, OWNER, task['task_id'])
        if marker != 'both':
            current.pop('reading_publication' if marker == 'figure' else 'figure_publication')
        store._save(db, current)
    fake = [dict(receipt, searchable=True, publish_id='forged'),
            dict(ref, publish_id='obsolete'), dict(purpose='transient', attachment_id='unknown')]
    finished = store.complete(OWNER, task['task_id'], task['attempt'], 'published', fake)
    assert finished['result_refs'] == [ref, receipt]


@pytest.mark.parametrize('partial', [False, True])
def test_resume_pending_keeps_transient_receipt_through_retry(published, monkeypatch, partial):
    from agent.tools.knowledge_ingest import KnowledgeIngest, resume_publication
    store, task, wiki, ref = published(pending=True)
    path = wiki.workspace / 'temporary.txt'
    path.write_text('本次临时参考', encoding='utf-8')
    entry = store.attach(OWNER, task['task_id'], path)
    result = KnowledgeIngest(dict(cwd=str(wiki.workspace), task_store=store)).execute(dict(
        task_id=task['task_id'], attachment_id=entry['attachment_id'], purpose='transient', reason='仅本次'))
    assert result.status == 'success', result.result
    current = store.get(OWNER, task['task_id'])
    receipt = current['attachment_decisions'][entry['attachment_id']]['result_ref']
    batch_id = next(iter(figures_pub.publication_state(store, task)['batches'].values()))['batch_id']
    # 恢复器从同一已提交批次接续；附加主题目标模拟部分成果尚未齐备。
    with store._transaction() as db:
        current = store._read(db, OWNER, task['task_id'])
        current.update(wiki_batch_id=batch_id, wiki_workspace=str(wiki.workspace))
        if partial:
            current['payload']['figure_requirement'] = dict(schema_version=1, page_ids=[ref['page_id'], 'missing'])
        task = store._save(db, current)
    render = reading_materials._plan_region
    monkeypatch.setattr(reading_materials, '_plan_region', lambda *a, **kw: (_ for _ in ()).throw(SourceError('storage_failed', '仍待补图')))
    pending = resume_publication(task, store)
    assert pending['status'] == 'running' and ref in pending['result_refs']
    assert pending['result_refs'].count(receipt) == 1
    store.fail(OWNER, task['task_id'], task['attempt'], 'figure_incomplete', 'publish')
    store.retry(OWNER, task['task_id'], task['attempt'])
    task = store.claim(OWNER, task['task_id'])
    monkeypatch.setattr(reading_materials, '_plan_region', render)
    resumed = resume_publication(task, store)
    assert resumed['result_refs'].count(receipt) == 1
    assert wiki.current(ref['page_id'])['publish_id'] != ref['publish_id']
    assert resumed['status'] == ('running' if partial else 'succeeded')
    assert any(r.get('publish_id') == wiki.current(ref['page_id'])['publish_id'] for r in resumed['result_refs'])


def test_task_migration_failure_keeps_source_candidate_retryable(published, monkeypatch):
    store, task, wiki, ref = published(empty=True)
    other = store.submit(OWNER, 'migration', {'text': '失败重试'}, {'channel': 'web'}, 'cfg')
    other = store.claim(OWNER, other['task_id'])
    candidate = wiki.prepare_figure_update(ref['page_id'], ref['publish_id'], task_id=other['task_id'])
    save = figures_pub._save
    monkeypatch.setattr(figures_pub, '_save', lambda *a: (_ for _ in ()).throw(TaskError('storage_failed')))
    with pytest.raises(TaskError, match='storage_failed'):
        figures_pub.register_batch(store, other, 'migration', candidate['batch_id'], wiki.workspace)
    assert 'figure_publication' not in store.get(OWNER, other['task_id'])
    assert wiki.current(ref['page_id'])['publish_id'] == ref['publish_id']
    monkeypatch.setattr(figures_pub, '_save', save)
    figures_pub.register_batch(store, other, 'migration', candidate['batch_id'], wiki.workspace)
    assert figures_pub.publication_state(store, other)['remaining']


def test_source_not_v3_refuses_figure_registration(published, monkeypatch):
    store, task, wiki, ref = published(empty=True)
    other = store.submit(OWNER, 'source-barrier', {'text': '两个库均就绪'}, {'channel': 'web'}, 'cfg')
    other = store.claim(OWNER, other['task_id'])
    candidate = wiki.prepare_figure_update(ref['page_id'], ref['publish_id'], task_id=other['task_id'])
    # 用隔离事务代理模拟来源迁移尚未就绪，不实际降级来源库。
    from contextlib import contextmanager
    from agent.knowledge.personal_assistant import SourceService
    transaction = SourceService.transaction
    class Cursor:
        def fetchone(self):
            return (2,)
    class PendingMigration:
        def __init__(self, db):
            self.db = db
        def execute(self, sql, *a):
            return Cursor() if sql == 'PRAGMA user_version' else self.db.execute(sql, *a)
    @contextmanager
    def pending(service):
        with transaction(service) as db:
            yield PendingMigration(db)
    monkeypatch.setattr(SourceService, 'transaction', pending)
    with pytest.raises(TaskError, match='version_conflict'):
        figures_pub.register_batch(store, other, 'migration', candidate['batch_id'], wiki.workspace)
    assert 'figure_publication' not in store.get(OWNER, other['task_id'])


@pytest.mark.parametrize('pending', [False, True])
def test_required_page_ids_prevent_partial_theme_completion(published, pending):
    store, task, wiki, ref = published(pending=pending, empty=not pending)
    missing_pages = ['unpublished-topic-2', 'unpublished-topic-3', 'unpublished-topic-4']
    with store._transaction() as db:
        current = store._read(db, OWNER, task['task_id'])
        current['payload']['figure_requirement'] = dict(schema_version=1, page_ids=[ref['page_id'], *missing_pages])
        task = store._save(db, current)
    result = figures_pub.resume_publication(store, task)
    assert result['status'] == 'running'
    assert result['result_refs'][0] == ref
    state = figures_pub.publication_state(store, task)
    assert state['remaining'] == ['page_required:' + page for page in missing_pages]
    assert len(state['targets']) == 1
    assert wiki.current(ref['page_id'])['figure_state'] == ('ready' if pending else 'none')
    with pytest.raises(TaskError, match='figure_incomplete'):
        store.complete(OWNER, task['task_id'], task['attempt'], 'published', [])


@pytest.fixture
def figure_api(published, monkeypatch):
    from channel.web.api import personal
    def setup(pending=False):
        store, task, wiki, ref = published(pending=pending)
        monkeypatch.setattr(personal, 'TaskStore', lambda: store)
        monkeypatch.setattr(personal, '_get_workspace_root', lambda: str(wiki.workspace))
        monkeypatch.setattr('config.get_data_root', lambda: str(store.root.parent))
        monkeypatch.setattr(personal, '_require_auth', lambda: None)
        monkeypatch.setattr(personal.web, 'header', lambda *a, **kw: None)
        return personal, store, task, wiki, ref
    return setup


def test_figure_get_is_readonly_no_png_and_shares_snapshot(figure_api, monkeypatch):
    from hashlib import sha256
    mod, store, task, wiki, ref = figure_api()
    task = store.complete(OWNER, task['task_id'], task['attempt'], 'published', [])
    def forbidden(*a, **kw):
        pytest.fail('GET 不得写库、协调发布或读取 PNG')
    monkeypatch.setattr(figures_pub, 'publication_state', forbidden)
    monkeypatch.setattr(figures_pub, '_inspect', forbidden)
    monkeypatch.setattr(figures, '_bytes', forbidden)
    monkeypatch.setattr(store, '_save', forbidden)
    monkeypatch.setattr('agent.knowledge.personal_assistant.SourceService.__init__', forbidden)
    original_connect = sqlite3.connect
    snapshots = []
    def connect(database, *a, **kw):
        if kw.get('uri'):
            snapshots.append(str(database))
        return original_connect(database, *a, **kw)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    paths = [store.db, wiki.source.db]
    before = [sha256(p.read_bytes()).hexdigest() for p in paths]
    result = mod._decorate(task)
    assert result['figure_progress']['targets'] == result['figure_progress']['ready'] == [ref['page_id']]
    assert result['figure_progress']['task_complete'] and result['reading_progress']['task_complete']
    assert not result['figure_progress']['pending'] and not result['figure_progress']['gaps']
    assert result['figure_progress']['published_batches'] == 1
    assert len(snapshots) == 2 and all(s.endswith('?mode=ro') for s in snapshots)
    assert before == [sha256(p.read_bytes()).hexdigest() for p in paths]
    assert str(wiki.workspace) not in json.dumps(result['figure_progress'])


def test_figure_get_pending_and_current_invalidation_constrain_reading_complete(figure_api):
    mod, store, task, wiki, ref = figure_api(pending=True)
    # 即使遗留/故障任务记录声称 succeeded，也必须反映真实发布状态。
    shown = mod._decorate(dict(task, status='succeeded'))
    assert shown['figure_progress']['pending'] == [ref['page_id']]
    assert not shown['figure_progress']['task_complete']
    assert not shown['reading_progress']['task_complete']
    with wiki.source.transaction() as db:
        db.execute("UPDATE wiki_pages SET status='paused',reason='upstream_changed' WHERE id=?", (ref['page_id'],))
    shown = mod._decorate(dict(task, status='succeeded'))
    assert shown['figure_progress']['gaps'][ref['page_id']] == ['publication_invalid']
    assert not shown['reading_progress']['task_complete']


def test_figure_projection_does_not_block_source_or_task_writers(figure_api, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    mod, store, task, wiki, ref = figure_api()
    # 独立图文任务走相同投影；移除阅读标记以单独验证新投影锁边界。
    task = {k: v for k, v in task.items() if k != 'reading_publication'}
    entered, release = Event(), Event()
    current = mod._ReadWiki.current
    def slow(view, page_id):
        entered.set()
        assert release.wait(5)
        return current(view, page_id)
    monkeypatch.setattr(mod._ReadWiki, 'current', slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(mod._decorate, task)
        try:
            assert entered.wait(3)
            for path in (store.db, wiki.source.db):
                with sqlite3.connect(path, timeout=.05) as writer:
                    writer.execute('BEGIN IMMEDIATE')
                    writer.execute('PRAGMA user_version')
                    writer.commit()
        finally:
            release.set()
        assert request.result(timeout=5)['figure_progress']['ready'] == [ref['page_id']]
    # 来源写事务未提交时读取已提交快照，GET 不等待来源写锁。
    writer = sqlite3.connect(wiki.source.db, timeout=.05)
    try:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute("UPDATE wiki_pages SET status='paused'")
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(mod._decorate, task).result(timeout=3)
        assert response['figure_progress']['ready'] == [ref['page_id']]
    finally:
        writer.rollback()
        writer.close()


def test_figure_requirement_without_ledger_is_pending_in_get(tmp_path, monkeypatch):
    from channel.web.api import personal
    store = TaskStore(tmp_path / 'personal')
    task = store.submit(OWNER, 'required-api', dict(text='图文', figure_requirement=dict(schema_version=1, page_ids=['a', 'b'])), {'channel': 'web'}, 'cfg')
    monkeypatch.setattr('config.get_data_root', lambda: str(tmp_path))
    result = personal._decorate(task)['figure_progress']
    assert result['pending'] == result['targets'] == ['a', 'b']
    assert result['gaps'] == {'a': ['selection_required'], 'b': ['selection_required']}
    assert not result['task_complete']
