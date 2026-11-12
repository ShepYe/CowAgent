"""个人资料 GET 的真实 SQLite 并发与材料核验回归，不调用模型。"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import socket
import sqlite3
from threading import Event

import pytest

from tests.test_personal_reading_ui_api import flow as flow_fixture, native_api as native_api_fixture


flow = flow_fixture
native_api = native_api_fixture


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("API 并发测试不得出网")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def quick_source_connections(monkeypatch, root):
    connect = sqlite3.connect
    target = str(root / '.personal_sources' / 'sources.sqlite3')

    def quick(database, *args, **kwargs):
        if target.replace('\\', '/') in str(database).replace('\\', '/'):
            kwargs['timeout'] = .05
        return connect(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', quick)
    return connect


def test_tasks_get_reads_committed_snapshot_while_source_writer_is_open(native_api, monkeypatch):
    mod, _, task, _, root = native_api
    connect = quick_source_connections(monkeypatch, root)
    writer = connect(root / '.personal_sources' / 'sources.sqlite3', timeout=.05)
    try:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute('UPDATE documents SET state=state+1')
        response = json.loads(mod.PersonalTasksHandler().GET())
        assert response['status'] == 'success', response
        displayed = next(t for t in response['tasks'] if t['task_id'] == task['task_id'])
        assert displayed['result_refs'][0]['current_searchable']
        assert displayed['reading_progress']['task_complete']
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize('operation', ['progress', 'reference'])
def test_material_verification_does_not_hold_source_database_lock(native_api, monkeypatch, operation):
    from agent.knowledge import reading, reading_materials
    mod, _, task, ref, root = native_api
    connect = quick_source_connections(monkeypatch, root)
    entered, release = Event(), Event()
    original = reading_materials.load_pdf_materials

    def slow(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(5), '未释放材料核验替身'
        return original(*args, **kwargs)

    monkeypatch.setattr(reading, 'load_pdf_materials', slow)
    monkeypatch.setattr(reading_materials, 'load_pdf_materials', slow)
    call = (lambda: mod._reading_progress(task)) if operation == 'progress' else (
        lambda: mod._reference_data(task['task_id'], ref['publish_id'], 0))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(call)
        try:
            assert entered.wait(3), '请求未进入材料核验'
            with connect(root / '.personal_sources' / 'sources.sqlite3', timeout=.05) as writer:
                writer.execute('BEGIN IMMEDIATE')
                writer.execute('UPDATE documents SET state=state')
                writer.commit()
        finally:
            release.set()
        result = pending.result(timeout=5)
    assert result.get('task_complete') if operation == 'progress' else result.get('valid')


def test_gets_reuse_one_snapshot_without_constructing_writing_services(native_api, monkeypatch):
    from agent.knowledge import personal_assistant, wiki
    mod, _, task, ref, root = native_api
    connect = sqlite3.connect
    snapshots = []

    def observe(database, *args, **kwargs):
        if kwargs.get('uri'):
            snapshots.append(str(database))
        return connect(database, *args, **kwargs)

    def writing_service(*args, **kwargs):
        pytest.fail('GET 不得构造写服务／创建解析／刷新投影')

    monkeypatch.setattr(sqlite3, 'connect', observe)
    monkeypatch.setattr(personal_assistant.SourceService, '__init__', writing_service)
    monkeypatch.setattr(personal_assistant.SourceService, 'parse', writing_service)
    monkeypatch.setattr(wiki.WikiService, '__init__', writing_service)
    monkeypatch.setattr(wiki, 'refresh', writing_service)
    db = root / '.personal_sources' / 'sources.sqlite3'
    before = sha256(db.read_bytes()).hexdigest()
    for call in (lambda: mod.PersonalTasksHandler().GET(),
                 lambda: mod._reference_data(task['task_id'], ref['publish_id'], 0),
                 lambda: mod.PersonalReadingCopyHandler().GET(task['task_id'], ref['publish_id'])):
        snapshots.clear()
        assert call()
        assert len(snapshots) == 1
        assert snapshots[0].endswith('?mode=ro')
    assert sha256(db.read_bytes()).hexdigest() == before


def test_reference_verification_is_bounded_and_concurrent_polling_succeeds(native_api, monkeypatch):
    from agent.knowledge import reading
    mod, _, task, ref, _ = native_api
    original = reading.load_pdf_materials
    calls = []

    def checked(*args, **kwargs):
        calls.append(kwargs['expected_manifest_hash'])
        return original(*args, **kwargs)

    monkeypatch.setattr(reading, 'load_pdf_materials', checked)
    reference = mod._reference_data(task['task_id'], ref['publish_id'], 0)
    assert reference['valid']
    assert 1 <= len(calls) <= 3, '同一引用不能触发多轮全库 refresh 和重复材料核验'
    with ThreadPoolExecutor(max_workers=4) as pool:
        operations = [lambda: json.loads(mod.PersonalTasksHandler().GET()),
                      lambda: mod._reference_data(task['task_id'], ref['publish_id'], 0)] * 3
        results = list(pool.map(lambda function: function(), operations))
    assert all(result.get('status') == 'success' or result.get('valid') for result in results)


@pytest.mark.parametrize('changed', ['material', 'original', 'parse', 'identity'])
def test_next_get_revalidates_materials_and_identity_without_stale_request_cache(native_api, changed):
    mod, _, task, ref, root = native_api
    first = mod._reference_data(task['task_id'], ref['publish_id'], 0)
    source = first['sources'][0]
    if changed == 'identity':
        with sqlite3.connect(root / '.personal_sources' / 'sources.sqlite3') as db:
            row = db.execute('SELECT id,record FROM reading_records').fetchone()
            record = json.loads(row[1])
            record['segments'][0]['page_map'] = [999]
            db.execute('UPDATE reading_records SET record=? WHERE id=?', (json.dumps(record), row[0]))
    else:
        paths = dict(material=root / first['reading_copy']['path'],
                     parse=root / '.personal_sources' / 'parses' / (source['parse_id'] + '.json'))
        if changed == 'original':
            with sqlite3.connect(root / '.personal_sources' / 'sources.sqlite3') as db:
                revision = json.loads(db.execute('SELECT record FROM revisions WHERE id=?', (source['revision_id'],)).fetchone()[0])
            path = root / '.personal_sources' / 'originals' / revision['sha256']
        else:
            path = paths[changed]
        path.write_bytes(path.read_bytes() + b'changed')
    response = json.loads(mod.PersonalReferenceHandler().GET(task['task_id'], ref['publish_id']))
    assert response['status'] == 'error'
    displayed = json.loads(mod.PersonalTasksHandler().GET())
    assert displayed['status'] == 'success'
    assert not displayed['tasks'][0]['result_refs'][0]['current_searchable']


@pytest.mark.parametrize('schema', ['v1-empty', 'v2-missing-wiki', 'v2-missing-column'])
def test_incomplete_schema_returns_json_error_instead_of_500(native_api, schema):
    mod, _, task, ref, root = native_api
    with sqlite3.connect(root / '.personal_sources' / 'sources.sqlite3') as db:
        if schema == 'v1-empty':
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                db.execute('DROP TABLE "' + table.replace('"', '""') + '"')
            db.execute('PRAGMA user_version=1')
        elif schema == 'v2-missing-wiki':
            db.execute('DROP TABLE wiki_publications')
        else:
            db.execute('ALTER TABLE documents RENAME COLUMN state TO broken_state')
    response = json.loads(mod.PersonalReferenceHandler().GET(task['task_id'], ref['publish_id']))
    assert response['status'] == 'error'
    assert response['code'] == 'storage_failed'
    assert str(root) not in json.dumps(response)
