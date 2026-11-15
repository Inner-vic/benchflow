# 新Windows电脑：逐步迁移交接

> 当前流程、现场状态和问题以[2026-11-05文档入口](../../../docs/README.md)为准。本页保留设备配置细节；注明11月3日的观察和ZIP交付范围属于历史快照。

目标：新电脑只替换旧Windows连接/解析电脑，原Linux工作站和台架不变。文档驱动，脚本是单步辅助，不采用“一键保证成功”。所有示例中的 `<...>` 需由接手Codex根据本机环境替换；不是可直接运行的命令。

## 0. 先读与先保留

阅读本包README、架构、现状与限制、运维回退。确认旧入口当前是否有解析或材料接收；不为迁移终止真实台架任务。新机尚未切换时不启动第二个CC Connect，不清租约，不重置SQLite，不修改台架锁。

将本包`windows/`复制到新电脑长期程序目录，例如 `D:\BenchGateway`。Windows与Linux源码不同，是两份设备快照，不互相覆盖。新机配置从`windows/config/gateway.example.json`建立`gateway.json`；不得把`workstation-public-reference.json`覆盖到Windows。

## 1. 本机环境

准备Windows当前用户可用的Python 3.11+（现机3.12）、pythonw、Windows OpenSSH和已获授权的Codex CLI。优先沿用已有安装；缺软件通过公司批准的软件源安装。不关闭安全防护、不改全局ExecutionPolicy、不使用SYSTEM、不收集Windows登录密码。

由Codex确定 `$ClientRoot`、`$Python`、`$Pythonw`；用实际路径执行版本检查。Codex二进制用本机PATH或项目`resolve_binary`发现，不能复制旧桌面应用带版本号的exe路径。账号首次登录由用户在新电脑按Codex官方流程完成；不要复制旧电脑Token，登录恢复不能冒充已经真实验证。

## 2. SSH与非敏感配置

必填/核对项：

| 字段 | 新机处理 |
|---|---|
| `deployment_role` | 固定 `ai-client`，不创建Windows业务库 |
| `workstation` | 使用已登记的 `ubuntu-workstation` SSH别名 |
| `remote_project_root` | `/mnt/data/bench-eval/service/gateway`，复用原服务 |
| `remote_python` | `/home/lixiang/miniconda3/envs/tvg/bin/python` |
| `cc_project` | 与Linux一致的 `bench-gateway` |
| `desktop_bridge_generation` | 从本包非敏感配置核对现有部署代次；Linux代次更新后必须重新核对 |
| `desktop_cc_binary` | 指向本包 `tools/cc-connect/v1.5.1-beta.1-desktop-return/cc-connect.exe` 的新机绝对路径 |
| `desktop_menu_native_adapter` | 保留true；须存在 `tools/desktop_gateway_adapter.exe` |
| `desktop_ssh_config`、`desktop_ssh_identity_file`、`desktop_ssh_known_hosts` | 指向新机自己的配置、公钥对应私钥和已核验主机记录 |
| `codex_binary` | 本机可用CLI，优先 `codex`，不要沿用旧机绝对路径 |

建立新机自己的SSH密钥，将**公钥**追加登记到原工作站账号，不替换整个authorized_keys。主机密钥通过可信渠道与本包`windows/config/workstation-public.json`中的配置结构准备自己的主机记录（公开副本不附带原主机密钥）并比对；不自动信任ssh-keyscan输出，不设置StrictHostKeyChecking=no。不要在聊天发送密码、私钥或Token。

可审阅后使用单步工具`python -B tools/windows_client_setup.py ssh --root <ClientRoot>`：创建新机专用密钥、独立known_hosts及workstation-ssh.conf，写入配置的ssh_config字段，不覆盖系统SSH。模板不预填desktop_ssh_*，避免无效占位字段覆盖该工具生成的ssh_config。只有选择手工配置时才填desktop_ssh_*或原ssh_config，两种方式择一并实测。

```powershell
& "$env:WINDIR\System32\OpenSSH\ssh.exe" -F <新机SSH配置> -o BatchMode=yes -o StrictHostKeyChecking=yes -o UpdateHostKeys=no ubuntu-workstation "hostname"
```

