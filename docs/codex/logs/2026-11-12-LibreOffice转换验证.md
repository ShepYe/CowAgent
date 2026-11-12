# LibreOffice 材料模块与验证

2026-11-12 10:45 +08:00

执行了[文档读取与知识入库优化方案](../plans/2026-11-11-文档读取与知识入库优化方案.md)的 Office 第三方材料子任务。遵循最新指示，优先普通可见 PPT／Word、图表和文字；隐藏页记录缺口，自动化脚本安全控制不作为本轮前置。仅新增模块、测试、探针与本日志，未修改 parser/read/reading/flow/config/备份等整合文件，未提交 Git，未调用云端 API。

## 接口与材料契约

模块：`agent/knowledge/office_materials.py`，仅依赖标准库和仓库已有 `pypdf`，使用 `subprocess.run` 参数数组调用 LibreOffice headless，Windows 指定 `CREATE_NO_WINDOW`。转换在数据根下独立临时目录及独立 UserInstallation 中运行，不改变系统关联；这里只报告调用方式，不宣称已完成自动化安全隔离验收。

```python
from agent.knowledge.office_materials import (
    OfficeMaterialError, prepare_office_materials,
    verify_office_materials, load_office_materials,
)

materials = prepare_office_materials(
    root, "originals/example.pptx",
    soffice="项目局部工具/program/soffice.com", timeout_seconds=120,
)
payload = materials.to_dict()
trusted_hash = materials.manifest_hash  # 由调用方保存到可信台账
restored = load_office_materials(root, payload, expected_manifest_hash=trusted_hash)
position = restored.location(1)  # 从 1 开始
```

- `PreparedOffice` 提供 `original_relpath/original_hash/source_format`、`engine/options/preparation_version`、`cache_key/material_relpath/material_hash/page_count/page_map`、`notes/coverage_gaps`；`to_dict()` 返回 JSON 类型及 `manifest_hash`。不含绝对路径和材料字节。
- 支持 `.docx/.pptx`；二进制 `.doc/.ppt`、XLSX 等返回 `unsupported_format`，没有悄悄回退到 COM 或云端。
- Word 的 `location()` 返回 `kind=word_reading_copy_page` 与 `reading_copy_page`，不声称这是 Word 原件固定页或段落锚点。
- PPT 从源 presentation 播放列表及关系读取 `sldId`、源序号、部件路径与可见性。每个可见源幻灯片在工作副本中单独导出，严格要求单页 PDF，再合并；映射包含 `source_slide_id/source_slide_number/source_part/pdf_page` 及 `mapping_method=isolated-source-slide`。不依赖幻灯片文件名排序、文本匹配或整个文件页数相同推断映射。
- 隐藏幻灯片不渲染，缺口记录其源 ID。备注从 notesSlide 正文单独提取为 `speaker_notes`，带源幻灯片 ID 与备注部件路径，过滤页码、页脚等占位符；备注图片和排版没有渲染。
- 原件只读，转换前后及材料恢复时核验 SHA-256。缓存键绑定源相对身份/hash、模块版本、引擎版本/可执行文件 hash、导出过滤器/选项/策略。独立目录转换后仅发布完整 PDF 与 manifest；失败不发布成功清单。缓存命中仍校验原件、PDF hash、页数和源映射，损坏缓存直接失败。
- `verify_office_materials(root, materials)` 供可信对象只读复核，不调用引擎；`load_office_materials` 要求调用方可信 hash，支持异根恢复。`prepare` 即使有缓存也要求引擎版本可确认，避免将缺依赖表述为当前转换已通过。
- `OfficeMaterialError.code` 包括 `invalid_path/invalid_request/unsupported_format/source_unavailable/invalid_package/engine_unavailable/conversion_timeout/conversion_failed/invalid_pdf/mapping_failed/no_visible_slides/source_changed/material_tampered/material_unavailable/invalid_manifest/cache_invalid`。`timeout_seconds` 限每次引擎调用，PPT 总耗时随可见页数增加，不表示整个文档统一总超时。

本次按 major-change-docs-gate 做受影响材料边界的定向检查：沿用现有规格与最新授权；原件身份、派生阅读副本、位置和失败/恢复有明确契约及验证，发布/任务终态由主代理整合处理。缓存为新命名空间，没有历史数据迁移；实际引擎转换质量仍未验收，不将离线契约测试当作产品放行证据。

