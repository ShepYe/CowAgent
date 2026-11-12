"""受控 PPTX 结构解析：真实 OOXML、无模型／渲染、稳定源位置。"""
from copy import deepcopy
from io import BytesIO
import json
import socket
import subprocess
import sys
import warnings
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import pytest
from PIL import Image
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.util import Inches

from agent.knowledge.parser import PARSER_ID, PARSER_VERSION, SourceError, parse_source
from agent.knowledge.personal_assistant import SourceService


P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **kw: pytest.fail("结构解析不得出网"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("结构解析不得启动转换进程"))
    for name in ("agent.knowledge.visual", "fitz", "pytesseract"):
        monkeypatch.setitem(sys.modules, name, None)


def image():
    stream = BytesIO()
    Image.new("RGB", (30, 30), "blue").save(stream, "PNG")
    stream.seek(0)
    return stream


def rewrite(path, transform):
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    transform(parts)
    with ZipFile(path, "w") as archive:
        # ZIP 部件顺序也与源幻灯片顺序不同，不能依赖名字或遍历顺序。
        for name, content in reversed(list(parts.items())):
            archive.writestr(name, content)


@pytest.fixture
def deck(tmp_path):
    presentation = Presentation()
    first = presentation.slides.add_slide(presentation.slide_layouts[6])
    outer = first.shapes.add_group_shape()
    nested = outer.shapes.add_group_shape()
    box = nested.shapes.add_textbox(0, 0, Inches(3), Inches(1))
    box.text_frame.paragraphs[0].text = ""
    box.text_frame.add_paragraph().text = "组合内的中文正文"
    table = first.shapes.add_table(2, 2, 0, Inches(2), Inches(4), Inches(2)).table
    table.cell(0, 0).merge(table.cell(0, 1))
    table.cell(0, 0).text = "合并标题"
    table.cell(1, 0).text, table.cell(1, 1).text = "预算", "42"
    first.notes_slide.notes_text_frame.text = "独立备注第一段\n独立备注第二段"
    for placeholder in first.notes_slide.shapes:
        if placeholder.is_placeholder and placeholder.placeholder_format.type == PP_PLACEHOLDER.SLIDE_NUMBER:
            placeholder.text = "9999页码不是备注"
    second = presentation.slides.add_slide(presentation.slide_layouts[6])
    second._element.set("show", "0")
    second.shapes.add_textbox(0, 0, Inches(3), Inches(1)).text = "隐藏页结构文字"
    second.shapes.add_picture(image(), 0, Inches(1))
    third = presentation.slides.add_slide(presentation.slide_layouts[6])
    picture = third.shapes.add_picture(image(), 0, 0)
    data = CategoryChartData()
    data.categories = ["Q1", "Q2"]
    data.add_series("预算", [42, 88])
    chart = third.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), 0, Inches(4), Inches(3), data)
    ids = [str(slide.slide_id) for slide in presentation.slides]
    info = dict(ids=ids, group_path=[str(outer.shape_id), str(nested.shape_id)],
                box_id=str(box.shape_id), picture_id=str(picture.shape_id), chart_id=str(chart.shape_id))
    path = tmp_path / "source.pptx"
    presentation.save(path)

    def reorder(parts):
        tree = ET.fromstring(parts["ppt/presentation.xml"])
        listing = tree.find(f"{{{P}}}sldIdLst")
        listing[:] = [listing[2], listing[0], listing[1]]
        parts["ppt/presentation.xml"] = ET.tostring(tree)

    rewrite(path, reorder)
    return path, info


