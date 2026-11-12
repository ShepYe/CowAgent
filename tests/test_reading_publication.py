"""真实隔离 wiki/任务库，多批提交窗口、恢复及取消竞态。"""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from agent.knowledge import reading_publication as pub
from agent.knowledge.personal_assistant import SourceService
from agent.knowledge.wiki import WikiService
from common.personal_tasks import OWNER, TaskError, TaskStore
from common import personal_worker as worker


@pytest.fixture
def env(tmp_path):
    store = TaskStore(tmp_path / 'tasks')
    task = store.submit(OWNER, 'read', {'text': '完整整理'}, {'channel': 'web'}, 'cfg')
    task = store.claim(OWNER, task['task_id'])
    workspace = tmp_path / 'workspace'
    service = SourceService(workspace)
    original = tmp_path / 'source.txt'
    original.write_text('预算42 dB。\n温度25度。', encoding='utf-8')
    source = service.receive(original, filename='source.txt', request_id='src', origin={'channel': 'web'})
    service.change(source['document_id'], purpose='long_term', expected_state=0, reason='分享')
    parsed = service.parse(source['revision_id'])
    targets = [source['revision_id'] + ':1', source['revision_id'] + ':2']
    task = pub.register_target(store, task, targets)
    wiki = WikiService(workspace)

    def prepare(index=0, submission=None, coverage=None):
        pages = [dict(title=f'主题{index}', summary='记录资料中的预算及适用条件。', category='工程',
            blocks=[dict(content=f'预算42 dB，主题{index}', sources=[dict(
                document_id=source['document_id'], revision_id=source['revision_id'],
                parse_id=parsed['parse_id'], ordinal=0)])])]
        batch = wiki.prepare(pages, task_id=task['task_id'])
        sid = submission or f's{index}'
        pub.register_batch(store, store.get(OWNER, task['task_id']), sid,
                           coverage or [targets[index]], batch['batch_id'], workspace, payload=pages)
        return sid, batch, pages

    return store, task, wiki, targets, prepare


def test_targets_are_immutable_and_completion_checks_full_coverage(env):
    store, task, wiki, targets, prepare = env
    for changed in ([targets[0]], targets + ['extra:3']):
        with pytest.raises(TaskError, match='idempotency_conflict'):
            pub.register_target(store, task, changed)
    assert pub.register_target(store, task, targets[::-1])['reading_publication']
    prepare()
    task = pub.publish_batch(store, task, 's0')
    assert task['status'] == 'running' and len(task['result_refs']) == 1
    with pytest.raises(TaskError, match='reading_incomplete'):
        store.complete(OWNER, task['task_id'], 1, 'published', [])
    assert pub.finish_publication(store, task)['status'] == 'running'
    prepare(1)
    pub.publish_batch(store, task, 's1')
    task = pub.finish_publication(store, task)
    assert task['status'] == 'succeeded' and len(task['result_refs']) == 2


def test_replay_conflict_and_out_of_scope_fail_before_publication(env):
    store, task, wiki, targets, prepare = env
    sid, batch, pages = prepare()
    pub.register_batch(store, task, sid, [targets[0]], batch['batch_id'], wiki.workspace, pages)
    with pytest.raises(TaskError, match='idempotency_conflict'):
        pub.register_batch(store, task, sid, [targets[1]], batch['batch_id'], wiki.workspace, pages)
    with pytest.raises(TaskError, match='idempotency_conflict'):
        pub.register_batch(store, task, 'other', [targets[0]], batch['batch_id'], wiki.workspace, pages)
    with pytest.raises(TaskError, match='invalid_request'):
        pub.register_batch(store, task, 'outside', ['other:1'], batch['batch_id'], wiki.workspace)
    with wiki.source.transaction() as db:
        assert db.execute('SELECT count(*) FROM wiki_publications').fetchone()[0] == 0


class PowerLoss(BaseException):
    pass


