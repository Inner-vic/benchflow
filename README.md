# BenchFlow · 台架回放与评测

[实习项目总览](https://github.com/Inner-vic/internship-projects) · [使用与验证](docs/project-guide.md)

把软件材料、双版本构建、共享台架和评测交付组织为可追溯的任务。

## 解决的问题

一次台架实验会跨越软件包、传感器库、配置、数据、共享设备和云端评测。材料齐全、脚本退出和结果有效分别代表不同状态；后续通知失败，也不能让昂贵的回放再跑一次。

这个项目把这些人工约定落成材料确认、任务状态、资源队列和执行收据，让一次实验的输入、占用、恢复和交付都有可追溯的依据。

## 实现了什么

| 能力 | 实现结果 |
| --- | --- |
| 双版本构建 | 解析分支与精确组件引用，经确认生成 OFF/ON 两个 APP 及各自结果卡 |
| 台架材料准备 | 组织 APP、SO、可选 YAML 和数据源，生成本任务的冻结输入 |
| 共享硬件执行 | 统一 FIFO、Runner 锁、预检、正式回放与服务恢复 |
| 评测和交付 | 收集预测、关联本次 GT、取得原始 TXT，回填原材料卡 |

构建与台架是两个独立阶段。当前由用户手动选择并转发一张构建结果卡，再完成台架材料确认；这段衔接尚未自动化。

## 实现架构

![台架回放与评测的主要模块、执行顺序和关键边界](https://raw.githubusercontent.com/Inner-vic/internship-projects/main/assets/architecture/benchflow.svg)

Windows 端处理消息接入和受限解析，Linux 工作站保存业务状态与材料；Runner 负责受控台架事务，云端完成相应数据读取和评测。

## 关键工程设计

### 1. 把确认绑定到材料版本

确认绑定用户、原卡、任务和当前材料版本。换软件或改配置会使旧确认失效，入队后输入被冻结，避免“用户确认的是一份，设备实际执行的是另一份”。构建确认与台架确认也各自保留。

### 2. 让资源锁覆盖真正的硬件副作用

材料下载、编译和后处理有各自任务状态；共享台架经统一 FIFO 与 Runner JSON 锁协调。执行器记录基线、激活、回放和恢复结果，恢复尚未核验时不把设备当作空闲资源交给下一项任务。

### 3. 按失败边界恢复

准备、台架执行、评测和通知分别保留检查点与收据。结果未知时先核对原执行；已完成回放后，评测或消息失败只从相应阶段继续。这让恢复范围和副作用范围保持一致。

## 实现与验证范围

源码包含 Windows 接入、Linux 网关、Runner 参考和云端适配。源项目保留了双版本构建的真实验收记录；台架、云端和不同软件版本的验证范围分别记录，不能据一个阶段成功推断整条链路通过。

详细依据见[构建实现与验证](docs/16-双版本构建确认与派发.md)、[调度与恢复设计](docs/07-调度锁与异常处理.md)和[公开验证说明](https://github.com/Inner-vic/benchflow/blob/main/PUBLICATION.md)。

## 源码导读

| 源码 | 关注的实现 |
| --- | --- |
| [workstation/gateway/bench_gateway/confirmations.py](workstation/gateway/bench_gateway/confirmations.py) | 材料确认与幂等 |
| [workstation/gateway/bench_gateway/pcm_build_engine.py](workstation/gateway/bench_gateway/pcm_build_engine.py) | 构建检查点与外部调用 |
| [workstation/runner-reference/automation/runner_state.py](workstation/runner-reference/automation/runner_state.py) | Runner 状态与资源锁 |
| [docs/01-执行流程.md](docs/01-执行流程.md) | 完整主链路与恢复边界 |

## 项目资料

使用步骤、依赖和本地验证见[使用与验证](docs/project-guide.md)；源码来源与验证范围见[发布说明](https://github.com/Inner-vic/benchflow/blob/main/PUBLICATION.md)。本页用于介绍实习阶段的实现与设计，具体结论以所链接的代码和证据为准。

[RoadModel 评测](https://github.com/Inner-vic/roadmodel-evaluation)保留独立业务边界，涉及同一物理台架时需共享资源协调。
