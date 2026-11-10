# 项目 Agent 约定

## Agent skills

### Issue tracker

任务与规格保存在本仓库 `.scratch/<feature>/`；处理工单前读取 `docs/agents/issue-tracker.md`。

### Triage labels

分诊时按 `docs/agents/triage-labels.md` 使用五个默认角色名，写入本地工单的 `Status:` 字段。

### Domain docs

采用 single-context：根目录 `CONTEXT.md` 与 `docs/adr/`；探索领域前读取 `docs/agents/domain.md`。

### 重大变更文档

起草、修订或审查重大及契约性变更的规格／计划，或准备实现此类变更时，使用 [major-change-docs-gate](.agents/skills/major-change-docs-gate/SKILL.md)；普通改动走原流程。
