"""PDFium renders real PDF fixtures; all model execution is replaced offline."""

import hashlib
import json
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject, NumberObject

from agent.knowledge.parser import parse_source
from agent.knowledge.visual import enrich_pdf_visual
from agent.tools.base_tool import ToolResult


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def deny(*args, **kwargs):
        raise AssertionError("离线视觉测试禁止外部请求")

    monkeypatch.setattr(socket.socket, "connect", deny)


def make_pdf(path, kinds=("scan", "mixed", "vector", "text", "blank")):
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for kind in kinds:
        page = writer.add_blank_page(width=300, height=400)
        resources = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        commands = ""
        if kind in ("text", "mixed"):
            commands += "BT /F1 12 Tf 20 350 Td (real source text) Tj ET\n"
        if kind in ("scan", "mixed"):
            image = DecodedStreamObject()
            image.set_data(bytes([0, 0, 0, 255, 255, 255] * 2))
            image.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(2),
                    NameObject("/Height"): NumberObject(2),
                    NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                    NameObject("/BitsPerComponent"): NumberObject(8),
                }
            )
            resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Im"): writer._add_object(image)})
            commands += "q 180 0 0 200 20 50 cm /Im Do Q\n"
        if kind == "vector":
            commands += "0 0 0 RG 2 w 20 20 m 100 200 l 200 60 l S\n"
        stream = DecodedStreamObject()
        stream.set_data(commands.encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = resources
    writer.write(path)
    return path


class FakeVision:
    def __init__(self, available=True, response=None):
        self.available, self.response, self.calls = available, response, []

    def _resolve_providers(self):
        return ["offline"] if self.available else []

    def execute(self, args):
        assert Path(args["image"]).suffix == ".png"
        assert Path(args["image"]).is_file()
        from config import conf

        self.calls.append((args, dict(conf())))
        return self.response or ToolResult.success(
            {
                "content": json.dumps(
                    {
                        "ocr": "图像转写 42 dB",
                        "visual_interpretation": "曲线显示趋势，未确认精确值",
                        "unprocessed_reason": None,
                    },
                    ensure_ascii=False,
                )
            }
        )


def enrich(path, parsed, vision, **kwargs):
    from models.config_revision import capture_config_revision

    return enrich_pdf_visual(path, parsed, vision=vision, config_revision=capture_config_revision(), **kwargs)


def test_offline_renders_scan_mixed_vector_and_retains_original(tmp_path, monkeypatch):
    from agent.tools.vision.vision import Vision

    monkeypatch.setattr(Vision, "execute", lambda *_: pytest.fail("离线 parse 不得调用视觉"))
    path = make_pdf(tmp_path / "hash-without-suffix")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    parsed = parse_source(path, "real.pdf")
    coverage = parsed["visual_coverage"]
    assert [c["physical_page"] for c in coverage] == [1, 2, 3, 4, 5]
    assert all(c["rendered"] for c in coverage)
    assert [c["text"] for c in coverage] == [False, True, False, True, False]
    assert all(c["status"] == "pending" for c in coverage[:4])
    assert coverage[-1]["status"] == "blank"
    assert all(not c["ocr"] and not c["visual_interpretation"] for c in coverage)
    assert before == hashlib.sha256(path.read_bytes()).hexdigest()


def test_scanned_pdf_without_text_returns_pending_parse(tmp_path):
    parsed = parse_source(make_pdf(tmp_path / "scan.pdf", ("scan",)))
    assert parsed["units"] == []
    assert parsed["visual_coverage"][0]["unprocessed_reason"] == "visual_not_requested"


def test_explicit_visual_has_physical_page_types_and_does_not_mutate_offline(tmp_path):
    path = make_pdf(tmp_path / "source.pdf")
    offline = parse_source(path)
    vision = FakeVision()
    parsed = enrich(path, offline, vision)
    assert len(vision.calls) == 4
    assert len(parsed["units"]) == len(offline["units"]) + 8
    assert all(u["type"] == u["location"]["type"] for u in parsed["units"])
    assert {u["location"]["physical_page"] for u in parsed["units"] if u["type"] == "ocr"} == {1, 2, 3, 4}
    assert all(c["status"] == "processed" for c in parsed["visual_coverage"][:4])
    assert all(c["status"] == "pending" for c in offline["visual_coverage"][:4])
    assert not Path(vision.calls[0][0]["image"]).exists()


@pytest.mark.parametrize(
    "response,reason",
    [
        (ToolResult.fail("offline failure"), "vision_failed"),
        (ToolResult.success({"content": "not json"}), "vision_invalid_response"),
        (ToolResult.success({"content": '{"ocr":"","visual_interpretation":""}'}), "vision_empty"),
    ],
)
def test_visual_failure_never_claims_understanding(tmp_path, response, reason):
    path = make_pdf(tmp_path / "source.pdf", ("mixed",))
    parsed = enrich(path, parse_source(path), FakeVision(response=response))
    item = parsed["visual_coverage"][0]
    assert item["unprocessed_reason"] == reason
    assert not item["ocr"] and not item["visual_interpretation"]
    assert len(parsed["units"]) == 1


def test_unavailable_and_stale_task_configuration_do_not_call_vision(tmp_path):
    path = make_pdf(tmp_path / "source.pdf", ("vector",))
    offline = parse_source(path)
    vision = FakeVision(available=False)
    assert enrich(path, offline, vision)["visual_coverage"][0]["unprocessed_reason"] == "vision_unavailable"
    for revision in (None, "unavailable-task-revision"):
        result = enrich_pdf_visual(path, offline, vision=vision, config_revision=revision)
        assert result["visual_coverage"][0]["unprocessed_reason"] == "config_unavailable"
    assert not vision.calls


def test_task_scope_snapshot_used_by_existing_vision(tmp_path):
    from config import Config, config_scope
    from models.config_revision import capture_config_revision

    path = make_pdf(tmp_path / "source.pdf", ("vector",))
    vision = FakeVision()
    with config_scope(Config({"model": "task-bound-model", "tools": {"vision": {"model": "bound-vision"}}})):
        parsed = enrich_pdf_visual(path, parse_source(path), vision=vision, config_revision=capture_config_revision())
    assert parsed["visual_coverage"][0]["status"] == "processed"
    assert vision.calls[0][1]["tools"]["vision"]["model"] == "bound-vision"


def test_render_and_visual_budgets_are_explicit(tmp_path):
    path = make_pdf(tmp_path / "source.pdf", ("mixed", "vector", "scan"))
    offline = parse_source(path, render_budget={"max_pages": 1})
    assert offline["visual_coverage"][1]["unprocessed_reason"] == "render_budget_exceeded"
    assert offline["visual_coverage"][2]["unprocessed_reason"] == "render_budget_exceeded"
    vision = FakeVision()
    result = enrich(path, parse_source(path), vision, budget={"max_pages": 1})
    assert len(vision.calls) == 1
    assert all(c["unprocessed_reason"] == "visual_budget_exceeded" for c in result["visual_coverage"][1:])
    result = enrich(path, parse_source(path), FakeVision(), budget={"max_bytes": 1})
    assert result["units"] == parse_source(path)["units"]
    assert all(c["unprocessed_reason"] == "visual_budget_exceeded" for c in result["visual_coverage"])


def test_renderer_unavailable_has_coverage(tmp_path, monkeypatch):
    import sys

    path = make_pdf(tmp_path / "source.pdf", ("scan", "vector"))
    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    parsed = parse_source(path)
    assert len(parsed["visual_coverage"]) == 2
    assert all(c["unprocessed_reason"] == "renderer_unavailable" for c in parsed["visual_coverage"])


def test_twenty_nine_pages_continue_without_repeating_completed_units(tmp_path):
    path = make_pdf(tmp_path / "source.pdf", ("vector",) * 29)
    vision = FakeVision()
    first = enrich(path, parse_source(path), vision)
    assert len(vision.calls) == 20
    assert [c["status"] for c in first["visual_coverage"]].count("pending") == 9
    units = json.loads(json.dumps(first["units"]))
    second = enrich(path, first, vision)
    assert len(vision.calls) == 29
    assert all(c["status"] == "processed" for c in second["visual_coverage"])
    assert second["units"][: len(units)] == units
    assert {u["location"]["physical_page"] for u in second["units"]} == set(range(1, 30))


def test_failed_first_batch_does_not_starve_later_pages(tmp_path):
    path = make_pdf(tmp_path / "source.pdf", ("vector",) * 3)
    failing = FakeVision(response=ToolResult.fail("offline"))
    first = enrich(path, parse_source(path), failing, budget={"max_pages": 1})
    good = FakeVision()
    second = enrich(path, first, good, budget={"max_pages": 1})
    assert second["visual_coverage"][1]["status"] == "processed"
    assert second["visual_coverage"][0]["unprocessed_reason"] == "visual_budget_exceeded"


def test_zero_time_budget_does_not_send_page(tmp_path):
    path = make_pdf(tmp_path / "source.pdf", ("mixed",))
    vision = FakeVision()
    result = enrich(path, parse_source(path), vision, budget={"max_seconds": 0})
    assert not vision.calls
    assert result["visual_coverage"][0]["unprocessed_reason"] == "visual_budget_exceeded"


@pytest.mark.parametrize("mode", ["reorganize", "source_action"])
@pytest.mark.parametrize("invalid", [None, "state", "hash"])
def test_read_restoration_keeps_registered_identity(tmp_path, mode, invalid):
    from agent.knowledge.personal_assistant import SourceService
    from agent.tools.read.read import Read
    from common.personal_tasks import TaskStore, OWNER, task_scope
    from common.runtime_identity import identity_scope

    original = tmp_path / "original.txt"
    original.write_text("registered source", encoding="utf-8")
    service = SourceService(tmp_path)
    source = service.receive(original, filename=original.name, request_id="initial", origin={"channel": "test"})
    directive = dict(
        document_id=source["document_id"],
        revision_id=source["revision_id"],
        expected_state=99 if invalid == "state" else 0,
    )
    store = TaskStore(tmp_path / "tasks")
    task = store.submit(OWNER, "restore", {mode: directive}, {"channel": "test"}, "offline")
    if invalid == "hash":
        original.write_text("unrelated file", encoding="utf-8")
    attachment = store.attach(OWNER, task["task_id"], original)
    store.claim(OWNER, task["task_id"])
    if mode == "source_action":
        store._change(
            OWNER, task["task_id"], 1, {"running"}, source_action_checkpoint={"state": directive["expected_state"]}
        )
    with identity_scope(user_id=OWNER), task_scope(task["task_id"]):
        result = Read({"cwd": str(tmp_path), "task_store": store}).execute({"path": attachment["path"]})
    if invalid:
        assert result.status == "error"
        assert result.result["code"] == "version_conflict"
    else:
        assert result.status == "success", result.result
        assert result.result["document_id"] == source["document_id"]
        assert result.result["revision_id"] == source["revision_id"]
        persisted = json.loads(
            (service.root / "parses" / (result.result["parse_id"] + ".json")).read_text(encoding="utf-8")
        )
        assert persisted["units"][result.result["units"][0]["ordinal"]]["text"] == "registered source"
    with service.transaction() as db:
        assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 1


def test_persisted_visual_parse_read_continuation_and_tamper_guards(tmp_path, monkeypatch):
    from agent.knowledge.personal_assistant import SourceService
    from agent.knowledge.parser import SourceError
    from agent.tools.read.read import Read
    from agent.tools.vision.vision import Vision
    from common.personal_tasks import TaskStore, OWNER, task_scope
    from common.runtime_identity import identity_scope
    from models.config_revision import capture_config_revision

    path = make_pdf(tmp_path / "source.pdf", ("vector",) * 29)
    store = TaskStore(tmp_path / "tasks")
    task = store.submit(OWNER, "read-visual", {}, {"channel": "test"}, capture_config_revision())
    attachment = store.attach(OWNER, task["task_id"], path)
    store.claim(OWNER, task["task_id"])
    vision = FakeVision()
    monkeypatch.setattr(Vision, "_resolve_providers", lambda self: ["offline"])
    monkeypatch.setattr(Vision, "execute", lambda self, args: vision.execute(args))
    tool = Read({"cwd": str(tmp_path), "task_store": store})
    with identity_scope(user_id=OWNER), task_scope(task["task_id"]):
        first = tool.execute({"path": attachment["path"]})
        assert first.status == "success", first.result
        assert first.result["next_visual_page"] == 21
        assert len(vision.calls) == 20
        second = tool.execute({"path": attachment["path"], **first.result["visual_continuation"]})
        assert second.status == "success", second.result
        assert second.result["next_visual_page"] is None
        assert len(vision.calls) == 29
        third = tool.execute({"path": attachment["path"], "pages": "21-"})
        assert third.status == "success", third.result
        assert third.result["parse_id"] == second.result["parse_id"]
        assert len(vision.calls) == 29
    service = SourceService(tmp_path)

    def load(parse_id):
        return json.loads((service.root / "parses" / f"{parse_id}.json").read_text(encoding="utf-8"))

    first_parse = load(first.result["parse_id"])
    second_parse = load(second.result["parse_id"])
    assert second_parse["units"][: len(first_parse["units"])] == first_parse["units"]
    for unit in second.result["units"]:
        assert second_parse["units"][unit["ordinal"]]["location"] == unit["location"]
    original_mtime = (service.root / "parses" / f"{second.result['parse_id']}.json").stat().st_mtime_ns
    service.parse(second.result["revision_id"], base_parse_id=second.result["parse_id"])
    assert (service.root / "parses" / f"{second.result['parse_id']}.json").stat().st_mtime_ns == original_mtime
    other = service.receive(path, filename="other.pdf", request_id="other", origin={"channel": "test"})
    with pytest.raises(SourceError, match="不属于"):
        service.parse(other["revision_id"], base_parse_id=second.result["parse_id"])
    second_parse["units"][0]["text"] = "tampered source"
    (service.root / "parses" / f"{second.result['parse_id']}.json").write_text(
        json.dumps(second_parse), encoding="utf-8"
    )
    with pytest.raises(SourceError, match="哈希"):
        service.parse(second.result["revision_id"], base_parse_id=second.result["parse_id"])


@pytest.mark.parametrize("copied_thread", [False, True])
def test_nested_read_calls_real_vision_with_admitted_snapshot_after_runtime_mutation(
    tmp_path, monkeypatch, copied_thread
):
    """Exercise Read -> persisted parse -> nested scope -> real Vision.execute."""
    import copy
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    import config
    from config import Config, conf, config_scope
    from models.config_revision import capture_config_revision, use_config_revision
    from agent.knowledge.personal_assistant import SourceService
    from agent.tools.read.read import Read
    from agent.tools.vision.vision import Vision
    from common.personal_tasks import OWNER, TaskStore, task_scope
    from common.runtime_identity import identity_scope

    admitted = {
        "model": "admitted-chat",
        "tools": {"vision": {"provider": "custom:fixture", "model": "admitted-vision"}},
        "custom_providers": [{"id": "fixture", "api_key": "offline-admitted-key"}],
    }
    calls = []

    def resolve_providers(self):
        assert dict(conf()) == admitted
        return ["offline-provider"]

    def call_with_fallback(self, providers, model, question, image_content):
        # Keep Vision.execute and its local image encoding real. Only model I/O
        # is substituted, and the autouse fixture denies every socket connect.
        assert providers == ["offline-provider"]
        assert image_content["image_url"]["url"].startswith("data:image/png;base64,")
        assert dict(conf()) == admitted
        calls.append(copy.deepcopy(dict(conf())))
        conf()["tools"]["vision"]["model"] = "callee-local-mutation"
        return ToolResult.success({"content": json.dumps({
            "ocr": "visual source", "visual_interpretation": "vector trend", "unprocessed_reason": None
        })})

    monkeypatch.setattr(Vision, "_resolve_providers", resolve_providers)
    monkeypatch.setattr(Vision, "_call_with_fallback", call_with_fallback)
    path = make_pdf(tmp_path / "source.pdf", ("mixed",))
    store = TaskStore(tmp_path / "tasks")
    with config_scope(Config(copy.deepcopy(admitted))):
        revision = capture_config_revision()
        task = store.submit(OWNER, "nested-visual", {}, {"channel": "test"}, revision)
        attachment = store.attach(OWNER, task["task_id"], path)
        store.claim(OWNER, task["task_id"])
        with use_config_revision(revision), identity_scope(user_id=OWNER), task_scope(task["task_id"]):
            conf()["runtime_normalization"] = True
            conf()["tools"]["vision"]["model"] = "runtime-mutated-vision"
            conf()["custom_providers"][0]["api_key"] = "runtime-mutated-key"
            caller_snapshot = copy.deepcopy(dict(conf()))
            from models.config_revision import _revision
            assert _revision(conf()) != revision
            assert capture_config_revision() == revision
            monkeypatch.setattr(config, "config", Config({"model": "foreign-global-config"}))
            tool = Read({"cwd": str(tmp_path), "task_store": store})
            if copied_thread:
                context = contextvars.copy_context()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    result = pool.submit(context.run, tool.execute, {"path": attachment["path"]}).result()
            else:
                result = tool.execute({"path": attachment["path"]})
            assert dict(conf()) == caller_snapshot
        assert dict(conf()) == admitted
    assert result.status == "success", result.result
    assert calls == [admitted]
    assert result.result["visual_coverage"][0]["status"] == "processed"
    persisted = json.loads((SourceService(tmp_path).root / "parses" /
                           f"{result.result['parse_id']}.json").read_text(encoding="utf-8"))
    assert persisted["visual_usage"]["calls"] == 1
    assert persisted["config"]["visual"]["config_revision"] == revision
    assert {u["type"] for u in persisted["units"]} == {"text", "ocr", "visual_interpretation"}


def test_nested_visual_cannot_borrow_different_revision_or_escape_scope(tmp_path):
    from config import Config, conf, config_scope
    from models.config_revision import capture_config_revision, use_config_revision

    path = make_pdf(tmp_path / "source.pdf", ("vector",))
    parsed = parse_source(path)
    vision = FakeVision()
    with config_scope(Config({"model": "admitted-model"})):
        revision = capture_config_revision()
        with use_config_revision(revision):
            conf()["model"] = "runtime-mutated-model"
            from models.config_revision import _revision
            foreign_revision = _revision(conf())
            assert foreign_revision != revision
            assert capture_config_revision() == revision
            blocked = enrich_pdf_visual(path, parsed, vision=vision, config_revision=foreign_revision)
            assert blocked["visual_coverage"][0]["unprocessed_reason"] == "config_unavailable"
            assert not vision.calls
        conf()["model"] = "new-global-model"
        blocked = enrich_pdf_visual(path, parsed, vision=vision, config_revision=revision)
        assert blocked["visual_coverage"][0]["unprocessed_reason"] == "config_unavailable"
        assert not vision.calls


@pytest.mark.parametrize("max_bytes", [9000, 45000])
def test_501_page_read_windows_metadata_and_lossless_continuation(tmp_path, monkeypatch, max_bytes):
    import importlib
    from copy import deepcopy
    from types import SimpleNamespace
    from unittest.mock import Mock
    import pypdf
    from agent.knowledge import personal_assistant
    from common import personal_tasks, runtime_identity

    module = importlib.import_module("agent.tools.read.read")
    monkeypatch.setattr(module, "DEFAULT_MAX_BYTES", max_bytes)
    path = str(tmp_path / "original-hash")
    attachment = dict(path=path, filename="large.pdf", attachment_id="fixture")
    task = dict(attachments=[attachment], origin={"channel": "test"})
    store = SimpleNamespace(get=lambda *args: task, attachment=lambda *args: attachment)
    coverage = [dict(physical_page=n, text=True, rendered=True, ocr=False,
                     visual_interpretation=False, status="pending", unprocessed_reason="visual_not_requested",
                     render_scale=1.5, pixels=1080000) for n in range(1, 502)]
    units = [dict(text=f"page {n} " + "evidence " * 450,
                  location=dict(kind="pdf", physical_page=n, type="text"), type="text")
             for n in range(1, 502)]
    parsed = dict(parse_id="stable-parse", units=units, visual_coverage=coverage)
    before = deepcopy(parsed)
    service = Mock()
    service.receive.return_value = dict(document_id="document", revision_id="revision")
    service.parse.return_value = parsed
    service.get.return_value = {"current": None}
    monkeypatch.setattr(personal_assistant, "SourceService", lambda *args: service)
    monkeypatch.setattr(personal_tasks, "current_task_id", lambda: "task")
    monkeypatch.setattr(runtime_identity, "current_identity", lambda: SimpleNamespace(user_id="owner"))
    monkeypatch.setattr(pypdf, "PdfReader", lambda *args: SimpleNamespace(pages=[None] * 501))
    from agent.tools.vision.vision import Vision
    monkeypatch.setattr(Vision, "execute", lambda *args: pytest.fail("分页替身不得调用视觉"))
    tool = module.Read({"cwd": str(tmp_path), "task_store": store})
    recovered = {n: "" for n in range(481, 501)}
    offset = 1
    while True:
        read = tool._read_task_attachment(path, offset, 200, "481-500", visual=False)
        assert read.status == "success", read.result
        result = read.result
        encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
        assert len(encoded) <= max_bytes
        assert list(result)[:6] == ["has_more", "next_offset", "next_pages", "next_visual_page",
                                    "visual_continuation", "parse_id"]
        assert result["parse_id"] == "stable-parse" and result["next_pages"] == "501-"
        assert [c["physical_page"] for c in result["visual_coverage"]] == list(range(481, 501))
        assert result["visual_coverage_summary"]["total_pages"] == 501
        assert result["visual_coverage_summary"]["pending_pages"] == 501
        assert result["units"]
        for unit in result["units"]:
            number = unit["location"]["physical_page"]
            assert unit["ordinal"] == number - 1
            assert unit["text_start"] == len(recovered[number])
            recovered[number] += unit["text"]
        if not result["has_more"]:
            break
        assert result["next_offset"] > offset
        offset = result["next_offset"]
    assert all(recovered[n] == units[n - 1]["text"] for n in recovered)
    final = tool._read_task_attachment(path, 1, 200, "501", visual=False).result
    assert len(final["visual_coverage"]) == 1 and final["next_pages"] is None
    assert parsed == before  # The complete persisted coverage is never sliced in place.
    monkeypatch.setattr(module, "DEFAULT_MAX_BYTES", 1500)
    rejected = tool._read_task_attachment(path, 1, 200, "481-500", visual=False)
    assert rejected.status == "error" and rejected.result["code"] == "limit_exceeded"
    assert rejected.result["parse_id"] == "stable-parse"


@pytest.mark.parametrize("extension", ["docx", "xlsx"])
def test_office_embedded_images_and_charts_are_marked_pending(tmp_path, extension):
    from io import BytesIO
    from zipfile import ZipFile
    from PIL import Image

    image = BytesIO()
    Image.new("RGB", (8, 8), "black").save(image, format="PNG")
    image.seek(0)
    path = tmp_path / f"source.{extension}"
    if extension == "docx":
        from docx import Document
        document = Document()
        document.add_paragraph("text evidence")
        document.add_picture(image)
        document.save(path)
        with ZipFile(path, "a") as package:
            package.writestr("word/charts/chart1.xml", "<chart/>")
        prefix = "word"
    else:
        from openpyxl import Workbook
        from openpyxl.chart import BarChart, Reference
        from openpyxl.drawing.image import Image as WorkbookImage
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["text evidence", 42])
        chart = BarChart()
        chart.add_data(Reference(sheet, min_col=2, min_row=1, max_row=1))
        sheet.add_chart(chart, "D1")
        sheet.add_image(WorkbookImage(image), "D20")
        workbook.save(path)
        workbook.close()
        prefix = "xl"
    parsed = parse_source(path)
    assert parsed["units"] and parsed["visual_coverage"] == []
    warnings = parsed["visual_warnings"]
    assert {warning["kind"] for warning in warnings} == {"image", "chart"}
    assert all(warning["part"].startswith(prefix + "/") for warning in warnings)
    assert all(warning["status"] == "pending" and warning["reason"] == "visual_not_supported"
               for warning in warnings)


