# 离线台架评测自动化运行时

Runner 1.8.1 在 1.8.0 目标驱动工作流上补充显式 `formal_update` 正式保留模式，并继续接受 protocol 1.0、1.1 Job。protocol 1.0 的 `full-evaluation`、`FULL_COMPLETED`、新评测 attempt 及台架事务语义是冻结的生产基线；1.1 只作为外围增量编排，不改变该基线。用户声明需要的最终产物，Planner 反向解析依赖，并只执行缺失或身份失效的节点：

- `workstation_predictions`：工作站上的团队共享、哈希验证预测权威产品；
- `lpai_predictions`：从工作站权威产品发布到提交者个人 LPAI 空间的副本；
- `evaluation_report`：在真值完整的前提下，由可验证评测 attempt 产生的报告。

因此一次任务可以只回灌并保留工作站结果、只把已有工作站结果上传到个人开发空间，或直接对已有 LPAI 预测执行评测。只有解析后的 DAG 需要台架时才申请 `ACTIVATE`/`REPLAY` 审批和 `bench.lock`。LPAI 上传或评测失败只从工作站权威产品继续，绝不反向触发回灌。

工作站仍是确定性执行面，不安装 AI；LPAI 是每位用户的个人空间。凡涉及个人 LPAI 的 `target-workflow` 或兼容 `full-evaluation` 均以前台 `worker once` 运行，使用提交者当前 SSH agent 转发会话，不把私钥、Token、Cookie 或会话保存到共享工作站。

## 统一 CLI

本地开发时：

```powershell
python automation/bench_eval.py version
python automation/bench_eval.py preflight --job automation/jobs/raw-pack-11-smoke.json
python automation/bench_eval.py plan --job automation/jobs/raw-pack-11-smoke.json --output automation/runs/dry-run-plan.json
python automation/bench_eval.py audit --job automation/jobs/raw-pack-11-smoke.json --output automation/runs/workflow-audit.json
python automation/bench_eval.py self-test
```

## protocol 1.1 目标与复用

Job 使用 `runner_protocol: "1.1"`，并声明 `workflow.targets`。以下是三个典型入口：

```json
{
  "runner_protocol": "1.1",
  "mode": "target_workflow",
  "workflow": {
    "targets": ["workstation_predictions"],
    "so_policy": "auto",
    "reuse_policy": "verified_only"
  }
}
```

```json
{
  "runner_protocol": "1.1",
  "mode": "target_workflow",
  "workflow": {
    "targets": ["lpai_predictions"],
    "sources": {
      "workstation_predictions": "/mnt/data/bench-eval/products/<product>"
    }
  }
}
```

```json
{
  "runner_protocol": "1.1",
  "mode": "target_workflow",
  "workflow": {
    "targets": ["evaluation_report"],
    "sources": {
      "lpai_predictions": "/lpai/bench-eval/predictions/<product>"
    }
  }
}
```

`sources` 只表示已存在的起始产品，不能把同一产品同时声明为 source 和 target，也不能同时提供工作站与 LPAI 两种预测源。作为 source 的工作站产品必须带相邻 `<product>.bench-eval-product.json` producer receipt，并重新核验 `producer_run_id`、当前文件 inventory、inventory SHA-256 与 product identity。旧产品不能因目录存在而受信任；只能由操作者显式运行 `transfer-predictions --backfill-workstation-product`，且所给 `HOP1_VERIFIED` evidence 必须与当前产品逐文件一致。`so_policy` 可为 `installed`、`prebuilt`、`build` 或 `auto`；1.8.0 不让 `auto` 自动接管已验证的生产路径。任何历史 receipt 只是一项复用候选；输入 identity、文件清单或输出校验不匹配时状态必须为 `INVALID_CACHE`，Planner 会保留证据并要求重新执行相应节点。

protocol 1.1 的成功终态是 `COMPLETED_TO_REQUESTED_GOAL`，表示已经达到本次声明的目标，不暗示未请求的评测或上传也完成。protocol 1.0 仍保持固定链路和 `FULL_COMPLETED` 语义。

评测以 `evaluation-attempts/<scene>/aNNN/` 保存不可覆盖历史。中断或失败后从 `a002`、`a003` 继续，不覆盖旧命令、报告和日志；已有成功 attempt 只有在 evaluation identity、报告、帧有效性与 required task 指标都重新验证通过时才返回 `EVALUATION_REUSED`。`run.sh` 不提供帧级 checkpoint，失败 attempt 本身不能断点续算。

