"""生产工具入口的幂等、可纠正输入及观察保存中断回归；只使用隔离根。"""
from copy import deepcopy
import hashlib
import json

import pytest

from agent.knowledge.personal_assistant import SourceService
from agent.knowledge.wiki import WikiService
from common.personal_tasks import OWNER
from tests import test_reading_ingest_flow as fixtures
from tests.test_reading_ingest_flow import deliver, submission


@pytest.fixture
def flow(tmp_path, monkeypatch):
    yield from fixtures.flow.__wrapped__(tmp_path, monkeypatch)


def snapshot(root, store, task):
    """同时核对不可变文件、登记表和任务，拒绝不能偷偷追加观察或候选。"""
    source = SourceService(root)
    tables = ('reading_aliases', 'reading_submissions', 'reading_parse_heads',
              'wiki_batches', 'wiki_publications')
    with source.transaction() as db:
        rows = {table: [tuple(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in tables}
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in (source.root / 'parses').glob('*.json')}
    return rows, files, store.get(OWNER, task['task_id'])


def read_submission(flow, completed=True):
    root, store, task, entry, read, tool = flow
    result = read.execute(dict(path=entry['path'], pages='1'))
    assert result.status == 'success', result.result
    if completed:
        deliver(result, task)
    return result, submission(task, result.result)


@pytest.mark.parametrize('field', ['pages', 'reading_evidence', 'reading_coverage', 'reading_submission_id'])
def test_succeeded_rejects_changed_submission_without_side_effects(flow, field):
    root, store, task, entry, read, tool = flow
    _, args = read_submission(flow)
    assert tool.execute(args).status == 'success'
    before = snapshot(root, store, task)
    changed = deepcopy(args)
    if field == 'pages':
        changed[field][0]['summary'] = '改动过的摘要不得冒充原提交成功。'
    elif field == 'reading_evidence':
        changed[field][0]['observation'] = '改动过的观察。'
    elif field == 'reading_coverage':
        changed[field][0]['segment_id'] = 'different-segment'
    else:
        changed[field] = 'different-submission'
    result = tool.execute(changed)
    assert result.status == 'error' and result.result['code'] == 'idempotency_conflict'
    assert snapshot(root, store, task) == before


def test_succeeded_identical_replay_never_prepares_or_publishes_again(flow, monkeypatch):
    root, store, task, entry, read, tool = flow
    _, args = read_submission(flow)
    first = tool.execute(args)
    assert first.status == 'success'
    before = snapshot(root, store, task)
    monkeypatch.setattr(WikiService, 'prepare', lambda *a, **k: pytest.fail('重放不得prepare'))
    monkeypatch.setattr(WikiService, 'publish', lambda *a, **k: pytest.fail('重放不得publish'))
    result = tool.execute(deepcopy(args))
    assert result.status == 'success'
    assert [r['publish_id'] for r in result.result['result_refs']] == [
        r['publish_id'] for r in first.result['result_refs']]
    assert snapshot(root, store, task) == before


def test_incomplete_delivery_keeps_running_and_can_be_corrected(flow):
    root, store, task, entry, read, tool = flow
    media, args = read_submission(flow, completed=False)
    before = snapshot(root, store, task)
    result = tool.execute(args)
    assert result.status == 'error' and result.result['code'] == 'reading_incomplete'
    assert snapshot(root, store, task) == before
    assert store.get(OWNER, task['task_id'])['status'] == 'running'
    deliver(media, task)
    result = tool.execute(args)
    assert result.status == 'success' and result.result['status'] == 'succeeded'


@pytest.mark.parametrize('defect', ['missing_alias', 'unused_evidence', 'mixed_source'])
def test_alias_validation_precedes_observation_and_candidate_writes(flow, defect):
    root, store, task, entry, read, tool = flow
    _, args = read_submission(flow)
    before = snapshot(root, store, task)
    invalid = deepcopy(args)
    source = invalid['pages'][0]['blocks'][0]['sources'][0]
    if defect == 'missing_alias':
        source['evidence_key'] = 'missing'
    elif defect == 'unused_evidence':
        extra = deepcopy(invalid['reading_evidence'][0])
        extra['evidence_key'] = 'unused'
        invalid['reading_evidence'].append(extra)
    else:
        source['document_id'] = 'mixed-form'
    result = tool.execute(invalid)
    assert result.status == 'error' and result.result['code'] == 'invalid_request'
    assert snapshot(root, store, task) == before
    assert tool.execute(args).status == 'success'


class PowerLoss(BaseException):
    pass


@pytest.fixture
def saved_observations(flow, monkeypatch):
    """中断在观察已保存、publication candidate 尚未保存的真实边界。"""
    import agent.knowledge.reading_publication as publication
    root, store, task, entry, read, tool = flow
    _, args = read_submission(flow)
    with monkeypatch.context() as patch:
        patch.setattr(publication, 'prepare_publication', lambda *a, **k: (_ for _ in ()).throw(PowerLoss()))
        with pytest.raises(PowerLoss):
            tool.execute(args)
    rows, _, current = snapshot(root, store, task)
    assert len(rows['reading_aliases']) == len(rows['reading_submissions']) == 1
    assert rows['wiki_batches'] == []
    assert not current.get('reading_tool_submissions')
    return args


def test_saved_observation_replay_reuses_parse_and_alias(flow, saved_observations):
    root, store, task, entry, read, tool = flow
    rows, files, _ = snapshot(root, store, task)
    result = tool.execute(deepcopy(saved_observations))
    assert result.status == 'success' and result.result['status'] == 'succeeded'
    after_rows, after_files, _ = snapshot(root, store, task)
    assert after_files == files
    assert after_rows['reading_aliases'] == rows['reading_aliases']
    assert after_rows['reading_submissions'] == rows['reading_submissions']
    assert len(after_rows['wiki_batches']) == len(after_rows['wiki_publications']) == 1


@pytest.mark.parametrize('change', ['observation', 'evidence_key'])
def test_registry_rejects_changed_evidence_without_raw_checkpoint(flow, saved_observations, change):
    root, store, task, entry, read, tool = flow
    before = snapshot(root, store, task)
    changed = deepcopy(saved_observations)
    if change == 'observation':
        changed['reading_evidence'][0]['observation'] = '不同的图文观察。'
    else:
        changed['reading_evidence'][0]['evidence_key'] = 'new-key'
        changed['pages'][0]['blocks'][0]['sources'][0]['evidence_key'] = 'new-key'
    result = tool.execute(changed)
    assert result.status == 'error' and result.result['code'] == 'idempotency_conflict'
    assert snapshot(root, store, task) == before
    assert store.get(OWNER, task['task_id'])['status'] == 'running'
    assert tool.execute(saved_observations).status == 'success'


@pytest.mark.parametrize('change', ['summary', 'content', 'extra_page', 'drop_evidence'])
def test_observation_only_checkpoint_rejects_changed_pages(flow, saved_observations, monkeypatch, change):
    root, store, task, entry, read, tool = flow
    before = snapshot(root, store, task)
    changed = deepcopy(saved_observations)
    if change == 'summary':
        changed['pages'][0]['summary'] = '观察已保存后的不同页面载荷。'
    elif change == 'content':
        changed['pages'][0]['blocks'][0]['content'] = '同一证据下更改了知识正文。'
    elif change == 'extra_page':
        other = deepcopy(changed['pages'][0])
        other['title'] = '同一提交下新增的知识页'
        changed['pages'].append(other)
    else:
        alias = json.loads(before[0]['reading_aliases'][0][1])
        changed['reading_evidence'] = []
        changed['pages'][0]['blocks'][0]['sources'] = [alias['ref']]
    with monkeypatch.context() as patch:
        patch.setattr(WikiService, 'prepare', lambda *a, **k: pytest.fail('同提交异pages须在prepare前拒绝'))
        result = tool.execute(changed)
    assert result.status == 'error' and result.result['code'] == 'idempotency_conflict'
    assert snapshot(root, store, task) == before
    result = tool.execute(deepcopy(saved_observations))
    assert result.status == 'success' and result.result['status'] == 'succeeded'


def test_observation_and_submission_fingerprint_commit_atomically(flow, monkeypatch):
    from contextlib import contextmanager
    root, store, task, entry, read, tool = flow
    _, args = read_submission(flow)
    before_rows, _, before_task = snapshot(root, store, task)
    transaction = SourceService.transaction
    attempted = []
    class InterruptedConnection:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *values):
            if sql.lstrip().upper().startswith('INSERT INTO READING_SUBMISSIONS'):
                attempted.append(True)
                raise PowerLoss()
            return self.db.execute(sql, *values)

        def __getattr__(self, name):
            return getattr(self.db, name)

    @contextmanager
    def interrupted(self):
        with transaction(self) as db:
            yield InterruptedConnection(db)

    with monkeypatch.context() as patch:
        patch.setattr(SourceService, 'transaction', interrupted)
        with pytest.raises(PowerLoss):
            tool.execute(args)
    assert attempted
    after_rows, _, after_task = snapshot(root, store, task)
    assert after_rows == before_rows
    assert after_task == before_task
    # 内容寻址文件可作为孤儿留存，重放复用；权威表不能留半条提交。
    result = tool.execute(deepcopy(args))
    assert result.status == 'success' and result.result['status'] == 'succeeded'
    rows, _, _ = snapshot(root, store, task)
    assert len(rows['reading_aliases']) == len(rows['reading_submissions']) == 1
    assert len(rows['wiki_publications']) == 1


