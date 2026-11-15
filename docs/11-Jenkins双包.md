# Jenkins普通版OFF与评测版ON双包

> 早期独立模块说明，保留兼容接口与历史验证。当前 Bot 构建和真实双包交付见[当前实现](16-双版本构建确认与派发.md)；本页“未部署”等描述仅对应当时模块，不代表当前 Bot 状态。

本模块专门生成两个APP普包：普通版OFF和评测版ON。入口为[pcm-dual-package Skill](../skills/pcm-dual-package/SKILL.md)，代码、测试和历史CI示例见[模块说明](../skills/pcm-dual-package/README.md)。模块独立调用，未连接材料卡、任务队列或台架Runner，也不修改共享Jenkins流水线。

## 如何使用

在能访问公司内网并操作已登录浏览器的执行助手中，明确要求它读取仓库内 `skills/pcm-dual-package/SKILL.md`，按该Skill发包。拉取仓库不会自动安装、登录或提交构建。

日常输入可只提供一个分支链接：

```text
按pcm-dual-package Skill，对这个分支生成普通版OFF和评测版ON两个APP普包：
https://gitlab.chehejia.com/livc-acore/chassis/uroad/-/tree/dev-314
```

助手核对代码、依赖和成功基线，准备两份可供原Jenkins选择的组件，再填写两套原参数。当前小工具只做本地解析、准备、状态记录与比较；联网核验和Jenkins页面操作由助手完成，尚非完全自动查询的一键客户端。

## 首次需要的账号与权限

| 平台 | 所需条件 |
|---|---|
| GitLab | 本人能读取目标业务分支及需要核验的依赖源码 |
| 组件项目GitLab CI | 缺少OFF/ON组件时，能创建独立维护分支、提交本次配方并运行对应CI；也可由维护者协助 |
| Jenkins | 本人能查看原Job、提交构建并读取构建结果，无需修改Job配置 |
| Artifactory | 个人账号用于读取；新组件配方使用项目现有CI服务账号发布，不把服务账号密钥复制到个人环境 |
| 飞书 | 由原Jenkins机器人发送正式卡片，无需额外配置个人飞书发消息凭据 |

本人源码访问和Jenkins构建身份的源码访问分别核验。能在浏览器打开源码，不代表构建机凭据一定有效；反之也不能仅因本人源码页打不开就要求补开发包。按实际身份与失败日志定位。

## Jenkins参数如何确定

固定Job为 `TRANSIT/XCU/SCM/CCU-ACORE-RELEASE-BUILD`。参数由助手按源码、匹配成功构建及标准Conan锁核对，不让使用者手填内部JSON。

| 参数 | 填写逻辑 |
|---|---|
| CONTROLLER | 当前适配固定为CCU_M100_AVMLIVC |
| APP_VERSION | 分别选择OFF、ON的M100组件，其他APP组件相同 |
| LOCKED_LIBS | 两版共用的精确依赖版本，由本次匹配结果确定 |
| BASE_LINE、SOFTWARE_BRANCH、MANIFEST_NAME | 从匹配成功构建及其依赖锁确定，不从分支名猜值 |
| BUILDVARIANT | 沿用核验后的发布约定 |
| BUILD_REPLAY | true；用于基线复现，不是OFF/ON开关 |
| UPLOAD_ALL_PACKAGE | false，避免重发其他组件的Conan包 |
| SILENT_BUILD | false，保留原正式通知 |
| 通知、镜像和Portal字段 | 核对原通知配置及基线继承，保证本次只生成APP普包 |

原Job没有直接接收WITH_EVAL_DUMP的参数。两版开关固定在各自组件配方中，Jenkins通过不同M100引用选择；两次填同一组件不能得到OFF/ON双包。

## 换新分支

提供新的uroad分支链接即可开始。助手固定真实源码提交，重新检查SDK、X-Lidar和ON所需接口，匹配成功基线；已有合适组件就复用，没有则在独立codex维护分支准备本次专用版本和通道，通过现有组件CI发布。用户提供的业务分支不被修改，不覆盖旧制品。

当前实际验证来自dev-314，其他分支仍需重新核验。历史组件CI绑定当时项目、分支和精确配方，不是其他分支可以直接执行的通用发布器。无法唯一确定基线或缺少权限时，说明具体缺项再确认，不能承诺任意仓库或分支自动匹配。

## 输出与验证边界

成功构建交付两个实际APP、各自Lockfile、SHA-256及两条构建链接。原Jenkins向「XCU_P A核APP发布群」各发一张正式卡片并@实际构建用户，不另发私信、不合并卡片。两次上传并非原子操作。

[历史实发记录](../skills/pcm-dual-package/references/verified-run-20261106.md)中的OFF #2949与ON #2950均成功，但严格双包比较发现公共组件修订及实际APP编译时间等额外差异，状态保留COMPARISON_FAILED；不能宣称最终两包仅开关不同。当前[48项离线测试](../skills/pcm-dual-package/tests/VALIDATION.md)不代替线上发包或上机验证。

本次2026-11-10迁入交接仓库只增加独立模块和说明，不修改生产、共享流水线或台架任务。

迁入后离线检查：仓库静态检查覆盖462份Python、24份JSON、49份Markdown及105个相对链接，0错误；规则自测7项和双包模块48项通过。模块初次测试因沙箱禁止访问Windows临时目录出现12项权限错误，恢复正常临时目录权限后全48项通过，未跳过用例。本次未登录构建服务、编译、发布组件或触发Jenkins。
