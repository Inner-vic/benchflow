# Jenkins 发包

基于指定的 uroad 分支和组件版本，复用原 Jenkins 流水线构建普通 APP，交付软件包、Lockfile、构建链接和正式通知卡片。

## 使用方式

提供源码分支地址、软件版本、构建类型和组件版本配置。组件配置带出本人上次确认的内容，首次使用空版本模板，支持编辑和追加。核验最终参数并确认后，由原流水线执行编译与打包。

台架所需的依赖替换、M100 准备和执行确认由后续材料流程处理。普通 APP 后编译 ON M100 是[目标流程](../../docs/18-M100编译流程.md)，独立评测变体与装配接收尚待适配。当前 Bot 后台使用工作站配置的 Jenkins 认证，实际发起账号以该配置为准。

## 文档与维护

| 入口 | 内容 |
| --- | --- |
| [功能说明](../../docs/17-Jenkins发包.md) | 输入要求、执行流程、交付结果及台架衔接 |
| [SKILL.md](SKILL.md) | 助手执行规范与参数核验要求 |
| [原流程说明](references/upstream-workflow.md) | 高鹏 Skill 的复用范围与原 Job 操作约定 |
| [历史资料](references/legacy-software-workflow.md) | 此前方案、兼容接口及能力边界 |

`scripts/resolve_pcm_input.py` 与 `scripts/source_build_discovery.py` 提供输入规范化和只读来源解析。其余历史计划、配方和派发脚本保留兼容，调用范围以当前 Skill 为准。

当前已完成 Bot 代码适配与隔离回归，尚未部署验收，详见[实现状态](../../docs/17-Jenkins发包.md#实现状态)。目录及调用名 `pcm-dual-package` 保留兼容，对外名称统一为 **Jenkins 发包**。
