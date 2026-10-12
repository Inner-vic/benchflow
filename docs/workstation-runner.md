# 工作站 Runner：持久执行层，而非个人助手的配置副本

## 为什么部署在工作站

个人电脑承担交互与身份，台架承担被测软件运行。持续任务、共享资源和证据需要一个独立于个人会话的中心，Runner 因此部署到工作站。工作站保存任务、队列、设备锁、输入缓存与预测产品；AI 助手通过协议提交目标，不直接接管所有设备命令。

## 仓库包含什么

`automation/` 包含统一 CLI、状态管理、队列 worker、目标规划、安装与回灌事务、传输、评测、诊断、接口协议及离线测试。`install_runtime.py` 将这些源码安装成独立 release；设备实配、生产任务、认证状态与商业依赖不包含在公开包中。

本次只读核对了现有工作站的 53 个顶层 Python 文件，均有本地源码对应。5 个文件与本地存在差异，涉及离线构建路径、预测根目录门禁、外部分区暂存及测试调整。公开参考采用脱敏后的本地源码与配套测试，不声称是生产目录的逐字节镜像，也未修改工作站部署。

## 部署结构

```text
<runner-root>/
├── bin/bench-eval              # 稳定 CLI 入口
├── current -> releases/<id>/   # 当前执行源码
├── releases/<id>/automation/   # 独立 release 的代码
├── releases/<id>/install-manifest.json
├── config/                    # 环境实配，部署时提供
├── jobs/                      # 任务输入
├── runs/                      # 状态、事件、授权与阶段收据
├── locks/                     # 稀缺资源协调
└── cache/                     # 经校验的输入与产品
```

release 保存源码快照与逐文件哈希，安装清单关联来源。`current` 将稳定入口与具体源码目录分开，可通过受控切换选择 release。任务与数据目录不随源码替换而销毁。切换指针只是部署操作，不代表新 release 已通过全部实机验收。

## 独立安装示例

在一台用于验证的 Linux 主机上，使用独立根目录：

```bash
python3 automation/install_runtime.py --target-root "$HOME/bench-eval-reference"
```

默认仅安装源码和清单，不激活入口，也不连接台架。确认 release 后，可在同一独立根目录使用 `--activate` 建立 `current` 与 launcher；不应未经验证覆盖现有生产 Runner。

安装后的离线入口：

```bash
"$HOME/bench-eval-reference/bin/bench-eval" capabilities
"$HOME/bench-eval-reference/bin/bench-eval" audit
```

这两条命令需先在独立根目录完成激活。真实 Job/Profile、SSH、传感器回放和评测依赖仍需本环境配置。

## 调度与会话的取舍

任务提交、授权与 ready 队列状态是持久的，但不同 executor 的运行方式不同。协调冒烟等路径支持后台 worker；涉及个人计算空间认证的工作流使用提交者的前台 SSH agent 转发会话。持久队列不意味着所有任务都能在个人认证失效后继续。

共享 Linux 账号上的 `submitted_by` 用于逻辑归属与操作检查，不是强租户隔离。实际多人部署还需系统账户、文件权限与平台访问控制。
