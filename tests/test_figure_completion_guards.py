"""完成提交跨源库持锁及原配置恢复屏障；仅使用隔离资料。"""
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from threading import Event

import pytest

from agent.knowledge import figure_publication as publication
from agent.knowledge import figures
from agent.knowledge.personal_assistant import SourceService
from agent.knowledge.wiki import WikiService
from common.personal_tasks import OWNER, TaskError
from models import config_revision
from tests.test_figure_publication import published as published_fixture
from tests.test_reading_ingest_flow import deliver, flow as flow_fixture, submission


published = published_fixture
flow = flow_fixture


def complete(store, task):
    return store.complete(OWNER, task['task_id'], task['attempt'], 'published', [])


def change_config(monkeypatch):
    snapshot = config_revision._snapshot()
    monkeypatch.setattr(config_revision, '_snapshot', lambda: dict(snapshot, completion_guard_test='changed'))


def assert_writable(path):
    with sqlite3.connect(path, timeout=.05) as db:
        db.execute('BEGIN IMMEDIATE')
        db.rollback()


def test_all_workspaces_locked_before_inspect_through_task_commit(published, monkeypatch):
    store, task, wiki, ref = published()
    # 首次插入 workspace，随后插入 a-workspace，验证加锁排序独立于登记顺序。
    other_root = wiki.workspace.parent / 'a-workspace'
    shutil.copytree(wiki.workspace, other_root)
    other = WikiService(other_root)
    candidate = other.prepare_figure_update(ref['page_id'], ref['publish_id'], task_id=task['task_id'])
    publication.register_batch(store, task, 'second-workspace', candidate['batch_id'], other_root)
    other.publish(candidate['batch_id'])
    publication.reconcile_committed(store, task)
    paths = [str(w.source.db) for w in (other, wiki)]
    # 独立进程尝试 SQLite 写入，排除 threading.Lock 带来的假阳性。
    probe = """import sqlite3,sys
for path in sys.argv[1:]:
    db=sqlite3.connect(path,timeout=.02)
    try:
        db.execute('BEGIN IMMEDIATE')
    except sqlite3.OperationalError as exc:
        assert 'locked' in str(exc), str(exc)
    else:
        raise AssertionError('source writable during completion: '+path)
    finally:
        db.close()
"""
    inspect = publication._inspect
    observed = []
    def locked_inspect(store, task, ledger, sources=None):
        assert list(sources) == sorted(sources) and len(sources) == 2
        subprocess.run([sys.executable, '-c', probe, *paths], check=True, timeout=5)
        observed.append('inspect')
        return inspect(store, task, ledger, sources)
    monkeypatch.setattr(publication, '_inspect', locked_inspect)
    commit = store._complete_record
    def locked_commit(*args):
        result = commit(*args)
        assert store.get(OWNER, task['task_id'])['status'] == 'succeeded'
        subprocess.run([sys.executable, '-c', probe, *paths], check=True, timeout=5)
        observed.append('commit')
        return result
    monkeypatch.setattr(store, '_complete_record', locked_commit)
    assert complete(store, task)['status'] == 'succeeded'
    assert observed == ['inspect', 'commit']
    for path in paths:
        assert_writable(path)


