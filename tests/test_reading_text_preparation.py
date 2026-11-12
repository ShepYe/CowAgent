"""原生PDF准备不渲染；旧解析版本及显式回读仍可用。"""
from pypdf import PdfWriter

from agent.knowledge.parser import parse_source
from agent.knowledge.personal_assistant import SourceService


def test_native_pdf_preparation_skips_renderer_and_keeps_blank_positions(tmp_path, monkeypatch):
    pdf = tmp_path / 'scan.pdf'
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    writer.add_blank_page(200, 200)
    writer.write(pdf)
    def forbidden(*a, **kw):
        raise AssertionError('原生准备不得渲染整本PDF')
    monkeypatch.setattr('agent.knowledge.visual.inspect_pdf', forbidden)
    parsed = parse_source(pdf, prepare_only=True)
    assert parsed['units'] == []
    assert [c['physical_page'] for c in parsed['visual_coverage']] == [1, 2]
    assert all(not c['rendered'] and c['status'] == 'pending' for c in parsed['visual_coverage'])


def test_native_and_legacy_parse_caches_stay_separate(tmp_path, monkeypatch):
    pdf = tmp_path / 'blank.pdf'
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    writer.write(pdf)
    monkeypatch.setattr('agent.knowledge.visual.inspect_pdf', lambda *a, **kw: [
        dict(physical_page=1, text=False, rendered=True, status='blank')])
    service = SourceService(tmp_path)
    receipt = service.receive(pdf, filename='blank.pdf', request_id='receipt', origin={'channel': 'test'})
    legacy = service.parse(receipt['revision_id'])
    native = service.parse(receipt['revision_id'], prepare_only=True)
    assert legacy['parse_id'] != native['parse_id']
    assert service.parse(receipt['revision_id']) == legacy
    assert service.parse(receipt['revision_id'], prepare_only=True) == native
    assert service.parse(receipt['revision_id'], base_parse_id=legacy['parse_id']) == legacy
