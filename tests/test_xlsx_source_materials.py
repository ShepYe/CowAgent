"""XLSX 原件结构与受信嵌图清单；真实包、不联网、不渲染、不重算。"""
from copy import deepcopy
from dataclasses import replace
import json
import shutil
import socket
import subprocess
import xml.etree.ElementTree as ET

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.worksheet.formula import ArrayFormula
import pytest

from agent.knowledge import parser
from agent.knowledge.parser import (
    SourceError, parse_source, parse_spreadsheet_source,
    read_spreadsheet_object, spreadsheet_object_location,
)
from agent.knowledge.personal_assistant import SourceService
from agent.knowledge import spreadsheet_materials as sm
from agent.knowledge.spreadsheet_materials import prepare_spreadsheet_materials
from tests.test_spreadsheet_materials import patch, workbook as workbook


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("结构解析不得执行进程、重算或访问网络")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def enhanced(path, materials, *, root=None, **kwargs):
    return parse_spreadsheet_source(path, "预算.xlsx", materials_root=root or path.parent,
                                    materials=materials, **kwargs)


def tree_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_legacy_identity_units_and_stale_formula_caches_unchanged(workbook):
    before = parse_source(workbook, prepare_only=True)
    materials = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    enhanced_result = enhanced(workbook, materials)
    assert parse_source(workbook, prepare_only=True) == before
    assert before["config"] == dict(max_bytes=10485760, max_units=200000,
                                    max_pages=2000, preparation="native-pdf-v1")
    assert enhanced_result["parser_id"] == before["parser_id"] == "personal-source"
    assert enhanced_result["parser_version"] == before["parser_version"] == "2"
    assert enhanced_result["units"] == before["units"]
    assert enhanced_result["config"]["xlsx_structure"] == "xlsx-embedded-structure-v1"
    assert enhanced_result["config"]["spreadsheet_manifest_hash"] == materials.manifest_hash
    units = {u["location"]["cell"]: u for u in enhanced_result["units"]}
    assert units["B1"] == dict(text="公式 =A1+A2；缓存值：999", formula="=A1+A2", cached_value=999,
                               location=dict(kind="xlsx", sheet="预算", cell="B1"))
    assert units["B2"]["formula"] == "=12345+67890" and units["B2"]["cached_value"] is None
    assert "无可用值（未重算）" in units["B2"]["text"]
    assert enhanced_result["spreadsheet_calculation"]["recalculated"] is False
    assert enhanced_result["spreadsheet_calculation"]["external_links_refreshed"] is False
    assert enhanced_result["spreadsheet_calculation"]["cache_freshness"] == "unverified"


def test_images_keep_each_reference_exact_anchor_and_readback(workbook):
    materials = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    before = tree_bytes(workbook.parent)
    result = enhanced(workbook, materials)
    images = [c for c in result["visual_coverage"] if c["kind"] == "image"]
    assert len(images) == 3
    red = [c for c in images if c["source_part"] == "xl/media/image1.png"]
    assert {c["location"]["sheet"] for c in red} == {"预算", "复用"}
    assert len({c["material_relpath"] for c in red}) == 1
    assert len({c["location"]["drawing_part"] for c in red}) == 2
    item = next(c for c in red if c["location"]["sheet"] == "预算")
    location = item["location"]
    assert location["kind"] == "xlsx" and location["cell"] == "B3"
    assert location["worksheet_id"] == "1" and location["worksheet_part"] == "xl/worksheets/sheet1.xml"
    assert location["drawing_id"] and location["object_id"] and location["object_path"]
    assert location["relationship_id"] and location["reference_index"] == 1
    assert location["anchor"]["from"] == dict(row=2, column=1, row_offset_emu=456,
                                              column_offset_emu=123, cell="B3")
    assert location["anchor"]["to"]["cell"] == "D7"
    assert location["anchor"]["to"]["row_offset_emu"] == 901
    answer = read_spreadsheet_object(workbook.parent, materials, json.loads(json.dumps(location)))
    assert answer["object_kind"] == "image" and answer["original_hash"] == materials.original_hash
    assert answer["manifest_hash"] == materials.manifest_hash
    assert answer["source_object"]["source_part"] == location["source_part"]
    assert all(c["status"] == "pending" and c["rendered"] is False and c["ocr"] is False
               and c["visual_interpretation"] is False for c in images)
    assert tree_bytes(workbook.parent) == before


