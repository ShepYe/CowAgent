"""摘要升级后的真实持久候选、任务提交间隙和拓扑恢复。"""
import copy
import json

import pytest

from agent.knowledge.parser import SourceError
from agent.knowledge.wiki import WikiService
from agent.tools.knowledge_ingest import KnowledgeIngest
from common import personal_worker as worker
from common.personal_tasks import OWNER
from common.runtime_identity import identity_scope
from models.config_revision import ConfigUnavailable
from test_personal_source_derivation import derivation as derivation_fixture, draft

derivation = derivation_fixture


def fail_index(*args):
    raise SourceError('index_failed', '模拟索引中断')


def start_failed(env, monkeypatch, session='summary-recovery'):
    page = draft(env)
    page['summary'] = '说明设计预算及其原始依据，供后续工程核验。'
    with identity_scope(user_id=OWNER, session_id=session), monkeypatch.context() as patch:
        patch.setattr(WikiService, '_verify_index', staticmethod(fail_index))
        result = KnowledgeIngest(env.config).execute(dict(purpose='long_term', reason='整理', pages=[page]))
    assert result.status == 'error'
    task = env.store.list()[0]
    assert task['wiki_batch_id'] and task['error']['code'] == 'index_failed'
    return task, page


def run_worker(env, monkeypatch, task):
    monkeypatch.setattr(worker, 'TaskStore', lambda: env.store)
    monkeypatch.setattr(worker, '_notify', lambda *a: None)
    monkeypatch.setattr(worker, 'run_agent', lambda *a: pytest.fail('保存的成品恢复不应调用模型'))
    env.store.retry(OWNER, task['task_id'], task['attempt'])
    worker._run(task['task_id'])
    return env.store.get(OWNER, task['task_id'])


@pytest.mark.parametrize('old_candidate', [False, True])
def test_worker_resumes_saved_summary_or_legacy_absence_without_model(derivation, monkeypatch, old_candidate):
    env = derivation
    task, page = start_failed(env, monkeypatch)
    if old_candidate:
        # 模拟升级前落盘的候选；不走新输入接口伪造兼容行为。
        with env.source.transaction() as db:
            record = json.loads(db.execute('SELECT record FROM wiki_batches WHERE id=?', (task['wiki_batch_id'],)).fetchone()[0])
            for item in record['pages']:
                item.pop('summary', None)
            db.execute('UPDATE wiki_batches SET record=? WHERE id=?', (json.dumps(record, ensure_ascii=False), task['wiki_batch_id']))
    result = run_worker(env, monkeypatch, task)
    assert result['status'] == 'succeeded'
    ref = result['result_refs'][0]
    assert ref['summary'] == ('' if old_candidate else page['summary'])
    assert WikiService(env.root).current(ref['page_id'])['summary'] == ref['summary']
    assert env.wiki.version(ref['publish_id'])['summary'] == ref['summary']


def test_unavailable_config_keeps_candidate_and_new_session_admits_current_config(derivation, monkeypatch):
    env = derivation
    task, page = start_failed(env, monkeypatch)
    with identity_scope(user_id=OWNER, session_id='summary-recovery'):
        same = KnowledgeIngest(env.config).execute(dict(purpose='long_term', reason='整理', pages=[page]))
        assert same.status == 'error' and len(env.store.list()) == 1
        assert env.store.list()[0]['task_id'] == task['task_id']
    with monkeypatch.context() as patch:
        def unavailable(*a):
            raise ConfigUnavailable()
        patch.setattr('models.config_revision.require_config_revision', unavailable)
        result = run_worker(env, patch, task)
    assert result['error']['code'] == 'config_unavailable'
    assert result['wiki_batch_id'] == task['wiki_batch_id'] and not result['result_refs']
    with env.source.transaction() as db:
        assert db.execute('SELECT published FROM wiki_batches WHERE id=?', (task['wiki_batch_id'],)).fetchone()[0] == 0
    with identity_scope(user_id=OWNER, session_id='new-current-config-session'):
        new = KnowledgeIngest(env.config).execute(dict(purpose='long_term', reason='整理', pages=[page]))
    assert new.status == 'success', new.result
    assert new.result['task_id'] != task['task_id']
    assert env.store.get(OWNER, task['task_id'])['wiki_batch_id'] == task['wiki_batch_id']
    assert new.result['result_refs'][0]['summary'] == page['summary']