真值在启动 `run.sh` 前重新检查。`.<scene>.partial`、`.<scene>.layout`、空目录或 `truth-manifest.json` inventory 不一致都会 fail-closed。manifest 可通过数据准备脚本生成；旧 Profile 可暂时使用非空目录兼容规则，正式严格流程应启用 `--require-truth-manifest`。

## 全量多场景批次

全量模式不把七个场景塞进一个不可恢复的长命令，而是扩展为七个独立子任务。每个场景拥有自己的候选目录、预测目录、传输哈希、LPAI 配置和恢复证据；普通场景失败后其余场景仍可继续，任何 `RESTORE_FAILED_UNSAFE` 都会保留全局台架锁并阻断后续任务。

先在工作站生成一次真实数据清单，再由个人 AI 或工作站 CLI 生成批次：

```bash
~/bench-eval/bin/bench-eval prepare-replay inventory \
  --archive /home/lixiang/developer/raw_pack_11.tar.gz \
  --output ~/bench-eval/jobs/raw-pack-11.inventory.json

~/bench-eval/bin/bench-eval batch plan \
  --base-job ~/bench-eval/jobs/raw-pack-11-full.template.json \
  --inventory ~/bench-eval/jobs/raw-pack-11.inventory.json \
  --batch-id sw0055-2723-full-20260917-001 \
  --output-dir ~/bench-eval/batches/sw0055-2723-full-20260917-001
```

`batch plan` 会拒绝多个场景共用 `release_package.isolated_run`、预测源/目标或评测配置路径。模板应在这些位置使用 `{run_id}`、`{batch_id}` 和 `{scene}`。检查生成的 `batch.json` 与 `jobs/*.json` 后，提交和审批仍是显式动作；不带 `--execute` 只预演：

```bash
~/bench-eval/bin/bench-eval batch submit \
  --batch-dir ~/bench-eval/batches/sw0055-2723-full-20260917-001 \
  --submitted-by developer --client codex-windows

~/bench-eval/bin/bench-eval batch submit \
  --batch-dir ~/bench-eval/batches/sw0055-2723-full-20260917-001 \
  --submitted-by developer --client codex-windows --execute

~/bench-eval/bin/bench-eval batch approve \
  --batch-dir ~/bench-eval/batches/sw0055-2723-full-20260917-001 \
  --stage ACTIVATE --approved-by developer --execute
~/bench-eval/bin/bench-eval batch approve \
  --batch-dir ~/bench-eval/batches/sw0055-2723-full-20260917-001 \
  --stage REPLAY --approved-by developer --execute

~/bench-eval/bin/bench-eval batch status \
  --batch-dir ~/bench-eval/batches/sw0055-2723-full-20260917-001
```

失败重跑必须只复制失败场景形成新的 `run_id`，不得覆盖原任务或原 LPAI 目录。批次完成条件是全部场景进入终态、每个成功场景都通过预测文件有效性检查，并且所有发生过激活的子任务均有恢复核验证据。

共享工作站 Runner 的本地状态接口：

```bash
~/bench-eval/bin/bench-eval capabilities
~/bench-eval/bin/bench-eval doctor
~/bench-eval/bin/bench-eval submit --job ~/bench-eval/jobs/job.json --submitted-by zhangsan --client codex-windows
~/bench-eval/bin/bench-eval approve --run-id <run-id> --stage ACTIVATE --approved-by zhangsan
~/bench-eval/bin/bench-eval approve --run-id <run-id> --stage REPLAY --approved-by zhangsan
~/bench-eval/bin/bench-eval claim-bench --run-id <run-id>
~/bench-eval/bin/bench-eval status --run-id <run-id>
~/bench-eval/bin/bench-eval queue
~/bench-eval/bin/bench-eval cancel --run-id <run-id> --cancelled-by <name> --reason '<reason>'
~/bench-eval/bin/bench-eval requeue --run-id <run-id> --requeued-by <name>
~/bench-eval/bin/bench-eval worker status
```

`doctor` 是部署在工作站上的只读接入与链路诊断。它从 `SSH_CONNECTION` 识别发起连接的个人电脑，检查返回路由、工作站 SSH 服务、台架父接口/VLAN、台架路由与邻居、TCP 22 和全局锁；输出稳定 JSON 错误码并返回 2 表示阻塞。它不会自动修路由、重启服务、刷包或回灌。

首次部署要从 release 复制设备模板到持久配置目录，并由工作站/网络管理员填写获批的管理网 CIDR：

```bash
cp ~/bench-eval/current/automation/profiles/device.example.json ~/bench-eval/config/device.json
${EDITOR:-vi} ~/bench-eval/config/device.json
```