def test_legal_withdrawal_waits_until_success_commit_returns(published, monkeypatch):
    store, task, wiki, ref = published()
    with wiki.source.transaction() as db:
        source = json.loads(db.execute('SELECT record FROM wiki_publications WHERE id=?',
                                      (ref['publish_id'],)).fetchone()[0])['sources'][0]
    document = wiki.source.get(source['document_id'])
    attempted = Event()
    transaction = SourceService.transaction
    @contextmanager
    def traced(service):
        if service is wiki.source:
            attempted.set()
        with transaction(service) as db:
            yield db
    monkeypatch.setattr(SourceService, 'transaction', traced)
    commit = store._complete_record
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = []
        def racing_commit(*args):
            future = pool.submit(wiki.source.change, document['id'], action='withdraw',
                                 expected_state=document['state'], reason='隔离撤回')
            futures.append(future)
            assert attempted.wait(3)
            # BEGIN IMMEDIATE 已发起，独立源连接仍不能跨过最终检查。
            with pytest.raises(TimeoutError):
                future.result(timeout=.1)
            result = commit(*args)
            assert not future.done()
            return result
        monkeypatch.setattr(store, '_complete_record', racing_commit)
        assert complete(store, task)['status'] == 'succeeded'
        assert futures[0].result(timeout=5)['validity'] == 'withdrawn'
    assert wiki.current(ref['page_id']) is None


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'withdraw', 'manifest'])
def test_failed_inspection_releases_source_locks_and_keeps_task_running(published, damage):
    store, task, wiki, ref = published()
    if damage in {'missing', 'corrupt'}:
        item = ref['figure_manifest']['items'][0]
        path = Path(figures.resolve_figure(wiki.workspace, ref['publish_id'], item['figure_id'])['path'])
        if damage == 'missing':
            path.unlink()
        else:
            path.write_bytes(b'broken')
    elif damage == 'withdraw':
        with wiki.source.transaction() as db:
            document = dict(db.execute('SELECT * FROM documents').fetchone())
        wiki.source.change(document['id'], action='withdraw', expected_state=document['state'], reason='隔离撤回')
    else:
        with store._transaction() as db:
            ledger = publication._load(db, task)
            next(iter(ledger['targets'].values()))['manifest'] = dict(schema_version=1, items=[])
            publication._save(db, ledger)
    with pytest.raises(TaskError, match='figure_incomplete'):
        complete(store, task)
    assert store.get(OWNER, task['task_id'])['status'] == 'running'
    assert_writable(wiki.source.db)


def test_commit_exception_releases_all_source_locks(published, monkeypatch):
    store, task, wiki, ref = published()
    def fail(*args):
        raise TaskError('storage_failed')
    monkeypatch.setattr(store, '_complete_record', fail)
    with pytest.raises(TaskError, match='storage_failed'):
        complete(store, task)
    assert_writable(wiki.source.db)
    assert store.get(OWNER, task['task_id'])['status'] == 'running'


def test_cancel_and_complete_share_task_lock(published, monkeypatch):
    store, task, wiki, ref = published()
    attempted = Event()
    commit = store._complete_record
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = []
        def cancel():
            attempted.set()
            return store.cancel(OWNER, task['task_id'], task['attempt'])
        def racing_commit(*args):
            futures.append(pool.submit(cancel))
            assert attempted.wait(3)
            with pytest.raises(TimeoutError):
                futures[0].result(timeout=.1)
            return commit(*args)
        monkeypatch.setattr(store, '_complete_record', racing_commit)
        assert complete(store, task)['status'] == 'succeeded'
        with pytest.raises(TaskError, match='version_conflict'):
            futures[0].result(timeout=5)


def test_cancel_first_prevents_completion(published):
    store, task, wiki, ref = published()
    store.cancel(OWNER, task['task_id'], task['attempt'])
    with pytest.raises(TaskError, match='version_conflict'):
        complete(store, task)


@pytest.mark.parametrize('point', ['entry', 'before_publish', 'during_final_inspect'])
def test_config_change_rejects_resume_or_completion_without_new_publication(published, monkeypatch, point):
    store, task, wiki, ref = published(pending=point != 'during_final_inspect')
    config_revision.require_config_revision(task['config_revision'])  # 模拟 worker 首次检查成功
    if point == 'entry':
        change_config(monkeypatch)
    elif point == 'before_publish':
        register = publication.register_batch
        def changed_after_prepare(*args, **kwargs):
            result = register(*args, **kwargs)
            change_config(monkeypatch)
            return result
        monkeypatch.setattr(publication, 'register_batch', changed_after_prepare)
    else:
        inspect = publication._inspect
        def changed_during_inspect(*args, **kwargs):
            result = inspect(*args, **kwargs)
            change_config(monkeypatch)
            return result
        monkeypatch.setattr(publication, '_inspect', changed_during_inspect)
    with pytest.raises(config_revision.ConfigUnavailable):
        if point == 'during_final_inspect':
            complete(store, task)
        else:
            publication.resume_publication(store, task)
    assert store.get(OWNER, task['task_id'])['status'] == 'running'
    assert wiki.current(ref['page_id'])['publish_id'] == ref['publish_id']
    assert_writable(wiki.source.db)


