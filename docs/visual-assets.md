# 视觉素材与维护

2026-10-29：当前 `architecture.svg`、`lifecycle.svg` 与 `index.html` 已按飞书统一入口、FIFO、本次预检/全量及独立后处理重绘。运行 `python tools/render_diagrams.py` 会调用 `render_current_diagrams.py` 生成新版。旧 GIF 与下文品牌图标仅为历史素材，不是当前统一入口实机录屏。

BenchFlow 的图示采用统一的深色底、环境图标与三类流向：蓝色表示任务控制，绿色表示预测产品与证据回流，紫色表示下游发布。职责文字与图标同时保留，避免将平台标识误解为系统契约。

GPT / Claude 图标表示个人助手的可选工具，Ubuntu 图标表示工作站操作系统。开发空间采用通用云端终端图标，专用台架采用通用设备图标。品牌标识仅用于说明工具与环境，不表示品牌方对本项目的认可或合作，也不表示已验证所有模型或云平台的兼容性。

| 素材 | 来源与归属 |
| --- | --- |
| OpenAI / GPT 图标 | [Simple Icons 15.0.0](https://github.com/simple-icons/simple-icons/blob/15.0.0/icons/openai.svg)；标识归 OpenAI 所有，参考 [品牌规范](https://openai.com/brand/) |
| Claude 图标 | [Simple Icons 15.0.0](https://github.com/simple-icons/simple-icons/blob/15.0.0/icons/claude.svg)；标识归 Anthropic 所有，参见 [Claude 官方网站](https://claude.com/) |
| Ubuntu 图标 | [Simple Icons 15.0.0](https://github.com/simple-icons/simple-icons/blob/15.0.0/icons/ubuntu.svg)；商标归 Canonical 所有，参考 [Ubuntu 品牌规范](https://design.ubuntu.com/brand) |
| 台架、开发空间图标 | 本仓库为架构说明绘制的通用 SVG |

Simple Icons 素材按其 [CC0 声明](https://github.com/simple-icons/simple-icons/blob/15.0.0/LICENSE.md)提供；商标权及品牌使用规范仍由各品牌方保留。素材许可不改变本项目源码尚未声明许可证的状态。

历史图标保存在 `docs/images/icons/`。新版图示与 HTML 不依赖外部图片服务；下载 `docs/index.html` 后可离线查看。

修改图标或布局后，在仓库根目录运行 `python tools/render_diagrams.py`，即可更新架构图、事务图与 HTML 内嵌图示。GIF 是同一视觉体系的演示快照，不包含真实实验数据。发布前应检查文字裁切、色彩对比、流向语义与失败情境，并重新执行公开内容审查。