def test_chart_is_unrendered_source_cache_never_body_fact(workbook):
    materials = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    result = enhanced(workbook, materials)
    chart = next(c for c in result["visual_coverage"] if c["kind"] == "chart")
    assert chart["location"]["cell"] == "H3" and chart["rendered"] is False
    assert chart["unprocessed_reason"] == "chart_not_rendered" and chart["cache_freshness"] == "unverified"
    assert chart["series"][0]["references"] == ["'预算'!$A$1:$A$2"]
    cache = chart["series"][0]["caches"][0]
    assert [p["children"][0]["text"] for p in cache["children"] if p["tag"].endswith("}pt")] == ["80", "81"]
    gap = next(g for g in result["visual_warnings"] if g["code"] == "chart_not_rendered")
    assert gap["status"] == "pending" and gap["location"]["cell"] == "H3"
    assert gap["location"]["source_part"] == "xl/charts/chart1.xml"
    assert all(g["reason"] != "visual_not_supported" for g in result["visual_warnings"])
    assert not any(u.get("type") in ("image", "chart", "visual_interpretation") for u in result["units"])
    assert read_spreadsheet_object(workbook.parent, materials, chart["location"])["source_object"]["series"] == chart["series"]


def test_zero_false_and_array_formula_caches_preserved(tmp_path):
    book = Workbook()
    sheet = book.active
    sheet["A1"] = "=0"
    sheet["B1"] = "=FALSE()"
    sheet["C1"] = ArrayFormula("C1:C3", "=SUM(D1:D3*E1:E3)")
    sheet["D1"], sheet["E1"] = 0, False
    path = tmp_path / "formulas.xlsx"
    book.save(path)
    book.close()

    def cache(entries):
        part = "xl/worksheets/sheet1.xml"
        tree = ET.fromstring(entries[part])
        tree.find(".//s:c[@r='A1']/s:v", sm.NS).text = "0"
        node = tree.find(".//s:c[@r='B1']", sm.NS)
        node.set("t", "b")
        node.find("s:v", sm.NS).text = "0"
        entries[part] = ET.tostring(tree)
    patch(path, cache)
    materials = prepare_spreadsheet_materials(tmp_path, path.name)
    result = enhanced(path, materials)
    units = {u["location"]["cell"]: u for u in result["units"]}
    assert units["A1"]["cached_value"] == 0 and "缓存值：0" in units["A1"]["text"]
    assert units["B1"]["cached_value"] is False and "缓存值：False" in units["B1"]["text"]
    assert units["C1"]["formula"] == "=SUM(D1:D3*E1:E3)" and units["C1"]["cached_value"] is None
    assert "ArrayFormula object" not in units["C1"]["text"]
    assert units["D1"]["text"] == "0" and units["E1"]["text"] == "False"


def test_image_only_workbook_pending_without_invented_text(workbook):
    path = workbook.parent / "pictures.xlsx"
    book = Workbook()
    book.active.add_image(XLImage(workbook.parent / "red.png"), "E11")
    book.save(path)
    book.close()
    materials = prepare_spreadsheet_materials(path.parent, path.name)
    result = enhanced(path, materials)
    assert result["units"] == []
    assert result["visual_coverage"][0]["location"]["cell"] == "E11"
    assert result["visual_coverage"][0]["unprocessed_reason"] == "native_reading_pending"


