"""Per-user material drafts. Recording a request never installs, builds, or queues it."""
import json
import re
import time
from .core import GatewayError

FIELDS=('goal','software_name','build_reference','assembly_ref','package','camera_so','lidar_so','m100_so','m100_mode','dataset','scene_scope','yaml_request','lpai_target','replay_mode')
ASSEMBLY_ID=re.compile(r'asm-[a-f0-9]{24}\Z')
CLEAR_YAML='保留原配置'
LINE_BREAKS='\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029'
SUBMISSION_TEMPLATE='''补充任务材料
软件名称：软件包A
软件包：<APP 下载链接、本人登记名称或工作站文件路径>
相机SO：<本人 libxcamera.so 来源>
雷达SO：<本人 libxlidar.so 来源>
M100SO：尝试编译
数据集：<工作站绝对路径，或 /lpai/volumes/已接入数据卷/数据根目录>
场景范围：全部
任务目标：回放后评测
YAML修改：保留原配置
交付方式：工作站目录
构建来源：'''
ASSEMBLY_TEMPLATE='''补充任务材料
装配存档：<从“我的可复用软件”选择的精确 asm-id>
数据集：<工作站绝对路径，或 /lpai/volumes/已接入数据卷/数据根目录>
场景范围：全部
任务目标：回放后评测
YAML修改：保留原配置
交付方式：工作站目录'''
DETAILED_GUIDE='''这是台架评测入口，默认回放后评测，交付工作站目录与原始 TXT；明确“仅回放”时跳过评测。
直接发送软件包链接，或私聊上传相机/雷达/M100 SO 和 YAML 即可，不需要先说“新建任务”。提交前补充当前材料；提交后修改则准备下一次回放，已提交材料保持不变。只改配置或SO时沿用本人确切原版本及其余材料；新APP先校验本人已有相机/雷达候选，清除旧YAML与M100。附件核验发送人后保存到本人工作站目录；未知SO角色或YAML目标会提示补充。本入口SO接收上限默认128 MiB、YAML为2 MiB，上游平台限制仍适用；大数据集不走附件。收到文件不代表运行兼容通过。
发送“我的任务”查看材料任务，发送“切换任务 TJ-日期-编号”继续其他草稿；正在执行的材料保持冻结。YAML只保存到本任务，不建立配置资产库。
材料会自动汇总到确认卡。请核对后点击“确认执行”，确认后才自动检查、绑定并进入执行队列；上传文件或保存表单只更新材料，不启动台架，也无需声明“材料发完了”。入队前补件使旧确认失效，需要再次确认；入队后材料冻结，新修改用于下一次任务。每次执行先进行本次小场景预检，通过后正式回放。“暂缓执行”保留未提交材料；恢复处理仍不能替代卡片确认。
详细操作：
1. 首次使用提供 APP 链接与相机、雷达 SO；M100可由工作站按已验收版本编译。后续只发变化的文件或修改要求即可，其余沿用本人当前版本。软件发版时提供新 APP 链接，系统先检查本人旧相机、雷达候选是否匹配，不匹配再补充。需要主动选择其他历史版本时，可发“我的可复用软件”。
2. 点击当前材料卡“确认执行”后，系统自动检查。工作站核验软件包、三份 SO、数据范围及 YAML，生成任务副本和差异，并绑定回放适配器。这一步不连接台架；缺材料不占执行队列。用“查看准备状态”查看检查结果。
3. 查看任务单：发送“查看任务单”，核对软件版本、三份 SO、YAML、场景、目标、交付方式和待处理项。静态检查或绑定通过仍不代表台架兼容性已验证；修改材料后需重新检查。
4. 本次确认仍有效且检查通过后进入统一队列。先做本次小场景预检，通过才正式回放所选场景；预检失败停止后续回放并返回错误线索。软件部署、预检、场景进度、问题和结果更新到原卡片；仅回放交付工作站目录，评测另交付原始 TXT，缺项会明确说明。

复用本人已验证装配（数据、范围、目标和 YAML 均按本次重新声明）：
'''+ASSEMBLY_TEMPLATE+'''

也可单独发送“复用装配 <asm-id>”先选择，再补本次数据和范围；选择会开始一份新任务，不继承旧 YAML、数据或评测目标，不会立即执行。

首次没有本人材料时，提供新软件包及 libxcamera.so、libxlidar.so；liburoad_m100.so 可提供配套成品，也可尝试系统编译。YAML不是必需项。可选完整模板：
'''+SUBMISSION_TEMPLATE+'''

模板字段规则：
- 相机、雷达、M100 三份 SO 分别固定替换 APP 内 lib/libxcamera.so、lib/libxlidar.so、lib/liburoad_m100.so，需与本次软件包匹配；不能互换角色或自行指定其他替换目标。
- 新包缺M100时优先匹配工作站固定编译配置；仅对已验收的精确APP和传感器组合自动构建。已有本人成果直接复用，失败或版本不匹配就停止并要求成品。提供M100成品时优先使用成品。复用有效装配时无需重复提供SO。
- 新APP提供下载链接或转发可读取的构建卡；已有本人软件可以直接引用。SO和YAML直接通过聊天附件上传，也支持本人登记来源或工作站路径。图片中的文件名不是下载地址。构建链接用于版本追踪；M100编译仍需已有匹配配方或准确源码、构建锁及环境，构建链接本身不能替代这些信息。APP集成Lockfile当前不单独接收。
- 数据集由本次任务指定工作站目录，或已接入数据卷内的 /lpai/volumes/... 数据根目录。云端入口只把所选场景的 original 回放输入直接下载到本人工作站目录，dataset 真值保留云端；没有全局默认数据源。评测按本次数据根关联GT；缺失或歧义会停止，不套用V151或其他真值。大数据集不经 CC Connect 中转。
- 材料卡“回放方式”可选流式回放或逐帧回放，默认沿用流式回放；确认时冻结本次选择，不能修改正在执行的回放方式。
- 任务目标只填“仅回放”或“回放后评测”。回放后评测交付原始 TXT；评测需先完成其接入条件。
- YAML修改只写一行，可填“保留原配置”，或写明文件、完整字段路径和新值，也可提供本人工作站替换文件路径及 APP 内目标。多项要求放在同一行；不接受模板字段续行。可另发自然语言详细描述。
- 交付方式只填“工作站目录”“团队LPAI”或“个人LPAI”。LPAI 授权和上传尚未接入，选择上传会列为待处理，不会声称已交付；工作站目录可作为当前交付方式。
- 软件名称和构建来源可省略；必填字段暂缺时请删去整行，空必填行会提示补齐，不能把尖括号说明当成真实来源。
- 装配存档只来自本人真实完整回放成功且恢复/产品证据通过后的归档；仅上传、原料登记、静态通过、小场景通过或模拟均不算。列表为空就提交新包原料，不伪造历史成功。
- 装配复用只省下载和依赖对齐；仍核验当前文件，重新绑定本次数据与配置，每个 Job 仍先小场景预检。旧任务 YAML 不作为可复用资产，本次基于原包默认配置生成；显式 YAML 及协议固定覆盖在任务单展示。
- 装配与新包来源不混填；已选择装配后可直接提供替换SO，系统取消旧成功资格并重新检查。新APP先尝试本人旧相机/雷达候选，不匹配再补充；旧YAML和M100不跨APP继承。本次数据、范围、目标和YAML可修改；数据路径必须明确，不使用全局默认数据集；同一材料的增量修改保留原范围。重复文件或配置无有效变化不重复派发，明确“重新回放”可按相同材料另跑一次。YAML不写回装配存档；是否评测按本次目标处理。
“查看材料”也可查看任务单；“评测环境”真实核验工作站评测工具；“取消 YAML 修改”保留原配置；“清空本次材料”重新开始。草稿不占队列，记录材料不等于已下载、编译或核验。'''