def test_source_order_stable_ids_objects_tables_and_notes(deck):
    path, info = deck
    before = path.read_bytes()
    parsed = parse_source(path)
    assert parsed["parser_id"] == PARSER_ID == "personal-source"
    assert parsed["parser_version"] == PARSER_VERSION == "2"
    assert parsed["config"]["pptx_structure"] == "pptx-structure-v1"
    coverage = parsed["visual_coverage"]
    assert [c["source_slide_id"] for c in coverage] == [info["ids"][2], info["ids"][0], info["ids"][1]]
    assert [c["source_slide_number"] for c in coverage] == [1, 2, 3]
    assert [c["source_part"] for c in coverage] == ["ppt/slides/slide3.xml", "ppt/slides/slide1.xml", "ppt/slides/slide2.xml"]
    assert [c["hidden"] for c in coverage] == [False, False, True]
    grouped = next(u for u in parsed["units"] if u["text"] == "组合内的中文正文")
    assert grouped["location"]["object_id"] == info["box_id"]
    assert grouped["location"]["group_path"] == info["group_path"]
    assert grouped["location"]["paragraph"] == 2
    assert grouped["location"]["source_slide_id"] == info["ids"][0]
    assert grouped["location"]["source_slide_number"] == 2
    assert grouped["layer"] == "slide" and grouped["type"] == "text"
    assert any(obj["location"]["object_id"] == info["group_path"][0] and obj["object_type"] == "GROUP"
               for obj in coverage[1]["objects"])
    assert [u["text"] for u in parsed["units"]].count("合并标题") == 1
    assert next(u for u in parsed["units"] if u["text"] == "预算\t42")["location"]["row"] == 2
    notes = [u for u in parsed["units"] if u["type"] == "speaker_notes"]
    assert [u["text"] for u in notes] == ["独立备注第一段", "独立备注第二段"]
    assert all(u["layer"] == "speaker_notes" and u["origin"] == "source" for u in notes)
    assert all(u["location"]["source_slide_number"] == 2 and
               u["location"]["source_part"].startswith("ppt/notesSlides/") for u in notes)
    assert "9999" not in json.dumps(parsed, ensure_ascii=False)
    hidden = next(u for u in parsed["units"] if u["text"] == "隐藏页结构文字")
    assert hidden["hidden"] is True and hidden["location"]["source_slide_number"] == 3
    assert before == path.read_bytes()
    assert parse_source(path) == parsed


def test_image_and_chart_gaps_are_locatable_and_not_read_as_chart_cache(deck):
    path, info = deck
    parsed = parse_source(path)
    gaps = parsed["visual_warnings"]
    picture = next(g for g in gaps if g["kind"] == "image" and g["location"]["source_slide_number"] == 1)
    assert picture["location"]["object_id"] == info["picture_id"]
    assert picture["part"].startswith("ppt/media/") and picture["relationship_id"]
    chart = next(g for g in gaps if g["kind"] == "chart")
    assert chart["location"]["object_id"] == info["chart_id"]
    assert chart["location"]["source_slide_id"] == info["ids"][2]
    assert chart["part"].startswith("ppt/charts/")
    assert chart["reason"] == "visual_not_supported" and chart["status"] == "pending"
    assert not any("88" in u["text"] for u in parsed["units"])
    assert any(g["kind"] == "hidden_slide" and g["hidden"] for g in gaps)
    assert all(not c["rendered"] and not c["ocr"] and not c["visual_interpretation"]
               and c["status"] == "pending" for c in parsed["visual_coverage"])


@pytest.mark.parametrize("notes_only", [False, True])
def test_pure_image_and_notes_only_decks_are_valid_without_ocr(tmp_path, notes_only):
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_picture(image(), 0, 0)
    if notes_only:
        slide.notes_slide.notes_text_frame.text = "独立说明"
    path = tmp_path / "blob-without-suffix"
    presentation.save(path)
    parsed = parse_source(path, "纯图.PPTX", prepare_only=True)
    assert parsed["config"]["preparation"] == "native-pptx-v1"
    assert parsed["visual_coverage"][0]["text"] is False
    assert parsed["visual_coverage"][0]["notes_text"] is notes_only
    assert any(g["kind"] == "image" for g in parsed["visual_warnings"])
    assert len(parsed["units"]) == int(notes_only)
    assert all(u["type"] == "speaker_notes" for u in parsed["units"])


