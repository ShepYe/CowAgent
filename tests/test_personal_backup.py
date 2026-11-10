"""个人实例全量备份、WAL 与恢复写入屏障。"""
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

from cli.commands.backup import create_backup_archive, restore_backup_archive


def fixture(tmp_path):
    data = tmp_path / 'data'
    workspace = data / 'workspace'
    workspace.mkdir(parents=True)
    (data / 'config.json').write_text(json.dumps({'personal_assistant_enabled': True, 'agent_workspace': str(workspace)}), encoding='utf-8')
    (workspace / 'tmp').mkdir()
    (workspace / 'tmp' / 'attachment.pdf').write_bytes(b'original-pdf')
    (workspace / 'sessions.json').write_text('{"text":"完整会话"}', encoding='utf-8')
    (data / 'personal' / 'originals').mkdir(parents=True)
    (data / 'personal' / 'originals' / 'source.docx').write_bytes(b'docx-bytes')
    (data / 'personal' / 'receipt.json').write_text('{"schema_version":1,"attachment":"source.docx"}', encoding='utf-8')
    return data, workspace


def test_complete_wal_snapshot_and_full_roundtrip(tmp_path):
    data, workspace = fixture(tmp_path)
    db = sqlite3.connect(data / 'personal' / 'tasks.sqlite3')
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('PRAGMA user_version=1')
        db.execute('CREATE TABLE tasks (record TEXT)')
        db.execute('INSERT INTO tasks VALUES (?)', ('{"schema_version":1,"status":"queued"}',))
        db.commit()
        archive = tmp_path / 'full.zip'
        manifest = create_backup_archive(archive, data, workspace)
        assert manifest['version'] == 3
        with zipfile.ZipFile(archive) as z:
            assert z.read('workspace/tmp/attachment.pdf') == b'original-pdf'
            assert not any(n.endswith(('-wal', '-shm')) for n in z.namelist())
            assert 'data/instance/personal/tasks.sqlite3' in manifest['files']
        target = tmp_path / 'restored'
        restore_backup_archive(archive, target, target / 'workspace')
        with sqlite3.connect(target / 'personal' / 'tasks.sqlite3') as restored:
            assert json.loads(restored.execute('SELECT record FROM tasks').fetchone()[0])['status'] == 'queued'
        assert (target / 'personal' / 'originals' / 'source.docx').read_bytes() == b'docx-bytes'
        assert (target / 'workspace' / 'tmp' / 'attachment.pdf').read_bytes() == b'original-pdf'
    finally:
        db.close()


def rewrite(archive, transform):
    with zipfile.ZipFile(archive) as z:
        entries = {name: z.read(name) for name in z.namelist()}
    transform(entries)
    with zipfile.ZipFile(archive, 'w') as z:
        for name, body in entries.items():
            z.writestr(name, body)


@pytest.mark.parametrize('failure', ['hash', 'missing', 'schema', 'record_schema', 'sqlite_schema'])
def test_restore_validates_everything_before_touching_target(tmp_path, failure):
    data, workspace = fixture(tmp_path)
    archive = tmp_path / 'full.zip'
    if failure == 'sqlite_schema':
        with sqlite3.connect(data / 'personal' / 'tasks.sqlite3') as db:
            db.execute('PRAGMA user_version=2')
            db.execute('CREATE TABLE future (id INTEGER)')
    if failure == 'record_schema':
        (data / 'personal' / 'receipt.json').write_text('{"schema_version":2}', encoding='utf-8')
    create_backup_archive(archive, data, workspace)
    def damage(entries):
        if failure == 'hash':
            entries['workspace/sessions.json'] = b'changed'
        elif failure == 'missing':
            del entries['workspace/sessions.json']
        elif failure == 'schema':
            manifest = json.loads(entries['manifest.json'])
            manifest['schema_version'] = 2
            entries['manifest.json'] = json.dumps(manifest).encode()
    rewrite(archive, damage)
    target = tmp_path / 'target'
    target.mkdir()
    (target / 'config.json').write_bytes(b'{"keep":true}')
    with pytest.raises(ValueError):
        restore_backup_archive(archive, target, target / 'workspace')
    assert (target / 'config.json').read_bytes() == b'{"keep":true}'
    assert list(target.iterdir()) == [target / 'config.json']


def test_recovery_point_preserves_newer_originals(tmp_path):
    data, workspace = fixture(tmp_path)
    archive = tmp_path / 'older.zip'
    create_backup_archive(archive, data, workspace)
    newer = data / 'personal' / 'originals' / 'newer.xlsx'
    newer.write_bytes(b'newer-original')
    result = restore_backup_archive(archive, data, workspace)
    point = json.loads((data / 'recovery-point.json').read_text(encoding='utf-8'))
    assert point['archive'] == result['recovery_point']['archive']
    with zipfile.ZipFile(point['archive']) as z:
        assert z.read('data/instance/personal/originals/newer.xlsx') == b'newer-original'
    assert newer.read_bytes() == b'newer-original'


