# Office 转 PDF 第三方方案核对

2026-11-12（Asia/Shanghai）。仅调研官方文档／项目源码；本轮未安装依赖、未运行 Office 或转换器、未编写生产代码。自动化脚本安全控制按用户指示暂停，不作为本轮选型或后续有界转换试验的前置条件。

## 选型结论

**免费方案优先 LibreOffice headless，商业备选 Aspose.Words + Aspose.Slides 的 Python via .NET 包。** 两条路线都能覆盖 Word／PPT 转 PDF，均不依赖 Microsoft Office。先用 LibreOffice 做小范围样本转换试验，再决定是否需要商业引擎；当前没有本机质量证据，不能承诺隐藏页完整、分页一致或复杂排版无损。

用户／主代理同步的环境结果是：本机及 bundle 未找到 LibreOffice，尚未安装。本轮没有重新探测；因此“优先 LibreOffice”是后续试验建议，不是当前已有可用转换器的结论。

## 方案比较

| 方案 | 实际渲染引擎／依赖 | Word → PDF | PPT → PDF | 免费、评估及授权 |
| --- | --- | --- | --- | --- |
| LibreOffice headless | 本机 LibreOffice Writer／Impress 的导入、布局与 PDF 导出引擎；headless 仅是不显示界面的运行方式，仍需 LibreOffice 程序；不需 Microsoft Office | 官方过滤器支持 DOC／DOCX 导入，`writer_pdf_Export` 输出 PDF | 官方过滤器支持 PPT／PPTX 导入，`impress_pdf_Export` 输出 PDF | 免费开源，无商业评估水印／页数配额；官方以 MPL 2.0 提供，包含其他开源许可组件，随软件分发应保留相应许可信息。[L1–L3] |
| LibreOffice 的 Python 封装（以 unoserver 为例） | 通过 UNO 调用 LibreOffice，不自带新渲染引擎；服务端需 LibreOffice 及匹配的 Python／UNO 环境 | 随底层 Writer 能力 | 随底层 Impress 能力 | unoserver 为 MIT；底层 LibreOffice 授权另计。README 明确 Windows／Mac 支持尚未经该项目测试，当前 Windows 项目宜先从官方 CLI 试验。[L4] |
| docx2pdf | Microsoft Word 本身；Windows 用 `win32com`／COM，macOS 用 JXA；必须安装 Word，不支持 Linux | 官方定位是 DOCX → PDF | 不提供 PowerPoint 转换 | 包为 MIT，Word 的安装和使用许可另计。封装可减少自写代码，但 Windows 路线仍依赖 Word COM，不符合本轮优先替换该依赖的方向。[D1–D3] |
| python-docx／python-pptx | 读写 OOXML 文件的对象模型，无页面渲染引擎；不需 Office | python-docx 读写 DOCX，不提供现成 PDF 导出 | python-pptx 读写 PPTX，不提供现成 PDF 导出 | 均为 MIT；适合结构提取／编辑，不能单独承担此次排版转换，旧 DOC／PPT 也不在其主格式范围。[P1–P4] |
| Aspose.Words for Python via .NET | Aspose 自有文档布局／渲染引擎，官方称模拟 Word 分页方式；不需 Word／LibreOffice | 官方支持 DOC／DOCX → PDF | 此组件不承担 PPT 转换 | 商业软件；未授权评估版加水印，并把文档大小限制为数百段落；可申请 30 天临时许可去除评估限制。[A1–A2] |
| Aspose.Slides for Python via .NET | Aspose 独立演示文稿渲染引擎，不需 PowerPoint／LibreOffice | 此组件不承担 Word 转换 | 官方支持 PPT／PPTX → PDF | 商业软件；评估输出加水印，读取演示文稿文字有截断限制；官方说明转换可用但输出带水印，可申请 30 天临时许可。[A3–A4] |

Aspose 两个组件需对应产品授权或覆盖它们的套件授权；不能把下载／pip 可安装理解为永久免费。采购需按开发者、部署及是否对外提供 SDK/API 等用途核对官方许可类型，不在本轮认定某种许可证必然覆盖 CowAgent 的全部分发场景。[A5]

## 隐藏幻灯片：官方设置与本机结果分开

用户后续质疑对隐藏页的优先投入，当前先验证常规可见页面、图表和文字；以下设置仅作后续参考，不作为正常转换开发的前置。

