import pytest
from common.personal_tasks import OWNER, TaskError, TaskStore
from common import personal_intake


def test_desktop_attachment_archive_and_idempotency(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / 'state')
    monkeypatch.setattr(personal_intake, 'TaskStore', lambda: store)
    monkeypatch.setattr(personal_intake, 'capture_config_revision', lambda: 'cfg-old')
    uploads = tmp_path / 'uploads'
    uploads.mkdir()
    file = uploads / 'document.txt'
    file.write_text('原文预算42 dB', encoding='utf-8')
    attachments = [{'file_path': str(file), 'file_name': 'document.txt'}]
    task = personal_intake.receive_desktop('request', 'session', '仅临时分析', attachments, uploads, tmp_path)
    assert task['payload']['text'] == '仅临时分析'
    assert store.attachment(OWNER, task['task_id'], '0')['filename'] == 'document.txt'
    monkeypatch.setattr(personal_intake, 'capture_config_revision', lambda: 'cfg-new')
    again = personal_intake.receive_desktop('request', 'session', '仅临时分析', attachments, uploads, tmp_path)
    assert again['task_id'] == task['task_id'] and again['config_revision'] == 'cfg-old'
    assert 'knowledge_ingest' in personal_intake.task_prompt(task)


def test_desktop_cannot_archive_arbitrary_machine_file(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / 'state')
    monkeypatch.setattr(personal_intake, 'TaskStore', lambda: store)
    file = tmp_path / 'private.txt'
    file.write_text('private', encoding='utf-8')
    uploads = tmp_path / 'uploads'
    uploads.mkdir()
    with pytest.raises(TaskError, match='forbidden'):
        personal_intake.receive_desktop('request', 'session', 'test', [{'file_path': str(file)}], uploads, uploads)
    assert store.list() == []
