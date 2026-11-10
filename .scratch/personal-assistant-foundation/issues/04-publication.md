# T-04 用途与发布闭环
Status: ready-for-agent
Type: task
Blocked by: 无（03 已完成并集成；真实模型认证待用户）

规格：[PA-07/08/09/13](../spec.md#6-自主判断留档与引用pa-07pa-09pa-13)。执行安排：计划第 5 节 T-04，验证 V-05/06/07/13。

## 实施与接口

`agent.knowledge.personal_assistant.SourceService(workspace_root, owner_id="local-owner")` 提供 receive/parse/get/change/prepare/publish/search/read_reference/current_publication。receive 只供已经授权附件路径的可信入口调用。来源工具 knowledge_ingest 使用 TaskStore.attachment 核验附件，禁止任意路径，主 Agent 自主判断 long_term/transient/waiting。工具实际注册并向主 Agent 注入工作空间配置。knowledge_source_action 固定当前可信 owner，用已登记 document_id、expected_state、reason 完成更正/撤回/用途改变。

原子发布：解析前捕获状态修订，候选持久保存；发布时单一 SQLite 事务复核 current revision、state、purpose、validity，写关键词单位后逐条回读校验，再提交发布指针。新候选失败保留旧有效版本；撤回、更正和用途变化先提交失效屏障。发布文件未登记/哈希不符即不可读。旧 API、read/write/edit、memory_get、memory_search、目录、图谱及 prompt 索引过滤托管页。未托管页沿用旧含义。

多附件支持 attachment_id 子集分别决定 long_term/transient/waiting，attachment_decisions 持久累计决定与候选；未全部明确前 source_candidates 为空，禁止发布。全部明确后在 commit_guard 内发布并 complete，保留每份临时/长期回执。恢复入口为 `agent.tools.knowledge_ingest.resume_publication(task,store)`，验证完整决定与用途/状态后复用候选，不重新解析；旧来源模块恢复函数仅供 legacy。Worker 先置 running，保留接收配置及当前 attempt。

独立关键词索引通过 MemoryManager.search 桥接 memory_search，支持中文重叠二元关键词与英文分词。返回回读的原文、真实位置和 document/revision/parse/publish/ordinal 关联；无 Embedding 调用。原件保存不表示可检索。

临时任务专用 current_task_id ContextVar 阻止摘要输入、日记写入及其后续 Dream；原始消息标记 personal_task_id 与 archive-only，持久化和重载保留，原始全文继续留档。来源 evidence_revision 指纹变化时局部清理来源证据及派生助手上下文；archive-only 每轮排除。跨 fresh Bridge/Agent 使用 workspace/session/agent 模块缓存指纹，普通助手和多模态用户上下文保持，来源指纹同时核验投影哈希。

## 验证与缺口

2026-11-10：`.venv/Scripts/python -X utf8 -m pytest tests/test_personal_sources.py tests/test_knowledge_service.py tests/test_knowledge_web.py tests/test_knowledge_publication_failure.py tests/test_personal_tasks.py -q --disable-warnings -k "not symlink"`：64 passed，2 deselected（Windows symlink 权限）。新测试 9 项通过，覆盖三格式、限额损坏、真实中文问句、哈希篡改、撤回重启、解析期间状态竞态、同资料去重、未知 schema、旧版本保留、多附件退出恢复、真实工具注册与 prompt/skill 加载、临时上下文持久化。未运行真实模型自主行为、用户三格式样本和安装版端到端；这些由主任务后续验收，不能将离线测试当成产品全验收。manager.py 后续 mock 回归由另一代理负责。未提交。


### 最终补充验证

2026-11-10：来源测试扩展为 11 passed（`.venv/Scripts/python -X utf8 -m pytest tests/test_personal_sources.py -q --disable-warnings`）；三格式均额外经过无扩展名哈希原件的 receive→prepare→publish→read_reference，XLSX 使用 BytesIO 修复扩展名检查。新增加同资料同哈希不重复发布、knowledge_source_action 更正/撤回期望修订、陌生 owner 拒绝、取消先提交后 ingest/source_action 均不能变更旧发布。来源工具、来源模块与测试 Ruff F 检查通过。

会话存储与摘要回归另执行 50 passed（conversation_store_page_window/long_sessions、summarizer_daily_hash_retry/atomic_memory_write/flush_retry 与当时来源 10 项）。此前知识/来源/任务回归为 65 passed、2 deselected；排除项为 Windows symlink 权限依赖。未触碰其他代理负责的凭据、Embedding factory、桌面、飞书及备份实现。manager.py 在桥接实现后移交回归代理，后续不再改写。

混合用途已补齐并验证：同任务一份长期、一份临时；waiting 阻止整批发布；故障重试保留临时回执；取消先提交阻止候选发布。身份不明的同名资料作为独立 document 留档，不按文件名替代；主 Agent 仅在有替代意图但关系不明确时用 waiting 澄清。


### 封包前最终冻结

2026-11-10：生产代码已冻结，无未测试补丁。最后回归综合 114 passed、2 failed、2 deselected；失败为知识 Web 测试替身污染（web.application 返回 SimpleNamespace 无 request）。将 publication_failure 独立进程运行 8 passed，其余本次回归独立运行 108 passed、2 deselected。来源与完整留档专项在此前为 24 passed；最后加入跨 Agent 普通上下文保留、临时派生未托管知识写入阻止、批次限额等测试均包含在 108 项通过结果内。Ruff F 与 git diff --check 通过。Windows symlink 权限依赖用例明确排除。

恢复操作：knowledge_source_action 的 republish 或 purpose=long_term 使用资料最新已登记 revision（也可显式指定已登记版本），重新 prepare/publish，仅真实可检索后成功。索引失败返回 pending_index、candidate_id 和状态修订，可单独接续不重新解析；任务检查点可恢复无附件文本恢复请求，撤回/取消屏障继续生效。

临时用途：current_task_id 中通用 write/edit 禁止真实 knowledge_root 下任何派生页，必须使用授权 knowledge_ingest；无来源任务的旧未托管页与同名但不属于实际知识根的项目文件行为不变。自动 cleanup_old_sessions/prune_scheduled_messages 在 personal 模式返回 0，明确手动删除保留。新 ingest 核验最多100附件、累计候选解析正文200MiB，超限为limit_exceeded且不发布。混合用途解析当前持提交锁，取消可能等待解析结束，按提交顺序生效，不承诺解析期间即时取消。

真实模型与安装版最终验收、构建产物和交付说明由主代理维护，本票不重复声明端到端验收通过。未提交、未推送。


### 主任务收口

本票实现与离线集成已完成，最终安装、新 UI、Agent 工具、重启及恢复结果和真实账号待办统一见[首期交付记录](../../../docs/codex/logs/2026-11-10-个人助手首期交付.md)；以上代理阶段的未运行项按该记录更新，不重复维护验收正文。
