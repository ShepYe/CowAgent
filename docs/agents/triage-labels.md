# 分诊标签

本地工单使用以下映射。技能提到标准角色时，将对应状态值写入工单顶部的 `Status:` 行。

| 标准角色 | 本地状态值 | 含义 |
| --- | --- | --- |
| `needs-triage` | `needs-triage` | 等待维护者评估 |
| `needs-info` | `needs-info` | 等待补充信息 |
| `ready-for-agent` | `ready-for-agent` | 规格完整，可交给 Agent 执行 |
| `ready-for-human` | `ready-for-human` | 需要人工实现 |
| `wontfix` | `wontfix` | 不予实施 |
