"""PDF 材料离线契约：范围、预算、身份、不可变性与异根恢复。"""
from dataclasses import FrozenInstanceError, replace
from io import BytesIO
import json
from pathlib import Path
import shutil
import sys

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from agent.knowledge.reading_materials import (
    PdfBudget, ReadingMaterialError, load_pdf_materials, prepare_pdf_materials,
    verify_pdf_materials,
)


@pytest.fixture(autouse=True)
def empty_text_cache():
    from agent.knowledge import reading_materials
    with reading_materials._text_cache_lock:
        reading_materials._text_cache.clear()
        reading_materials._text_cache_bytes = 0


@pytest.fixture
def source(tmp_path):
    writer = PdfWriter()
    for number in range(1, 7):
        page = writer.add_blank_page(width=200 + number, height=300)
        # 有实际文字层的前五页；第六页模拟无文字层的扫描页。
        if number < 6:
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                     NameObject('/Subtype'): NameObject('/Type1'),
                                     NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({
                NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(f'BT /F1 12 Tf 10 10 Td (Page {number}) Tj ET'.encode())
            page[NameObject('/Contents')] = writer._add_object(stream)
    original = tmp_path / 'originals' / 'no-extension'
    original.parent.mkdir()
    with original.open('wb') as output:
        writer.write(output)
    return tmp_path


def prepare(root, **kwargs):
    options = dict(document_id='doc-1', revision_id='rev-1', parse_id='parse-1', config_revision='cfg-1')
    options.update(kwargs)
    return prepare_pdf_materials(root, 'originals/no-extension', **options)


def assert_error(code, function, *args, **kwargs):
    with pytest.raises(ReadingMaterialError) as error:
        function(*args, **kwargs)
    assert error.value.code == code


def test_original_text_scanned_and_no_model_or_render(source, monkeypatch):
    for name in ('agent.knowledge.visual', 'fitz', 'models', 'pytesseract'):
        monkeypatch.setitem(sys.modules, name, None)
    before = (source / 'originals/no-extension').read_bytes()
    result = prepare(source)
    assert result.page_count == 6
    assert len(result.segments) == 1
    segment = result.segments[0]
    assert segment.material_relpath == result.original_relpath
    assert segment.page_map == segment.target_pages == tuple(range(1, 7))
    assert 'Page 1' in segment.text_pages[0]
    assert segment.text_pages[-1] == ''
    assert segment.material_hash == result.original_hash
    assert before == (source / result.original_relpath).read_bytes()
    assert not (source / 'reading_materials').exists()
    with pytest.raises(FrozenInstanceError):
        segment.page_map = (9,)
    assert verify_pdf_materials(source, result) is result


def test_material_verification_reuses_text_but_still_rejects_changed_bytes(source, monkeypatch):
    from pypdf._page import PageObject
    count = []
    original = PageObject.extract_text
    def extract(page, *args, **kwargs):
        count.append(1)
        return original(page, *args, **kwargs)
    monkeypatch.setattr(PageObject, 'extract_text', extract)
    result = prepare(source)
    assert len(count) == 6
    verify_pdf_materials(source, result)
    verify_pdf_materials(source, result)
    assert len(count) == 6
    path = source / result.original_relpath
    path.write_bytes(path.read_bytes() + b'changed')
    assert_error('material_tampered', verify_pdf_materials, source, result)


def test_batches_overlap_mapping_and_target_partition(source):
    result = prepare(source, budget=PdfBudget(max_pages=4), overlap_pages=1)
    assert len(result.segments) > 1
    assert tuple(p for s in result.segments for p in s.target_pages) == result.target_pages
    assert any(set(a.page_map) & set(b.page_map) for a, b in zip(result.segments, result.segments[1:]))
    original = PdfReader(source / result.original_relpath)
    for segment in result.segments:
        reader = PdfReader(source / segment.material_relpath)
        assert len(reader.pages) == len(segment.page_map) <= 4
        for batch, physical in enumerate(segment.page_map, 1):
            assert segment.physical_page(batch) == physical
            assert reader.pages[batch - 1].mediabox == original.pages[physical - 1].mediabox
        assert set(segment.target_pages) <= set(segment.page_map)
        for bad in (0, -1, len(segment.page_map) + 1, True, '1'):
            assert_error('invalid_location', segment.physical_page, bad)


def test_subset_only_extracts_selected_text(source, monkeypatch):
    from pypdf._page import PageObject
    original = PageObject.extract_text
    seen = []

    def extract(page, *args, **kwargs):
        seen.append(int(page.mediabox.width) - 200)
        return original(page, *args, **kwargs)

    monkeypatch.setattr(PageObject, 'extract_text', extract)
    result = prepare(source, target_pages=(3, 4))
    assert set(seen) == {3, 4}
    assert result.segments[0].page_map == (3, 4)
    assert result.segments[0].material_relpath != result.original_relpath


def test_all_scan_pdf_supported(source):
    writer = PdfWriter()
    page = writer.add_blank_page(100, 100)
    image = DecodedStreamObject()
    image.set_data(b'\xff\x00\x00')
    image.update({NameObject('/Type'): NameObject('/XObject'),
                  NameObject('/Subtype'): NameObject('/Image'),
                  NameObject('/Width'): NumberObject(1), NameObject('/Height'): NumberObject(1),
                  NameObject('/ColorSpace'): NameObject('/DeviceRGB'),
                  NameObject('/BitsPerComponent'): NumberObject(8)})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/XObject'): DictionaryObject({
        NameObject('/Scan'): writer._add_object(image)})})
    stream = DecodedStreamObject()
    stream.set_data(b'q 100 0 0 100 0 0 cm /Scan Do Q')
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    (source / 'originals/no-extension').write_bytes(output.getvalue())
    result = prepare(source)
    assert result.segments[0].text_pages == ('',)
    assert result.target_pages == (1,)
    assert len(PdfReader(source / result.segments[0].material_relpath).pages[0].images) == 1