def test_prepared_candidate_rejects_changed_pages_without_raw_checkpoint(flow, monkeypatch):
    root, store, task, entry, read, tool = flow
    _, args = read_submission(flow)
    original = store._change
    def interrupt(*a, **kw):
        if 'reading_tool_submissions' in kw:
            raise PowerLoss()
        return original(*a, **kw)
    with monkeypatch.context() as patch:
        patch.setattr(store, '_change', interrupt)
        with pytest.raises(PowerLoss):
            tool.execute(args)
    before = snapshot(root, store, task)
    assert len(before[0]['wiki_batches']) == 1
    assert not before[2].get('reading_tool_submissions')
    changed = deepcopy(args)
    changed['pages'][0]['summary'] = '不同的候选载荷。'
    with monkeypatch.context() as patch:
        patch.setattr(WikiService, 'prepare', lambda *a, **k: pytest.fail('冲突必须先于prepare'))
        result = tool.execute(changed)
    assert result.status == 'error' and result.result['code'] == 'idempotency_conflict'
    assert snapshot(root, store, task) == before
    assert tool.execute(args).status == 'success'


@pytest.mark.parametrize('change', ['pages', 'evidence'])
def test_saved_raw_checkpoint_rejects_conflict_before_registry_writes(flow, monkeypatch, change):
    import agent.knowledge.reading_publication as publication
    from agent.knowledge.reading import ReadingService
    root, store, task, entry, read, tool = flow
    _, args = read_submission(flow)
    with monkeypatch.context() as patch:
        patch.setattr(publication, 'publish_batch', lambda *a, **k: (_ for _ in ()).throw(PowerLoss()))
        with pytest.raises(PowerLoss):
            tool.execute(args)
    before = snapshot(root, store, task)
    assert before[2]['reading_tool_submissions'] and before[2]['status'] == 'running'
    changed = deepcopy(args)
    if change == 'pages':
        changed['pages'][0]['summary'] = '不同的正文摘要。'
    else:
        changed['reading_evidence'][0]['observation'] = '不同的证据观察。'
    with monkeypatch.context() as patch:
        patch.setattr(ReadingService, 'resolve_evidence', lambda *a, **k: pytest.fail('冲突须先于registry写入'))
        patch.setattr(WikiService, 'prepare', lambda *a, **k: pytest.fail('冲突须先于wiki写入'))
        result = tool.execute(changed)
    assert result.status == 'error' and result.result['code'] == 'idempotency_conflict'
    assert snapshot(root, store, task) == before
    assert tool.execute(args).status == 'success'


