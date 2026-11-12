"""Bounded full source extraction; display pagination never enters publication."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from io import BytesIO
from zipfile import ZipFile

PARSER_ID = "personal-source"
PARSER_VERSION = "2"
MAX_BYTES = 10 * 1024 * 1024
MAX_UNITS = 200000
MAX_PAGES = 2000
PPTX_MAX_PACKAGE_BYTES = 200 * 1024 * 1024
PPTX_MAX_PART_BYTES = 32 * 1024 * 1024
PPTX_MAX_PARTS = 20000


class SourceError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def spreadsheet_object_location(item):
    """统一材料对象／缺口的原件位置；cell 仅表示已记录的容器锚点。

    纯位置映射，不构成材料核验或模型观察。组内对象仍保留原始 anchor，
    group_transform_not_applied 缺口由清单携带，不推算最终对象坐标。
    """
    location = {"kind": "xlsx"}
    if item.get("worksheet") is not None:
        location["sheet"] = item["worksheet"]
    for key in ("worksheet_id", "worksheet_part", "drawing_id", "drawing_part", "object_id",
                "object_name", "object_path", "relationship_id", "reference_index", "source_part", "anchor"):
        if key in item:
            location[key] = deepcopy(item[key])
    anchor = item.get("anchor") or {}
    start = anchor.get("from") or {}
    if start.get("cell") is not None:
        location["cell"] = start["cell"]
    return location


def _verified_spreadsheet(materials_root, materials):
    from agent.knowledge.spreadsheet_materials import SpreadsheetMaterialError, verify_spreadsheet_materials

    # Frozen dataclass 内仍有可变 dict；只核验并使用同一独立快照。
    snapshot = deepcopy(materials)
    try:
        return verify_spreadsheet_materials(materials_root, snapshot)
    except SpreadsheetMaterialError as exc:
        code = {"budget_exceeded": "limit_exceeded", "invalid_package": "parse_failed",
                "invalid_request": "invalid_request", "unsupported_format": "invalid_request"}.get(
                    exc.code, "storage_failed")
        raise SourceError(code, str(exc)) from exc


def _check_spreadsheet_source(path, materials):
    try:
        limit = materials.budget["max_source_bytes"]
        with Path(path).open("rb") as stream:
            data = stream.read(limit + 1)
    except OSError as exc:
        raise SourceError("storage_failed", "工作簿原件不可读取") from exc
    if len(data) > limit or sha256(data).hexdigest() != materials.original_hash:
        raise SourceError("storage_failed", "工作簿原件与嵌图材料 hash 不符")


def parse_spreadsheet_source(path, filename=None, *, materials_root, materials,
                             max_bytes=MAX_BYTES, max_units=MAX_UNITS, prepare_only=True):
    """显式绑定 PreparedSpreadsheet 的只读 XLSX 结构解析，不重算或渲染。

    原件可为无扩展 blob；materials_root 下的同 hash 工作材料及全部派生文件
    由 spreadsheet_materials 重新核验。旧 parse_source 的配置、units 和 ordinal
    保持原样，仅此入口增加独立 config、对象 coverage 和准确 gap。
    images/charts 不生成正文 unit；图表缓存仅保存为未验证的源结构。
    """
    if Path(filename or Path(path).name).suffix.lower() != ".xlsx":
        raise SourceError("invalid_request", "嵌图结构解析仅支持 XLSX")
    materials = _verified_spreadsheet(materials_root, materials)
    _check_spreadsheet_source(path, materials)
    payload = parse_source(path, filename, max_bytes=max_bytes, max_units=max_units, prepare_only=prepare_only)
    _check_spreadsheet_source(path, materials)
    payload["config"].update(xlsx_structure="xlsx-embedded-structure-v1",
                             spreadsheet_manifest_hash=materials.manifest_hash)
    coverage = []
    for kind, items in (("image", materials.images), ("chart", materials.charts)):
        for item in items:
            reason = "chart_not_rendered" if kind == "chart" else (
                "native_reading_pending" if item["material_relpath"] else "unsupported_image_format")
            coverage.append(dict(deepcopy(item), kind=kind, location=spreadsheet_object_location(item),
                                 rendered=False, ocr=False, visual_interpretation=False, status="pending",
                                 unprocessed_reason=reason))
    payload["visual_coverage"] = coverage
    # 用受信关系定位替代旧的文件名枚举；不能声称每个 media 部件都有单元格出处。
    payload["visual_warnings"] = [dict(kind="spreadsheet_gap", code=gap["code"], reason=gap["code"],
        location=spreadsheet_object_location(gap["location"]), status="pending") for gap in materials.coverage_gaps]
    payload["spreadsheet_calculation"] = deepcopy(materials.calculation)
    return payload


def read_spreadsheet_object(materials_root, materials, location):
    """按完整位置回查源对象；materials 应来自任务已固定的来源清单。

    重新核验后返回源位置和材料元数据，不生成视觉证据；来源修订及原件
    hash 的绑定仍由 flow 的可信台账承担。
    """
    materials = _verified_spreadsheet(materials_root, materials)
    for kind, items in (("image", materials.images), ("chart", materials.charts)):
        for item in items:
            if spreadsheet_object_location(item) == location:
                return dict(object_kind=kind, location=spreadsheet_object_location(item), source_object=deepcopy(item),
                            original_hash=materials.original_hash, manifest_hash=materials.manifest_hash)
    raise SourceError("invalid_request", "该位置不属于已核验工作簿对象")


def parse_source(path, filename=None, *, max_bytes=MAX_BYTES, max_units=MAX_UNITS,
                 render_budget=None, prepare_only=False):
    """Return all text with genuine positions, or fail without a partial result."""
    path = Path(path)
    ext = Path(filename or path.name).suffix.lower()
    units, size = [], 0
    visual_coverage, render_config = [], None
    visual_warnings = []

    def add(text, location, **metadata):
        nonlocal size
        size += len(text.encode("utf-8"))
        if size > max_bytes or len(units) >= max_units:
            raise SourceError("limit_exceeded", "解析预算超限；未发布截断文本")
        if text.strip():
            units.append(dict(text=text, location=location, **metadata))

    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(path)
            if reader.is_encrypted:
                raise SourceError("parse_failed", "PDF 已加密")
            if len(reader.pages) > MAX_PAGES:
                raise SourceError("limit_exceeded", "PDF 物理页数超限")
            for number, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                add(text, {"kind": "pdf", "physical_page": number, "type": "text"}, type="text")
            if prepare_only:
                text_pages = {u["location"]["physical_page"] for u in units}
                visual_coverage = [dict(physical_page=p, text=p in text_pages,
                    rendered=False, ocr=False, visual_interpretation=False, status="pending",
                    unprocessed_reason="native_reading_pending") for p in range(1, len(reader.pages) + 1)]
            else:
                from agent.knowledge.visual import inspect_pdf, RENDER_BUDGET
                render_config = dict(RENDER_BUDGET, **(render_budget or {}))
                visual_coverage = inspect_pdf(path, len(reader.pages), units, budget=render_budget)
        elif ext == ".docx":
            from docx import Document
            from common.office_text import iter_docx_source_units
            for unit in iter_docx_source_units(Document(path)):
                add(**unit)
        elif ext == ".pptx":
            from pptx import Presentation
            from common.office_text import iter_pptx_source_objects

            # 只对新格式增加读取边界，不改变旧格式解析配置与身份。
            with ZipFile(path) as package:
                parts = package.infolist()
                if (len(parts) > PPTX_MAX_PARTS or sum(p.file_size for p in parts) > PPTX_MAX_PACKAGE_BYTES
                        or any(p.file_size > PPTX_MAX_PART_BYTES for p in parts)):
                    raise SourceError("limit_exceeded", "PPTX 包／部件预算超限")
                if len({p.filename for p in parts}) != len(parts):
                    raise SourceError("parse_failed", "PPTX 包存在重复部件身份")
            presentation = Presentation(path)
            # slides 属性会按当前顺序 rename_slide_parts；受控出处不能采用重命名后的路径。
            listing = presentation._element.sldIdLst
            if listing is None or not len(listing):
                raise SourceError("parse_failed", "PPTX 无幻灯片")
            if len(listing) > MAX_PAGES:
                raise SourceError("limit_exceeded", "PPTX 幻灯片数量超限")
            slides_seen, parts_seen, object_count = set(), set(), 0
            for number, entry in enumerate(listing, 1):
                slide = presentation.part.related_slide(entry.rId)
                slide_id, part = str(entry.id), str(slide.part.partname).lstrip("/")
                if slide_id in slides_seen or part in parts_seen:
                    raise SourceError("parse_failed", "PPTX 幻灯片身份重复")
                slides_seen.add(slide_id)
                parts_seen.add(part)
                show = slide._element.get("show", "1")
                if show not in ("0", "1", "true", "false"):
                    raise SourceError("parse_failed", "PPTX 隐藏标记无效")
                hidden = show in ("0", "false")
                base = dict(kind="slide", source_slide_id=slide_id, source_slide_number=number, source_part=part)
                coverage = dict(base, hidden=hidden, objects=[], text=False, notes_text=False, rendered=False,
                    ocr=False, visual_interpretation=False, status="pending",
                    unprocessed_reason="hidden_slide_not_rendered" if hidden else "native_reading_pending")
                visual_coverage.append(coverage)
                layers = [("slide", slide.shapes, part)]
                # has_notes_slide 不创建不存在的备注页；只读取已有备注部件。
                if slide.has_notes_slide:
                    notes = slide.notes_slide
                    layers.append(("speaker_notes", notes.shapes, str(notes.part.partname).lstrip("/")))
                for layer, shapes, source_part in layers:
                    object_ids = set()
                    for event in iter_pptx_source_objects(shapes, layer=layer):
                        location = dict(base, source_part=source_part, **event["location"])
                        if event["event"] == "object":
                            object_count += 1
                            object_id = event["location"]["object_id"]
                            if object_id in object_ids:
                                raise SourceError("parse_failed", "PPTX 同一层的对象 ID 重复")
                            object_ids.add(object_id)
                            if object_count > max_units:
                                raise SourceError("limit_exceeded", "PPTX 对象数量超限")
                            coverage["objects"].append(dict(location=location, layer=layer,
                                                             object_type=event["object_type"]))
                        elif event["event"] == "text":
                            add(event["text"], location, type="speaker_notes" if layer == "speaker_notes" else "text",
                                origin="source", layer=layer, hidden=hidden)
                            if event["text"].strip():
                                coverage["notes_text" if layer == "speaker_notes" else "text"] = True
                        else:
                            gap = {k: v for k, v in event.items() if k not in ("event", "location")}
                            visual_warnings.append(dict(gap, location=location, layer=layer, hidden=hidden,
                                                        status="pending"))
                if hidden:
                    visual_warnings.append(dict(kind="hidden_slide", part=part, location=base, layer="slide",
                        hidden=True, status="pending", reason="hidden_slide_not_rendered"))
                if slide._element.find("{http://schemas.openxmlformats.org/presentationml/2006/main}timing") is not None:
                    visual_warnings.append(dict(kind="animation", part=part, location=base, layer="slide",
                        hidden=hidden, status="pending", reason="animation_not_rendered"))
        elif ext == ".xlsx":
            from openpyxl import load_workbook
            # Content-addressed originals intentionally have no extension.
            # Passing a stream also avoids openpyxl's filename extension gate.
            source_bytes = path.read_bytes()
            cached = load_workbook(BytesIO(source_bytes), read_only=True, data_only=True, keep_links=False)
            try:
                source = load_workbook(BytesIO(source_bytes), read_only=True, data_only=False, keep_links=False)
                try:
                    count = 0
                    for sheet in source:
                        values = cached[sheet.title]
                        sheet.reset_dimensions()
                        values.reset_dimensions()
                        for row, cached_row in zip(sheet.iter_rows(), values.iter_rows()):
                            for cell, value in zip(row, cached_row):
                                count += 1
                                if count > max_units:
                                    raise SourceError("limit_exceeded", "工作表单元格预算超限")
                                if cell.value is None:
                                    continue
                                formula = str(getattr(cell.value, "text", cell.value)) if cell.data_type == "f" else None
                                text = str(cell.value)
                                if formula:
                                    text = f"公式 {formula}；缓存值：{value.value if value.value is not None else '无可用值（未重算）'}"
                                add(text, {"kind": "xlsx", "sheet": sheet.title, "cell": cell.coordinate},
                                    formula=formula, cached_value=value.value if formula else None)
                finally:
                    source.close()
            finally:
                cached.close()
        elif ext in (".txt", ".md"):
            if path.stat().st_size > max_bytes:
                raise SourceError("limit_exceeded", "文本大小超限")
            for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
                add(line, {"kind": "text", "line": number})
        else:
            raise SourceError("invalid_request", f"不支持的资料格式：{ext}")
        if ext in (".docx", ".xlsx"):
            prefix = "word" if ext == ".docx" else "xl"
            with ZipFile(path) as package:
                for part in sorted(set(package.namelist())):
                    if part.endswith("/"):
                        continue
                    for folder, kind in (("media", "image"), ("charts", "chart")):
                        if part.startswith(f"{prefix}/{folder}/") and "/_rels/" not in part:
                            visual_warnings.append(dict(kind=kind, part=part, status="pending",
                                                        reason="visual_not_supported"))
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError("parse_failed", f"资料解析失败：{type(exc).__name__}") from exc
    if not units and not visual_coverage and not visual_warnings:
        raise SourceError("needs_ocr" if ext == ".pdf" else "parse_failed", "未提取到可定位文本")
    config = {"max_bytes": max_bytes, "max_units": max_units, "max_pages": MAX_PAGES}
    if ext == ".pptx":
        config.update(pptx_structure="pptx-structure-v1", max_package_bytes=PPTX_MAX_PACKAGE_BYTES,
                      max_part_bytes=PPTX_MAX_PART_BYTES, max_parts=PPTX_MAX_PARTS)
    if prepare_only:
        config["preparation"] = "native-pptx-v1" if ext == ".pptx" else "native-pdf-v1"
    if render_config is not None:
        config["render"] = render_config
    return {"parser_id": PARSER_ID, "parser_version": PARSER_VERSION,
            "config": config,
            "units": units, "visual_coverage": visual_coverage, "visual_warnings": visual_warnings}
