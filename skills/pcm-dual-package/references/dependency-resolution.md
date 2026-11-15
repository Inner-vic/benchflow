# 按本次输入重新解析依赖

本流程由运行 Skill 的代理及 `source_build_discovery.py` 使用已配置的 GitLab、Jenkins 和制品库只读能力执行。自动发现器不执行 recipe、不编译、不发包，详见[源码输入与确认](source-input-review.md)。

当前发包路径按所选交付范围复用原 Jenkins，单 APP 仅一次构建；明确双 APP 才分别构建。原 Job 会强制重编部分 @livc 组件，包括 SDK；下面复用预编译 SDK 的历史本地试配是接口与组包证据，不等于原 Jenkins 会跳过 SDK 编译。实际交付以原 Job 的标准 Conan 锁及制品核对为准。

## 证据优先级与失效条件

1. 指定业务提交的依赖声明、options、CMake 条件编译和 CI 引用。
2. 该提交对应的成功 CI 记录、完整锁与正式组件发布记录。记录原始锁 SHA-256；分清 OFF 成功和 ON 成功。
3. 发布 recipe 指向的源码提交，以及实际二进制的 settings、options、RREV、PID、PREV 和头文件/库来源。
4. 历史成功 APP 可在核实本次分支的 SDK、平台、完整锁和二进制身份后选为共同基线；没有这些匹配证据时，不因历史构建成功就直接套用。

缓存以业务源码提交、SDK/依赖完整身份、平台、profiles、构建选项、工具链及 CI 配置提交为键。输入任一项变化，重新检查引用关系、所需接口和二进制配置。不能把某分支名或“0.2.8 系列”作为长期兼容结论。

## 解析与匹配

- 从用户分支解析直接依赖，并沿 SDK recipe、锁与 CI 记录解析传递依赖和 override。版本范围必须落到精确发布身份；记录冲突的两端和解析依据。
- 从 OFF/ON 条件编译路径提取实际调用的方法、参数类型、数据结构、生命周期和注册入口。SDK 对雷达库的需要也要纳入，不能只修业务代码中一个编译错误。
- 依赖匹配须覆盖声明、定义、条件编译、安装内容与目标平台二进制。头文件中有 setter，但该包关闭 replay，不能视为可用实现。
- 对仍然适用的已发布组件，可直接准备 Jenkins 集成参数；用户无需额外拥有其源码读取权限。只有发布证据不足、需要源码检查或需要重建依赖时，才报告所需源码访问能力。
- 自动发现候选时，先追溯现用发布 recipe 中的官方源码地址，再查同仓库分支/标签及对应正式发布配置。无法读取时报告具体路径，不猜测迁移，不无限遍历无关仓库。
- 版本高低不是兼容依据。分别记录“源码接口具备”“匹配二进制存在”“完整依赖闭包可解析”“编译/运行验证”的状态。

## 候选与选用分离

候选记录包含：源码地址与提交、标签、接口证据、发布引用与完整二进制身份、目标配置、相对当前基线的变化、缺口及证据链接。缺证据就保留未知，不能自动写进最终 APP_VERSION 或 LOCKED_LIBS。

已声明依赖满足所有需要时继续固定；不满足时把候选和影响纳入审阅。更换依赖涉及本次业务基线变化时，应有用户/维护配置的明确依据。用户授权自动采用某项受维护的匹配策略后，在该策略范围内执行，不重复请求同一授权。

## 没有匹配二进制时可以自行试配

候选源码已有所需接口而现有包关闭 replay 时，不应把“必须先请工程师提供 True 包”作为前置条件。用户要求自己尝试配置时，可在隔离工作区按固定源码传 `-o with_replay=True -o platform=schu`；无需先修改发布 CI。

先准备适用的完整基线和 profiles，再解析锁并以 `conan install --build=never` 仅获取预编译依赖。不要调用原 build.sh（会更新基线/补构建）或 conan create（可能调用 source()重新拉 master）。本地 consumer 的配置/依赖逻辑会执行，应固定已审阅源码及 recipe 哈希；版本、source branch 等 CI 替换元数据在试配副本中显式固定并记录差异。

配置、依赖解析、CMake 配置、业务编译和发布是不同阶段。recipe 的 build() 往往连续 configure/build/install，不可当作只配置入口。实际编译须在用户授权范围内进行，本地试配结果不能直接晋级为正式共同基线。

认证缺失时准确区分 GitLab 源码读取与 Liconan/Artifactory 依赖读取。先查看当前已授权环境的登录状态，不读取或复制其他人的凭据；需要用户协助时只说明具体仓库及登录能力，不要求普通用户理解 SSH/容器，也不要求其逐个寻找开发包。

## 当前案例（2026-10-27 核验）

| 业务/依赖 | 当时观察到的事实 |
|---|---|
| uroad dev-314 `9a61fe17167b97d7b13c5b0c02348762e84ed206` | SDK `0.1.89-rc2`，X-Lidar `0.2.8`，`with_replay=True` |
| X-Lidar `0.2.8`，提交 `36c532db6b15dd9ce2e9683dae1c5dee69199026` | 有 RegisterBuiltinReceivers，缺 SetSceneSwitchPrepareCallback |
| dev/bwc_replay / `0.2.8-userdebug2`，提交 `9ae4cb4d3efd1dd334b5e04203f1087b36867b04` | 两个接口都有；标签说明“仅回放使用”；新增回调前 30ms 等待、场景 RPC，EDS 下界变为 >0.8.0 |
| 候选 recipe `47331ff89b5281c8b54f9cfb544fc87d` 下观察到的包 | PID `85101b8d3dd9c1e35a902937102c563a25358f0e`，PREV `6a09582caa01e95c8f01b9ca9ede13d0`，`with_replay=False`；不能直接满足本次 ON |

在此固定输入上，已用原成功 #2867 APP/完整锁复用 SDK 二进制，自行编译开启 replay 的候选 X-Lidar，再并行编译 OFF/ON、执行原 recipe 的 package()、组装完整 APP 并逐文件比较，通过。两版实际需要的 X-Lidar 动态符号均有导出。SDK 没有重编，身份和内容与原基线相同。

这证明当前固定组合可以编译、链接与组包；未完成全部 SDK ABI 行为、上机运行和评测数据验证。该映射仅适用于已列明的版本和提交，后续版本重新解析。历史本地试配的组合锁不能当本 Skill 的原 Jenkins 标准 Conan 锁使用；当前流程从真实构建取得标准锁，不伪造 SDK 包身份。

来源：[X-Lidar 源码提交](https://gitlab.chehejia.com/livc-acore/framework-platform/xlidar/-/commit/9ae4cb4d3efd1dd334b5e04203f1087b36867b04)、[发布二进制元数据](https://artifactory.ep.chehejia.com/ui/repos/tree/General/liconan-dev-local/livc/xlidar/0.2.8-userdebug2/stable/47331ff89b5281c8b54f9cfb544fc87d/package/85101b8d3dd9c1e35a902937102c563a25358f0e/6a09582caa01e95c8f01b9ca9ede13d0)。
