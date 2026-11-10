# T-06 原 React 知识界面与个人资料 API
Status: ready-for-agent
Type: task
Plan: T-06
Blocked by: 无（T-03/T-04/T-05 离线闭环及整包集成已完成；真实账号验收待用户）

规格：[PA-01/02/04/08/09/10/11](../spec.md)。执行安排：[计划第 5 节](../../../docs/codex/plans/2026-11-10-个人助手改造方案-讨论稿.md#5-第一批从哪里开始)。

## 实施

- 保留原知识文档/图谱页，增加“资料任务”页签。接收 PDF/DOCX/XLSX/MD/TXT，展示阶段、错误、结果、来源版本及主 Agent 回复；等待输入时补用途，可重试失败显示重试，非终态显示取消，原件按附件 ID 打开，来源可独立撤回、历史出处可查看。
- API 全部沿用 `_require_auth`，固定 OWNER，不接受请求覆盖身份。本人绑定额外要求 `_desktop_token_matches`，仅保存 `personal_feishu_binding` 四个标识。设置位于原设置/集成页。
- 上传先验证格式/大小与字节摘要，再 submit → attach → enqueue。请求 ID 在重试时复用，同 ID 内容变化拒绝；原件通过 TaskStore.attachment 校验并附 UTF-8 filename 下载头。任务与来源冲突返回最新任务。
- 撤回适配 SourceService.change 的 expected_state，必须是任务关联来源；历史引用只允许读取任务 result_refs 关联 publish_id，不接受原件路径。
- 普通聊天附件接收由主代理在 `channel/web/core/channel.py` 接入，保留原 SSE；本任务不修改 chatStore。现有 ChannelsPage 图标声明改为真实 LucideIcon/纸飞机组件联合类型，修复完整类型检查错误，无运行行为变化。

## 验证

2026-11-10：`.venv/Scripts/python.exe -m pytest tests/test_personal_web.py tests/test_knowledge_web.py -q`：23 passed。新增接口测试含归档、重复/冲突、attempt、固定身份、补用途/取消/重试、原件限制和中文下载头、桌面绑定令牌、来源撤回、旧引用回读及所有任务端点认证先于存储。

`node desktop/node_modules/typescript/bin/tsc --noEmit -p desktop/tsconfig.json`：通过。`npm run build:renderer`：通过，存在原有静态字体路径/包体积提示。

## Comments

- Gate 5/7/8：复用已可执行规格与计划；T06 权限、来源有效状态、attempt 冲突与旧接口兼容映射已核查。无新增业务取舍。
- 未运行：真实本人飞书认证、桌面逐按钮交互、三格式真实模型出处闭环、安装包启动、重启/备份恢复的跨模块验收；由主代理与独立桌面验证代理继续。接口单测使用临时 TaskStore/SourceService，enqueue 替身，无外部模型或 Embedding 请求。
- 避免争抢 P01 桌面测试代理，未控制 CDP 19222。未提交、未推送。

### 2026-11-10 T06 补齐更正、用途与多位置出处

- PersonalTask POST 增加 `correct`、`change_purpose` 和显式 `republish`。前两项要求非空 reason，核验 task attempt、任务关联 document 和 expected_state；用途仅 long_term/transient，通过 SourceService.change 提交。改为长期不自动发布。界面据真实发布登记显示当前可检索/历史发布状态。
- 来源操作区显示资料/版本/用途/有效性，更正依据与用途修订共用必填输入。更正立即停止当前检索；重新长期发布独立建任务，复用已校验原件和新配置修订，request_id 保持幂等。
- **worker 接点已完成（主代理实现并确认）**：新任务 `payload.source_action = {document_id, revision_id, expected_state, action: republish, purpose: long_term, reason}`；origin.parent_task_id 指向父任务。`common.personal_worker.execute_source_action` 受信处理该指令，核验父任务 result_refs 的 document/revision，固定 directive，通过 scope 与 commit_guard 直接调用 KnowledgeSourceAction，不依赖模型改变期望状态。SourceAction 已支持实际重新发布与 checkpoint 恢复；主代理已补真实 SourceService + TaskStore 的 worker 恢复测试。T06 未修改核心执行器。
- Reference GET 从已授权 publication 的 parse 清单返回 units（ordinal/location）、unit_count，任意 ordinal 回读。界面支持上一处/下一处/数字跳转和当前位置所在 50 单位目录，显示实际页/段落/单元格与当前有效状态；保留历史版本出处，变更后刷新有效状态。
- 验证更新：个人 API + 旧知识回归 **37 passed**；完整 TypeScript 检查及最新 React 构建通过。新增测试覆盖更正/用途即时屏蔽、状态/身份/依据校验、改回长期不自动发布、显式任务去重与原件关联、第 3 单位历史回查、越界/非法位置。本代理未控制真实桌面；worker 实际重新发布及恢复验证由主代理补充，结果不混入上述 37 项。

### 最终冻结与交接

T06 UI/API 代码已最终冻结，无未完成编辑或运行中测试。路由导入已按项目模式补 `noqa: F401`，Ruff F401 与 diff 检查通过。主代理报告综合测试增量 112 项通过、Ruff 通过；该综合结果与本任务 37 项验证分别记录。worker 接点已由主代理完成，T06 接口交接项关闭；封包与真实安装版 GUI 验收由主代理维护。


### 主任务收口

本票实现与离线集成已完成，最终安装、新 UI、Agent 工具、重启及恢复结果和真实账号待办统一见[首期交付记录](../../../docs/codex/logs/2026-11-10-个人助手首期交付.md)；以上代理阶段的未运行项按该记录更新，不重复维护验收正文。
