"""PDF 离线材料准备；普通准备不渲染，显式区域补读才裁剪渲染。

registry 保存 ``to_dict()`` 和可信 ``manifest_hash``，恢复时调用 load。
页码从 1 开始，page_map[n - 1] 是批内第 n 页的原件物理页。
target_pages 才是本批目标，邻接页仅作上下文。上下文和请求大小均为
估值；调用方须将完整请求额外开销纳入 budget，实际超限时缩小批次。
"""
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from collections import OrderedDict
from io import BytesIO
import json
import math
import os
from pathlib import Path, PureWindowsPath
import tempfile
import zlib
from threading import RLock

import pypdf
from pypdf import PdfReader, PdfWriter


class ReadingMaterialError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class PdfBudget:
    max_original_bytes: int = 20 * 1024 * 1024
    max_request_bytes: int = 28 * 1024 * 1024
    max_pages: int = 100
    max_context_tokens: int = 120000
    reserved_input_tokens: int = 4000
    reserved_output_tokens: int = 16000
    estimated_tokens_per_page: int = 2000
    request_overhead_bytes: int = 4096


@dataclass(frozen=True)
class PdfSegment:
    segment_id: str
    material_relpath: str
    material_hash: str
    page_map: tuple
    target_pages: tuple
    text_pages: tuple
    original_bytes: int
    request_bytes: int
    estimated_context_tokens: int

    def physical_page(self, batch_page):
        """越界／非整数位置一律拒绝，不采用 Python 的负索引。"""
        if type(batch_page) is not int or not 1 <= batch_page <= len(self.page_map):
            raise ReadingMaterialError("invalid_location", "批内物理页越界")
        return self.page_map[batch_page - 1]


@dataclass(frozen=True)
class PreparedPdf:
    schema_version: int
    preparation_version: str
    document_id: str
    revision_id: str
    parse_id: str
    config_revision: str
    original_relpath: str
    original_hash: str
    page_count: int
    target_pages: tuple
    budget: PdfBudget
    overlap_pages: int
    cache_key: str
    segments: tuple
    derived_source: dict = None
    region_supplement: dict = None

    def _payload(self):
        value = asdict(self)
        # 普通 PDF 的 v1 清单与哈希逐字兼容，不输出可选字段的 null。
        if self.derived_source is None:
            value.pop("derived_source")
        if self.region_supplement is None:
            value.pop("region_supplement")
        return value

    @property
    def manifest_hash(self):
        return _digest(self._payload())

    def to_dict(self):
        """只返回 JSON 类型；不包含绝对路径、PDF 字节或模型结果。"""
        return dict(json.loads(_canonical(self._payload())), manifest_hash=self.manifest_hash)

    @property
    def source_hash(self):
        return self.original_hash if self.derived_source is None else self.derived_source["original_hash"]

    @property
    def source_relpath(self):
        return self.original_relpath if self.derived_source is None else self.derived_source["original_relpath"]

    def location(self, pdf_page):
        """阅读 PDF 的全局页码映射到源位置；Word 页属于本次阅读副本。"""
        if type(pdf_page) is not int or not 1 <= pdf_page <= self.page_count:
            raise ReadingMaterialError("invalid_location", "阅读 PDF 页越界")
        if self.region_supplement is not None:
            if pdf_page != self.region_supplement["source_page"]:
                raise ReadingMaterialError("invalid_location", "位置不属于局部补读页")
            base = replace(self, region_supplement=None).location(pdf_page)
            return dict(base, region=list(self.region_supplement["region"]))
        if self.derived_source is None:
            return dict(kind="pdf", physical_page=pdf_page)
        item = self.derived_source["office_manifest"]["page_map"][pdf_page - 1]
        if item["kind"] == "word_reading_copy_page":
            return dict(kind=item["kind"], reading_copy_page=item["reading_copy_page"])
        return dict(kind="slide", source_slide_id=item["source_slide_id"],
                    source_slide_number=item["source_slide_number"])

    def coverage_keys(self, pages=None):
        """可信覆盖键；Word 绑定排版清单，PPT 使用稳定源 slide ID。"""
        pages = self.target_pages if pages is None else _pages(pages, self.page_count)
        if self.region_supplement is not None:
            return []  # 局部图像不能证明整页覆盖，须结合主 reading 单独对账。
        if self.derived_source is None:
            return [f"{self.revision_id}:{p}" for p in pages]
        return office_coverage_keys(self.revision_id, self.derived_source["office_manifest"], pages)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _hash(data):
    return sha256(data).hexdigest()


