# automation

当前业务入口是飞书材料/任务卡，详见[当前执行链路](../docs/current-system.md)。此目录保留被复用的下层 Runner/适配接口；其中到点事前开关仅作旧 CLI 兼容，飞书当前只由本人已交付原任务卡按钮触发。

确定性 Runner、协议、离线测试和可选到点后处理编排。使用入口见[执行 Skill](../skills/bench-eval-execution/SKILL.md)，源码导航见[工具索引](../docs/tooling.md)。

队列隔离校验失败任务，预测校验默认检查 NPY/JSON 内容。NumPy 依赖见 `requirements-test.txt`。

到点后处理默认关闭，依赖已有 Tasks CSV、同场景 XCP 和产品/恢复收据。`arrival_workstation.py`、解析/统计助手与 XLSX 渲染源码公开；`vendor/arrival-core/` 的计算核心和真实来源记录不分发。运行时需审核外部核心，并在独立维护版本中配置实际哈希，不能使用示例哈希冒充验证通过。XLSX 运行另需 openpyxl、SciPy 和 LibreOffice。

原始回放工具和组织内构建、评测及通知依赖仍由环境适配层提供，详见[公开边界](../docs/publication.md)。
