"""KF-08：显式图文重整入场固定目标，旧入口仍沿用原契约。"""
import pytest

from agent.knowledge.wiki import WikiService
from agent.knowledge.figure_publication import require_explicit_figures
from common.personal_tasks import OWNER, TaskError
from models.config_revision import capture_config_revision
from test_personal_web import api as api_fixture, published_task
from test_personal_reorganize_api import request

api = api_fixture


def topics(tmp_path, service, ref):
    parsed = service.parse(ref['revision_id'])
    source = dict(document_id=ref['document_id'], revision_id=ref['revision_id'],
                  parse_id=parsed['parse_id'], ordinal=0)
    wiki = WikiService(tmp_path / 'workspace')
    pages = [dict(page_id=f'topic-{i}', title=f'主题{i}', category='技术', summary='测试摘要。',
                  blocks=[dict(content='第一行证据', sources=[source])],
                  related_page_ids=[], depends_on=[]) for i in range(4)]
    return wiki.publish(wiki.prepare(pages)['batch_id'])


def test_figures_reorganize_fixes_four_targets_and_idempotency(api, tmp_path, monkeypatch):
    # 此用例进入真实完成屏障，恢复共享 API fixture 替换的配置捕获函数。
    monkeypatch.setattr('models.config_revision.capture_config_revision', capture_config_revision)
    mod, store, _ = api
    service, parent, ref = published_task(mod, store, tmp_path)
    refs = topics(tmp_path, service, ref)
    requirement = dict(schema_version=1, page_ids=[p['page_id'] for p in refs])
    body = request(ref, figure_requirement=requirement)
    child = mod._reorganize(store, parent['task_id'], 1, body)
    assert child['payload']['figure_requirement'] == requirement
    assert 'visual_interpretation' in child['payload']['text']
    assert 'legacy' in child['payload']['text']
    assert mod._reorganize(store, parent['task_id'], 1, body)['task_id'] == child['task_id']
    with pytest.raises(TaskError, match='idempotency_conflict'):
        mod._reorganize(store, parent['task_id'], 1, request(ref))
    with pytest.raises(TaskError, match='invalid_request'):
        require_explicit_figures(child, [{'blocks': [{'content': '旧调用漏交图片'}]}])
    store.claim(OWNER, child['task_id'])
    with pytest.raises(TaskError, match='figure_incomplete'):
        store.complete(OWNER, child['task_id'], 1, 'published', refs)
    assert store.get(OWNER, child['task_id'])['status'] == 'running'


@pytest.mark.parametrize('requirement', [None, {}, {'schema_version': True},
    {'schema_version': 2}, {'schema_version': 1, 'extra': 1},
    {'schema_version': 1, 'page_ids': []}, {'schema_version': 1, 'page_ids': ['x', 'x']},
    {'schema_version': 1, 'page_ids': [False]}, {'schema_version': 1, 'page_ids': ['foreign']}])
def test_invalid_or_foreign_figure_targets_never_admitted(api, tmp_path, requirement):
    mod, store, _ = api
    _, parent, ref = published_task(mod, store, tmp_path)
    with pytest.raises(TaskError):
        mod._reorganize(store, parent['task_id'], 1, request(ref, figure_requirement=requirement))
    assert len(store.list()) == 1