@pytest.mark.parametrize('cancel', [False, True])
def test_commit_before_receipt_restart_or_cancel_retains_first_batch(env, monkeypatch, cancel):
    store, task, wiki, targets, prepare = env
    prepare()
    receipt = pub._receipt
    monkeypatch.setattr(pub, '_receipt', lambda *a: (_ for _ in ()).throw(PowerLoss()))
    with pytest.raises(PowerLoss):
        pub.publish_batch(store, task, 's0')
    assert store.get(OWNER, task['task_id'])['result_refs'] == []
    monkeypatch.setattr(pub, '_receipt', receipt)
    monkeypatch.setattr(WikiService, 'publish', lambda *a: pytest.fail('已提交批次不得再次publish'))
    if cancel:
        task = store.cancel(OWNER, task['task_id'], 1)
        assert task['status'] == 'cancelled' and len(task['result_refs']) == 1
        with pytest.raises(TaskError, match='version_conflict'):
            pub.publish_batch(store, task, 's0')
    else:
        store.reconcile()
        task = store.get(OWNER, task['task_id'])
        assert task['attempt'] == 2
        store._change(OWNER, task['task_id'], 2, {'needs_reconcile'}, status='queued')
        task = store.claim(OWNER, task['task_id'])
        task = pub.resume_publication(store, task)
        assert task['status'] == 'running' and len(task['result_refs']) == 1
        assert pub.publication_state(store, task)['remaining'] == [targets[1]]


def test_retry_adopts_saved_candidate_and_skips_successful_batch(env, monkeypatch):
    store, task, wiki, targets, prepare = env
    prepare()
    pub.publish_batch(store, task, 's0')
    prepare(1)
    store.fail(OWNER, task['task_id'], 1, 'reading_incomplete', 'parse')
    task = store.retry(OWNER, task['task_id'], 1)
    task = store.claim(OWNER, task['task_id'])
    with pytest.raises(TaskError, match='version_conflict'):
        pub.publish_batch(store, task, 's1')
    published = []
    original = WikiService.publish
    def publish(self, batch_id):
        published.append(batch_id)
        return original(self, batch_id)
    monkeypatch.setattr(WikiService, 'publish', publish)
    task = pub.resume_publication(store, task)
    state = pub.publication_state(store, task)
    assert task['status'] == 'succeeded' and len(task['result_refs']) == 2
    assert published == [state['batches']['s1']['batch_id']]
    assert state['batches']['s1']['attempt'] == 1
    assert state['batches']['s1']['adopted_attempt'] == 2


def test_concurrent_replay_publishes_once(env, monkeypatch):
    store, task, wiki, targets, prepare = env
    sid, batch, pages = prepare()
    calls = []
    original = WikiService.publish
    def publish(self, batch_id):
        calls.append(batch_id)
        return original(self, batch_id)
    monkeypatch.setattr(WikiService, 'publish', publish)
    def replay(_):
        pub.register_batch(store, task, sid, [targets[0]], batch['batch_id'], wiki.workspace, pages)
        return pub.publish_batch(store, task, sid)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(replay, range(8)))
    assert calls == [batch['batch_id']]
    assert all(len(r['result_refs']) == 1 and r['status'] == 'running' for r in results)


def test_cancel_wins_over_late_publish_and_registration(env):
    store, task, wiki, targets, prepare = env
    prepare()
    store.cancel(OWNER, task['task_id'], 1)
    with pytest.raises(TaskError, match='version_conflict'):
        pub.publish_batch(store, task, 's0')
    with pytest.raises(TaskError, match='version_conflict'):
        pub.register_target(store, task, targets)
    assert store.get(OWNER, task['task_id'])['result_refs'] == []


