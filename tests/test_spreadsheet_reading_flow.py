"""结构回执与现有 function_call_output 图片通道的离线集成。"""
import base64
from dataclasses import replace
import json
import socket

import pytest

from agent.knowledge.spreadsheet_materials import SpreadsheetMaterialError
from agent.knowledge.spreadsheet_reading_flow import SpreadsheetReadBudget, prepare_spreadsheet_read
from agent.protocol.tool_media import ToolMediaError, expand_tool_media
from common.personal_tasks import task_scope
from common.runtime_identity import current_identity, identity_scope
from models.config_revision import capture_config_revision
from tests.test_reading_capabilities import ProbeModel
from tests.test_spreadsheet_materials import patch, workbook as workbook


def context(model):
    from common.personal_tasks import current_task_id
    return dict(protocol="responses", model=model.model, provider=model.bot_type,
                api_base=model.bot.get_api_config()["api_base"], identity=current_identity(),
                config_revision=capture_config_revision(), task_id=current_task_id(), transport="inline",
                mime_type="application/pdf")  # 现有通道将此上下文字段固定为 PDF，实际 MIME 由 loader 提供。


def read(workbook, **kwargs):
    return prepare_spreadsheet_read(workbook.parent, workbook.name, **kwargs)


def select(workbook, **kwargs):
    structure = read(workbook, visual=False)
    keys = [i["object_key"] for i in structure.result["images"]]
    return read(workbook, selected_objects=keys[:1], **kwargs), keys


