"""Render two light 16:9 documentation figures and a compact offline viewer."""
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INK, MUTED, BLUE, GREEN = '#172b4d', '#61718b', '#3274cf', '#268e7c'


def text(x, y, value, size=22, color=INK, weight=400, anchor='start'):
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}" text-anchor="{anchor}">{escape(value)}</text>'


def frame(title, subtitle, body):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900" viewBox="0 0 1600 900" role="img" aria-labelledby="title desc">
<title id="title">{escape(title)}</title><desc id="desc">{escape(subtitle)}</desc>
<defs><marker id="blue" markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto"><path d="M0 0L9 4.5L0 9" fill="{BLUE}"/></marker><marker id="green" markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto"><path d="M0 0L9 4.5L0 9" fill="{GREEN}"/></marker></defs>
<rect width="1600" height="900" fill="#f7f9fc"/>
<g font-family="Segoe UI,Microsoft YaHei,sans-serif">
{text(72,76,title,36,INK,700)}{text(72,116,subtitle,21,MUTED)}
{body}</g></svg>'''


def card(x, y, width, title, subtitle, lines, accent=BLUE, height=200):
    return (f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="18" fill="white" stroke="#dce4ef" stroke-width="1.5"/>'
        + f'<rect x="{x+24}" y="{y+28}" width="5" height="30" rx="2.5" fill="{accent}"/>'
        + text(x+44,y+52,title,28,INK,650)
        + text(x+26,y+91,subtitle,19,accent)
        + ''.join(text(x+26,y+132+i*31,line,21,MUTED) for i,line in enumerate(lines)))


def edge(points, label='', x=0, y=0, returned=False):
    color, marker = (GREEN,'green') if returned else (BLUE,'blue')
    dash=' stroke-dasharray="7 6"' if returned else ''
    return (f'<path d="{points}" fill="none" stroke="{color}" stroke-width="2.3"{dash} marker-end="url(#{marker})"/>'
        + (text(x,y,label,19,color,anchor='middle') if label else ''))


def architecture():
    b = card(72,186,350,'飞书','统一业务入口',['材料卡 · 确认执行','原任务卡 · 结果交付'])
    b += card(528,186,390,'个人电脑','CC Connect + Codex + 网关',['材料准备 · 冻结候选','持久 FIFO · 独立后处理'])
    b += card(1128,186,400,'评测计算环境','云端 / 既有工作站后端',['本任务 GT · 固定评测器','独立评测 · 原始 TXT'],GREEN)
    b += card(528,526,390,'共享工作站','Runner · 执行与证据中心',['软件 / 数据准备 · 预测产品','JSON 台架锁 · 恢复收据'])
    b += card(1128,526,400,'专用台架','被测 APP · 传感器回放',['本次小场景预检 → 全量','流式 / 逐帧 · 正式服务恢复'])
    b += edge('M422 244H522','消息 / 确认',475,225)
    b += edge('M528 333H428','状态 / 附件',475,361,True)
    b += edge('M666 386V520','冻结任务 / SSH',772,463)
    b += edge('M918 598H1122','按需部署 / 回放',1024,577)
    b += edge('M1128 674H924','预测 / 恢复证据',1024,705,True)
    b += edge('M 918 548 H 1028 V 333 H 1122','预测副本',1100,449)
    b += edge('M1128 253H924','评测报告',1025,234,True)
    b += text(72,558,'职责分工',23,INK,650)
    for i,line in enumerate(['飞书统一交互与确认','个人电脑组织任务与交付','工作站持锁，台架执行']):
        b += text(72,605+i*36,line,22,MUTED)
    b += '<path d="M72 790H1528" stroke="#dce4ef"/>'
    b += edge('M75 833H121') + text(138,840,'任务控制 / 数据交付',19,MUTED)
    b += edge('M440 833H486',returned=True) + text(503,840,'结果与证据返回',19,MUTED)
    b += text(1528,840,'评测与到点后处理不占台架锁',20,MUTED,anchor='end')
    return frame('BenchFlow · 整体架构','飞书作为统一入口，连接个人电脑、共享工作站、专用台架与评测计算环境。',b)


def sequence():
    actors=[(160,'飞书','材料 / 任务卡'),(460,'个人电脑','网关 / FIFO'),(820,'共享工作站','Runner / 产品'),(1110,'专用台架','APP / 回放'),(1430,'评测环境','原生评测器')]
    b=''
    for x,title,sub in actors:
        b+=f'<rect x="{x-116}" y="152" width="232" height="78" rx="12" fill="white" stroke="#dce4ef"/>'
        b+=text(x,184,title,24,INK,650,'middle')+text(x,212,sub,17,MUTED,anchor='middle')
        b+=f'<path d="M{x} 232V796" stroke="#c9d4e4" stroke-width="1.4" stroke-dasharray="5 7"/>'
    # Very light bands distinguish two independent transactions, not one lock.
    for y,h in [(327,111),(492,112)]:
        b+=f'<rect x="772" y="{y}" width="390" height="{h}" rx="9" fill="#edf4fe" fill-opacity="0.85"/>'
    b+='<rect x="397" y="662" width="1150" height="106" rx="9" fill="#edf8f4"/>'
    b+=text(412,681,'可选：独立评测队列',16,GREEN)
    def msg(a,z,y,label,returned=False,label_x=None):
        return edge(f'M{a} {y}H{z}',label,(a+z)/2 if label_x is None else label_x,y-10,returned)
    b+=msg(160,460,268,'确认当前材料')
    b+=msg(460,820,314,'准备与冻结候选')
    b+=msg(820,1110,358,'按需部署 + 小场景预检')
    b+=msg(1110,820,402,'恢复核验 / 预检产品',True)
    b+=msg(820,460,450,'预检 PASS',True)
    b+=msg(460,820,492,'派发完整选定范围')
    b+=msg(820,1110,535,'全量回灌')
    b+=msg(1110,820,580,'恢复核验 / 预测产品',True)
    b+=msg(820,460,630,'结果目录与收据',True)
    b+=msg(460,1430,707,'签名派发 / 本任务 GT 关联')
    b+=msg(1430,460,751,'原始 TXT / 覆盖检查',True)
    b+=msg(460,160,793,'原卡交付结果',True)
    b+='<path d="M72 830H1528" stroke="#dce4ef"/>'
    b+=text(72,864,'预检与全量分别恢复；全量包含预检场景。',19,MUTED)
    b+=text(1528,864,'到点精度：交付后由原卡按钮另行触发。',19,MUTED,anchor='end')
    return frame('BenchFlow · 执行时序','确认 → 锁外准备 → 本次预检 → 全量回灌 → 独立后处理',b)


def viewer(arch, seq):
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BenchFlow · 架构与执行时序</title>
<style>:root{font-family:Segoe UI,Microsoft YaHei,sans-serif;color:#172b4d;background:#f7f9fc}*{box-sizing:border-box}body{margin:0}main{max-width:1440px;margin:auto;padding:32px 28px}header{display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap}h1{font-size:26px;margin:0}a{color:#3274cf;text-decoration:none}.toolbar{display:flex;gap:8px;align-items:center;margin:24px 0 18px;flex-wrap:wrap}button{font:inherit;padding:9px 18px;border:1px solid #d5dfed;background:white;color:#61718b;border-radius:8px;cursor:pointer}button[aria-selected="true"]{color:#245eaa;background:#e9f1fc;border-color:#a8c6ed}button:focus-visible{outline:3px solid #a8c6ed}.download{margin-left:auto}.canvas{border:1px solid #dce4ef;border-radius:14px;overflow:auto;background:#f7f9fc}figure{margin:0}figure[hidden]{display:none}svg{display:block;width:100%;height:auto;min-width:880px}.caption{margin:15px 0;color:#61718b;line-height:1.7;font-size:14px}@media(max-width:650px){main{padding:24px 14px}h1{font-size:22px}.download{margin-left:0}.caption{font-size:13px}}</style></head><body><main>
<header><h1>BenchFlow · 架构与执行时序</h1><a href="current-system.md">阅读完整说明 ↗</a></header>
<div class="toolbar" role="tablist" aria-label="图示"><button role="tab" id="architecture-tab" aria-controls="architecture" aria-selected="true">整体架构</button><button role="tab" id="sequence-tab" aria-controls="sequence" aria-selected="false">执行时序</button><button class="download" id="download">下载当前 SVG</button></div>
<div class="canvas"><figure id="architecture" role="tabpanel" aria-labelledby="architecture-tab">'''+arch+'''</figure><figure id="sequence" role="tabpanel" aria-labelledby="sequence-tab" hidden>'''+seq+'''</figure></div>
<p class="caption" id="caption">飞书是统一业务入口；原有四类环境保留各自职责。蓝色为任务与数据流，绿色虚线为结果与证据返回。</p>
<script>let active='architecture';const captions={architecture:'飞书是统一业务入口；原有四类环境保留各自职责。蓝色为任务与数据流，绿色虚线为结果与证据返回。',sequence:'预检与全量是两个独立 Runner 事务。评测独立排队；到点精度在回灌交付后从原任务卡按钮触发。'};function show(id){active=id;for(const name of ['architecture','sequence']){document.getElementById(name).hidden=name!==id;document.getElementById(name+'-tab').setAttribute('aria-selected',String(name===id))}document.getElementById('caption').textContent=captions[id]}for(const id of ['architecture','sequence']){document.getElementById(id+'-tab').onclick=()=>show(id)}document.getElementById('download').onclick=()=>{const svg=document.getElementById(active).querySelector('svg').outerHTML;const url=URL.createObjectURL(new Blob([svg],{type:'image/svg+xml'}));const a=document.createElement('a');a.href=url;a.download='benchflow-'+active+'.svg';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};</script></main></body></html>'''


def render():
    arch, seq = architecture(), sequence()
    (ROOT/'docs/images/architecture.svg').write_text(arch,encoding='utf-8')
    (ROOT/'docs/images/lifecycle.svg').write_text(seq,encoding='utf-8')
    (ROOT/'docs/index.html').write_text(viewer(arch,seq),encoding='utf-8')


if __name__ == '__main__':
    render()
