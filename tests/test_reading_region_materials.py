"""真实 PDFium 局部裁剪：位置、像素、预算、不可变性及异根恢复。"""
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json
import shutil
import socket
import sys

from PIL import Image
import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject
import pytest

from agent.knowledge.reading_materials import (
    PdfBudget, ReadingMaterialError, _digest, load_pdf_materials, prepare_pdf_materials,
    prepare_pdf_region, replan_pdf_segment, retry_pdf_segment, verify_pdf_materials,
)
from tests import test_office_reading_flow as office_helpers
from tests.test_office_reading_flow import prepare as prepare_office, setup as setup_office


office_env = office_helpers.env


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("局部材料测试不得出网")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture
def source(tmp_path):
    writer = PdfWriter()
    for number in range(3):
        page = writer.add_blank_page(width=200, height=100)
        content = DecodedStreamObject()
        # PDF 原点在左下；页面显示的左上红、右上绿、左下蓝、右下黄。
        content.set_data(b"1 0 0 rg 0 50 100 50 re f 0 1 0 rg 100 50 100 50 re f "
                         b"0 0 1 rg 0 0 100 50 re f 1 1 0 rg 100 0 100 50 re f")
        page[NameObject("/Contents")] = writer._add_object(content)
        if number == 2:
            page.rotate(90)
    original = tmp_path / "original.pdf"
    writer.write(original)
    materials = prepare_pdf_materials(tmp_path, original.name, document_id="doc", revision_id="rev",
                                     parse_id="parse", config_revision="cfg")
    return tmp_path, materials


