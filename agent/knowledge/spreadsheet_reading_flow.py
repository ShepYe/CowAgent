"""Read 的 XLSX 结构／可选图片准备入口，不调用模型或登记长期观察。

prepare_spreadsheet_read(root, relative, worksheet=None, selected_objects=(),
    visual=True, model=None, media_supports=None, authorize=None, ...)
返回 ToolResult：result 是 JSON 结构，media 直接交既有 ToolResult 媒体通道。
先结构读取取得 images/charts[].object_key，再用 selected_objects=[key] 补读。
worksheet 是精确名称；公式用 formula_offset/formula_limit（零基）分页，区域选择由
Read 主入口负责。formula/value 都是原包文字；共享／数组公式属性原样保留，不展开计算。

图片发送要求两个受信回调：media_supports(context, mime_type) is True 核对实际图片
能力（不能借用 PDF 探针）；authorize(context, binding) is True 在创建及每次展开时核对
文件权限、owner/session/task/attempt 和来源有效性。binding 包含原件/清单 hash、
精确对象位置及运行身份；任务 attempt 由回调按当前任务台账核验。本模块不保存 attempt。
调用方已完成 root/原件访问授权；传入回调不能来自模型参数。未提供回调则只返回结构缺口。
长期图像观察、reading_id、发布和 parser 由各自模块负责，此处不建立新契约。
"""
import copy
from dataclasses import asdict, dataclass
from pathlib import Path

from agent.knowledge.spreadsheet_materials import (
    NS, R, S, SpreadsheetBudget, SpreadsheetMaterialError, _Package, _digest, _hash,
    _json, _path, _read, load_spreadsheet_materials, prepare_spreadsheet_materials,
)
from agent.protocol.tool_media import ToolMediaContent, ToolMediaError, ToolMediaReference
from agent.tools.base_tool import ToolResult


@dataclass(frozen=True)
class SpreadsheetReadBudget:
    max_selected_objects: int = 8
    max_media_bytes: int = 8 * 1024 * 1024
    max_encoded_bytes: int = 12 * 1024 * 1024
    max_formula_items: int = 200
    max_receipt_bytes: int = 1024 * 1024


def _invalid(message):
    raise SpreadsheetMaterialError("invalid_request", message)


def _formula_page(root, materials, worksheet, offset, limit):
    source = _read(_path(root, materials.original_relpath), materials.budget["max_source_bytes"])
    if _hash(source) != materials.original_hash:
        raise SpreadsheetMaterialError("source_changed", "原件 hash 已改变")
    package = _Package(source, SpreadsheetBudget(**materials.budget))
    try:
        origin = next(v["target"] for v in package.relations[""].values() if v["type"] == R + "/officeDocument")
        tree = package.xml(origin)
        sheets, formulas, total = [], [], 0
        for sheet in tree.findall("s:sheets/s:sheet", NS):
            name = sheet.attrib["name"]
            part = package.relation(origin, sheet.attrib[f"{{{R}}}id"], "worksheet")["target"]
            sheets.append(dict(worksheet=name, worksheet_id=sheet.attrib["sheetId"], worksheet_part=part,
                               state=sheet.get("state", "visible")))
            if worksheet is not None and worksheet != name:
                continue
            for cell in package.xml(part).findall(".//s:sheetData/s:row/s:c", NS):
                formula = cell.find(f"{{{S}}}f")
                if formula is None:
                    continue
                if offset <= total < offset + limit:
                    value = cell.find(f"{{{S}}}v")
                    formulas.append(dict(worksheet=name, worksheet_part=part, cell=cell.get("r"),
                                         formula=formula.text, formula_attributes=dict(formula.attrib),
                                         cached_value=value.text if value is not None else None,
                                         cached_type=cell.get("t", "n"), recalculated=False,
                                         cache_status="missing" if value is None or value.text is None else "unverified"))
                total += 1
        if worksheet is not None and worksheet not in {s["worksheet"] for s in sheets}:
            _invalid("工作表名称不存在")
        end = min(offset + len(formulas), total)
        return sheets, dict(items=formulas, total=total, offset=offset, limit=limit,
                            has_more=end < total, next_offset=end if end < total else None,
                            recalculated=False, cache_freshness="unverified")
    finally:
        package.archive.close()


