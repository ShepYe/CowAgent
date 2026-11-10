# 源码一键本地启动

Status: ready-for-agent
Type: task
Blocked by: 无

用户后续决定：本人单机部署与调试优先使用命令行直接启动源码，安装包保留为已有交付物。沿用原 Electron、React、Python 与独立个人数据目录，不新增业务契约。

## 实施

根目录 `start-personal.cmd` 自动定位仓库，调用 PowerShell 检查 Python 3.11/Node 18+，缺失时准备仓库虚拟环境和依赖。默认直接构建前端并启动 Electron，后端使用仓库 Python 源码；`-Dev` 启用 Vite 热更新，`-SetupOnly` 准备依赖，`-CheckOnly` 只检查，`-DataDir` 指定隔离资料根。普通模式显式加载编译后的本项目界面。

## Comments

2026-11-10：实现与本机离线验证完成，真实账号仍由用户统一认证。使用说明唯一正文为 [本地启动](../../../docs/codex/本地启动.md)。

2026-11-10 审核收口：Standards 与 Spec 双轴分别发现同一项 P2：手写模块探针漏检依赖。已改为按两份 requirements 离线检查发行包、版本和环境标记，并补充缺 pip 时的 ensurepip 引导；42 项定向测试、真实清单检查、隔离无 pip 引导、只读环境检查及静态检查通过。审核发现与复核结论见[源码启动审核](../../../docs/codex/logs/2026-11-10-源码启动审核.md)；[本人配置验证清单](../../../docs/codex/logs/2026-11-10-个人助手首期交付.md#待本人配置和验证)保持未完成。

## 验证与边界

- 从仓库外运行根入口，使用含中文和空格的 `local/源码启动 验证` 数据根，构建 React/TypeScript 并实际启动 Electron；后端日志确认运行仓库 `.venv` 与 `app.py`，health 正常，配置显式为关键词模式。`Ctrl+C` 后本次 Electron/Python 及监听端口均退出。
- 根入口 `-Dev` 实际启动 Vite 和 Electron，观察到 Vite 连接及 React 文件变更的 hot updated 回执，随后恢复探针文件原字节。Python/主进程变更通过重启命令生效。
- 真实开发运行发现跨域失败后，增加仅限显式桌面开发模式、本机客户端及精确 `http://localhost:5173` Origin 的处理器；正常接口认证继续执行。修后界面可加载聊天与知识页，带 Origin 的 health 响应匹配；子代理复核发现还需允许原界面使用的 PUT/DELETE，已补齐并覆盖 GET/POST/PUT/DELETE 的预检和实际认证请求，连同认证失败、范围限制与个人 API 定向测试共 39 项通过。测试输出为 `local/source-launch-tests.txt`，修后日志为 `local/source-hot-console.txt`、`local/source-hot-debug-console.txt`，截图为 `local/source-dev-ui.png`。
- 再起开发实例时 5173 忙端口明确失败，原实例继续运行，见 `local/source-port-collision.txt`。最终停止本次调试进程树后 5173/9876 均无监听。启动与数据根证据汇总于 `local/source-launch-evidence.json`。
- Node 语法、PowerShell AST/UTF-8 BOM、`-CheckOnly` 环境检查与差异空白检查通过。首次全新机器建虚拟环境、全量联网安装及 `-SetupOnly` 未实跑；当前验证复用本机已安装依赖。

本次按 Gate 定向检查启动、开发 Origin 和兼容边界：沿用个人数据根与现有 Electron/Python 生命周期，未变更知识或任务契约；开发跨域只在明确开启的本机 Vite 模式生效，普通运行不开放该开发入口。新增源码入口未重新打入历史安装包。
