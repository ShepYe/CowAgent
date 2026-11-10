---
name: knowledge-wiki
description: Manage the personal knowledge wiki. Use when the user shares articles, documents, or asks to organize knowledge; when a conversation produces insights worth preserving as structured knowledge; or when the user asks about the knowledge base.
metadata:
  cowagent:
    always: true
---

# Knowledge Wiki

Maintain a persistent, structured knowledge base in the `knowledge/` directory.

## Core Operations

### 1. Ingest — User shares an article, document, or resource

1. 若上下文包含 personal task_id，先通过 `knowledge_ingest` 处理接收任务附件。主 Agent 自主判断 long_term/transient/waiting，不逐份询问。缺少替代关系等必要信息才 waiting。工具返回 published/searchable 才能说可检索；失败与 pending_index 如实说明。
2. 托管页 `knowledge/managed/` 禁止用 write/edit 修改或复制为未托管页。更正/撤回/用途改变调用 `knowledge_source_action`，携带 document_id、expected_state、reason；使用来源服务状态修订；引用需包含 document_id/revision_id/parse_id/publish_id 及真实原文位置，并回读有效原文。PDF 使用物理页，DOCX 使用段落/表格位置，XLSX 使用工作表和单元格，公式缓存缺失不得猜值。
3. 临时附件和派生结论仅保留原始留档，不写知识、日记、摘要或 Deep Dream。长期附件由来源工具发布，不另存无版本摘要。
4. Read and understand the source material
2. Extract key facts, insights, and structured knowledge
3. Determine the appropriate subdirectory:
   - Read `knowledge/index.md` to see existing categories
   - If a matching category exists, follow that structure
   - If not, create a new subdirectory with a clear name
4. Create the knowledge page: `knowledge/<category>/<slug>.md`
5. Update `knowledge/index.md` and append to `knowledge/log.md`

### 2. Synthesize — Conversation produces valuable structured knowledge

1. Create a knowledge page under the appropriate category
2. Update related pages with cross-references
3. Update `knowledge/index.md` and `knowledge/log.md`

### 3. Query — User asks about accumulated knowledge

1. Check `knowledge/index.md` (already in your context) for relevant pages
2. Read specific pages with the `read` tool
3. Supplement with `memory_search` if needed

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
- **Index is mandatory**: always update `knowledge/index.md` after any change
- **Be concise**: capture essence, not copy entire sources
- **Full paths in replies**: when referencing knowledge files in conversation replies, use the full path from workspace root (e.g. `[Title](knowledge/<category>/<slug>.md)`), not relative paths. Relative paths are only for cross-references inside knowledge pages themselves.
- **Cite sources**: when answering based on knowledge pages, include links to the relevant pages so the user can explore further.
