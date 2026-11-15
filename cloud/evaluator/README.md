# 原生评测器参考

本目录保留上游评测器源码、配置和Linux脚本供追溯。统一入口通过固定云端接收端生成本任务GT/预测路径并执行；不要直接套用etc中的上游默认路径或修改指标。

固定二进制和动态库未进入Git。恢复需要核对profile工具摘要、实际依赖、挂载和签名公钥，步骤见[云端手册](../../skills/bench-gateway-operations/references/05-LPAI挂载与调用.md)。本轮未重编评测器或运行云端评测。

[历史上游说明](../../docs/archive/evaluator-upstream-README.md)保留旧Windows编译参考，其中build.ps1等文件并未交付。[评测器代码说明](evaluator.md)可用于理解源码，不作为当前运行配置。
