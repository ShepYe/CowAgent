"""文档读取离线依赖自测；只在调用方指定的全新隔离根生成合成资料。"""
import argparse
from contextlib import contextmanager
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import shutil
import sys
import time


def _require(condition, message):
    # 自测在 python -O 下也必须保留核验。
    if not condition:
        raise ValueError(message)


@contextmanager
def _isolation(root):
    previous_cwd = Path.cwd()
    keys = ("COW_DATA_DIR", "TMP", "TEMP", "TMPDIR")
    previous = {key: os.environ.get(key) for key in keys}
    import tempfile
    previous_tempdir = tempfile.tempdir
    try:
        (root / "runtime").mkdir()
        (root / "tmp").mkdir()
        os.environ["COW_DATA_DIR"] = str(root / "runtime")
        for key in keys[1:]:
            os.environ[key] = str(root / "tmp")
        tempfile.tempdir = str(root / "tmp")
        os.chdir(root)
        yield
    finally:
        os.chdir(previous_cwd)
        tempfile.tempdir = previous_tempdir
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _make_pdf(path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=100)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
        NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"1 0 0 rg 0 50 100 50 re f 0 0 1 rg 0 0 100 50 re f "
                    b"0 0 0 rg BT /F1 10 Tf 110 70 Td (PDF CHECK 42) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(path)


def _pdf(root):
    from pypdf import PdfReader
    from agent.knowledge.reading_materials import prepare_pdf_materials, verify_pdf_materials
    path = root / "samples/source.pdf"
    _make_pdf(path)
    before = sha256(path.read_bytes()).hexdigest()
    materials = prepare_pdf_materials(root, "samples/source.pdf", document_id="selftest",
        revision_id="revision", parse_id="parse", config_revision="offline")
    verify_pdf_materials(root, materials)
    _require(materials.page_count == 1 and materials.segments, "PDF 分段失败")
    _require("PDF CHECK 42" in PdfReader(path).pages[0].extract_text(), "pypdf 文字提取失败")
    _require(sha256(path.read_bytes()).hexdigest() == before, "PDF 原件改变")
    return dict(pages=1, manifest=materials.to_dict(), original_hash_unchanged=True)


def _region(root):
    from PIL import Image
    from pypdf import PdfReader
    from agent.knowledge.reading_materials import prepare_pdf_materials, prepare_pdf_region
    path = root / "samples/region.pdf"
    _make_pdf(path)
    before = sha256(path.read_bytes()).hexdigest()
    base = prepare_pdf_materials(root, "samples/region.pdf", document_id="region",
        revision_id="revision", parse_id="parse", config_revision="offline")
    region = prepare_pdf_region(root, base, 1, [0, 0, .5, .5])
    png_path = root / region.region_supplement["png_relpath"]
    with Image.open(png_path) as png:
        _require(png.size == (300, 150), "PDFium 区域尺寸错误")
        _require(png.getpixel((150, 75))[:3] == (255, 0, 0), "PDFium 区域方向或像素错误")
    _require(len(PdfReader(root / region.segments[0].material_relpath).pages) == 1, "区域 PDF 无效")
    _require(sha256(path.read_bytes()).hexdigest() == before, "区域渲染改变原件")
    return dict(png=str(png_path.relative_to(root)), manifest=region.to_dict(), original_hash_unchanged=True)


def _word(root):
    from docx import Document
    from agent.knowledge.parser import parse_source
    path = root / "samples/source.docx"
    document = Document()
    document.add_paragraph("WORD CHECK 43 中文段落")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "TABLE CHECK 44"
    document.save(path)
    before = sha256(path.read_bytes()).hexdigest()
    parsed = parse_source(path, prepare_only=True)
    _require(any("WORD CHECK 43" in unit["text"] for unit in parsed["units"]), "Word 段落缺失")
    _require(any("TABLE CHECK 44" in unit["text"] and "/table[" in unit["location"]["path"]
                 for unit in parsed["units"]), "Word 表格结构定位缺失")
    _require(sha256(path.read_bytes()).hexdigest() == before, "Word 原件改变")
    return dict(units=parsed["units"], original_hash_unchanged=True)


def _ppt(root):
    from pptx import Presentation
    from pptx.util import Inches
    from agent.knowledge.parser import parse_source
    path = root / "samples/source.pptx"
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1)).text = "PPT CHECK 45 中文幻灯片"
    slide.notes_slide.notes_text_frame.text = "NOTES CHECK 46"
    presentation.save(path)
    before = sha256(path.read_bytes()).hexdigest()
    parsed = parse_source(path, prepare_only=True)
    _require(any("PPT CHECK 45" in unit["text"] and unit["location"].get("source_slide_id")
                 and unit["location"].get("object_id") for unit in parsed["units"]), "PPT 对象定位缺失")
    _require(any("NOTES CHECK 46" in unit["text"] and unit["type"] == "speaker_notes"
                 for unit in parsed["units"]), "PPT 备注结构缺失")
    _require(sha256(path.read_bytes()).hexdigest() == before, "PPT 原件改变")
    return dict(units=parsed["units"], original_hash_unchanged=True)


