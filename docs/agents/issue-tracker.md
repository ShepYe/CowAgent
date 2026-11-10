# 任务跟踪：本地 Markdown

本仓库的任务与规格作为 Markdown 文件保存在 `.scratch/`，使用 UTF-8 编码并随仓库版本管理。

## 工单约定

- 每个功能使用独立目录：`.scratch/<feature-slug>/`。
- 规格保存在 `.scratch/<feature-slug>/spec.md`。
- 每个实施工单独立成文：`.scratch/<feature-slug>/issues/<NN>-<slug>.md`，从 `01` 开始编号。
- 分诊状态记录在工单顶部的 `Status:` 行，使用 [分诊标签映射](triage-labels.md) 中的五个默认角色名。
- 评论与讨论追加到工单底部的 `## Comments` 下。

## 技能约定

- “发布到任务跟踪器”：在 `.scratch/<feature-slug>/` 下写入对应规格或独立工单，按需创建目录。
- “获取相关工单”：读取用户引用路径处的文件；仅有编号时，在相关功能目录中解析。

## Wayfinder 操作

- Map：`.scratch/<effort>/map.md`，记录 Notes、Decisions-so-far 和 Fog。
- 子任务：`.scratch/<effort>/issues/<NN>-<slug>.md`，从 `01` 开始编号；`Type:` 记录 `research`、`prototype`、`grilling` 或 `task`。
- 执行状态：Wayfinder 子任务在领取、解决阶段使用 `Status: claimed`、`Status: resolved`。进入执行前按上述分诊词汇记录状态。
- 阻塞关系：顶部的 `Blocked by: NN, NN` 列出依赖；所有依赖均为 `resolved` 后解除阻塞。
- Frontier：按编号选取首个未解决、未阻塞且未领取的子任务。
- 领取：开始处理前设置 `Status: claimed` 并保存。
- 解决：在 `## Answer` 下追加结论，设置 `Status: resolved`，并在 Map 的 Decisions-so-far 中追加结论摘要与链接。