def test_succeeded_replay_accepts_evidence_array_reordering(flow):
    root, store, task, entry, read, tool = flow
    _, args = read_submission(flow)
    extra = deepcopy(args['reading_evidence'][0])
    extra.update(evidence_key='second', observation='图形背景为白色。')
    args['reading_evidence'].append(extra)
    args['pages'][0]['blocks'][0]['sources'].append(dict(
        reading_id=extra['reading_id'], evidence_key=extra['evidence_key']))
    assert tool.execute(args).status == 'success'
    before = snapshot(root, store, task)
    args['reading_evidence'].reverse()
    result = tool.execute(args)
    assert result.status == 'success'
    assert snapshot(root, store, task) == before


def test_worker_adopts_saved_readings_before_publication_resume_and_agent(flow, monkeypatch):
    from common import personal_worker as worker
    from agent.knowledge.reading import ReadingService
    import agent.knowledge.reading_publication as publication
    root, store, task, entry, read, tool = flow
    reading, args = read_submission(flow)
    store.fail(OWNER, task['task_id'], task['attempt'], 'reading_incomplete', 'parse')
    retry = store.retry(OWNER, task['task_id'], task['attempt'])
    events = []
    original_resume = ReadingService.resume_all
    original_publication = publication.resume_publication
    def resume_readings(self, current):
        assert self.workspace == root.resolve()
        events.append('readings')
        return original_resume(self, current)
    def resume_publication(task_store, current):
        assert events and events[0] == 'readings'
        events.append('publication')
        return original_publication(task_store, current)
    def agent(current):
        events.append('agent')
        saved = ReadingService(root, task_store=store).get(current, reading.result['reading_id'])
        assert saved['attempt'] == task['attempt'] < current['attempt']
        assert saved['segments'][0]['state'] == 'completed'
        result = tool.execute(args)
        assert result.status == 'success', result.result
        return '已复用成功阅读并完成发布'
    monkeypatch.setattr(worker, 'TaskStore', lambda: store)
    monkeypatch.setattr(ReadingService, 'resume_all', resume_readings)
    monkeypatch.setattr(publication, 'resume_publication', resume_publication)
    monkeypatch.setattr(worker, 'run_agent', agent)
    worker._run(task['task_id'])
    result = store.get(OWNER, task['task_id'])
    assert result['status'] == 'succeeded' and result['attempt'] == retry['attempt']
    assert len(result['result_refs']) == 1
    assert events[:3] == ['readings', 'publication', 'agent']


def test_withdrawn_source_read_cache_never_claims_searchable(flow, monkeypatch):
    root, store, task, entry, read, tool = flow
    _, args = read_submission(flow)
    published = tool.execute(args)
    assert published.status == 'success', published.result
    refs = published.result['result_refs']
    service = SourceService(root)
    document = service.get(refs[0]['document_id'])
    service.change(document['id'], action='withdraw', expected_state=document['state'], reason='测试撤回')
    before = snapshot(root, store, task)
    monkeypatch.setattr(SourceService, 'parse', lambda *a, **k: pytest.fail('完成任务缓存不得重新解析'))
    monkeypatch.setattr(WikiService, 'publish', lambda *a, **k: pytest.fail('撤回后不得重新发布'))
    result = read.execute(dict(path=entry['path'], pages='1'))
    assert result.status == 'success', result.result
    assert result.result['searchable'] is False
    assert result.result['media_attached'] is False and not result.media
    assert [r['publish_id'] for r in result.result['result_refs']] == [r['publish_id'] for r in refs]
    assert store.get(OWNER, task['task_id'])['status'] == 'succeeded'
    assert snapshot(root, store, task) == before