def test_publish_wins_batch_then_cancel_stops_remaining(env, monkeypatch):
    store, task, wiki, targets, prepare = env
    prepare()
    prepare(1)
    entered, release = threading.Event(), threading.Event()
    original = WikiService.publish
    def blocked(self, batch_id):
        entered.set()
        assert release.wait(5)
        return original(self, batch_id)
    monkeypatch.setattr(WikiService, 'publish', blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publishing = pool.submit(pub.publish_batch, store, task, 's0')
        assert entered.wait(5)
        cancelling = pool.submit(store.cancel, OWNER, task['task_id'], 1)
        release.set()
        publishing.result(5)
        result = cancelling.result(5)
    assert result['status'] == 'cancelled' and len(result['result_refs']) == 1
    assert pub.publication_state(store, task)['remaining'] == [targets[1]]


def test_unknown_ledger_version_rejected_before_publish(env, monkeypatch):
    store, task, wiki, targets, prepare = env
    prepare()
    with store._transaction() as db:
        row = json.loads(db.execute('SELECT record FROM reading_publications').fetchone()[0])
        row['schema_version'] = 99
        db.execute('UPDATE reading_publications SET record=?', (json.dumps(row),))
    monkeypatch.setattr(WikiService, 'publish', lambda *a: pytest.fail('未知版本不得发布'))
    with pytest.raises(TaskError, match='version_conflict'):
        pub.resume_publication(store, task)


def test_worker_continues_remaining_after_first_batch_and_budget_failure(env, monkeypatch):
    store, task, wiki, targets, prepare = env
    prepare()
    pub.publish_batch(store, task, 's0')
    store._change(OWNER, task['task_id'], 1, {'running'}, status='queued')
    monkeypatch.setattr(worker, 'TaskStore', lambda: store)
    monkeypatch.setattr('models.config_revision.require_config_revision', lambda *a: None)
    called = []
    monkeypatch.setattr(worker, 'run_agent', lambda t: called.append(t['task_id']) or '预算耗尽')
    worker._run(task['task_id'])
    result = store.get(OWNER, task['task_id'])
    assert called == [task['task_id']]
    assert result['status'] == 'failed' and len(result['result_refs']) == 1
    assert result['stage'] == 'parse' and result['error']['code'] == 'reading_incomplete'


def test_worker_finishes_remaining_after_recovery(env, monkeypatch):
    store, task, wiki, targets, prepare = env
    prepare()
    pub.publish_batch(store, task, 's0')
    store.reconcile()
    task = store.get(OWNER, task['task_id'])
    store._change(OWNER, task['task_id'], task['attempt'], {'needs_reconcile'}, status='queued')
    monkeypatch.setattr(worker, 'TaskStore', lambda: store)
    monkeypatch.setattr('models.config_revision.require_config_revision', lambda *a: None)
    def agent(t):
        prepare(1)
        pub.publish_batch(store, t, 's1')
        return '已完成'
    monkeypatch.setattr(worker, 'run_agent', agent)
    worker._run(task['task_id'])
    result = store.get(OWNER, task['task_id'])
    assert result['status'] == 'succeeded' and len(result['result_refs']) == 2


def test_relative_workspace_survives_whole_root_move(env, tmp_path):
    from cli.commands.backup import create_backup_archive, restore_backup_archive
    store, task, wiki, targets, prepare = env
    prepare()
    # 使用现有受信恢复器重定位 SourceService 的绝对原件槽位。
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'config.json').write_text(json.dumps({'agent_workspace': str(wiki.workspace)}), encoding='utf-8')
    archive = tmp_path / 'backup.zip'
    # TaskStore fixture 的 tasks/ 与 workspace/ 是同级；保留该布局。
    create_backup_archive(archive, data, wiki.workspace, complete=True)
    destination = tmp_path / 'restored'
    restore_backup_archive(archive, destination / 'data', destination / 'workspace')
    # 台账本身不含旧根绝对路径；任务库可单独快照到同布局的新位置。
    import sqlite3
    restored = TaskStore(destination / 'tasks')
    with sqlite3.connect(store.db) as source_db, sqlite3.connect(restored.db) as dest_db:
        source_db.backup(dest_db)
    result = pub.resume_publication(restored, task)
    assert len(result['result_refs']) == 1
    assert pub.publication_state(restored, task)['remaining'] == [targets[1]]


def test_legacy_task_keeps_original_completion(tmp_path):
    store = TaskStore(tmp_path)
    task = store.submit(OWNER, 'old', {'text': 'old'}, {'channel': 'web'}, 'cfg')
    task = store.claim(OWNER, task['task_id'])
    assert pub.publication_state(store, task) is None
    assert store.complete(OWNER, task['task_id'], 1, 'transient', [])['status'] == 'succeeded'