个人电脑侧由唯一的 `bench-eval-execution` Skill 连接固定的 `ubuntu-workstation`。Windows 通用入口是 `skills/bench-eval-execution/scripts/run_target_workflow.ps1`；它读取本次 Job 与工作站已审查 Profile，没有内置 APP、SO、数据、场景或目标产物。Prepare 调用内置的 `scripts/access/connect_workstation_ssh.ps1`：已接入电脑只做一次 SSH + Runner `capabilities` 握手；首次用户在本机初始化、公钥登记等明确授权后由同一接入脚本自动复验。工作站密码只允许用于首次登记 `.pub`，不得保存或作为日常自动化凭据。执行阶段只消费新鲜的 `bench-workstation-access/v1` READY 报告。个人电脑 SSH 失败、台架占用、工作站到台架和工作站到个人 LPAI 是独立作用域，不得混合归因。005 的 SW0058/v151 输入仅是历史案例，不能代替新 Job 的 Profile、候选身份及回滚验收。

`submit` 将 Job、计划、提交人、客户端和事件日志持久化到唯一运行目录。共享账号采用逻辑区分而非严格操作系统隔离。protocol 1.0 仍要求 ACTIVATE、REPLAY 两项审批；protocol 1.1 则由实际 DAG 动态计算：需要台架时要求相同审批，从已有工作站或 LPAI 产品开始的上传/评测任务无需台架审批即可进入 `READY`。`queue` 按持久化提交顺序返回队列位置。`plan.json` 中的 `workflow.requires_bench` 只是审计期望；Worker 必须从不可变 `job.json` 和所选 Profile 重新计算锁需求，二者不一致时在 executor、SSH 或锁操作之前 fail-closed。只有重算值为 `true` 的任务才创建全局 `bench.lock`；合法非台架 `target-workflow` 可在台架被其他任务占用时继续。临时候选必须使用 `RESTORE_VERIFIED` 释放锁；用户明确要求正式替换且不可变 Job 使用 `workflow.bench_strategy=formal_update` 时，可在输出、活动文件哈希和服务状态全部验证后使用 `FORMAL_ACTIVATED_VERIFIED`：

```bash
~/bench-eval/bin/bench-eval release-bench \
  --run-id <run-id> \
  --reason RESTORE_VERIFIED \
  --evidence ~/bench-eval/runs/<run-id>/restore-verification.json

~/bench-eval/bin/bench-eval release-bench \
  --run-id <formal-run-id> \
  --reason FORMAL_ACTIVATED_VERIFIED \
  --evidence ~/bench-eval/runs/<formal-run-id>/formal-activation.json
```

`cancel` 只允许提交者或 Job 中 `coordination.allowed_actors` 指定的协助者，在任务尚未持锁时取消；不会删除任何运行证据。`requeue` 只允许恢复 `FAILED_COORDINATION`，不能绕过 App 恢复或实机失败处理。

Runner 提供 `coordination-smoke`、`profile-transaction`、兼容 `full-evaluation` 和 protocol 1.1 的 `target-workflow`。后两者只允许前台 `worker once`，必须运行在提交者保持有效的 SSH agent 转发会话中；它们不会作为共享后台服务保存个人 LPAI 凭据。`target-workflow` 根据声明目标执行到 `COMPLETED_TO_REQUESTED_GOAL`。Job 只能选择 Profile id，不能携带 shell 命令。

```bash
~/bench-eval/bin/bench-eval worker start --executor coordination-smoke
~/bench-eval/bin/bench-eval worker start --executor profile-transaction --profile ~/bench-eval/config/device.json
~/bench-eval/bin/bench-eval worker once --executor full-evaluation --profile ~/bench-eval/config/sw0055-2723-full.json
~/bench-eval/bin/bench-eval worker once --executor target-workflow --profile ~/bench-eval/config/<submitted-by>.json
~/bench-eval/bin/bench-eval worker status
~/bench-eval/bin/bench-eval worker stop
```

安装到 Ubuntu 工作站后：

```bash
~/bench-eval/bin/bench-eval version
~/bench-eval/bin/bench-eval preflight --job ~/bench-eval/jobs/job.json
~/bench-eval/bin/bench-eval status --run-id <run-id>
```

统一入口也代理现有窄权限工具：`prepare-replay`、`stage-package`、`stage-existing`、`prepare-config`、`compare-trees`、`replay` 和 `validate-output`。各工具原有的参数和 `--execute` 限制保持不变，例如：

