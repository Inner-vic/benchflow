# 原 Jenkins 卡片发给谁

来源：共享库 pipeline 提交 b77ebd1c2b3882dc06f64e2a67b179f2ec98c30f 中的 CivuIntegrate.groovy 和 feishu_notify.py。实际运行前读取当时维护值，不能把此记录覆盖到共享 Job。

- 默认群：**XCU_P A核APP发布群**，来自原 Job 的 FEISHU_GROUP。
- 正常人工触发：按 Jenkins 的 BUILD_USER_ID 查询 `<账号>@lixiang.com` 对应飞书用户，在默认群卡片中 @ 该用户。它不是单独私信。
- 以本次真实 Jenkins 发起账号为准；只有原脚本成功解析该邮箱时才会 @ 发起人，解析失败时不能声称已 @ 成功。2026-11-06 的实际双包由 developer 发起，两张卡片均已在默认群查到并 @ 卓昊哲。
- SILENT_BUILD=false 时执行原卡片通知；true 时只输出日志，不发卡。
- 正常发起人成功解析后，原脚本会忽略 NOTIFY_CHAT_ID / NOTIFY_USER。
- 只有未解析到真实发起人且两个代理通知字段都有效时，才额外向指定 chat_id 发卡；默认群仍然会收到。该参数不是替换默认群的开关。

保持两个代理通知字段为空，沿用默认群；通知次数取决于实际 APP 构建次数。单 APP 只由原 Job 通知一次，独立 ON/OFF SO 不额外产生 APP 发包通知。只有明确双 APP 时才分别通知；实际接收结果以各次构建的通知日志为准。

需要变更接收群时，先核实原流程已有的路由能力，不能仅填两个字段就承诺会替换默认群，也不为此修改共享流水线。
