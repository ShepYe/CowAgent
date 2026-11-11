# 配置不可用失败的可观察性与重新提交

Status: ready-for-agent
Type: task
Blocked by: 无

## 依据与范围

用户点击重试无反应，实际只读确认原任务已 attempt=3、failed/download、config_unavailable，在模型前被配置修订检查拒绝。沿用规格 PA-03 的“旧配置不可重建时停止并说明重新提交方式”，当前修订校验保持严格，不静默修改旧任务。

## 执行安排与定向契约

- `POST /api/personal/tasks/{id}` 新增 `action=resubmit`，携带旧 attempt 和 request_id；仅允许 config_unavailable 的失败普通附件任务，无发布结果/候选，不接续来源状态动作。
- 后端核验归属、attempt、原件哈希和状态，保留原任务及处理历史，从可信记录复制原件、来源和会话，附 parent_task_id，新任务捕获当前 config_revision；客户端不能指定路径、身份、配置、候选或旧用途决定。同 request_id 返回同一新任务；保留 task schema v1。
- 桌面展示中文失败原因，“按当前配置重新处理”明确授权新提交并复用原件，成功反馈与旧失败重试区分，重复请求沿用稳定 request_id。
- worker 记录不含消息正文/凭据的任务执行失败日志。测试覆盖真实 TaskStore/API、模型前失败、原件与历史保留、重复/并发、错误状态及旧 attempt、坏原件、当前配置取自后端；浏览器替身只验证按钮链路，不操作本人任务。

## Comments

隔离接口测试先复现缺少入口（local/personal-resubmit-before.txt）。本次为 PA-03 的定向契约补齐，沿用原配置拒绝行为及显式新任务语义，Gate 5/7/8 映射至 T-01/T-05/T-06；旧读写器忽略新增来源字段和动作，旧任务/原件不迁移。本人真实重新处理继续由本人操作。

## 修复与验证结果

接口按事务核验并登记包含全部已保存附件的新任务，保留 download_complete 避免重下载，配置只从后端捕获。客户端提交标识按父 task/attempt 隔离，同请求及并发重复仅登记一个子任务；不复制旧候选、用途决定、结果或回答。worker 失败新增结构化任务 ID/attempt/stage/code 日志，界面显示中文原因和重新处理后的新任务反馈。

联合回归 115 passed（local/personal-resubmit-regressions.txt），补齐执行器→真实收录工具的替身联调用例后，新专项 10 passed；测试批次交叠不累加。前端 build 与 tsc 通过（local/personal-resubmit-build.txt），Python F 检查、新测试全规则和差异空白检查通过。隔离浏览器实际点击“按当前配置重新处理”，验证一次请求、成功提示、新旧任务并列且旧错误保持，截图 local/config-resubmit-ui.png；浏览器无异常。

本人任务只读取状态，仍保留原始失败记录，未调用真实模型、修改配置或自动重新提交；由本人重启后决定是否使用当前配置处理。没有放宽 config_revision 的比较规则（包括当前将 Debug 纳入比较的现状），不推送、不打包、不发布。
