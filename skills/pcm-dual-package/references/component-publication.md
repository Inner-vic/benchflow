# 使用现有组件 CI 发布专用 recipe

公司[Artifactory 仓库、账号与权限管理规范](https://example.feishu.cn/docx/DEMO00000000000000000000001)要求个人账号只读，上传使用 CI/CD 中的服务账号。下载权限已经可用时，不重复要求用户申请下载权限；个人账号上传被拒绝也不应引导申请个人 Deploy。

先读组件项目当前 `.gitlab-ci.yml` 及其引用模板，确认实际发布任务、目的库和现有服务账号。凭据只在原 CI 中使用，不读取变量明文、不复制密钥到工作站、不改变变量保护或共享模板。本人源码页面打不开、Jenkins 源码凭据失效和制品库发布被拒绝是不同问题，按实际执行身份和错误定位。

需要新 recipe 时，在用户授权的同一组件项目中创建独立 `codex/` 维护分支，固定业务源码 commit。仅该分支的 CI 配置可调整为本次精确引用发布任务，不改 dev-314、其他分支、共享模板或 Jenkins Job。用 GitLab CI lint 核对实际展开任务；不能只看到继承了上传模板，就运行其中可能包含的编译和通配符上传。

发布任务应核验清单、recipe 内容 SHA-256 和固定源码；预检所有目的引用，不存在才新增，同 revision、同内容则只读跳过，已有不同内容或 revision 则停止。使用独立 Conan 缓存，只 export 和 upload 清单中的 recipe，不加 `--all`、通配符或覆盖参数。保存不含凭据的 receipt，包含 CI 链接、引用、实际 RREV、SHA-256 和新增/跳过状态。然后从制品库独立回读，确认与计划一致，才设置 `availability: remote_recipe`。

在固定 SHA 页面编辑出现 Fork 提示，不能直接推断整个项目没有写权限；可先只读查询项目角色及目标分支 `can_push`。不得通过个人 Fork 获取项目服务账号。

## 已验证的 dev-314 发布记录

当时的受限发布脚本、CI 配置及精确 recipe 字节保留在 [历史 CI 示例](../examples/ci-20261106/README.md)。该示例未被本仓库 CI 引用；其他分支不能直接复用其中固定的项目、分支和组件身份。完整构建与比较结果见 [实发记录](verified-run-20261106.md)。

这是一次具体执行证据，不是其他输入分支的默认版本。新任务仍须解析自己的源码、依赖和唯一通道，不覆盖这次引用。

- 原项目 `livc-acore/chassis/uroad`，独立分支 `codex/pcm-dual-publish-20261106`。
- [分支提交 bc2bb558](https://gitlab.chehejia.com/livc-acore/chassis/uroad/-/commit/bc2bb558b091668382b749e6b81dfa5268c629bf)，仅分支 CI 和 internal_pcm_pair 文件。
- [发布 CI 1493277](https://gitlab.chehejia.com/livc-acore/chassis/uroad/-/pipelines/1493277)、[任务 4621722](https://gitlab.chehejia.com/livc-acore/chassis/uroad/-/jobs/4621722)成功，使用项目已有 `xcu_sd-rt-svc`，目的库 `liconan-dev-local`。
- 精确发布 `uroad_m100/3.1.4-pcm-off.dev314-20261027-001`、`uroad_m100/3.1.4-pcm-on.dev314-20261027-001`、`xlidar/0.2.8-pcm.dev314-20261027-001`，均为 `@livc/pcm-dev314-20261027-001`。
- 已独立回读确认三个 recipe。发布 recipe 成功不等于 Jenkins APP 编译、上传或双包比较成功，须继续跟踪原两次构建。
