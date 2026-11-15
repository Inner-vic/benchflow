# 两次原构建的内部上下文和状态

本页只适用于明确选择 `app_pair` 的旧双 APP 模式。新请求先读[软件准备工作流 v2](workflow-state.md)，不得仅因编译两份 M100 就运行本页的两次提交循环。单 APP 模式只生成、提交被选中的一组 APP 参数；SO 准备不创建台架任务。

`pcm_existing_job.py` 只生成文件、记录状态与比较下载结果，不联网提交、不上传、不发送消息。实际页面操作沿用高鹏原流程。

## context

由代理从分支、原 Job、成功构建和制品库解析：

- `pair_id`：唯一小写 ID，8–48 字符。
- `source`：`repository` 固定 uroad HTTPS Git 地址，用户 `branch`，完整 `commit`。
- `baseline_lock_sha256`：匹配成功构建的标准 Conan 锁 SHA-256。
- `common_parameters`：原 Job 的 CONTROLLER、BASE_LINE、SOFTWARE_BRANCH、BUILDVARIANT、MANIFEST_NAME、BUILD_REPLAY、UPLOAD_ALL_PACKAGE、SILENT_BUILD，以及原通知/镜像/Portal 等实际字段。不要包含 APP_VERSION、LOCKED_LIBS 或新造的 PCM_* 字段。BUILD_REPLAY=true、UPLOAD_ALL_PACKAGE=false。
- `common_app_refs`：两版共同 APP 组件的精确引用，从基线和实际集成范围解析，排除 M100。
- `common_locked_refs`：共同依赖的精确版本引用，包括选定公共雷达。不得与 common_app_refs 同名；不传范围、*、#revision。
- `common_recipe_evidence`：对新建专用通道公共依赖，以完整引用为键记录实际远端 `availability:"remote_recipe"`、`recipe_revision`、`recipe_sha256`。未核实的引用保留在 pending_common_refs，不能提交。
- `components.OFF/ON`：`ref`、`source_commit`、`cmake: {"WITH_EVAL_DUMP":"OFF或ON"}`、`recipe_sha256`。远端已核实后增加 `availability:"remote_recipe"` 和真实 `recipe_revision`；本地准备时不能加该状态。若复用已有非专用通道组件，还需 `reused_existing:true`。

M100 以不同版本落在本次专用通道，实际 APP_VERSION 除该引用外相同。SDK 版本、实际集成 APP 列表和 common 引用必须来自维护证据，不让用户补填内部 JSON。

## 组件 recipe 进入原流程

缺少远端组件时，按[组件 CI 发布](component-publication.md)在项目 CI 内使用已有服务账号，并设置本次独立 CONAN_USER_HOME。以下命令仅在该 CI 环境逐一处理清单中的精确引用，目的库沿用原 Job 的 liconan-dev-local：

```sh
conan export <组件recipe目录> <本次完整引用>
conan upload <本次完整引用> -r liconan-dev-local --confirm
```

当前 Conan 1 的 upload 不加 --all 时仅上传 recipe，原 Jenkins 会按原规则编译。执行前仍须检查工具版本和帮助；不使用通配符或 --all 上传现存依赖。先展示本次新引用与上传效果并沿用用户已有授权。不要修改普通 stable 通道；没有上传时保留本地草案状态。个人账号只读，不在工作站上借用服务账号密钥上传。

上传后从远端读取实际 recipe，核验 SHA-256、revision、源码 commit 和开关，再更新 context 并重新生成待审阅计划。SDK 等现有组件仍由原 Job 处理，不让用户额外准备源码包。本次新增二进制未上传 Liconan 时，标准锁是构建记录，不承诺所有精确二进制都可直接远端下载。

## 状态操作

```sh
python <skill>/scripts/pcm_existing_job.py plan --context context.json --state pair.json
python <skill>/scripts/pcm_existing_job.py submit-intent --state pair.json --mode OFF --reviewed-sha <plan_sha256>
# 用户已对最终表单授权后，用原 Skill 点击一次 Build，核实本次跳转/queue。
python <skill>/scripts/pcm_existing_job.py record-build --state pair.json --mode OFF --build-url <本次真实URL>
python <skill>/scripts/pcm_existing_job.py record-result --state pair.json --mode OFF --result off-result.json
# ON 同样执行；不能重复使用 OFF 构建编号。
python <skill>/scripts/pcm_existing_job.py compare --state pair.json --off-app <下载文件> --on-app <下载文件> --off-lock <下载锁> --on-lock <下载锁>
python <skill>/scripts/pcm_existing_job.py summary --state pair.json
```

单次 result 包含 `build_url`、终态 `result`；成功时还需 `app_url`、`app_sha256`、`lock_url`、`lock_sha256`。先从实际构建和制品库核验再记录，不能用脚本生成假线上证据。下载文件保留原文件名。

主要状态：`COMPONENT_RECIPES_REQUIRED`、`READY_FOR_REVIEW`、`SUBMISSION_UNCERTAIN`、`RUNNING`、`UPLOADED`、`AWAITING_COMPARISON`、`COMPLETE`、`PARTIAL_OR_FAILED`、`COMPARISON_FAILED`。

原 Job 先上传再由本 Skill 比较。单次构建成功不会直接把整对标记为 COMPLETE，也不能承诺先比较再上传。失败不覆盖旧产物、回滚原卡片或自动发补偿任务。

主分支兼容接口仍支持 plan_from_card.py 的 input_confirmation 摘要绑定，以及 pcm_existing_job.py export-delivery 的 OFF/ON 身份清单导出。仅适用于显式 app_pair；当前 Bot worker 使用独立状态，不据旧离线计划自动提交。