@pytest.mark.parametrize('field', ['max_original_bytes', 'max_request_bytes', 'max_pages', 'max_context_tokens'])
def test_each_budget_forces_split(source, field):
    # 用三页批次的实际大小作为上限，验证不能只按页数分批。
    probe = prepare(source, target_pages=(1, 2, 3)).segments[0]
    limits = dict(max_original_bytes=probe.original_bytes,
                  max_request_bytes=probe.request_bytes, max_pages=3,
                  max_context_tokens=probe.estimated_context_tokens)
    budget = replace(PdfBudget(), **{field: limits[field]})
    result = prepare(source, budget=budget)
    assert len(result.segments) > 1
    for segment in result.segments:
        assert segment.original_bytes <= budget.max_original_bytes
        assert segment.request_bytes <= budget.max_request_bytes
        assert len(segment.page_map) <= budget.max_pages
        assert segment.estimated_context_tokens <= budget.max_context_tokens
        assert segment.request_bytes == 4 * ((segment.original_bytes + 2) // 3) + budget.request_overhead_bytes


def test_reservations_and_impossible_single_page_have_no_partial_writes(source):
    budget = PdfBudget(max_context_tokens=23000)
    result = prepare(source, budget=budget)
    assert all(len(s.page_map) == 1 for s in result.segments)
    assert_error('budget_exceeded', prepare, source,
                 budget=replace(budget, reserved_output_tokens=18000))
    shutil.rmtree(source / 'reading_materials')
    assert_error('budget_exceeded', prepare, source, budget=PdfBudget(max_pages=1), overlap_pages=1)
    assert not (source / 'reading_materials').exists()


def test_restore_different_root_and_reuse_without_rewriting(source, tmp_path):
    result = prepare(source, budget=PdfBudget(max_pages=2))
    paths = [source / s.material_relpath for s in result.segments]
    times = [p.stat().st_mtime_ns for p in paths]
    assert prepare(source, budget=result.budget) == result
    assert times == [p.stat().st_mtime_ns for p in paths]
    restored = source.parent / (source.name + '-restored')
    shutil.copytree(source, restored)
    manifest = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))
    loaded = load_pdf_materials(restored, manifest, expected_manifest_hash=result.manifest_hash)
    assert loaded == result
    assert str(source) not in json.dumps(manifest)
    assert all(not Path(s.material_relpath).is_absolute() for s in result.segments)


