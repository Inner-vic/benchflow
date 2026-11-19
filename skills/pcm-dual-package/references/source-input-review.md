# 源码输入、自动解析与构建确认

> 2026-11-17起，当前操作统一见[Jenkins 发包](../../../docs/17-Jenkins发包.md)，按原流程构建普通APP。本页保留此前实现和验证记录，相关派发接口不作为当前默认执行器；本次未调整或部署线上Bot。

用户提供源码仓库、源码分支；基线构建编号可选。允许填写完整分支链接，但链接与显式分支冲突时拒绝。输入记忆仅属于本人草稿；没有分支不能从其他用户或上一任务兜底。仓库改变后不能保留原分支的隐式含义。

## 只读解析

工作站使用现有加密认证，仅对 GitLab、原 Jenkins Job、JFrog 发起限定 GET。个人电脑不保存解析结果或凭据；失败不自动改认证，也不启动 CI。

内部 request 形状：

```json
{"repository":"uroad","branch":"填写本次分支"}
```

可选 `baseline_build` 指定成功构建编号；`product_variant` 指定已明确的产品配置；`base_app_mode` 指定明确选择的构建变体。没有明确选择时，模式从当前源码声明读取，不能从回放/评测目标推导。输入不含固定产品版本、SDK 或消息库版本。

```sh
python <skill>/scripts/source_build_discovery.py \
  --request <工作站/request.json> --output <新的工作站/discovery.json> \
  --gateway-root <现有网关目录> --owner <已登记的请求用户>
```

顺序与边界：

1. 查询本次分支当前 Commit；只读取该 Commit 的 conanfile.py、CMakeLists.txt，不执行 recipe。保存文件摘要。
2. 解析 SDK、消息库、相机和雷达声明。精确引用直接比较；支持数字稳定版本的 AND 比较范围，从真实 Lockfile 固定具体版本。动态表达式、OR/caret/tilde、不能可靠判断的预发布范围等交给真实 Conan 解析证据，不近似推算。
3. 有界读取最近成功构建；用实际 Lockfile SHA、host 依赖与源码声明匹配。产品配置有歧义、搜索边界不足或制品多义时显示候选，要求明确选择，不静默套用历史基线。指定基线也必须通过相同匹配。
4. 依据锁定的 livc_integrate 精确配方修订读取 package.lst，支持直接导出文件或只读 conan_export.tgz 内单个常规文件，不解包到文件系统。集成清单内组件进 APP_VERSION，其余依赖进 LOCKED_LIBS。
5. 从选定构建提取产品配置、依赖和历史基线命名作为来源记录。本次输出的BASE_LINE（软件版本）由用户明确填写，构建类型由用户选择；本入口全为AVM，SOFTWARE_BRANCH固定0111，不作为表单输入，也不从其他历史基线继承。独立双版本卡要求明确填写conan_shared、uroad_m100、dag_compiler三个精确引用；其余组件可追加。MANIFEST_NAME保留选定基线的真实制品名用于复现，不是新包名称。核查基线继承的镜像属性；不悄悄删除这些属性后当作通过。

`RESOLVED_FOR_REVIEW` 只表示声明、基线和清单已解析，不表示头文件接口、ON/OFF 编译、ABI 或台架已经通过。例如基础雷达声明匹配，仍可能缺 ON 代码所需接口；需按现有依赖/编译预检处理，不能凭解析成功提交 APP。

`common_inputs(discovery, release_parameters)` 转成计划器公共输入，保留源码、基线锁和集成清单SHA；不能省略指纹核验。release_parameters必须显式给出BASE_LINE、BUILDVARIANT，SOFTWARE_BRANCH省略时固定0111，显式给其他ID则拒绝，不回退至历史命名。输入的uroad_m100引用单独保留为m100_recipe_ref，作为配方/版本基准，不重复放进公共APP_VERSION；未来OFF/ON变体引用仍须由本次固定源码和真实编译/发布证据确定。

## 确认顺序

解析输入 → 确定精确组件引用和最终 Jenkins 参数 → 展示确认 → 编译 M100 / 发布配方 → 核对真实收据且冻结参数未变 → 一次提交 Jenkins。

展示包括仓库、分支、Commit、产品基线、APP 模式、ON/OFF 交付范围、全部 Jenkins 参数、具体 APP_VERSION/LOCKED_LIBS 和待验证状态。不能仅显示“使用推荐配置”。内部固定引用后，分支移动、基线/依赖/参数或草稿版本变化会使旧确认失效。

`build_review` / `confirm_build_review` 是纯确认边界；调用方须由真实用户回调完成身份、私聊/卡片/nonce、原子消费与幂等校验。`ready_parameters_after_confirmation` 只允许编译/发布证据补齐，不允许换源码、版本、依赖或最终参数。构建提交不明查原队列，不重复发包。构建确认不能启动台架；生成软件资源后仍走原材料卡确认。

## 当前接入状态

独立PCM双版本功能已接入Bot输入和只读校验回调，详见[实际部署边界](pcm-dual-feature.md)。最终构建确认、后台构建worker仍未接入；旧同卡切换软件来源方案仍为候选，预览按钮禁用。不能把只读解析通过称为Bot已能自动发包。旧APP入口、材料确认、调度和台架保持原状。
