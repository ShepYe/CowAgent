"""XLSX 离线嵌图材料；直接读取包关系，不加载 Excel、不计算或访问网络。

prepare_spreadsheet_materials(root, original_relpath, budget=SpreadsheetBudget())
返回 PreparedSpreadsheet；images/charts 中的每一项代表一个源对象，同图多处使用
保留各自位置并共享内容文件。from/to 的 row/column 为零基，偏移和尺寸均为 EMU。
load_spreadsheet_materials(root, manifest, expected_manifest_hash=可信台账哈希)
支持异根恢复；prepare/verify/load 均不登记 parser 或模型阅读证据。

coverage_gaps 是 {code, location} 列表。无单元格锚点、孤立媒体、EMF 等格式、
图表未渲染及缓存新鲜度未知均明确登记；不推测布局或重算缓存。图片只保存原字节，
不解码、转码或证明像素完整性。组内对象保留容器锚点并登记未应用组变换的缺口。
错误通过 SpreadsheetMaterialError.code 区分 invalid_request/invalid_path/
unsupported_format/invalid_package/budget_exceeded/source_changed/invalid_manifest/
material_tampered/material_unavailable/cache_invalid。失败不自动重建已发布材料。
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import posixpath
import tempfile
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile
import zlib


VERSION = "xlsx-embedded-materials-v1"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
NS = dict(s=S, xdr=XDR, a=A, c=C)


class SpreadsheetMaterialError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class SpreadsheetBudget:
    """硬上限：超限整体失败，不把截断清单当成完整结果。bytes 为解压后字节。

    max_objects 计算引用次数，含重复图片、图表及未挂接媒体；材料字节按去重后计。
    """
    max_source_bytes: int = 32 * 1024 * 1024
    max_package_bytes: int = 128 * 1024 * 1024
    max_parts: int = 4096
    max_part_bytes: int = 32 * 1024 * 1024
    max_objects: int = 512
    max_material_bytes: int = 32 * 1024 * 1024
    max_metadata_bytes: int = 8 * 1024 * 1024


@dataclass(frozen=True)
class PreparedSpreadsheet:
    schema_version: int
    preparation_version: str
    original_relpath: str
    original_hash: str
    budget: dict
    cache_key: str
    images: tuple
    charts: tuple
    calculation: dict
    coverage_gaps: tuple

    @property
    def manifest_hash(self):
        return _digest(asdict(self))

    def to_dict(self):
        return dict(json.loads(_json(asdict(self))), manifest_hash=self.manifest_hash)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(data):
    return sha256(data).hexdigest()


def _digest(value):
    return _hash(_json(value).encode("utf-8"))


def _fail(code, message):
    raise SpreadsheetMaterialError(code, message)


def _canonical(name):
    if (not isinstance(name, str) or not name or any(c in name for c in "\\:%?#\x00")
            or any(ord(c) < 32 for c in name)
            or any(p in ("", ".", "..") or p.endswith((" ", ".")) for p in name.split("/"))):
        _fail("invalid_path", "路径必须是根内规范相对路径")
    return name


def _path(root, name):
    _canonical(name)
    root = Path(root).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        _fail("invalid_path", "材料路径越出数据根")
    return path


def _budget(value):
    if not isinstance(value, SpreadsheetBudget) or any(type(v) is not int or v <= 0 for v in asdict(value).values()):
        _fail("invalid_request", "预算必须是正整数 SpreadsheetBudget")
    return value


def _read(path, limit):
    with path.open("rb") as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        _fail("budget_exceeded", "文件字节预算超限")
    return payload


class _Package:
    def __init__(self, data, budget):
        self.archive = zipfile.ZipFile(BytesIO(data))
        infos = self.archive.infolist()
        if len(infos) > budget.max_parts or sum(i.file_size for i in infos) > budget.max_package_bytes:
            _fail("budget_exceeded", "包部件数量或解压字节预算超限")
        self.names = set()
        case_names = set()
        for info in infos:
            name = info.filename.rstrip("/") if info.is_dir() else info.filename
            _canonical(name)
            if name.casefold() in case_names or info.flag_bits & 1:
                _fail("invalid_package", "重复部件或加密包不支持")
            case_names.add(name.casefold())
            if info.file_size > budget.max_part_bytes:
                _fail("budget_exceeded", "单部件字节预算超限")
            if not info.is_dir():
                self.names.add(name)
        # 验证所有 XML、CRC 和关系，不让未引用坏部件悄悄通过。
        self.trees = {}
        for name in sorted(self.names):
            payload = self.archive.read(name)
            if name.endswith((".xml", ".rels")):
                # UTF-16/32 也检查；OOXML 不需要 DTD 或实体声明。
                probe = payload.replace(b"\x00", b"").upper()
                if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
                    _fail("invalid_package", "XML 不允许 DTD 或实体声明")
                self.trees[name] = ET.fromstring(payload)
        self.relations = {}
        for name in sorted(self.names):
            if name.endswith(".rels"):
                if name == "_rels/.rels":
                    part = ""
                elif "/_rels/" in name:
                    folder, base = name.rsplit("/_rels/", 1)
                    part = folder + "/" + base[:-5]
                else:
                    _fail("invalid_package", "关系部件位置无效")
                rels = {}
                tree = self.xml(name)
                if tree.tag != f"{{{PKG}}}Relationships":
                    _fail("invalid_package", "关系命名空间不支持")
                for node in tree:
                    rid, target, kind = (node.attrib[k] for k in ("Id", "Target", "Type"))
                    if rid in rels or not rid or node.tag != f"{{{PKG}}}Relationship":
                        _fail("invalid_package", "关系 ID 重复或无效")
                    external = node.get("TargetMode") == "External"
                    if node.get("TargetMode") not in (None, "Internal", "External"):
                        _fail("invalid_package", "关系模式无效")
                    if not external:
                        parsed = urlsplit(target)
                        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or "\\" in target:
                            _fail("invalid_path", "内部关系目标不是包路径")
                        decoded = unquote(target)
                        resolved = posixpath.normpath(decoded.lstrip("/") if decoded.startswith("/") else
                                                      posixpath.join(posixpath.dirname(part), decoded))
                        _canonical(resolved)
                        if resolved not in self.names:
                            _fail("invalid_package", "关系目标不存在：" + resolved)
                        target = resolved
                    rels[rid] = dict(target=target, type=kind, external=external)
                self.relations[part] = rels

    def xml(self, part):
        if part not in self.trees:
            _fail("invalid_package", "缺少 XML 部件：" + part)
        return self.trees[part]

    def relation(self, part, rid, kind):
        value = self.relations.get(part, {}).get(rid)
        if value is None or value["type"] != R + "/" + kind:
            _fail("invalid_package", "缺少或错误的 " + kind + " 关系")
        return value


def _number(value, *, signed=False):
    digits = value.removeprefix("-") if signed and isinstance(value, str) else value
    if digits is None or not digits.isascii() or not digits.isdigit():
        _fail("invalid_package", "锚点整数无效")
    return int(value)


def _cell(marker):
    values = {key: _number(marker.findtext(f"{{{XDR}}}{tag}"), signed=tag.endswith("Off"))
              for key, tag in (("row", "row"), ("column", "col"), ("row_offset_emu", "rowOff"), ("column_offset_emu", "colOff"))}
    if values["row"] >= 1048576 or values["column"] >= 16384:
        _fail("invalid_package", "单元格锚点越界")
    column, label = values["column"] + 1, ""
    while column:
        column, remainder = divmod(column - 1, 26)
        label = chr(65 + remainder) + label
    return dict(values, cell=label + str(values["row"] + 1))


def _anchor(node):
    kind = node.tag.removeprefix("{" + XDR + "}")
    result = dict(kind=kind, **{"from": None, "to": None}, extent_emu=None, position_emu=None)
    for key in ("from", "to"):
        marker = node.find(f"{{{XDR}}}{key}")
        if marker is not None:
            result[key] = _cell(marker)
    for tag, key, attrs in (("ext", "extent_emu", ("cx", "cy")), ("pos", "position_emu", ("x", "y"))):
        child = node.find(f"{{{XDR}}}{tag}")
        if child is not None:
            result[key] = {attr: _number(child.get(attr), signed=tag == "pos") for attr in attrs}
    result["edit_as"] = node.get("editAs")
    return result


def _structure(node):
    return dict(tag=node.tag, attributes=dict(node.attrib), text=node.text or "",
                children=[_structure(child) for child in node])


def _drawing_objects(parent, path, grouped=False):
    for index, obj in enumerate(parent, 1):
        current = path + f"/*[{index}]"
        if obj.tag == f"{{{XDR}}}grpSp":
            yield from _drawing_objects(obj, current, grouped=True)
        elif obj.tag in {f"{{{XDR}}}{kind}" for kind in ("pic", "sp", "cxnSp", "graphicFrame")}:
            yield obj, current, grouped


def _chart_series(chart):
    if chart.tag != f"{{{C}}}chartSpace":
        _fail("invalid_package", "chart 部件类型错误")
    return [dict(references=[n.text or "" for n in series.findall(".//c:f", NS)],
                 caches=[_structure(n) for n in series.iter() if n.tag in {
                     f"{{{C}}}numCache", f"{{{C}}}strCache", f"{{{C}}}multiLvlStrCache",
                     f"{{{C}}}numLit", f"{{{C}}}strLit"}]) for series in chart.findall(".//c:ser", NS)]


def _image_format(payload, part):
    suffix = Path(part).suffix.lower()
    signatures = ((".png", "image/png", payload.startswith(b"\x89PNG\r\n\x1a\n")),
                  (".jpg", "image/jpeg", payload.startswith(b"\xff\xd8\xff")),
                  (".gif", "image/gif", payload.startswith((b"GIF87a", b"GIF89a"))),
                  (".bmp", "image/bmp", payload.startswith(b"BM")),
                  (".tiff", "image/tiff", payload.startswith((b"II*\x00", b"MM\x00*"))))
    suffix = {".jpeg": ".jpg", ".tif": ".tiff"}.get(suffix, suffix)
    for ext, mime, matches in signatures:
        if suffix == ext:
            if not matches:
                _fail("invalid_package", "图片格式与字节签名不匹配：" + part)
            return ext, mime
    return None, None


def _extract(data, original_relpath, budget):
    identity = dict(preparation_version=VERSION, original_relpath=original_relpath,
                    original_hash=_hash(data), budget=asdict(budget))
    key = _digest(identity)
    base = "spreadsheet_materials/" + key
    images, charts, gaps, assets = [], [], [], {}
    count = 0
    metadata_bytes = 0

    def metadata(value):
        nonlocal metadata_bytes
        metadata_bytes += len(_json(value).encode("utf-8"))
        if metadata_bytes > budget.max_metadata_bytes:
            _fail("budget_exceeded", "锚点／缓存元数据字节预算超限")

    def gap(code, location):
        value = dict(code=code, location=location)
        metadata(value)
        gaps.append(value)

    def account():
        nonlocal count
        count += 1
        if count > budget.max_objects:
            _fail("budget_exceeded", "对象数量预算超限")

    def asset(payload, ext):
        path = base + "/objects/" + _hash(payload) + ext
        assets[path] = payload
        if sum(map(len, assets.values())) > budget.max_material_bytes:
            _fail("budget_exceeded", "材料字节预算超限")
        return dict(content_hash=_hash(payload), material_relpath=path, byte_count=len(payload))

    package = None
    try:
        package = _Package(data, budget)
        origins = [v for v in package.relations.get("", {}).values() if v["type"] == R + "/officeDocument"]
        if len(origins) != 1 or origins[0]["external"]:
            _fail("invalid_package", "缺少唯一内部 workbook 关系")
        workbook_part = origins[0]["target"]
        workbook = package.xml(workbook_part)
        if workbook.tag != f"{{{S}}}workbook":
            _fail("invalid_package", "不支持的工作簿格式")
        types = package.xml("[Content_Types].xml")
        if any("macroEnabled" in n.get("ContentType", "") for n in types):
            _fail("unsupported_format", "宏格式不属于 XLSX 材料范围")
        for part, rels in sorted(package.relations.items()):
            for rid, rel in rels.items():
                if rel["external"]:
                    gap("external_relationship_not_accessed", dict(source_part=part, relationship_id=rid))
        calculation = dict(recalculated=False, external_links_refreshed=False, cache_freshness="unverified",
                           workbook_properties=dict(workbook.find(f"{{{S}}}calcPr").attrib)
                           if workbook.find(f"{{{S}}}calcPr") is not None else {},
                           formula_cells=0, missing_formula_caches=0)
        gap("formula_and_chart_cache_freshness_unverified", dict(source_part=workbook_part))
        used_parts, sheet_ids, sheet_names = set(), set(), set()
        sheets = workbook.findall("s:sheets/s:sheet", NS)
        if not sheets:
            _fail("invalid_package", "工作簿没有工作表")
        for sheet in sheets:
            sid, name = sheet.attrib["sheetId"], sheet.attrib["name"]
            if sid in sheet_ids or name in sheet_names:
                _fail("invalid_package", "工作表身份重复")
            sheet_ids.add(sid)
            sheet_names.add(name)
            rel = package.relation(workbook_part, sheet.attrib[f"{{{R}}}id"], "worksheet")
            if rel["external"]:
                _fail("invalid_package", "工作表不能是外链")
            part = rel["target"]
            tree = package.xml(part)
            if tree.tag != f"{{{S}}}worksheet":
                _fail("invalid_package", "工作表部件类型错误")
            sheet_location = dict(worksheet=name, worksheet_id=sid, worksheet_part=part)
            for cell in tree.findall(".//s:sheetData/s:row/s:c", NS):
                if cell.find("s:f", NS) is not None:
                    calculation["formula_cells"] += 1
                    value = cell.find("s:v", NS)
                    if value is None or value.text is None:
                        calculation["missing_formula_caches"] += 1
            for legacy in tree.findall("s:legacyDrawing", NS):
                gap("legacy_drawing_not_processed", dict(sheet_location, relationship_id=legacy.get(f"{{{R}}}id")))
            for drawing in tree.findall("s:drawing", NS):
                drid = drawing.attrib[f"{{{R}}}id"]
                rel = package.relation(part, drid, "drawing")
                if rel["external"]:
                    gap("external_drawing_not_accessed", dict(sheet_location, drawing_id=drid))
                    continue
                dpart = rel["target"]
                dtree = package.xml(dpart)
                if dtree.tag != f"{{{XDR}}}wsDr":
                    _fail("invalid_package", "drawing 部件类型错误")
                ids = set()
                for index, container in enumerate(dtree, 1):
                    anchor = _anchor(container)
                    for obj, object_path, grouped in _drawing_objects(container, f"/*/*[{index}]"):
                        refs = list(obj.findall(".//a:blip", NS)) + list(obj.findall(".//c:chart", NS))
                        if not refs:
                            if obj.tag in (f"{{{XDR}}}sp", f"{{{XDR}}}grpSp", f"{{{XDR}}}cxnSp", f"{{{XDR}}}graphicFrame"):
                                account()
                                gap("drawing_object_not_rendered", dict(sheet_location, drawing_part=dpart, object_path=object_path))
                            continue
                        props = obj.find(".//xdr:cNvPr", NS)
                        oid = props.get("id") if props is not None else None
                        if oid is not None and oid in ids:
                            _fail("invalid_package", "drawing 对象 ID 重复")
                        ids.add(oid)
                        location = dict(sheet_location, drawing_id=drid, drawing_part=dpart,
                                        object_id=oid, object_name=props.get("name") if props is not None else None,
                                        object_path=object_path, anchor=anchor)
                        if grouped:
                            # anchor 是外层容器的单元格位置，不推算组内对象的最终坐标。
                            gap("group_transform_not_applied", location)
                        if oid is None:
                            gap("object_id_missing", location)
                        if (anchor["kind"] not in ("oneCellAnchor", "twoCellAnchor") or anchor["from"] is None
                                or anchor["kind"] == "twoCellAnchor" and anchor["to"] is None):
                            gap("cell_anchor_missing", location)
                        for ref_index, ref in enumerate(refs, 1):
                            account()
                            is_chart = ref.tag == f"{{{C}}}chart"
                            rid = ref.get(f"{{{R}}}id") if is_chart else ref.get(f"{{{R}}}embed") or ref.get(f"{{{R}}}link")
                            relation = package.relation(dpart, rid, "chart" if is_chart else "image")
                            entry = dict(location, relationship_id=rid, reference_index=ref_index)
                            if relation["external"]:
                                gap("external_object_not_accessed", entry)
                                continue
                            source_part = relation["target"]
                            used_parts.add(source_part)
                            payload = package.archive.read(source_part)
                            entry.update(source_part=source_part, content_hash=_hash(payload), byte_count=len(payload), material_relpath=None)
                            if is_chart:
                                chart = package.xml(source_part)
                                entry.update(asset(payload, ".xml"), rendered=False,
                                             cache_freshness="unverified", series=_chart_series(chart))
                                charts.append(entry)
                                metadata(entry)
                                gap("chart_not_rendered", dict(location, source_part=source_part))
                                if not any(s["caches"] for s in entry["series"]):
                                    gap("chart_cache_missing", dict(location, source_part=source_part))
                            else:
                                ext, mime = _image_format(payload, source_part)
                                entry["media_type"] = mime
                                if ext is None:
                                    gap("unsupported_image_format", dict(location, source_part=source_part))
                                else:
                                    entry.update(asset(payload, ext))
                                metadata(entry)
                                images.append(entry)
        for part in sorted(package.names - used_parts):
            orphan_chart = (part.startswith("xl/charts/") and part.endswith(".xml")
                            and package.xml(part).tag == f"{{{C}}}chartSpace")
            if part.startswith("xl/media/") or orphan_chart:
                account()
                gap("unreferenced_media_or_chart_no_anchor", dict(source_part=part))
                payload = package.archive.read(part)
                entry = dict(worksheet=None, worksheet_id=None, worksheet_part=None,
                             drawing_id=None, drawing_part=None, object_id=None, object_name=None,
                             object_path=None, anchor=None, relationship_id=None, reference_index=None,
                             source_part=part, content_hash=_hash(payload), material_relpath=None,
                             byte_count=len(payload))
                if orphan_chart:
                    entry.update(asset(payload, ".xml"), rendered=False, cache_freshness="unverified",
                                 series=_chart_series(package.xml(part)))
                    charts.append(entry)
                    gap("chart_not_rendered", dict(source_part=part))
                    if not any(s["caches"] for s in entry["series"]):
                        gap("chart_cache_missing", dict(source_part=part))
                else:
                    ext, mime = _image_format(payload, part)
                    entry["media_type"] = mime
                    if ext is not None:
                        entry.update(asset(payload, ext))
                    else:
                        gap("unsupported_image_format", dict(source_part=part))
                    images.append(entry)
                metadata(entry)
        if calculation["missing_formula_caches"]:
            gap("formula_cache_missing_not_recalculated", dict(source_part=workbook_part,
                                                              count=calculation["missing_formula_caches"]))
        result = PreparedSpreadsheet(1, **identity, cache_key=key, images=tuple(images), charts=tuple(charts),
                                     calculation=calculation, coverage_gaps=tuple(gaps))
        if len(_json(result.to_dict()).encode("utf-8")) > budget.max_metadata_bytes:
            _fail("budget_exceeded", "完整材料清单字节预算超限")
        return result, assets
    except (zipfile.BadZipFile, ET.ParseError, KeyError, ValueError, RecursionError,
            NotImplementedError, RuntimeError, EOFError, zlib.error) as exc:
        if isinstance(exc, SpreadsheetMaterialError):
            raise
        raise SpreadsheetMaterialError("invalid_package", "XLSX 包或结构损坏：" + str(exc)) from exc
    finally:
        if package is not None:
            package.archive.close()


def verify_spreadsheet_materials(root, materials):
    """只读复核原件、关系／锚点／缓存结构和全部派生文件，不自动修复。"""
    if (not isinstance(materials, PreparedSpreadsheet) or materials.schema_version != 1
            or materials.preparation_version != VERSION):
        _fail("invalid_manifest", "不支持的 XLSX 材料版本")
    try:
        budget = _budget(SpreadsheetBudget(**materials.budget))
        source = _read(_path(root, materials.original_relpath), budget.max_source_bytes)
        if _hash(source) != materials.original_hash:
            _fail("source_changed", "原件 hash 已改变")
        expected, assets = _extract(source, materials.original_relpath, budget)
        if expected.to_dict() != materials.to_dict():
            _fail("invalid_manifest", "材料清单与原件结构不一致")
        for relpath, payload in assets.items():
            if _read(_path(root, relpath), budget.max_material_bytes) != payload:
                _fail("material_tampered", "派生材料 hash 不匹配")
    except OSError as exc:
        raise SpreadsheetMaterialError("material_unavailable", "原件或材料不可读取") from exc
    except TypeError as exc:
        raise SpreadsheetMaterialError("invalid_manifest", "预算字段无效") from exc
    return materials


def load_spreadsheet_materials(root, manifest, *, expected_manifest_hash):
    """expected_manifest_hash 必须来自调用方可信台账，不能取自未受信文件。"""
    try:
        value = dict(manifest)
        recorded = value.pop("manifest_hash")
        if recorded != expected_manifest_hash or _digest(value) != recorded:
            _fail("invalid_manifest", "材料清单 hash 不匹配")
        for key in ("images", "charts", "coverage_gaps"):
            value[key] = tuple(value[key])
        materials = PreparedSpreadsheet(**value)
    except (TypeError, KeyError, ValueError) as exc:
        raise SpreadsheetMaterialError("invalid_manifest", "材料清单格式或 hash 无效") from exc
    return verify_spreadsheet_materials(root, materials)


def prepare_spreadsheet_materials(root, original_relpath, *, budget=None):
    """完整准备后原子发布；超限／坏包不发布，坏缓存报错，原件从不写入。"""
    budget = _budget(SpreadsheetBudget() if budget is None else budget)
    source = _path(root, original_relpath)
    if source.suffix.lower() != ".xlsx":
        _fail("unsupported_format", "仅支持原始 XLSX OOXML")
    try:
        data = _read(source, budget.max_source_bytes)
        result, assets = _extract(data, original_relpath, budget)
        destination = _path(root, "spreadsheet_materials/" + result.cache_key)
        if destination.exists():
            try:
                manifest = json.loads(_read(_path(root, "spreadsheet_materials/" + result.cache_key + "/manifest.json"),
                                            budget.max_metadata_bytes).decode("utf-8"))
                if manifest != result.to_dict():
                    _fail("invalid_manifest", "缓存身份不匹配")
                return load_spreadsheet_materials(root, manifest, expected_manifest_hash=result.manifest_hash)
            except (OSError, ValueError, UnicodeError) as exc:
                raise SpreadsheetMaterialError("cache_invalid", "缓存缺失或损坏") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".xlsx-work-", dir=destination.parent) as temporary:
            published = Path(temporary) / "published"
            published.mkdir()
            (published / "objects").mkdir()
            for relpath, payload in assets.items():
                (published / "objects" / Path(relpath).name).write_bytes(payload)
            (published / "manifest.json").write_text(_json(result.to_dict()), encoding="utf-8")
            if _read(source, budget.max_source_bytes) != data:
                _fail("source_changed", "准备期间原件改变")
            try:
                published.rename(destination)
            except OSError:
                if not destination.exists():
                    raise
                return prepare_spreadsheet_materials(root, original_relpath, budget=budget)
        return verify_spreadsheet_materials(root, result)
    except OSError as exc:
        raise SpreadsheetMaterialError("material_unavailable", "原件或材料目录不可读写") from exc