def error(code, function, *args, **kwargs):
    with pytest.raises(ReadingMaterialError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code


def test_only_selected_page_is_rendered_under_shared_lock_and_pdf_is_lossless(source, monkeypatch):
    from agent.knowledge import visual
    root, base = source
    before = (root / base.original_relpath).read_bytes()
    loaded_pages, rendered = [], []
    lock = visual._pdfium_lock
    inside = []

    class ObservedLock:
        def __enter__(self):
            lock.acquire()
            inside.append(True)

        def __exit__(self, *args):
            inside.pop()
            lock.release()

    original_page = pdfium.PdfDocument.get_page
    original_render = pdfium.PdfPage.render

    def get_page(doc, index):
        assert inside
        loaded_pages.append(index)
        return original_page(doc, index)

    def render(page, *args, **kwargs):
        assert inside
        rendered.append(kwargs)
        return original_render(page, *args, **kwargs)

    monkeypatch.setattr(visual, "_pdfium_lock", ObservedLock())
    monkeypatch.setattr(pdfium.PdfDocument, "get_page", get_page)
    monkeypatch.setattr(pdfium.PdfPage, "render", render)
    crop = prepare_pdf_region(root, base, 2, [0, 0, .5, .5])
    assert loaded_pages and set(loaded_pages) == {1}
    assert all(r["crop"] == (0, 50, 100, 0) for r in rendered)
    assert all(r["scale"] == 3 for r in rendered)
    assert crop.location(2) == dict(kind="pdf", physical_page=2, region=[0, 0, .5, .5])
    assert crop.coverage_keys() == crop.coverage_keys([2]) == []
    assert base.coverage_keys() == ["rev:1", "rev:2", "rev:3"]
    assert crop.page_count == 3 and crop.target_pages == (2,)
    segment = crop.segments[0]
    assert segment.page_map == segment.target_pages == (2,)
    assert segment.physical_page(1) == 2 and segment.text_pages == ("",)
    assert (root / base.original_relpath).read_bytes() == before
    with Image.open(root / crop.region_supplement["png_relpath"]) as png:
        assert png.size == (300, 150) and png.getpixel((150, 75)) == (255, 0, 0)
        pdf = PdfReader(root / segment.material_relpath)
        assert len(pdf.pages) == 1 and not pdf.pages[0].extract_text()
        image = list(pdf.pages[0].images)[0].image
        assert image.size == png.size and image.tobytes() == png.tobytes()
    error("invalid_location", crop.location, 1)
    assert crop.source_hash == base.source_hash and crop.source_relpath == base.source_relpath


@pytest.mark.parametrize("page,region,color", [(2, [0, .5, .5, 1], (0, 0, 255)),
                                             (2, [.5, 0, 1, .5], (0, 255, 0)),
                                             (3, [0, 0, .5, .5], (0, 0, 255))])
def test_top_left_coordinates_and_rotated_display_page(source, page, region, color):
    root, base = source
    crop = prepare_pdf_region(root, base, page, region)
    with Image.open(root / crop.region_supplement["png_relpath"]) as png:
        assert png.getpixel((png.width // 2, png.height // 2)) == color


def test_cache_no_rewrite_different_regions_and_restore(source, tmp_path):
    root, base = source
    before = base.to_dict()
    crop = prepare_pdf_region(root, base, 2, [0, 0, .5, .5])
    paths = [root / crop.region_supplement["png_relpath"], root / crop.segments[0].material_relpath]
    stamps = [p.stat().st_mtime_ns for p in paths]
    again = prepare_pdf_region(root, base, 2, [0, 0, .5, .5])
    assert again.to_dict() == crop.to_dict()
    assert [p.stat().st_mtime_ns for p in paths] == stamps
    other = prepare_pdf_region(root, base, 2, [.5, .5, 1, 1])
    assert other.cache_key != crop.cache_key and other.manifest_hash != crop.manifest_hash
    restored = tmp_path / "restored"
    shutil.copytree(root, restored, ignore=shutil.ignore_patterns("restored"))
    loaded = load_pdf_materials(restored, crop.to_dict(), expected_manifest_hash=crop.manifest_hash)
    assert loaded == crop and str(root) not in json.dumps(crop.to_dict())
    assert base.to_dict() == before
    assert retry_pdf_segment(root, crop, crop.segments[0].segment_id) == crop
    error("reading_budget_exceeded", replan_pdf_segment, root, crop, crop.segments[0].segment_id)
    error("invalid_request", prepare_pdf_region, root, crop, 2, [0, 0, 1, 1])


def test_old_payload_and_hash_unchanged_without_region(source):
    _, base = source
    old_payload = asdict(base)
    old_payload.pop("derived_source")
    old_payload.pop("region_supplement")
    assert "region_supplement" not in base.to_dict()
    assert base.manifest_hash == _digest(old_payload)


@pytest.mark.parametrize("region", [[-.1, 0, 1, 1], [0, 0, 1.1, 1], [0, 1, 1, 0],
                                    [0, 0, 0, 1], [False, 0, 1, 1], [0, 0, float("nan"), 1],
                                    [0, 0, float("inf"), 1], [0, 0, 1], "0,0,1,1"])
def test_invalid_regions_rejected_without_render(source, monkeypatch, region):
    root, base = source
    monkeypatch.setattr(pdfium.PdfPage, "render", lambda *a, **k: pytest.fail("非法区域不得渲染"))
    error("invalid_location", prepare_pdf_region, root, base, 2, region)


@pytest.mark.parametrize("page", [0, 4, True, "2"])
def test_invalid_source_page(source, page):
    root, base = source
    error("invalid_location", prepare_pdf_region, root, base, page, [0, 0, 1, 1])


def test_pixel_png_and_pdf_request_budgets(source):
    root, base = source
    crop = prepare_pdf_region(root, base, 2, [0, 0, 1, 1], max_pixels=1000)
    details = crop.region_supplement
    assert details["pixel_width"] * details["pixel_height"] <= 1000
    assert details["limits"]["max_pixels"] == 1000
    error("budget_exceeded", prepare_pdf_region, root, base, 2, [0, 0, 1, 1], max_png_bytes=1)
    # 原生PDF自身可放行，而图像PDF超过此预算；不允许缩图后忽略请求预算。
    restricted = prepare_pdf_materials(root, base.original_relpath, document_id="doc", revision_id="rev",
                                      parse_id="parse", config_revision="cfg", budget=PdfBudget(max_request_bytes=7000))
    error("budget_exceeded", prepare_pdf_region, root, restricted, 2, [0, 0, 1, 1], scale=8)
    for limits in (dict(scale=True), dict(scale=float("inf")), dict(max_pixels=0), dict(max_png_bytes=True)):
        error("invalid_request", prepare_pdf_region, root, base, 2, [0, 0, 1, 1], **limits)


@pytest.mark.parametrize("kind", ["png", "pdf", "source"])
def test_damaged_material_is_rejected_not_repaired(source, kind):
    root, base = source
    crop = prepare_pdf_region(root, base, 2, [0, 0, .5, .5])
    relative = dict(png=crop.region_supplement["png_relpath"], pdf=crop.segments[0].material_relpath,
                    source=base.original_relpath)[kind]
    (root / relative).write_bytes(b"damaged")
    error("material_tampered", verify_pdf_materials, root, crop)
    error("material_tampered", prepare_pdf_region, root, base, 2, [0, 0, .5, .5])


@pytest.mark.parametrize("field,value", [("region", [0, 0, 1, 1]), ("source_page", 1),
                                       ("pixel_width", 1), ("engine", {"name": "fake"})])
def test_reanchored_manifest_tampering_rejected_by_real_render(source, field, value):
    root, base = source
    crop = prepare_pdf_region(root, base, 2, [0, 0, .5, .5])
    supplement = deepcopy(crop.region_supplement)
    supplement[field] = value
    altered = replace(crop, region_supplement=supplement)
    error("invalid_manifest", load_pdf_materials, root, altered.to_dict(),
          expected_manifest_hash=altered.manifest_hash)


def test_broken_png_with_reanchored_hash_still_rejected(source):
    root, base = source
    crop = prepare_pdf_region(root, base, 2, [0, 0, .5, .5])
    (root / crop.region_supplement["png_relpath"]).write_bytes(b"not png")
    supplement = dict(crop.region_supplement, png_hash=sha256(b"not png").hexdigest())
    error("invalid_manifest", verify_pdf_materials, root, replace(crop, region_supplement=supplement))


def test_page_outside_main_targets_is_rejected(source):
    root, base = source
    narrow = prepare_pdf_materials(root, base.original_relpath, document_id="doc", revision_id="rev",
                                  parse_id="parse", config_revision="cfg", target_pages=[1])
    error("invalid_location", prepare_pdf_region, root, narrow, 2, [0, 0, 1, 1])


def test_missing_png_and_invalid_png_path_are_rejected(source):
    root, base = source
    crop = prepare_pdf_region(root, base, 2, [0, 0, .5, .5])
    invalid = replace(crop, region_supplement=dict(crop.region_supplement, png_relpath="../outside.png"))
    error("invalid_path", verify_pdf_materials, root, invalid)
    (root / crop.region_supplement["png_relpath"]).unlink()
    error("material_unavailable", verify_pdf_materials, root, crop)


@pytest.mark.parametrize("failure", ["missing-engine", "render-error"])
def test_renderer_failure_does_not_write_success_materials(source, monkeypatch, failure):
    root, base = source
    if failure == "missing-engine":
        monkeypatch.setitem(sys.modules, "pypdfium2", None)
        expected = "renderer_unavailable"
    else:
        def broken(*args, **kwargs):
            raise RuntimeError("simulated renderer failure")
        monkeypatch.setattr(pdfium.PdfPage, "render", broken)
        expected = "render_failed"
    error(expected, prepare_pdf_region, root, base, 2, [0, 0, .5, .5])
    assert not (root / "reading_materials").exists()


@pytest.mark.parametrize("suffix", ["docx", "pptx"])
def test_office_source_location_and_coverage_survive_region_and_restore(office_env, suffix, tmp_path):
    root, _, _, _, _ = office_env
    task, receipts = setup_office(office_env, (suffix,))
    _, _, base, _, _ = prepare_office(office_env, task, receipts[0])
    before = deepcopy(base.to_dict())
    crop = prepare_pdf_region(root, base, 2, [.1, .2, .8, .9], max_pixels=10000)
    assert crop.derived_source == base.derived_source
    assert crop.location(2) == dict(base.location(2), region=[.1, .2, .8, .9])
    assert crop.source_hash == receipts[0]["sha256"]
    assert crop.coverage_keys() == [] and base.coverage_keys()
    assert base.to_dict() == before
    restored = tmp_path / "new-workspace"
    shutil.copytree(root, restored)
    loaded = load_pdf_materials(restored, crop.to_dict(), expected_manifest_hash=crop.manifest_hash)
    assert loaded.location(2) == crop.location(2) and loaded.source_hash == base.source_hash


@pytest.mark.parametrize("suffix", ["docx", "pptx"])
@pytest.mark.parametrize("changed", ["original", "working-copy"])
def test_office_source_tampering_rejects_region_prepare_verify_and_load(
        office_env, suffix, changed, monkeypatch):
    root, _, _, _, calls = office_env
    task, receipts = setup_office(office_env, (suffix,))
    _, _, base, _, _ = prepare_office(office_env, task, receipts[0])
    crop = prepare_pdf_region(root, base, 2, [.1, .2, .8, .9], max_pixels=10000)
    artifacts = [root / crop.region_supplement["png_relpath"], root / crop.segments[0].material_relpath]
    before = [path.read_bytes() for path in artifacts]
    engine_calls = len(calls)
    relative = (base.source_relpath if changed == "original"
                else base.derived_source["office_manifest"]["original_relpath"])
    assert relative != base.original_relpath
    path = root / relative
    path.write_bytes(path.read_bytes() + b"\nchanged-office-source")
    monkeypatch.setattr(pdfium.PdfPage, "render", lambda *a, **k: pytest.fail("Office 原件失效后不得渲染"))

    code = "material_tampered" if changed == "original" else "source_changed"
    error(code, verify_pdf_materials, root, crop)
    error(code, load_pdf_materials, root, crop.to_dict(), expected_manifest_hash=crop.manifest_hash)
    error(code, prepare_pdf_region, root, base, 2, [.1, .2, .8, .9], max_pixels=10000)
    assert [path.read_bytes() for path in artifacts] == before
    assert len(calls) == engine_calls


@pytest.mark.parametrize("suffix", ["docx", "pptx"])
def test_office_mapping_tampering_rejected_even_with_reanchored_region_manifest(office_env, suffix, monkeypatch):
    root, _, _, _, _ = office_env
    task, receipts = setup_office(office_env, (suffix,))
    _, _, base, _, _ = prepare_office(office_env, task, receipts[0])
    crop = prepare_pdf_region(root, base, 2, [.1, .2, .8, .9], max_pixels=10000)
    derived = deepcopy(crop.derived_source)
    office = derived["office_manifest"]
    location = office["page_map"][1]
    if suffix == "docx":
        location["reading_copy_page"] = 1
    else:
        location["source_slide_id"] = "forged-slide-id"
    office["manifest_hash"] = _digest({key: value for key, value in office.items() if key != "manifest_hash"})
    derived["office_manifest_hash"] = office["manifest_hash"]
    altered = replace(crop, derived_source=derived)
    monkeypatch.setattr(pdfium.PdfPage, "render", lambda *a, **k: pytest.fail("Office 映射失效后不得渲染"))

    error("invalid_manifest", verify_pdf_materials, root, altered)
    error("invalid_manifest", load_pdf_materials, root, altered.to_dict(),
          expected_manifest_hash=altered.manifest_hash)
