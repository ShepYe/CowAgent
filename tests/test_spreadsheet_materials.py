"""真实 openpyxl 包及手工 OOXML 变体；不替换关系解析或文件写入。"""
from dataclasses import replace
import json
import shutil
import socket
import subprocess
import xml.etree.ElementTree as ET
import zipfile

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.drawing.image import Image as XLImage
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, OneCellAnchor, TwoCellAnchor
from openpyxl.drawing.xdr import XDRPositiveSize2D
from PIL import Image
import pytest

from agent.knowledge import spreadsheet_materials as sm
from agent.knowledge.spreadsheet_materials import (
    SpreadsheetBudget, SpreadsheetMaterialError, load_spreadsheet_materials,
    prepare_spreadsheet_materials, verify_spreadsheet_materials,
)


def patch(path, transform):
    with zipfile.ZipFile(path) as z:
        entries = {name: z.read(name) for name in z.namelist()}
    transform(entries)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)


@pytest.fixture
def workbook(tmp_path):
    first = tmp_path / "red.png"
    second = tmp_path / "blue.png"
    Image.new("RGB", (12, 8), "red").save(first)
    Image.new("RGB", (5, 9), "blue").save(second)
    book = Workbook()
    sheet = book.active
    sheet.title = "预算"
    sheet["A1"], sheet["A2"] = 6, 3
    sheet["B1"] = "=A1+A2"
    sheet["B2"] = "=12345+67890"
    picture = XLImage(first)
    picture.anchor = TwoCellAnchor(_from=AnchorMarker(col=1, row=2, colOff=123, rowOff=456),
                                   to=AnchorMarker(col=3, row=6, colOff=789, rowOff=901), editAs="oneCell")
    sheet.add_image(picture)
    picture = XLImage(second)
    picture.anchor = OneCellAnchor(_from=AnchorMarker(col=5, row=8, colOff=55, rowOff=66),
                                   ext=XDRPositiveSize2D(cx=123456, cy=654321))
    sheet.add_image(picture)
    chart = BarChart()
    chart.add_data(Reference(sheet, min_col=1, min_row=1, max_row=2))
    sheet.add_chart(chart, "H3")
    book.create_sheet("复用").add_image(XLImage(first), "C4")
    path = tmp_path / "source.xlsx"
    book.save(path)
    book.close()

    def cached(entries):
        part = "xl/worksheets/sheet1.xml"
        tree = ET.fromstring(entries[part])
        tree.find(".//s:c[@r='B1']/s:v", sm.NS).text = "999"  # 故意过期，不能重算为 9。
        entries[part] = ET.tostring(tree)
        part = "xl/charts/chart1.xml"
        tree = ET.fromstring(entries[part])
        ref = tree.find(".//c:numRef", sm.NS)
        cache = ET.SubElement(ref, f"{{{sm.C}}}numCache")
        ET.SubElement(cache, f"{{{sm.C}}}formatCode").text = "General"
        ET.SubElement(cache, f"{{{sm.C}}}ptCount", val="2")
        for index, value in enumerate(("80", "81")):
            point = ET.SubElement(cache, f"{{{sm.C}}}pt", idx=str(index))
            ET.SubElement(point, f"{{{sm.C}}}v").text = value
        entries[part] = ET.tostring(tree)
        # 同一源图片关系跨 sheet 引用，不能依赖 xl/media 文件名枚举定位。
        part = "xl/drawings/_rels/drawing2.xml.rels"
        tree = ET.fromstring(entries[part])
        tree[0].set("Target", "../media/image1.png")
        entries[part] = ET.tostring(tree)
        del entries["xl/media/image3.png"]
    patch(path, cached)
    return path


def codes(result):
    return {gap["code"] for gap in result.coverage_gaps}


def test_multiple_images_shared_across_sheets_and_exact_anchors(workbook):
    root = workbook.parent
    before = workbook.read_bytes()
    result = prepare_spreadsheet_materials(root, workbook.name)
    assert len(result.images) == 3 and len(result.charts) == 1
    red = [i for i in result.images if i["source_part"] == "xl/media/image1.png"]
    assert {i["worksheet"] for i in red} == {"预算", "复用"}
    assert len({i["material_relpath"] for i in red}) == 1
    assert len({i["drawing_part"] for i in red}) == 2
    assert all(i["object_id"] and i["drawing_id"] and i["worksheet_part"] for i in result.images)
    item = next(i for i in red if i["worksheet"] == "预算")
    assert item["anchor"]["from"] == dict(row=2, column=1, row_offset_emu=456, column_offset_emu=123, cell="B3")
    assert item["anchor"]["to"] == dict(row=6, column=3, row_offset_emu=901, column_offset_emu=789, cell="D7")
    blue = next(i for i in result.images if i["source_part"] != "xl/media/image1.png")
    assert blue["anchor"]["to"] is None
    assert blue["anchor"]["extent_emu"] == dict(cx=123456, cy=654321)
    assert blue["anchor"]["from"]["cell"] == "F9"
    with zipfile.ZipFile(workbook) as z:
        for item in result.images:
            payload = (root / item["material_relpath"]).read_bytes()
            assert payload == z.read(item["source_part"])
            assert sm._hash(payload) == item["content_hash"]
    assert workbook.read_bytes() == before
    assert prepare_spreadsheet_materials(root, workbook.name) == result
    assert str(root) not in json.dumps(result.to_dict())


