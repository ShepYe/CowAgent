"""审查回归：旧发布兼容边界与投影故障下的失效持久性（规格 5.5）。"""
from pathlib import Path
import sqlite3

import pytest

from agent.knowledge import wiki as wiki_module
from agent.knowledge.parser import SourceError
from agent.knowledge.personal_assistant import SourceService, readable
from agent.knowledge.wiki import WikiService


@pytest.fixture
def registered_source(tmp_path):
    service = SourceService(tmp_path)
    original = tmp_path / 'original.txt'
    original.write_text('obsolete_budget_42', encoding='utf-8')
    receipt = service.receive(original, filename=original.name, request_id='review-source',
                              origin={'channel': 'test'})
    service.change(receipt['document_id'], purpose='long_term', expected_state=0,
                   reason='回归测试收录')
    parsed = service.parse(receipt['revision_id'])
    source = {key: receipt[key] for key in ('document_id', 'revision_id')}
    source.update(parse_id=parsed['parse_id'], ordinal=0)
    return service, receipt, source


def publish_topic(workspace, source):
    wiki = WikiService(workspace)
    batch = wiki.prepare([dict(title='审查主题', category='review', blocks=[
        dict(content='curated_wiki_conclusion', sources=[source])])])
    return wiki.publish(batch['batch_id'])[0]


@pytest.fixture
def received_replacement(tmp_path, registered_source):
    service, receipt, source = registered_source
    legacy = service.publish(service.prepare(receipt['revision_id'])['candidate_id'])
    topic = publish_topic(tmp_path, source)
    assert not service.search('obsolete_budget_42')
    assert not readable(tmp_path / legacy['path'])

    replacement = tmp_path / 'replacement.txt'
    replacement.write_text('replacement_budget_99', encoding='utf-8')
    newer = service.receive(replacement, filename=replacement.name, request_id='review-replacement',
                            origin={'channel': 'test'}, document_id=receipt['document_id'])
    return service, receipt, legacy, topic, newer


def test_received_replacement_preserves_wiki_without_reviving_legacy(
        tmp_path, received_replacement):
    """接收新原件尚未成功发布时，旧 Wiki 保持有效，legacy 始终退役。"""
    _, receipt, legacy, topic, _ = received_replacement
    restarted = SourceService(tmp_path)
    current = WikiService(tmp_path).current(topic['page_id'])
    assert current is not None, 'receive 只登记新原件，不能暂停尚未被成功替代的旧 Wiki'
    assert current['publish_id'] == topic['publish_id']
    assert restarted.search('curated_wiki_conclusion')
    assert readable(tmp_path / topic['path'])
    assert restarted.current_publication(receipt['document_id'], receipt['revision_id'])['publish_id'] == topic['publish_id']
    assert not restarted.search('obsolete_budget_42')
    assert not readable(tmp_path / legacy['path'])
    assert restarted.read_reference(legacy['publish_id'], 0, historical=True)['valid'] is False


def test_successful_replacement_retires_old_wiki_and_legacy(
        tmp_path, received_replacement):
    """新 Wiki 成功发布才替代旧 Wiki；legacy 不能随旧 Wiki 失效而复活。"""
    service, receipt, legacy, topic, newer = received_replacement
    parsed = service.parse(newer['revision_id'])
    source = {key: newer[key] for key in ('document_id', 'revision_id')}
    source.update(parse_id=parsed['parse_id'], ordinal=0)
    wiki = WikiService(tmp_path)
    batch = wiki.prepare([dict(page_id=topic['page_id'], expected_version=topic['page_version'],
        title='审查主题', category='review', blocks=[
            dict(content='replacement_wiki_conclusion', sources=[source])])])
    replacement = wiki.publish(batch['batch_id'])[0]

    restarted = SourceService(tmp_path)
    assert WikiService(tmp_path).current(topic['page_id'])['publish_id'] == replacement['publish_id']
    assert restarted.search('replacement_wiki_conclusion')
    assert not restarted.search('curated_wiki_conclusion')
    assert not restarted.search('obsolete_budget_42')
    assert restarted.current_publication(receipt['document_id'], receipt['revision_id']) is None
    assert not readable(tmp_path / topic['path'])
    assert not readable(tmp_path / legacy['path'])
    assert restarted.read_reference(topic['publish_id'], 0, historical=True)['valid'] is False
    historical = restarted.read_reference(legacy['publish_id'], 0, historical=True)
    assert historical['valid'] is False
    assert historical['text'] == 'obsolete_budget_42'


def test_late_legacy_candidate_cannot_displace_published_wiki(tmp_path, registered_source):
    """模拟升级前保存的候选在新 Wiki 提交之后恢复发布的确定性交错。"""
    service, receipt, source = registered_source
    candidate = service.prepare(receipt['revision_id'])
    topic = publish_topic(tmp_path, source)
    assert WikiService(tmp_path).current(topic['page_id'])['publish_id'] == topic['publish_id']

    with pytest.raises(SourceError) as error:
        SourceService(tmp_path).publish(candidate['candidate_id'])
    assert error.value.code == 'version_conflict'

    restarted = WikiService(tmp_path)
    assert restarted.current(topic['page_id'])['publish_id'] == topic['publish_id']
    assert restarted.search('curated_wiki_conclusion')
    assert not SourceService(tmp_path).search('obsolete_budget_42')


def test_projection_write_failure_cannot_rollback_withdrawal(
        tmp_path, registered_source, monkeypatch):
    """目录是可重建投影；日志写入失败不能撤销来源的不可召回标记。"""
    service, receipt, source = registered_source
    topic = publish_topic(tmp_path, source)
    write = wiki_module.write_text_atomic
    failures = []

    def fail_log_write(path, text, **kwargs):
        if Path(path) == tmp_path / 'knowledge' / 'log.md':
            failures.append(path)
            # 用独立连接检查可见性，确保投影开始写入前撤回已经提交。
            db = sqlite3.connect(service.db)
            try:
                validity = db.execute('SELECT validity FROM documents WHERE id=?',
                                      (receipt['document_id'],)).fetchone()[0]
            finally:
                db.close()
            assert validity == 'withdrawn', '写目录投影前必须先提交撤回标记'
            raise OSError('回归测试：日志投影写入失败')
        return write(path, text, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(wiki_module, 'write_text_atomic', fail_log_write)
        # 允许入口报告投影错误，也允许记录错误后正常返回；持久撤回必须成立。
        try:
            service.change(receipt['document_id'], action='withdraw', expected_state=1,
                           reason='回归测试撤回')
        except (OSError, SourceError):
            pass

    assert failures, '必须实际触发目录投影故障'
    restarted = SourceService(tmp_path)
    assert restarted.get(receipt['document_id'])['validity'] == 'withdrawn'
    assert restarted.get(receipt['document_id'])['state'] == 2
    wiki = WikiService(tmp_path)
    assert wiki.current(topic['page_id']) is None
    assert not restarted.search('curated_wiki_conclusion')
    assert not readable(tmp_path / topic['path'])
    assert wiki.read_reference(topic['publish_id'], historical=True)['valid'] is False
