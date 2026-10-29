# 功能与维护索引

更新时间：2026-10-29。

| 能力 | 公开范围 | 接入要求 |
| --- | --- | --- |
| Runner 与预测交付 | 规划、队列/锁、事务恢复、收据、预测校验和离线测试 | 当前设备 Profile、回放驱动、恢复基线 |
| 到点后处理 | CSV/XCP 适配、来源验证、编排、统计和 XLSX 源码 | 经审核外部核心、同场景数据、真实哈希及报表运行时 |
| 独立离线评测 | 能力与来源边界说明 | 环境自备评测器与 GT；内部实现默认全场景，Linux 执行 |
| 双 TXT 发版报告 | 能力与模板边界说明 | 两份结果、明确角色与版本、当前包链接、审核模板及发布身份 |
| PCM 双包 | 构建流程边界说明 | 同源码/依赖的 OFF/ON 组件、组织内构建和通知平台 |

后面三项的组织内实现、评测器、模板、构建/通知工具不分发。公开仓库没有通用原生评测器或生产构建服务。

## 离线验证

Python 3.12 下先安装 `automation/requirements-test.txt`，再执行 README 中的 Python 测试命令；Windows 可另运行 `powershell -NoProfile -File automation/test_arrival_stream_hook.ps1`。公开到点测试使用核心替身，不验证真实检测算法。公开扫描工具 `tools/audit_public_tree.py` 检查当前文件和可达 Git 历史。

2026-10-29 在 Windows/Python 3.12 上验证公开副本：230 项自动化、22 项执行脚本、34 项 SSH 接入、5 项打包测试全部通过，另有 7 条 PowerShell 模拟编排路径通过。未连接真实设备或重新计算实机精度。

真实到点运行还需 NumPy/SciPy、openpyxl 和 LibreOffice，以及当前输入、核心版本和同步口径的独立验收。仅生成报告或离线测试通过不足以证明算法达标。

历史本地配置入口 `automation/run_arrival_accuracy.py` 使用 `render_arrival_workbook.mjs`，另需 Node 和可解析 `@oai/artifact-tool` 的显式运行时目录；日常工作站入口使用 openpyxl/LibreOffice。两种渲染入口的运行依赖不能互换。
