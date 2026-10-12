# 回灌协议与环境适配

本文呈现通用契约，不提供生产设备配置。参考 Job、Profile 与源码接口需要在目标环境中实现并联调。

## Job 与 Profile 分工

Job 固定 Run ID、提交者、输入身份、场景、产品来源与所需目标；Profile 固定设备能力、受控命令集、回放与传输适配。Job 不能携带任意 shell、command 或 argv。

`execution.command_set_id` 必须与 Profile 的命令集键一致。场景清单、输出位置与传输集应属于同一次输入组合。不要复用旧 run 的硬编码身份。

## 回灌事务

`submit → 必要授权 → READY 队列 → worker → precheck → capture_baseline → stop_active → start_candidate → health_candidate → replay → restore → verify_restore`

执行器重新计算任务是否需要台架，所需授权和锁与真实依赖一致。恢复核验失败时保留资源锁与证据，后续任务不得接管不确定状态。

## 新环境与批次准入

软件、库、配置、传感器端点、回放频率或数据内容改变时，用小场景建立新基线。确认事务完成、服务恢复、必需输出和产品身份，再用只读批次规划确认快速入口兼容。

配置端口不等于适配器支持端口。库、端点配置、健康检查、驱动与实际接收器必须一致。

## 交付与故障边界

工作站产品保存文件清单与收据；需要团队交付时生成按场景组织的索引。传输或评测失败从对应阶段处理，不重放成功的台架事务。

具体字段以 `automation/job.schema.json`、`workflow-contract.json` 与源码校验为准。公共示例账号、地址、哈希和数据身份均为演示值。
