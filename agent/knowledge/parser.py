"""Bounded full source extraction; display pagination never enters publication."""
from pathlib import Path
from io import BytesIO

PARSER_ID = "personal-source"
PARSER_VERSION = "1"
MAX_BYTES = 10 * 1024 * 1024
MAX_UNITS = 200000
MAX_PAGES = 2000


class SourceError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def parse_source(path, filename=None, *, max_bytes=MAX_BYTES, max_units=MAX_UNITS):
    """Return all text with genuine positions, or fail without a partial result."""
    path = Path(path)
    ext = Path(filename or path.name).suffix.lower()
    units, size = [], 0

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
                # A mixed scanned/text document also requires OCR: no silent omission.
                if not text.strip() and page.images:
                    raise SourceError("needs_ocr", f"PDF 物理页 {number} 需要 OCR")
                add(text, {"kind": "pdf", "physical_page": number})
        elif ext == ".docx":
            from docx import Document
            from common.office_text import iter_docx_source_units
            for unit in iter_docx_source_units(Document(path)):
                add(**unit)
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
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError("parse_failed", f"资料解析失败：{type(exc).__name__}") from exc
    if not units:
        raise SourceError("needs_ocr" if ext == ".pdf" else "parse_failed", "未提取到可定位文本")
    return {"parser_id": PARSER_ID, "parser_version": PARSER_VERSION,
            "config": {"max_bytes": max_bytes, "max_units": max_units, "max_pages": MAX_PAGES},
            "units": units}