def _object_key(materials, kind, item):
    return _digest(dict(original_hash=materials.original_hash, kind=kind,
                        location={key: item.get(key) for key in (
                            "worksheet", "worksheet_part", "drawing_part", "drawing_id", "object_id",
                            "object_path", "relationship_id", "reference_index", "source_part", "anchor")},
                        content_hash=item["content_hash"]))


def _entry(materials, kind, item):
    result = copy.deepcopy(item)
    result.pop("material_relpath", None)  # 历史只保存结构，不暴露运行期文件路径。
    result.update(object_key=_object_key(materials, kind, item), original_hash=materials.original_hash,
                  manifest_hash=materials.manifest_hash, kind=kind,
                  media_attached=False, media_eligible=bool(kind == "image" and item["material_relpath"]
                      and item.get("media_type") in ("image/png", "image/jpeg", "image/gif", "image/webp")))
    return result


def prepare_spreadsheet_read(root, original_relpath, *, worksheet=None, selected_objects=(),
                             visual=True, model=None, media_supports=None, authorize=None,
                             formula_offset=0, formula_limit=100, budget=None, material_budget=None):
    """离线准备；不发送请求。超预算／非法选择抛 SpreadsheetMaterialError。

    selected_objects 为结构回执的 object_key 字符串列表，不接受裸 object_id，避免跨 sheet
    碰撞。visual=false 即使指定选择也只给结构；空选择不自动发送所有图片。
    """
    budget = SpreadsheetReadBudget() if budget is None else budget
    if not isinstance(budget, SpreadsheetReadBudget) or any(type(v) is not int or v <= 0 for v in asdict(budget).values()):
        _invalid("读取预算必须是正整数 SpreadsheetReadBudget")
    if type(visual) is not bool or worksheet is not None and (not isinstance(worksheet, str) or not worksheet):
        _invalid("visual 或 worksheet 无效")
    if (type(formula_offset) is not int or formula_offset < 0 or type(formula_limit) is not int
            or not 1 <= formula_limit <= budget.max_formula_items):
        _invalid("公式分页超出预算")
    if not isinstance(selected_objects, (tuple, list)) or any(not isinstance(k, str) or not k for k in selected_objects):
        _invalid("selected_objects 须是 object_key 列表")
    if len(set(selected_objects)) != len(selected_objects):
        _invalid("不能重复选择同一对象")
    if len(selected_objects) > budget.max_selected_objects:
        raise SpreadsheetMaterialError("budget_exceeded", "选择对象数量超限")
    root = Path(root).resolve()
    materials = prepare_spreadsheet_materials(root, original_relpath, budget=material_budget)
    sheets, formulas = _formula_page(root, materials, worksheet, formula_offset, formula_limit)
    indexed, images, charts = {}, [], []
    for kind, items, output in (("image", materials.images, images), ("chart", materials.charts, charts)):
        for item in items:
            if worksheet is not None and worksheet != item["worksheet"]:
                continue
            entry = _entry(materials, kind, item)
            indexed[entry["object_key"]] = (item, entry)
            output.append(entry)
    if any(key not in indexed for key in selected_objects):
        _invalid("选择对象不在当前原件或工作表范围内")
    gaps = copy.deepcopy(list(materials.coverage_gaps))
    refs, media_map = [], []
    selected = [indexed[key] for key in selected_objects]
    for _, entry in selected:
        if visual and entry["kind"] == "image" and not entry["media_eligible"]:
            gaps.append(dict(code="selected_image_media_format_unsupported",
                             location=dict(object_key=entry["object_key"], media_type=entry.get("media_type"))))
    eligible = [(item, entry) for item, entry in selected if entry["media_eligible"]]
    if visual:
        raw_bytes = sum(item["byte_count"] for item, _ in eligible)
        encoded_bytes = sum(4 * ((item["byte_count"] + 2) // 3) + 128 for item, _ in eligible)
        if raw_bytes > budget.max_media_bytes or encoded_bytes > budget.max_encoded_bytes:
            raise SpreadsheetMaterialError("budget_exceeded", "图片原始或 base64 字节预算超限")
    if visual and eligible:
        from agent.knowledge.reading_capabilities import capability_identity
        from common.personal_tasks import current_task_id
        from common.runtime_identity import current_identity
        from models.config_revision import capture_config_revision, require_config_revision
        revision, identity, task_id = capture_config_revision(), current_identity(), current_task_id()
        # 只复用路由身份；PDF 支持记录不能代表图片能力。实际图片能力由回调逐 MIME 复核。
        provider = capability_identity(model, revision) if model is not None else None
        if provider is None or not callable(media_supports) or not callable(authorize):
            gaps.append(dict(code="image_media_capability_or_authorization_unavailable", location={}))
        else:
            manifest = materials.to_dict()
            manifest_hash = materials.manifest_hash
            for item, entry in eligible:
                binding = dict(original_relpath=original_relpath, original_hash=materials.original_hash,
                               manifest_hash=manifest_hash, object=copy.deepcopy(entry), identity=identity,
                               task_id=task_id, config_revision=revision)

                def supports(context, binding=binding, mime=item["media_type"]):
                    try:
                        return (context.get("protocol") == "responses" and context.get("model") == provider["model"]
                            and context.get("provider") == provider["provider"]
                            and context.get("transport", "inline") == "inline"
                            and context.get("config_revision") == revision and context.get("identity") == identity
                            and context.get("task_id") == task_id
                            and current_identity() == identity and current_task_id() == task_id
                            and _hash(str(context.get("api_base") or "").rstrip("/").encode()) == provider["endpoint_hash"]
                            and capability_identity(model, revision) == provider
                            and media_supports(dict(context), mime) is True
                            and authorize(dict(context), copy.deepcopy(binding)) is True)
                    except Exception:
                        return False

                context = dict(protocol="responses", model=provider["model"], provider=provider["provider"],
                               api_base=getattr(model, "bot").get_api_config().get("api_base"), transport="inline",
                               config_revision=revision, identity=identity, task_id=task_id)
                if not supports(context):
                    gaps.append(dict(code="image_media_not_authorized_or_supported", location=dict(object_key=entry["object_key"])))
                    continue

                def load(context, item=copy.deepcopy(item), supports=supports):
                    if not supports(context):
                        raise ToolMediaError("attachment_unsupported")
                    try:
                        require_config_revision(revision)
                        load_spreadsheet_materials(root, manifest, expected_manifest_hash=manifest_hash)
                        payload = _read(_path(root, item["material_relpath"]), budget.max_media_bytes)
                        if _hash(payload) != item["content_hash"]:
                            raise ToolMediaError("media_validation_failed")
                        # 权限/配置可在材料核验期间改变，返回字节前再次核对。
                        require_config_revision(revision)
                        if not supports(context):
                            raise ToolMediaError("attachment_unsupported")
                        return ToolMediaContent(item["media_type"], payload)
                    except ToolMediaError:
                        raise
                    except Exception:
                        raise ToolMediaError("media_validation_failed") from None

                media_map.append(dict(media_index=len(refs), object_key=entry["object_key"],
                                      original_hash=materials.original_hash, manifest_hash=manifest_hash,
                                      location={k: copy.deepcopy(item[k]) for k in (
                                          "worksheet", "worksheet_part", "drawing_part", "drawing_id", "object_id", "anchor")}))
                refs.append(ToolMediaReference(load, supports))
                entry["media_attached"] = True
    result = dict(format="xlsx", preparation_version="xlsx-read-flow-v1", original_hash=materials.original_hash,
                  manifest_hash=materials.manifest_hash, worksheets=sheets, worksheet=worksheet,
                  images=images, charts=charts, formulas=formulas, calculation=copy.deepcopy(materials.calculation),
                  coverage_gaps=gaps, selected_objects=list(selected_objects), media_map=media_map,
                  media_attached=bool(refs), published=False, searchable=False)
    if len(_json(result).encode("utf-8")) > budget.max_receipt_bytes:
        raise SpreadsheetMaterialError("budget_exceeded", "结构回执字节预算超限")
    return ToolResult.success(result, media=tuple(refs))