def test_absolute_anchor_orphan_and_external_gaps_do_not_fake_cells(workbook):
    def modify(entries):
        part = "xl/drawings/drawing1.xml"
        tree = ET.fromstring(entries[part])
        anchor = next(n for n in tree if n.find("xdr:pic", sm.NS) is not None)
        anchor.remove(anchor.find("xdr:from", sm.NS))
        anchor.tag = f"{{{sm.XDR}}}absoluteAnchor"
        ET.SubElement(anchor, f"{{{sm.XDR}}}pos", x="100", y="200")
        entries[part] = ET.tostring(tree)
        entries["xl/media/orphan.png"] = entries["xl/media/image1.png"]
        part = "xl/drawings/_rels/drawing1.xml.rels"
        tree = ET.fromstring(entries[part])
        ET.SubElement(tree, f"{{{sm.PKG}}}Relationship", Id="remote", Target="https://example.invalid/image.png",
                      Type=sm.R + "/image", TargetMode="External")
        entries[part] = ET.tostring(tree)
    patch(workbook, modify)
    materials = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    result = enhanced(workbook, materials)
    orphan = next(c for c in result["visual_coverage"] if c["source_part"].endswith("orphan.png"))
    assert "cell" not in orphan["location"] and "sheet" not in orphan["location"]
    assert orphan["location"]["anchor"] is None
    absolute = next(c for c in result["visual_coverage"] if (c["location"].get("anchor") or {}).get("kind") == "absoluteAnchor")
    assert "cell" not in absolute["location"]
    assert absolute["location"]["anchor"]["position_emu"] == dict(x=100, y=200)
    gaps = {g["code"]: g for g in result["visual_warnings"]}
    assert {"cell_anchor_missing", "unreferenced_media_or_chart_no_anchor", "external_relationship_not_accessed"} <= gaps.keys()
    assert "cell" not in gaps["cell_anchor_missing"]["location"]
    assert read_spreadsheet_object(workbook.parent, materials, orphan["location"])["source_object"]["worksheet"] is None


def test_extensionless_blob_and_new_root_restore(workbook):
    materials = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    first = enhanced(workbook, materials)
    restored = workbook.parent / "restored"
    restored.mkdir()
    shutil.copyfile(workbook, restored / workbook.name)
    shutil.copytree(workbook.parent / "spreadsheet_materials", restored / "spreadsheet_materials")
    blob = restored / materials.original_hash
    shutil.copyfile(workbook, blob)
    result = enhanced(blob, materials, root=restored)
    assert result == first
    assert str(restored) not in json.dumps(result)
    location = result["visual_coverage"][0]["location"]
    assert read_spreadsheet_object(restored, materials, location)["original_hash"] == materials.original_hash


@pytest.mark.parametrize("fault", ["source", "material", "manifest", "missing", "parser_blob"])
def test_untrusted_or_changed_source_material_rejected(workbook, fault):
    materials = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    target = workbook
    if fault == "source":
        workbook.write_bytes(b"changed")
    elif fault == "material":
        (workbook.parent / materials.images[0]["material_relpath"]).write_bytes(b"changed")
    elif fault == "missing":
        (workbook.parent / materials.images[0]["material_relpath"]).unlink()
    elif fault == "manifest":
        images = deepcopy(materials.images)
        images[0]["anchor"]["from"]["cell"] = "Z999"
        materials = replace(materials, images=images)
    else:
        target = workbook.parent / "otherblob"
        target.write_bytes(b"different source")
    with pytest.raises(SourceError) as error:
        enhanced(target, materials)
    assert error.value.code == "storage_failed"


@pytest.mark.parametrize("kwargs", [{"max_bytes": 3}, {"max_units": 1}])
def test_cell_text_budget_still_fails_without_partial_result(workbook, kwargs):
    materials = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    with pytest.raises(SourceError) as error:
        enhanced(workbook, materials, **kwargs)
    assert error.value.code == "limit_exceeded"


def test_invalid_format_and_forged_readback_location_rejected(workbook):
    materials = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    with pytest.raises(SourceError) as error:
        parse_spreadsheet_source(workbook, "source.pdf", materials_root=workbook.parent, materials=materials)
    assert error.value.code == "invalid_request"
    location = spreadsheet_object_location(materials.images[0])
    location["cell"] = "Z999"
    with pytest.raises(SourceError) as error:
        read_spreadsheet_object(workbook.parent, materials, location)
    assert error.value.code == "invalid_request"


def test_returned_locations_and_metadata_do_not_mutate_trusted_manifest(workbook):
    materials = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    original = materials.to_dict()
    result = enhanced(workbook, materials)
    location = result["visual_coverage"][0]["location"]
    readback = read_spreadsheet_object(workbook.parent, materials, location)
    result["visual_coverage"][0]["anchor"]["from"]["cell"] = "Z1"
    location["anchor"]["from"]["cell"] = "Z2"
    result["spreadsheet_calculation"]["recalculated"] = True
    readback["source_object"]["anchor"]["from"]["cell"] = "Z3"
    assert materials.to_dict() == original


