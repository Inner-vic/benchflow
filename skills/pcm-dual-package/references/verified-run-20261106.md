# 2026-11-06 实际构建记录

此文件记录历史结果，不授权再次 Build，也不作为新分支的默认依赖配置。

| 项目 | 已核验结果 |
| --- | --- |
| 业务源码 | uroad/dev-314，提交 `9a61fe17167b97d7b13c5b0c02348762e84ed206` |
| 公共依赖 | SDK `0.1.89-rc2`；X-Lidar 源码提交 `9ae4cb4d3efd1dd334b5e04203f1087b36867b04`，两版共用 WITH_REPLAY=ON |
| 专用组件通道 | `@livc/pcm-dev314-20261027-001`，OFF／ON 分别固定 WITH_EVAL_DUMP 值 |
| 组件发布 | [CI 1493277](https://gitlab.chehejia.com/livc-acore/chassis/uroad/-/pipelines/1493277)成功，服务账号 `xcu_sd-rt-svc`，三份 recipe 已独立回读 |
| 普通版 OFF | [Jenkins #2949](https://ci.ep.chehejia.com/job/TRANSIT/job/XCU/job/SCM/job/CCU-ACORE-RELEASE-BUILD/2949/)成功 |
| 评测版 ON | [Jenkins #2950](https://ci.ep.chehejia.com/job/TRANSIT/job/XCU/job/SCM/job/CCU-ACORE-RELEASE-BUILD/2950/)成功 |
| 通知 | 原工程效率机器人分别发出两张正式卡片；默认发布群中已核验，并 @ 实际构建发起人 |
| 整对验收 | `COMPARISON_FAILED`，没有改成 COMPLETE；未上机运行验证 |

两个 APP 均为 `.app.tar.gz` 普包。外部标准锁与包内嵌锁一致，APP 和外部锁下载 SHA-256 均与制品库相符：

| 产物 | 下载 | SHA-256 |
| --- | --- | --- |
| OFF APP | [APP](https://artifactory.ep.chehejia.com/artifactory/xcu-acore-test-local/Acore/Civu/Release/app/CCU_LIVC_AVM-release-SW0075_ULTRA_ID0111.20261106-2949.app.tar.gz) | `bac6e5f88b6025ae302c354c4130afc76d501889c1af749e4541ca45fcb6fc9a` |
| OFF 锁 | [Lockfile](https://artifactory.ep.chehejia.com/artifactory/xcu-acore-test-local/Acore/Civu/Release/CCU_LIVC_AVM-release-SW0075_ULTRA_ID0111.20261106-2949.lock) | `cbf38ad2dedf5c6f1ff4e9d6e775265d71b957695bbf75c4edfd04c75e9c9030` |
| ON APP | [APP](https://artifactory.ep.chehejia.com/artifactory/xcu-acore-test-local/Acore/Civu/Release/app/CCU_LIVC_AVM-release-SW0075_ULTRA_ID0111.20261106-2950.app.tar.gz) | `ca4ab4e3b2b91349c1a75b0d7e5348e24dfc210c810745fb21ef0d3bbb36e9e7` |
| ON 锁 | [Lockfile](https://artifactory.ep.chehejia.com/artifactory/xcu-acore-test-local/Acore/Civu/Release/CCU_LIVC_AVM-release-SW0075_ULTRA_ID0111.20261106-2950.lock) | `bb4b2a1721f9203102fc3b2e8bf97dcd5ff8f216484aabce04bda60ca0243934` |

两次原 Job 参数只有 APP_VERSION 中的 M100 引用不同。非 M100 的版本、RREV、package ID、options、依赖边及 profiles 一致；五个公共组件 PREV 不同：data_center_interface、exiv2_wrapper、infer_proxy、mdc_avm、nvs_processor。

实际 APP 除 M100、内嵌锁、etc/VERSION、x01.md5 外，还存在 `libexiv2.so.0.27.1` 差异。该库大小均为 3,085,848 字节，仅四个字节不同，位于 Exiv2::dumpLibraryInfo 中的编译时间常量：OFF 01:46:19，ON 01:37:10；对应[上游源码的 __TIME__](https://github.com/Exiv2/exiv2/blob/v0.27.1/src/version.cpp#L450)。其他归档内容及权限／链接一致，包括实际 SDK、X-Lidar 运行文件。

原 Job 会独立重编公共组件并先上传，再进行双包比较，因此相同版本参数不能保证逐字节一致。本模块继续拒绝这些差异，不通过修改允许名单或改写已发布 APP 来消除失败状态。