@pytest.mark.parametrize('field,value', [('document_id', 'doc-2'), ('revision_id', 'rev-2'),
                                       ('parse_id', 'parse-2'), ('config_revision', 'cfg-2'),
                                       ('target_pages', (2, 3))])
def test_same_bytes_distinct_identity_and_scope(source, field, value):
    base = prepare(source, target_pages=(1, 2))
    options = dict(target_pages=(1, 2))
    options[field] = value
    changed = prepare(source, **options)
    assert base.original_hash == changed.original_hash
    assert base.cache_key != changed.cache_key
    assert base.segments[0].segment_id != changed.segments[0].segment_id


def test_tampering_material_original_missing_and_cache_not_repaired(source):
    result = prepare(source, budget=PdfBudget(max_pages=2))
    path = source / result.segments[0].material_relpath
    original = path.read_bytes()
    path.write_bytes(original + b'changed')
    assert_error('material_tampered', verify_pdf_materials, source, result)
    assert_error('material_tampered', prepare, source, budget=result.budget)
    path.unlink()
    assert_error('material_unavailable', verify_pdf_materials, source, result)
    path.write_bytes(original)
    (source / result.original_relpath).write_bytes(b'changed')
    assert_error('material_tampered', verify_pdf_materials, source, result)


def test_manifest_anchor_mapping_and_unknown_schema_rejected(source):
    result = prepare(source, budget=PdfBudget(max_pages=2))
    raw = result.to_dict()
    raw['segments'][0]['page_map'][0] = 6
    assert_error('invalid_manifest', load_pdf_materials, source, raw,
                 expected_manifest_hash=result.manifest_hash)
    altered = replace(result, segments=(replace(result.segments[0], page_map=(6, 2)),) + result.segments[1:])
    # 即使外部误信新 hash，仍重建映射对照原件。
    assert_error('invalid_manifest', load_pdf_materials, source, altered.to_dict(),
                 expected_manifest_hash=altered.manifest_hash)
    boolean_page = replace(result, segments=(replace(result.segments[0], page_map=(True, 2)),) + result.segments[1:])
    assert_error('invalid_manifest', verify_pdf_materials, source, boolean_page)
    unknown = replace(result, schema_version=2)
    assert_error('invalid_manifest', load_pdf_materials, source, unknown.to_dict(),
                 expected_manifest_hash=unknown.manifest_hash)
    assert_error('invalid_manifest', load_pdf_materials, source, result.to_dict(), expected_manifest_hash='')


@pytest.mark.parametrize('pages', [(), (0,), (7,), (True,), (2, 1), (1, 1), ('1',)])
def test_invalid_target_ranges(source, pages):
    assert_error('invalid_location', prepare, source, target_pages=pages)


@pytest.mark.parametrize('path', ['../file', '/file', 'C:/file', 'C:file', 'originals/../file',
                                  'originals\\file', './file', 'originals//file', 'originals/file:stream'])
def test_paths_cannot_escape_root(source, path):
    assert_error('invalid_path', prepare_pdf_materials, source, path,
                 document_id='d', revision_id='r', parse_id='p', config_revision='c')


def test_invalid_budget_and_pdf(source):
    assert_error('invalid_request', prepare, source, budget=PdfBudget(max_pages=True))
    assert_error('invalid_request', prepare, source, overlap_pages=-1)
    assert_error('invalid_request', prepare, source, document_id='')
    (source / 'originals/no-extension').write_bytes(b'not a PDF')
    assert_error('parse_failed', prepare, source)


def test_encrypted_pdf_rejected(source):
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.encrypt('secret')
    output = BytesIO()
    writer.write(output)
    (source / 'originals/no-extension').write_bytes(output.getvalue())
    assert_error('parse_failed', prepare, source)


def test_unknown_fields_and_invalid_shapes_rejected(source):
    from agent.knowledge.reading_materials import _digest
    result = prepare(source)
    for extra in ({'schema_version': True}, {'future_field': 'unknown'}, {'segments': [None]}):
        raw = result.to_dict()
        raw.pop('manifest_hash')
        raw.update(extra)
        anchor = _digest(raw)
        raw['manifest_hash'] = anchor
        assert_error('invalid_manifest', load_pdf_materials, source, raw, expected_manifest_hash=anchor)