def test_chart_caches_are_source_structure_never_recalculated(workbook, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("离线材料不得调用计算器或网络")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    before = workbook.read_bytes()
    result = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    chart = result.charts[0]
    assert chart["anchor"]["from"]["cell"] == "H3" and chart["rendered"] is False
    assert chart["cache_freshness"] == "unverified"
    assert chart["series"][0]["references"] == ["'预算'!$A$1:$A$2"]
    cache = chart["series"][0]["caches"][0]
    assert [n["children"][0]["text"] for n in cache["children"] if n["tag"].endswith("}pt")] == ["80", "81"]
    calc = result.calculation
    assert calc["recalculated"] is False and calc["formula_cells"] == 2 and calc["missing_formula_caches"] == 1
    assert {"chart_not_rendered", "formula_and_chart_cache_freshness_unverified", "formula_cache_missing_not_recalculated"} <= codes(result)
    with zipfile.ZipFile(workbook) as z:
        tree = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        assert tree.findtext(".//s:c[@r='B1']/s:v", namespaces=sm.NS) == "999"
        assert tree.findtext(".//s:c[@r='B2']/s:f", namespaces=sm.NS) == "12345+67890"
    assert workbook.read_bytes() == before


def test_missing_anchor_unsupported_emf_external_link_and_orphan(workbook):
    def modify(entries):
        part = "xl/drawings/drawing1.xml"
        tree = ET.fromstring(entries[part])
        anchor = next(n for n in tree if n.find("xdr:pic", sm.NS) is not None)
        anchor.remove(anchor.find("xdr:from", sm.NS))
        anchor.tag = f"{{{sm.XDR}}}absoluteAnchor"
        ET.SubElement(anchor, f"{{{sm.XDR}}}pos", x="100", y="200")
        entries[part] = ET.tostring(tree)
        part = "xl/drawings/_rels/drawing1.xml.rels"
        tree = ET.fromstring(entries[part])
        rel = next(n for n in tree if n.get("Target").endswith("image1.png"))
        rel.set("Target", "../media/vector.emf")
        ET.SubElement(tree, f"{{{sm.PKG}}}Relationship", Id="external", Target="https://example.invalid/book.xlsx",
                      Type=sm.R + "/externalLink", TargetMode="External")
        entries[part] = ET.tostring(tree)
        entries["xl/media/vector.emf"] = b"unsupported-emf-bytes"
        entries["xl/media/orphan.png"] = entries["xl/media/image1.png"]
    patch(workbook, modify)
    result = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    assert {"cell_anchor_missing", "unsupported_image_format", "external_relationship_not_accessed",
            "unreferenced_media_or_chart_no_anchor"} <= codes(result)
    emf = next(i for i in result.images if i["source_part"].endswith(".emf"))
    assert emf["material_relpath"] is None and emf["anchor"]["position_emu"] == dict(x=100, y=200)
    assert emf["anchor"]["from"] is None
    orphan = next(i for i in result.images if i["source_part"].endswith("orphan.png"))
    assert orphan["anchor"] is None and orphan["worksheet"] is None
    assert (workbook.parent / orphan["material_relpath"]).is_file()


def test_chart_missing_cache_explicit(workbook):
    def modify(entries):
        part = "xl/charts/chart1.xml"
        tree = ET.fromstring(entries[part])
        ref = tree.find(".//c:numRef", sm.NS)
        ref.remove(ref.find("c:numCache", sm.NS))
        entries[part] = ET.tostring(tree)
    patch(workbook, modify)
    assert "chart_cache_missing" in codes(prepare_spreadsheet_materials(workbook.parent, workbook.name))


def test_restore_new_root_and_tampering(workbook, tmp_path):
    result = prepare_spreadsheet_materials(tmp_path, workbook.name)
    restored = tmp_path / "restore"
    restored.mkdir()
    shutil.copyfile(workbook, restored / workbook.name)
    shutil.copytree(tmp_path / "spreadsheet_materials", restored / "spreadsheet_materials")
    assert load_spreadsheet_materials(restored, result.to_dict(), expected_manifest_hash=result.manifest_hash) == result
    wrong = result.to_dict()
    wrong["images"][0]["anchor"]["from"]["row"] = 700
    with pytest.raises(SpreadsheetMaterialError, match="hash"):
        load_spreadsheet_materials(restored, wrong, expected_manifest_hash=result.manifest_hash)
    forged = replace(result, images=tuple(wrong["images"]))
    with pytest.raises(SpreadsheetMaterialError) as error:
        load_spreadsheet_materials(restored, forged.to_dict(), expected_manifest_hash=forged.manifest_hash)
    assert error.value.code == "invalid_manifest"
    (restored / result.images[0]["material_relpath"]).write_bytes(b"changed")
    with pytest.raises(SpreadsheetMaterialError) as error:
        verify_spreadsheet_materials(restored, result)
    assert error.value.code == "material_tampered"
    with pytest.raises(SpreadsheetMaterialError) as error:
        prepare_spreadsheet_materials(restored, workbook.name)
    assert error.value.code == "cache_invalid"
    workbook.write_bytes(b"changed")
    with pytest.raises(SpreadsheetMaterialError) as error:
        verify_spreadsheet_materials(tmp_path, result)
    assert error.value.code == "source_changed"


@pytest.mark.parametrize("field,limit", [("max_source_bytes", 10), ("max_package_bytes", 10),
    ("max_parts", 2), ("max_part_bytes", 20), ("max_objects", 2), ("max_material_bytes", 5), ("max_metadata_bytes", 100)])
def test_budget_no_partial_manifest(workbook, field, limit):
    with pytest.raises(SpreadsheetMaterialError) as error:
        prepare_spreadsheet_materials(workbook.parent, workbook.name, budget=replace(SpreadsheetBudget(), **{field: limit}))
    assert error.value.code == "budget_exceeded"
    assert not list(workbook.parent.glob("spreadsheet_materials/**/manifest.json"))


@pytest.mark.parametrize("relative", ["../source.xlsx", "C:/source.xlsx", "/source.xlsx", "a\\source.xlsx", "a/../source.xlsx"])
def test_path_rejected(tmp_path, relative):
    with pytest.raises(SpreadsheetMaterialError) as error:
        prepare_spreadsheet_materials(tmp_path, relative)
    assert error.value.code == "invalid_path"


@pytest.mark.parametrize("target", ["../../../../outside.png", "C:/outside.png", "file:///etc/passwd", "%2e%2e/%2e%2e/%2e%2e/outside", "..\\media\\image1.png"])
def test_malicious_relationship_cannot_escape(workbook, target):
    def modify(entries):
        part = "xl/drawings/_rels/drawing1.xml.rels"
        tree = ET.fromstring(entries[part])
        tree[0].set("Target", target)
        entries[part] = ET.tostring(tree)
    patch(workbook, modify)
    with pytest.raises(SpreadsheetMaterialError) as error:
        prepare_spreadsheet_materials(workbook.parent, workbook.name)
    assert error.value.code == "invalid_path"
    assert not (workbook.parent / "spreadsheet_materials").exists()


@pytest.mark.parametrize("fault", ["xml", "missing", "dtd", "zip_path", "duplicate_id", "image_signature", "not_zip"])
def test_bad_package_fails(workbook, fault):
    def modify(entries):
        if fault == "xml":
            entries["xl/drawings/drawing1.xml"] = b"<broken"
        elif fault == "missing":
            del entries["xl/media/image1.png"]
        elif fault == "dtd":
            entries["xl/evil.xml"] = b'<!DOCTYPE x [<!ENTITY a "bad">]><x>&a;</x>'
        elif fault == "zip_path":
            entries["../../outside.txt"] = b"bad"
        elif fault == "duplicate_id":
            part = "xl/drawings/_rels/drawing1.xml.rels"
            tree = ET.fromstring(entries[part])
            tree[1].set("Id", tree[0].get("Id"))
            entries[part] = ET.tostring(tree)
        elif fault == "image_signature":
            entries["xl/media/image1.png"] = b"not-a-png"
    patch(workbook, modify)
    if fault == "not_zip":
        workbook.write_bytes(b"not zip")
    with pytest.raises(SpreadsheetMaterialError) as error:
        prepare_spreadsheet_materials(workbook.parent, workbook.name)
    assert error.value.code in {"invalid_path", "invalid_package"}
    assert not (workbook.parent / "spreadsheet_materials").exists()


def test_unknown_version_and_invalid_budgets(workbook):
    for budget in (False, {}, replace(SpreadsheetBudget(), max_objects=True), replace(SpreadsheetBudget(), max_parts=0)):
        with pytest.raises(SpreadsheetMaterialError) as error:
            prepare_spreadsheet_materials(workbook.parent, workbook.name, budget=budget)
        assert error.value.code == "invalid_request"
    result = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    with pytest.raises(SpreadsheetMaterialError) as error:
        verify_spreadsheet_materials(workbook.parent, replace(result, preparation_version="future"))
    assert error.value.code == "invalid_manifest"


def test_signed_emu_offsets_are_preserved(workbook):
    def modify(entries):
        part = "xl/drawings/drawing1.xml"
        tree = ET.fromstring(entries[part])
        for marker in tree.findall(".//xdr:from", sm.NS):
            marker.find("xdr:colOff", sm.NS).text = "-123"
            marker.find("xdr:rowOff", sm.NS).text = "-456"
        entries[part] = ET.tostring(tree)
    patch(workbook, modify)
    result = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    for item in result.images + result.charts:
        if item["drawing_part"] == "xl/drawings/drawing1.xml":
            assert item["anchor"]["from"]["column_offset_emu"] == -123
            assert item["anchor"]["from"]["row_offset_emu"] == -456


def test_source_change_during_preparation_not_published(workbook, monkeypatch):
    original_write = sm.Path.write_text

    def write(path, *args, **kwargs):
        result = original_write(path, *args, **kwargs)
        if path.name == "manifest.json":
            workbook.write_bytes(b"concurrent edit")
        return result

    monkeypatch.setattr(sm.Path, "write_text", write)
    with pytest.raises(SpreadsheetMaterialError) as error:
        prepare_spreadsheet_materials(workbook.parent, workbook.name)
    assert error.value.code == "source_changed"
    assert not list(workbook.parent.glob("spreadsheet_materials/**/manifest.json"))
    assert not list(workbook.parent.glob("spreadsheet_materials/.xlsx-work-*"))


def test_symlink_material_escape_rejected(workbook, tmp_path):
    result = prepare_spreadsheet_materials(tmp_path, workbook.name)
    material = tmp_path / result.images[0]["material_relpath"]
    outside = tmp_path.parent / (tmp_path.name + "-outside-image")
    outside.write_bytes(material.read_bytes())
    material.unlink()
    try:
        material.symlink_to(outside)
    except OSError:
        pytest.skip("当前 Windows 用户无符号链接权限")
    with pytest.raises(SpreadsheetMaterialError) as error:
        verify_spreadsheet_materials(tmp_path, result)
    assert error.value.code == "invalid_path"
    assert outside.read_bytes()


def test_duplicate_zip_member_refused(workbook):
    with zipfile.ZipFile(workbook, "a") as z:
        with pytest.warns(UserWarning, match="Duplicate name"):
            z.writestr("xl/workbook.xml", z.read("xl/workbook.xml"))
    with pytest.raises(SpreadsheetMaterialError) as error:
        prepare_spreadsheet_materials(workbook.parent, workbook.name)
    assert error.value.code == "invalid_package"


def test_plain_workbook_without_drawings(tmp_path):
    book = Workbook()
    book.active["A1"] = 42
    book.save(tmp_path / "plain.xlsx")
    book.close()
    result = prepare_spreadsheet_materials(tmp_path, "plain.xlsx")
    assert not result.images and not result.charts and result.calculation["formula_cells"] == 0
    assert load_spreadsheet_materials(tmp_path, result.to_dict(), expected_manifest_hash=result.manifest_hash) == result


def test_grouped_images_keep_leaf_ids_and_transform_gap(workbook):
    def modify(entries):
        part = "xl/drawings/drawing1.xml"
        tree = ET.fromstring(entries[part])
        anchor = next(n for n in tree if n.find("xdr:pic", sm.NS) is not None)
        picture = anchor.find("xdr:pic", sm.NS)
        anchor.remove(picture)
        group = ET.SubElement(anchor, f"{{{sm.XDR}}}grpSp")
        props = ET.SubElement(group, f"{{{sm.XDR}}}nvGrpSpPr")
        ET.SubElement(props, f"{{{sm.XDR}}}cNvPr", id="900", name="group")
        group.append(picture)
        entries[part] = ET.tostring(tree)
    patch(workbook, modify)
    result = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    assert len(result.images) == 3
    assert all(i["object_id"] != "900" for i in result.images)
    assert "group_transform_not_applied" in codes(result)
    assert any(i["object_path"].count("/*[") == 3 for i in result.images)


def test_unanchored_chart_cache_is_still_listed(workbook):
    def modify(entries):
        entries["xl/charts/orphan.xml"] = entries["xl/charts/chart1.xml"]
    patch(workbook, modify)
    result = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    chart = next(c for c in result.charts if c["source_part"].endswith("orphan.xml"))
    assert chart["anchor"] is None and chart["worksheet"] is None
    assert chart["series"][0]["caches"] and chart["rendered"] is False
    assert (workbook.parent / chart["material_relpath"]).is_file()
    assert any(g["code"] == "unreferenced_media_or_chart_no_anchor" and
               g["location"]["source_part"].endswith("orphan.xml") for g in result.coverage_gaps)
