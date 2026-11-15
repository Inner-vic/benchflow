"""Shared compact Card 2.0 layout; full sources are disclosed on demand."""
from urllib.parse import quote
from .wording import display_text

def md(text,**kwargs):return {'tag':'markdown','content':text,**kwargs}
def column(elements,weight=1,**kwargs):return {'tag':'column','width':'weighted','weight':weight,'elements':elements,**kwargs}
def group(elements):return {'tag':'column_set','flex_mode':'none','columns':[column(elements)],'horizontal_spacing':'8px'}
def library_line(rows):
    from .task_cards import escaped
    # Display-only indicators: attachments and checks own state, never a click.
    lines=[]
    for row in rows:
        state=row.get('indicator') or ('provided' if row['status']=='已提供' else 'error' if row['status'] in ('保存失败','校验失败','不匹配') else 'missing')
        glyph,color={'provided':('☑','green'),'missing':('☐','grey'),'error':('☒','red')}[state]
        label='保存失败' if row['status']=='保存失败' else '校验错误' if state=='error' else row['status']
        lines.append("<font color='"+color+"'>"+glyph+"</font> **"+escaped(row['name'],100)+'**\u00a0'+escaped(label,40))
    return ' · '.join(lines)
def library_panel(rows):
    from .task_cards import escaped
    return {'tag':'column_set','flex_mode':'stretch','horizontal_spacing':'8px','columns':[
        column([md(library_line([row]),text_size='bench_caption')]+
               ([md("<font color='red'>"+escaped(row['error'],160)+"</font>",text_size='notation')] if row.get('error') else [])) for row in rows]}
def terminology(value,text):
    text=display_text(text)
    rows=value.get('material_display',{}).get('rows',[])
    names=[r['name'] for r in rows[:3]] or ['libxcamera.so','libxlidar.so','liburoad_m100.so']
    for old,new in [('相机 SO',names[0]),('相机SO',names[0]),('雷达 SO',names[1]),('雷达SO',names[1]),('M100 SO',names[2]),('M100SO',names[2]),('M100 准备',names[2])]:
        text=text.replace(old,new)
    return text

def status(value,progress=None):
    from .task_cards import escaped
    content=progress if progress is not None else escaped(terminology(value,value.get('detail','')),220)
    elements=[md('**'+escaped(display_text(value['label']))+'**',text_size='bench_status')]
    if content:elements.append(md(content,text_size='bench_caption'))
    if value.get('queue_text'):elements.append(md(value['queue_text'],text_size='bench_caption'))
    if value.get('action'):
        prefix='待处理：' if value.get('state') in ('FAILED','NEEDS_REVIEW','PARTIAL_SUCCESS','NEEDS_INPUT') else ''
        elements.append(md(prefix+escaped(terminology(value,value['action']),320),text_size='notation'))
    return {'tag':'column_set','flex_mode':'none','columns':[column(elements,background_style='blue-50',padding='8px',vertical_spacing='4px')]}

def materials(value):
    from .task_cards import escaped
    from .material_summary import public_url
    d=value['material_display'];goal='仅评测' if value.get('evaluation_only') else '回放后评测' if value.get('evaluate_after') else '仅回放'
    mode='逐帧回放' if d.get('replay_mode')=='frame_by_frame' else '流式回放'
    overview=md('**'+escaped(d['software'],110)+'** · '+escaped(d['scope'],80)+' · '+goal+' · '+mode,text_size='bench_caption')
    url=public_url(d.get('software_url'))
    app='['+escaped(url,4000)+']('+quote(url,safe=':/%._~-')+')' if url else '已选择本人软件包，原始下载链接未记录' if d.get('software_provided') else '未提供下载链接'
    elements=[md('**LIVC APP**\n'+app,text_size='bench_caption'),overview,
              library_panel(d['rows']),
              md("<font color='grey'>数据集路径</font>\n"+escaped(d['dataset_path'],1200),text_size='bench_caption')]
    if d.get('yaml_requested'):elements.append(md('配置变更（可选）：'+escaped('、'.join(d['configs'][:2]) or '已登记修改要求',160),text_size='notation'))
    return group(elements)

def details(value):
    from .task_cards import escaped
    from .material_summary import public_url
    d=value['material_display'];lines=[]
    for name,key in [('构建链接','build_reference')]:
        url=public_url(d.get(key))
        if url:lines.append(name+'：['+escaped(url,4000)+']('+quote(url,safe=':/%._~-')+')')
    if d.get('changes') and d['changes']!='沿用当前材料':lines.append('本次变更：'+escaped(terminology(value,d['changes']),250))
    if len(d.get('configs',[]))>2:lines.append('配置变更（可选）：'+escaped('、'.join(d['configs']),1200))
    method=d.get('m100_method')
    lines.append('liburoad_m100.so：'+('按配套构建信息由工作站编译；可复用已核验的编译产物。' if method=='系统编译' else '使用本人已提供的文件。' if method=='已提供' else '尚未提供可用文件，可上传配套文件或选择系统编译。'))
    if value.get('input_phase') and value['state'] not in ('CANCELLED','TRANSFERRED'):lines.append('SO、YAML 通过聊天附件发送。材料只登记；点击“确认执行”后检查并排队。')
    folded={'tag':'collapsible_panel','expanded':False,'header':{'title':{'tag':'plain_text','content':'材料详情'}},'elements':[md('\n'.join(lines),text_size='notation')]}
    if not value.get('task'):return folded
    # Plain rich text wraps on mobile; inline code can force horizontal overflow.
    # Preserve the exact path, without inserting invisible wrapping characters.
    path=escaped(str(value['task']),4000)
    return group([md('**结果目录**\n'+path,text_size='bench_caption'),folded])

def feedback(value):
    ui=value.get('card_ui') or {};text=display_text(ui.get('feedback',''))
    if not text or text.startswith(('已确认本次材料','交互已更新')):return ''
    if value.get('input_phase'):
        return text if value.get('feedback_current') and value['state'] not in ('CHECKING','WAITING','TRANSFERRED','CANCELLED') else ''
    if value['state'] not in ('QUEUED','RUNNING','POSTPROCESSING'):return ''
    return text if ui.get('feedback_at',0)>=value.get('observed',0) else ''

def shell(value,elements):
    demo='【展示验证】' if value.get('demo') else ''
    return {'schema':'2.0','config':{'update_multi':True,'width_mode':'default','enable_forward':False,
            'style':{'text_size':{'bench_caption':{'default':'notation','pc':'notation','mobile':'normal'},
                                  'bench_status':{'default':'heading-3','pc':'heading-3','mobile':'heading-4'}}},
            'summary':{'content':demo+value['label']}},
            'header':{'title':{'tag':'plain_text','content':demo+'台架评测'},'template':'blue'},
            'body':{'elements':elements,'padding':'12px','vertical_spacing':'8px'}}