- **LibreOffice Impress**：官方 PDF 属性 `ExportHiddenSlides` 默认 `false`；需显式设为 `true`。CLI 的 PDF 参数可携带该布尔属性；unoserver 提供 `--filter-option` 传递过滤器选项，底层仍是 LibreOffice。以上是官方能力与接口说明，本轮未验证封装传参及导出页数。[L2、L4]
- **Aspose.Slides**：`PdfOptions.show_hidden_slides` 默认 `False`；设为 `True` 并把选项交给 PDF 保存操作，官方提供包含隐藏页的转换示例。本轮未实跑。[A3、A6]
- **docx2pdf** 不处理 PPT；**python-pptx** 没有 PDF 导出器，不能为它填写“导出时保留／丢弃隐藏页”的结论。

未转换这些第三方库的本机样本；本轮没有任何第三方方案的隐藏页或排版质量实测结果。既有 COM 探针的特定失败不能外推为上述库的行为。后续小样本试验应核对隐藏页内容、源幻灯片顺序与 PDF 页序，并目视检查中文字体、公式、图表和跨页表格；这属于质量验证建议。

## 已有技能线索对结论的影响

以下来自主代理本轮读取技能后的同步，调研子代理未重复执行技能脚本或查询 bundle：

- `pdf`／`pdf:pdf` 中的 pypdf、pdfplumber 用于已有 PDF 的提取／校验；ReportLab 创建新 PDF；Poppler 渲染已有 PDF。这些工具不能直接补上 Office 原件的布局渲染引擎。
- `office-mcp` 的 `docx_to_pdf` 明确需要 external tool；`office-to-md` 采用 MarkItDown，走内容转换而非 Office 页面排版渲染。二者不能据此认定为独立的高保真 Office → PDF 后端。
- `documents` 的 `render_docx.py` 背后仍是 LibreOffice，强化了优先复用成熟引擎的选择。技能建议从 `load_workspace_dependencies` 返回的 bundled 路径寻找依赖，但本轮未调用该工具；技能中提到该路径不等于当前环境包含 LibreOffice，当前同步结果为未找到。

因此，优先方向保持为 **LibreOffice 官方 headless CLI 做免费有界试验；需要商业包时再比较 Aspose 两组件**。docx2pdf 只适合作为接受本机 Word 依赖时的 DOCX 便捷封装；python-docx／python-pptx 留作结构处理。上述选择是文档核对后的建议，不是通过本机转换验收的结论。

## 官方来源

- [L1：LibreOffice 启动参数（headless／convert-to）](https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html)；[文件转换过滤器清单](https://help.libreoffice.org/latest/en-US/text/shared/guide/convertfilters.html)。
- [L2：LibreOffice PDF CLI 参数（含 ExportHiddenSlides）](https://help.libreoffice.org/latest/en-US/text/shared/guide/pdf_params.html)。
- [L3：LibreOffice 官方许可证](https://www.libreoffice.org/licenses/)。
- [L4：unoserver 官方 README](https://github.com/unoconv/unoserver/blob/master/README.rst)；[包元数据（MIT）](https://github.com/unoconv/unoserver/blob/master/pyproject.toml)。
- [D1：docx2pdf 官方说明](https://github.com/AlJohri/docx2pdf)；[D2：实际调用源码](https://github.com/AlJohri/docx2pdf/blob/main/docx2pdf/__init__.py)；[D3：MIT 许可证](https://github.com/AlJohri/docx2pdf/blob/main/LICENSE)。
- [P1：python-docx 官方文档](https://python-docx.readthedocs.io/en/latest/)；[P2：MIT 许可证](https://github.com/python-openxml/python-docx/blob/master/LICENSE)。
- [P3：python-pptx 官方文档](https://python-pptx.readthedocs.io/en/latest/)；[P4：MIT 许可证](https://github.com/scanny/python-pptx/blob/master/LICENSE)。
- [A1：Aspose.Words DOC／DOCX 转 PDF 与布局引擎说明](https://docs.aspose.com/words/python-net/convert-a-document-to-pdf/)；[系统依赖](https://docs.aspose.com/words/python-net/system-requirements/)。
- [A2：Aspose.Words 评估限制／临时许可证](https://docs.aspose.com/words/python-net/licensing/)。
- [A3：Aspose.Slides PPT／PPTX 转 PDF（含隐藏页示例）](https://docs.aspose.com/slides/python-net/convert-powerpoint-to-pdf/)。
- [A4：Aspose.Slides 独立引擎及评估限制](https://docs.aspose.com/slides/python-net/evaluate-aspose-slides/)。
- [A5：Aspose 官方许可类型](https://purchase.aspose.com/policies/license-types/)。
- [A6：Aspose.Slides PdfOptions.show_hidden_slides API 与默认值](https://reference.aspose.com/slides/python-net/aspose.slides.export/pdfoptions/show_hidden_slides/)。

来源为核对日访问的官方在线版本／仓库分支，可能随版本变化；本轮未锁定或验证具体安装版本。