def test_restart_after_publish_before_task_complete_replays_same_version(derivation, monkeypatch):
    env = derivation
    task, page = start_failed(env, monkeypatch)
    env.store.retry(OWNER, task['task_id'], task['attempt'])
    running = env.store.claim(OWNER, task['task_id'])
    refs = env.wiki.publish(running['wiki_batch_id'])
    # 此时发布已提交，任务 complete 尚未执行，模拟进程中断。
    before = env.wiki.history()['total']
    env.store.reconcile()
    monkeypatch.setattr(worker, 'TaskStore', lambda: env.store)
    monkeypatch.setattr(worker, '_reconciled', False)
    queued = []
    monkeypatch.setattr(worker, 'enqueue', queued.append)
    monkeypatch.setattr(worker, '_notify', lambda *a: None)
    monkeypatch.setattr(worker, 'run_agent', lambda *a: pytest.fail('已发布结果不可重新生成'))
    worker.recover()
    assert task['task_id'] in queued
    worker._run(task['task_id'])
    result = env.store.get(OWNER, task['task_id'])
    assert result['status'] == 'succeeded' and result['result_refs'] == refs
    assert result['result_refs'][0]['summary'] == page['summary']
    assert env.wiki.history()['total'] == before


def test_topological_batches_keep_upstream_once_and_resume_paused_downstream(derivation, monkeypatch):
    env = derivation
    downstream = draft(env, summary='基于上游预算依据说明设计结论。')
    old = env.wiki.publish(env.wiki.prepare([downstream], task_id='initial-downstream')['batch_id'])[0]
    upstream = draft(env, page_id=env.ref['page_id'], expected_version=env.ref['page_version'],
                     depends_on=[], summary='提供修订后的预算依据和适用范围。')
    new = env.wiki.publish(env.wiki.prepare([upstream], task_id='update-upstream')['batch_id'])[0]
    assert env.wiki.current(old['page_id']) is None
    next_page = copy.deepcopy(downstream)
    next_page.update(page_id=old['page_id'], expected_version=old['page_version'], summary='采用新版上游预算依据核验设计结论。',
                     depends_on=[dict(page_id=new['page_id'], page_version=new['page_version'])])
    batch = env.wiki.prepare([next_page], task_id='update-downstream')
    with monkeypatch.context() as patch:
        patch.setattr(WikiService, '_verify_index', staticmethod(fail_index))
        with pytest.raises(SourceError):
            env.wiki.publish(batch['batch_id'])
    assert env.wiki.current(old['page_id']) is None
    recovered = WikiService(env.root).publish(batch['batch_id'])[0]
    assert recovered['page_id'] == old['page_id'] and recovered['summary'] == next_page['summary']
    assert env.wiki.current(new['page_id'])['page_version'] == new['page_version']
    assert env.wiki.version(old['publish_id'])['summary'] == downstream['summary']
    with env.source.transaction() as db:
        assert db.execute('SELECT count(*) FROM wiki_publications WHERE page=?', (new['page_id'],)).fetchone()[0] == 2


def test_summary_current_history_and_category_survive_complete_relocated_restore(tmp_path):
    from cli.commands.backup import create_backup_archive, restore_backup_archive
    from test_knowledge_wiki_publication import source, page, publish
    data = tmp_path / 'data'
    workspace = data / 'workspace'
    workspace.mkdir(parents=True)
    (data / 'config.json').write_text(json.dumps(dict(personal_assistant_enabled=True, agent_workspace=str(workspace))), encoding='utf-8')
    _, _, evidence = source(workspace)
    wiki = WikiService(workspace)
    first = publish(wiki, page(evidence, summary='旧版预算依据。'))[0]
    wiki.set_page_category(first['page_id'], first['page_version'], 0, '本人/收藏')
    second = publish(wiki, page(evidence, summary='新版预算依据及用途。', page_id=first['page_id'], expected_version=first['page_version']), task='second')[0]
    archive = tmp_path / 'full.zip'
    create_backup_archive(archive, data, workspace, complete=True)
    data.rename(tmp_path / 'hidden-old-data')
    restored = tmp_path / 'restored'
    restore_backup_archive(archive, restored, restored / 'workspace')
    new = WikiService(restored / 'workspace')
    assert new.current(first['page_id'])['summary'] == second['summary']
    assert new.current(first['page_id'])['category_mode'] == 'manual'
    assert new.current(first['page_id'])['category'] == '本人/收藏'
    assert new.version(first['publish_id'])['summary'] == first['summary']
    assert new.version(first['publish_id'])['current_status'] == 'superseded'
    assert new.search('星际通信')[0]['summary'] == second['summary']
    assert new.read_reference(second['publish_id'])['valid']
