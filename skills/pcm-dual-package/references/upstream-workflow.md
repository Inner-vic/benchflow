# 高鹏原 Skill 的复用边界

来源：[jenkins-package-build-pcm](https://ai-market.chehejia.com/?page=skills&skill=qjz8e3xjavkhlevwymgh)，高鹏，v1.0.0。2026-10-27 已核对其可见正文与 defaults。原 Skill 是现有单包 Job 的操作流程，没有部署新流水线的步骤。

有原 Skill 时沿用它的当时内容，并以用户已给出的范围为准。没有安装时，以下是本包保留的操作规则；它是来源明确的流程复用，不假称工具已安装。

1. 固定完整 Job `TRANSIT/XCU/SCM/CCU-ACORE-RELEASE-BUILD`。加载已登录浏览器，核实完整名称，不能只凭末级 Job 名相同就提交。
2. 调用已授权预填未提交表单，按参数名填写，保留未知项。缺字段、选项不匹配或地址不符时停止提交，先查维护配置，不修改 Job。
3. 实际填写后展示最终值，以及上传和通知效果。用户编辑后重读表单。最终状态获准才点击一次 Build；已有对此状态的明确授权无需重复确认。
4. 根据本次跳转或 queue 获取真实构建编号，核对时间、发起人和实际参数，不使用 lastBuild 代替本次身份。提交状态不明时先查证，不自动重复点击。
5. 监控 SUCCESS / FAILURE / ABORTED，核实实际上传。成功返回 APP、集成锁、构建链接；失败保留精简错误和已上传状态，不自动重试。
6. 原通知继续由 Job 按 SILENT_BUILD、NOTIFY_CHAT_ID、NOTIFY_USER 处理；不自行另发私信或群消息。

本 Skill 按已确认的交付范围复用以上流程：单 APP 只提交所选基础模式的一组参数；只有明确要求双 APP 时才分别提交 OFF、ON。原 defaults 中的 SW0043、旧 manifest、SDK 0.1.86 等历史版本不适用于所有分支，当前值从本次分支和匹配成功构建解析。

每次真实 APP 构建分别保存原通知、标准锁和构建链接。仅发布两份 M100 配方不会增加 APP 构建次数；一份普通版 APP 加 ON/OFF 两份 SO 只提交一次 OFF 基础 APP。独立 SO 的真实编译收据与 APP 构建收据分别保存。
