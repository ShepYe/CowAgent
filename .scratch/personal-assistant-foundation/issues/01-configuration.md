# P-04 / T-01 配置一致性与显式关键词模式

Status: ready-for-agent
Type: task

本票实现、离线集成与安装包验收已完成；真实端点认证由用户集中处理，最终证据见[首期交付记录](../../../docs/codex/logs/2026-11-10-个人助手首期交付.md)。

## 依据与边界

- 行为唯一正文：[规格第 7 节](../spec.md#7-provider-与配置切换)。执行安排：[计划第 5 节](../../../docs/codex/plans/2026-11-10-个人助手改造方案-讨论稿.md#5-第一批从哪里开始)。
- 影响 PA-03、PA-13，验证 V-01 的配置接口、V-09、V-10 的停用部分。Gate 5/7/8 受影响项核对通过：保持旧实例缺省语义，旧修订不可取时明确失败；不做 T-08 向量世代迁移。
- 不修改 desktop、common/state_dir、agent/knowledge。按协作追加 common/config_revision.py 兼容导出、channel/channel.py 与 chat_channel.py 个人实例接入。
- 主代理继续负责桌面任务 claim/finalize、飞书及持久恢复集成。chat_channel.py 已交回主代理，不继续并行编辑。

## 已交付接口

```python
from models.config_revision import capture_config_revision, require_config_revision
revision = capture_config_revision()  # 接收提交前，task 仅保存此字符串
require_config_revision(revision)    # 成功返回 None，否则 ConfigUnavailable.code

from bridge.bridge import bridge_for_config_revision
with bridge_for_config_revision(revision) as bridge:
    reply = bridge.fetch_agent_reply(query, context)
```

common.config_revision 同样导出 capture/require/use_config_revision 和 ConfigUnavailable。

- 修订为 `cfg1-<SHA-256>` 不透明标识，覆盖实际配置（含凭据变化）；任务不存配置正文或密钥。没有历史凭据仓库；旧修订已不匹配当前配置时统一 `config_unavailable`，排队、重启和重试均不得自动切到新连接。无配置变化时重启可重算相同修订。
- `use_config_revision(revision)` 校验并复制配置到 ContextVar；作用域中的同步调用固定旧配置，线程须自行进入作用域。capture 内部取得共享保存锁，调用方不要预先持有该非重入锁。
- `bridge_for_config_revision` 同时创建 `Bridge.new_instance()` 并安装作用域 bridge；`current_bridge()` 供 Channel 的会话、ASR、TTS 使用。仅使用 conf 快照后继续调用 Bridge 单例不能提供缓存隔离。
- 个人实例 produce 绑定修订，执行时进入 fresh bridge 和 task_scope；身份为 local-owner。未开启个人助手且无修订的旧消息流程保持原义。
- available_setting 新增 personal_assistant_enabled=False、personal_feishu_binding={}；受认证设置 API 可读写。个人模式以 COW_PERSONAL=1 或 personal_assistant_enabled 为准，打包端保持一致。
- embedding_enabled 缺省 True 延续旧实例行为，显式 False 在工厂任何 legacy/explicit 分支前直接返回 None；定制包由打包代理写 False。设置 API 及 models set_capability(embedding) 支持布尔开关；选择厂商不会覆盖已显式关闭的开关。

## 实现与探针

核查原实现发现：空 Embedding 厂商回退旧凭据；自定义厂商删除会自动改路由；多个 models 保存入口先改内存后写盘；设置 API 只在切模型时刷新 Bridge。

现改为副本计算、验证、原子文件保存后发布内存，保存失败保留旧配置；保存地址/密钥也刷新客户端，凭据环境镜像在成功后更新。引用厂商删除返回 provider_in_use 与 references，非法 custom 绑定拒绝保存。掩码保留原密钥，配置回执掩码；自定义厂商显式空密钥仍可清空。

替身探针只用 `.invalid` 地址及假密钥，拦截 Embedding 客户端构造；没有真实服务请求。最初回归暴露三个旧自动删除路由的断言，按 PA-03 更新为拒绝引用删除；两处新增测试 Context 构造参数已修正。

## 验证结果

- 配置、并发锁、自定义厂商、绘图、语音、Embedding 工厂、文件保存故障、模型发现、目录 API 与单例初始化定向回归：**180 passed, 1 skipped**。
- 最后补齐凭据环境镜像刷新后，相关配置定向回归：**80 passed**。
- tests/test_personal_configuration.py：**18 passed**，覆盖地址/密钥/模型单独切换，旧排队/恢复/重试拒绝，进行中副本不变，fresh Bridge 缓存隔离、Channel 实际路由、关键词无客户端构造、旧缺省兼容、失败保存、引用删除及个人身份。
- Ruff F 类检查：本票配置、bridge、Channel 与相关测试通过；chat_channel 后续由主代理编辑，最终合并检查由主代理运行。
- 现有 ffmpeg 缺失和 audioop 弃用警告未影响这些配置测试。

## 缺口与集成验收

- 尚未运行认证真实厂商、真实桌面 GUI、飞书实收、真实进程强杀重启及安装包配置切换；不能以替身结果声称这些场景已验收。
- 队列/重启边界目前为持久标识与配置替身验证；真实 TaskStore、worker、桌面队列的 claim/finalize 与错误回执由主代理接续完成。
- 配置修订按完整有效配置保守比较，无关设置变化也可能使尚未开始的旧任务不可用；不会静默换连接。
- ContextVar 不自动跨新线程，进程级技能环境变量也不是 attempt 私有环境。未来启用多能力并发/独立技能子进程时，须传入作用域配置构造的环境和客户端，不能重新读取全局旧缓存。
- T-08 指纹、世代迁移、同维向量兼容验证未实施；保持显式关键词模式。

## Comments

2026-11-10：用户授权配置工作，不提交；真实认证端点留最后。主代理已采用 fresh bridge 包裹 worker，并接手 chat_channel 任务提交与结束回执。P-01 的真实联调、V-12 安装包验证由总任务继续。
