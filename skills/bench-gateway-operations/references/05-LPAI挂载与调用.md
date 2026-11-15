# LPAI开发空间：挂载、数据传输、调用与报告

> 当前流程、现场状态和问题以[2026-11-05文档入口](../../../docs/README.md)为准。本页保留设备配置细节；注明11月3日的观察和ZIP交付范围属于历史快照。

## 已配置实例（只读查询，2026-11-03）

| 项 | 值 |
|---|---|
| 名称 | `bench-eval-offline` |
| UID | `395dafd5-4701-4480-8fad-3c33e9d24e53` |
| namespace | `vc-chassis` |
| cluster | `cnhb01-algorithm-bidu-p-03` |
| 镜像 | `reg-ai.chehejia.com/developer/notebook/bench-eval-offline` |
| 资源 | 1 CPU、4GiB、0 GPU |
| 闲置策略 | 240分钟自动停止 |
| 本次查询状态 | stopped；未启动、未运行新的评测 |
| Jupyter配置 | `/lpai/bench-eval-offline/jupyter-config` |
| 程序PYTHONPATH | `/lpai/bench-eval-offline/runtime` |

当前三个挂载：

| 卷名 | 容器内路径 | 用途 |
|---|---|---|
| `jfs-dip-vfsc-dataset-ga` | `/mnt/volumes/jfs-dip-vfsc-dataset-ga` | PCM等回放/GT来源，输入下载白名单UID `53a8e589e3a342ac893c54a554494f60` |
| `jfs-dip-vfsc-chassis-ga` | `/mnt/volumes/jfs-dip-vfsc-chassis-ga` | 已有V151等GT挂载范围 |
| `vc-chassis-bd-ga` | `/mnt/volumes/vc-chassis-bd-ga` | 独立预测和报告输出，UID `2da0a26d1ab44fed845235a9fcd56c98` |

平台返回三个挂载均为Writable；业务程序只读GT且不写源数据卷，**不能宣称平台已经强制只读挂载**。团队可见实例不等于所有人都使用团队服务账号，当前镜像及输出前缀仍关联原维护者命名。

## A. 开发空间路径到工作站

用户在数据集框输入`/lpai/volumes/<已授权卷>/<数据根>`，或兼容`/mnt/volumes/...`；工作站路径也继续支持，不绑定PCM/V151具体目录。当前输入下载白名单只配置dataset-ga；挂载了其他卷不意味着它自动成为允许下载的输入卷。

1. `cloud_datasets.py`规划本次场景；`cloud_dataset_paths.py`验证卷和路径。
2. 工作站以`cloud_sdk_python=/usr/bin/python3`运行`cloud_dataset_sdk.py`，使用官方`lpai-asset`及原Linux账号的SDK配置。
3. 按卷UID读取实际对象存储地址并申请只读STS；不使用浏览器下载、不把公开路径当匿名URL。
4. 数据worker用boto3从对象存储直接读取；工作站本地执行模式不绕Windows。兼容的SSH stdin只携带程序/请求/临时认证，不携带数据集字节，也不把STS写入计划。
5. 只下载本次选择的`original`回放输入，GT不下载。输入缓存落`/mnt/data/bench-eval/datasets/<数据根末级名>/`，冻结视图和manifest随后登记。
6. 同源已完成场景可复用；扩范围只补缺失；同名异源停止，不覆盖旧版本。

该路径是逻辑卷路径，不是让工作站直接访问容器的POSIX目录。常规回放需要工作站可读取的本地目录/已存在挂载；当前自动云端入口实现为下载缓存，不是台架直接挂载LPAI。大型数据不经飞书/CC Connect/Windows。

场景按完整名称识别，支持根下场景及一层日期目录；当前用户可用编号范围选择。按时间段/VIN交互筛选不是已交付的新入口，不能把目录名中含日期/VIN就宣称已支持任意查询。

## B. GT如何确定

同组织数据通常为`<scene>/original`与`<scene>/dataset`。同上层目录并不意味着所有文件都要下载，也不意味着任意源必有完整GT。

工作站数据描述符的`evaluation_source`包含schema、dataset_root、gt_root；任务冻结时加入source_identity。云端请求携带该关联；接收端只在允许挂载根内按完整场景精确匹配，缺失/歧义停止。工作站已有目录若要评测，管理员须关联它在云端的真实GT；不能自动使用历史V151。

```json
{"schema":1,"dataset_root":"/mnt/volumes/<卷>/<本次数据根>","gt_root":"/mnt/volumes/<卷>/<本次GT根>"}
```