def test_default_parse_reuses_best_visual_revision_across_tasks(tmp_path, monkeypatch):
    from agent.knowledge.personal_assistant import SourceService
    from models.config_revision import capture_config_revision

    path = make_pdf(tmp_path / "source.pdf", ("vector",) * 3)
    service = SourceService(tmp_path)
    source = service.receive(path, filename="source.pdf", request_id="first", origin={"channel": "test"})
    offline = service.parse(source["revision_id"])
    vision = FakeVision()
    first = service.parse(source["revision_id"], visual=True, vision=vision,
                          config_revision=capture_config_revision(), visual_budget={"max_pages": 1})
    assert len(vision.calls) == 1
    # Default/ingest reads must preserve both the identity and original ordinal.
    reopened = SourceService(tmp_path)
    cached = reopened.parse(source["revision_id"])
    assert cached == first
    assert len(vision.calls) == 1
    second = reopened.parse(source["revision_id"], visual=True, vision=vision,
                            config_revision=capture_config_revision(), visual_budget={"max_pages": 1})
    assert len(vision.calls) == 2
    assert second["units"][:len(first["units"])] == first["units"]
    assert [c["status"] for c in second["visual_coverage"]] == ["processed", "processed", "pending"]
    # A later failed branch must not replace a more complete immutable parse.
    reopened.parse(source["revision_id"], base_parse_id=offline["parse_id"], visual=True,
                   vision=None, config_revision=capture_config_revision())
    expected_path = reopened.root / "parses" / f"{second['parse_id']}.json"
    mtime = expected_path.stat().st_mtime_ns
    from agent.knowledge import personal_assistant
    monkeypatch.setattr(personal_assistant, "parse_source", lambda *a, **kw: pytest.fail("不得重新离线解析"))
    assert reopened.parse(source["revision_id"]) == second
    assert expected_path.stat().st_mtime_ns == mtime


