"""XLSX 实际 Read：原单元格文字、关系锚点和显式图片媒体共同返回。"""
import json

import pytest

from agent.tools.read.read import Read
from common.personal_tasks import TaskStore, OWNER, task_scope
from common.runtime_identity import current_identity, identity_scope
from models.config_revision import capture_config_revision
from tests.test_reading_capabilities import ProbeModel
from tests.test_spreadsheet_materials import workbook as workbook_environment

workbook = workbook_environment


def model():
    result = ProbeModel()
    result.catalog_model_meta = lambda: dict(capabilities=['vision'])
    return result


def context(read, task=None):
    return dict(protocol='responses', provider=read.model.bot_type, model=read.model.model,
                api_base='https://offline.invalid/v1', transport='inline', mime_type='application/pdf',
                identity=current_identity(), config_revision=capture_config_revision(),
                task_id=None if task is None else task['task_id'])


def test_general_xlsx_retains_cached_values_and_explicit_image_scope(workbook):
    read = Read(dict(cwd=str(workbook.parent)))
    read.model = model()
    before = workbook.read_bytes()
    with identity_scope(user_id=OWNER, session_id='sheet-tools'):
        initial = read.execute(dict(path=str(workbook), visual=False))
        assert initial.status == 'success', initial.result
        assert '999' in initial.result['content']
        assert len(initial.result['spreadsheet']['images']) == 3 and not initial.media
        key = initial.result['spreadsheet']['images'][0]['object_key']
        selected = read.execute(dict(path=str(workbook), worksheet='预算', objects=[key]))
        assert selected.status == 'success', selected.result
        assert selected.result['spreadsheet']['long_term_image_evidence_available'] is False
        assert len(selected.media) == 1
        assert selected.media[0].loader(context(read)).data.startswith(b'\x89PNG')
        assert selected.result['spreadsheet']['media_map'][0]['location']['anchor']['from']['cell'] == 'B3'
        workbook.write_bytes(b'changed')
        with pytest.raises(ValueError):
            selected.media[0].loader(context(read))
    assert before.startswith(b'PK')
    assert not (workbook.parent / '.personal_sources').exists()


def test_task_xlsx_object_selection_rejects_cancelled_attempt(workbook, tmp_path):
    root = tmp_path / 'runtime'
    root.mkdir()
    store = TaskStore(root / 'personal')
    task = store.submit(OWNER, 'xlsx-tools', {}, {'channel': 'web'}, capture_config_revision())
    entry = store.attach(OWNER, task['task_id'], workbook)
    task = store.claim(OWNER, task['task_id'])
    read = Read(dict(cwd=str(root), workspace_root=str(root), task_store=store))
    read.model = model()
    with identity_scope(user_id=OWNER, session_id='sheet-task'), task_scope(task['task_id']):
        first = read.execute(dict(path=entry['path'], visual=False))
        assert first.status == 'success', first.result
        key = first.result['spreadsheet']['images'][0]['object_key']
        parsed = json.loads((root / '.personal_sources/parses' / (first.result['parse_id'] + '.json')).read_text(encoding='utf-8'))
        assert parsed['config']['xlsx_structure'] == 'xlsx-embedded-structure-v1'
        assert parsed['visual_coverage'][0]['location']['kind'] == 'xlsx'
        selected = read.execute(dict(path=entry['path'], objects=[key]))
        assert selected.status == 'success', selected.result
        assert selected.media[0].loader(context(read, task)).data.startswith(b'\x89PNG')
        store.cancel(OWNER, task['task_id'], task['attempt'])
        assert not selected.media[0].supports(context(read, task))


def test_undeclared_image_capability_keeps_structure_without_media(workbook, monkeypatch):
    monkeypatch.setattr('agent.knowledge.image_reading_capabilities.ensure_image_capability', lambda *a: False)
    read = Read(dict(cwd=str(workbook.parent)))
    read.model = ProbeModel()
    first = read.execute(dict(path=str(workbook), visual=False))
    key = first.result['spreadsheet']['images'][0]['object_key']
    selected = read.execute(dict(path=str(workbook), objects=[key]))
    assert selected.status == 'success' and not selected.media
    assert any(g['code'] == 'image_media_not_authorized_or_supported'
               for g in selected.result['spreadsheet']['coverage_gaps'])


def test_unknown_image_capability_uses_independent_probe_once_per_read(workbook, monkeypatch):
    calls = []
    monkeypatch.setattr('agent.knowledge.image_reading_capabilities.ensure_image_capability',
                        lambda *a: calls.append(a[-1]) or True)
    read = Read(dict(cwd=str(workbook.parent)))
    read.model = ProbeModel()
    initial = read.execute(dict(path=str(workbook), visual=False))
    assert not calls
    key = initial.result['spreadsheet']['images'][0]['object_key']
    selected = read.execute(dict(path=str(workbook), objects=[key]))
    assert selected.status == 'success' and len(selected.media) == 1
    assert selected.media[0].loader(context(read)).data.startswith(b'\x89PNG')
    assert calls == ['image/png']
