# Windows连接与解析端

CC Connect持有唯一飞书长连接；desktop_frontend.py管理工作站租约，desktop_gateway_agent.py经SSH发送消息。独立受限Codex解析进程不保存业务库。补丁二进制不在Git，迁移需按固定摘要取得并核验。两个后台任务均需配置。

先读[当前文档入口](../docs/README.md)及[部署基线](../docs/06-部署基线.md)。
