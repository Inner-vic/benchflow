"""One visible native form; only its explicit submit confirms an execution."""
import json
from .core import GatewayError


def schema(store):
    with store.connect() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS inline_forms(
          task_id TEXT PRIMARY KEY REFERENCES task_requests(id),owner TEXT NOT NULL,
          revision INTEGER NOT NULL,initial_values TEXT NOT NULL)''')


def values(cfg, store, task):
    from .card_ui import form_values
    from .material_summary import describe
    result = form_values(store, task)
    data = task['data']; source = data.get('dataset', '')
    yaml = data.get('yaml_request', '')
    result['yaml_request'] = yaml if len(yaml) <= 1000 else ''
    if source.startswith('/'): result['dataset'] = source
    else:
        summary = describe(store, task['owner'], task=task)
        path = summary.get('dataset_input') or summary.get('dataset_source', '')
        from .incremental import DEFAULTS
        if not path.startswith('/') and source == DEFAULTS['dataset']: path = cfg.get('default_dataset_path', '')
        result['dataset'] = path if path.startswith('/') else ''
    return result


def describe(cfg, store, task):
    schema(store); initial = values(cfg, store, task)
    with store.connect() as c:
        c.execute('INSERT OR IGNORE INTO inline_forms VALUES(?,?,?,?)',
                  (task['id'], task['owner'], task['revision'], json.dumps(initial, ensure_ascii=False)))
        row = c.execute('SELECT * FROM inline_forms WHERE task_id=? AND owner=?', (task['id'], task['owner'])).fetchone()
    return {'inline': True, 'form_revision': row['revision'], 'form_values': json.loads(row['initial_values']),
            'latest_values': initial, 'supplements': {k:task['data'].get(k,'') for k in ('scene_scope','yaml_request')}}


def reset(store, tid):
    schema(store)
    with store.connect() as c: c.execute('DELETE FROM inline_forms WHERE task_id=?', (tid,))


def render(value):
    from .card_layout import shell, md, column, group, library_panel
    from .card_ui import input_field
    from .task_cards import escaped
    ui = value['card_ui']; initial = ui['form_values']; latest = ui['latest_values']
    def field(name, title, placeholder='', required=False):
        result = input_field(name, title, placeholder)
        result.update(required=required, default_value=initial.get(name, ''), width='fill', input_type='text')
        return result
    def input_row(name, title, placeholder='', required=False):
        control = field(name, title, placeholder, required)
        # One full-width control: a fixed side caption squeezed the editable
        # text on phones, where the native counter also consumes input space.
        # Native top labels keep the same left edge without nested columns.
        control['label_position'] = 'top'
        return control
    app = input_row('app_source', 'LIVC APP', '完整下载链接或 CCU_LIVC_AVM-*.app.tar.gz 文件名', required=True)
    dataset = input_row('dataset', '数据集路径', '工作站绝对路径，或 /lpai/volumes/卷名/数据根目录', required=True)
    goal = {'tag': 'select_static', 'name': 'goal', 'width': '120px',
            'initial_option': initial.get('goal', 'replay_and_evaluate'),
            'options': [{'text': {'tag': 'plain_text', 'content': label}, 'value': v}
                        for label, v in [('回放后评测', 'replay_and_evaluate'), ('仅回放', 'replay')]]}
    display = value['material_display']
    file_elements = [library_panel(display['rows'])]
    lines = []
    for item in ui.get('files', []):
        if item['state']=='NEEDS_ROLE':lines.append(escaped(item['filename'],100)+'：请在聊天中说明是相机、雷达还是 M100 SO。')
        elif item['state']=='NEEDS_TARGET':lines.append(escaped(item['filename'],100)+'：请在聊天中说明 APP 内 YAML 替换路径，例如“替换 etc/uroad/config/custom.yaml”。')
        elif item['state']=='FAILED':lines.append(escaped(item['filename'],100)+'：保存失败，请重发附件。')
    if lines: file_elements.append(md('\n'.join(lines), text_size='notation'))
    file_elements.append(md("<font color='grey'>SO、YAML 通过聊天附件发送，接收后更新。</font>", text_size='notation'))
    files = group(file_elements)
    files['columns'][0]['vertical_spacing'] = '4px'
    files['element_id'] = 'bench_files'
    sources = []
    for key, title in [('app_source', '当前登记的 LIVC APP'), ('dataset', '当前登记的数据集路径')]:
        if latest.get(key) and latest.get(key) != initial.get(key): sources.append(title + '：\n' + escaped(latest[key], 4000))
    supplements = ui.get('supplements', {})
    extra = []
    scope = supplements.get('scene_scope', '')
    if scope and scope not in ('全部', 'all'): extra.append('场景范围：' + escaped(scope,1000))
    if supplements.get('yaml_request'): extra.append('配置修改：' + escaped(supplements['yaml_request'],4000))
    if extra: sources.append('**补充信息（仅本次有效）**\n' + '\n'.join(extra))
    source_block = md('\n'.join(sources) or '\u200b', text_size='notation', element_id='bench_sources')
    feedback = ui.get('feedback', '') if value.get('feedback_current') else ''
    if not feedback and value['state'] == 'NEEDS_INPUT': feedback = value.get('action', '')
    feedback_block = md(escaped(feedback or '确认后检查材料并进入队列', 1000), text_size='notation', element_id='bench_feedback')
    confirm = {'tag': 'button', 'name': 'bench_confirm_' + ui['nonce'] + '_' + str(ui['form_revision']),
               'form_action_type': 'submit', 'type': 'primary_filled', 'width':'100px',
               'behaviors': [{'type': 'callback', 'value': {'action': 'bench:confirm_materials',
                    'nonce': ui['nonce'], 'revision': ui['form_revision']}}],
               'text': {'tag': 'plain_text', 'content': '确认执行'}}
    if ui.get('confirmed') or value['state'] == 'CHECKING':
        confirm.update(disabled=True, disabled_tips={'tag': 'plain_text', 'content': '已确认，正在处理，请勿重复提交'})
    build = input_row('build_reference', '构建链接', '请输入 Jenkins 构建详情链接')
    mode={'tag':'select_static','name':'replay_mode','width':'120px','initial_option':initial.get('replay_mode','stream'),
          'options':[{'text':{'tag':'plain_text','content':label},'value':v} for label,v in [('流式回放','stream'),('逐帧回放','frame_by_frame')]]}
    def choice_row(title,control):
        return {'tag':'column_set','flex_mode':'none','horizontal_spacing':'8px','columns':[
            column([md(title,text_size='normal')],width='56px',vertical_align='center'),
            column([control],width='auto',vertical_align='center')]}
    # Each label stays beside its select. Native stretch keeps the three
    # groups on one desktop row and stacks complete groups on mobile.
    footer = {'tag': 'column_set', 'element_id':'bench_footer', 'flex_mode': 'stretch',
        'horizontal_spacing': '8px', 'columns': [
        column([choice_row('回放方式',mode)],weight=2,vertical_align='center'),
        column([choice_row('任务目标',goal)],weight=2,vertical_align='center'),
        column([confirm],weight=1,vertical_align='center',horizontal_align='right')]}
    fields = [app, build, dataset, files, source_block, feedback_block, footer]
    form = {'tag': 'form', 'name': 'bench_inline_form', 'element_id': 'bench_form', 'vertical_spacing': '8px', 'elements': fields}
    return shell(value, [form])


def submit(service, user, session, task, ui, data):
    from .intake import validate
    from .tasks import Tasks
    from .confirmations import confirm
    cfg, store = service.cfg, service.store
    schema(store)
    if task['job_id']: raise GatewayError('本次材料已提交，不会重复执行。')
    with store.connect() as c: row = c.execute('SELECT * FROM inline_forms WHERE task_id=? AND owner=?', (task['id'], user)).fetchone()
    if not row or type(data['value'].get('revision')) is not int or row['revision'] != data['value']['revision']:
        raise GatewayError('材料输入卡已更新，请使用最新卡片确认。')
    initial = json.loads(row['initial_values']); latest = values(cfg, store, task); form = data.get('form_value') or {}
    allowed = {'app_source', 'dataset', 'goal', 'build_reference','replay_mode'}
    if set(form) & {'scene_scope','yaml_request'}:
        raise GatewayError('补充要求已改为通过聊天设置，请重新打开“当前配置”核对后确认；未执行。')
    if set(form) - allowed: raise GatewayError('表单包含未知字段，本次未确认。')
    if not all(isinstance(form.get(k),str) and form[k].strip() for k in ('app_source','dataset')):
        raise GatewayError('表单未完整提交 LIVC APP 和数据路径，请在当前卡片填写后确认。')
    changes = {}
    for field, raw in form.items():
        if not isinstance(raw, str): raise GatewayError('表单内容类型无效。')
        if len(raw)>1000:raise GatewayError('输入内容超过1000字符，请缩短后重新确认；本次未执行。')
        v = raw.strip(); before = initial.get(field, '')
        if field=='app_source':
            from .app_sources import normalize
            v=normalize(cfg,v);before=normalize(cfg,before)
        if v == before: continue
        if latest.get(field, '') not in (before, v):
            raise GatewayError('聊天和输入框同时修改了同一项，请重新打开“当前配置”核对后确认；未执行。')
        if not v and field in ('app_source', 'dataset'): raise GatewayError('LIVC APP 和工作站数据集路径不可清空。')
        if v: changes['package' if field == 'app_source' else field] = v
    if changes.get('dataset') and not changes['dataset'].startswith('/'):
        raise GatewayError('数据集请填写工作站绝对路径，不使用场景数量或数据集名称。')
    changes = validate(changes)
    if changes:
        # Guard the exact revision checked above; unrelated attachments arriving
        # later invalidate confirmation through the existing intake boundary.
        service.update_inputs(user, changes, expected_task={'id': task['id'], 'revision': task['revision']})
    current = Tasks(store).current(user)
    confirm(cfg, store, user, session, current['id'], current['revision'], getattr(service, 'input_batch_token', None))
    return current
