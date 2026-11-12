"""MR-07：真实 archive API 的隔离异根恢复与台账校验。"""
import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing

import pytest

from agent.memory.conversation_store import ConversationStore
from agent.memory.ledger import MemoryLedger
from cli.commands.backup import create_backup_archive, restore_backup_archive
from common.runtime_identity import identity_scope


@pytest.fixture
def instance(tmp_path, monkeypatch):
    data = tmp_path / 'source'
    workspace = data / 'workspace'
    workspace.mkdir(parents=True)
    monkeypatch.setenv('COW_DATA_DIR', str(data))
    config = {'personal_assistant_enabled': True, 'agent_workspace': str(workspace)}
    (data / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    db_path = workspace / 'memory/long-term/index.db'
    store = ConversationStore(db_path, agent_id='')
    with identity_scope(agent_id='', user_id='', session_id='chat'):
        ledger = MemoryLedger(workspace, store=store)
        messages = [{'role': 'user', 'content': '以后请简洁回答'}]
        ledger.capture(messages, origin='human')
        store.append_messages('chat', messages)
        ref = ledger.evidence(messages)[0]
        ref = {k: v for k, v in ref.items() if k not in ('text', 'origin')}
        proposal = dict(content='回答简洁', disposition='conversation_memory', basis=[ref], reason='用户偏好')
        receipt = ledger.record('remember', [proposal], trusted=True)
        assert receipt['results'][0]['status'] == 'remembered'
    return data, workspace, db_path, ledger, proposal


def restore(tmp_path, instance):
    data, workspace, *_ = instance
    archive = tmp_path / 'memory.zip'
    manifest = create_backup_archive(archive, data, workspace, complete=True)
    assert manifest['version'] == 4 and manifest['memory_schema_version'] == 1
    data.rename(tmp_path / 'hidden-source')
    target = tmp_path / 'restored'
    result = restore_backup_archive(archive, target)
    new_workspace = target / 'workspace'
    with identity_scope(agent_id='', user_id='', session_id='chat'):
        ledger = MemoryLedger(new_workspace, store=ConversationStore(new_workspace / 'memory/long-term/index.db', agent_id=''))
    return result, ledger


def test_restore_unacknowledged_projection_after_new_authority(tmp_path, instance, monkeypatch):
    data, workspace, _, original, proposal = instance
    replace = original._strict_replace
    def interrupted(*args):
        replace(*args)
        raise OSError('after projection replace')
    with identity_scope(agent_id='', user_id='', session_id='chat'):
        messages = [dict(role='user', content='以后也多用表格')]
        original.capture(messages, origin='human')
        ref = original.evidence(messages)[0]
        basis = [{k: ref[k] for k in ('message_id', 'block_index', 'start', 'end', 'sha256')}]
        monkeypatch.setattr(original, '_strict_replace', interrupted)
        receipt = original.record('crashed', [dict(content='多用表格', disposition='conversation_memory', basis=basis, reason='偏好')], trusted=True)
        assert receipt['results'][0]['status'] == 'pending_projection'
        # 失效提交继续前进，但上次生成文件尚无完成回执。
        with closing(sqlite3.connect(original.db_path)) as db, db:
            db.execute('UPDATE memory_scopes SET revision=revision+1')
    _, recovered = restore(tmp_path, instance)
    assert recovered.project()
    assert len(recovered.active_entries()) == 2


@pytest.mark.parametrize('projection', ['current', 'missing', 'stale', 'ahead', 'external'])
def test_actual_cross_root_restore(tmp_path, instance, projection):
    data, workspace, db_path, original, proposal = instance
    path = workspace / 'memory/routed.md'
    external = b'changed by external editor\n'
    if projection == 'missing':
        path.unlink()
    elif projection == 'stale':
        # 提交已完成，文件仍是旧权威修订（失败发生在生成投影前）。
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute('UPDATE memory_scopes SET revision=revision+1')
    elif projection == 'ahead':
        # 文件替换完成而数据库事务未提交，保留领先文件供诊断。
        path.write_text(path.read_text(encoding='utf-8').replace('回答简洁', '文件领先'), encoding='utf-8')
    elif projection == 'external':
        path.write_bytes(external)
    with closing(sqlite3.connect(db_path)) as db:
        scopes = db.execute('SELECT * FROM memory_scopes').fetchall()
        records = db.execute('SELECT * FROM memory_operations').fetchall()
    result, restored = restore(tmp_path, instance)
    assert not data.exists()
    assert restored.workspace_id == original.workspace_id
    assert restored.active_entries() == original_entries(tmp_path / 'hidden-source/workspace/memory/long-term/index.db')
    with closing(sqlite3.connect(restored.db_path)) as db:
        assert db.execute('SELECT * FROM memory_scopes').fetchall() == scopes
        assert db.execute('SELECT * FROM memory_operations').fetchall() == records
        trace = json.loads(db.execute('SELECT extras FROM messages').fetchone()[0])['memory_trace']
        assert trace['workspace_id'] == original.workspace_id
        assert all(not row[0].startswith(str(workspace)) for row in db.execute('SELECT path FROM memory_files'))
    text = (restored.workspace / 'memory/routed.md').read_text(encoding='utf-8')
    if projection in ('ahead', 'external'):
        assert result['memory_diagnostics'][0]['status'] == 'projection_preserved'
        assert ('文件领先' in text) if projection == 'ahead' else text.encode() == external
        assert restored.filter_text('memory/routed.md', text) == ''
    else:
        assert result['memory_diagnostics'] == []
        assert restored.filter_text('memory/routed.md', text) == '回答简洁'
        assert json.loads(text.split('<!-- cow-memory ')[1].split(' -->')[0])['revision'] == scopes[0][3]
    with identity_scope(agent_id='', user_id='', session_id='chat'):
        retry = restored.record('remember', [proposal], trusted=True)
    assert retry['results'][0]['entry_id'] == restored.active_entries()[0]['entry_id']
    assert len(restored.active_entries()) == 1


def original_entries(path):
    with closing(sqlite3.connect(path)) as db:
        return [json.loads(r[0]) for r in db.execute("SELECT record FROM memory_entries WHERE state='active' ORDER BY rowid")]


@pytest.mark.parametrize('damage', ['version', 'table', 'column', 'record', 'scope', 'evidence', 'span', 'decision', 'entry', 'operation', 'path'])
def test_invalid_ledger_rejected_before_publication(tmp_path, instance, damage):
    data, workspace, db_path, *_ = instance
    with closing(sqlite3.connect(db_path)) as db, db:
        if damage == 'version':
            db.execute('UPDATE memory_schema SET version=2')
        elif damage == 'table':
            db.execute('DROP TABLE memory_history')
        elif damage == 'column':
            db.execute('ALTER TABLE memory_files RENAME TO discarded_files')
            db.execute('CREATE TABLE memory_files (agent_id TEXT)')
        elif damage == 'record':
            db.execute("UPDATE memory_entries SET record='{' ")
        elif damage == 'scope':
            db.execute('UPDATE memory_scopes SET workspace_id=?', ('other-workspace',))
        elif damage == 'evidence':
            db.execute('DELETE FROM memory_evidence')
        elif damage == 'span':
            row = json.loads(db.execute('SELECT record FROM memory_candidates').fetchone()[0])
            row['basis'][0]['end'] = 999
            db.execute('UPDATE memory_candidates SET record=?', (json.dumps(row),))
        elif damage == 'decision':
            db.execute('DELETE FROM memory_decisions')
        elif damage == 'entry':
            db.execute('DELETE FROM memory_entries')
        elif damage == 'operation':
            db.execute('DELETE FROM memory_operations')
        elif damage == 'path':
            db.execute("UPDATE memory_files SET path='../escape.md'")
    archive = tmp_path / 'bad.zip'
    create_backup_archive(archive, data, workspace, complete=True)
    target = tmp_path / 'target'
    target.mkdir()
    sentinel = target / 'config.json'
    sentinel.write_text('{"keep":true}', encoding='utf-8')
    with pytest.raises(ValueError, match='memory ledger'):
        restore_backup_archive(archive, target)
    assert sentinel.read_text(encoding='utf-8') == '{"keep":true}'
    assert list(target.iterdir()) == [sentinel]


def test_wal_snapshot_contains_memory_and_original_extras(tmp_path, instance):
    data, workspace, db_path, *_ = instance
    with closing(sqlite3.connect(db_path)) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('UPDATE memory_scopes SET revision=revision+1')
        db.commit()
        archive = tmp_path / 'wal.zip'
        manifest = create_backup_archive(archive, data, workspace, complete=True)
        with zipfile.ZipFile(archive) as z:
            assert not any(n.endswith(('-wal', '-shm')) for n in z.namelist())
            name = 'workspace/memory/long-term/index.db'
            body = z.read(name)
            assert hashlib.sha256(body).hexdigest() == manifest['files'][name]['sha256']
        target = tmp_path / 'target'
        restore_backup_archive(archive, target)
        with closing(sqlite3.connect(target / 'workspace/memory/long-term/index.db')) as restored:
            assert restored.execute('SELECT revision FROM memory_scopes').fetchone() == db.execute('SELECT revision FROM memory_scopes').fetchone()


def test_memory_requires_full_backup(tmp_path, instance):
    data, workspace, *_ = instance
    with pytest.raises(ValueError, match='完整离线备份'):
        create_backup_archive(tmp_path / 'partial.zip', data, workspace, complete=False)
    assert not (tmp_path / 'partial.zip').exists()


@pytest.mark.parametrize('history', ['excluded', 'merged', 'human_edit', 'external_human_edit'])
def test_restore_preserves_valid_history_and_dependencies(tmp_path, instance, history):
    data, workspace, db_path, ledger, _ = instance
    with identity_scope(agent_id='', user_id='', session_id='chat'):
        entry_id = ledger.active_entries()[0]['entry_id']
        if history == 'excluded':
            ledger.exclude(entry_id)
        elif history == 'merged':
            receipt = ledger.merge('dream', '简洁回答的约定', [entry_id], ledger.revision())
            assert receipt['results'][0]['status'] == 'remembered'
            ledger.exclude(entry_id)
            assert ledger.active_entries() == []
        else:
            ledger.human_edit('MEMORY.md', '人工第一次修订')
            ledger.human_edit('MEMORY.md', '人工第二次修订')
            if history == 'external_human_edit':
                (workspace / 'MEMORY.md').write_text('外部修改', encoding='utf-8')
    expected = original_entries(db_path)
    result, restored = restore(tmp_path, instance)
    assert restored.active_entries() == expected
    if history == 'external_human_edit':
        assert (restored.workspace / 'MEMORY.md').read_text(encoding='utf-8') == '外部修改'
        assert any(d.get('path') == 'MEMORY.md' for d in result['memory_diagnostics'])
    else:
        assert result['memory_diagnostics'] == []


def test_global_conversation_database_with_multiple_agent_scopes(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    workspace = data / 'workspace'
    workspace.mkdir(parents=True)
    monkeypatch.setenv('COW_DATA_DIR', str(data))
    agents = [dict(id='primary', name='主助手', workspace=str(workspace)),
              dict(id='other', name='另一助手', workspace=str(workspace / 'agents/other'))]
    (data / 'config.json').write_text(json.dumps(dict(personal_assistant_enabled=True, agent_workspace=str(workspace), agents=agents, default_agent_id='primary')), encoding='utf-8')
    database = data / 'global/index.db'
    identities = {}
    for agent, user in [('primary', ''), ('other', 'person')]:
        agent_workspace = workspace if agent == 'primary' else workspace / 'agents/other'
        agent_workspace.mkdir(parents=True, exist_ok=True)
        with identity_scope(agent_id=agent, user_id=user, session_id='shared-session'):
            ledger = MemoryLedger(agent_workspace, store=ConversationStore(database, agent_id=agent))
            ledger.human_edit('MEMORY.md', agent + '的记忆')
            ledger.project()
            identities[agent] = ledger.workspace_id
    archive = tmp_path / 'global.zip'
    create_backup_archive(archive, data, workspace, complete=True)
    data.rename(tmp_path / 'old-data')
    target = tmp_path / 'new-data'
    result = restore_backup_archive(archive, target, target / 'workspace')
    assert result['memory_diagnostics'] == []
    destinations = {item['id']: item['workspace'] for item in result['agents']}
    for agent, user in [('primary', ''), ('other', 'person')]:
        with identity_scope(agent_id=agent, user_id=user):
            ledger = MemoryLedger(destinations[agent], store=ConversationStore(target / 'global/index.db', agent_id=agent))
            assert ledger.workspace_id == identities[agent]
            assert ledger.active_entries()[0]['content'] == agent + '的记忆'


def test_old_reader_and_unknown_memory_backup_marker_reject(tmp_path, instance, monkeypatch):
    from cli.commands import backup
    data, workspace, *_ = instance
    archive = tmp_path / 'v4.zip'
    create_backup_archive(archive, data, workspace, complete=True)
    with zipfile.ZipFile(archive) as z:
        with monkeypatch.context() as patch:
            patch.setattr(backup, '_SUPPORTED_BACKUP_VERSIONS', {1, 2, 3})
            with pytest.raises(ValueError, match='unsupported'):
                backup._validate_archive(z)
        contents = {n: z.read(n) for n in z.namelist()}
    manifest = json.loads(contents['manifest.json'])
    manifest['memory_schema_version'] = 2
    contents['manifest.json'] = json.dumps(manifest).encode()
    with zipfile.ZipFile(archive, 'w') as z:
        for name, body in contents.items():
            z.writestr(name, body)
    with pytest.raises(ValueError, match='memory backup schema'):
        restore_backup_archive(archive, tmp_path / 'target')