def test_notes_images_remain_separate_gaps(deck):
    path, _ = deck

    def move_picture(parts):
        slide = ET.fromstring(parts["ppt/slides/slide3.xml"])
        picture = slide.find(f"{{{P}}}cSld/{{{P}}}spTree/{{{P}}}pic")
        notes = ET.fromstring(parts["ppt/notesSlides/notesSlide1.xml"])
        copied = deepcopy(picture)
        copied.find(f"{{{P}}}nvPicPr/{{{P}}}cNvPr").set("id", "50")
        copied.find(f"{{{P}}}blipFill/{{{A}}}blip").set(f"{{{R}}}embed", "noteImage")
        notes.find(f"{{{P}}}cSld/{{{P}}}spTree").append(copied)
        parts["ppt/notesSlides/notesSlide1.xml"] = ET.tostring(notes)
        relations = ET.fromstring(parts["ppt/notesSlides/_rels/notesSlide1.xml.rels"])
        ET.SubElement(relations, "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship",
                      Id="noteImage", Type=R + "/image", Target="../media/image1.png")
        parts["ppt/notesSlides/_rels/notesSlide1.xml.rels"] = ET.tostring(relations)

    rewrite(path, move_picture)
    parsed = parse_source(path)
    note_image = next(g for g in parsed["visual_warnings"] if g["layer"] == "speaker_notes")
    assert note_image["kind"] == "image" and note_image["location"]["object_id"] == "50"
    assert note_image["location"]["source_part"] == "ppt/notesSlides/notesSlide1.xml"
    assert note_image["location"]["source_slide_number"] == 2


def test_external_image_records_gap_without_fetching(deck):
    path, _ = deck

    def external(parts):
        tree = ET.fromstring(parts["ppt/slides/_rels/slide3.xml.rels"])
        for relation in tree:
            if relation.get("Type").endswith("/image"):
                relation.set("Target", "https://example.invalid/private.png")
                relation.set("TargetMode", "External")
        parts["ppt/slides/_rels/slide3.xml.rels"] = ET.tostring(tree)

    rewrite(path, external)
    parsed = parse_source(path)
    gap = next(g for g in parsed["visual_warnings"] if g["reason"] == "external_resource_not_loaded")
    assert gap["kind"] == "image" and gap["part"] is None
    assert gap["location"]["source_slide_number"] == 1


@pytest.mark.parametrize("failure", ["not_zip", "missing_part", "bad_xml", "duplicate_id", "missing_image_rel", "duplicate_zip"])
def test_corrupt_package_returns_parse_failed_without_partial_result(deck, failure):
    path, _ = deck
    if failure == "not_zip":
        path.write_bytes(b"not-a-pptx")
    elif failure == "duplicate_zip":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with ZipFile(path, "a") as archive:
                archive.writestr("ppt/presentation.xml", b"duplicate")
    else:
        def corrupt(parts):
            if failure == "missing_part":
                del parts["ppt/slides/slide1.xml"]
            elif failure == "bad_xml":
                parts["ppt/slides/slide1.xml"] = b"<broken"
            elif failure == "duplicate_id":
                tree = ET.fromstring(parts["ppt/presentation.xml"])
                listing = tree.find(f"{{{P}}}sldIdLst")
                listing[1].set("id", listing[0].get("id"))
                parts["ppt/presentation.xml"] = ET.tostring(tree)
            else:
                tree = ET.fromstring(parts["ppt/slides/_rels/slide3.xml.rels"])
                for relation in list(tree):
                    if relation.get("Type").endswith("/image"):
                        tree.remove(relation)
                parts["ppt/slides/_rels/slide3.xml.rels"] = ET.tostring(tree)
        rewrite(path, corrupt)
    with pytest.raises(SourceError) as error:
        parse_source(path)
    assert error.value.code == "parse_failed"


@pytest.mark.parametrize("budget", [dict(max_bytes=10), dict(max_units=1)])
def test_text_and_object_budgets_do_not_publish_truncated_result(deck, budget):
    with pytest.raises(SourceError) as error:
        parse_source(deck[0], **budget)
    assert error.value.code == "limit_exceeded"