这是结构示例，不是可直接提交的配置。目录、权限和场景匹配必须实查。包内最新profile的gt_policy是`task-dataset-v1`。实例环境中仍有历史`BENCH_EVAL_GT_ROOT=.../v151/dataset`，该值不能覆盖新策略；只有实际加载的profile与接收端校验一致才可评测。实例当前停止，镜像内运行文件与最新策略是否一致须启动后另做只读核验，不能凭环境变量认定已接通。

## C. 预测如何上传，谁触发评测

原回放结果由工作站自动收集，用户不填写回放结果路径。`cloud_provider.py`根据固定评测器需要的required_tasks打包相应预测文件：trajectory的pred_traj_*.npy、vision的pred_img_*.json、road的pred_road_*.npy；不是把所有回放日志和GT一起上传。

工作站保存cloud-plan.json→导出predictions.tar.gz→生成request.json→固定Node签名工具生成request.sig→官方SDK上传专用输出卷。**request.sig最后上传**，是接收端可读取任务的边界；不是一次远程网页点击，也不是每任务新建实例。

输出路径：

```text
/mnt/volumes/vc-chassis-bd-ga/bench-eval-offline/developer/
├── runs/worker-status.json
├── runs/u_<owner>/cloud-eval-<UTC日期>-<任务摘要>/
│   ├── predictions.tar.gz
│   ├── request.json
│   ├── request.sig
│   ├── state.json
│   └── attempt/
│       ├── receipt.json
│       ├── eval_report/*.txt
│       └── 原生日志/配置视图
└── receiver-bundle-v2-task-gt/      历史接收端分发位置，非预测目录
```

工作站本次请求/上传收据/TXT在`state/users/<owner>/reports/<evaluation-job>/`；预测导出还可能在`users/<owner>/evaluations/.../cloud-export`，同盘可用硬链接，避免绕Windows。

不同用户、任务和软件版本使用稳定独立任务目录，不覆盖数据集目录或同名latest。相同任务提交结果未知时只续收同一cloud ID，禁止另建目录再次计算。

## D. 固定接收端如何常驻

镜像/运行目录须包含`cloud_agent.py`、`cloud_evaluation.py`、`cloud_truth.py`、`bench_cloud_server.py`、`profile.json`、签名公钥、report_scope.py、parse_eval_report.py以及`evaluator/`原生程序。

Jupyter通过`JUPYTER_CONFIG_DIR`加载server extension `bench_cloud_server`，扩展启动固定cloud_agent。`BENCH_EVAL_RUN_ROOT`指向上面的runs，`PYTHONPATH`指向runtime。平台可能覆盖JUPYTER_CONFIG_PATH，不能用它代替已核验的JUPYTER_CONFIG_DIR配置。重新建镜像时需保留这个启动链，不是只把脚本上传到输出卷。

接收端使用Linux抽象UNIX socket避免双接收器；每约2秒扫描`u_*/cloud-eval-*`。只读取有request.sig且尚无state.json的目录；验签、profile摘要、owner目录、工具hash、GT授权均通过后执行固定评测脚本。评测中独立线程每10秒更新心跳，完成后恢复READY；不接受请求中的任意shell或替换评测器。

心跳是`runs/worker-status.json`，包含实例、profile摘要、generation和时间。工作站检查60秒新鲜度以及精确profile，不只看文件存在。心跳不是登录保活；旧心跳既不能证明实例仍运行，也不能证明账号失效。

## E. TXT返回

工作站轮询state.json，SUCCEEDED/PARTIAL_SUCCESS后读取稳定attempt/receipt.json和其TXT路径，核对目录和场景范围，保存evaluation-report.txt并通过机器人API交付。当前收集上限16MiB，单个上传文件上限2GiB，云端等待约2700秒；超过限制或未知结果保留原目录待核验，不自动重跑。常规评测不遍历整份GT/预测做内容SHA，只校验授权路径、小清单、必要组件及报告。

## F. 新卷、新账号、新实例怎么改

需要平台挂载与授权、输入卷UID白名单、接收端gt_mount_roots、新的profile摘要与策略生效时间相互一致。旧Job保留原策略，不能改原profile冒充新版本。更换输出卷还涉及`cloud_storage.py`固定VOLUME和PREFIX，不是只改gateway.json；必须审查、测试并同步接收端。

当前`cloud_browser_autostart=false`，后台解析器没有浏览器能力。实例停机需管理员按平台流程启动；SDK/CLI审批返回未知或pending时不能绕过。本文不自动启动实例、不改变挂载或资源。
