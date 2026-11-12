"""PDF 材料离线契约：范围、预算、身份、不可变性与异根恢复。"""
from dataclasses import FrozenInstanceError, replace
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from pathlib import Path
import shutil
import sys

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject, DecodedStreamObject, DictionaryObject, IndirectObject, NameObject, NumberObject,
)

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
    with reading_materials._plan_hash_cache_lock:
        reading_materials._plan_hash_cache.clear()
    if hasattr(reading_materials, '_region_hash_cache'):
        with reading_materials._region_hash_cache_lock:
            reading_materials._region_hash_cache.clear()


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


@pytest.fixture
def compressed_cmap_source(source):
    """间接 CID 字体和压缩 ToUnicode：extract_text 会改变 reader 对象图。"""
    return write_compressed_cmap_source(source, 3)


def write_compressed_cmap_source(source, page_count):
    writer = PdfWriter()
    cid = DictionaryObject({
        NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/CIDFontType2'),
        NameObject('/BaseFont'): NameObject('/ArialMT'),
        NameObject('/DW'): NumberObject(1000),
    })
    cmap = DecodedStreamObject()
    cmap.set_data(
        b'/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n'
        b'1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n'
        b'1 beginbfchar\n<0041> <0041>\nendbfchar\nendcmap\nend\nend'
    )
    font = writer._add_object(DictionaryObject({
        NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type0'),
        NameObject('/BaseFont'): NameObject('/ArialMT'),
        NameObject('/Encoding'): NameObject('/Identity-H'),
        NameObject('/DescendantFonts'): writer._add_object(ArrayObject([writer._add_object(cid)])),
        NameObject('/ToUnicode'): writer._add_object(cmap.flate_encode()),
    }))
    # 必须留一页不选，避免全页路径直接复用原件字节。
    for number in range(1, page_count + 1):
        page = writer.add_blank_page(width=200 + number, height=300)
        page[NameObject('/Resources')] = DictionaryObject({
            NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 10 10 Td <0041> Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream.flate_encode())
    output = BytesIO()
    writer.write(output)
    (source / 'originals/no-extension').write_bytes(output.getvalue())
    return source


def old_v1_materials(source, targets, budget=None):
    """冻结旧版的同 reader／LRU 行为，实际跑准备和立即自验两遍。"""
    from agent.knowledge import reading_materials as rm
    cache = OrderedDict()
    cache_bytes = 0

    def text(reader, version, source_hash, page):
        nonlocal cache_bytes
        key = (version, source_hash, page)
        if key in cache:
            cache.move_to_end(key)
            return cache[key][0]
        value = reader.pages[page - 1].extract_text() or ''
        size = len(value.encode('utf-8'))
        if size <= 8 * 1024 * 1024:
            cache[key] = (value, size)
            cache_bytes += size
            while len(cache) > 128 or cache_bytes > 8 * 1024 * 1024:
                _, (_, removed) = cache.popitem(last=False)
                cache_bytes -= removed
        return value

    options = dict(document_id='doc-1', revision_id='rev-1', parse_id='parse-1',
                   config_revision='cfg-1', target_pages=targets, budget=budget or PdfBudget(), overlap_pages=0)
    data = (source / 'originals/no-extension').read_bytes()
    original_pdf = rm._pdf
    plans = []
    for _ in range(2):
        reader = original_pdf(data)
        # 将新实现两个 reader 合回旧版同一对象；其余规划和序列化走真实路径。
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(rm, '_pdf', lambda data: reader)
            patch.setattr(rm, '_page_text_by_hash', text)
            plans.append(rm._plan(source, 'originals/no-extension', **options))
    assert plans[0][0].manifest_hash == plans[1][0].manifest_hash
    materials, outputs = plans[0]
    for segment, payload in outputs:
        rm._store(source, segment.material_relpath, payload)
    return materials


def test_old_v1_evicted_text_cache_restores_without_rewriting(source, tmp_path):
    from agent.knowledge import reading_materials as rm
    write_compressed_cmap_source(source, 130)
    old = old_v1_materials(source, tuple(range(1, 130)))
    paths = [source / s.material_relpath for s in old.segments]
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
    assert load_pdf_materials(source, old.to_dict(), expected_manifest_hash=old.manifest_hash) == old
    assert before == [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
    restored = tmp_path / 'old-v1-restored'
    shutil.copytree(source, restored, ignore=shutil.ignore_patterns('old-v1-restored'))
    with rm._plan_hash_cache_lock:
        rm._plan_hash_cache.clear()
    with rm._text_cache_lock:
        rm._text_cache.clear()
        rm._text_cache_bytes = 0
    assert load_pdf_materials(restored, old.to_dict(), expected_manifest_hash=old.manifest_hash) == old
    clean = prepare(source, target_pages=old.target_pages)
    assert clean.manifest_hash != old.manifest_hash


def test_verification_reuses_only_planning_and_always_reads_bytes(source, monkeypatch):
    from agent.knowledge import reading_materials as rm
    prepared = prepare(source, target_pages=(1, 2, 3, 4))
    calls, reads = [], []
    plan, read = rm._plan, rm._read

    def counted_plan(*args, **kwargs):
        calls.append(1)
        return plan(*args, **kwargs)

    def counted_read(root, path):
        reads.append(path)
        return read(root, path)

    monkeypatch.setattr(rm, '_plan', counted_plan)
    monkeypatch.setattr(rm, '_read', counted_read)
    for _ in range(30):
        assert verify_pdf_materials(source, prepared) is prepared
    assert len(calls) <= 1
    assert reads.count(prepared.original_relpath) >= 30
    assert reads.count(prepared.segments[0].material_relpath) >= 30
    for relative in (prepared.original_relpath, prepared.segments[0].material_relpath):
        path = source / relative
        original = path.read_bytes()
        path.write_bytes(original + b'changed')
        assert_error('material_tampered', verify_pdf_materials, source, prepared)
        path.write_bytes(original)


def plan_subset(source):
    from agent.knowledge import reading_materials
    return reading_materials._plan(
        source, 'originals/no-extension', document_id='doc-1', revision_id='rev-1',
        parse_id='parse-1', config_revision='cfg-1', target_pages=(1, 2),
        budget=PdfBudget(), overlap_pages=0,
    )


def test_compressed_cmap_fixture_mutates_reader_when_extracting_text(compressed_cmap_source):
    from agent.knowledge import reading_materials
    data = (compressed_cmap_source / 'originals/no-extension').read_bytes()
    clean = reading_materials._subset(PdfReader(BytesIO(data)), (1, 2))
    reader = PdfReader(BytesIO(data))
    font = reader.pages[0]['/Resources']['/Font']['/F1']
    descendants = font['/DescendantFonts']
    assert isinstance(descendants[0], IndirectObject)
    assert font['/ToUnicode'].decoded_self is None
    assert reader.pages[0].extract_text() == 'A'
    assert font['/ToUnicode'].decoded_self is not None
    # pypdf 7 的 Font.from_pdf_object 会把间接字体替换为字典，写出时内联。
    assert isinstance(descendants[0], DictionaryObject)
    mutated = reading_materials._subset(reader, (1, 2))
    assert mutated != clean
    assert (compressed_cmap_source / 'originals/no-extension').read_bytes() == data


def test_compressed_cmap_plan_bytes_do_not_depend_on_text_cache(compressed_cmap_source):
    cold, cold_outputs = plan_subset(compressed_cmap_source)
    hot, hot_outputs = plan_subset(compressed_cmap_source)
    assert cold.segments[0].text_pages == hot.segments[0].text_pages == ('A', 'A')
    assert cold_outputs[0][1] == hot_outputs[0][1]
    assert cold.manifest_hash == hot.manifest_hash


def test_compressed_text_subset_survives_cold_cache_and_restore(compressed_cmap_source, tmp_path):
    from agent.knowledge import reading_materials
    source = compressed_cmap_source
    # 本两页夹具的旧成功变体来自热缓存；大范围淘汰场景另行验证。
    plan_subset(source)
    prepared = prepare(source, target_pages=(1, 2))
    stored = (source / prepared.segments[0].material_relpath).read_bytes()
    assert prepared.schema_version == 1
    assert stored == reading_materials._subset(PdfReader(source / 'originals/no-extension'), (1, 2))
    # 新进程没有文字缓存，但必须恢复同一份成功登记的 v1 清单与字节。
    with reading_materials._text_cache_lock:
        reading_materials._text_cache.clear()
        reading_materials._text_cache_bytes = 0
    assert load_pdf_materials(source, prepared.to_dict(), expected_manifest_hash=prepared.manifest_hash) == prepared
    assert prepare(source, target_pages=(1, 2)) == prepared
    restored = tmp_path / 'independent-restore'
    shutil.copytree(source, restored, ignore=shutil.ignore_patterns('independent-restore'))
    with reading_materials._text_cache_lock:
        reading_materials._text_cache.clear()
        reading_materials._text_cache_bytes = 0
    assert load_pdf_materials(restored, prepared.to_dict(), expected_manifest_hash=prepared.manifest_hash) == prepared
    assert (restored / prepared.segments[0].material_relpath).read_bytes() == stored


@pytest.mark.parametrize('pages,text_size', [(9, 1024 * 1024), (1, 8 * 1024 * 1024 + 1)])
def test_old_v1_byte_eviction_and_uncacheable_page(source, monkeypatch, pages, text_size):
    from pypdf._page import PageObject
    write_compressed_cmap_source(source, pages + 1)
    extract = PageObject.extract_text

    def large_text(page, *args, **kwargs):
        assert extract(page, *args, **kwargs) == 'A'
        return 'A' * text_size

    monkeypatch.setattr(PageObject, 'extract_text', large_text)
    old = old_v1_materials(source, tuple(range(1, pages + 1)),
                           PdfBudget(max_context_tokens=20 * 1024 * 1024))
    assert load_pdf_materials(source, old.to_dict(), expected_manifest_hash=old.manifest_hash) == old
    assert prepare(source, target_pages=old.target_pages, budget=old.budget).manifest_hash != old.manifest_hash


def test_legacy_rebuilds_are_isolated_between_threads_and_global_cache(source, monkeypatch):
    from threading import Barrier
    from agent.knowledge import reading_materials as rm
    write_compressed_cmap_source(source, 130)
    old = old_v1_materials(source, tuple(range(1, 130)))
    data = (source / old.original_relpath).read_bytes()
    rm._expected_manifest_hash(source, old, data, 'clean')
    snapshot = list(rm._text_cache.items()), rm._text_cache_bytes
    barrier = Barrier(2)
    factory = rm._legacy_text_reader

    def isolated_reader():
        text = factory()
        first = True

        def synchronized_text(*args):
            nonlocal first
            if first:
                first = False
                barrier.wait(timeout=10)
            return text(*args)
        return synchronized_text

    def no_global_text(*args):
        pytest.fail('legacy 不得读写全局文字缓存')

    monkeypatch.setattr(rm, '_legacy_text_reader', isolated_reader)
    monkeypatch.setattr(rm, '_page_text_by_hash', no_global_text)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: verify_pdf_materials(source, old), range(2)))
    assert results == [old, old]
    assert snapshot == (list(rm._text_cache.items()), rm._text_cache_bytes)