def _relative(root, relpath):
    # 同时检查 Windows 语义，避免备份换平台后盘符／反斜杠被误读。
    if (not isinstance(relpath, str) or not relpath or "\\" in relpath or ":" in relpath
            or PureWindowsPath(relpath).drive or Path(relpath).is_absolute()
            or any(p in ("", ".", "..") for p in relpath.split("/"))):
        raise ReadingMaterialError("invalid_path", "材料路径必须为规范根内相对路径")
    root = Path(root).resolve()
    path = (root / relpath).resolve()
    if not path.is_relative_to(root):
        raise ReadingMaterialError("invalid_path", "材料路径越出数据根")
    return path


def _read(root, relpath):
    try:
        return _relative(root, relpath).read_bytes()
    except OSError as exc:
        raise ReadingMaterialError("material_unavailable", "PDF 材料不存在或不可读取") from exc


def _pages(value, page_count):
    try:
        pages = tuple(value)
    except TypeError as exc:
        raise ReadingMaterialError("invalid_location", "目标页必须为物理页序列") from exc
    if (not pages or any(type(p) is not int or p < 1 or p > page_count for p in pages)
            or tuple(sorted(set(pages))) != pages):
        raise ReadingMaterialError("invalid_location", "目标页须递增、唯一且在原件范围内")
    return pages


def office_coverage_keys(revision_id, office_manifest, pages=None):
    """仅接受可信／已校验 Office 清单；供任务登记与 registry 共用。"""
    count = office_manifest["page_count"]
    pages = _pages(range(1, count + 1) if pages is None else pages, count)
    result = []
    for page in pages:
        loc = office_manifest["page_map"][page - 1]
        key = (f"word:{office_manifest['manifest_hash']}:{loc['reading_copy_page']}"
               if loc["kind"] == "word_reading_copy_page" else f"slide:{loc['source_slide_id']}")
        result.append(f"{revision_id}:{key}")
    return result


def _validate_budget(budget, overlap):
    if not isinstance(budget, PdfBudget):
        raise ReadingMaterialError("invalid_request", "budget 必须是 PdfBudget")
    for key, value in asdict(budget).items():
        minimum = 0 if key.startswith("reserved_") or key == "request_overhead_bytes" else 1
        if type(value) is not int or value < minimum:
            raise ReadingMaterialError("invalid_request", "预算须为有效整数")
    if type(overlap) is not int or overlap < 0:
        raise ReadingMaterialError("invalid_request", "邻接页数须为非负整数")


def _pdf(data):
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted or not len(reader.pages):
            raise ValueError("PDF 加密或无页面")
        return reader
    except Exception as exc:
        raise ReadingMaterialError("parse_failed", "PDF 无法离线解析（损坏、加密或无页面）") from exc


def _subset(reader, pages):
    writer = PdfWriter()
    for page in pages:
        writer.add_page(reader.pages[page - 1])
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


_text_cache = OrderedDict()
_text_cache_lock = RLock()
_text_cache_bytes = 0


def _page_text_by_hash(reader, version, content_hash, page_number):
    """仅缓存同版本同字节的纯文字提取；不保留PDF字节，最多8MiB。"""
    global _text_cache_bytes
    key = (version, content_hash, page_number)
    with _text_cache_lock:
        if key in _text_cache:
            _text_cache.move_to_end(key)
            return _text_cache[key][0]
    text = reader.pages[page_number - 1].extract_text() or ''
    size = len(text.encode('utf-8'))
    if size <= 8 * 1024 * 1024:
        with _text_cache_lock:
            if key not in _text_cache:
                _text_cache[key] = (text, size)
                _text_cache_bytes += size
                while len(_text_cache) > 128 or _text_cache_bytes > 8 * 1024 * 1024:
                    _, (_, removed) = _text_cache.popitem(last=False)
                    _text_cache_bytes -= removed
    return text


