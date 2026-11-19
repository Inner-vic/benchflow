# BenchFlow · 台架回放与评测

[实习项目总览](https://github.com/Inner-vic/internship-projects) · [使用与验证](docs/project-guide.md)

把 Jenkins 普通 APP 发包、材料确认、共享台架和评测交付组织为可追溯的任务。

## 解决的问题

软件发版后、进入实车测试前，需要在台架回放已录制场景，检查软件启动、数据处理和预测结果，并与真值比较。一次验证会跨越软件包、传感器库、配置、数据、共享设备和云端评测，原本需要工程师逐项准备并人工传递材料。

这个项目把这些人工约定落成材料确认、任务状态、资源队列和执行收据，让一次实验的输入、占用、恢复和交付都有可追溯的依据。

## 实现了什么

| 能力 | 实现结果 |
| --- | --- |
| Jenkins 发包 | 核对分支、APP 和依赖锁，交付普通 APP 与构建记录；当前适配代码待部署验收 |
| 台架材料准备 | 组织 APP、SO、可选 YAML 和数据源，生成本任务的冻结输入 |
| 共享硬件执行 | 统一 FIFO、Runner 锁、预检、正式回放与服务恢复 |
| 评测和交付 | 收集预测、关联本次 GT、取得原始 TXT，回填原材料卡 |

Jenkins 发包、评测 M100 准备和台架执行分别确认。新发包计划仅交付普通 APP；普通 APP 后独立准备 ON M100 与装配接收仍待适配。构建卡转发不会自动创建台架任务。

## 实现架构

![台架回放与评测的主要模块、执行顺序和关键边界](https://raw.githubusercontent.com/Inner-vic/internship-projects/main/assets/architecture/benchflow.svg)

| 节点 | 职责 |
| --- | --- |
| 飞书 | 接收材料、本人确认、进度与结果；大体积场景数据直接在执行节点间流转 |
| Windows | CC Connect 消息接入，按需调用 Codex 解析自然语言要求 |
| Linux 工作站 | 持久保存材料和任务，组织准备、排队、Runner 与结果回收 |
| 台架 | 运行待测软件，接收历史传感器输入并生成预测 |
| 云端平台 | 提供回放数据与真值，按任务接收预测并执行评测 |

自然语言要求先转成结构化任务，材料和生效配置经确认后交给固定工具执行。配置、预测目录与评测报告分别构成阶段产物，关联同一任务；当前发包与装配边界以上文说明为准。

## 关键工程设计

### 1. 把确认绑定到材料版本

确认绑定用户、原卡、任务和当前材料版本。换软件或改配置会使旧确认失效，入队后输入被冻结，避免“用户确认的是一份，设备实际执行的是另一份”。构建确认与台架确认也各自保留。

### 2. 让资源锁覆盖真正的硬件副作用

材料下载、编译和后处理有各自任务状态；共享台架经统一 FIFO 与 Runner JSON 锁协调。执行器记录基线、激活、回放和恢复结果，恢复尚未核验时不把设备当作空闲资源交给下一项任务。

### 3. 按失败边界恢复

准备、台架执行、评测和通知分别保留检查点与收据。结果未知时先核对原执行；已完成回放后，评测或消息失败只从相应阶段继续。评测作为独立后处理，台架完成恢复后可服务下一项任务，避免让云端等待持续占用硬件。

## 实现与验证范围

源码包含 Windows 接入、Linux 网关、Runner 参考和云端适配。当前 Jenkins 发包已完成代码适配和隔离回归，待部署验收；评测专用 ON M100 与普通 APP 的装配衔接尚未贯通。历史双包验收保留为各自版本的事实，不作为当前默认流程已上线的证明。

入口与边界见[Jenkins 发包](docs/17-Jenkins发包.md)、[M100 编译流程](docs/18-M100编译流程.md)及[代码适配与部署要求](docs/19-Jenkins发包部署与验证.md)。离线检查、真实构建、台架回放和模型指标验收分别判断。

## 源码导读

| 源码 | 关注的实现 |
| --- | --- |
| [workstation/gateway/bench_gateway/confirmations.py](workstation/gateway/bench_gateway/confirmations.py) | 材料确认与幂等 |
| [workstation/gateway/bench_gateway/pcm_release.py](workstation/gateway/bench_gateway/pcm_release.py) | 普通 APP 发布计划与版本核验 |
| [workstation/runner-reference/automation/runner_state.py](workstation/runner-reference/automation/runner_state.py) | Runner 状态与资源锁 |
| [docs/01-执行流程.md](docs/01-执行流程.md) | 完整主链路与恢复边界 |

## 项目资料

使用步骤、依赖和本地验证见[使用与验证](docs/project-guide.md)；源码来源与验证范围见[发布说明](https://github.com/Inner-vic/benchflow/blob/main/PUBLICATION.md)。本页用于介绍实习阶段的实现与设计，具体结论以所链接的代码和证据为准。

[RoadModel 评测](https://github.com/Inner-vic/roadmodel-evaluation)保留独立业务边界，涉及同一物理台架时需共享资源协调。
