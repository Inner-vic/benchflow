# 软件准备工作流 v2

当前功能分为两个独立阶段：双版本构建交付OFF/ON APP和两张结果卡；用户手动选择并转发其中一张卡片，才作为既有台架评测的软件输入。没有自动关联台架任务、自动迁移材料或发包后自动执行台架；后续整合不属于本次实现。下文关于软件来源追溯及不同装配的规则，不代表已经部署了跨阶段自动衔接。


这是独立软件计划，不是网关 Job，也不是生产确认回调。代理填内部 JSON，用户只提供业务输入和需要的交付方式。

2026-11-12：仓库与分支独立输入，按[源码解析与确认](source-input-review.md)生成本次公共 context。新的 discovery 绑定源码文件、基线锁与集成清单 SHA；集成清单中的依赖若被放入 LOCKED_LIBS，计划器在提交前拒绝。

## 上下文

- `source`：uroad 仓库、分支、准确 40 位 Commit。
- `compile_modes`：明确请求的 `OFF` / `ON` 列表，不能为空或重复；台架 goal 不改变它。
- `delivery`：`components_only`、`single_app_with_variants`、`app_pair`，不从“两份 SO”推导“两次 APP 发包”。
- `publish_modes`：可选的显式配方发布范围；默认只为实际 APP 所需变体核对远端配方。两份配方均需发布时填写 OFF、ON，但不因此增加 APP 次数或台架任务数。
- `components.<模式>`：精确 M100 ref、source_commit、cmake `{WITH_EVAL_DUMP:模式}`、recipe_sha256。供 APP 使用时还须真实远端 availability=remote_recipe、recipe_revision；本地配方不伪造这些字段。
- `compile_evidence.<模式>`：真实 BUILT 收据的 source_commit、component、effective_eval_dump、machine=AArch64、output_sha256、output_bytes，以及实际编译副本 conanfile.py 的 recipe_sha256。原收据缺该字段时，保留原件，另用真实副本摘要补充来源，不能随意填写或改写旧收据。
- 单 APP 的 `base_app_mode` 与 `base_app_mode_evidence`：mode、source_commit、origin=verified_source_default 或 explicit_build_variant。不能由是否计算指标反推。
- APP 模式保留原 common_parameters/common_app_refs/common_locked_refs/baseline_lock_sha256。BASE_LINE 支持真实四位十六进制版本，例如 008D；SOFTWARE_BRANCH 保持四位软件 ID。
- `baseline_artifact_checks`：verified、liware_present、limesh_present，必须来自基线真实 JFrog 属性，不是表单空值。公共专用通道组件记录 common_recipe_evidence 的真实 availability、revision 和 SHA256。

计划输出 `requested_app_modes` 和 `app_count`，只对这些模式生成 APP 参数。只有 READY_FOR_APP_REVIEW 才提供非空 jenkins_modes；`require_app_review(plan,mode,sha)` 校验完整计划摘要并拒绝其他模式。该函数只返回参数，不提交网络请求。

`build_review(plan, owner, session, revision)` 可在精确参数已确定但 M100 仍待编译/发布时生成确认展示，明确列出准备状态；不能在基线属性未知或公共依赖未核对时确认。真实回调调用 `confirm_build_review`，分支当前 Commit、owner、session、材料 revision 和展示摘要均需相同。确认后才准备软件，实际 Jenkins 提交仍调用 `ready_parameters_after_confirmation` 核对输入未变及现有 readiness 门禁。纯函数不发起任何构建，也不代替生产确认消费事务。

仅 SO 无需 Jenkins 参数或配方发布即可形成 COMPONENTS_COMPILED；一份 APP 加两份 SO，只要求被选入 APP 的配方远端可用，另一份 SO 可以独立交付。需要将两份 recipe 均发布时作为明确发布范围单独记录，不扩大 APP 次数。

## 三种状态不能替代

1. M100 BUILT：实际编译及产物核对，不是台架已运行。
2. remote_recipe：配方已发布并回读，不是目标二进制已发布。
3. APP 构建成功：软件包及锁已上传，不是台架回放或指标已通过。

所有计划输出 bench_task_count=0。编译结束、配方上传和软件包完成，不自动创建或派发台架任务。

## 一份 APP 下的两次台架执行

软件构建记录保存共同 APP 和配套依赖，OFF/ON 分别保存 SO 摘要。需要都运行时，由现有网关创建两个独立执行记录，各自冻结 APP、SO、运行 YAML、数据源、场景、回放方式和执行目标；共享软件构建来源而不共享可变材料快照。

基础 APP 原始打包模式与实际候选装配模式分别记录。基础包内原来是 OFF，不代表显式换装配套 ON SO 后的候选仍是 OFF；也不能把换装后的候选冒称为原官方 ON APP。新来源入口必须保存 base_app_sha256、构建来源、m100_sha256、实际编译模式与本次配置，生成新的装配记录供材料卡确认。这个显式变体装配与“按评测目标暗中改已有 APP 的宏”不同；生产已有 APP 的官方模式保护不能在未接通新来源凭据时直接绕过。

每条执行由本人有效材料卡确认，进入原全局硬件 FIFO，保留 Runner JSON 锁，重新对齐当前台架软件状态；同 APP 不意味着跳过部署核验。结果目录分开，第二次不得覆盖第一次或复用第一次的完成收据。不要把两次运行合并为一次运行中途换库，也不因构建了两份 SO 自动派发两次。

`evaluation_requirements` 仅检查编译能力、运行配置及预测产物，不能代替 GT 匹配、真实输入和硬件准入。OFF 不具备该组 eval 能力；ON+enable=true 也不保证 CSV 或完整预测存在。两种模式均可按明确目标仅回放，不能自动要求两次都做指标评测。

旧 [pair-state](pair-state.md) 只描述明确双 APP 的跟踪格式，不再是新请求的默认路由。