def test_prepare_conflict_rejected_before_any_wiki_prepare(env, monkeypatch):
    import copy
    store, task, wiki, targets, prepare = env
    sid, candidate, pages = prepare()
    monkeypatch.setattr(WikiService, 'prepare', lambda *a, **k: pytest.fail('已登记提交不可再次prepare'))
    assert pub.prepare_publication(store, task, sid, [targets[0]], pages, wiki.workspace)['batch_id'] == candidate['batch_id']
    changed = copy.deepcopy(pages)
    changed[0]['summary'] = '异载荷必须在知识候选写入前拒绝。'
    with pytest.raises(TaskError, match='idempotency_conflict'):
        pub.prepare_publication(store, task, sid, [targets[0]], changed, wiki.workspace)
    with pytest.raises(TaskError, match='idempotency_conflict'):
        pub.prepare_publication(store, task, sid, [targets[1]], pages, wiki.workspace)


def test_concurrent_prepare_reuses_one_candidate(env, monkeypatch):
    store, task, wiki, targets, prepare = env
    _, _, pages = prepare()
    pages[0]['title'] = '新的并发主题'
    calls = []
    original = WikiService.prepare
    def tracked(self, *a, **kw):
        calls.append(True)
        return original(self, *a, **kw)
    monkeypatch.setattr(WikiService, 'prepare', tracked)
    def submit(_):
        return pub.prepare_publication(store, task, 'new', [targets[1]], pages, wiki.workspace)
    with ThreadPoolExecutor(max_workers=4) as pool:
        candidates = list(pool.map(submit, range(8)))
    assert len(calls) == 1
    assert len({b['batch_id'] for b in candidates}) == 1


def test_old_attempt_cannot_submit_after_restart(env):
    store, old_task, wiki, targets, prepare = env
    _, candidate, pages = prepare()
    store.reconcile()
    task = store.get(OWNER, old_task['task_id'])
    store._change(OWNER, task['task_id'], task['attempt'], {'needs_reconcile'}, status='queued')
    store.claim(OWNER, task['task_id'])
    with pytest.raises(TaskError, match='version_conflict'):
        pub.prepare_publication(store, old_task, 'late', [targets[1]], pages, wiki.workspace)
    with pytest.raises(TaskError, match='version_conflict'):
        pub.publish_batch(store, old_task, 's0')
    assert not store.get(OWNER, task['task_id'])['result_refs']


def test_failed_candidate_is_retained_without_false_coverage(env, monkeypatch):
    store, task, wiki, targets, prepare = env
    prepare()
    pub.publish_batch(store, task, 's0')
    prepare(1)
    monkeypatch.setattr(WikiService, 'publish', lambda *a: (_ for _ in ()).throw(TaskError('index_failed')))
    with pytest.raises(TaskError, match='index_failed'):
        pub.publish_batch(store, task, 's1')
    state = pub.publication_state(store, task)
    assert state['remaining'] == [targets[1]]
    assert state['batches']['s1']['status'] == 'prepared'
    assert len(store.get(OWNER, task['task_id'])['result_refs']) == 1


def test_bounded_batch_limit_fails_before_prepare(env, monkeypatch):
    store, task, wiki, targets, prepare = env
    _, _, pages = prepare()
    monkeypatch.setattr(pub, 'MAX_BATCHES', 1)
    monkeypatch.setattr(WikiService, 'prepare', lambda *a, **k: pytest.fail('超过上限不得prepare'))
    with pytest.raises(TaskError, match='limit_exceeded'):
        pub.prepare_publication(store, task, 'extra', [targets[1]], pages, wiki.workspace)


def test_cancel_wins_final_completion_race(env):
    store, task, wiki, targets, prepare = env
    prepare(coverage=targets)
    pub.publish_batch(store, task, 's0')
    task = store.cancel(OWNER, task['task_id'], 1)
    with pytest.raises(TaskError, match='version_conflict'):
        pub.finish_publication(store, task)
    assert task['status'] == 'cancelled' and len(task['result_refs']) == 1