def test_prepared_recovery_rechecks_config_before_publish(published, monkeypatch):
    store, task, wiki, ref = published(pending=True)
    candidate = wiki.prepare_figure_update(ref['page_id'], ref['publish_id'], task_id=task['task_id'])
    publication.register_batch(store, task, 'prepared', candidate['batch_id'], wiki.workspace)
    candidate_lookup = publication._candidate
    def changed_lookup(*args):
        result = candidate_lookup(*args)
        change_config(monkeypatch)
        return result
    monkeypatch.setattr(publication, '_candidate', changed_lookup)
    with pytest.raises(config_revision.ConfigUnavailable):
        publication.resume_publication(store, task)
    assert wiki.current(ref['page_id'])['publish_id'] == ref['publish_id']


@pytest.mark.parametrize('entry', ['native', 'legacy'])
@pytest.mark.parametrize('mode', ['explicit', 'required', 'old'])
def test_first_publication_rechecks_config_after_registration(request, tmp_path, monkeypatch, entry, mode):
    if entry == 'native':
        root, store, task, attachment, read, tool = request.getfixturevalue('flow')
        result = read.execute(dict(path=attachment['path'], pages='1'))
        deliver(result, task)
        args = submission(task, result.result)
    else:
        from agent.tools.knowledge_ingest import KnowledgeIngest
        from common.personal_tasks import TaskStore
        root = tmp_path / 'workspace'
        store = TaskStore(tmp_path / 'personal')
        task = store.submit(OWNER, 'first-legacy', {'knowledge_synthesis': True}, {'channel': 'web'},
                            config_revision.capture_config_revision())
        task = store.claim(OWNER, task['task_id'])
        service = SourceService(root)
        original = tmp_path / 'source.txt'
        original.write_text('蓝色方块。', encoding='utf-8')
        source = service.receive(original, filename=original.name, request_id='source', origin={'channel': 'web'})
        service.change(source['document_id'], purpose='long_term', expected_state=0, reason='隔离分享')
        parsed = service.parse(source['revision_id'])
        citation = dict(document_id=source['document_id'], revision_id=source['revision_id'],
                        parse_id=parsed['parse_id'], ordinal=0)
        args = dict(task_id=task['task_id'], purpose='long_term', reason='整理', pages=[dict(
            title='隔离主题', summary='记录蓝色方块。', category='测试',
            blocks=[dict(content='蓝色方块。', sources=[citation])])])
        tool = KnowledgeIngest(dict(cwd=str(root), task_store=store))
    if mode == 'required':
        task = store._change(OWNER, task['task_id'], task['attempt'], {'running'},
                             payload=dict(task['payload'], figure_requirement={'schema_version': 1}))
    if mode != 'old':
        args['pages'][0]['blocks'][0]['figures'] = []
    config_revision.require_config_revision(task['config_revision'])
    register = publication.register_batch
    registrations = []
    def change_after_register(*args, **kwargs):
        batch = register(*args, **kwargs)
        if not registrations:
            change_config(monkeypatch)
        registrations.append(batch)
        return batch
    monkeypatch.setattr(publication, 'register_batch', change_after_register)
    from common.runtime_identity import identity_scope
    with identity_scope(user_id=OWNER, session_id='session'):
        result = tool.execute(args)
    assert registrations
    with WikiService(root).source.transaction() as db:
        batches = [row[0] for row in db.execute('SELECT published FROM wiki_batches')]
        published_count = db.execute('SELECT count(*) FROM wiki_publications').fetchone()[0]
    current = store.get(OWNER, task['task_id'])
    if mode == 'old':
        assert result.status == 'success' and current['status'] == 'succeeded'
        assert batches == [1] and published_count == 1 and current['result_refs']
    else:
        assert result.status == 'error' and result.result['code'] == 'config_unavailable'
        assert current['status'] == 'failed' and current['error']['code'] == 'config_unavailable'
        assert batches == [0] and published_count == 0 and not current['result_refs']
        assert registrations[0]['status'] == 'prepared'


def test_worker_config_change_after_first_check_reports_config_unavailable(published, monkeypatch):
    from common import personal_worker as worker
    store, task, wiki, ref = published(pending=True)
    store.fail(OWNER, task['task_id'], task['attempt'], 'figure_incomplete', 'publish')
    store.retry(OWNER, task['task_id'], task['attempt'])
    monkeypatch.setattr(worker, 'TaskStore', lambda: store)
    require = config_revision.require_config_revision
    checks = []
    def checked_then_changed(revision):
        require(revision)
        checks.append(revision)
        change_config(monkeypatch)
    monkeypatch.setattr(config_revision, 'require_config_revision', checked_then_changed)
    notices = []
    monkeypatch.setattr(worker, '_notify', lambda store, task, text: notices.append(text))
    monkeypatch.setattr(worker, 'run_agent', lambda task: pytest.fail('配置失效不应再调用模型'))
    worker._run(task['task_id'])
    result = store.get(OWNER, task['task_id'])
    assert len(checks) == 1
    assert result['status'] == 'failed' and result['error']['code'] == 'config_unavailable'
    assert result['result_refs'] == task['result_refs']
    assert wiki.current(ref['page_id'])['publish_id'] == ref['publish_id']
    assert len(notices) == 1 and 'config_unavailable' in notices[0]


