"""Render self-contained architecture SVGs and refresh the HTML diagram.

Run from any directory with Python 3.12. No device or network access is used.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ROOT / "docs/images"
COLORS = ["#9eb5ff", "#E95420", "#71c7ff", "#c5afff"]


def icon(name, x, y, size=36):
    source = (IMAGES / "icons" / f"{name}.svg").read_text(encoding="utf-8")
    body = re.search(r"<svg[^>]*>(.*)</svg>", source, re.S).group(1)
    body = re.sub(r"<title>.*?</title>", "", body)
    return f'<g aria-hidden="true" transform="translate({x} {y}) scale({size/24})">{body}</g>'


def text(x, y, value, size=16, color="#a5b6cd", weight="400", **attrs):
    extra = " ".join(f'{k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}" {extra}>{value}</text>'


def start(width, height, title, desc):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
<title id="title">{title}</title><desc id="desc">{desc}</desc>
<defs><linearGradient id="bg" x2="1" y2="1"><stop stop-color="#101b30"/><stop offset="1" stop-color="#0a101e"/></linearGradient>
<marker id="blue" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0L8 4L0 8" fill="#71c7ff"/></marker>
<marker id="green" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0L8 4L0 8" fill="#6be2b6"/></marker>
<marker id="purple" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0L8 4L0 8" fill="#c5afff"/></marker></defs>
<rect width="{width}" height="{height}" rx="20" fill="url(#bg)"/>
<g font-family="Segoe UI,Microsoft YaHei,sans-serif">'''


def architecture():
    parts = [start(1200, 590, "BenchFlow：可验证台架实验编排", "GPT 或 Claude 助手通过执行 Skill 提交声明式任务，Ubuntu 工作站 Runner 控制台架并保存权威预测，按需交付开发空间评测。图标表示环境角色，不表示指定模型、云服务或官方合作。")]
    parts += [text(44, 64, "BenchFlow", 42, "#edf3ff", "700"), text(44, 102, "可验证台架实验编排 · 从目标意图到可信证据", 21, "#a5b6cd")]
    parts += ['<rect x="924" y="36" width="232" height="36" rx="18" fill="#1c2c43" stroke="#34445f"/>', text(1040, 59, "ARCHITECTURE REFERENCE", 12, "#a5b6cd", "500", text_anchor="middle")]
    cards = [
        (44, "01  INTENT", "个人 AI 助手", "GPT / Claude", ["执行 Skill · 声明目标与输入", "确认身份、来源与交付要求", "将意图交给受控工具执行"], "个人身份与认证会话"),
        (334, "02  ORCHESTRATION", "Ubuntu 工作站", "Runner", ["依赖规划 · 队列 · 独占锁", "确定性执行与事务恢复核验", "保存预测权威产品与收据"], "共享产品与持久状态"),
        (624, "03  EXECUTION", "专用台架", "Hardware bench", ["被测 APP · 传感器回灌", "候选激活 · 批量场景执行", "正式服务恢复与状态核验"], "稀缺硬件 · 受控副作用"),
        (914, "04  EVALUATION", "开发 / 评测空间", "Compute workspace", ["预测副本 + 匹配真值", "隔离环境 · 独立评测尝试", "样本有效性与覆盖检查"], "个人空间 · 按需使用"),
    ]
    for n, (x, label, title, sub, lines, footer) in enumerate(cards):
        parts += [text(x, 166, label, 12, COLORS[n], "600"), f'<rect x="{x}" y="184" width="240" height="242" rx="16" fill="#151f31" stroke="#34445f"/>']
        if n == 0:
            parts += [icon("openai", x+22, 207, 34), icon("claude", x+78, 207, 34)]
        else:
            parts += [icon(["", "ubuntu", "bench", "devspace"][n], x+22, 204, 40)]
        parts += [text(x+22, 283, title, 23, "#edf3ff", "700"), text(x+22, 308, sub, 13, COLORS[n])]
        for j, line in enumerate(lines):
            parts.append(text(x+22, 341+j*26, line, 15))
        parts += [f'<path d="M{x+22} 400H{x+218}" stroke="#34445f"/>', text(x+22, 417, footer, 12)]
    parts += ['<g fill="none" stroke="#71c7ff" stroke-width="2" marker-end="url(#blue)"><path d="M288 272H329"/><path d="M578 272H619"/></g>', text(308, 257, "Job", 12, "#71c7ff", text_anchor="middle"), text(598, 257, "控制", 12, "#71c7ff", text_anchor="middle")]
    parts += ['<path d="M744 430V464H454V431" fill="none" stroke="#6be2b6" stroke-width="2" marker-end="url(#green)"/>', text(599, 490, "预测回收 → 内容身份校验", 14, "#6be2b6", text_anchor="middle")]
    parts += ['<path d="M574 219H598V132H1034V179" fill="none" stroke="#c5afff" stroke-width="2" stroke-dasharray="6 5" marker-end="url(#purple)"/>', text(828, 123, "已验证产品 → 按需发布副本", 13, "#c5afff", text_anchor="middle")]
    parts += [text(44, 537, "控制流", 13, "#71c7ff"), text(126, 537, "产品与证据回流", 13, "#6be2b6"), text(286, 537, "下游交付", 13, "#c5afff"), text(44, 566, "助手表达意图；Runner 保证约束；台架产生结果；评测空间判断结果是否有效。", 16)]
    (IMAGES / "architecture.svg").write_text("\n".join(parts)+"\n</g></svg>\n", encoding="utf-8")


def lifecycle():
    p = [start(1200, 360, "回灌事务与故障恢复边界", "锁内进行候选激活、批量回灌和正式服务恢复核验，随后收集产品并发布评测。恢复失败保留台架锁，下游失败保留工作站产品。")]
    p += [text(38, 50, "一次回灌，分阶段验证与交付", 27, "#edf3ff", "700"), text(38, 80, "TRANSACTION → VERIFIED PRODUCT → INDEPENDENT ATTEMPT", 12, "#a5b6cd")]
    p += ['<rect x="221" y="99" width="526" height="123" rx="14" fill="#172b3a" stroke="#71c7ff"/>', text(242, 125, "独占台架锁 · 恢复或正式激活核验后才释放", 16, "#71c7ff")]
    labels = [(35, 156, "冻结输入 / 预检"), (245, 135, "候选激活"), (410, 135, "批量回灌"), (575, 147, "服务恢复核验"), (784, 160, "收集 / 校验产品"), (992, 174, "发布 / 评测报告")]
    for i, (x, width, value) in enumerate(labels):
        p += [f'<rect x="{x}" y="148" width="{width}" height="52" rx="8" fill="#151f31" stroke="#34445f"/>', text(x+width/2, 181, value, 17, "#edf3ff", text_anchor="middle")]
        if i<5:
            p.append(text((x+width+labels[i+1][0])/2, 181, "→", 22, "#71c7ff", text_anchor="middle"))
    p += [text(38, 269, "设备边界", 15, "#71c7ff", "600"), text(140, 269, "台架事务失败保留证据；恢复核验失败保留锁，由负责人处理。", 17), text(38, 303, "产品边界", 15, "#6be2b6", "600"), text(140, 303, "下游交付或评测失败保留权威产品，从对应阶段继续处理。", 17), text(38, 337, "工程一致性与算法精度分别验收；算法结论仍依赖匹配真值与有效样本。", 14)]
    (IMAGES / "lifecycle.svg").write_text("\n".join(p)+"\n</g></svg>\n", encoding="utf-8")


def html_diagram():
    p = ['<!-- BEGIN GENERATED DIAGRAM -->', '<svg viewBox="0 0 1080 340" role="img" aria-labelledby="diagram-title diagram-desc">', '<title id="diagram-title">BenchFlow：个人助手、Ubuntu 工作站、台架与开发空间</title><desc id="diagram-desc">助手声明任务，Runner 规划与执行，台架生成预测，产品回到工作站校验后按需发布开发空间。</desc>', '<defs><marker id="arrow" markerWidth="9" markerHeight="9" refX="7" refY="4" orient="auto"><path d="M0 0L8 4L0 8" fill="none" stroke="#71c7ff"/></marker><marker id="arrow-product" markerWidth="9" markerHeight="9" refX="7" refY="4" orient="auto"><path d="M0 0L8 4L0 8" fill="none" stroke="#6be2b6"/></marker><marker id="arrow-delivery" markerWidth="9" markerHeight="9" refX="7" refY="4" orient="auto"><path d="M0 0L8 4L0 8" fill="none" stroke="#c5afff"/></marker></defs>']
    p += ['<path id="intent" class="edge" d="M235 155H297" marker-end="url(#arrow)"/><path id="control" class="edge" d="M555 155H609" marker-end="url(#arrow)"/>', '<path id="collect" class="edge product-edge" d="M710 252V285H425V252" marker-end="url(#arrow-product)"/><path id="publish" class="edge delivery-edge" d="M555 83H580V14H939V45" marker-end="url(#arrow-delivery)"/>']
    cards = [("pc",20,215,"个人 AI 助手","GPT / Claude · 执行 Skill",["目标与输入声明","个人身份与认证会话"]),("ws",300,255,"Ubuntu 工作站","Runner · 确定性执行",["依赖规划 · 队列 · 台架锁","权威产品 · 持久状态与收据"]),("bench",612,185,"专用台架","Hardware bench",["候选激活 · 场景回灌","基线 · 服务恢复核验"]),("cloud",817,245,"开发 / 评测空间","Compute workspace",["预测副本 + 匹配真值","独立尝试 · 有效报告"])]
    for i,(ident,x,w,title,sub,lines) in enumerate(cards):
        p += [f'<rect id="{ident}" class="node" x="{x}" y="50" width="{w}" height="200" rx="14"/>']
        if i==0:p += [icon("openai",x+20,69,30),icon("claude",x+70,69,30)]
        else:p.append(icon(["","ubuntu","bench","devspace"][i],x+20,66,36))
        p += [f'<text x="{x+20}" y="136" class="label">{title}</text>',f'<text x="{x+20}" y="161" class="sub" style="fill:{COLORS[i]}">{sub}</text>']
        for j,line in enumerate(lines):p.append(f'<text x="{x+20}" y="{202+j*27}" class="sub">{line}</text>')
    p += ['<text x="250" y="330" class="sub">控制流 / 产品回流 / 下游交付 · 共享生产，个人评测。</text>', '<circle id="packet" r="5" fill="var(--blue)" cx="0" cy="0" visibility="hidden"/>', '</svg>', '<!-- END GENERATED DIAGRAM -->']
    path=ROOT / "docs/index.html"
    page=path.read_text(encoding="utf-8")
    diagram="\n".join(p)
    if '<!-- BEGIN GENERATED DIAGRAM -->' in page:
        page=re.sub(r'<!-- BEGIN GENERATED DIAGRAM -->.*?<!-- END GENERATED DIAGRAM -->',lambda _:diagram,page,flags=re.S)
    else:page=re.sub(r'<svg viewBox=.*?</svg>',lambda _:diagram,page,count=1,flags=re.S)
    path.write_text(page,encoding="utf-8")


if __name__ == "__main__":
    architecture()
    lifecycle()
    html_diagram()
    print("Rendered architecture.svg, lifecycle.svg and the standalone HTML diagram")
