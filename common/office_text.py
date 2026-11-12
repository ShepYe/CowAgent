"""Office document text extraction shared by the read and web_fetch tools.

The parsers are optional dependencies, so they are imported lazily (docx) or
supplied by the caller (openpyxl's ``load_workbook``).
"""

from contextlib import contextmanager
from copy import deepcopy


def iter_docx_source_units(document):
    """Body positions follow XML order, including content controls and nested tables."""
    from docx.oxml.ns import qn
    from docx.table import Table

    def walk(container, parent, prefix):
        paragraph, table = 0, 0
        for element in container.iterchildren():
            if element.tag == qn("w:p"):
                paragraph += 1
                yield {"text": _docx_paragraph_text(element, parent),
                       "location": {"kind": "docx", "path": f"{prefix}/paragraph[{paragraph}]"}}
            elif element.tag == qn("w:sdt"):
                content = element.find(qn("w:sdtContent"))
                if content is not None:
                    yield from walk(content, parent, f"{prefix}/control[{list(container).index(element) + 1}]")
            elif element.tag == qn("w:tbl"):
                table += 1
                seen = set()
                for r, row in enumerate(Table(element, parent).rows, 1):
                    for c, cell in enumerate(row.cells, 1):
                        if cell._tc not in seen:
                            seen.add(cell._tc)
                            yield from walk(cell._tc, cell, f"{prefix}/table[{table}]/row[{r}]/cell[{c}]")
    yield from walk(document.element.body, document, "body")


def iter_docx_body_text(document):
    """Yield paragraphs and table rows in body order, including content controls.

    Builds python-docx's block objects directly, which also works on versions
    that predate ``Document.iter_inner_content()``.
    """
    yield from _iter_docx_blocks(document.element.body, document)


def _iter_docx_blocks(container, parent):
    """Traverse paragraphs and tables in either a document body or a cell."""
    from docx.oxml.ns import qn
    from docx.table import Table

    for element in container.iterchildren():
        if element.tag == qn("w:p"):
            yield _docx_paragraph_text(element, parent)
        elif element.tag == qn("w:sdt"):
            content = element.find(qn("w:sdtContent"))
            if content is not None:
                yield from _iter_docx_blocks(content, parent)
        elif element.tag == qn("w:tbl"):
            seen_cells = set()
            for row in Table(element, parent).rows:
                values = []
                for cell in row.cells:
                    # python-docx repeats the same XML cell for every covered
                    # grid slot, including vertical continuations. Keep those
                    # slots empty without dropping equal text in other cells.
                    if cell._tc in seen_cells:
                        values.append("")
                    else:
                        seen_cells.add(cell._tc)
                        values.append("\n".join(_iter_docx_blocks(cell._tc, cell)))
                yield "\t".join(values)


def _docx_paragraph_text(element, parent):
    """Include inline controls and cached field text without changing the source."""
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph

    tags = (qn("w:sdt"), qn("w:fldSimple"))
    if any(element.find(".//" + tag) is not None for tag in tags):
        element = deepcopy(element)
        for control in list(element.iter(*tags)):
            content = (control.find(qn("w:sdtContent"))
                       if control.tag == tags[0] else control)
            if content is not None:
                for child in list(content):
                    control.addprevious(child)
            control.getparent().remove(control)
    return Paragraph(element, parent).text