def test_pure_image_object_budget_and_slide_budget(tmp_path, monkeypatch):
    import agent.knowledge.parser as parser
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_picture(image(), 0, 0)
    slide.shapes.add_picture(image(), 1, 1)
    path = tmp_path / "images.pptx"
    presentation.save(path)
    with pytest.raises(SourceError) as error:
        parse_source(path, max_units=1)
    assert error.value.code == "limit_exceeded"
    monkeypatch.setattr(parser, "MAX_PAGES", 0)
    with pytest.raises(SourceError) as error:
        parse_source(path)
    assert error.value.code == "limit_exceeded"


def test_source_service_accepts_extensionless_blob_and_keeps_stable_parse(deck, tmp_path):
    path, _ = deck
    service = SourceService(tmp_path / "workspace")
    receipt = service.receive(path, filename="幻灯片.pptx", request_id="pptx", origin={"channel": "test"})
    first = service.parse(receipt["revision_id"])
    assert first["sha256"] == receipt["sha256"]
    assert service.parse(receipt["revision_id"])["parse_id"] == first["parse_id"]
    assert first["source_identity"] == dict(document_id=receipt["document_id"], revision_id=receipt["revision_id"])


def test_existing_docx_xlsx_and_pdf_identity_config_unchanged(tmp_path):
    from docx import Document
    from openpyxl import Workbook
    from tests.test_personal_sources import write_test_pdf

    doc = Document()
    doc.add_paragraph("DOCX text")
    doc.save(tmp_path / "source.docx")
    book = Workbook()
    book.active["A1"] = "XLSX text"
    book.save(tmp_path / "source.xlsx")
    book.close()
    write_test_pdf(tmp_path / "source.pdf", ["PDF text"])
    for suffix in ("docx", "xlsx", "pdf"):
        parsed = parse_source(tmp_path / f"source.{suffix}", prepare_only=True)
        assert parsed["parser_id"] == "personal-source" and parsed["parser_version"] == "2"
        assert parsed["config"] == dict(max_bytes=10 * 1024 * 1024, max_units=200000,
                                        max_pages=2000, preparation="native-pdf-v1")
        assert "speaker_notes" not in {u.get("type") for u in parsed["units"]}


@pytest.mark.parametrize("name", ["PPTX_MAX_PACKAGE_BYTES", "PPTX_MAX_PART_BYTES", "PPTX_MAX_PARTS"])
def test_package_reading_budgets_fail_explicitly(deck, monkeypatch, name):
    monkeypatch.setattr("agent.knowledge.parser." + name, 1)
    with pytest.raises(SourceError) as error:
        parse_source(deck[0])
    assert error.value.code == "limit_exceeded"


def test_blank_slide_is_pending_without_creating_notes_or_changing_original(tmp_path):
    presentation = Presentation()
    presentation.slides.add_slide(presentation.slide_layouts[6])
    path = tmp_path / "blank.pptx"
    presentation.save(path)
    before = path.read_bytes()
    with ZipFile(path) as archive:
        assert not any(n.startswith("ppt/notesSlides/") for n in archive.namelist())
    parsed = parse_source(path)
    assert parsed["units"] == []
    assert parsed["visual_coverage"][0]["status"] == "pending"
    assert parsed["visual_coverage"][0]["objects"] == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure", ["object", "empty_presentation", "show"])
def test_ambiguous_or_invalid_slide_structure_rejected(deck, failure):
    path, _ = deck

    def corrupt(parts):
        if failure == "empty_presentation":
            tree = ET.fromstring(parts["ppt/presentation.xml"])
            tree.find(f"{{{P}}}sldIdLst").clear()
            parts["ppt/presentation.xml"] = ET.tostring(tree)
        else:
            tree = ET.fromstring(parts["ppt/slides/slide1.xml"])
            if failure == "object":
                nodes = tree.findall(f".//{{{P}}}cNvPr")
                nodes[-1].set("id", nodes[1].get("id"))
            else:
                tree.set("show", "invalid")
            parts["ppt/slides/slide1.xml"] = ET.tostring(tree)

    rewrite(path, corrupt)
    with pytest.raises(SourceError) as error:
        parse_source(path)
    assert error.value.code == "parse_failed"
