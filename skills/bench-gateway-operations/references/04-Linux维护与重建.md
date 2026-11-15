# Linux工作站：配置、维护与重建

> 当前流程、现场状态和问题以[2026-11-05文档入口](../../../docs/README.md)为准。本页保留设备配置细节；注明11月3日的观察和ZIP交付范围属于历史快照。

## 保持原工作站时

默认迁移不重装Linux、不搬动业务目录、不改变共享Runner、不重建数据库。Windows只需要正确SSH访问与接管连接/解析角色。不要把本包Linux快照直接全目录覆盖生产服务；它供核对、备份恢复和经审查的版本更新。

当前登录账号为 `lixiang`，工作站SSH别名 `ubuntu-workstation`。地址和主机公钥需由接手人通过可信现有SSH配置核对；本包不打包个人SSH私钥、完整known_hosts或含其他服务器的系统SSH配置。

## Python和程序环境

| 组件 | 当前配置 / 核对结果 |
|---|---|
| 网关业务Python | `/home/lixiang/miniconda3/envs/tvg/bin/python`，3.12.11 |
| SDK及旧受控回放Python | `/usr/bin/python3`，3.8.10；不能统一替换为网关解释器 |
| LPAI asset SDK | `lpai-asset==2.3.38`（不是公共PyPI任意同名asset包） |
| S3 SDK | boto3、botocore均1.35.99 |
| 加密 | 解释器路径上可见cryptography 46.0.3/系统2.8，须用实际调用解释器核对，不能据pip清单认定生效版本 |
| Node | `/usr/local/bin/node`；用于固定签名工具，不在命令参数传账号密码 |
| Docker | `/usr/bin/docker`；编译及Recorder环境由固定镜像/哈希约束 |
| YAML | 已登记的独立PyYAML归档，由网关配置引用 |

网关在Linux不需要Codex。旧`doctor`检查codex二进制不可用不是要求在工作站安装AI；应分别核对broker和Windows真实解析。LPAI SDK的原生Python依赖从公司批准源补齐，安装前先读现有版本，不盲目升级。

## 监督器与systemd的区别

生产业务由`bench_gateway.deployment`监督器管理worker和桌面连接观察器。标准只读状态命令：

```bash
cd /mnt/data/bench-eval/service/gateway
/home/lixiang/miniconda3/envs/tvg/bin/python -B -m bench_gateway.deployment --root /mnt/data/bench-eval/service/gateway status
```

11月5日紧急修复已将监督器交既有`bench-feishu-gateway.service`托管并核对active/running、bridge/worker ready；早先11月3日inactive观察不再代表现状。接手时仍同时检查systemd与项目状态，不能只看unit存在/ enabled。本次仓库整理不重启服务；后续切换先确认无全部项目活动及准备/解析/到点分析在途、正常排空，不同时运行两套监督器。真实系统重启恢复仍待验证。

正常停服务用项目`deployment stop`排空，不kill执行器。标准start不应被拿来额外启动另一个入口；维护前确认当前托管者。systemd状态与项目状态都要读，不能只看其中一个。

## 配置与启动依赖

`config/gateway.json`是权威配置。目录、域名白名单、模板、工具摘要、云端策略、确认及真实执行开关都在此。包中的`config/workstation-public-reference.json`是去除了敏感认证配置的**参考快照**，不是完整可启动配置；`workstation/gateway/config/gateway.safe-example.json`默认不开放真实执行。

必要外部组件：

1. 原Runner正式CLI、candidate-environments扩展和当前automation发布；保留完整扩展链，禁止直接调用内层Runner绕过保护。
2. 已审核模板、对应资格收据、受控回放工具、Recorder/lixplayCLI及约674MB依赖目录、原虚拟环境和Docker镜像。
3. 已登记的工作站runtime发布和PyYAML归档；本包源码不能替代其完整已安装环境。
4. 原生评测器与共享辅助脚本；Git仅包含评测器参考源码，不含二进制及动态库，完整安装须从受控原环境取得并核对路径/摘要。
5. RoadModel独立发布及其自身依赖。包内网关含集成代码，未打包RoadModel模型、权重或完整业务发布。

具体路径及摘要在参考配置和静态清单。受控模板引用历史小收据，这些不是无用日志；本包不搬运用户历史，复用原工作站时保持原位。全新Linux重建需要安全备份与官方组件补齐，本包不是包含所有数据/镜像/认证的磁盘镜像。

## 如确需重建Linux

按顺序：确认账号/磁盘/网络→准备解释器→恢复程序和原Runner扩展→补固定工具/镜像/评测器→私有配置认证→恢复一致性业务数据库和源目录（或明确全新空部署）→只读检查→受限解析→飞书菜单/卡片→最后经真实确认做台架验收。不能用空数据库覆盖已有业务，也不能把别的台架当原设备。

复制源码不赋予可执行权限；按实际目标对`desktop_bridge_observer`、正式CLI、评测`run.sh`和二进制设置所需0755，不递归chmod用户软件和数据。配置与认证目录使用最小权限。不要执行旧`deploy_*.py`、迁移清理器或修锁器来“补环境”。

## 台架SSH和服务

Runner通过自己的`config/ssh_config`访问`bench-avm`（当前目标192.0.2.15:22）。没有登录密码不等于没有SSH主机公钥。正常检查严格核对设备身份。连接失败先区分路由/物理连接/SSH认证/指纹/远端命令，不能把任意错误都说成物理断线。

回放涉及正式服务切换、候选目录、权限与恢复；全部由原受控驱动和收据管理，不能把历史临时Permissive修复写成每任务默认操作，也不能在交接时手工重复刷包。
