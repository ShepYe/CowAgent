"""局部放大经真实工具与凭据发布；局部不扩大整页覆盖。"""
from copy import deepcopy
from io import BytesIO

from PIL import Image
import pytest

from agent.knowledge.reading import ReadingService
from agent.knowledge.wiki import WikiService
from agent.tools.read.read import Read
from tests.test_reading_ingest_flow import flow as reading_environment, deliver, submission

flow = reading_environment


def test_region_observation_publishes_with_full_page_coverage(flow):
    root, store, task, entry, read, ingest = flow
    whole = read.execute(dict(path=entry['path'], pages='1'))
    deliver(whole, task)
    cropped = read.execute(dict(path=entry['path'], pages='1', region=[0, 0, .5, .5]))
    assert cropped.status == 'success', cropped.result
    assert cropped.result['supplemental'] and cropped.result['coverage_keys'] == []
    assert cropped.result['reading_id'] != whole.result['reading_id']
    location = cropped.result['source_locations'][0]['location']
    assert location == dict(kind='pdf', physical_page=1, region=[0., 0., .5, .5])
    deliver(cropped, task)
    registry = ReadingService(root, task_store=store)
    assert registry.coverage(task, cropped.result['reading_id'], cropped.result['segment_id']) == []
    cached = read.execute(dict(path=entry['path'], pages='1', region=[0, 0, .5, .5]))
    assert not cached.media and cached.result['reading_id'] == cropped.result['reading_id']
    args = submission(task, cropped.result)
    args['reading_evidence'][0]['location'] = location
    forged = deepcopy(args)
    forged['reading_evidence'][0]['location'].pop('region')
    assert ingest.execute(forged).status == 'error', '局部观察不能冒充整页观察'
    args['reading_coverage'].append({k: whole.result[k] for k in ('reading_id', 'segment_id')})
    result = ingest.execute(args)
    assert result.status == 'success', result.result
    assert result.result['status'] == 'succeeded'
    body = WikiService(root).read_reference(result.result['result_refs'][0]['publish_id'])
    assert body['sources'][0]['location']['region'] == [0., 0., .5, .5]
    assert body['sources'][0]['reading_copy']['supplemental']


@pytest.mark.parametrize('options', [
    dict(pages='1-2'), dict(pages='1', visual=False), dict(pages='1', offset=1),
    dict(pages='1', parse_id='old'), dict(pages='1', region=[0, 0, 0, 1]),
])
def test_invalid_region_does_not_send_media(flow, options):
    _, _, _, entry, read, _ = flow
    result = read.execute(dict(path=entry['path'], **{'region': [0, 0, .5, .5], **options}))
    assert result.status == 'error' and not result.media


def test_general_region_returns_cropped_pdf_without_registering_source(tmp_path, monkeypatch):
    from tests.test_reading_capabilities import ProbeModel
    from common.runtime_identity import current_identity, identity_scope
    from models.config_revision import capture_config_revision
    monkeypatch.setattr('agent.knowledge.reading_capabilities.ensure_pdf_capability', lambda *a: True)
    output = BytesIO()
    Image.new('RGB', (100, 100), 'blue').save(output, 'PDF')
    source = tmp_path / 'source.pdf'
    source.write_bytes(output.getvalue())
    read = Read(dict(cwd=str(tmp_path), document_reading_native_pdf_enabled=True))
    read.model = ProbeModel()
    with identity_scope(user_id='local-owner', session_id='region-general'):
        result = read.execute(dict(path=str(source), pages='1', region=[0, 0, .5, .5]))
        assert result.status == 'success', result.result
        context = dict(protocol='responses', model=read.model.model, api_base='https://offline.invalid/v1',
                       identity=current_identity(), config_revision=capture_config_revision(), task_id=None)
        data = result.media[0].loader(context).data
        assert data.startswith(b'%PDF-') and data != source.read_bytes()
        assert result.result['source_locations'][0]['location']['region'] == [0., 0., .5, .5]
    assert not (tmp_path / '.personal_sources/sources.sqlite3').exists()
