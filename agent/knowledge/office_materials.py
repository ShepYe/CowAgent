"""LibreOffice 离线阅读副本。仅处理 DOCX/PPTX，不调用模型或登记知识。

root 是调用方已授权的数据根；所有持久身份均为根内相对路径。
PPT 逐张隔离导出，禁止仅凭总页数猜测源幻灯片映射。
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path, PureWindowsPath
import posixpath
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from pypdf import PdfReader, PdfWriter
from lxml import etree as LX


LEGACY_VERSION = "libreoffice-materials-v1"
VERSION = "libreoffice-materials-v2"
WORD_POLICY = "ooxml-final-view-v1"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


class OfficeMaterialError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(data):
    return sha256(data).hexdigest()


def _digest(value):
    return _hash(_json(value).encode("utf-8"))


def _path(root, relpath):
    if (not isinstance(relpath, str) or not relpath or "\\" in relpath or ":" in relpath
            or PureWindowsPath(relpath).drive or Path(relpath).is_absolute()
            or any(p in ("", ".", "..") for p in relpath.split("/"))):
        raise OfficeMaterialError("invalid_path", "材料身份必须是规范相对路径")
    root = Path(root).resolve()
    path = (root / relpath).resolve()
    if not path.is_relative_to(root):
        raise OfficeMaterialError("invalid_path", "材料路径越出数据根")
    return path


@dataclass(frozen=True)
class PreparedOffice:
    schema_version: int
    preparation_version: str
    original_relpath: str
    original_hash: str
    source_format: str
    engine: dict
    options: dict
    cache_key: str
    material_relpath: str
    material_hash: str
    page_count: int
    page_map: tuple
    notes: tuple
    coverage_gaps: tuple

    @property
    def manifest_hash(self):
        return _digest(asdict(self))

    def to_dict(self):
        return dict(json.loads(_json(asdict(self))), manifest_hash=self.manifest_hash)

    def location(self, pdf_page):
        """页码从 1 开始。Word 只返回阅读副本页，PPT 带源稳定 ID。"""
        if type(pdf_page) is not int or not 1 <= pdf_page <= self.page_count:
            raise OfficeMaterialError("invalid_location", "阅读副本页越界")
        return dict(self.page_map[pdf_page - 1])


def _run(args, timeout):
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, check=False,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired as exc:
        raise OfficeMaterialError("conversion_timeout", "LibreOffice 调用超时") from exc
    except OSError as exc:
        raise OfficeMaterialError("engine_unavailable", "LibreOffice 无法启动") from exc
    if result.returncode:
        raise OfficeMaterialError("conversion_failed", "LibreOffice 返回非零状态")
    return result.stdout.decode("utf-8", errors="replace").strip()


def _engine(soffice, timeout):
    executable = shutil.which(str(soffice))
    if not executable:
        raise OfficeMaterialError("engine_unavailable", "未找到指定 LibreOffice 可执行程序")
    version = _run([executable, "--headless", "--version"], timeout)
    if not version.startswith("LibreOffice "):
        raise OfficeMaterialError("engine_unavailable", "无法确认 LibreOffice 版本")
    return executable, dict(name="LibreOffice", version=version,
                            executable_hash=_hash(Path(executable).read_bytes()))


def _pdf(data):
    try:
        pdf = PdfReader(BytesIO(data), strict=True)
        if pdf.is_encrypted or not len(pdf.pages):
            raise ValueError("empty/encrypted")
        return pdf
    except Exception as exc:
        raise OfficeMaterialError("invalid_pdf", "转换未产生有效非空 PDF") from exc


def _relationships(archive, part):
    relpart = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    if relpart not in archive.namelist():
        return {}
    result = {}
    for node in ET.fromstring(archive.read(relpart)):
        if node.get("TargetMode") == "External":
            continue
        target = node.attrib["Target"]
        target = target.lstrip("/") if target.startswith("/") else posixpath.normpath(
            posixpath.join(posixpath.dirname(part), target))
        result[node.attrib["Id"]] = (target, node.get("Type", ""))
    return result


def _slides(data):
    """从源包读取顺序、稳定 ID、可见性及备注正文，排除页脚占位符。"""
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            tree = ET.fromstring(archive.read("ppt/presentation.xml"))
            relations = _relationships(archive, "ppt/presentation.xml")
            slides, notes = [], []
            ids = set()
            for index, node in enumerate(tree.findall(f"{{{P}}}sldIdLst/{{{P}}}sldId"), 1):
                stable_id = node.attrib["id"]
                if stable_id in ids:
                    raise ValueError("duplicate slide id")
                ids.add(stable_id)
                part, kind = relations[node.attrib[f"{{{R}}}id"]]
                if not kind.endswith("/slide"):
                    raise ValueError("invalid slide relationship")
                slide = ET.fromstring(archive.read(part))
                slides.append(dict(source_slide_id=stable_id, source_slide_number=index,
                                   source_part=part, hidden=slide.get("show", "1") in ("0", "false")))
                for note_part, reltype in _relationships(archive, part).values():
                    if not reltype.endswith("/notesSlide"):
                        continue
                    paragraphs = []
                    note = ET.fromstring(archive.read(note_part))
                    for shape in note.findall(f".//{{{P}}}sp"):
                        placeholder = shape.find(f".//{{{P}}}ph")
                        if placeholder is not None and placeholder.get("type") in ("sldImg", "sldNum", "dt", "ftr", "hdr"):
                            continue
                        for para in shape.findall(f".//{{{A}}}p"):
                            text = "".join(t.text or "" for t in para.findall(f".//{{{A}}}t"))
                            if text:
                                paragraphs.append(text)
                    notes.append(dict(source_slide_id=stable_id, source_part=note_part,
                                      text="\n".join(paragraphs), kind="speaker_notes"))
            if not slides:
                raise ValueError("no slides")
            return slides, notes
    except (KeyError, ValueError, ET.ParseError, zipfile.BadZipFile) as exc:
        raise OfficeMaterialError("invalid_package", "无法建立 PPTX 源结构") from exc


def _single_slide(data, stable_id):
    output = BytesIO()
    with zipfile.ZipFile(BytesIO(data)) as source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        tree = ET.fromstring(source.read("ppt/presentation.xml"))
        listing = tree.find(f"{{{P}}}sldIdLst")
        for node in list(listing):
            if node.get("id") != stable_id:
                listing.remove(node)
        # 原文件其余部件原样保留；只改变工作副本的播放列表。
        for info in source.infolist():
            target.writestr(info, ET.tostring(tree, encoding="utf-8", xml_declaration=True)
                            if info.filename == "ppt/presentation.xml" else source.read(info.filename))
    return output.getvalue()


def _convert(executable, source, folder, filter_name, options, timeout):
    output = folder / "output"
    output.mkdir()
    profile = folder / "profile"
    _run([executable, "-env:UserInstallation=" + profile.resolve().as_uri(),
          "--headless", "--nologo", "--nodefault", "--norestore",
          "--convert-to", "pdf:" + filter_name + ":" + _json(options),
          "--outdir", str(output), str(source)], timeout)
    result = output / (source.stem + ".pdf")
    if not result.is_file():
        raise OfficeMaterialError("conversion_failed", "引擎退出成功但未产生 PDF")
    payload = result.read_bytes()
    _pdf(payload)
    return payload


def _word_final_view(data):
    """仅改转换包；位置一律指原件 XML，不声称对应 PDF 页。

    lxml 保留命名空间声明（包括 mc:Ignorable 引用的前缀），不重建 OOXML。
    """
    ns = {"w": W}
    changes = {"ins", "del", "moveFrom", "moveTo", "cellIns", "cellDel",
               "rPrChange", "pPrChange", "tblPrChange", "tblPrExChange",
               "trPrChange", "tcPrChange", "sectPrChange", "tblGridChange", "numberingChange"}
    markers = {"commentRangeStart", "commentRangeEnd", "commentReference",
               "moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd"}
    notes, parts = [], {}
    output = BytesIO()
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            if "word/document.xml" not in archive.namelist():
                raise ValueError("missing document")
            for name in sorted(archive.namelist()):
                if name.startswith("word/") and name.endswith(".xml"):
                    tree = LX.fromstring(archive.read(name), LX.XMLParser(resolve_entities=False, no_network=True))
                    if "http://purl.oclc.org/ooxml/wordprocessingml/main" in tree.nsmap.values():
                        raise ValueError("Strict OOXML is not supported")
                    parts[name] = tree
            anchors = {}
            for name, tree in parts.items():
                for node in tree.iter():
                    if node.tag in (f"{{{W}}}commentRangeStart", f"{{{W}}}commentReference"):
                        anchors.setdefault(node.get(f"{{{W}}}id"), []).append(
                            dict(source_part=name, source_xpath=tree.getroottree().getpath(node)))
            for name, tree in parts.items():
                for node in tree.iter():
                    if not isinstance(node.tag, str):
                        continue
                    q = LX.QName(node)
                    if q.namespace != W:
                        continue
                    kind = q.localname
                    if kind in {"cellMerge", "conflictIns", "conflictDel"} or kind.startswith("customXml") and "Range" in kind:
                        raise ValueError("unsupported structural revision: " + kind)
                    if kind in changes or kind == "comment":
                        note = dict(kind="word_comment" if kind == "comment" else "word_revision",
                                    layer="additional", current_fact=False, source_part=name,
                                    source_xpath=tree.getroottree().getpath(node), revision_type=kind,
                                    revision_id=node.get(f"{{{W}}}id"),
                                    text="".join(node.xpath(".//w:t/text() | .//w:delText/text()", namespaces=ns)),
                                    xml=LX.tostring(node, encoding="unicode"))
                        if kind == "comment":
                            note["anchors"] = anchors.get(note["revision_id"], [])
                        notes.append(note)
                # 删除行／单元格；插入行保留。段落标记删除意味着与下一段合并。
                for node in list(tree.xpath(".//w:tr[w:trPr/w:del] | .//w:tc[w:tcPr/w:cellDel]", namespaces=ns)):
                    node.getparent().remove(node)
                for paragraph in reversed(tree.xpath(".//w:p[w:pPr/w:rPr/w:del or w:pPr/w:rPr/w:moveFrom]", namespaces=ns)):
                    following = paragraph.getnext()
                    if following is None or following.tag != f"{{{W}}}p":
                        raise ValueError("deleted paragraph mark without adjacent paragraph")
                    # 后段属性决定合并后的段落格式；保留其当前属性。
                    prop = paragraph.find(f"{{{W}}}pPr")
                    if prop is not None:
                        paragraph.remove(prop)
                    prop = following.find(f"{{{W}}}pPr")
                    if prop is not None:
                        following.remove(prop)
                        paragraph.insert(0, prop)
                    for child in list(following):
                        paragraph.append(child)
                    following.getparent().remove(following)
                def clean(parent):
                    for node in list(parent):
                        if not isinstance(node.tag, str):
                            continue
                        q = LX.QName(node)
                        kind = q.localname if q.namespace == W else ""
                        if (kind in {"del", "moveFrom", "delText", "delInstrText", "comment"}
                                or kind in markers or kind in changes - {"ins", "moveTo", "cellIns"}):
                            parent.remove(node)
                            continue
                        clean(node)
                        if kind in {"ins", "moveTo", "cellIns"}:
                            index = parent.index(node)
                            for child in list(node):
                                parent.insert(index, child)
                                index += 1
                            parent.remove(node)
                clean(tree)
            with zipfile.ZipFile(output, "w") as target:
                for info in archive.infolist():
                    tree = parts.get(info.filename)
                    target.writestr(info, archive.read(info.filename) if tree is None else
                                    LX.tostring(tree, encoding="UTF-8", xml_declaration=True, standalone=True))
    except (zipfile.BadZipFile, LX.XMLSyntaxError, ValueError, KeyError) as exc:
        raise OfficeMaterialError("invalid_package", "无法可靠生成 Word 最终正文：" + str(exc)) from exc
    return output.getvalue(), notes


def verify_office_materials(root, materials):
    """校验可信 PreparedOffice 的原件与阅读副本；不调用引擎、不自动修复。"""
    if not isinstance(materials, PreparedOffice) or materials.schema_version != 1:
        raise OfficeMaterialError("invalid_manifest", "不支持的 Office 材料清单")
    if materials.preparation_version not in (LEGACY_VERSION, VERSION):
        raise OfficeMaterialError("invalid_manifest", "不支持的 Office 准备版本")
    try:
        source_data = _path(root, materials.original_relpath).read_bytes()
        if _hash(source_data) != materials.original_hash:
            raise OfficeMaterialError("source_changed", "资料原件 hash 已改变")
        payload = _path(root, materials.material_relpath).read_bytes()
        if _hash(payload) != materials.material_hash:
            raise OfficeMaterialError("material_tampered", "阅读副本 hash 不匹配")
        if len(_pdf(payload).pages) != materials.page_count or len(materials.page_map) != materials.page_count:
            raise OfficeMaterialError("invalid_manifest", "PDF 页数与映射不一致")
        if [x.get("pdf_page") for x in materials.page_map] != list(range(1, materials.page_count + 1)):
            raise OfficeMaterialError("invalid_manifest", "PDF 映射不连续")
        identity = {name: getattr(materials, name) for name in (
            "preparation_version", "original_relpath", "original_hash", "source_format", "engine", "options")}
        if (materials.cache_key != _digest(identity)
                or materials.material_relpath != "office_materials/" + materials.cache_key + "/reading.pdf"):
            raise OfficeMaterialError("invalid_manifest", "材料身份与缓存键不一致")
        if materials.source_format == "pptx":
            slides, notes = _slides(source_data)
            expected = [dict(slide, pdf_page=i, kind="slide", mapping_method="isolated-source-slide")
                        for i, slide in enumerate((s for s in slides if not s["hidden"]), 1)]
            if list(materials.page_map) != expected or list(materials.notes) != notes:
                raise OfficeMaterialError("invalid_manifest", "PPT 映射或备注与源结构不一致")
        elif materials.source_format == "docx":
            if materials.preparation_version == VERSION:
                if materials.options.get("word_view") != WORD_POLICY:
                    raise OfficeMaterialError("invalid_manifest", "Word 最终正文策略不匹配")
                _, notes = _word_final_view(source_data)
            else:
                # v1 是原有带修订标记的阅读副本，不能追认成最终正文。
                if "word_view" in materials.options:
                    raise OfficeMaterialError("invalid_manifest", "旧版 Word 策略不匹配")
                notes = []
            expected = [dict(pdf_page=p, kind="word_reading_copy_page", reading_copy_page=p)
                        for p in range(1, materials.page_count + 1)]
            if list(materials.page_map) != expected or list(materials.notes) != notes:
                raise OfficeMaterialError("invalid_manifest", "Word 阅读副本位置不一致")
        else:
            raise OfficeMaterialError("invalid_manifest", "未支持的源格式")
    except OSError as exc:
        raise OfficeMaterialError("material_unavailable", "原件或阅读副本不可读取") from exc
    return materials


def load_office_materials(root, manifest, *, expected_manifest_hash):
    """从调用方可信台账恢复；expected_manifest_hash 必须取自可信台账。"""
    try:
        value = dict(manifest)
        recorded = value.pop("manifest_hash")
        if recorded != expected_manifest_hash or _digest(value) != recorded:
            raise ValueError("manifest hash")
        for name in ("page_map", "notes", "coverage_gaps"):
            value[name] = tuple(value[name])
        result = PreparedOffice(**value)
    except (TypeError, KeyError, ValueError) as exc:
        raise OfficeMaterialError("invalid_manifest", "材料清单校验失败") from exc
    return verify_office_materials(root, result)


def prepare_office_materials(root, original_relpath, *, soffice="soffice", timeout_seconds=120):
    """生成或复用阅读副本；不改原件。仅成功验证后原子发布缓存目录。

    timeout_seconds 是每次引擎调用的超时。PPT 每个可见源幻灯片单独调用。
    缓存损坏直接报错；缺少引擎也报错，不将历史缓存伪装为本次验证成功。
    """
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 3600:
        raise OfficeMaterialError("invalid_request", "超时须在 0～3600 秒之间")
    source = _path(root, original_relpath)
    suffix = source.suffix.lower()
    if suffix not in (".docx", ".pptx"):
        raise OfficeMaterialError("unsupported_format", "当前仅支持 DOCX/PPTX；旧格式未处理")
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise OfficeMaterialError("source_unavailable", "资料原件不可读取") from exc
    slides, notes = _slides(data) if suffix == ".pptx" else ([], [])
    word_data = None
    if suffix == ".docx":
        word_data, notes = _word_final_view(data)
    executable, engine = _engine(soffice, min(timeout_seconds, 15))
    filter_name = "impress_pdf_Export" if slides else "writer_pdf_Export"
    export = {"ExportNotesPages": {"type": "boolean", "value": "false"},
              "ExportHiddenSlides": {"type": "boolean", "value": "false"}}
    options = dict(filter=filter_name, export=export, strategy="single-visible-slide" if slides else "writer-layout")
    if suffix == ".docx":
        options["word_view"] = WORD_POLICY
    identity = dict(preparation_version=VERSION if suffix == ".docx" else LEGACY_VERSION, original_relpath=original_relpath,
                    original_hash=_hash(data), source_format=suffix[1:], engine=engine, options=options)
    key = _digest(identity)
    relbase = "office_materials/" + key
    destination = _path(root, relbase)
    if destination.exists():
        try:
            cached = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
            if any(cached.get(k) != v for k, v in identity.items()) or cached.get("cache_key") != key:
                raise ValueError("cache identity")
            return load_office_materials(root, cached, expected_manifest_hash=cached["manifest_hash"])
        except (OSError, ValueError, KeyError) as exc:
            raise OfficeMaterialError("cache_invalid", "缓存缺失或损坏；未返回成功") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".office-work-", dir=destination.parent) as temporary:
        work = Path(temporary)
        mapping = []
        gaps = ["字体替换、公式、修订及批注显示尚未逐层验收" if slides else "字体替换、复杂公式及版式尚未逐层验收"]
        if slides:
            writer = PdfWriter()
            for slide in slides:
                if slide["hidden"]:
                    gaps.append("隐藏幻灯片未渲染：" + slide["source_slide_id"])
                    continue
                folder = work / ("slide-" + str(slide["source_slide_number"]))
                folder.mkdir()
                copy = folder / "source.pptx"
                copy.write_bytes(_single_slide(data, slide["source_slide_id"]))
                converted = _convert(executable, copy, folder, filter_name, export, timeout_seconds)
                reader = _pdf(converted)
                if len(reader.pages) != 1:
                    raise OfficeMaterialError("mapping_failed", "单幻灯片转换结果不是一页")
                writer.add_page(reader.pages[0])
                mapping.append(dict(slide, pdf_page=len(mapping) + 1, kind="slide",
                                    mapping_method="isolated-source-slide"))
            if not mapping:
                raise OfficeMaterialError("no_visible_slides", "没有可见幻灯片可转换")
            output = BytesIO()
            writer.write(output)
            payload = output.getvalue()
            gaps.append("备注只提取独立结构文字，备注图片与排版未渲染")
        else:
            copy = work / "source.docx"
            copy.write_bytes(word_data)
            payload = _convert(executable, copy, work, filter_name, export, timeout_seconds)
            mapping = [dict(pdf_page=p, kind="word_reading_copy_page", reading_copy_page=p)
                       for p in range(1, len(_pdf(payload).pages) + 1)]
            gaps.append("Word 页码仅指本次引擎排版阅读副本，不代表原 Word 固定页；未建立段落锚点")
            gaps.append("修订及批注仅在 notes 附加层按原件 XML 部件／XPath 定位，不是当前正文事实；未建立其 PDF 页映射或渲染附加层版式")
        if source.read_bytes() != data:
            raise OfficeMaterialError("source_changed", "转换期间资料原件改变")
        result = PreparedOffice(1, **identity, cache_key=key, material_relpath=relbase + "/reading.pdf",
                                material_hash=_hash(payload), page_count=len(mapping),
                                page_map=tuple(mapping), notes=tuple(notes), coverage_gaps=tuple(gaps))
        published = work / "published"
        published.mkdir()
        (published / "reading.pdf").write_bytes(payload)
        (published / "manifest.json").write_text(_json(result.to_dict()), encoding="utf-8")
        try:
            published.rename(destination)
        except OSError:
            if not destination.exists():
                raise
            # 并发同键以已发布者为准，再核验其材料；不覆盖另一次转换。
            return prepare_office_materials(root, original_relpath, soffice=soffice,
                                            timeout_seconds=timeout_seconds)
    return verify_office_materials(root, result)
