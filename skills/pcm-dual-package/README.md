# M100 构建与可选 Jenkins 发包

当前功能分为两个独立阶段：双版本构建交付OFF/ON APP和两张结果卡；用户手动选择并转发其中一张卡片，才作为既有台架评测的软件输入。没有自动关联台架任务、自动迁移材料或发包后自动执行台架；后续整合不属于本次实现。下文关于软件来源追溯及不同装配的规则，不代表已经部署了跨阶段自动衔接。


入口是 [SKILL.md](SKILL.md)。名称 pcm-dual-package 为兼容旧调用保留，当前逻辑不强制双 APP：先固定源码、配套依赖和 M100 编译模式，再确定仅 SO、一份 APP 加配套 SO，或明确双 APP。

一份 APP 的 ON/OFF 两套装配若分别上台架运行，是两个独立执行任务；两份 SO 本身不产生台架任务。编译宏、运行 YAML、软件交付形式、指标评测目标分别管理。

另有明确的[PCM 双版本构建](references/pcm-dual-feature.md)独立入口：交付两份APP，卡片收分支链接、本次软件版本、构建类型和本人组件版本。“校验构建配置”不编译或发包；原台架材料卡不改。

| 工具 | 用途 |
|---|---|
| scripts/resolve_pcm_input.py | 解析仓库分支输入，不联网 |
| scripts/source_build_discovery.py | 输入仓库、分支；用现有认证只读解析 Commit、成功基线、依赖与集成清单，不提交 CI |
| scripts/pcm_dual_config.py | 独立双APP功能的Linux只读校验CLI与候选卡输出，不提交构建 |
| scripts/prepare_pcm_components.py | 准备独立配方，不编译、不发布 |
| scripts/pcm_workflow.py | 新工作流范围、编译证据及 APP 参数计划；默认不猜交付方式 |
| scripts/pcm_existing_job.py | 明确 app_pair 时的旧双包跟踪和严格比较 |

[源码输入与构建确认](references/source-input-review.md)说明自动解析和冻结参数；[工作流字段](references/workflow-state.md)供代理填写，用户无需手填内部 JSON。计划、编译、组件发布、Jenkins 提交和台架执行是不同状态。只读发现复用已配置身份，不处理交互登录；确认函数不执行网络提交。生产Bot已接入独立校验、最终参数确认、软件worker和双结果卡，11月13日SW00F1已真实发包通过。CLI和普通软件准备工具的计划/确认函数本身仍不执行网络提交；两种入口不能混淆。详见[当前实现与部署](../../docs/16-双版本构建确认与派发.md)。

从仓库根目录执行标准库测试：

```sh
python -B -m unittest discover -s skills/pcm-dual-package/tests -p "test_*.py"
```

[旧 synthetic context](examples/context.synthetic.json)只用于旧双 APP 工具演示，不能提交真实构建。[历史实发](references/verified-run-20261106.md)保留其比较未通过结论；[受限发布示例](examples/ci-20261106/README.md)只绑定当时的组件与独立分支，不是本次通用部署脚本。