def test_copy_failure_rolls_back_previous_writes(tmp_path, monkeypatch):
    from cli.commands import backup
    data, workspace = fixture(tmp_path)
    archive = tmp_path / 'full.zip'
    create_backup_archive(archive, data, workspace)
    target = tmp_path / 'target'
    target.mkdir()
    (target / 'config.json').write_bytes(b'{"keep":true}')
    original = backup._atomic_copy
    calls = []
    def fail(source, destination, **kwargs):
        calls.append(destination)
        if len(calls) == 2:
            raise OSError('模拟磁盘失败')
        original(source, destination, **kwargs)
    monkeypatch.setattr(backup, '_atomic_copy', fail)
    with pytest.raises(OSError):
        restore_backup_archive(archive, target, target / 'workspace')
    assert (target / 'config.json').read_bytes() == b'{"keep":true}'
    assert not list((target / 'workspace').rglob('*.*'))


def test_personal_owner_and_desktop_share_user_root(tmp_path, monkeypatch):
    from common import state_dir
    from common.runtime_identity import RuntimeIdentity
    monkeypatch.setenv('COW_PERSONAL', '1')
    monkeypatch.setattr(state_dir, 'state_root', lambda ident=None: tmp_path)
    monkeypatch.setattr(state_dir, 'shared_root', lambda: tmp_path)
    assert state_dir.user_root(RuntimeIdentity(user_id='local-owner')) == state_dir.user_root(RuntimeIdentity())
    assert state_dir.user_root(RuntimeIdentity(user_id='other')) == tmp_path / 'users' / 'other'


def test_real_task_store_backed_up_with_history_and_originals(tmp_path):
    from common.personal_tasks import TaskStore, OWNER
    data, workspace = fixture(tmp_path)
    store = TaskStore(data / 'personal')
    task = store.submit(OWNER, 'test-req', {'question': '测试'}, {'channel': 'local'}, 'cfg-1')
    archive = tmp_path / 'tasks.zip'
    create_backup_archive(archive, data, workspace)
    target = tmp_path / 'new-instance'
    result = restore_backup_archive(archive, target)
    assert result['workspace'] == str(target / 'workspace')
    restored = TaskStore(target / 'personal')
    assert restored.get(OWNER, task['task_id']) == task
    assert restored.history(OWNER, task['task_id']) == store.history(OWNER, task['task_id'])
    assert (target / 'personal' / 'originals' / 'source.docx').read_bytes() == b'docx-bytes'


