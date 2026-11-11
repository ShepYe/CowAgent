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
3. 实际用 `read` 读取授权附件，按 `has_more/next_offset/next_pages` 获取所需内容，保留 `document_id/revision_id/parse_id/ordinal`。PDF 首次读取启用 visual，后续用返回 parse_id 续读；有 visual_continuation 时分批继续待视觉页。仅分页读取已有解析稿用 visual=false。检查 visual_coverage，区分原文文字、OCR 转写及视觉解释；未读取图表不能作为结论依据，失败或配置不可用如实记录。
4. 查 `knowledge/index.md`，用 `memory_search/read` 找相关主题页，沿用分类。每页一个主题；一源可拆多页，多源可合一页。更新使用稳定 `page_id` 和当前 `page_version`，不按文件名猜身份。
5. 提炼知识正文，给清晰标题、分类、适用条件和关键结论，每个正文块附一处或多处真实出处。普通相关关系与作为论据的派生依赖分别登记。
6. 提交 `knowledge_ingest` 的完整 `pages` 数组。每页结构：`{title,category,blocks:[{content,sources:[{document_id,revision_id,parse_id,ordinal}]}],related_page_ids:[],depends_on:[{page_id,page_version}]}`。新页可省略 page_id；同批新建多个相关页时先各自指定稳定 page_id，再用 related_page_ids 关联同批页。更新页传 page_id 和 expected_version。相关链接目标须为已有页或同批创建页；派生依赖须为已核验发布版本。
7. 受控发布维护来源、页版本、索引及日志，无需另用 write/edit 修改托管页和目录索引。`running` 继续处理；`pending_index` 接续索引。只有当前有效且索引核验通过的 searchable 回执才能报告可检索；视觉仍有缺口时单列未处理范围，不能称整份资料已完整理解。回复仅展示标题、分类、可读出处及状态，资料/版本/解析/发布编号和状态修订留在工具参数中。

托管页位于 `knowledge/wiki/`，旧投影位于 `knowledge/managed/`，都通过受控发布维护。更正、撤回和用途改变调用 `knowledge_source_action(document_id,expected_state,reason,action)`；来源失效时受影响整页与派生页暂停。恢复长期资格后重新读取、整理和发布，不将资格恢复称为可检索。冲突时核对当前依据并重新整理。

### 2. Synthesize — Conversation produces valuable structured knowledge

1. 有接收任务的对话结论沿用上述受控整理与来源关联。普通会话引用托管资料产生的新结论，调用 knowledge_ingest(purpose=long_term,pages,reason)（省略 task_id）受控发布，保留真实出处及 depends_on；检索问答本身无需重复收录。普通无来源对话知识沿用下列目录约定，记录对话出处；临时任务及派生内容始终排除。
2. Create a knowledge page under the appropriate category
2. Update related pages with cross-references
3. Update `knowledge/index.md` and `knowledge/log.md`

### 3. Query — User asks about accumulated knowledge

1. Check `knowledge/index.md` (already in your context) for relevant pages
2. Read specific pages with the `read` tool
3. Supplement with `memory_search` if needed. 回读当前有效原文，回答提供知识页及足以定位原件的出处。PDF 用物理页，DOCX 用段落/表格，XLSX 用工作表/单元格；缓存缺失不猜公式值。图谱只表示知识关系，不代替原件或成功回执。

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