通过条件是实际SSH成功、Linux路径和程序可读、旧业务服务仍正常。网络/VPN、公钥未登记或主机身份变化，应停在具体问题，不重装整套项目。

## 3. 两套Windows后台任务

| 任务 | 注册脚本 | 固定动作 |
|---|---|---|
| Codex解析客户端 `BenchGateway-<根目录摘要>` | `tools/register_windows_host.ps1` | pythonw `-B tools/windows_service_host.py` |
| 飞书连接端 `BenchDesktopBridge-<根目录摘要>-v2` | `register_desktop_frontend.ps1` | pythonw `-B desktop_frontend.py` |

摘要由各脚本按新机绝对根目录计算，不照搬旧任务名。它们以当前用户、Limited/Interactive注册，登录触发及每分钟恢复、IgnoreNew避免重复实例；首次ai-client注册后保持禁用。

```powershell
& <Python> -B <ClientRoot>\tools\windows_client_setup.py install --root <ClientRoot>
& <获准PowerShell> -File <ClientRoot>\register_desktop_frontend.ps1 -ProjectRoot <ClientRoot> -PythonwPath <Pythonw>
```

解析端的install是单步工具：核对现有Linux/登录、调用注册脚本，再写入本机`runtime/service/windows-task.json`登记；不启动任务、不停止Linux。仅直接运行register_windows_host.ps1不会生成这份程序登记，不能把任务存在当成完整安装。手工安装必须按client_setup.install和windows_host.manifest核对同样的登记字段，或优先使用上述单步install。

先阅读脚本、核对同名任务动作，不覆盖未知任务。脚本被策略限制时按批准途径逐项注册，不放宽全局策略。**这是登录后自动启动，不是无需登录的开机系统服务**；无人登录、睡眠或断网期间不能保证飞书在线。该边界必须向使用方说明。

## 4. 有序切换

1. 在旧电脑确认解析无在途请求、附件无在途接收；保存旧程序和静态配置。对已经在Linux执行的台架任务只读观察。
2. **先禁用旧飞书连接端的周期触发，再正常停止该连接端**，等待程序确认子CC进程结束并释放本机租约。结果未确认时不启动新CC，不以PID消失/超时手工删租约。
3. 停止并禁用旧解析客户端。`tools/windows_client_setup.py stop`只负责旧解析客户端，**不能代替步骤2**。
4. 在新机注册的两套任务中，先启动解析端并核对Linux broker已识别新客户端，再启用/启动新连接端。实际任务名取注册输出，不猜。
5. 新连接端通过SSH向`desktop_connector_lease.py`申请现有generation的唯一租约；App Secret仅经受控管道进入新CC进程环境，不复制到新机配置文件。
6. 确认新CC长连接就绪、Linux连接观察器就绪、原材料和任务未变，旧机两个任务均保持禁用。

如果旧机不可访问或旧租约结果未知，须核实旧连接确实结束再接管，不能通过手工删保护状态“抢入口”。

## 5. 验收

- 检查新机两个任务的固定动作、路径、当前用户、触发器、启用状态及运行结果。
- 解析端状态为CONNECTED不等于模型调用成功；用现有受限解析往返验收，不保存业务提示词到Windows。
- 连接端`runtime/desktop-frontend/status.json`显示CONNECTED；Linux`deployment status`的bridge观察器和worker均ready。
- 用户真实点击“当前配置”，应直接出现本人材料卡，不代发消息。补一份材料时只更新卡片，不自动执行。
- 真实台架测试另选授权任务，由用户点击“确认执行”；不伪造/重放回调，不把迁移验证当成回放授权。
- 在合适窗口验证退出恢复、Windows重启并登录后的恢复；未做真实重启必须写“待验证”。

## 6. 回退

先禁用并停止新连接端和解析端，核实连接与租约结束；再恢复旧电脑原任务。只恢复程序和静态配置，不回滚Linux数据库，不清台架锁，不重跑已经完成的任务或帧。新机部署完成后暂保留旧机备份，防止未验收就失去回退入口。