@pytest.mark.parametrize('external_workspace', [False, True])
@pytest.mark.parametrize('decision_checkpoint', [False, True])
def test_relocated_restore_opens_sources_and_resumes_without_old_root(tmp_path, external_workspace, decision_checkpoint):
    from common.personal_tasks import TaskStore, OWNER
    from agent.knowledge.personal_assistant import SourceService
    from agent.tools.knowledge_ingest import resume_publication
    from docx import Document
    data = tmp_path / 'old-data'
    workspace = tmp_path / 'old-workspace' if external_workspace else data / 'workspace'
    data.mkdir()
    workspace.mkdir(parents=True)
    (data / 'config.json').write_text(json.dumps({'personal_assistant_enabled': True, 'agent_workspace': str(workspace)}), encoding='utf-8')
    literal = '原文中的绝对路径必须保留 ' + str(data) + ' and ' + str(workspace)
    sample = tmp_path / 'source.docx'
    doc = Document()
    doc.add_paragraph('budget 42 dB ' + literal)
    doc.save(sample)
    store, sources = TaskStore(data / 'personal'), SourceService(workspace)
    tasks, publications = [], []
    for n in range(2):
        task = store.submit(OWNER, f'relocate-{n}', {'text': literal}, {'channel': 'test'}, 'cfg-1')
        attachment = store.attach(OWNER, task['task_id'], sample, 'source.docx', 'file')
        store.claim(OWNER, task['task_id'])
        source = sources.receive(attachment['path'], filename='source.docx', request_id=task['task_id'], origin=task['origin'])
        state = sources.get(source['document_id'])
        sources.change(source['document_id'], purpose='long_term', expected_state=state['state'], reason=literal)
        candidate = sources.prepare(source['revision_id'], task_id=task['task_id'], summary=literal)
        if n == 0:
            pub = sources.publish(candidate['candidate_id'])
            store.complete(OWNER, task['task_id'], 1, 'published', [pub])
            publications.append(pub)
        else:
            updates = dict(source_candidates=[{'candidate_id': candidate['candidate_id'], 'workspace': str(workspace)}])
            if decision_checkpoint:
                updates['attachment_decisions'] = {'file': dict(attachment_id='file', purpose='long_term', ready=True, source=source, workspace=str(workspace), candidate_id=candidate['candidate_id'], summary=literal)}
            store._change(OWNER, task['task_id'], 1, {'running'}, **updates)
        tasks.append(task)
    archive = tmp_path / 'portable.zip'
    manifest = create_backup_archive(archive, data, workspace)
    assert manifest['data_root_source'] == str(data)
    data.rename(tmp_path / 'hidden-old-data')
    if external_workspace:
        workspace.rename(tmp_path / 'hidden-old-workspace')
    assert not data.exists() and not workspace.exists()
    target = tmp_path / 'new-data'
    target_workspace = tmp_path / 'new-workspace'
    restore_backup_archive(archive, target, target_workspace)
    restored, restored_sources = TaskStore(target / 'personal'), SourceService(target_workspace)
    for task in tasks:
        item = restored.attachment(OWNER, task['task_id'], 'file')
        assert Path(item['path']).is_relative_to(target)
        assert Path(item['path']).read_bytes() == sample.read_bytes()
        assert restored.get(OWNER, task['task_id'])['payload']['text'] == literal
        assert restored.history(OWNER, task['task_id'])[-1]['attachments'][0]['path'] == item['path']
    pub = publications[0]
    reference = restored_sources.read_reference(pub['publish_id'], 0)
    assert reference['valid'] and literal in reference['text']
    assert Path(reference['original']).is_relative_to(target_workspace)
    assert restored_sources.search('budget')
    pending = restored.get(OWNER, tasks[1]['task_id'])
    assert pending['source_candidates'][0]['workspace'] == str(target_workspace)
    result = resume_publication(pending, restored)
    assert result['status'] == 'succeeded'
    assert len(restored_sources.search('budget')) == 2
    for ref in result['result_refs']:
        assert restored_sources.read_reference(ref['publish_id'], 0)['valid']
    # Repeated receipt/parse paths must also use the new roots.
    with restored_sources.transaction() as db:
        for (body,) in db.execute('SELECT record FROM receipts'):
            assert Path(json.loads(body)['original']).is_relative_to(target_workspace)
        revision = db.execute('SELECT id FROM revisions LIMIT 1').fetchone()[0]
    assert restored_sources.parse(revision)['units']


def test_complete_backup_and_restore_reject_live_backend(tmp_path):
    import os
    from click.testing import CliRunner
    from cli.commands.backup import backup_command
    data, workspace = fixture(tmp_path)
    archive = tmp_path / 'offline.zip'
    create_backup_archive(archive, data, workspace)
    (data / '.personal-backend.pid').write_text(str(os.getpid()), encoding='utf-8')
    with pytest.raises(ValueError, match='先退出'):
        create_backup_archive(tmp_path / 'forbidden.zip', data, workspace)
    with pytest.raises(ValueError, match='先退出'):
        restore_backup_archive(archive, data, workspace)
    assert not (tmp_path / 'forbidden.zip').exists()
    result = CliRunner().invoke(backup_command, ['--output', str(tmp_path / 'cli.zip')], env={'COW_DATA_DIR': str(data)})
    assert result.exit_code != 0 and '先退出' in result.output


@pytest.mark.parametrize('fail_after_log', [False, True])
def test_restore_preserves_open_log_and_resumes_appending(tmp_path, monkeypatch, fail_after_log):
    import logging
    from cli.commands import backup

    data, workspace = fixture(tmp_path)
    (data / 'run.log').write_text('archived log\n', encoding='utf-8')
    archive = tmp_path / 'logs.zip'
    create_backup_archive(archive, data, workspace)
    target = tmp_path / 'restored'
    target.mkdir()
    log = logging.getLogger('personal-restore-open-log')
    handler = logging.FileHandler(target / 'run.log', mode='w', encoding='utf-8')
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    log.info('before restore')
    copy = backup._atomic_copy

    def fail_on_config(source, destination, **kwargs):
        if Path(destination) == target / 'config.json':
            raise OSError('模拟日志恢复后的写入失败')
        return copy(source, destination, **kwargs)

    try:
        if fail_after_log:
            monkeypatch.setattr(backup, '_atomic_copy', fail_on_config)
            with pytest.raises(OSError):
                restore_backup_archive(archive, target, target / 'workspace')
        else:
            restore_backup_archive(archive, target, target / 'workspace')
        log.info('after restore')
        handler.flush()
        expected = 'before restore\n' if fail_after_log else 'archived log\n'
        assert (target / 'run.log').read_text(encoding='utf-8') == expected + 'after restore\n'
        assert handler in log.handlers
    finally:
        log.removeHandler(handler)
        handler.close()
