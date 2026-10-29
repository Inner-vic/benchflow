"""Render the icon architecture; keep sequence diagrams as native Mermaid text."""
from html import escape
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
INK,MUTED,BLUE,GREEN,PURPLE='#172b4d','#61718b','#3274cf','#268e7c','#8164bd'


def text(x,y,value,size=20,color=MUTED,weight=400,anchor='start'):
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}" text-anchor="{anchor}">{escape(value)}</text>'


def icon(name,x,y,size=44):
    source=(ROOT/'docs/images/icons'/f'{name}.svg').read_text(encoding='utf-8')
    body=re.search(r'<svg[^>]*>(.*)</svg>',source,re.S).group(1)
    body=re.sub(r'<title>.*?</title>','',body)
    if name=='openai':body=body.replace('#ffffff',INK)
    if name=='bench':body=body.replace('#71c7ff',BLUE)
    if name=='devspace':body=body.replace('#c5afff',PURPLE)
    return f'<g aria-hidden="true" data-icon="{name}" transform="translate({x} {y}) scale({size/24})">{body}</g>'


def edge(path,label,x,y,tone='blue'):
    color={'blue':BLUE,'green':GREEN,'purple':PURPLE}[tone]
    dash=' stroke-dasharray="7 6"' if tone!='blue' else ''
    return f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.3"{dash} marker-end="url(#{tone})"/>'+text(x,y,label,17,color,anchor='middle')


def architecture():
    b=text(64,76,'BenchFlow',42,INK,700)+text(64,118,'飞书统一入口 · 个人电脑编排 · 工作站执行 · 台架回放 · 独立评测',22)
    b+='<rect x="64" y="160" width="1472" height="120" rx="18" fill="#eef5ff" stroke="#cfdef4"/>'
    b+=icon('feishu',91,190,54)+text(166,212,'飞书',30,INK,650)+text(167,246,'统一业务入口',18,BLUE)
    b+=text(465,211,'材料卡与明确确认',24,INK,600)+text(465,246,'本人材料版本 · 任务状态 · 原卡结果与附件交付',20)
    b+=edge('M 224 280 V 362','确认材料',286,320)
    b+=edge('M 768 418 H 800 V 320 H 1376 V 362','预测副本 · 独立评测',1100,309,'purple')
    cards=[
        (64,'01  PERSONAL','个人电脑','Codex / CC Connect',['材料准备与冻结候选','本人确认后进入 FIFO','独立后处理与报告交付'],'会话与业务状态','openai',BLUE),
        (448,'02  ORCHESTRATION','共享工作站','Ubuntu / Runner',['Job / Profile · JSON 台架锁','回灌编排与正式服务恢复','保存预测产品与收据'],'执行证据与权威产品','ubuntu','#d86735'),
        (832,'03  EXECUTION','专用台架','APP / Sensor replay',['本次预检 → 全量回灌','流式 / 逐帧回放','正式服务恢复与核验'],'受控硬件与软件','bench',BLUE),
        (1216,'04  EVALUATION','评测计算环境','Compute workspace',['预测副本 + 本任务 GT','固定原生评测器','独立计算 · 原始 TXT'],'独立后处理','devspace',PURPLE),
    ]
    for x,label,title,sub,lines,footer,name,color in cards:
        b+=text(x,350,label,13,color,600)
        b+=f'<rect x="{x}" y="370" width="320" height="310" rx="18" fill="white" stroke="#dce4ef" stroke-width="1.4"/>'
        b+=icon(name,x+25,393,42)+text(x+25,471,title,27,INK,650)+text(x+25,502,sub,17,color)
        for i,line in enumerate(lines):b+=text(x+25,541+i*29,line,18)
        b+=f'<path d="M {x+25} 629 H {x+295}" stroke="#e1e8f2"/>'+text(x+25,656,footer,16)
    b+=edge('M 384 484 H 440','SSH',416,465)
    b+=edge('M 768 484 H 824','控制',800,465)
    b+=edge('M 992 680 V 718 H 608 V 688','预测与恢复收据',800,747,'green')
    b+=edge('M 1376 680 V 787 H 224 V 688','评测报告 → 个人电脑 → 飞书原卡',1100,815,'green')
    b+='<path d="M 64 844 H 1536" stroke="#dce4ef"/>'
    b+=text(64,879,'控制流',17,BLUE)+text(180,879,'产品与证据回流',17,GREEN)+text(375,879,'独立评测交付',17,PURPLE)
    b+=text(1536,879,'预检 / 全量分别恢复；评测不占台架锁。',18,MUTED,anchor='end')
    markers=''.join(f'<marker id="{name}" markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto"><path d="M 0 0 L 9 4.5 L 0 9" fill="{color}"/></marker>' for name,color in [('blue',BLUE),('green',GREEN),('purple',PURPLE)])
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900" viewBox="0 0 1600 900" role="img" aria-labelledby="title desc">
<title id="title">BenchFlow · 飞书统一入口与四类环境</title><desc id="desc">带图标的架构卡片。飞书作为统一入口，保留个人电脑、共享工作站、专用台架和评测计算环境。</desc>
<defs>{markers}</defs><rect width="1600" height="900" fill="#f7f9fc"/>
<g font-family="Segoe UI,Microsoft YaHei,sans-serif">{b}</g></svg>'''


def sequence_markdown(source):
    return '# 执行时序（Mermaid）\n\n复制下方 `sequenceDiagram` 到飞书文档的 Mermaid 图表中即可编辑和渲染。时序由 Mermaid 原生排版，不再维护手绘 SVG。\n\n```mermaid\n'+source.rstrip()+'\n```\n\n[原始 Mermaid 文件](execution-sequence.mmd) · [完整执行链路](current-system.md)\n'


def update_block(path,source):
    if not path.exists():return
    s=path.read_text(encoding='utf-8')
    block='<!-- BEGIN EXECUTION SEQUENCE -->\n```mermaid\n'+source.rstrip()+'\n```\n<!-- END EXECUTION SEQUENCE -->'
    s=re.sub(r'<!-- BEGIN EXECUTION SEQUENCE -->.*?<!-- END EXECUTION SEQUENCE -->',lambda _:block,s,flags=re.S)
    path.write_text(s,encoding='utf-8',newline='\n')


def viewer(arch,source):
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BenchFlow · 架构与 Mermaid 时序</title>
<style>:root{font-family:Segoe UI,Microsoft YaHei,sans-serif;color:#172b4d;background:#f7f9fc}*{box-sizing:border-box}body{margin:0}main{max-width:1440px;margin:auto;padding:30px 26px}header{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}h1{font-size:25px;margin:0}a{color:#3274cf;text-decoration:none}.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:22px 0 16px}button{font:inherit;padding:9px 17px;border:1px solid #d5dfed;background:white;color:#61718b;border-radius:8px;cursor:pointer}button[aria-selected="true"]{color:#245eaa;background:#e9f1fc;border-color:#a8c6ed}button:focus-visible{outline:3px solid #a8c6ed}.actions{margin-left:auto;display:flex;gap:8px}.canvas{border:1px solid #dce4ef;border-radius:14px;overflow:auto;background:white}section[hidden]{display:none}svg{display:block;width:100%;height:auto;min-width:900px}pre{margin:0;padding:28px;overflow:auto;font:15px/1.8 Consolas,monospace;background:white;color:#22364f}.caption{font-size:14px;line-height:1.7;color:#61718b}@media(max-width:650px){main{padding:22px 12px}h1{font-size:21px}.actions{margin-left:0}pre{padding:18px}}</style></head><body><main>
<header><h1>BenchFlow · 架构与执行时序</h1><a href="current-system.md">阅读完整说明 ↗</a></header>
<div class="toolbar"><div role="tablist" aria-label="图示"><button role="tab" id="architecture-tab" aria-controls="architecture" aria-selected="true">整体架构</button><button role="tab" id="sequence-tab" aria-controls="sequence" aria-selected="false">Mermaid 时序源码</button></div><div class="actions"><button id="copy" hidden>复制 Mermaid</button><button id="download">下载 SVG</button></div></div>
<div class="canvas"><section id="architecture" role="tabpanel" aria-labelledby="architecture-tab">'''+arch+'''</section><section id="sequence" role="tabpanel" aria-labelledby="sequence-tab" hidden><pre><code id="mermaid-source">'''+escape(source)+'''</code></pre></section></div>
<p id="caption" class="caption">沿用图标卡片风格，补充飞书统一入口；架构保持亮色 16:9。</p>
<script>let active='architecture';function show(id){active=id;for(const name of ['architecture','sequence']){document.getElementById(name).hidden=name!==id;document.getElementById(name+'-tab').setAttribute('aria-selected',String(name===id))}document.getElementById('copy').hidden=id!=='sequence';document.getElementById('download').textContent=id==='sequence'?'下载 .mmd':'下载 SVG';document.getElementById('caption').textContent=id==='sequence'?'复制源码到飞书文档的 Mermaid 图表；由原生 sequenceDiagram 渲染，不使用手绘 SVG。':'沿用图标卡片风格，补充飞书统一入口；架构保持亮色 16:9。'}for(const id of ['architecture','sequence'])document.getElementById(id+'-tab').onclick=()=>show(id);document.getElementById('copy').onclick=async()=>{const value=document.getElementById('mermaid-source').textContent;try{await navigator.clipboard.writeText(value);document.getElementById('copy').textContent='已复制'}catch{const selection=getSelection(),range=document.createRange();range.selectNodeContents(document.getElementById('mermaid-source'));selection.removeAllRanges();selection.addRange(range);document.getElementById('copy').textContent='源码已选中，请复制'}};document.getElementById('download').onclick=()=>{const isSeq=active==='sequence',value=isSeq?document.getElementById('mermaid-source').textContent:document.querySelector('#architecture svg').outerHTML,url=URL.createObjectURL(new Blob([value],{type:isSeq?'text/plain;charset=utf-8':'image/svg+xml'})),a=document.createElement('a');a.href=url;a.download=isSeq?'benchflow-sequence.mmd':'benchflow-architecture.svg';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};</script></main></body></html>'''


def render():
    arch=architecture();source=(ROOT/'docs/execution-sequence.mmd').read_text(encoding='utf-8')
    (ROOT/'docs/images/architecture.svg').write_text(arch,encoding='utf-8')
    (ROOT/'docs/execution-sequence.md').write_text(sequence_markdown(source),encoding='utf-8')
    for name in ['README.md','docs/current-system.md']:update_block(ROOT/name,source)
    (ROOT/'docs/index.html').write_text(viewer(arch,source),encoding='utf-8')


if __name__=='__main__':render()