def _plan(root, original_relpath, *, document_id, revision_id, parse_id, config_revision,
          target_pages, budget, overlap_pages):
    _validate_budget(budget, overlap_pages)
    identity = dict(document_id=document_id, revision_id=revision_id,
                    parse_id=parse_id, config_revision=config_revision)
    if any(not isinstance(v, str) or not v.strip() for v in identity.values()):
        raise ReadingMaterialError("invalid_request", "逻辑资料、版本、解析及配置身份不可为空")
    data = _read(root, original_relpath)
    reader = _pdf(data)
    count = len(reader.pages)
    targets = _pages(range(1, count + 1) if target_pages is None else target_pages, count)
    # 只提取本次目标和必要邻接页，不读取无关页的文字层。
    needed = sorted({p for t in targets for p in range(max(1, t - overlap_pages),
                                                     min(count, t + overlap_pages) + 1)})
    version = "pdf-materials-v1/pypdf-" + pypdf.__version__
    source_hash = _hash(data)
    try:
        texts = {p: _page_text_by_hash(reader, version, source_hash, p) for p in needed}
    except Exception as exc:
        raise ReadingMaterialError("parse_failed", "PDF 本地文字层提取失败") from exc
    key = _digest(dict(identity, preparation_version=version, original_hash=source_hash,
                       original_relpath=original_relpath, target_pages=targets,
                       budget=asdict(budget), overlap_pages=overlap_pages))

    def candidate(batch):
        mapping = tuple(sorted({p for t in batch for p in range(max(1, t - overlap_pages),
                                                                min(count, t + overlap_pages) + 1)}))
        if len(mapping) > budget.max_pages:
            return None
        text = tuple(texts[p] for p in mapping)
        # UTF-8 字节数作为文字 token 的保守估值，另加每页图文估值。
        context = (sum(len(t.encode("utf-8")) for t in text)
                   + len(mapping) * budget.estimated_tokens_per_page
                   + budget.reserved_input_tokens + budget.reserved_output_tokens)
        if context > budget.max_context_tokens:
            return None
        whole = mapping == tuple(range(1, count + 1))
        payload = data if whole else _subset(reader, mapping)
        size = len(payload)
        request = 4 * ((size + 2) // 3) + budget.request_overhead_bytes
        if size > budget.max_original_bytes or request > budget.max_request_bytes:
            return None
        material_hash = _hash(payload)
        segment_id = _digest(dict(cache_key=key, page_map=mapping, target_pages=batch,
                                  material_hash=material_hash))
        relpath = original_relpath if whole else f"reading_materials/{key}/{segment_id}.pdf"
        return PdfSegment(segment_id, relpath, material_hash, mapping, batch, text,
                          size, request, context), payload

    # 能整份／整个请求范围处理时优先整批；拆分时目标页只分配一次。
    full = candidate(targets)
    outputs = []
    if full is not None:
        outputs.append(full)
    else:
        start = 0
        while start < len(targets):
            best = None
            end = start
            while end < len(targets):
                current = candidate(targets[start:end + 1])
                if current is None:
                    break
                best = current
                end += 1
            if best is None:
                raise ReadingMaterialError("budget_exceeded", "单页及必要邻接上下文超预算；未准备部分材料")
            outputs.append(best)
            start += len(best[0].target_pages)
    materials = PreparedPdf(1, version, **identity, original_relpath=original_relpath,
                            original_hash=source_hash, page_count=count, target_pages=targets,
                            budget=budget, overlap_pages=overlap_pages, cache_key=key,
                            segments=tuple(s for s, _ in outputs))
    return materials, outputs


def _store(root, relpath, payload):
    path = _relative(root, relpath)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 同目录临时文件 + 不覆盖的原子发布；中断不留下半个正式材料。
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ReadingMaterialError("material_tampered", "已有不可变材料与预期不一致")
    except OSError as exc:
        raise ReadingMaterialError("storage_error", "无法保存 PDF 阅读材料") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_pdf_materials(root, original_relpath, *, document_id, revision_id, parse_id,
                          config_revision, target_pages=None, budget=None, overlap_pages=0):
    """准备原件或选页 PDF。输入须已通过调用方路径权限和资料登记校验。

    max_original_bytes 指每批 PDF 原始字节；request_bytes 为 base64 加预算开销。
    函数与模型可用性无关；不会导入 parser、SourceService 或视觉模块。
    """
    materials, outputs = _plan(root, original_relpath, document_id=document_id, revision_id=revision_id,
                               parse_id=parse_id, config_revision=config_revision, target_pages=target_pages,
                               budget=PdfBudget() if budget is None else budget, overlap_pages=overlap_pages)
    for segment, payload in outputs:
        if segment.material_relpath != original_relpath:
            _store(root, segment.material_relpath, payload)
    return verify_pdf_materials(root, materials)


def bind_office_pdf(root, materials, office, *, original_relpath, original_hash):
    """受信准备入口把 PDF 清单绑定到 Office 原件和完整转换清单。

    PDF original_hash 始终是 PDF 的哈希；源 Office 的哈希只进入关联字段。
    office 必须来自已授权的转换，不能接受模型提供的转换清单。
    """
    derived = dict(original_relpath=original_relpath, original_hash=original_hash,
                   office_manifest=office.to_dict(), office_manifest_hash=office.manifest_hash)
    return verify_pdf_materials(root, replace(materials, derived_source=derived))


def _verify_derived_source(root, materials):
    from agent.knowledge.office_materials import OfficeMaterialError, load_office_materials

    derived = materials.derived_source
    if not isinstance(derived, dict) or set(derived) != {
            "original_relpath", "original_hash", "office_manifest", "office_manifest_hash"}:
        raise ReadingMaterialError("invalid_manifest", "非法 Office 原件关联")
    if _hash(_read(root, derived["original_relpath"])) != derived["original_hash"]:
        raise ReadingMaterialError("material_tampered", "Office 原件哈希不符")
    try:
        office = load_office_materials(root, derived["office_manifest"],
                                      expected_manifest_hash=derived["office_manifest_hash"])
    except OfficeMaterialError as exc:
        raise ReadingMaterialError(exc.code, str(exc)) from exc
    work = f"office_sources/{derived['original_hash']}/source.{office.source_format}"
    if (office.original_hash != derived["original_hash"] or office.original_relpath != work
            or office.material_relpath != materials.original_relpath
            or office.material_hash != materials.original_hash or office.page_count != materials.page_count):
        raise ReadingMaterialError("invalid_manifest", "Office 原件、工作副本与阅读 PDF 关联不符")
    return office


def verify_pdf_materials(root, materials):
    """重新校验原件、材料、预算及映射；只读，不重建缺失材料。

    materials 必须来自 prepare 或可信 registry；非可信 JSON 使用 load。
    核验不代替调用方的来源有效性、owner、task/attempt 及发送状态校验。
    """
    if (not isinstance(materials, PreparedPdf) or type(materials.schema_version) is not int
            or materials.schema_version != 1):
        raise ReadingMaterialError("invalid_manifest", "未知 PDF 材料清单版本")
    if _hash(_read(root, materials.original_relpath)) != materials.original_hash:
        raise ReadingMaterialError("material_tampered", "PDF 原件哈希不匹配")
    if materials.derived_source is not None:
        _verify_derived_source(root, materials)
    if materials.region_supplement is not None:
        return _verify_pdf_region(root, materials)
    for segment in materials.segments:
        if _hash(_read(root, segment.material_relpath)) != segment.material_hash:
            raise ReadingMaterialError("material_tampered", "PDF 阅读材料哈希不匹配")
    expected, _ = _plan(root, materials.original_relpath, document_id=materials.document_id,
                        revision_id=materials.revision_id, parse_id=materials.parse_id,
                        config_revision=materials.config_revision, target_pages=materials.target_pages,
                        budget=materials.budget, overlap_pages=materials.overlap_pages)
    if materials.derived_source is not None:
        expected = replace(expected, derived_source=materials.derived_source)
    if materials.manifest_hash != expected.manifest_hash:
        raise ReadingMaterialError("invalid_manifest", "材料版本、映射、位置或预算与原件不一致")
    return materials


def retry_pdf_segment(root, materials, segment_id):
    """只重建未知段的阅读材料，保留预算、邻接范围和原件映射。"""
    verify_pdf_materials(root, materials)
    segment = next((s for s in materials.segments if s.segment_id == segment_id), None)
    if segment is None:
        raise ReadingMaterialError('invalid_request', '重试阅读段不存在')
    if materials.region_supplement is not None:
        return materials  # 阅读器另建交付身份；材料复用不扩大局部范围。
    prepared = prepare_pdf_materials(root, materials.original_relpath,
        document_id=materials.document_id, revision_id=materials.revision_id,
        parse_id=materials.parse_id, config_revision=materials.config_revision,
        target_pages=segment.target_pages, budget=materials.budget, overlap_pages=materials.overlap_pages)
    if materials.derived_source is not None:
        prepared = replace(prepared, derived_source=json.loads(_canonical(materials.derived_source)))
    return verify_pdf_materials(root, prepared)


def replan_pdf_segment(root, materials, segment_id):
    """离线缩批：返回新清单，严格覆盖一个父段的全部目标，关闭邻接页。

    只负责材料；调用方必须证明父段明确超预算、未完成且未被其他成功
    阅读覆盖。页数至少减半（向下取整），单目标页不能继续缩小。
    不修改原清单或任何原材料；不能证明可缩批时不写部分材料。
    """
    verify_pdf_materials(root, materials)
    parent = next((s for s in materials.segments if s.segment_id == segment_id), None)
    if parent is None:
        raise ReadingMaterialError("invalid_request", "父阅读段不存在")
    if len(parent.target_pages) < 2:
        raise ReadingMaterialError("reading_budget_exceeded", "单目标页已无法继续缩批")
    budget = replace(materials.budget, max_pages=min(materials.budget.max_pages,
                                                    len(parent.target_pages) // 2))
    child, outputs = _plan(root, materials.original_relpath, document_id=materials.document_id,
        revision_id=materials.revision_id, parse_id=materials.parse_id,
        config_revision=materials.config_revision, target_pages=parent.target_pages,
        budget=budget, overlap_pages=0)
    if materials.derived_source is not None:
        child = replace(child, derived_source=json.loads(_canonical(materials.derived_source)))
    if (child.original_hash != materials.original_hash or child.target_pages != parent.target_pages
            or tuple(p for s in child.segments for p in s.target_pages) != parent.target_pages
            or any(s.page_map != s.target_pages or len(s.target_pages) >= len(parent.target_pages)
                   for s in child.segments)):
        raise ReadingMaterialError("invalid_manifest", "缩批未形成父段目标的严格分割")
    for segment, payload in outputs:
        if segment.material_relpath != materials.original_relpath:
            _store(root, segment.material_relpath, payload)
    return verify_pdf_materials(root, child)


def _region_options(region, scale, max_pixels, max_png_bytes):
    if (not isinstance(region, (list, tuple)) or len(region) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(v) for v in region)):
        raise ReadingMaterialError("invalid_location", "region 须为归一化 [left, top, right, bottom]")
    left, top, right, bottom = region
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ReadingMaterialError("invalid_location", "region 须在页内且具有正面积")
    if (isinstance(scale, bool) or not isinstance(scale, (int, float))
            or not math.isfinite(scale) or not 0 < scale <= 8
            or type(max_pixels) is not int or not 1 <= max_pixels <= 16_000_000
            or type(max_png_bytes) is not int or not 1 <= max_png_bytes <= 32 * 1024 * 1024):
        raise ReadingMaterialError("invalid_request", "局部渲染比例或像素／PNG预算无效")
    return [float(v) for v in region], dict(scale=float(scale), max_pixels=max_pixels,
                                           max_png_bytes=max_png_bytes)


def _render_region(data, page_number, region, limits):
    try:
        import pypdfium2 as pdfium
        import PIL
        from agent.knowledge.visual import _pdfium_lock
    except ImportError as exc:
        raise ReadingMaterialError("renderer_unavailable", "局部补读需要 PDFium 与 Pillow") from exc
    engine = dict(name="pdfium", version=str(pdfium.version.PDFIUM_INFO),
                  binding_version=str(pdfium.version.PYPDFIUM_INFO), pillow_version=PIL.__version__)
    document = page = bitmap = None
    try:
        with _pdfium_lock:
            try:
                document = pdfium.PdfDocument(data)
                page = document[page_number - 1]  # 只加载指定页，不渲染其他页。
                width, height = page.get_size()
                if not all(math.isfinite(v) and v > 0 for v in (width, height)):
                    raise ValueError("invalid page size")
                left, top, right, bottom = region
                crop = (left * width, (1 - bottom) * height, (1 - right) * width, top * height)
                scale = min(limits["scale"], math.sqrt(limits["max_pixels"] /
                            (width * height * (right - left) * (bottom - top))))

                def dimensions(value):
                    return (math.ceil(width * value) - math.ceil(crop[0] * value) - math.ceil(crop[2] * value),
                            math.ceil(height * value) - math.ceil(crop[1] * value) - math.ceil(crop[3] * value))

                pixels_w, pixels_h = dimensions(scale)
                for _ in range(10):
                    if pixels_w * pixels_h <= limits["max_pixels"]:
                        break
                    scale *= .99
                    pixels_w, pixels_h = dimensions(scale)
                if (min(pixels_w, pixels_h) < 1 or max(pixels_w, pixels_h) > 16384
                        or max(width * scale, height * scale) >= 2 ** 31
                        or pixels_w * pixels_h > limits["max_pixels"]):
                    raise ReadingMaterialError("budget_exceeded", "裁剪区域无法在像素预算内渲染")
                # PDFium crop 次序为 left,bottom,right,top；实际 bitmap 只分配裁剪区域。
                bitmap = page.render(scale=scale, crop=crop, rotation=0, draw_annots=True,
                                     may_draw_forms=False, fill_color=(255, 255, 255, 255))
                with bitmap.to_pil().convert("RGB") as image:
                    if image.size != (pixels_w, pixels_h):
                        raise ValueError("unexpected bitmap dimensions")
                    stream = BytesIO()
                    image.save(stream, format="PNG", compress_level=6)
                png = stream.getvalue()
                if len(png) > limits["max_png_bytes"]:
                    raise ReadingMaterialError("budget_exceeded", "局部 PNG 超出字节预算")
                return png, dict(engine=engine, render_scale=scale, pixel_width=pixels_w,
                                 pixel_height=pixels_h, crop_pdf_units=list(crop),
                                 coordinate_system="top-left-normalized-displayed-page",
                                 render_options=dict(rotation=0, draw_annots=True, may_draw_forms=False,
                                                     background="white", color_mode="RGB"))
            finally:
                for resource in (bitmap, page, document):
                    if resource is not None:
                        resource.close()
    except ReadingMaterialError:
        raise
    except Exception as exc:
        raise ReadingMaterialError("render_failed", "指定页的区域渲染失败") from exc


def _png_pdf(png):
    """PNG 解码后无损封装单页 PDF，不添加时钟元数据，支持稳定 hash。"""
    from PIL import Image
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

    try:
        with Image.open(BytesIO(png)) as image:
            if image.format != "PNG" or image.mode != "RGB":
                raise ValueError("invalid PNG")
            image.load()
            width, height = image.size
            pixels = image.tobytes()
        writer = PdfWriter()
        page = writer.add_blank_page(width=width, height=height)
        obj = DecodedStreamObject()
        obj.set_data(pixels)
        obj.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(width), NameObject("/Height"): NumberObject(height),
                    NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
        obj = obj.flate_encode()
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({
            NameObject("/Region"): writer._add_object(obj)})})
        content = DecodedStreamObject()
        content.set_data(f"q {width} 0 0 {height} 0 0 cm /Region Do Q".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(content)
        output = BytesIO()
        writer.write(output)
        return output.getvalue()
    except Exception as exc:
        raise ReadingMaterialError("render_failed", "局部 PNG 无法封装为 PDF") from exc


def _plan_region(root, materials, page, region, *, scale, max_pixels, max_png_bytes):
    region, limits = _region_options(region, scale, max_pixels, max_png_bytes)
    _pages((page,), materials.page_count)
    _validate_budget(materials.budget, 0)
    if (type(materials.page_count) is not int or materials.page_count < 1
            or any(not isinstance(getattr(materials, k), str) or not getattr(materials, k).strip()
                   for k in ("document_id", "revision_id", "parse_id", "config_revision"))):
        raise ReadingMaterialError("invalid_manifest", "局部材料的资料身份或页数无效")
    data = _read(root, materials.original_relpath)
    if _hash(data) != materials.original_hash:
        raise ReadingMaterialError("material_tampered", "PDF 原件哈希不匹配")
    if len(_pdf(data).pages) != materials.page_count:
        raise ReadingMaterialError("invalid_manifest", "局部补读源 PDF 页数不符")
    png, details = _render_region(data, page, region, limits)
    payload = _png_pdf(png)
    size = len(payload)
    budget = materials.budget
    request = 4 * ((size + 2) // 3) + budget.request_overhead_bytes
    context = budget.estimated_tokens_per_page + budget.reserved_input_tokens + budget.reserved_output_tokens
    if size > budget.max_original_bytes or request > budget.max_request_bytes or context > budget.max_context_tokens:
        raise ReadingMaterialError("budget_exceeded", "局部阅读 PDF 超出输入预算")
    version = "pdf-region-v1/pypdf-" + pypdf.__version__ + "/zlib-" + zlib.ZLIB_RUNTIME_VERSION
    supplement = dict(schema_version=1, source_page=page, region=region, limits=limits,
                      png_hash=_hash(png), **details)
    identity = {k: getattr(materials, k) for k in ("document_id", "revision_id", "parse_id", "config_revision",
                                                "original_relpath", "original_hash", "page_count")}
    key = _digest(dict(identity, preparation_version=version, region_supplement=supplement,
                       derived_source=materials.derived_source, budget=asdict(budget)))
    base = f"reading_materials/{key}"
    supplement["png_relpath"] = base + "/region.png"
    sid = _digest(dict(cache_key=key, material_hash=_hash(payload), page_map=[page]))
    segment = PdfSegment(sid, base + "/region.pdf", _hash(payload), (page,), (page,), ("",), size, request, context)
    result = PreparedPdf(1, version, **identity, target_pages=(page,), budget=budget, overlap_pages=0,
                         cache_key=key, segments=(segment,), derived_source=materials.derived_source,
                         region_supplement=supplement)
    return result, png, payload


def prepare_pdf_region(root, materials, page, region, *, scale=3, max_pixels=4_000_000,
                       max_png_bytes=8 * 1024 * 1024):
    """显式裁剪单页局部并封装 PNG→PDF；region 为左上原点归一化 l,t,r,b。

    page 始终是主阅读 PDF 页号，局部 PDF 批内页只有 1，page_map 保留该源页。
    location 追加 region；coverage_keys 恒为空，局部不能证明整页已完成。
    """
    verify_pdf_materials(root, materials)
    if materials.region_supplement is not None:
        raise ReadingMaterialError("invalid_request", "局部补读须从主阅读材料创建，不接受递归裁剪")
    _pages((page,), materials.page_count)
    if page not in materials.target_pages:
        raise ReadingMaterialError("invalid_location", "局部页不属于主阅读目标")
    result, png, payload = _plan_region(root, materials, page, region, scale=scale,
                                       max_pixels=max_pixels, max_png_bytes=max_png_bytes)
    _store(root, result.region_supplement["png_relpath"], png)
    _store(root, result.segments[0].material_relpath, payload)
    return verify_pdf_materials(root, result)


def _verify_pdf_region(root, materials):
    try:
        supplement = materials.region_supplement
        if not isinstance(supplement, dict) or type(supplement.get("schema_version")) is not int or supplement["schema_version"] != 1:
            raise ReadingMaterialError("invalid_manifest", "局部补读清单版本无效")
        png = _read(root, supplement["png_relpath"])
        if _hash(png) != supplement["png_hash"]:
            raise ReadingMaterialError("material_tampered", "局部 PNG 哈希不匹配")
        payload = _read(root, materials.segments[0].material_relpath)
        if _hash(payload) != materials.segments[0].material_hash:
            raise ReadingMaterialError("material_tampered", "局部 PDF 哈希不匹配")
        expected, expected_png, expected_pdf = _plan_region(root, materials, supplement["source_page"],
                                                          supplement["region"], **supplement["limits"])
        if (materials.manifest_hash != expected.manifest_hash or png != expected_png or payload != expected_pdf):
            raise ReadingMaterialError("invalid_manifest", "局部图像、映射、引擎或预算与源页重渲染不一致")
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        if isinstance(exc, ReadingMaterialError):
            raise
        raise ReadingMaterialError("invalid_manifest", "局部补读清单结构无效") from exc
    return materials


def load_pdf_materials(root, manifest, *, expected_manifest_hash):
    """从 registry 清单恢复；可信锚须另行保存，不能取自不可信输入自身。"""
    try:
        raw = dict(manifest)
        stored_hash = raw.pop("manifest_hash")
        if not expected_manifest_hash or stored_hash != expected_manifest_hash or _digest(raw) != expected_manifest_hash:
            raise ReadingMaterialError("invalid_manifest", "PDF 材料清单可信哈希不匹配")
        raw["target_pages"] = tuple(raw["target_pages"])
        raw["budget"] = PdfBudget(**raw["budget"])
        segments = []
        for item in raw["segments"]:
            item = dict(item)
            for key in ("page_map", "target_pages", "text_pages"):
                item[key] = tuple(item[key])
            segments.append(PdfSegment(**item))
        raw["segments"] = tuple(segments)
        materials = PreparedPdf(**raw)
    except ReadingMaterialError:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise ReadingMaterialError("invalid_manifest", "非法 PDF 材料清单") from exc
    return verify_pdf_materials(root, materials)