def test_default_visual_cache_rejects_foreign_tampered_and_old_parser(tmp_path):
    from agent.knowledge.personal_assistant import SourceService, _hash, _json
    from models.config_revision import capture_config_revision

    path = make_pdf(tmp_path / "source.pdf", ("vector",))
    service = SourceService(tmp_path)
    source = service.receive(path, filename="source.pdf", request_id="first", origin={"channel": "test"})
    offline = service.parse(source["revision_id"])
    good = service.parse(source["revision_id"], visual=True, vision=FakeVision(),
                         config_revision=capture_config_revision())
    other = service.receive(path, filename="same-bytes.pdf", request_id="other", origin={"channel": "test"})
    isolated = service.parse(other["revision_id"])
    assert isolated["document_id"] != source["document_id"]
    assert not any(c["ocr"] for c in isolated["visual_coverage"])
    # A self-consistent old parser output is not eligible for automatic reuse.
    old = dict(good, parser_version="old-version")
    content = {k: v for k, v in old.items() if k not in {"parse_id", "document_id", "revision_id", "sha256"}}
    old["parse_id"] = _hash(_json([source["sha256"], content]).encode())
    (service.root / "parses" / f"{old['parse_id']}.json").write_text(_json(old), encoding="utf-8")
    tampered = dict(good, units=[dict(text="forged source", location={"kind": "pdf", "physical_page": 1})])
    (service.root / "parses" / f"{good['parse_id']}.json").write_text(_json(tampered), encoding="utf-8")
    restored = service.parse(source["revision_id"])
    assert restored == offline
    assert restored["parse_id"] not in (good["parse_id"], old["parse_id"], isolated["parse_id"])
