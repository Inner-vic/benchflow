# 功能与维护索引

更新时间：2026-10-29。仓库保存可复核源码，实际设备状态、个人认证与每次运行证据由部署环境管理。

## 业务入口与下层工具

当前统一入口是飞书材料/任务卡，完整流程与源码映射见[当前系统](current-system.md)。用户不需要手工挑选以下 Skill。

| 层次/目标 | 当前入口与作用 |
| --- | --- |
| 用户材料、确认和任务状态 | 内部 `gateway/bench_gateway/service.py`、卡片/确认模块；本人材料版本与原卡核验 |
| 准备和硬件执行 | `preparation.py`、`executor.py`、`worker.py`；冻结候选、FIFO、本次小场景→全量 |
| 普通评测 | 独立 postprocess 队列；按任务创建时间选择云端/既有后端，GT 显式关联本次数据源 |
| 到点精度 | 已交付原任务卡“计算到点精度”按钮；独立分析记录，复用原工作站 CSV/XCP |
| 下层 Runner/兼容脚本 | `automation/` 和 [bench-eval-execution](../skills/bench-eval-execution/SKILL.md)，保留 Job/Profile、事务、原 CLI |
| 配套离线评测 v0.4.1 | bench-offline-eval（组织内配套工具），网关按 vendor 固定的相关源码复用，不等于每次重跑原全量内容校验 |
| 配套报告/构建工具 | 双 TXT 报告（组织内配套工具）、PCM 双包（组织内配套工具），当前未自动接入统一主链路 |

网关普通评测交付原始 TXT，不自动创建在线飞书报告。旧 `-ArrivalAccuracy` 是脚本兼容接口，不能作为当前飞书事前开关。网关生产配置、密钥、数据库和运行结果不随快照归档。

## 到点精度的当前入口

飞书当前仅在本人已交付原任务卡显示计算按钮，详见[当前系统](current-system.md)。公开版的[到点说明](arrival-accuracy.md)与[下层脚本契约](../skills/bench-eval-execution/references/到点精度可选后处理.md)保留计算依赖与兼容接口；历史输入追查和内部18场景证据不随公开源码分发。

## 公开源码验证

Python 3.12 安装 `automation/requirements-test.txt` 后可运行 README 中的公开 Runner/适配测试。前轮公开副本 291 项 Python 测试和 7 条 PowerShell 模拟路径通过；这不覆盖未公开的统一网关，也不证明真实云端/逐帧或按钮端到端验收。

完整网关代码、固定 vendor 和隔离测试在内部仓库维护，公开版保留职责和接口说明。真实计算核心、数据、身份配置、平台凭据和部署状态不公开。当前版本的验收范围见[当前系统](current-system.md)。
