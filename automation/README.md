# automation

这里保存 Runner 源码、协议文件、可复用工具和当前任务输入。台架评测的控制流程以 `../skills/bench-eval-execution/SKILL.md` 为准。

`workflow-contract.json`、`control-plane-contract.json` 和 `job.schema.json` 是 Runner 的机器接口定义。当前状态与收据位置见根目录 `CODEX_HANDOFF.md`。

Runner doctor accepts authenticated SSH controllers from any source network by default. VPN interface and return-source differences are advisory warnings and do not prevent READY. Missing return routes, SSH service failures, bench link failures and occupied locks still block execution. Set management.enforce_controller_subnets to true for optional CIDR admission; otherwise allowed_controller_subnets is not required. Updating the shared Runner takes effect without redistributing Skills.
