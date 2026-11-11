# 空知识库仍可查看资料任务

Status: ready-for-agent
Type: task
Blocked by: 无

## 问题与范围

用户截图确认“知识”页面仅显示空库引导，看不到“资料任务”。`KnowledgePage` 的 isEmpty 提前返回绕开了包含任务标签的公共导航，导致第一份资料收录失败时无法从界面重试。沿用个人助手计划 T-06 与任务查询/恢复契约，仅修复桌面显示分支，不修改任务状态、重试 API 或知识发布规则。

## 实施与验证

复用公共页头与标签，空库仅影响内容区域；资料任务优先于空库内容渲染。通过 agent-browser 在隔离页面加载真实 KnowledgePage 和 PersonalTasksPanel、使用虚构空库与失败任务，修改前复现标签缺失，修改后实际点击标签、显示失败任务和重试按钮、点击重试仅调用一次替身 API，并可切回空库文档页，浏览器无异常。

截图 `local/knowledge-empty-tasks.png`；替身页面与服务位于忽略的 local 目录，未访问真实任务或配置，未触发模型/飞书请求。`npm run build`、`npx tsc --noEmit` 和差异空白检查通过，构建输出 `local/knowledge-empty-build.txt`；真实实例由本人重启后查看并决定重试。
