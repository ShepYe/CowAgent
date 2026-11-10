# T-03 资料来源、解析与版本
Status: ready-for-agent
Type: task

规格：[PA-05/06](../spec.md#5-资料身份解析与发布pa-05pa-06pa-08)。执行安排：计划第 5 节 P-03/T-03，验证 V-04。

## 实施与证据

P-03 在实现前运行代表探针：三页文本 PDF 物理页、DOCX 段落与表格单元格、XLSX B2 公式无缓存可区分。Python 3.14 系统解析依赖运行通过；项目 Python 3.11 环境补齐后也通过。

共享全文解析 `agent/knowledge/parser.py` 支持 PDF/DOCX/XLSX/MD/TXT，正文预算 10 MiB、PDF 2000 页、单元格/输出单位 200000；超限整体失败，绝不发布截断稿。DOCX 实测 2200 段落后表格尾部仍完整。公式与缓存分别标记，不执行宏、链接、重算。混合扫描 PDF 的图片无文字页需 OCR。

原件、解析与来源清单在工作空间 `.personal_sources/`，与只扫描 MEMORY.md、memory/、knowledge/ 的旧索引隔离。原件 SHA-256 校验、同资料版本复用、来源回执独立保留，未知 schema 拒写。复制原件采用 fsync+replace。

## Comments

2026-11-10：实现及离线解析/来源测试通过；真实用户代表文件与 Windows 安装版联调由主任务验收。未提交。


## 封包前冻结记录

2026-11-10：生产代码已完成针对性测试后冻结，后续仅更新工单。Read 对 current_task_id 中 TaskStore 已授权附件按登记 filename 解析无扩展名 hash 原件，分页返回 document/revision/parse、真实位置、has_more/next_offset；只解析留档，不发布，临时资料可直接回答。PDF 测试夹具改用 pypdf 生成，不依赖 reportlab。原件相同 request_id 与 ingest 复用。

SourceService 的 publication 列为旧 v1 的增量添加；仅首次加列后回填 current、state_revision、purpose/validity 与投影哈希均有效的最新 publication，撤回 current=NULL 不恢复，普通重启不反复回填。无扩展名三格式闭环、迁移及阅读已通过测试。


### 主任务收口

本票实现与离线集成已完成，最终安装、新 UI、Agent 工具、重启及恢复结果和真实账号待办统一见[首期交付记录](../../../docs/codex/logs/2026-11-10-个人助手首期交付.md)；以上代理阶段的未运行项按该记录更新，不重复维护验收正文。