def _xlsx(root):
    from PIL import Image
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from agent.knowledge.spreadsheet_materials import prepare_spreadsheet_materials
    from agent.knowledge.parser import parse_spreadsheet_source, read_spreadsheet_object
    picture = root / "samples/embedded.png"
    Image.new("RGB", (12, 8), "red").save(picture)
    path = root / "samples/source.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "预算"
    sheet["A1"] = "XLSX CHECK 47"
    sheet["B1"] = "=1+1"
    sheet.add_image(XLImage(picture), "B3")
    book.save(path)
    book.close()
    before = sha256(path.read_bytes()).hexdigest()
    materials = prepare_spreadsheet_materials(root, "samples/source.xlsx")
    _require(len(materials.images) == 1, "XLSX 嵌图缺失")
    item = materials.images[0]
    _require(item["worksheet"] == "预算" and item["anchor"]["from"]["cell"] == "B3", "嵌图锚点错误")
    _require((root / item["material_relpath"]).read_bytes() == picture.read_bytes(), "嵌图内容改变")
    parsed = parse_spreadsheet_source(path, materials_root=root, materials=materials)
    location = next(item["location"] for item in parsed["visual_coverage"] if item["kind"] == "image")
    _require(read_spreadsheet_object(root, materials, location)["object_kind"] == "image", "嵌图回读失败")
    _require(materials.calculation["recalculated"] is False, "自测不应重算公式")
    _require(sha256(path.read_bytes()).hexdigest() == before, "XLSX 原件改变")
    return dict(manifest=materials.to_dict(), original_hash_unchanged=True,
                limitations=["嵌图仅验证提取与定位；未调用模型，未验证图表渲染及公式缓存新鲜度"])


def _convert(root, suffix, soffice):
    from agent.knowledge.office_materials import prepare_office_materials, verify_office_materials
    from pypdf import PdfReader
    from agent.knowledge.reading_materials import prepare_pdf_materials, prepare_pdf_region
    path = root / f"samples/source.{suffix}"
    before = sha256(path.read_bytes()).hexdigest()
    office = prepare_office_materials(root, path.relative_to(root).as_posix(),
                                      soffice=soffice, timeout_seconds=45)
    verify_office_materials(root, office)
    text = "\n".join(page.extract_text() or "" for page in PdfReader(root / office.material_relpath).pages)
    _require(("WORD CHECK 43" if suffix == "docx" else "PPT CHECK 45") in text, "转换正文缺失")
    pdf = prepare_pdf_materials(root, office.material_relpath, document_id=suffix,
        revision_id="revision", parse_id="parse", config_revision="offline")
    rendered = prepare_pdf_region(root, pdf, 1, [0, 0, 1, 1], max_pixels=500_000)
    _require(sha256(path.read_bytes()).hexdigest() == before, "转换改变原件")
    return dict(manifest=office.to_dict(), preview_png=rendered.region_supplement["png_relpath"],
                original_hash_unchanged=True, visual_quality="仅检查可见页转换及渲染；未目视验收版式")


def _check(name, function, *args):
    start = time.monotonic()
    try:
        result = dict(name=name, status="passed", details=function(*args))
    except Exception as exc:
        result = dict(name=name, status="failed", code=getattr(exc, "code", type(exc).__name__),
                      message=str(exc))
    result["elapsed_seconds"] = round(time.monotonic() - start, 3)
    return result


def run(root, *, soffice=None):
    """拒绝复用已有根；核心失败返回 failed，缺可选转换器返回 partial。"""
    root = Path(root).expanduser().resolve()
    # 必须在 chdir 之前解析显式可执行路径；也允许 PATH 中的命令名。
    executable = shutil.which(str(soffice)) if soffice else None
    if executable:
        executable = str(Path(executable).resolve())
    root.mkdir(parents=True, exist_ok=False)
    with _isolation(root):
        (root / "samples").mkdir()
        checks = [_check(name, function, root) for name, function in (
            ("pdf", _pdf), ("pdfium_region", _region), ("word_structure", _word),
            ("ppt_structure", _ppt), ("xlsx_embedded_image", _xlsx))]
        if executable:
            for suffix in ("docx", "pptx"):
                converted = _check(f"{suffix}_visible_conversion", _convert, root, suffix, executable)
                if converted.get("code") == "engine_unavailable":
                    converted.update(status="unavailable", affects_pdf=False)
                checks.append(converted)
        else:
            checks.append(dict(name="office_converter", status="unavailable", code="engine_unavailable",
                message="未指定 --soffice" if not soffice else "指定的 LibreOffice 可执行程序不存在",
                affects=["docx_visible_conversion", "pptx_visible_conversion"], affects_pdf=False))
        versions = {}
        for package in ("pypdf", "pypdfium2", "python-docx", "python-pptx", "openpyxl", "Pillow", "lxml"):
            try:
                versions[package] = version(package)
            except PackageNotFoundError:
                versions[package] = "metadata_unavailable"
        status = "failed" if any(c["status"] == "failed" for c in checks) else (
            "partial" if any(c["status"] == "unavailable" for c in checks) else "passed")
        report = dict(schema_version=1, status=status, root=str(root), checks=checks, versions=versions,
            runtime=dict(executable=sys.executable, frozen=bool(getattr(sys, "frozen", False))),
            cloud_api_called=False, scope="离线合成夹具依赖核验；未验证模型、安装包、复杂版式或真实知识库")
        (root / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-reading-self-test", required=True, metavar="NEW_ROOT",
                        help="尚不存在的隔离根；拒绝复用已有目录")
    parser.add_argument("--soffice", help="可选 LibreOffice 可执行程序路径，仅影响 Word/PPT 可见页转换")
    args = parser.parse_args(argv)
    try:
        report = run(args.document_reading_self_test, soffice=args.soffice)
    except (OSError, ValueError) as exc:
        print(json.dumps(dict(status="failed", code=type(exc).__name__, message=str(exc)), ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
