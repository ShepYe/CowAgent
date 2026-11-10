from common.personal_tasks import OWNER, TaskStore
from common import personal_worker as worker
from models.config_revision import ConfigUnavailable


def setup_worker(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    monkeypatch.setattr(worker, "TaskStore", lambda: store)
    monkeypatch.setattr("models.config_revision.require_config_revision", lambda value: None)
    return store


def task(store):
    return store.submit(OWNER, "req", {"text": "test"}, {"channel": "web"}, "cfg-1")


def test_changed_configuration_stops_before_agent(tmp_path, monkeypatch):
    store = setup_worker(tmp_path, monkeypatch)
    t = task(store)
    def unavailable(value):
        raise ConfigUnavailable()
    monkeypatch.setattr("models.config_revision.require_config_revision", unavailable)
    monkeypatch.setattr(worker, "run_agent", lambda task: (_ for _ in ()).throw(AssertionError("不能调用模型")))
    worker._run(t["task_id"])
    after = store.get(OWNER, t["task_id"])
    assert after["status"] == "failed" and after["error"]["code"] == "config_unavailable"


def test_cancel_during_agent_does_not_notify_late_answer(tmp_path, monkeypatch):
    store = setup_worker(tmp_path, monkeypatch)
    t = task(store)
    sent = []
    def cancelled(task):
        store.cancel(OWNER, task["task_id"], task["attempt"])
        return "旧模型结果声称发布成功"
    monkeypatch.setattr(worker, "run_agent", cancelled)
    monkeypatch.setattr(worker, "_notify", lambda store, task, text: sent.append(text))
    worker._run(t["task_id"])
    assert store.get(OWNER, t["task_id"])["status"] == "cancelled"
    assert all("旧模型结果" not in text for text in sent)


def test_unknown_notification_reuses_business_result_without_agent(tmp_path, monkeypatch):
    store = setup_worker(tmp_path, monkeypatch)
    t = task(store)
    store.claim(OWNER, t["task_id"])
    store.complete(OWNER, t["task_id"], 1, "published", [{"publish_id": "p1"}])
    store.notification(OWNER, t["task_id"], "unknown")
    calls = []
    monkeypatch.setattr(worker, "run_agent", lambda task: (_ for _ in ()).throw(AssertionError("不得重做业务")))
    monkeypatch.setattr(worker, "_notify", lambda store, task, text: calls.append(task["result_refs"]))
    worker._run(t["task_id"])
    assert calls == [[{"publish_id": "p1"}]]


def test_reconcile_queues_existing_download_without_losing_receipt(tmp_path, monkeypatch):
    store = setup_worker(tmp_path, monkeypatch)
    t = task(store)
    store.claim(OWNER, t["task_id"])
    calls = []
    monkeypatch.setattr(worker, "_reconciled", False)
    monkeypatch.setattr(worker, "enqueue", lambda task_id: calls.append(task_id))
    worker.recover()
    assert calls == [t["task_id"]]
    assert store.get(OWNER, t["task_id"])["status"] == "queued"


def test_desktop_republish_runs_registered_source_with_expected_state(tmp_path, monkeypatch):
    from agent.knowledge.personal_assistant import SourceService
    from common import state_dir
    store = setup_worker(tmp_path / 'tasks', monkeypatch)
    workspace = tmp_path / 'workspace'
    monkeypatch.setattr(state_dir, 'state_root', lambda *a: workspace)
    workspace.mkdir()
    original = tmp_path / 'source.txt'
    original.write_text('budget 42', encoding='utf-8')
    service = SourceService(workspace)
    parent = store.submit(OWNER, 'parent', {'text': '分享'}, {'channel': 'web'}, 'cfg')
    store.attach(OWNER, parent['task_id'], original, 'source.txt', 'file')
    store.claim(OWNER, parent['task_id'])
    source = service.receive(original, filename='source.txt', request_id='source', origin={'channel': 'web'})
    service.change(source['document_id'], purpose='long_term', expected_state=0, reason='分享')
    ref = service.publish(service.prepare(source['revision_id'])['candidate_id'])
    store.complete(OWNER, parent['task_id'], 1, 'published', [ref])
    service.change(source['document_id'], action='withdraw', expected_state=1, reason='撤回')
    directive = dict(document_id=source['document_id'], revision_id=source['revision_id'],
                     expected_state=2, action='republish', purpose='long_term', reason='明确恢复')
    child = store.submit(OWNER, 'republish', {'source_action': directive},
                         {'channel': 'web', 'parent_task_id': parent['task_id']}, 'cfg')
    child = store.claim(OWNER, child['task_id'])
    worker.execute_source_action(store, child)
    after = store.get(OWNER, child['task_id'])
    assert after['status'] == 'succeeded' and service.search('budget')
    # Another withdrawal committed after admission wins; the old expected state
    # cannot be silently updated to republish a newer business decision.
    stale = store.submit(OWNER, 'stale', {'source_action': directive},
                         {'channel': 'web', 'parent_task_id': parent['task_id']}, 'cfg')
    stale = store.claim(OWNER, stale['task_id'])
    import pytest
    from common.personal_tasks import TaskError
    with pytest.raises(TaskError, match='version_conflict'):
        worker.execute_source_action(store, stale)
