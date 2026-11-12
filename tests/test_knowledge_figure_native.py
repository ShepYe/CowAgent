"""图文任务在媒体能力未知时保留真实缺口，不能静默降为旧视觉缓存。"""
from common.personal_tasks import OWNER
from tests.test_reading_ingest_flow import flow as flow_fixture

flow = flow_fixture


def test_figure_task_unknown_native_capability_does_not_parse_legacy(flow, monkeypatch):
    root, store, task, entry, read, _ = flow
    task = store._change(OWNER, task['task_id'], task['attempt'], {'running'},
        payload=dict(task['payload'], figure_requirement={'schema_version': 1}))
    monkeypatch.setattr('agent.knowledge.reading_capabilities.ensure_pdf_capability', lambda *args: False)
    from agent.knowledge.personal_assistant import SourceService
    monkeypatch.setattr(SourceService, 'parse', lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError('图文任务不能静默解析为legacy')))
    result = read.execute(dict(path=entry['path'], pages='1', visual=True))
    assert result.status == 'error'
    assert result.result['code'] == 'attachment_unsupported'
    assert result.result['media_attached'] is False and result.result['retryable'] is True
    assert not store.get(OWNER, task['task_id']).get('source_read_parses')