GUIDE='''台架评测使用说明
1. 首次提供 LIVC APP 下载链接或转发构建卡，并上传 libxcamera.so、libxlidar.so。liburoad_m100.so可提供配套成品，或尝试已支持版本的系统编译；YAML可选。
2. 后续只发变化的SO、YAML或配置字段要求，其余沿用本人材料。新APP需重新核验配套依赖。
3. 材料汇总到卡片后，点击“调整选项”核对场景和目标，再点击“确认执行”；确认后自动检查并排队。上传或保存材料不会启动台架。
提供明确的工作站数据路径，不默认选择数据集；默认回放后评测，只需回放可在卡片选择“仅回放”。评测使用该数据源明确关联的云端GT，大数据不走聊天。
执行进度、问题及结果在原卡更新，完成后交付工作站目录和评测TXT。
可直接发送材料，或发送“开始台架评测”查看本人材料卡。“M100编译说明”“详细说明”可查看补充要求。“评测数据说明”可查看云端数据卷、路径与接入状态。'''


def validate(values):
    if not isinstance(values,dict) or set(values)-set(FIELDS):raise GatewayError('材料字段无效')
    if any(not isinstance(v,str) or len(v)>4000 for v in values.values()):raise GatewayError('材料内容过长或格式无效')
    if values.get('assembly_ref') and not ASSEMBLY_ID.fullmatch(values['assembly_ref']):
        raise GatewayError('装配存档需为本人列表中的精确 asm-id；请先发送“我的可复用软件”选择，不接受路径或原始包名')
    software_name=values.get('software_name','')
    if software_name and (not software_name.strip() or len(software_name)>60 or any(c in software_name for c in '/\\'+LINE_BREAKS) or any(ord(c)<32 for c in software_name)):
        raise GatewayError('软件名称需为 1 至 60 个字符的普通名称，不能包含路径分隔符或换行')
    if any(c in values.get('build_reference','') for c in LINE_BREAKS) or any(ord(c)<32 for c in values.get('build_reference','')):
        raise GatewayError('构建来源只能是一行构建编号或链接，用于版本追踪')
    for key,allowed in {'goal':('','replay','replay_and_evaluate'),'m100_mode':('','provided','build_if_missing'),'lpai_target':('','none','team','personal'),'replay_mode':('','stream','frame_by_frame')}.items():
        if values.get(key,'') not in allowed:raise GatewayError('材料选项无效：'+key)
    return values