台架 SSH 默认使用 `publickey`。只有设备配置已核实服务端明确采用 PAM 空确认时，才设置 `ssh_auth_mode=keyboard-interactive-empty` 或为隔离刷包传入 `--ssh-auth-mode keyboard-interactive-empty`。该模式关闭公钥和密码尝试、最多提交一次空响应，并通过强制 askpass 保证不打开窗口；它不是空密码，也不允许扩展为任意密码保存机制。

```bash
~/bench-eval/bin/bench-eval replay --base-url http://127.0.0.1:15000 status
~/bench-eval/bin/bench-eval stage-existing --help
```

回灌后、传输前先验证场景帧数和必需产物：

```bash
~/bench-eval/bin/bench-eval validate-output \
  --root /app/livc-test/<run-id>/outputs/eval_pred_result \
  --scene <scene-name> \
  --expected-frames 120 \
  --require-prefix pred_traj_ \
  --require-prefix traj_xy_
```

只有输出 `PREDICTION_OUTPUT_VALID` 和 `transfer_eligible=true` 才能进入结果回传阶段。具体必需前缀由评测配置决定，不应由 CLI 猜测。

预测首先收集到工作站的共享权威产品，并逐文件校验 SHA-256；只有本次目标包含 `lpai_predictions` 或 `evaluation_report` 时，才继续发布到提交者的个人 LPAI：

```bash
~/bench-eval/bin/bench-eval transfer-predictions \
  --job ~/bench-eval/jobs/<job>.json \
  --profile ~/bench-eval/config/device.json \
  --run-dir ~/bench-eval/runs/<run-id>

# 计划确认后才加 --execute
```

历史 hop-1 产品需要显式建立 producer receipt 时，必须同时给出共享缓存根、生产 run id 和对应证据；该模式只写凭据，不上传 LPAI：

```bash
~/bench-eval/bin/bench-eval transfer-predictions \
  --backfill-workstation-product /mnt/data/bench-eval/products/<product> \
  --cache-root /mnt/data/bench-eval/products \
  --producer-run-id <original-run-id> \
  --hop1-evidence ~/bench-eval/runs/<original-run-id>/transfer-evidence/hop1.json
```

Profile 用 `prediction_transfer.sets` 固定两端 SSH 别名、允许根目录和工作站共享产品根目录；Job 只选择 `transfer_set_id` 并给出根目录下的源/目标路径。工作站收集与 LPAI 发布分别生成 `COLLECT_WORKSTATION`、`PUBLISH_LPAI` receipt；收集完成后还在产品旁写入 `<product>.bench-eval-product.json` producer receipt。上传已有工作站产品前必须按当前内容重新核验该 receipt。历史 hop-1 产品只能通过上述显式 backfill 模式和逐文件匹配的 `HOP1_VERIFIED` evidence 建立凭据，不自动扫描或默认证任。LPAI 段失败后重新执行会验证并复用工作站产品，不重新访问台架或重新回灌。现有不同内容的目标、`.partial` 目录或哈希差异都会阻塞，不覆盖现场。

SSH 端点默认调用 `python3` 生成远程哈希清单和执行原子收口。若 LPAI 镜像仅有 Conda Python，可在该端点显式配置安全的命令名或绝对路径，例如 `"python_binary": "/platform/conda/bin/python3.12"`；不得用该字段携带 shell 参数。
目标镜像没有 `rsync` 时，可将目标端点设为 `"copy_protocol": "tar"`，并用 `tar_binary` 指定已有 tar 路径。Runner 通过 SSH 直接流式传输到唯一 `.partial` 目录，仍需逐文件哈希验证和原子收口；默认协议保持为 `rsync`。

LPAI 评测同样采用个人 Profile 固定命令：

```bash
~/bench-eval/bin/bench-eval run-evaluation \
  --job ~/bench-eval/jobs/<job>.json \
  --profile ~/bench-eval/config/users/<submitted-by>.json \
  --run-dir ~/bench-eval/runs/<run-id>
```

预演不会执行命令；明确执行时才加 `--execute`。Profile 的 `evaluation.sets` 必须同时定义 `evaluate` 和 `collect_report`，Job 只能提供评测集 ID、受限根目录下的预测/配置路径和可选准入规则。解析器可从 `uroad-dev-eval-20260818` 报告提取帧完成度、轨迹 Z_MAE、Acc_1cm、综合通过率、occupied IoU 和视觉 mAP，并按报告中实际启用的任务校验对应指标；轨迹-only 报告不要求视觉 mAP，已出现的任务指标不完整仍会拒绝。未提供准入规则时结论固定为 `NOT_EVALUATED`，不自行编造发布阈值。

