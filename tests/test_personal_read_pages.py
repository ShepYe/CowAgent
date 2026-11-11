"""个人附件 read 回归；任务和资料服务模拟，仅生成临时 PDF 核验页数。"""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.knowledge import personal_assistant
from agent.tools.read.read import Read
from common import personal_tasks, runtime_identity


def pdf_unit(page, text=None):
    return {"text": text if text is not None else f"物理页 {page}",
            "location": {"kind": "pdf", "physical_page": page}}


@pytest.fixture
def attachment_read(monkeypatch):
    import pypdf
    # 无扩展名的哈希路径；不创建或读取附件文件。
    path = str(Path("/virtual-personal-read/originals/abcdef").absolute())
    attachment = dict(path=path, filename="资料.PDF", attachment_id="attachment-test")
    origin = {"channel": "test"}
    store = Mock()
    store.get.return_value = dict(attachments=[attachment], origin=origin)
    store.attachment.return_value = attachment
    parsed = dict(parse_id="parse-test", units=[pdf_unit(n) for n in range(1, 31)])
    service = Mock()
    service.receive.return_value = dict(document_id="document-test", revision_id="content-revision")
    service.parse.return_value = parsed
    service.get.return_value = {"current": "published-revision"}
    factory = Mock(return_value=service)
    original_pdf_reader = pypdf.PdfReader
    pdf_reader = Mock(return_value=SimpleNamespace(pages=[None] * 30))
    monkeypatch.setattr(pypdf, "PdfReader", pdf_reader)
    monkeypatch.setattr(personal_assistant, "SourceService", factory)
    monkeypatch.setattr(personal_assistant, "guard_path", Mock())
    monkeypatch.setattr(personal_tasks, "current_task_id", lambda: "task-test")
    monkeypatch.setattr(runtime_identity, "current_identity", lambda: SimpleNamespace(user_id="test-owner"))
    monkeypatch.setattr(Path, "resolve", lambda self: self.absolute())
    tool = Read(dict(cwd=str(Path(path).parent), task_store=store))
    monkeypatch.setattr(tool, "_resolve_path", lambda _: path)
    monkeypatch.setattr(tool, "_is_credential_path", lambda _: False)
    # execute 的存在性检查也不能接触附件。
    import os
    exists = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda p: True if str(p) == path else exists(p))

    def read(**args):
        return tool.execute(dict(path=path, **args))

    return SimpleNamespace(read=read, parsed=parsed, service=service,
                           attachment=attachment, path=path, origin=origin,
                           tool=tool, pdf_reader=pdf_reader, original_pdf_reader=original_pdf_reader)


def result_of(result):
    assert result.status == "success", result.result
    return result.result


def physical_pages(result):
    return [unit["location"]["physical_page"] for unit in result["units"]]


def test_distinct_pdf_windows_use_real_pages(attachment_read):
    early = result_of(attachment_read.read(pages="1-20"))
    late = result_of(attachment_read.read(pages="25-29"))
    assert early["units"] != late["units"]
    assert physical_pages(early) == list(range(1, 21))
    assert physical_pages(late) == list(range(25, 30))
    assert late["total_units"] == 5
    attachment_read.pdf_reader.assert_called_with(attachment_read.path)


def test_pdf_window_metadata_distinguishes_remaining_pages(attachment_read):
    first = result_of(attachment_read.read())
    assert first["total_pages"] == 30 and first["pages_read"] == "1-20"
    assert not first["has_more"] and first["next_offset"] is None
    assert first["next_pages"] == "21-"
    last = result_of(attachment_read.read(pages=first["next_pages"]))
    assert last["total_pages"] == 30 and last["pages_read"] == "21-30"
    assert physical_pages(last) == list(range(21, 31))
    assert not last["has_more"] and last["next_offset"] is None
    assert last["next_pages"] is None


def test_sparse_pages_keep_source_ordinals_and_locations(attachment_read):
    units = [pdf_unit(1), pdf_unit(24), pdf_unit(26, "甲" * 4100), pdf_unit(29), pdf_unit(30)]
    attachment_read.parsed["units"] = units
    original = deepcopy(units)
    result = result_of(attachment_read.read(pages="25-29"))
    assert physical_pages(result) == [26, 26, 26, 29]
    assert [u["ordinal"] for u in result["units"]] == [2, 2, 2, 3]
    assert [(u["text_start"], u["text_end"]) for u in result["units"]] == [
        (0, 2000), (2000, 4000), (4000, 4100), (0, len(units[3]["text"]))]
    assert "".join(u["text"] for u in result["units"][:3]) == units[2]["text"]
    assert units == original


def test_offset_limit_continue_inside_page_window(attachment_read):
    attachment_read.parsed["units"][24]["text"] = "续" * 4100
    first = result_of(attachment_read.read(pages="25-29", limit=2))
    second = result_of(attachment_read.read(pages="25-29", offset=first["next_offset"], limit=20))
    assert physical_pages(first) == [25, 25]
    assert first["has_more"] and first["next_offset"] == 3
    assert physical_pages(second) == [25, 26, 27, 28, 29]
    assert second["offset"] == 3 and second["total_units"] == 7
    assert not second["has_more"] and second["next_offset"] is None
    tail = result_of(attachment_read.read(pages="25-29", offset=-2, limit=2))
    assert physical_pages(tail) == [28, 29]


@pytest.mark.parametrize("pages, expected", [
    (None, list(range(1, 21))), ("", list(range(1, 21))),
    ("25", [25]), ("25-", list(range(25, 31))),
    ("1-30", list(range(1, 21))), ("29-99", [29, 30])])