def test_source_service_legacy_parse_id_and_formula_reference_still_readable(workbook):
    service = SourceService(workbook.parent / "workspace")
    receipt = service.receive(workbook, filename="预算.xlsx", request_id="xlsx", origin={"channel": "test"})
    service.change(receipt["document_id"], purpose="long_term", expected_state=0, reason="结构回读")
    parsed = service.parse(receipt["revision_id"])
    materials = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    enhanced(workbook, materials)
    assert service.parse(receipt["revision_id"])["parse_id"] == parsed["parse_id"]
    candidate = service.prepare(receipt["revision_id"])
    published = service.publish(candidate["candidate_id"])
    ordinal = next(i for i, u in enumerate(parsed["units"]) if u.get("formula") == "=A1+A2")
    reference = service.read_reference(published["publish_id"], ordinal)
    assert reference["valid"] and reference["location"] == dict(kind="xlsx", sheet="预算", cell="B1")
    assert "999" in reference["text"]


def test_changed_parser_blob_during_parse_rejected(workbook, monkeypatch):
    materials = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    original = parser.parse_source
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        workbook.write_bytes(b"changed after text parse")
        return result
    monkeypatch.setattr(parser, "parse_source", changed)
    with pytest.raises(SourceError) as error:
        enhanced(workbook, materials)
    assert error.value.code == "storage_failed"


def test_source_service_explicit_material_parse_persisted_and_default_id_retained(workbook):
    service = SourceService(workbook.parent / "workspace")
    receipt = service.receive(workbook, filename="预算.xlsx", request_id="xlsx", origin={"channel": "test"})
    legacy = service.parse(receipt["revision_id"], prepare_only=True)
    materials = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    parsed = service.parse(receipt["revision_id"], prepare_only=True,
                           spreadsheet_materials=materials, materials_root=workbook.parent)
    assert parsed["parse_id"] != legacy["parse_id"] and parsed["units"] == legacy["units"]
    assert parsed["sha256"] == receipt["sha256"] == materials.original_hash
    assert parsed["source_identity"] == legacy["source_identity"]
    stored = service.root / "parses" / (parsed["parse_id"] + ".json")
    assert json.loads(stored.read_text(encoding="utf-8")) == parsed
    assert service.parse(receipt["revision_id"], prepare_only=True)["parse_id"] == legacy["parse_id"]
    assert service.parse(receipt["revision_id"], prepare_only=True, spreadsheet_materials=materials,
                         materials_root=workbook.parent) == parsed
    restored_service = SourceService(service.workspace)
    assert restored_service.parse(receipt["revision_id"], base_parse_id=parsed["parse_id"]) == parsed


@pytest.mark.parametrize("mode", ["no_root", "no_materials", "base_conflict", "wrong_format"])
def test_source_service_explicit_binding_rejects_incomplete_or_mixed_request(workbook, mode):
    service = SourceService(workbook.parent / "workspace")
    if mode == "wrong_format":
        path = workbook.parent / "text.txt"
        path.write_text("plain text", encoding="utf-8")
    else:
        path = workbook
    receipt = service.receive(path, filename=path.name, request_id="source", origin={"channel": "test"})
    materials = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    kwargs = dict(spreadsheet_materials=materials, materials_root=workbook.parent)
    if mode == "no_root":
        kwargs.pop("materials_root")
    elif mode == "no_materials":
        kwargs.pop("spreadsheet_materials")
    elif mode == "base_conflict":
        kwargs["base_parse_id"] = service.parse(receipt["revision_id"])["parse_id"]
    with pytest.raises(SourceError) as error:
        service.parse(receipt["revision_id"], **kwargs)
    assert error.value.code == "invalid_request"


def test_source_service_materials_of_other_original_not_persisted(workbook):
    other = workbook.parent / "other.xlsx"
    book = Workbook()
    book.active["A1"] = "different source"
    book.save(other)
    book.close()
    service = SourceService(workbook.parent / "workspace")
    receipt = service.receive(other, filename=other.name, request_id="other", origin={"channel": "test"})
    materials = prepare_spreadsheet_materials(workbook.parent, workbook.name)
    with pytest.raises(SourceError) as error:
        service.parse(receipt["revision_id"], spreadsheet_materials=materials, materials_root=workbook.parent)
    assert error.value.code == "storage_failed"
    assert not list((service.root / "parses").glob("*.json"))
