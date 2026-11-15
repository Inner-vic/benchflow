# 2026-11-06 受限组件 CI 示例

此目录保留当时已经实际成功运行的发布实现和精确 recipe 字节，用于代码审阅、历史追溯及离线边界测试。它没有被 BenchFlow 根目录 CI 引用，也不接入台架评测任务。

- `component-ci.gitlab-ci.yml` 是当时 uroad 独立分支的配置快照，不是本仓库可直接启用的流水线。
- `internal_pcm_pair/publish_recipes_ci.py` 只接受 `livc-acore/chassis/uroad`、`codex/pcm-dual-publish-20261106`、项目 CI 中的服务账号，以及脚本固定的三份 recipe。个人环境或个人 Fork 会在任何联网／上传前停止。
- `internal_pcm_pair/recipes/components.json` 是发布前的输入快照，`LOCAL_RECIPES_ONLY` 表示该输入文件生成时的状态。实际发布成功证据见模块的 [实发记录](../../references/verified-run-20261106.md)，不能把清单中的布尔值当作最新远端状态。
- 三份 `conanfile.py` 的 SHA-256 与当时的核验记录相同。未包含源码仓库本体、编译二进制、认证文件或凭据值。

快照目录的 `.gitattributes` 仅对三份 recipe 保留原始字节与历史空白，避免跨平台换行转换改变已审阅的 SHA-256；不改变其他代码的格式规则。

将来需要新分支发包时，重新解析源码、依赖、唯一版本通道及授权范围，按 [组件 CI 发布规则](../../references/component-publication.md)准备新方案；不要直接重跑这个历史分支或覆盖旧版本。

原上传流程仅 export/upload 精确 recipe、不加 `--all` 或覆盖参数；发现既有不同 revision／内容时停止。所有变更预检通过后才发布，发生中途错误时保留部分完成记录，不宣称原子发布。