def test_whole_completion_wins_late_cancel(env):
    store, task, wiki, targets, prepare = env
    prepare(coverage=targets)
    pub.publish_batch(store, task, 's0')
    task = pub.finish_publication(store, task)
    with pytest.raises(TaskError, match='version_conflict'):
        store.cancel(OWNER, task['task_id'], 1)
    assert task['status'] == 'succeeded'


def test_receipt_limit_checked_before_wiki_commit(env, monkeypatch):
    store, task, wiki, targets, prepare = env
    prepare()
    state = pub.publication_state(store, task)
    state.pop('remaining')
    monkeypatch.setattr(pub, 'MAX_RECORD_BYTES', len(json.dumps(state, ensure_ascii=False).encode()) + 50)
    monkeypatch.setattr(WikiService, 'publish', lambda *a: pytest.fail('回执无法持久化不得发布'))
    with pytest.raises(TaskError, match='limit_exceeded'):
        pub.publish_batch(store, task, 's0')


def test_cross_task_candidate_cannot_be_registered(env):
    store, task, wiki, targets, prepare = env
    _, batch, _ = prepare()
    other = store.submit(OWNER, 'other', {'text': 'other'}, {'channel': 'web'}, 'cfg')
    other = store.claim(OWNER, other['task_id'])
    other = pub.register_target(store, other, targets)
    with pytest.raises(TaskError, match='forbidden'):
        pub.register_batch(store, other, 'stolen', [targets[0]], batch['batch_id'], wiki.workspace)


def test_real_process_exit_after_wiki_commit_recovers_without_republishing(env, monkeypatch):
    import subprocess
    import sys
    store, task, wiki, targets, prepare = env
    prepare()
    script = '''
import json, os, sys
from common.personal_tasks import TaskStore
from agent.knowledge import reading_publication as pub
store = TaskStore(sys.argv[1])
task = json.loads(sys.argv[2])
pub._receipt = lambda *args: os._exit(29)
pub.publish_batch(store, task, 's0')
'''
    child = subprocess.run([sys.executable, '-c', script, str(store.root), json.dumps(task)],
                           timeout=20, capture_output=True, text=True)
    assert child.returncode == 29, child.stderr
    restarted = TaskStore(store.root)
    assert restarted.get(OWNER, task['task_id'])['result_refs'] == []
    restarted.reconcile()
    current = restarted.get(OWNER, task['task_id'])
    restarted._change(OWNER, task['task_id'], current['attempt'], {'needs_reconcile'}, status='queued')
    current = restarted.claim(OWNER, task['task_id'])
    monkeypatch.setattr(WikiService, 'publish', lambda *a: pytest.fail('进程崩溃后不能重复publish'))
    result = pub.resume_publication(restarted, current)
    assert result['status'] == 'running' and len(result['result_refs']) == 1
    assert pub.publication_state(restarted, current)['remaining'] == [targets[1]]


def test_reading_target_upgrades_database_barrier_without_changing_task_record(tmp_path):
    import sqlite3
    store = TaskStore(tmp_path)
    with sqlite3.connect(store.db) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
    task = store.submit(OWNER, 'new', {'text': '完整阅读'}, {'channel': 'web'}, 'cfg')
    task = pub.register_target(store, task, ['revision:1'])
    assert task['schema_version'] == 1
    with sqlite3.connect(store.db) as db:
        version = db.execute('PRAGMA user_version').fetchone()[0]
        assert version == 2 and version not in (0, 1)  # 旧 TaskStore 构造器拒绝。
    reopened = TaskStore(tmp_path)
    assert pub.publication_state(reopened, task)['targets'] == ['revision:1']
    with sqlite3.connect(store.db) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2


def test_failed_target_registration_does_not_upgrade_old_database(tmp_path):
    import sqlite3
    store = TaskStore(tmp_path)
    task = store.submit(OWNER, 'old', {'text': '原路径'}, {'channel': 'web'}, 'cfg')
    with pytest.raises(TaskError):
        pub.register_target(store, task, [])
    with sqlite3.connect(store.db) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