@pytest.mark.parametrize('point', ['resume', 'publish', 'complete'])
def test_pinned_and_scoped_config_cannot_hide_global_change(published, monkeypatch, point):
    import config
    store, task, wiki, ref = published(pending=point != 'complete')
    config_revision.require_config_revision(task['config_revision'])  # worker 入场核验
    with config_revision.use_config_revision(task['config_revision']):
        with config.config_scope(config.Config(dict(config.conf()))):
            # 替换测试进程的全局对象，不落盘、不修改真实配置。
            monkeypatch.setattr(config, 'config', config.Config(dict(config.config, model='changed-offline')))
            assert config_revision.capture_config_revision() == task['config_revision']
            config_revision.require_config_revision(task['config_revision'])  # 原 API 此时会误放行
            with pytest.raises(config_revision.ConfigUnavailable):
                if point == 'complete':
                    complete(store, task)
                elif point == 'resume':
                    publication.resume_publication(store, task)
                else:
                    # 复用已发布批次不触发新发布；必须准备未提交候选验证边界。
                    candidate = wiki.prepare_figure_update(ref['page_id'], ref['publish_id'], task_id=task['task_id'])
                    batch = publication.register_batch(store, task, 'pinned', candidate['batch_id'], wiki.workspace)
                    publication._publish(store, task, 'pinned', batch)
    assert store.get(OWNER, task['task_id'])['status'] == 'running'
    assert wiki.current(ref['page_id'])['publish_id'] == ref['publish_id']


@pytest.mark.parametrize('operation', ['complete', 'publish'])
def test_config_writer_waits_until_commit_returns(published, monkeypatch, operation):
    import config
    store, task, wiki, ref = published(pending=operation == 'publish')
    monkeypatch.setattr(config, 'config', config.Config(dict(config.config)))
    attempted = Event()
    def write_config():
        attempted.set()
        with config.config_lock:
            config.config['model'] = 'changed-after-commit'
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = []
        if operation == 'complete':
            commit = store._complete_record
        else:
            commit = WikiService.publish
        def racing_commit(*args, **kwargs):
            futures.append(pool.submit(write_config))
            assert attempted.wait(3)
            with pytest.raises(TimeoutError):
                futures[0].result(timeout=.1)
            result = commit(*args, **kwargs)
            assert not futures[0].done()
            return result
        if operation == 'complete':
            monkeypatch.setattr(store, '_complete_record', racing_commit)
            assert complete(store, task)['status'] == 'succeeded'
        else:
            candidate = wiki.prepare_figure_update(ref['page_id'], ref['publish_id'], task_id=task['task_id'])
            batch = publication.register_batch(store, task, 'config-write', candidate['batch_id'], wiki.workspace)
            monkeypatch.setattr(WikiService, 'publish', racing_commit)
            publication._publish(store, task, 'config-write', batch)
            assert wiki.current(ref['page_id'])['publish_id'] != ref['publish_id']
        futures[0].result(timeout=5)
    assert config.config['model'] == 'changed-after-commit'
    assert config.config_lock.acquire(timeout=1)
    config.config_lock.release()


def test_reorganize_acquires_task_lock_before_source_transaction(published, monkeypatch):
    from channel.web.api import personal
    store, task, wiki, ref = published()
    monkeypatch.setattr(personal, '_source_service', lambda: wiki.source)
    transaction = wiki.source.transaction
    @contextmanager
    def source_transaction():
        assert store.lock._is_owned()
        with transaction() as db:
            yield db
    monkeypatch.setattr(wiki.source, 'transaction', source_transaction)
    # 无授权源回执时仍须先遵守同一锁顺序，再按原规则拒绝。
    with pytest.raises(TaskError, match='forbidden'):
        personal._reorganize(store, task['task_id'], task['attempt'],
                             dict(request_id='lock-order', expected_state=0, document_id='foreign'))
