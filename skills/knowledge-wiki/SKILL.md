---
name: knowledge-wiki
description: 用户分享资料、对话产生可复用结论，或查询和整理知识库时，提炼并维护有出处的主题知识页。
metadata:
  cowagent:
    always: true
---

# Knowledge Wiki

Maintain a persistent, structured knowledge base in the `knowledge/` directory.

## Core Operations

### 1. Ingest — User shares an article, document, or resource

1. 按上下文自主决定 `long_term/transient/waiting`，不逐份询问。仅缺少必要版本关系等信息才 waiting。临时资料及派生结论只留原始档案，不写知识、记忆、日记或摘要。
2. 有 personal task_id 时先调用 `knowledge_ingest(task_id,purpose,reason)` 登记用途。`organization_required` 表示尚未完成：继续读取、提炼并提交 pages，不结束或询问要不要收录。
3. 先查 `knowledge/index.md`，用 `memory_search/read` 找相关主题页及其当前版本；空库目录不存在时按新建主题继续。沿用当前有效分类，`category_mode=manual` 保持本人固定的选择；恢复自动后由后续整理判断。更新使用稳定 `page_id` 和当前 `page_version`。
4. 实际用 `read` 读取授权附件，按 `has_more/next_offset/next_pages` 续读。返回 `reading_id` 时，直接阅读工具提供的 PDF；在紧接的响应中保存带位置的具体主题发现，或直接提交成品，然后再调用其他工具。媒体仅在这一轮提供，后轮复用自己的发现；无需全书逐页转写。一源可拆多页，多源可合一页。旧路线保留 `document_id/revision_id/parse_id/ordinal`，按 visual_continuation 补读缺口，显式 parse_id 保持原解析。仅分页读取已有文字用 visual=false；未读取图表保留缺口。
5. 提炼知识正文，给清晰标题、分类、适用条件和关键结论，每个正文块附一处或多处真实出处。为每篇知识写一句 `summary`，说明该页主题、范围或用途，建议 30～60 字；依据本页正文，不重复标题，不加入正文没有的结论、数值或未核验图表解释。普通相关关系与作为论据的派生依赖分别登记。
6. 提交 `knowledge_ingest` 的完整 `pages` 数组。每页结构：`{title,category,summary,blocks:[{content,sources:[{document_id,revision_id,parse_id,ordinal}]}],related_page_ids:[],depends_on:[{page_id,page_version}]}`。每页 `pages[].summary` 必填，去除首尾空白后为 1～160 个 Unicode 码点的单行纯文本，禁止换行及控制字符；任务顶层 `summary` 不能代替各页摘要。新增和更新正文都提交对应摘要；收到 invalid_request 时修正输入并重提，不截断摘要或以标题、分类、正文首段冒充。新页可省略 page_id；同批新建多个相关页时先各自指定稳定 page_id，再用 related_page_ids 关联同批页。更新页传 page_id 和 expected_version。相关链接目标须为已有页或同批创建页；派生依赖须为已核验发布版本。
7. 受控发布维护来源、页版本、索引及日志，无需另用 write/edit 修改托管页和目录索引。`running` 继续处理；`pending_index` 接续索引。只有当前有效且索引核验通过的 searchable 回执才能报告可检索；视觉仍有缺口时单列未处理范围，不能称整份资料已完整理解。回复仅展示标题、分类、可读出处及状态，资料/版本/解析/发布编号和状态修订留在工具参数中。

托管页位于 `knowledge/wiki/`，旧投影位于 `knowledge/managed/`，都通过受控发布维护。更正、撤回和用途改变调用 `knowledge_source_action(document_id,expected_state,reason,action)`；来源失效时受影响整页与派生页暂停。恢复长期资格后重新读取、整理和发布，不将资格恢复称为可检索。冲突时核对当前依据并重新整理。

### 原生阅读提交

只在 `read` 返回 reading_id 时使用。直接从实际提供的PDF图文提炼主题正文与每页 summary；PDF原件物理页来自 page_map，Office观察使用 source_locations 返回的完整 location：Word是阅读副本页，PPT是源幻灯片。局部不清时用 `read(path,pages="单页",region=[left,top,right,bottom])` 放大，坐标以左上为原点、取值0到1，观察位置沿用返回的完整region；局部凭据不算整页覆盖，提交时同时保留主阅读的reading_coverage。跨批同主题合并。

XLSX先读单元格及 `spreadsheet` 结构，按 `worksheet` 和 `objects=[object_key]` 显式补读内嵌图片，出处定位到工作表／对象／锚点。公式缓存状态为missing或unverified时按原值说明，不重算；未渲染图表保留缺口。当前图片回执没有长期reading_id，不能把图内观察伪装成单元格文字来源发布。

