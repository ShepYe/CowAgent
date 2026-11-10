# T-02 可信身份与持久接收
Status: ready-for-agent
Type: task
Plan: T-02 / P-01
Blocked by: 无（真实认证联调按用户最新指示末尾集中验证）

规格：[PA-02/04/10](../spec.md)。执行细节：[计划第 5 节](../../../docs/codex/plans/2026-11-10-个人助手改造方案-讨论稿.md#5-第一批从哪里开始)。

## 实施与验证

主代理负责 `common/personal_tasks.py`、`channel/feishu/personal.py` 与飞书入口。SQLite WAL 事务先登记再处理，固定受信 owner、实例/app/tenant/open_id 校验早于下载。原件按哈希不可变归档；任务与通知分别保存，状态冲突拒绝。

## Comments

2026-11-10：离线持久接收、并发去重、冲突、磁盘失败、陌生身份及未知schema等 10 项测试通过（含T05部分）。真实本人飞书验证待用户统一认证。

### 主任务收口

本票实现与离线集成已完成，最终安装、新 UI、Agent 工具、重启及恢复结果和真实账号待办统一见[首期交付记录](../../../docs/codex/logs/2026-11-10-个人助手首期交付.md)；以上代理阶段的未运行项按该记录更新，不重复维护验收正文。