def test_structure_formula_caches_and_visual_false_never_probe(workbook, monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError("不得出网或探测")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("agent.knowledge.reading_capabilities.capability_identity", forbidden)
    before = workbook.read_bytes()
    result = read(workbook, visual=False)
    assert result.status == "success" and not result.media
    assert len(result.result["images"]) == 3 and len(result.result["charts"]) == 1
    formulas = result.result["formulas"]["items"]
    assert formulas[0]["formula"] == "A1+A2" and formulas[0]["cached_value"] == "999"
    assert formulas[0]["cache_status"] == "unverified"
    assert formulas[1]["formula"] == "12345+67890" and formulas[1]["cache_status"] == "missing"
    assert any(g["code"] == "chart_not_rendered" for g in result.result["coverage_gaps"])
    key = result.result["images"][0]["object_key"]
    assert not read(workbook, visual=False, selected_objects=[key], model=object(),
                    media_supports=forbidden, authorize=forbidden).media
    encoded = json.dumps(result.result)
    assert str(workbook.parent) not in encoded and "base64" not in encoded and "material_relpath" not in encoded
    assert "reading_id" not in result.result and result.result["published"] is False
    assert workbook.read_bytes() == before


def test_selected_image_uses_real_tool_media_and_exact_binding(workbook, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("不得出网"))
    model = ProbeModel()
    checked = []
    with identity_scope(user_id="local-owner", session_id="xlsx", run_id="attempt-1"), task_scope("task-x"):
        def authorize(ctx, binding):
            checked.append(binding)
            return binding["task_id"] == "task-x" and binding["identity"].run_id == "attempt-1"
        result, keys = select(workbook, model=model, media_supports=lambda ctx, mime: mime == "image/png", authorize=authorize)
        assert len(result.media) == 1 and len(result.result["media_map"]) == 1
        mapping = result.result["media_map"][0]
        assert mapping["object_key"] == keys[0]
        item = next(i for i in result.result["images"] if i["object_key"] == keys[0])
        assert checked[0]["object"]["anchor"] == item["anchor"]
        assert checked[0]["original_hash"] == result.result["original_hash"]
        payload = dict(input=[dict(type="function_call_output", call_id="xlsx", output=json.dumps(result.result))])
        pending = {"xlsx": result.media}
        delivery = expand_tool_media(payload, pending, context(model))
        assert not pending and len(delivery.refs) == 1
        parts = payload["input"][0]["output"]
        assert parts[0]["type"] == "input_text" and parts[1]["type"] == "input_image"
        raw = base64.b64decode(parts[1]["image_url"].split(",", 1)[1])
        assert raw.startswith(b"\x89PNG")
        from hashlib import sha256
        assert sha256(raw).hexdigest() == item["content_hash"]
        assert result.media[0].on_response is None  # 普通传输不创建长期观察。


@pytest.mark.parametrize("change", ["identity", "task_id", "config_revision", "provider", "model", "api_base", "protocol"])
def test_media_scope_changes_are_rejected(workbook, change):
    model = ProbeModel()
    with identity_scope(user_id="local-owner", session_id="xlsx"):
        result, _ = select(workbook, model=model, media_supports=lambda *a: True, authorize=lambda *a: True)
        ctx = context(model)
        assert result.media[0].supports(ctx)
        ctx[change] = "other"
        assert not result.media[0].supports(ctx)
        with pytest.raises(ToolMediaError):
            result.media[0].loader(ctx)


def test_owner_session_and_attempt_rechecked_before_load(workbook):
    model = ProbeModel()
    state = {"attempt": 1}
    with identity_scope(user_id="local-owner", session_id="xlsx"):
        result, _ = select(workbook, model=model, media_supports=lambda *a: True,
                            authorize=lambda *a: state["attempt"] == 1)
        ctx = context(model)
        assert result.media[0].loader(ctx).data
        state["attempt"] = 2
        with pytest.raises(ToolMediaError):
            result.media[0].loader(ctx)
        state["attempt"] = 1
        with identity_scope(session_id="other"):
            assert not result.media[0].supports(ctx)


@pytest.mark.parametrize("tamper", ["source", "image", "caller_receipt"])
def test_loader_binds_source_hash_and_detaches_mutable_receipt(workbook, tamper):
    model = ProbeModel()
    with identity_scope(user_id="local-owner", session_id="xlsx"):
        result, _ = select(workbook, model=model, media_supports=lambda *a: True, authorize=lambda *a: True)
        if tamper == "source":
            workbook.write_bytes(b"tampered")
        elif tamper == "image":
            next(workbook.parent.glob("spreadsheet_materials/*/objects/*.png")).write_bytes(b"tampered")
        else:
            result.result["images"][0]["anchor"]["from"]["row"] = 999
            result.result["original_hash"] = "forged"
        if tamper == "caller_receipt":
            assert result.media[0].loader(context(model)).data.startswith(b"\x89PNG")
        else:
            with pytest.raises(ToolMediaError, match="media_validation_failed"):
                result.media[0].loader(context(model))


def test_sheet_filter_object_keys_and_formula_pagination(workbook):
    all_items = read(workbook).result["images"]
    shared = [i for i in all_items if i["source_part"] == "xl/media/image1.png"]
    assert len({i["object_key"] for i in shared}) == 2
    result = read(workbook, worksheet="预算", formula_limit=1).result
    assert len(result["images"]) == 2 and result["formulas"]["has_more"]
    assert result["formulas"]["next_offset"] == 1
    next_page = read(workbook, worksheet="预算", formula_offset=1, formula_limit=1).result
    assert next_page["formulas"]["items"][0]["cell"] == "B2" and not next_page["formulas"]["has_more"]
    foreign = next(i["object_key"] for i in shared if i["worksheet"] == "复用")
    with pytest.raises(SpreadsheetMaterialError, match="工作表范围"):
        read(workbook, worksheet="预算", selected_objects=[foreign])
    with pytest.raises(SpreadsheetMaterialError, match="不存在"):
        read(workbook, worksheet="unknown")


def test_chart_selection_and_unknown_image_capability_are_structure_only(workbook):
    initial = read(workbook).result
    chart = initial["charts"][0]
    selected = read(workbook, selected_objects=[chart["object_key"]])
    assert not selected.media and not selected.result["charts"][0]["rendered"]
    result, _ = select(workbook, model=ProbeModel())
    assert not result.media
    assert any(g["code"] == "image_media_capability_or_authorization_unavailable" for g in result.result["coverage_gaps"])
    result, _ = select(workbook, model=ProbeModel(), media_supports=lambda *a: False, authorize=lambda *a: True)
    assert not result.media


@pytest.mark.parametrize("field, value", [("max_media_bytes", 1), ("max_encoded_bytes", 1), ("max_receipt_bytes", 20)])
def test_read_budgets(workbook, field, value):
    with pytest.raises(SpreadsheetMaterialError) as error:
        select(workbook, budget=replace(SpreadsheetReadBudget(), **{field: value}))
    assert error.value.code == "budget_exceeded"


def test_invalid_selections_and_formula_options(workbook):
    key = read(workbook).result["images"][0]["object_key"]
    for options in (dict(selected_objects=[key, key]), dict(selected_objects="all"),
                    dict(selected_objects=["forged"]), dict(formula_offset=-1),
                    dict(formula_limit=True), dict(formula_limit=999), dict(visual="false")):
        with pytest.raises(SpreadsheetMaterialError) as error:
            read(workbook, **options)
        assert error.value.code == "invalid_request"


def test_array_and_shared_formula_attributes_not_expanded(workbook):
    def modify(entries):
        import xml.etree.ElementTree as ET
        from agent.knowledge.spreadsheet_materials import NS
        part = "xl/worksheets/sheet1.xml"
        tree = ET.fromstring(entries[part])
        tree.find(".//s:c[@r='B1']/s:f", NS).attrib.update(t="array", ref="B1:B2")
        formula = tree.find(".//s:c[@r='B2']/s:f", NS)
        formula.attrib.update(t="shared", si="2")
        formula.text = None
        entries[part] = ET.tostring(tree)
    patch(workbook, modify)
    items = read(workbook).result["formulas"]["items"]
    assert items[0]["formula_attributes"] == dict(t="array", ref="B1:B2")
    assert items[1]["formula_attributes"] == dict(t="shared", si="2") and items[1]["formula"] is None


def test_live_model_change_and_callback_failure_are_rechecked(workbook):
    model = ProbeModel()
    state = {"raise": False}
    def authorize(*args):
        if state["raise"]:
            raise ValueError("private data must not escape")
        return True
    with identity_scope(user_id="local-owner", session_id="xlsx"):
        result, _ = select(workbook, model=model, media_supports=lambda *a: True, authorize=authorize)
        ctx = context(model)
        model.model = "changed-model"
        assert not result.media[0].supports(ctx)
        model.model = "offline-model"
        state["raise"] = True
        with pytest.raises(ToolMediaError, match="^attachment_unsupported$"):
            result.media[0].loader(ctx)


def test_selected_object_count_budget(workbook):
    keys = [i["object_key"] for i in read(workbook).result["images"]]
    with pytest.raises(SpreadsheetMaterialError) as error:
        read(workbook, selected_objects=keys, budget=replace(SpreadsheetReadBudget(), max_selected_objects=1))
    assert error.value.code == "budget_exceeded"


def test_unsupported_image_send_format_keeps_structure_and_gap(workbook):
    def modify(entries):
        from io import BytesIO
        import xml.etree.ElementTree as ET
        from PIL import Image
        part = "xl/drawings/_rels/drawing1.xml.rels"
        tree = ET.fromstring(entries[part])
        rel = next(n for n in tree if n.get("Target").endswith("image2.png"))
        rel.set("Target", "../media/image2.bmp")
        raw = BytesIO()
        Image.new("RGB", (3, 3), "blue").save(raw, "BMP")
        entries["xl/media/image2.bmp"] = raw.getvalue()
        del entries["xl/media/image2.png"]
        entries[part] = ET.tostring(tree)
    patch(workbook, modify)
    initial = read(workbook).result
    bmp = next(i for i in initial["images"] if i["media_type"] == "image/bmp")
    result = read(workbook, selected_objects=[bmp["object_key"]])
    assert not result.media
    assert any(g["code"] == "selected_image_media_format_unsupported" for g in result.result["coverage_gaps"])