def iter_pptx_shape_text(shapes):
    """Yield slide text, including table cells and nested groups, in shape order."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from iter_pptx_shape_text(shape.shapes)
        else:
            for block in _pptx_text_blocks(shape):
                yield block["text"]


def _pptx_text_blocks(shape):
    """共享文字语义；保留原段落／表格行号，不按非空文字重新编号。"""
    if shape.has_table:
        for number, row in enumerate(shape.table.rows, 1):
            # 合并起点拥有文字，覆盖单元格不重复贡献文字。
            yield dict(text="\t".join(cell.text for cell in row.cells if not cell.is_spanned), row=number)
    elif shape.has_text_frame:
        for number, paragraph in enumerate(shape.text_frame.paragraphs, 1):
            text = paragraph.text.strip()
            if text:
                yield dict(text=text, paragraph=number)


def iter_pptx_source_objects(shapes, *, layer="slide", group_path=()):
    """惰性输出对象、文字与视觉缺口事件；只读 OOXML，不渲染、不读取外链。

    slide 身份由调用方附加；object_id 是源 cNvPr ID，group_path 保留嵌套。
    备注页的页码、日期、页眉页脚和幻灯片缩略图不混入独立备注正文。
    """
    from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

    excluded = {PP_PLACEHOLDER.SLIDE_IMAGE, PP_PLACEHOLDER.SLIDE_NUMBER, PP_PLACEHOLDER.DATE,
                PP_PLACEHOLDER.HEADER, PP_PLACEHOLDER.FOOTER}
    for shape in shapes:
        if layer == "speaker_notes" and shape.is_placeholder and shape.placeholder_format.type in excluded:
            continue
        anchor = dict(object_id=str(shape.shape_id), group_path=list(group_path))
        yield dict(event="object", location=anchor, object_type=shape.shape_type.name)
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from iter_pptx_source_objects(shape.shapes, layer=layer,
                                                group_path=(*group_path, str(shape.shape_id)))
            continue
        for block in _pptx_text_blocks(shape):
            yield dict(event="text", text=block["text"],
                       location=dict(anchor, **{k: v for k, v in block.items() if k != "text"}))
        # 同时捕获图片形状、形状填充图片和图表，关系按当前 part 解析。
        references = shape._element.xpath(".//a:blip | .//c:chart")
        for index, node in enumerate(references, 1):
            kind = "chart" if node.tag.endswith("}chart") else "image"
            namespace = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
            rid = node.get(namespace + "id") or node.get(namespace + "embed") or node.get(namespace + "link")
            if not rid:
                raise ValueError("图片／图表缺少源关系 ID")
            relationship = shape.part.rels[rid]
            if relationship.is_external:
                part, reason = None, "external_resource_not_loaded"
            else:
                part, reason = str(relationship.target_part.partname).lstrip("/"), "visual_not_supported"
            yield dict(event="gap", kind=kind, part=part, relationship_id=rid, reason=reason,
                       location=dict(anchor, reference=index))
        if not references and shape.shape_type in {
                MSO_SHAPE_TYPE.MEDIA, MSO_SHAPE_TYPE.WEB_VIDEO, MSO_SHAPE_TYPE.EMBEDDED_OLE_OBJECT,
                MSO_SHAPE_TYPE.LINKED_OLE_OBJECT, MSO_SHAPE_TYPE.DIAGRAM, MSO_SHAPE_TYPE.IGX_GRAPHIC}:
            yield dict(event="gap", kind="embedded_object", reason="object_not_rendered", location=anchor)
        elif not references and shape.shape_type in {
                MSO_SHAPE_TYPE.AUTO_SHAPE, MSO_SHAPE_TYPE.LINE, MSO_SHAPE_TYPE.FREEFORM}:
            yield dict(event="gap", kind="drawing", reason="visual_not_supported", location=anchor)

def _sheet_rows(cached_sheet, formula_sheet):
    for cached_row, formula_row in zip(cached_sheet.iter_rows(), formula_sheet.iter_rows()):
        values = []
        for cached, source in zip(cached_row, formula_row):
            value = cached.value
            if value is None and source.data_type == "f":
                # openpyxl's array formulas store their source in .text.
                formula = getattr(source.value, "text", source.value)
                value = f"[Formula: {formula} (not calculated)]"
            values.append(str(value) if value is not None else "")
        yield values


@contextmanager
def spreadsheet_sheets(file_path, load_workbook):
    """Yield (sheet name, rows) pairs and close both read-only views on exit.

    Cached values win, including 0/False. A formula with no cached result is
    shown as its source, marked unevaluated; nothing is recalculated.
    """
    cached = load_workbook(file_path, read_only=True, data_only=True)
    try:
        formulas = load_workbook(file_path, read_only=True, data_only=False)
        try:
            # Some spreadsheet producers leave stale used-range metadata. In
            # read-only mode those bounds silently clip real XML cells, so let
            # both views stream their actual rows/columns before pairing them.
            for sheet in cached.worksheets:
                sheet.reset_dimensions()
                formulas[sheet.title].reset_dimensions()
            yield ((sheet.title, _sheet_rows(sheet, formulas[sheet.title])) for sheet in cached.worksheets)
        finally:
            formulas.close()
    finally:
        cached.close()
