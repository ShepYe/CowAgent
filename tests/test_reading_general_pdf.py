"""普通read复用媒体且不建立知识来源，明确文字分页保留。"""
from io import BytesIO

from PIL import Image
import pytest

from agent.tools.read.read import Read
from common.runtime_identity import identity_scope, current_identity
from models.config_revision import capture_config_revision


def test_general_pdf_media_is_scoped_and_original_hash_is_rechecked(tmp_path, monkeypatch):
    from tests.test_reading_capabilities import ProbeModel
    monkeypatch.setattr('agent.knowledge.reading_capabilities.ensure_pdf_capability', lambda *a: True)
    pdf = tmp_path / 'original.pdf'
    raw = BytesIO()
    Image.new('RGB', (30, 30), 'red').save(raw, 'PDF')
    pdf.write_bytes(raw.getvalue())
    tool = Read(dict(cwd=str(tmp_path), document_reading_native_pdf_enabled=True))
    tool.model = ProbeModel()
    with identity_scope(user_id='local-owner', session_id='general'):
        result = tool.execute(dict(path=str(pdf), pages='1'))
        assert result.status == 'success', result.result
        assert result.result['media_attached'] and 'reading_id' not in result.result
        context = dict(protocol='responses', model=tool.model.model, api_base='https://offline.invalid/v1',
            identity=current_identity(), config_revision=capture_config_revision(), task_id=None)
        ref = result.media[0]
        assert ref.supports(context)
        assert ref.loader(context).data == pdf.read_bytes()
        assert not ref.supports(dict(context, task_id='other'))
        pdf.write_bytes(b'changed')
        with pytest.raises(ValueError, match='version_conflict'):
            ref.loader(context)
    assert not (tmp_path / '.personal_sources' / 'sources.sqlite3').exists()


@pytest.mark.parametrize('options', [{'visual': False}, {'offset': 1}, {'limit': 2}])
def test_general_text_pagination_does_not_probe_or_send_media(tmp_path, monkeypatch, options):
    monkeypatch.setattr('agent.knowledge.reading_capabilities.ensure_pdf_capability',
                        lambda *a: pytest.fail('文字分页禁止新模型调用'))
    pdf = tmp_path / 'original.pdf'
    raw = BytesIO()
    Image.new('RGB', (30, 30), 'red').save(raw, 'PDF')
    pdf.write_bytes(raw.getvalue())
    tool = Read(dict(cwd=str(tmp_path), document_reading_native_pdf_enabled=True))
    result = tool.execute(dict(path=str(pdf), **options))
    assert result.status == 'success' and not result.media