def test_pdf_range_contract(attachment_read, pages, expected):
    result = result_of(attachment_read.read(pages=pages))
    assert physical_pages(result) == expected


@pytest.mark.parametrize("pages", ["nope", "0", "5-2", "31"])
def test_invalid_pdf_range_is_rejected(attachment_read, pages):
    result = attachment_read.read(pages=pages)
    assert result.status == "error"
    assert result.result["code"] == "invalid_request"


def test_blank_page_window_does_not_return_nearby_text(attachment_read):
    attachment_read.parsed["units"] = [pdf_unit(1), pdf_unit(30)]
    result = result_of(attachment_read.read(pages="25-29"))
    assert result["units"] == [] and result["total_units"] == 0
    assert not result["has_more"] and result["next_offset"] is None


@pytest.mark.parametrize("filename, location", [
    ("资料.txt", {"kind": "text", "line": 25}),
    ("资料.docx", {"kind": "docx", "path": "paragraph[25]"}),
    ("资料.xlsx", {"kind": "xlsx", "sheet": "预算", "cell": "B25"})])
def test_non_pdf_ignores_pages_and_keeps_pagination(attachment_read, filename, location):
    attachment_read.attachment["filename"] = filename
    attachment_read.parsed["units"] = [dict(text=str(n), location=location) for n in range(30)]
    baseline = result_of(attachment_read.read(offset=23, limit=3))
    with_pages = result_of(attachment_read.read(pages="invalid", offset=23, limit=3))
    assert baseline == with_pages
    assert [u["text"] for u in with_pages["units"]] == ["22", "23", "24"]
    assert with_pages["total_units"] == 30 and with_pages["next_offset"] == 26
    attachment_read.pdf_reader.assert_not_called()


@pytest.mark.parametrize("current", [None, "published-revision"])
def test_source_identity_and_expected_current_revision(attachment_read, current):
    attachment_read.service.get.return_value = {"current": current}
    result = result_of(attachment_read.read(pages="25-29"))
    assert result["document_id"] == "document-test"
    assert result["revision_id"] == "content-revision"
    assert result["expected_current_revision"] == current
    assert result["parse_id"] == "parse-test" and result["attachment_id"] == "attachment-test"
    assert result["filename"] == "资料.PDF"
    assert not result["published"] and not result["searchable"] and result["display_fragments"]
    attachment_read.service.get.assert_called_once_with("document-test")
    attachment_read.service.receive.assert_called_once_with(
        attachment_read.path, filename="资料.PDF", request_id="task-test:attachment-test",
        origin=attachment_read.origin)
    attachment_read.service.parse.assert_called_once_with("content-revision")


@pytest.mark.parametrize("max_bytes", [51200, 9000])
def test_long_english_pdf_budget_and_lossless_continuation(attachment_read, monkeypatch, max_bytes):
    from importlib import import_module
    module = import_module("agent.tools.read.read")
    monkeypatch.setattr(module, "DEFAULT_MAX_BYTES", max_bytes)
    units = [pdf_unit(1, "outside selected window"),
             pdf_unit(25, "English source page twenty five. " * 2200),
             pdf_unit(29, "English source page twenty nine. " * 800),
             pdf_unit(30, "outside selected window")]
    attachment_read.parsed["units"] = units
    recovered = {25: [], 29: []}
    offset = 1
    reads = 0
    while True:
        result = result_of(attachment_read.read(pages="25-29", offset=offset, limit=200))
        serialized = json.dumps(result, ensure_ascii=False)
        assert len(serialized) < 50000
        assert len(serialized.encode("utf-8")) <= min(max_bytes, 45000)
        decoded = json.loads(serialized)
        assert "next_offset" in decoded and "has_more" in decoded
        assert result["offset"] == offset
        for unit in result["units"]:
            page = unit["location"]["physical_page"]
            assert unit["ordinal"] == (1 if page == 25 else 2)
            assert unit["text_start"] == sum(map(len, recovered[page]))
            recovered[page].append(unit["text"])
            assert unit["text_end"] == sum(map(len, recovered[page]))
        reads += 1
        if not result["has_more"]:
            assert result["next_offset"] is None
            break
        assert result["next_offset"] > offset
        offset = result["next_offset"]
        assert reads < 100
    assert reads > 1
    assert "".join(recovered[25]) == units[1]["text"]
    assert "".join(recovered[29]) == units[2]["text"]


def test_real_pdf_trailing_blank_page_is_a_valid_window(attachment_read, tmp_path, monkeypatch):
    import pypdf
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    from agent.knowledge.parser import parse_source

    # 全新生成的测试 PDF；前两页有文字，第 3 页空白。
    writer = pypdf.PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    for number in range(1, 4):
        page = writer.add_blank_page(width=600, height=800)
        if number < 3:
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
                DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 70 700 Td (Page {number}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    path = tmp_path / "trailing-blank.pdf"
    writer.write(path)
    monkeypatch.setattr(pypdf, "PdfReader", attachment_read.original_pdf_reader)
    parsed = parse_source(path)
    assert [u["location"]["physical_page"] for u in parsed["units"]] == [1, 2]
    attachment_read.parsed["units"] = parsed["units"]
    attachment_read.attachment["path"] = str(path)
    monkeypatch.setattr(attachment_read.tool, "_resolve_path", lambda _: str(path))
    # read 只能读取页数元数据，不能重提取文本。
    monkeypatch.setattr(pypdf.PageObject, "extract_text", Mock(side_effect=AssertionError("禁止重提取文本")))
    result = result_of(attachment_read.read(pages="3"))
    assert result["units"] == [] and result["total_units"] == 0
    assert not result["has_more"] and result["next_offset"] is None
