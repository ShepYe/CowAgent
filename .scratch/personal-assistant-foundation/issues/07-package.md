# P-02 / T-07 独立数据根、完整备份与 Windows 交付

Status: ready-for-agent
Type: task
Blocked by: 无（P-01、T-06 离线安装联调已完成；账号验收由用户集中处理）

规格：[第 8 节 PA-01/PA-11](../spec.md#8-兼容失败与桌面交付pa-01pa-11)。执行安排：[计划第 5 节](../../../docs/codex/plans/2026-11-10-个人助手改造方案-讨论稿.md#5-第一批从哪里开始)。本票代码、备份测试、最终 Windows 封包和安装验证完成；实际账号与历史跨版本验收边界见[交付记录](../../../docs/codex/logs/2026-11-10-个人助手首期交付.md)。

## 实现与 P-02 结论

- 应用 CowAgent Personal，appId `com.cowagent.personal`；NSIS 保留个人数据，禁用官方更新检查、下载、安装；构建配置不发布更新源。
- 源码与安装模式默认数据根 `~/.cow-personal`，工作空间 `<数据根>/workspace`，Electron userData `<数据根>/electron`；`COW_PERSONAL_DATA_DIR` 可指定独立测试根。首启排他创建 config.json：`personal_assistant_enabled=true`、`embedding_enabled=false`、独立工作空间。后端显式传入 `COW_PERSONAL=1`、`COW_DATA_DIR`、`COW_HOME`。不扫描或迁移官方目录。
- 已启用个人实例时，`user_root(local-owner)` 折回 `state_root`，与桌面默认身份共享记忆与工作空间；其他身份、普通实例保持原行为。
- `desktop/flavors/personal/app-config.json` 为版本管理源；predev/predev:main 和封包脚本应用 flavor。desktop/.gitignore 只放行该配置，resources 仍为生成物。
- 个人实例备份格式 v3，旧 v1/v2 仍可读取。备份全部工作空间（包括 tmp 附件），及数据根中 personal/tasks.sqlite3、originals、接收/处理/会话关联、配置等；排除代码缓存和 SQLite WAL/SHM，数据库通过 SQLite backup API 合并已提交 WAL。逐文件 SHA-256、尺寸与完整清单进入 manifest。
- 恢复前检查清单、哈希、CRC、路径、SQLite integrity/user_version、JSON 与表内/record 内 schema_version；未知版本拒绝写入。发布失败撤回已复制文件；恢复数据库前移走旧 WAL/SHM，失败恢复。恢复保留无关文件。
- 恢复到现有个人根前创建全量 `cow-recovery-<UTC>.zip`，`recovery-point.json` 记录时间、快照和恢复来源。新留档仍留存，回升可恢复快照。恢复须先停止后端；仅 SQLite 单库快照支持在线读取，跨文件业务一致点应在停机下制作。
- PyInstaller 收集动态 common 模块（包括 personal_worker）及 lark_oapi；requirements-desktop 加 lark-oapi，安装初次启用飞书无需下载 SDK。现有 PDF/DOCX/XLSX 依赖和数据收集继续复用。

## 验证（2026-11-10）

Gate 5/7/8：复用已可执行规格与计划，核对本票 PA-01/PA-11 → P-02/T-07 → V-11/V-12 映射；构建与安装实测由主代理执行，不以静态通过替代。

- `.venv/Scripts/python.exe -m pytest tests/test_personal_backup.py tests/test_cli_backup.py tests/test_backup_executable_skills.py tests/test_state_dir.py -q`：61 passed，14 skipped（Windows 不适用 POSIX 权限测试）。覆盖真实 TaskStore/历史/原件往返、WAL 未 checkpoint 提交、tmp 附件、损坏/缺文件/未知 schema 拦截、恢复失败回滚、最新恢复点、个人 owner 路径共享、旧备份兼容。修正旧测试 HOME 在 Windows 需同时指定 USERPROFILE 的隔离设置。
- Python 修改文件 `ruff check --select F`：通过。
- `node desktop/node_modules/typescript/bin/tsc -p desktop/tsconfig.main.json --noEmit`：通过。
- PowerShell 构建脚本语法解析：通过。
- V-11：上述离线恢复子项通过；运行中的整套接收/知识发布与备份关联验收待主代理集成验证。
- V-12：PyInstaller、NSIS、真实空目录安装、升级/降版/回升、安装版三格式/飞书/模型端点验收未运行；按主代理要求本票不封包。

## 主代理封包步骤

在仓库根：

```powershell
.venv/Scripts/python.exe -m pip install -r desktop/build/requirements-desktop.txt pyinstaller
powershell -NoProfile -ExecutionPolicy Bypass -File desktop/scripts/build-personal.ps1
```

脚本先 PyInstaller（`desktop/build/dist/cowagent-backend`），再 Electron 编译和 NSIS，`--publish never`；任一步失败即停止。`-BackendOnly` 可仅试编后端。预期安装包 `D:/CodeAndDesign/CowAgent/desktop/release/CowAgent Personal-Setup-2.2.2-x64.exe`，当前尚未生成或验证。签名、模型凭据与飞书本人绑定由主代理按现场配置完成。主代理统一补 V-12 报告及月度日志；本票不提交、不推送。

## Comments

主代理 P-01 测试根使用 `D:/CodeAndDesign/CowAgent/local/p01-desktop`；启动前设置 `COW_PERSONAL_DATA_DIR` 后从 desktop 执行 `npm run dev`。配置代理已注册 `personal_assistant_enabled`、`embedding_enabled`；初始化同时保留 `personal_instance` 供原始文件辨识，备份实际也识别已注册的 personal_assistant_enabled。


### 跨根恢复与实例进程隔离补验

恢复前在已校验 staging 中仅重定位已知数据库路径字段：TaskStore tasks/events 的 attachments.path、source_candidates.workspace、attachment_decisions.workspace/source.original/result_ref、result_refs/source_existing_refs 的路径；SourceService revisions/receipts.record.original 和 publications.path。manifest 补充 data_root_source，映射复用每个 Agent 的 workspace_source；只接受归档根下的路径，不替换原文、摘要、定位、哈希或幂等指纹。无映射的绝对来源路径停止恢复。

新增真实 TaskStore+SourceService 联合测试，覆盖嵌套/独立工作空间、旧/新用途检查点：备份后隐藏旧根，再在新根实际打开附件、检索、回查出处、重新解析并续发候选；正文中旧路径文本原样保留。个人完整备份仅在后端停止后运行，CLI 与完整归档/恢复 API 检查存活 PID 拒绝在线操作；桌面在个人根记录 .personal-backend.pid。SQLite backup API 只保证单库快照，不承诺跨库在线一致性；PID 属于运行锁，不进入归档。

PythonBackend 个人实例跳过忙端口，freePort 另加个人模式禁止入口；stopSync 个人模式只对自己 tracked PID /T，禁止按 cowagent-backend.exe 全局杀进程。纯替身探针未给任何实际进程发信号。

验证：备份/路径/兼容/状态目录回归 **66 passed、14 skipped**；源码三格式增强自检通过（local/selftest-relocated-source/result.json）；Python F 类与 TypeScript 主进程检查通过。最终增量冻结与增强冻结自检由本票继续执行，NSIS 与安装验收仍由主代理负责。


最终交接：端口/进程替身探针通过（忙端口 9876 跳到 19876，仅请求模拟 tracked PID /T，没有向真实进程发信号）。恢复修复后的回归重跑仍为 66 passed、14 skipped，TypeScript、Python F 类与 diff 空白检查通过。源码增强三格式自检在新根恢复后实际验证附件与出处通过。此前冻结自检只验证旧版恢复路径，不能作为本次跨根修复的冻结证据；主代理将统一最终冻结/NSIS/安装。为避免并行覆盖 dist，本票最后试编在 Analysis 阶段停止，未替换 18:52:50 的已有 exe。

版本确认：cli/VERSION 与 desktop/package.json 均为 2.2.2；build-personal.ps1 已补构建前从 package.json 同步 cli/VERSION（UTF-8 无 BOM），并在 PyInstaller 后强制核对 _internal/cli/VERSION，不更改安装器版本。此脚本补丁仅做语法检查，最终构建由主代理执行。

### 主代理最终交付（2026-11-10 19:45，Asia/Shanghai）

最终构建完整通过 PyInstaller → React/TypeScript → Electron/NSIS，未发布、未配置代码签名。安装包 `D:/CodeAndDesign/CowAgent/desktop/release/CowAgent Personal-Setup-2.2.2-x64.exe`，200,390,810 字节，SHA-256 `b4da37ef28737abba4f959e130f39a9cae47fdd2e598bcab120e20bef47306fa`。

真实 NSIS 安装及覆盖安装、启动、前后端产物哈希一致、最终冻结和安装目录后端的三格式/完整跨根恢复自检均通过。安装版实际主 Agent 三格式检索六项断言通过，新知识页上传/出处/原件/用途修订/重新发布/撤回通过；停止进程树后重启保留 7 条任务及全部原件哈希，检索再次通过。完整证据和 V-01～V-13 的运行边界集中在交付记录；真实模型/绘图与飞书认证、历史版本降版/回升未运行，等待用户统一处理认证。

实际 CLI 备份/跨根恢复补验发现恢复进程自身持有 run.log 导致 Windows 替换失败；已在恢复事务中仅暂停命中目标的本进程日志句柄，成功或回滚后按追加模式恢复。68 passed、14 skipped；真实测试 profile 跨根恢复保留 7 条任务、14 会话、56 消息、14 运行记录及原件哈希与撤回状态。修复后重新封包，最终路径和哈希以交付记录为准。