`preflight` 在有阻塞项时返回退出码 2，并区分 `stage_ready` 和 `replay_ready`。这允许已安全解压的版本继续保留，同时阻止尚未批准的 App 切换和回灌。

## 制品、编译与候选组装

正式输入由 Jenkins/JFrog 生成的 `.app.tar.gz`、对应 Lockfile，以及三份经批准的 AArch64 评测库组成。推荐直接在工作站执行 `artifact acquire`；它接受 JFrog 页面中的 `/ui/native/` 链接并转换为下载地址，支持断点续传、强制 SHA-256 校验和原子收口。认证只读取工作站上权限为 `600` 的外部 netrc 文件。Cookie、密码和 Token 不得写入 Job、Profile、资产清单、共享脚本或命令行。

工作站管理员或使用者本人应通过交互终端创建 netrc；优先填写团队发放的只读访问令牌，不要把内容发送给 AI。若公司只允许浏览器 SSO，则仍可在工作站桌面浏览器下载，再把下载后的工作站路径交给 `artifact intake`。

```bash
~/bench-eval/bin/bench-eval artifact acquire \
  --artifact-id sw0055-2711-source \
  --url 'https://artifactory.example/ui/native/repo/path/package.app.tar.gz' \
  --expected-sha256 '<jenkins-or-jfrog-sha256>' \
  --netrc-file ~/.config/bench-eval/jfrog.netrc

# 审核计划后才执行；失败时保留 .partial，重跑会从断点继续
~/bench-eval/bin/bench-eval artifact acquire ... --execute
```

`liburoad_m100.so` 在已批准的 LiDocker 环境中编译。每次换新 APP/Lockfile 组合都必须重新判定是否编译：新 uroad commit、评测补丁/source diff、baselock/依赖身份变化或旧构建证据缺失时必须重编；只有构建身份完全一致且新三库组合的 `artifact intake` ABI 校验为 `COMPATIBLE` 时才可复用旧 SO。换新 APP 后即使复用 SO，也必须用新 `artifact-id` 重做 INTAKE/ASSEMBLE。需要重编时，先在隔离源码副本应用版本化补丁，再执行只读计划，确认后加 `--execute`：

先用受控脚本启动临时容器；脚本从工作站现有 Docker 登录态读取凭据但不打印或落入任务证据，并固定官方 `0.7.25` 镜像。源码目录若已有 `build/` 或 `install/` 会被拒绝，避免复用旧产物：

```bash
bash ~/bench-eval/current/automation/assets/uroad-lidocker-container.sh \
  start /path/to/fresh-patched-uroad bench-eval-lidocker-build-<run-id>
```

```bash
~/bench-eval/bin/bench-eval build-eval-lib \
  --source /home/lixiang/developer/build-work/uroad-3.1.0-evaldump \
  --expected-commit d04caab7e81ae7c9b377e904706fffa84229c8b7 \
  --artifact-id uroad-3.1.0-sw0055-evaldump

# 先启动 LiDocker；AI 从 docker ps 解析唯一容器 ID，再明确执行
~/bench-eval/bin/bench-eval build-eval-lib ... --container <lidocker-container-id> --execute
```

无论编译成功或失败，保存日志后都删除本次临时容器：

```bash
bash ~/bench-eval/current/automation/assets/uroad-lidocker-container.sh \
  stop bench-eval-lidocker-build-<run-id>
```

拿齐 APP、Lockfile、`libxcamera.so`、`libxlidar.so` 和 `liburoad_m100.so` 后，先登记哈希和 ABI 兼容证据，再组装候选。`artifact intake` 会用 `readelf` 将 `liburoad_m100.so` 需要的 `XCamera`/`XLidar` 动态符号与对应库的导出符号逐项匹配；缺失符号会在缓存写入和组包前直接阻断。两个命令默认只给计划，加 `--execute` 才写缓存：

```bash
~/bench-eval/bin/bench-eval artifact intake \
  --artifact-id sw0055-2711-eval \
  --app /path/CCU_LIVC_AVM-release-SW0055_ULTRA_ID0111.20260911-2711.app.tar.gz \
  --lock /path/CCU_LIVC_AVM-release-SW0055_ULTRA_ID0111.20260911-2711.lock \
  --library libxcamera.so=/approved/libxcamera.so \
  --library libxlidar.so=/approved/libxlidar.so \
  --library liburoad_m100.so=/approved/liburoad_m100.so \
  --execute

~/bench-eval/bin/bench-eval artifact assemble \
  --manifest ~/bench-eval/cache/assets/sw0055-2711-eval/artifact-manifest.json \
  --run-id sw0055-2711-candidate-001 \
  --execute
```