def test_legacy_rejects_third_bytes_and_forged_metadata_after_cache_hit(source, monkeypatch):
    from agent.knowledge import reading_materials as rm
    write_compressed_cmap_source(source, 130)
    old = old_v1_materials(source, tuple(range(1, 130)))
    verify_pdf_materials(source, old)
    # 同一旧材料的后续校验只复用重建 hash，仍逐次读实际原件和分段。
    with monkeypatch.context() as patch:
        def no_plan(*args, **kwargs):
            pytest.fail('已缓存的重建不得再次解析')
        patch.setattr(rm, '_plan', no_plan)
        for _ in range(3):
            verify_pdf_materials(source, old)
        for relative in (old.original_relpath, old.segments[0].material_relpath):
            path = source / relative
            before = path.read_bytes()
            path.write_bytes(before + b'changed')
            assert_error('material_tampered', verify_pdf_materials, source, old)
            path.write_bytes(before)
    segment = old.segments[0]
    for forged in (
        replace(old, page_count=131),
        replace(old, preparation_version='pdf-materials-v1/pypdf-other'),
        replace(old, target_pages=old.target_pages[:-1]),
        replace(old, budget=replace(old.budget, max_pages=1)),
        replace(old, segments=(replace(segment, page_map=(2,) + segment.page_map[1:]),) + old.segments[1:]),
        replace(old, segments=(replace(segment, text_pages=('forged',) + segment.text_pages[1:]),) + old.segments[1:]),
        replace(old, segments=(replace(segment, estimated_context_tokens=1),) + old.segments[1:]),
    ):
        assert_error('invalid_manifest', load_pdf_materials, source, forged.to_dict(),
                     expected_manifest_hash=forged.manifest_hash)
    payload = (source / segment.material_relpath).read_bytes() + b'\n% third variant\n'
    payload_hash = rm._hash(payload)
    sid = rm._digest(dict(cache_key=old.cache_key, page_map=segment.page_map,
                          target_pages=segment.target_pages, material_hash=payload_hash))
    relpath = f'reading_materials/{old.cache_key}/{sid}.pdf'
    rm._store(source, relpath, payload)
    forged_segment = replace(segment, segment_id=sid, material_relpath=relpath,
                             material_hash=payload_hash, original_bytes=len(payload),
                             request_bytes=4 * ((len(payload) + 2) // 3) + old.budget.request_overhead_bytes)
    forged = replace(old, segments=(forged_segment,) + old.segments[1:])
    assert_error('invalid_manifest', load_pdf_materials, source, forged.to_dict(),
                 expected_manifest_hash=forged.manifest_hash)


def test_plan_hash_cache_is_bounded_and_contains_only_hashes(source):
    from agent.knowledge import reading_materials as rm
    prepared = prepare(source, target_pages=(1, 2))
    data = (source / prepared.original_relpath).read_bytes()
    for i in range(130):
        item = replace(prepared, document_id=f'doc-{i}')
        rm._expected_manifest_hash(source, item, data, 'clean')
    assert len(rm._plan_hash_cache) == 128
    assert all(len(key) == 64 and len(value) == 64 for key, value in rm._plan_hash_cache.items())


def test_region_cold_restore_cache_and_actual_tampering(source, tmp_path, monkeypatch):
    from agent.knowledge import reading_materials as rm
    base = prepare(source)
    crop = rm.prepare_pdf_region(source, base, 1, [0, 0, .5, .5])
    if hasattr(rm, '_region_hash_cache'):
        rm._region_hash_cache.clear()
    plan = rm._plan_region
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return plan(*args, **kwargs)

    monkeypatch.setattr(rm, '_plan_region', counted)
    verify_pdf_materials(source, crop)
    verify_pdf_materials(source, crop)
    assert len(calls) == 1
    restored = tmp_path / 'region-restored'
    shutil.copytree(source, restored, ignore=shutil.ignore_patterns('region-restored'))
    assert load_pdf_materials(restored, crop.to_dict(), expected_manifest_hash=crop.manifest_hash) == crop
    assert len(calls) == 1
    for relative in (crop.original_relpath, crop.region_supplement['png_relpath'],
                     crop.segments[0].material_relpath):
        path = restored / relative
        original = path.read_bytes()
        path.write_bytes(original + b'changed')
        assert_error('material_tampered', verify_pdf_materials, restored, crop)
        path.write_bytes(original)
    rm._region_hash_cache.clear()
    verify_pdf_materials(restored, crop)
    assert len(calls) == 2


@pytest.mark.parametrize('version', ['pypdf', 'zlib', 'pdfium', 'binding', 'PIL'])
def test_region_cache_does_not_hide_runtime_version_change(source, monkeypatch, version):
    import PIL
    import pypdfium2 as pdfium
    from agent.knowledge import reading_materials as rm
    crop = rm.prepare_pdf_region(source, prepare(source), 1, [0, 0, .5, .5])
    owner, name = {'pypdf': (rm.pypdf, '__version__'),
                   'zlib': (rm.zlib, 'ZLIB_RUNTIME_VERSION'),
                   'pdfium': (pdfium.version, 'PDFIUM_INFO'),
                   'binding': (pdfium.version, 'PYPDFIUM_INFO'),
                   'PIL': (PIL, '__version__')}[version]
    monkeypatch.setattr(owner, name, str(getattr(owner, name)) + '-changed')
    assert_error('invalid_manifest', verify_pdf_materials, source, crop)


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