class Intake:
    def __init__(self,store):self.store=store

    def get(self,user):
        self.store.require_user(user)
        with self.store.connect() as c:row=c.execute('SELECT data FROM material_drafts WHERE owner=?',(user,)).fetchone()
        return json.loads(row['data']) if row else None

    def update(self,user,values=None,new=False,incoming_kind='',force_run=False,expected_task=None):
        self.store.require_user(user);values=validate(values or {})
        from .tasks import Tasks
        tasks=Tasks(self.store)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT data FROM material_drafts WHERE owner=?',(user,)).fetchone()
            previous=json.loads(row['data']) if row else {}
            focus=c.execute('SELECT t.* FROM task_focus f JOIN task_requests t ON t.id=f.task_id WHERE f.owner=? AND t.owner=?',(user,user)).fetchone()
            if expected_task and (not focus or focus['id']!=expected_task['id'] or focus['revision']!=expected_task['revision'] or focus['job_id']):
                raise GatewayError('材料在确认期间发生变化，请核对当前卡片后重新确认；本次未执行。')
            from .incremental import DEFAULTS,frozen_inputs,latest_private_base
            base=focus
            if not row and not new:
                base=latest_private_base(c,user)
                if base:previous=json.loads(base['data'])
            supplied=any(values.values()) or bool(incoming_kind)
            if (not new and not incoming_kind and not force_run and values and previous
                    and all(v==previous.get(k) or (k=='yaml_request' and v==CLEAR_YAML and not previous.get(k)) for k,v in values.items() if v)
                    and not (values.get('m100_mode')=='build_if_missing' and previous.get('m100_so'))):
                return self.summary(user)
            # The submitted request and its preparation are immutable, including failures.
            # Follow-up edits start one draft from the same owner's material snapshot.
            rollover=bool(focus and focus['job_id'] and supplied) or force_run
            release=bool(values.get('package') and previous.get('package') and values['package']!=previous['package'])
            inherited=frozen_inputs(c,user,base,previous)
            data=dict(inherited) if previous and not new and not release else dict(DEFAULTS,dataset=getattr(self.store,'default_dataset_path',DEFAULTS['dataset']))
            if not new and release:
                # Sensor files are candidates, not a compatibility certificate for the new APP.
                data.update({k:inherited[k] for k in ('camera_so','lidar_so','dataset','scene_scope','goal','lpai_target','replay_mode') if k in inherited})
            # A replay-only choice applies to one request. Unsubmitted drafts retain
            # it; a new request defaults to evaluation unless this message says otherwise.
            fresh=new or rollover or release or (not focus and bool(base and base['job_id']))
            if fresh:data['goal']=DEFAULTS['goal']
            # Chat-only requirements belong to this request, not the user's
            # persistent software selection. An explicit repeat requests the
            # same snapshot; an ordinary next task starts without old overrides.
            reset_supplements=bool(getattr(self.store,'request_scoped_supplements',False) and fresh and not force_run)
            if reset_supplements:
                data['scene_scope']=DEFAULTS['scene_scope']
                data.pop('yaml_request',None)
            if (not new and focus and not focus['job_id'] and c.execute("SELECT 1 FROM sqlite_master WHERE name='automatic_tasks'").fetchone()
                    and c.execute("SELECT 1 FROM automatic_tasks WHERE task_id=? AND owner=? AND state='AWAITING_MATERIAL'",(focus['id'],user)).fetchone()):
                # A mode explicitly chosen for the upcoming material also applies
                # when that material is a new APP, rather than this old base.
                data['goal']=previous.get('goal',DEFAULTS['goal'])
            data.setdefault('goal',DEFAULTS['goal'])
            for key in ('dataset','scene_scope'):data.setdefault(key,DEFAULTS[key])
            dependency_change=incoming_kind=='so' or any(values.get(k) and values[k]!=previous.get(k) for k in ('camera_so','lidar_so','m100_so')) or values.get('m100_mode')=='build_if_missing'
            if dependency_change:data.pop('assembly_ref',None)
            data.update({k:v for k,v in values.items() if v})
            if (values.get('yaml_request') and values['yaml_request']!=CLEAR_YAML and inherited.get('yaml_request')
                    and not new and not release and not reset_supplements and inherited['yaml_request'] not in values['yaml_request']):
                data['yaml_request']=inherited['yaml_request']+'；后续修改（同一字段以此为准）：'+values['yaml_request']
                if len(data['yaml_request'])>4000:raise GatewayError('累计 YAML 要求过长，请提供完整配置文件或简化本次要求。')
            if (any(values.get(k) and values[k]!=previous.get(k) for k in ('camera_so','lidar_so'))
                    and inherited.get('m100_mode')=='build_if_missing' and not values.get('m100_so')):
                data.pop('m100_so',None);data['m100_mode']='build_if_missing'
            # Empty schema fields mean "not supplied". Clearing requires an explicit request.
            if values.get('yaml_request','').strip()==CLEAR_YAML:data.pop('yaml_request',None)
            if values.get('m100_so'):data['m100_mode']='provided'
            elif values.get('m100_mode')=='build_if_missing':data.pop('m100_so',None)
            if focus and focus['job_id'] and not supplied and not new:
                return self.summary(user)
            if focus and not focus['job_id'] and not new and not release and not force_run and data==previous:
                return self.summary(user)
            tid=tasks.sync(c,user,data,new=new or rollover or release)
            lineage=c.execute('SELECT * FROM task_lineage WHERE task_id=? AND owner=?',(base['id'],user)).fetchone() if base else None
            explicit=set(k for k,v in values.items() if v)
            if base and not new:
                prior={r['field'] for r in c.execute('SELECT field FROM task_explicit_fields WHERE task_id=? AND owner=?',(base['id'],user))}
                explicit.update(prior if lineage else previous.keys())
            c.executemany('INSERT OR IGNORE INTO task_explicit_fields VALUES(?,?,?)',[(tid,user,k) for k in explicit if k in data])
            base_task=base['id'] if base and base['job_id'] else (lineage['base_task_id'] if lineage else None)
            base_job=base['job_id'] if base and base['job_id'] else (lineage['base_job_id'] if lineage else None)
            kind='new_package' if release else 'repeat' if force_run else 'incremental' if base_job else 'initial'
            c.execute('INSERT OR IGNORE INTO task_lineage VALUES(?,?,?,?,?,?,?)',(tid,user,base_task,base_job,kind,int(force_run),time.time()))
            c.execute('INSERT INTO material_drafts VALUES(?,?,?) ON CONFLICT(owner) DO UPDATE SET data=excluded.data,updated=excluded.updated',(user,json.dumps(data,ensure_ascii=False),time.time()))
        return self.summary(user)

    def summary(self,user):
        data=self.get(user)
        if data is None:return GUIDE
        from .business import render_review
        return render_review(data)