构建器会先校验指定容器确实把源码目录挂载到 `/root/workspace/apps`，再通过 `docker exec` 运行固定的 `build.sh` 参数；动态容器名由 AI 从当前唯一 LiDocker 容器解析，不固化到 Profile。组装器只允许替换这三个库，要求 APP 中每个目标唯一，校验替换库为 ELF64 AArch64，保存原库副本、前后 SHA-256、候选压缩包与清单；全程不连接台架。候选包仍须经过 `stage-package` 隔离写入 `/app/livc-test/<run_id>`，不得直接覆盖 `/app/livc`。

`audit` 与 `preflight` 的含义不同：前者同时检查“任务信息是否齐全”和“对应阶段是否已有可执行适配器”。因此，即使某个任务没有 TODO，当前版本也不会错误声称整条实机链路已经自动化。`self-test` 只运行本地单元测试，不会发起 SSH 或访问 LPAI。

## 当前自动化覆盖

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| ACQUIRE | 已实现 | 工作站 HTTPS/JFrog 直下、断点续传、外置 netrc、强制哈希；浏览器 SSO 仍由用户本人完成 |
| BUILD | 已实现 | 固定参数编译并归档评测版 `liburoad_m100.so`，需在已批准 LiDocker 环境执行 |
| INTAKE | 已实现 | APP、Lockfile、三库及 SHA-256 原子登记到工作站缓存 |
| PREFLIGHT | 已实现 | 作业、路径、哈希、敏感信息和阻塞项检查 |
| STAGE | 已实现 | 工作站传包或台架现有包隔离解压 |
| ASSEMBLE | 已实现 | 只替换三份获批库，保留原库、生成候选包与清单 |
| CONFIGURE | 已实现 | 在候选副本生成离线回放配置并记录差异 |
| ACTIVATE | 部分实现 | Profile 事务与启停/健康步骤已实现；缺评审后的真实设备命令及实机验证 |
| REPLAY | 已实现 | RecorderHMI 状态、加载、启动及失败清理 |
| COLLECT_WORKSTATION | 已实现 | 台架预测校验、共享工作站产品、inventory identity 与 receipt |
| PUBLISH_LPAI | 已实现 | 从已验证工作站产品发布个人副本；失败不触发回灌 |
| EVALUATE | 已实现 | 真值 readiness、不可覆盖 attempt、报告有效性和成功复用；真实运行仍依赖个人 Profile/授权 |
| RESTORE | 部分实现 | 激活后必执行一次恢复和核验；恢复失败保留锁；缺真实命令及实机验证 |
| REPORT | 已实现（目标级） | protocol 1.1 记录 requested targets、节点 receipt 与 `COMPLETED_TO_REQUESTED_GOAL`；跨场景业务汇总仍按批次生成 |

协同执行层已经具备持久任务、FIFO 队列位置、逻辑所有者取消、可恢复的协调失败、后台 Worker PID/日志、原子台架锁、fail-closed 台架事务、双段传输和 LPAI 评测适配边界。真实闭环仍须补齐并评审设备/个人 Profile，不能用本地 smoke Profile 代替实机验证。

台架策略分三类：`use_current_app` 不启停 App；`isolated_candidate` 在成功或失败后都恢复并核验；`formal_update` 只用于用户明确要求正式替换、输入内容身份固定且计划允许活动路径写入的任务。`formal_update` 在输出、活动文件哈希和服务健康全部通过后保留新版本；在提交点前任一步骤失败则只恢复一次。任何恢复或核验失败都进入 `RESTORE_FAILED_UNSAFE` 并保留 `bench.lock`。

机器可读的安全契约在 `workflow-contract.json` 和 `control-plane-contract.json`。Jenkins 任务本身仍是外部系统；1.8.0 可以继续调用已实现的 ACQUIRE/BUILD/INTAKE/ASSEMBLE 独立工具，但当前 `target-workflow` 执行器主要编排台架、工作站收集、LPAI 发布和评测节点，不能把计划中的前置节点误报为已自动执行。将这些前置工具接入生产 target executor、让 `auto` SO 策略自动选路、广义 requeue/resume 和帧级 checkpoint 均不属于本次收口。任何交互登录或访问令牌初始化由个人完成，运行时不回显账号凭据。

## v151 当前批次状态（2026-09-18）

