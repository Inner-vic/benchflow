# 图示与维护

整体架构沿用带图标的卡片风格：飞书作为上方统一入口，个人电脑、Ubuntu 工作站、专用台架与评测环境保留四张独立卡片。画布为亮色 1600 × 900（16:9），控制、产品回流和独立评测使用不同线型/颜色。

执行时序使用原生 Mermaid `sequenceDiagram`，不再维护手绘时序 SVG，也不强制原生图表的宽高比。可从[时序说明](execution-sequence.md)复制代码到飞书文档的 Mermaid 图表，或下载[原始 .mmd](execution-sequence.mmd)。`README.md` 与当前系统说明中的代码块来自同一份源文件。

运行 `python tools/render_diagrams.py`，由 `render_current_diagrams.py` 更新图标架构 SVG、Mermaid 文档代码块及离线[查看页](index.html)。查看页提供架构预览与 SVG 下载、时序源码复制与 .mmd 下载，不自行绘制时序图。

## 图标来源

| 图标 | 来源 |
| --- | --- |
| OpenAI / Codex、Ubuntu | 沿用仓库已保存的 Simple Icons 素材（[OpenAI](https://github.com/simple-icons/simple-icons/blob/15.0.0/icons/openai.svg)、[Ubuntu](https://github.com/simple-icons/simple-icons/blob/15.0.0/icons/ubuntu.svg)） |
| 台架、评测环境 | 沿用本仓库绘制的通用 SVG |
| 飞书统一入口 | 本地通用消息图标，位于 `images/icons/feishu.svg` |

图标保存在 `images/icons/` 并内嵌到架构图，离线不依赖外部图片服务。Simple Icons 素材沿用其 [CC0 声明](https://github.com/simple-icons/simple-icons/blob/15.0.0/LICENSE.md)，商标权仍属各自权利人；图标仅用于识别角色。旧 GIF 和 Claude 图标保留为历史素材。
