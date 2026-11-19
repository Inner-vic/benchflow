# 高鹏原 Skill 的复用边界

来源：[jenkins-package-build-pcm](https://ai-market.chehejia.com/?page=skills&skill=qjz8e3xjavkhlevwymgh)，高鹏，v1.0.0。2026-10-27 已核对其可见正文与 defaults。原 Skill 是现有 Jenkins Job 的操作流程，没有部署新流水线的步骤。

有原 Skill 时沿用它的当时内容，并以用户已给出的范围为准。没有安装时，以下是本包保留的操作规则；它是来源明确的流程复用，不假称工具已安装。

1. 固定完整 Job `TRANSIT/XCU/SCM/CCU-ACORE-RELEASE-BUILD`。加载已登录浏览器，核实完整名称，不能只凭末级 Job 名相同就提交。
2. 调用已授权预填未提交表单，按参数名填写，保留未知项。缺字段、选项不匹配或地址不符时停止提交，先查维护配置，不修改 Job。
3. 实际填写后展示最终值，以及上传和通知效果。用户编辑后重读表单。最终状态获准才点击一次 Build；已有对此状态的明确授权无需重复确认。
4. 根据本次跳转或 queue 获取真实构建编号，核对时间、发起人和实际参数，不使用 lastBuild 代替本次身份。提交状态不明时先查证，不自动重复点击。
5. 监控 SUCCESS / FAILURE / ABORTED，核实实际上传。成功返回 APP、集成锁、构建链接；失败保留精简错误和已上传状态，不自动重试。
6. 原通知继续由 Job 按 SILENT_BUILD、NOTIFY_CHAT_ID、NOTIFY_USER 处理；不自行另发私信或群消息。

2026-11-17起，本Skill默认只复用上述 Jenkins 发包流程：一套最终参数、一次Build、一个普通APP和对应Lockfile、构建链接、原正式通知。所填uroad_m100引用用于本次普通APP，不默认派生两份OFF/ON配方或补发评测APP。原defaults中的SW0043、旧manifest、SDK 0.1.86等历史值不适用于所有分支，当前值仍从本次输入和匹配来源核验。

后续需要的依赖替换、配套M100编译及台架材料确认由原材料流程单独处理，不作为本次Jenkins发包前置或自动后置动作。原软件准备指引保留在[历史说明](legacy-software-workflow.md)，当前默认操作见[Jenkins 发包](../../../docs/17-Jenkins发包.md)。原Bot未在本次切换，不使用其确认两版按钮代替一次Build。