## 验证结果

` .venv/Scripts/python.exe -m pytest tests/test_office_materials.py -q `：**16 passed**。

覆盖原件 hash 与转换期间并发更改、相对身份与路径越界、缓存复用/损坏、源版本/引擎文件/引擎版本失效、清单篡改、异根恢复、位置越界、PPT 源顺序与稳定 ID（刻意不同于部件名顺序）、隐藏页缺口、独立备注、超时/非零退出/零退出无产物/坏 PDF、单幻灯片输出多页拒绝映射。引擎替身只替换进程边界，实际运行材料发布、PDF 解析/合并、hash 和恢复校验。

` .venv/Scripts/python.exe -m ruff check agent/knowledge/office_materials.py tests/test_office_materials.py scripts/document_reading_libreoffice_probe.py --select F `：**All checks passed**。

### 官方依赖下载

[LibreOffice 官方发行入口](https://download.documentfoundation.org/libreoffice/stable/25.8.7/win/x86_64/LibreOffice_25.8.7_Win_x86-64.msi) HEAD 返回 200，包大小 366,170,112 字节（约 349 MiB）。执行 `curl -L --fail --max-time 180`，180.089 秒后超时退出 28，仅取得 **47,382,271 字节**，平均约 257 KiB/s，预计完整下载二十余分钟，按用户“不盲耗时”要求停止。

不完整包保留于 `tmp/libreoffice-tool/LibreOffice_25.8.7_Win_x86-64.msi.partial`，不可执行或作为安装包使用。本轮未提取、未安装 LibreOffice，未启动 MSI，不修改系统默认关联。官方 DOCX skill 的缺失 bundle 不作为软件接口开发阻塞，也未调用其工具。

### 用户已有样本实跑

命令：` .venv/Scripts/python.exe scripts/document_reading_libreoffice_probe.py --report tmp/document-reading-office/libreoffice-report.json `。探针真实退出码 **1**，两个样本均返回 **engine_unavailable**；没有生成成功 Office 材料或宣称转换成功，报告 `cloud_api_called=false`。

| 样本 | 大小 | SHA-256 | 结果 |
| --- | ---: | --- | --- |
| `fixtures/sample.docx` | 51,447 | `3e4a37db34451a452fb001444916da4a3c4cf76914528b8563297abc856baca8` | 约 0.031 秒，原件 hash 不变 |
| `fixtures/sample.pptx` | 49,801 | `c0f6badcb194aa18dc3ecbe6de437ccf30a311b181c5a14fd1e291e9851bb192` | 约 0.032 秒，原件 hash 不变 |

可复跑入口：给探针传入 `--soffice <局部工具/program/soffice.com>`（Linux 用 soffice），可重复 `--source <根内相对路径>`。成功后报告包含引擎信息、材料清单、PDF 文字和二次缓存复用结果，仍需对图表和文字排版作视觉验收。

## 剩余验证

没有真实 LibreOffice 引擎运行证据，普通可见页、图表、文字的排版保真尚未通过；隔离单幻灯片策略也须在实际 LibreOffice 上确认。字体替换、公式、修订与批注、Word 段落锚点、隐藏页渲染、备注图像/排版和旧格式均未验收。本模块提供转换与材料准备，不创建阅读凭据、不改变任务状态、不发布知识；主代理应保留这些覆盖缺口。

## 继续取得引擎与真实转换（2026-11-12 10:58 +08:00）

用户进一步授权持续后台下载，不再以最初 180 秒为停止条件。本阶段只取得局部引擎、执行转换与验证并追加本日志，**没有改动模块接口、实现、测试或探针脚本**。以下结果更新上一阶段“无真实引擎”的状态。

### 下载与官方核验

从官方发行响应的 `Link: rel=duplicate` 选取北外镜像，原 47,382,271 字节文件直接断点续传。镜像响应 `206 Partial Content`，`Content-Range: bytes 47382271-366170111/366170112`，约 3 分钟取得剩余部分，最终大小 **366,170,112 字节**。中途按官方 Metalink 验证前 599 个完整 256 KiB 分块，未发现不匹配。

- [实际镜像地址](https://mirrors.bfsu.edu.cn/libreoffice/libreoffice/stable/25.8.7/win/x86_64/LibreOffice_25.8.7_Win_x86-64.msi)来自官方镜像列表。
- [官方 SHA-256](https://download.documentfoundation.org/libreoffice/stable/25.8.7/win/x86_64/LibreOffice_25.8.7_Win_x86-64.msi.sha256)与本机 `Get-FileHash` 一致：`ecdb65e76f5e91dc198b8c8dce5b5d6e1eb12fea6023553e52b591afd10b619d`。
- `Get-AuthenticodeSignature`：包签名 **Valid**，签名者 **The Document Foundation / LibreOffice Build Team**，证书指纹 `6480532A562B36D1BFFFC5B5EACF7C31E74E9B28`，时间戳签名者 `Certum Timestamp 2026`。
- 实际引擎 `soffice.com` 的 Authenticode 签名亦为 **Valid**，同属 The Document Foundation。
- 证据保留于 `tmp/libreoffice-tool/package-verification.json`、官方 `.sha256/.meta4` 与 `resume-headers.txt`；完整 MSI 已去掉 `.partial` 后缀。

### 项目局部提取

最初把校验、重命名与 `Start-Process` 提取组成一条命令，被自动审批以 `blocked by policy` 拒绝，未提供具体原因。拆为独立校验及固定路径命令后获准执行：

```powershell
msiexec.exe /a D:\CodeAndDesign\CowAgent\tmp\libreoffice-tool\LibreOffice_25.8.7_Win_x86-64.msi /qn TARGETDIR=D:\CodeAndDesign\CowAgent\tmp\libreoffice-tool\25.8.7 /norestart /L*V D:\CodeAndDesign\CowAgent\tmp\libreoffice-tool\admin-extract.log
```

`admin-extract.log` 在 10:52:19 记录 `MainEngineThread is returning 0`。使用 `/a` 管理提取、`/qn` 静默运行，没有执行系统安装 `/i` 或修改默认关联；运行结束检查系统/HKCU 卸载注册表未发现 LibreOffice 安装项。

**主代理可用引擎路径：** `D:/CodeAndDesign/CowAgent/tmp/libreoffice-tool/25.8.7/program/soffice.com`。

版本：`LibreOffice 25.8.7.3 30742500f2d3eb4366ac312fa33d3dcabdb3eba5`；可执行文件 SHA-256：`42c9eda6b670cb8715b5c29d1dedb1830de5b95964f8199a0a25035a85c6fc0e`。继续使用现有材料模块的独立工作副本、独立 UserInstallation 和 headless 调用，没有启动可见 Office 窗口。

### 实跑结果

```powershell
.venv/Scripts/python.exe scripts/document_reading_libreoffice_probe.py --soffice D:/CodeAndDesign/CowAgent/tmp/libreoffice-tool/25.8.7/program/soffice.com --source fixtures/sample.docx --source fixtures/sample.pptx --source fixtures/chart-visible.pptx --timeout 120 --report tmp/document-reading-office/libreoffice-real-report.json
```

真实退出码 **0**。没有云端请求。表中首次耗时包含首次转换、校验和探针要求的第二次缓存调用；复跑耗时也包含两次缓存调用及引擎版本确认。

| 样本 | PDF 页数 | 首次耗时 | 完整复跑耗时 | 原件 hash 不变／缓存复用 |
| --- | ---: | ---: | ---: | --- |
| 原有 `sample.docx` | 4 | 22.046 秒 | 4.296 秒 | 均为 true |
| 原有 `sample.pptx` | 2 | 24.891 秒 | 3.797 秒 | 均为 true |
| 补充 `chart-visible.pptx` | 1 | 16.797 秒 | 3.813 秒 | 均为 true |

原有两个样本 hash 与上阶段完全一致。补充样本在 tmp 内用 python-pptx 新建，包含真正的 `ppt/charts/chart1.xml` 与嵌入数据，其源图表缓存值是 **6、3**，不是预绘制图片；SHA-256 为 `ef4628f76f11971fcc9ed6efe281021b870c3af6f741ba5d66d9f3c442ef58cd`。原有样本包含位图及 PPT 矢量箭头，未包含原生数据图表部件，因此另补此项证据。

**实际 PPT 映射验证通过：** 源可见幻灯片 1（`sldId=256`、`ppt/slides/slide1.xml`）→ PDF 页 1；源可见幻灯片 3（`sldId=258`、`ppt/slides/slide3.xml`）→ PDF 页 2。逐个工作副本均成功导出恰好一页，最终 PDF 的“幻灯片 1／幻灯片 3”文字标记对应，未出现“幻灯片 2”。隐藏源页 `257` 仍不渲染并保留缺口；备注 1／2／3 的校验值 **80／81／82** 全部提取为独立结构，未混入普通 PDF 页。

**实际 Word 内容核验：** 4 页仅标为 Word 阅读副本页。正文 `42 dB`、公式 `P = 2 × 3 = 6 W` 可读；70 行表格“站点 01…70”及功率“10…79”逐项提取验证完整，页眉页脚保留。PDF 保留与原 PNG 像素完全一致的图像资源，图内 `37 dB` 在渲染中可见。

### 渲染与目视

使用本机 Poppler `pdftoppm -scale-to 1400 -png`，输出 7 张 PNG，逐页实际查看：

- `tmp/document-reading-office/libreoffice-render/sample_docx/page-1.png` 至 `page-4.png`：普通中文、图像、跨页表格均可读，未发现截断或缺行；图中“青苗站 → 山泉站，预算 37 dB”完整。
- `tmp/document-reading-office/libreoffice-render/sample_pptx/page-1.png` 至 `page-2.png`：可见源页 1／3 标题正确，图片、箭头形状、文字“输入 6 W／输出 3 W”保留。
- `tmp/document-reading-office/libreoffice-render/chart-visible_pptx/page-1.png`：柱形高度 **6／3**、标签“青苗站／山泉站”、纵轴 **0～8** 和单位 W 均清晰，无可见错位；PDF 页面无位图资源，图表以绘图内容导出。

注意：PDF 页面资源字典可能共享未实际绘制的图像，资源枚举不能代表每页可见图片数；可见图像由渲染结果判定。

### 剩余范围与交接证据

常规可见样本、图像、简单矢量图形、简单原生柱状图、跨页表格和源幻灯片映射已获得本机真实转换与目视证据。**Word 修订层实际同时显示删除的“旧预算 12 dB”和插入的“新预算 42 dB”**；批注锚点有图标，批注全文未进入 PDF。该结果只能作为包含修订标记的阅读副本，不能标成“仅最终修订内容”。复杂公式、字体替换、其他图表类型、Word 段落锚点、隐藏页渲染、备注图片/版式和旧二进制格式仍未验收。本阶段未修改导出语义或接口来掩盖这些范围。

机器证据：`tmp/document-reading-office/libreoffice-real-report.json`（首跑清单/文字/hash/缓存）、`libreoffice-cache-report.json`（完整复跑）、`libreoffice-validation.json`（结构断言、图像资源像素核验、目视结论），后者 `semantic_checks_passed=true`。三个成功材料均通过可信 manifest hash 的 `load_office_materials` 恢复校验；结束时未发现本次遗留 soffice 进程。材料目录分别为：

- Word：`office_materials/9b56d95582a0e0bf18f19450be2a4de49998f3d61dcb5f8e6ac145bf4072bacf/reading.pdf`。
- PPT：`office_materials/e0281e0f5cf3e26be05d1e9dbed0a76970446be9a9929b896f63124726cbf3a9/reading.pdf`，对应清单同时保存可靠映射与独立备注。
- 原生图表：`office_materials/a0c03fbb5fefa06c02ba9eda17151e3d3de409c5e4d1c47df99c4d18a7e01109/reading.pdf`。

本次没有新增产品代码变更，因此没有重复无关测试；上一阶段 16 项契约测试结果保持为当时证据。真实模型阅读、凭据与知识发布仍由主代理验证，本模块不自行推进任务终态。

## Word 最终正文工作副本（2026-11-12 11:26 +08:00）

执行了[文档读取与知识入库优化方案](../plans/2026-11-11-文档读取与知识入库优化方案.md) O-02 的独立修订消解子任务，对应规格 5.8 RD-02、V-20／V-25。用户已授权实现，继续暂不处理自动化脚本安全和隐藏页；只修改 `agent/knowledge/office_materials.py`、`tests/test_office_materials.py`、tmp 诊断及追加本日志，未修改 read／ingest／reading／office_reading_flow／parser／config，保留其他会话现场，未提交、未调用云端。

### 实现与兼容检查

- DOCX 只在转换工作包中接受修订：删除 `w:del`／`w:moveFrom`，展开 `w:ins`／`w:moveTo`；删除属性历史，保留现值。兼顾删除行、删除单元格、插入行／单元格、可证明相邻的删除段落标记合并。正文、页眉页脚、脚注尾注等 `word/*.xml` 一并处理，媒体等非 XML 字节保持不变。
- 使用现有 python-docx 所依赖的 lxml 保留命名空间声明，避免 ElementTree 重写前缀后破坏 `mc:Ignorable`。没有新增安装或依赖配置变更。
- 修订及批注提取到 `notes`，标记 `layer=additional/current_fact=false`；保留原件部件路径、原件 XPath、修订类型／ID、文字和原始 XML。批注另有原件锚点路径列表；工作副本清除批注正文及标记，正文锚定文字保留。当前样本共两项修订、一项批注，旧预算只在修订附加层出现。
- 明确升级 Word `preparation_version=libreoffice-materials-v2`、`options.word_view=ooxml-final-view-v1`，两者参与缓存身份。PPT 保持 v1 策略与原身份计算。v2 Word 恢复重新从原件核对附加层及策略；v1 按旧语义核对，不补写 notes、不追认最终正文、不改 cache_key、manifest hash、原件或 PDF 身份。未知版本拒绝恢复。
- 对未可靠支持的单元格合并修订、冲突修订、自定义 XML 范围修订、无法证明相邻关系的删除段落标记、Strict OOXML，返回 `invalid_package`，不发布最终正文成功清单。复杂结构修订尚非通用覆盖承诺。

major-change-docs-gate 受影响项定向复核：规格最终正文与附加层规则、O-02 实施边界、V-20／25 验证映射一致；新策略独立缓存、旧版按原版本恢复、原件及已发布材料不迁移，失败不覆盖旧材料。以本段记录授权范围内的检查和证据，不扩大为工单 16 整体放行。

### 测试与真实转换

- `pytest tests/test_office_materials.py tests/test_office_reading_flow.py -q`：**49 passed**（材料 21 项，既有阅读流程 28 项）。新增覆盖修订／移动／属性历史、跨 Word 部件、批注可定位与正文隔离、命名空间／媒体保留、结构修订与明确拒绝、v1/v2 缓存隔离及旧身份恢复。阅读流程测试文件仅执行，未改动。
- `ruff check agent/knowledge/office_materials.py tests/test_office_materials.py --select F`：**All checks passed**。
- 使用已校验的 LibreOffice 25.8.7.3 真实重转 `fixtures/sample.docx`，首次转换连同缓存复核耗时 **14.296 秒**，4 页，缓存复用成功。原件 SHA-256 仍为 `3e4a37db34451a452fb001444916da4a3c4cf76914528b8563297abc856baca8`。
- PDF 文字层断言：删除的“旧预算 12 dB”消失；插入的“新预算 42 dB”保留；站点 01～70 与对应功率 10～79 逐行完整。图像资源与原图像素一致，各页文字层保留原页眉页脚。附加层带原件定位且不作为当前事实。新清单与原真实 v1 清单均通过可信 manifest hash 恢复，旧清单／原 PDF hash 未变。

新 PDF：`tmp/document-reading-office/office_materials/6886d198bfbcf87af563fa2f6accce6969bcb42e3f3e3a1e7685534d4e78234a/reading.pdf`，SHA-256 `fdf760f3339ad31464894ca2836b4925f904c0c2806a08ae4c26ef2c04c2012f`。机器报告：`libreoffice-final-view-report.json`、`libreoffice-final-view-validation.json`；诊断入口：`tmp/verify_word_final_view.py`。

### 实际可见范围

Poppler 渲染到 `tmp/document-reading-office/libreoffice-final-view-render/page-1.png` 至 `page-4.png`，四页逐页实际查看。第 1 页无旧预算，新预算 42 dB、图内“青苗站 → 山泉站，预算 37 dB”、公式及表格起始可见，批注图标已去除、锚定正文仍在；70 行表格跨四页连续。

第 1／4 页页眉页脚可见；第 2／3 页表格延伸到页边，不能声称页眉页脚在这两页实际可见。与旧版渲染比较，第 2／3／4 页像素完全一致，这是样本原有转换版式，不是本次修订消解引入的变化。故“文字层各页保留”与“渲染各页可见”分别报告，不将资源／文字枚举替代视觉结论。

本轮只验收该 Word 样本的最终正文与材料兼容，不宣称复杂公式／字体／版式、附加层 PDF 页映射／排版、隐藏页、安全隔离或真实模型阅读及知识发布已验收。