`v151-formal-20260918-002` 的台架回灌已经完成 18/18 场景。批次回灌证据位于 `/home/lixiang/bench-eval/runs/v151-formal-20260918-002/replay-evidence/batch-result.json`。原工作站结果 `/mnt/data/bench-eval/results/v151-formal-20260918-002` 曾包含 1,800 个 `pred_traj_*.npy`、1,800 个 `traj_xy_*.npy` 和 1,800 个 `pred_img_*.json`，共 5,400 个文件；这些回灌预测结果已于 2026-09-20 按用户要求删除，原始数据集 `/mnt/data/bench-eval/datasets/v151-20260918-001` 保留。

该批次是历史测试，当时用户只需要回灌与工作站预测收集；truth 传输、LPAI 评测和最终评测汇总没有被请求，因此不是未完成任务，也不应自动续跑。工作站上 6 个 2026-09-17 历史 READY 测试任务已使用 Runner `cancel` 转为 `CANCELLED`，证据保留，当前队列为空。如未来需要对 v151 评测，必须从保留数据集重新生成带完整 producer receipt 的工作站预测产品，再新建任务/attempt；不得删除或覆盖任何 `.partial`/`.layout` 失败证据。

## 先做什么

在等待负责同事确认服务器信息期间，可以完成以下工作：

1. 用 `config.example.json` 把已知信息填出来（场景名、PCM 包版本、真值目录、基线版本等）；未知信息保留为 `TODO`。
2. 运行模拟任务，确认各阶段、产物目录和报告格式是否符合团队习惯。
3. 把每一次人工跑通时使用的真实命令、输入路径、输出路径和成功标准补进配置与记录。
4. 约到同事后，优先确认“连接矩阵”：工作站、台架板子、开发空间各自的主机名/IP、账号、SSH 端口、跳板机、认证方式、允许访问方向，以及实际启动/停止/回放命令。

不要把密码、JWT、私钥或访问令牌写进这个目录。认证信息应该放在本机 SSH 配置、密钥管理工具或团队批准的凭据系统中。

## 运行模拟流程

在项目根目录执行：

```powershell
python automation/bench_eval.py simulate --config automation/config.example.json
```

如果 Windows 上 `python` 命令不可用，可尝试：

```powershell
py automation/bench_eval.py simulate --config automation/config.example.json
```

运行完成后会生成 `automation/runs/<run_id>/`：

- `manifest.json`：本次任务的输入和配置快照；
- `events.jsonl`：逐步状态记录；
- `report.md`：给人阅读的简短模拟报告。

## 流程边界

本骨架的顺序对应文档中的离线台架评测：

`预检 → 隔离部署 → 组装候选 → 启动台架 App → 回灌场景 → 校验并上传预测产物 → 运行评测 → 恢复并核验原 App → 生成报告`

其中数据准备、隔离部署和 RecorderHMI 单场景 `connect/load/start` 已在实际工作站与台架验证；开发空间 `run.sh` 已用历史预测结果完成一次人工冒烟。受控 App 切换/强制恢复、Fresh 预测结果生成、双跳回传和自动评测仍需补齐适配器并在台架可用时验证。

## 已实现的真实安全能力

- `prepare_replay.py`：校验原始数据包，按场景安全抽取，检查五类回放信号与标定文件，生成回放配置和任务 manifest。
- `replay_client.py`：非交互检查 RecorderHMI 状态、校验/加载配置；只有显式 `--execute` 才允许发送 `load` 或执行回放；连接、加载或启动失败时会请求 `stop` 清理 Recorder 状态。
- `stage_package.py`：把工作站上的包传到台架 `/app/livc-test/<run_id>`，校验双端 SHA-256 后隔离解压。
- `stage_existing_package.py`：直接校验并隔离解压台架 `/app/*.tar.gz`，避免包在工作站和台架之间往返。
- `prepare_bench_config.py`：从 `common_param.yaml` 原件生成未激活候选，只修改严格指定的离线回放键并记录前后哈希。
- `install_runtime.py`：在 Ubuntu 设备上版本化安装以上运行时，保留旧 release 回退。
- `network_doctor.py`：只读诊断个人电脑返回路由、工作站 SSH、台架物理/VLAN 网络和全局占用，不执行自动修复。

`RecorderHMI v0.2.4` 需要比当前工作站 GLIBC 2.31 更新的运行环境。本机已验证可通过已有 `vehicle-ppo:v1.0` 容器运行 HTTP 宿主，无需升级工作站系统。

## 需要向同事确认的最小信息

