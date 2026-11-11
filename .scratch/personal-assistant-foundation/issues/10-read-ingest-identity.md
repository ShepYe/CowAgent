# 已读取附件的收录身份与分页修复

Status: ready-for-agent
Type: task
Blocked by: 无

## 范围与执行安排

用户 2026-11-11 提交的日志表明飞书收发正常，knowledge-wiki 已加载、原件已保存且 read 解析成功；随后 knowledge_ingest 报 idempotency_conflict，再次调用报 version_conflict。沿用规格 PA-04/PA-05/PA-08 的资料回执与发布契约及主计划 T-03/T-04，修复 read 与 ingest 复用同一接收回执的实现，不更换资料关联、不放宽更正撤回屏障。

- `SourceService.receive`：隐式分配 ID 后允许回传同一个 ID；显式关联后省略 ID 仍复用回执。字节、文件名、来源或关联不同仍拒绝，保留原回执和旧 fingerprint，无 schema 迁移。
- `knowledge_ingest`：明确已发布版本前置条件与内容 revision_id 的区别；只对 state=0、waiting、active 且未发布的新资料兼容旧 read 的内容版本回传。任何已发生状态变化的资料仍执行原版本检查。
- `read`：个人原件 PDF 使用实际 pages 范围并保留原文位置、片段续读；返回当前已发布版本字段。
- `app.py`：启动渠道列表仅记录实例 ID/类型，避免 dataclass repr 打印凭据。
- 回归：真实工具串联 read→ingest，PDF/DOCX/XLSX/TXT、失败任务重开后重试、同一回执不改绑、分页续读、既有撤回和版本检查；所有数据与凭据使用隔离替身，不调用模型或 Embedding。

## Comments

主线最小用例实际复现 `idempotency_conflict: 请求标识已用于不同资料`，记录 `local/read-ingest-before.txt`。本机任务只读核对：原件哈希完整、任务 failed/decide、错误 idempotency_conflict、零发布引用；来源状态为 state=0/waiting/active，未发布。未重试真实任务、未修改运行实例或配置，更新后由本人从桌面任务页重试。

## 修复与验证

根因是回执 fingerprint 把首次未指定 document_id 和读取后回传同一已分配 ID 视为不同请求。已做同身份兼容，旧回执不改写；新字段明确已发布版本前置条件，旧内容版本回传只在初始状态兼容，撤回、更正和后续状态变化仍严格拒绝。失败任务保持失败，不自动再次调用模型；本人更新源码并重启后，在“知识 → 资料任务”找到原任务点击“重试”，复用原件。

PDF 筛选保留物理页号与解析 ordinal，空白尾页使用原件真实页数，offset/limit 在选定区间内续读；响应含元数据预留预算，避免 50000 字符硬截断吞掉 next_offset。完整解析与发布内容未按展示分页截断，解析格式和持久 schema 不变。

定向回归覆盖三格式、TXT、旧参数回传及新字段、失败任务重开后重试、身份冲突、版本屏障、PDF 区间与长文无损续读，以及已有任务/配置/飞书/Web 用例，结果与命令输出见 `local/knowledge-intake-regressions.txt`。Ruff 新测试全规则、受改模块 F 规则通过；app.py 仅排除原有星号导入产生的 F403/F405。未运行真实账号重试、模型调用或本人文件重新收录，关键词配置保持原状，未打包、推送或发布。

联合批次 138 passed；收尾增加 PDF 总页数/当前区间/后续页区间提示后，对受影响的收录、分页与来源执行 69 passed（`local/knowledge-intake-final.txt`），批次有重叠，不累加。复核同 ID/不同 ID、来源字段、内容字节、旧回执及撤回屏障，无新增发现；真实资料恢复验收保留为待办。此次仅修复既有规格行为，不增加新业务动作。