每次提交生成稳定 `reading_submission_id`，重放沿用同一标识及相同载荷。`reading_evidence` 每项为 `{reading_id,evidence_key,segment_id,location:{kind:"pdf",physical_page:原件物理页},observation:具体所见,type:"ocr"或"visual_interpretation"}`。图中文字转写用 ocr，图形关系解释用 visual_interpretation；不能用“附件已读”占位。正文 sources 使用 `{reading_id,evidence_key}`；已有文字出处仍用原四元身份，两种形状互斥。

同次提交附 `reading_coverage:[{reading_id,segment_id}]`，只列已完成阅读和主题整理的批次。工具返回 running 时按 next_pages 继续剩余目标；部分主题可检索不等于整任务完成。已成功的批次和已保存观察直接复用，不重复发布。遇到 reading_incomplete、未知响应或取消，保留已发布成果与缺口，不能缩小目标后宣称完整成功。

### 2. Synthesize — Conversation produces valuable structured knowledge

1. 有接收任务的对话结论沿用上述受控整理与来源关联。普通会话引用托管资料产生的新结论，调用 knowledge_ingest(purpose=long_term,pages,reason)（省略 task_id）受控发布，保留真实出处及 depends_on；检索问答本身无需重复收录。普通无来源对话知识沿用下列目录约定，记录对话出处；临时任务及派生内容始终排除。
2. Create a knowledge page under the appropriate category
2. Update related pages with cross-references
3. Update `knowledge/index.md` and `knowledge/log.md`

### 3. Query — User asks about accumulated knowledge

1. Check `knowledge/index.md` in context to select pages. Large indexes contain summary previews of at most 40 characters marked truncated and may omit complete entries with a count. Previews only help select pages. 按需用 `read` 的 offset/limit 分页读取完整索引，或 `memory_search` 找页；缩略摘要只供选页，不作为事实依据。
2. Read selected pages' full bodies and currently valid original sources with `read`, checking versions and source locations before answering. Follow pagination when needed; do not answer from summary previews or search snippets alone.
3. 回读当前有效原文，回答提供知识页及足以定位原件的出处。PDF 用物理页，DOCX 用段落/表格，XLSX 用工作表/单元格；缓存缺失不猜公式值。失效或撤回页不能作为有效依据；旧页缺摘要时直接读正文，不猜摘要。图谱只表示知识关系，不代替原件或成功回执。

## Page Format

```markdown
# Page Title

> Source: <URL or description of the original material>

Content here. Cross-reference related pages with markdown links:
[Related Page](../category/related-page.md)

## Key Points

- ...

## Related

- [Page A](../category/page-a.md) — how it relates
- [Page B](../category/page-b.md) — how it relates
```

The `> Source:` line records where the knowledge came from (URL, document name, conversation, etc.). Always include it when the material originates from a specific source.

Cross-references build a knowledge graph. When creating or updating a page, link to related pages and update those pages to link back. **Only link to pages that already exist** — if a concept deserves its own page, create it first, then add the link.

## Index Format (`knowledge/index.md`)

Flat list, one line per page: `[Title](path) — one-line summary`. Group by category (matching subdirectories). No tables, no emoji.

```markdown
# Knowledge Index

## Category A
- [Page Title](category-a/page-slug.md) — one-line summary

## Category B
- [Page Title](category-b/page-slug.md) — one-line summary
```

Category names and structure are flexible — follow whatever organization already exists in the index, or create new categories based on the content.

## Log Format (`knowledge/log.md`)

Append-only, newest at bottom:

```markdown
## [YYYY-MM-DD] ingest | Page Title
## [YYYY-MM-DD] synthesize | Page Title
```

## Guidelines

- **File naming**: lowercase kebab-case (e.g. `machine-learning.md`)
- **One topic per page**: link between pages rather than duplicating
- **Update, don't duplicate**: if a page exists, update it
- **Cross-reference**: every page should link to related pages; keep the knowledge graph connected
- **Index is mandatory**: 托管页发布自动维护主题索引；普通未托管知识页修改后更新 `knowledge/index.md`。
- **Be concise**: capture essence, not copy entire sources
- **Full paths in replies**: when referencing knowledge files in conversation replies, use the full path from workspace root (e.g. `[Title](knowledge/<category>/<slug>.md)`), not relative paths. Relative paths are only for cross-references inside knowledge pages themselves.
- **Cite sources**: when answering based on knowledge pages, include links to the relevant pages so the user can explore further.
