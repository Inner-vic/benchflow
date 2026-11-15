# 原 Jenkins 如何选择 OFF / ON

核验对象：`livc-acore/livc_integrate/livc_integrate` 的 `pipeline` 提交 `b77ebd1c2b3882dc06f64e2a67b179f2ec98c30f`。使用本次实际加载版本时重新核对关键行为。

| 原实现 | 观察到的行为 |
| --- | --- |
| CivuIntegrate 参数校验 | 将 APP_VERSION、LOCKED_LIBS 交给 check_parameter.py |
| check_parameter.py:check_package | 按包名在 APP_VERSION 优先选组件，其次从基线锁选择；写入 package.lst |
| gen_conanfile.py | 生成 Conan requires；LOCKED_LIBS 标记 override=True；输入中的 #revision 会被去除 |
| detect_livc_build_args.py | 对多数 @livc 组件生成 --build=<name>，包含 M100、X-Lidar、SDK；不应承诺直接复用 SDK 二进制 |
| packaging.py:check_installed_packages | 对比实际 conan.lock 与要求的 name/version/user/channel，选错版本会报错 |
| 原上传和通知 | 分别上传 APP/标准 Conan lock，分别生成原构建卡片 |

所以 OFF/ON 应具有不同的 M100 引用，且每个 recipe 能在原 Job 的构建环境中固定对应 CMake 值。把同一 APP_VERSION 提交两次、只改本地文件、仅在聊天中标注 OFF/ON 都不够。

本包的准备脚本从已核验业务 recipe 生成两份独立版本：源码 branch 固定到同一完整 commit，在 `_cmake.configure()` 前写入 OFF 或 ON。没有增加要求 Jenkins 传入的新 Conan option，因此原参数接口不变。两版版本宏可能不同，这是独立组件的版本元数据，业务源码相同。

业务 recipe 原本把源码克隆到子目录，而 configure/package 从 source_folder 根目录读取。准备脚本仅在本次独立 recipe 中统一源码落盘位置，保证 CMake、配置和模型路径一致。Git 凭据通过临时 askpass 程序读取现有环境变量，不写入 URL、命令或 recipe；不修改原业务分支。

新引用使用 `@livc/pcm-<pair-id>` 通道。现有选择和打包脚本按完整 user/channel 处理，能够保留该通道；它不会覆盖 stable 引用。公共 X-Lidar 同样独立通道，两版完全共用。参数归类须以本次集成 `package.lst` 为准：已经属于集成打包清单的组件，通过 APP_VERSION 指定替换引用；其余传递依赖再使用 LOCKED_LIBS。不要同时在两栏填同名包。

2026-11-12 实际 #3002 及原脚本 `4f7ea5c237df0bdf9ffb0e953e27b544348e29a7` 已证明：将集成清单内的 xlidar 只写入 LOCKED_LIBS，会让 Conan 解析到新库，但 package.lst 仍保留基线旧库，最终在 packaging.py 拒绝打包。已用原 check_package/check_installed_packages 离线复现，并验证移入 APP_VERSION 后接受正确库、仍拒绝错误库。此规则修正此前将 X-Lidar 一律放入 LOCKED_LIBS 的说明；不修改共享流水线或放松版本校验。真实证据留工作站 `research/source-build-dev315-eval-20261112/parameter-correction-validation.json`。修正参数后的构建与失败构建分别保存身份，不自动循环重试。

## 需要分别证明

- 原选择脚本保留两个不同引用，生成的 Conan 消费者实际解析到对应 recipe。
- 该 recipe 的源码 commit 和实际 CMake configure 值分别正确。
- 原任务的版本核验接受实际标准锁中的对应引用。
- Jenkins 身份能从制品库读取 recipe，并能读取 recipe 指向的源码；无需把代码合入 master 才能读取一个已经存在的 commit。
- 两次线上实际非 M100 依赖及包内容一致。版本输入一致不能替代此检查。

本地选择核验不等于完成线上发包。原 Job 的 --update、强制重编、revision 丢弃行为保留；共同版本可通过既有参数固定，精确二进制和运行文件由结果比较确认。若原 Job 的实际结果不能满足共同依赖要求，报告具体差异，不擅自改共享流水线。

实际两次独立重编可能产生不同 PREV，即使版本、RREV、package ID 和 options 相同。2026-11-06 的 #2949/#2950 已成功上传，但严格比较发现五个公共组件 PREV 不同，完整 APP 中另有 libexiv2 的编译时间常量差异，以及构建编号和派生校验文件差异。该结果保留 COMPARISON_FAILED；不能因为认定时间差异无关业务，就自动扩大允许名单、改写 APP 或宣布逐字节一致。需要严格一致时另行解决公共组件的可重复构建或同一二进制复用，并重新验证。