| 连接/对象 | 需要确认 | 目前可先填的位置 |
| --- | --- | --- |
| Ubuntu 工作站 | 主机名或 IP、账号、端口、是否需 VPN/跳板机、密钥登录方式 | `hosts.workstation` |
| 台架板子 | 从工作站如何连接、账号、端口、应用与结果目录、如何查看状态 | `hosts.bench`、`paths` |
| 开发空间 | 主机别名、账号、数据挂载路径、评测入口命令 | `hosts.devspace`、`commands.evaluate` |
| 回放端 | 由哪台机器执行、脚本路径、参数、日志与结束判据 | `commands.replay` |
| 版本/准入 | PCM 包来源、基线、评测指标、通过阈值、失败后的回滚方式 | `job`、`acceptance` |

在这些信息未确认时，不能把“模拟通过”理解为真实台架评测通过；它只说明编排框架和记录格式能运行。

## 隔离刷包演示

`stage_package.py` 用于在 Ubuntu 工作站上把一个已有 `.tar.gz` 包传到台架的隔离目录并校验。它具有以下硬限制：

- 只允许写入 `/app/livc-test/<run_id>/`；
- 不读取或修改 `/app/livc`；
- 传输前检查归档成员，拒绝绝对路径、`..` 越界路径、危险链接、设备文件和 FIFO；
- 传输后比较工作站与台架 SHA-256；
- 检查 `/app` 剩余空间并预留 512 MiB；
- 同名 `run_id` 已存在时拒绝覆盖；
- 失败后保留现场，不自动删除；
- 不执行版本激活、App 重启或回滚。

在工作站执行：

```bash
python3 ~/bench-eval/current/automation/stage_package.py \
  --package ~/bench-eval/cache/packages/example.app.tar.gz \
  --bench bench-avm \
  --ssh-config ~/bench-eval/config/ssh_config \
  --run-id demo-stage-YYYYMMDD-HHMMSS \
  --extract
```

成功后会在工作站 `~/bench-eval/runs/<run_id>/manifest.json` 和台架 `/app/livc-test/<run_id>/manifest.json` 各保存一份清单。隔离目录中的内容只有在后续单独设计并明确批准 `activate` 步骤后才允许影响当前台架版本。

脚本还会比较工作站与台架系统时间。默认 `--clock-skew-policy warn`：偏差超过 300 秒时记录告警但不阻塞，适用于负责人已接受时钟不同步的离线台架。需要严格校时的设备可传 `--clock-skew-policy block`。

## 部署到其他 Ubuntu 设备

运行时不绑定当前工作站。将项目复制到目标 Ubuntu 设备后执行：

```bash
# 首先安装但不激活，验证新 release 不改变 current
python3 automation/install_runtime.py --target-root ~/bench-eval

# 仅在 no-activate smoke、self-test 和人工评审全部通过后，才单独授权激活
python3 automation/install_runtime.py --target-root ~/bench-eval --activate
```

安装器会：

- 检查 Python 版本并对所有 Python 文件执行语法验证；
- 创建版本目录 `~/bench-eval/releases/<release_id>/`；
- 创建持久目录 `cache/`、`config/`、`jobs/`、`locks/` 和 `runs/`；
- 写入包含文件 SHA-256 的安装清单；
- 使用原子符号链接 `~/bench-eval/current` 激活新版本（仅当显式传入 `--activate`）；
- 创建稳定入口 `~/bench-eval/bin/bench-eval`（仅在激活时）；
- 保留旧 release，便于将 `current` 切回 1.7.0 等已验证版本。

1.8.1 的发布顺序固定为 no-activate 安装 → 检查 `install-manifest.json` 的 `activated=false` 且 `current` 未变化 → 在新 release 内运行自检 → 人工决定是否另行激活。生成交付包和 no-activate smoke 均不构成 production `current` 切换授权。

主机地址、密钥、路径和工具位置属于设备 profile，不打进运行时 release。工作站 `/home/lixiang/developer` 的外部资产状态记录在 `assets/workstation-developer.inventory.json`；只有状态为 `valid` 且具有 SHA-256 的资产才允许被工作流调用。

## 当前实机冒烟任务

本地任务配置为 `automation/jobs/raw-pack-11-smoke.json`，准备证据目录为 `automation/runs/raw-pack-11-smoke-20260909-001/`，实机回灌证据目录为 `automation/runs/raw-pack-11-eval-sw0045-20260909-001/`。已经完成单场景抽取、RecorderHMI HTTP 启动、临时切换至正式 `/app/livc` 的 PCM 入口，以及 120 帧回灌；随后恢复了 `infer_expert_fusion`。当前回放库的端口是相机 `50053`、雷达 `50052`，历史脚本中的 `50054` 不适用于该库。
