# Jenkins 发包部署与验证

## 代码范围

当前适配复用原 Job、独立软件状态库与 worker，不修改共享 Jenkins 流水线或台架评测程序。

| 部分 | 实现 |
| --- | --- |
| 当前发包计划 | `pcm_release.py`，使用 `pcm-bot-release/v2`，只生成一套普通 APP 参数 |
| 输入与确认 | `pcm_dual_entry.py` 接收“Jenkins 发包”，展示完整参数；保留本人已确认组件配置、原卡身份和回调校验 |
| 源码核验 | 从精确 M100 配方读取固定源码提交及有效 OFF 配置，与本次分支核对；版本不匹配或动态取码时提示准备匹配版本 |
| 构建派发 | 原 Engine/Jobs 保存提交意图、真实 queue/build 身份和检查点；当前计划使用已发布组件，不创建组件发布分支或启动额外 CI |
| 结果交付 | 原请求卡更新 APP、Lockfile、构建链接和摘要；正式群卡片仍由原 Jenkins 发送 |
| 历史兼容 | 已确认的 v1 任务保持冻结计划和原恢复路径；未确认的旧参数不再创建新任务，重新校验后进入当前流程 |

旧目录名、字段名前缀、数据库及服务名保留，避免破坏既有身份与收据。M100 编译模块、原 `build.sh` 调用器和台架策略未改动。

## 代码验证结果

- Bot 定向隔离回归 81 项通过，包含当前卡片、最终参数、本人确认、源码变化、配方歧义、实际锁文件修订、排队／失败／通知恢复，以及历史任务继续执行。
- 原 M100 回归在基线与候选上分别运行 70 项，结果一致：66 项通过，4 项受当前测试环境限制。一项需要 Linux 符号链接能力、三项依赖未随交接仓库收录的工作站模板；保留原错误记录，未为通过测试修改 M100 业务代码。
- 上述结果对应[Bot 改造修订 900b4b99](https://github.com/Inner-vic/benchflow/commit/900b4b996545869f3ba5cc87bb0a7db395fc0c71)的模拟平台测试。未进行真实 Jenkins 构建、正式通知或台架验收，也未验证普通 APP 加另编 ON M100 的目标链路。

Windows 定向测试使用标准 Python，设置 `PYTHONPATH` 为 `workstation/gateway`，M100 测试另加入 `workstation/gateway/vendor`，临时目录置于源码树外；M100 完整环境回归须使用原 Linux 运行环境和已核验模板。静态检查运行 `python -B tools/check_repository.py`。

## 文档与代码核对

| 核对项 | 代码依据 | 文档结论 |
| --- | --- | --- |
| 普通 APP 的组件及模式 | [pcm_release.py](../workstation/gateway/bench_gateway/pcm_release.py) 的 `binding`、`checked` | 读取原精确 M100 引用，核对源码提交与 OFF 模式；新任务使用一套构建参数 |
| 官方模式的自动编译 | [m100_build.py](../workstation/gateway/bench_gateway/m100_build.py) 的 `build`、[m100_auto_inputs.py](../workstation/gateway/bench_gateway/m100_auto_inputs.py) 的 `prepare` | 现有程序保持 APP 官方模式；普通 OFF APP 不能据目标文档被视为已自动编成 ON |
| 有效 ON/OFF 的核验 | [build_uroad_eval.py](../workstation/gateway/vendor/build_uroad_eval.py) 的 `verify_effective_mode` | 检查实际 CMakeCache；源码默认值与配方覆盖需一并核对 |
| 评测装配的接收 | [candidate_adapter.py](../workstation/gateway/bench_gateway/candidate_adapter.py) 的 `official_replay_without_dump` | 编译能力和运行配置参与材料校验；另编 ON M100 的目标接入仍须专门适配验收 |
| 后台发起身份 | [pcm_build_remote.py](../workstation/gateway/bench_gateway/pcm_build_remote.py) 的 `Remote`、[m100_ci.py](../workstation/gateway/bench_gateway/m100_ci.py) 的 `authorization` | 后台使用工作站配置的 Jenkins 认证，飞书身份隔离不等于自动使用各人的 Jenkins 账号 |

目标流程、当前代码、既有部署记录和真实验收分别说明。历史构建成功只支持其原有验证范围；本次文档核对未刷新生产运行状态，未改变程序或配置。

## 部署要求

当前仓库代码尚未部署。生产更新按既有运维流程执行：

1. 核对现场源码与他人修改、软件队列和原 Jenkins 在途任务；在软件任务排空后安排切换。
2. 备份本功能程序、配置与一致性数据库，核对当前修改清单；保持原状态库、确认、任务和外部构建身份。
3. 将本轮程序作为同一修订安装：`pcm_release.py`、`pcm_build_plan.py`、`pcm_dual_build.py`、`pcm_dual_entry.py`、`pcm_build_engine.py`、`pcm_build_cards.py`。所用私有来源解析脚本须与仓库已核验版本一致。
4. 仅按现场托管方式切换独立软件 worker。核验实际入口进程加载新代码；共享台架、Runner 和其他业务服务保持原部署。
5. 先进行真实入口与只读校验检查。使用者核对最终参数后，才按有效授权进行正式构建验收，检查实际构建次数、产物、Lockfile、正式通知及恢复行为。

回退只恢复本轮程序与配置，不回滚业务数据库、清理队列或取消其他人的构建。已保存的 v2 任务需保留本轮兼容代码处理，不能交给仅支持 v1 的旧 worker；回退前应核对其状态与外部收据。

## 使用入口

[Jenkins 发包](17-Jenkins发包.md)说明输入与交付；[M100.so 评测编译流程](18-M100编译流程.md)说明后续目标步骤、原脚本复用及接入差距。代码合并、生产部署及真实发包验收分别记录。
