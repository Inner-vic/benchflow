"""Feishu attachments become owner-scoped workstation inputs, never model-selected paths."""
import hashlib
import json
from pathlib import Path,PurePosixPath
import re
import time
import uuid
from .core import GatewayError,digest
from .tasks import Tasks
from .feishu_files import FeishuFiles

TRAILER='\n\n(Files saved locally, please read them: '
ROLES={'camera':('camera_so','lib/libxcamera.so'),'lidar':('lidar_so','lib/libxlidar.so'),
       'm100':('m100_so','lib/liburoad_m100.so')}
YAMLS={'common_param.yaml':'etc/uroad/config/common_param.yaml',
       'postprocess_param.yaml':'etc/uroad/config/postprocess_param.yaml'}


def role_for(name):
    for pattern,role in ((r'(?:lib)?xcamera(?:\(\d+\))?\.so','camera'),
                         (r'(?:lib)?xlidar(?:\(\d+\))?\.so','lidar'),
                         (r'liburoad_m100(?:\(\d+\))?\.so','m100')):
        if re.fullmatch(pattern,name,re.I):return role
    return 'yaml' if name.lower().endswith(('.yaml','.yml')) else ''


class Attachments:
    def __init__(self,cfg,store,registry,client=None,caption_handler=None):
        self.cfg,self.store,self.registry=cfg,store,registry
        self.client=client or FeishuFiles(cfg);self.tasks=Tasks(store)
        self.caption_handler=caption_handler
        with store.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS task_attachments(
              id TEXT PRIMARY KEY,owner TEXT NOT NULL,task_id TEXT NOT NULL,message_id TEXT NOT NULL,
              filename TEXT NOT NULL,data TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,
              UNIQUE(owner,message_id,filename))''')

    def provenance(self,user,session,raw):
        source=Path(raw)
        base=Path(self.cfg['project_root'])/'.cc-connect/attachments'
        try:relative=source.relative_to(base)
        except ValueError:raise GatewayError('附件路径不属于本入口接收目录') from None
        if len(relative.parts)!=2 or not re.fullmatch(r'om_[A-Za-z0-9_-]{5,128}',relative.parts[0]):raise GatewayError('附件缺少独立的飞书消息身份')
        for p in (source,*source.parents):
            if p.is_symlink() or getattr(p,'is_junction',lambda:False)():raise GatewayError('附件路径不能经过链接或目录联接')
        if not source.is_file() or source.resolve().parent.parent!=base.resolve():raise GatewayError('附件未完整保存到本机')
        message=self.client.message(relative.parts[0]);body=json.loads(message.get('body',{}).get('content','{}'))
        if (message.get('message_id')!=relative.parts[0] or message.get('msg_type')!='file'
                or message.get('sender',{}).get('id')!=user or message.get('sender',{}).get('sender_type')!='user'
                or message.get('chat_id')!=session.split(':')[1] or body.get('file_name')!=source.name):
            raise GatewayError('附件原消息、发送人或私聊归属不匹配，未读取或转存文件')
        if not body.get('file_key'):raise GatewayError('附件原消息没有文件资源')
        return source,message

    def receive(self,user,session,raw,*,role_override='',target_override=''):
        started=time.monotonic();source,message=self.provenance(user,session,raw)
        size=source.stat().st_size;maximum=int(self.cfg.get('attachment_max_bytes',128*1024*1024))
        if not 0<size<=maximum:raise GatewayError(f'本入口附件上限为 {maximum//1024**2} MiB；大文件请提供工作站目录或下载链接。')
        name=source.name
        known=role_for(name)
        if role_override and known and role_override!=known:raise GatewayError('附件名称与指定角色不一致，未关联到任务。')
        selected_role=role_override or known;selected_target=target_override or YAMLS.get(name,'')
        if not name.lower().endswith(('.so','.yaml','.yml')):raise GatewayError('附件入口目前只接收 SO、YAML；APP 请提供链接，数据集使用工作站目录。')
        with self.store.connect() as c:
            old=c.execute('SELECT * FROM task_attachments WHERE owner=? AND message_id=? AND filename=?',(user,message['message_id'],name)).fetchone()
        if old:return self.summary(dict(old))
        local=self.store.user_root(user)/'inbox'/message['message_id']/name
        local.parent.mkdir(parents=True,exist_ok=True)
        h=hashlib.sha256();before=source.stat()
        if not local.exists():
            try:
                with source.open('rb') as inp,local.open('xb') as out:
                    while True:
                        chunk=inp.read(1024*1024)
                        if not chunk:break
                        h.update(chunk);out.write(chunk)
            except BaseException:
                local.unlink(missing_ok=True);raise
        else:
            with local.open('rb') as inp:
                for chunk in iter(lambda:inp.read(1024*1024),b''):h.update(chunk)
            if h.hexdigest()!=digest(source):raise GatewayError('本机暂存附件与飞书接收副本不一致')
        after=source.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns) or local.stat().st_size!=size:
            raise GatewayError('附件在接收时发生变化，尚未传往工作站')
        if name.lower().endswith('.so'):
            with local.open('rb') as stream:header=stream.read(20)
            if len(header)<20 or header[:4]!=b'\x7fELF' or header[4:6]!=b'\x02\x01' or int.from_bytes(header[18:20],'little')!=183:
                raise GatewayError('SO 不是目标台架使用的 AArch64 ELF64 文件，请提供匹配成品。')
        elif size>2*1024*1024:raise GatewayError('YAML 超过 2 MiB，请核对是否上传了配置文本')
        else:
            try:local.read_text(encoding='utf-8-sig')
            except UnicodeError:raise GatewayError('YAML 必须为 UTF-8 文本') from None
        duplicate=self.duplicate(user,h.hexdigest(),name,selected_role,selected_target)
        if duplicate:
            task=self.tasks.current(user);data=json.loads(duplicate['data']);aid='att-'+uuid.uuid4().hex[:16];now=time.time()
            data.update(local_path=str(local),duplicate_of=duplicate['id'])
            with self.store.connect() as c:c.execute('INSERT INTO task_attachments VALUES(?,?,?,?,?,?,?,?,?)',
                (aid,user,task['id'],message['message_id'],name,json.dumps(data,ensure_ascii=False),'DUPLICATE',now,now))
            return '已接收 '+name+'，内容与当前材料一致，未创建重复变更。'
        from .intake import Intake
        previous=self.tasks.current(user)
        Intake(self.store).update(user,{},incoming_kind='so' if name.lower().endswith('.so') else 'yaml')
        task=self.tasks.current(user)
        aid='att-'+uuid.uuid4().hex[:16];role=selected_role
        data={'sha256':h.hexdigest(),'size':size,'local_path':str(local),'role':role,
              'target':selected_target,'received_seconds':round(time.monotonic()-started,3),
              'next_request':bool(previous and previous['job_id'] and task['id']!=previous['id'])}
        now=time.time()
        with self.store.connect() as c:
            c.execute('INSERT INTO task_attachments VALUES(?,?,?,?,?,?,?,?,?)',
                (aid,user,task['id'],message['message_id'],name,json.dumps(data,ensure_ascii=False),'RECEIVED',now,now))
        from .progress import notify_input
        notify_input(self.store,user,session,aid,f"{name} 已收到，正在保存到你的工作站目录。")
        try:
            transfer=time.monotonic()
            remote=self.registry.ssh.upload(self.store,{'owner':user,'kind':'tasks/'+task['id']+'/inputs','data':data},trusted_source=local)
            data.update(remote_path=remote,upload_seconds=round(time.monotonic()-transfer,3))
            with self.store.connect() as c:c.execute("UPDATE task_attachments SET data=?,state='STORED',updated=? WHERE id=? AND owner=?",(json.dumps(data,ensure_ascii=False),time.time(),aid,user))
            return self.associate(user,aid,role,data['target'])
        except Exception:
            with self.store.connect() as c:
                c.execute("UPDATE task_attachments SET state='FAILED',updated=? WHERE id=? AND owner=?",(time.time(),aid,user))
                c.execute('UPDATE input_outbox SET sent=2 WHERE owner=? AND event_key=? AND sent=0',(user,aid))
            raise GatewayError('附件已在本人本机目录保留，但工作站保存或任务关联未完成。可发送“重试附件 '+aid+'”，不会刷包或执行台架。') from None

    def duplicate(self,user,content_hash,name,role,target):
        task=self.tasks.current(user)
        if not task or not role:return None
        selected_matches=False
        selected_duplicate=None
        with self.store.connect() as c:
            rows=c.execute("SELECT * FROM task_attachments WHERE owner=? AND state IN ('ASSOCIATED','DUPLICATE') AND json_extract(data,'$.sha256')=? ORDER BY created DESC",(user,content_hash)).fetchall()
            if role in ROLES:
                field,expected=ROLES[role]
                selected=c.execute("SELECT id,data FROM assets WHERE id=? AND owner=? AND kind='dependency'",(task['data'].get(field,''),user)).fetchone()
                if selected:
                    current=json.loads(selected['data'])
                    selected_matches=current.get('sha256')==content_hash and current.get('target')==expected
                    if selected_matches:
                        selected_duplicate={'id':selected['id'],'data':json.dumps(dict(current,role=role,target=expected,asset_id=selected['id'],duplicate_basis='selected_asset'),ensure_ascii=False)}
        matches=[]
        for row in rows:
            data=json.loads(row['data'])
            if role!=data.get('role'):continue
            if role in ROLES and data.get('target')==ROLES[role][1] and (selected_matches or task['data'].get(ROLES[role][0])==data.get('asset_id')):matches.append(dict(row))
            elif role=='yaml' and (not target or target==data.get('target')) and (target or name==row['filename']):
                operation='用工作站文件 '+data.get('remote_path','')+' 替换 APP 内 '+data.get('target','')
                if operation in task['data'].get('yaml_request',''):matches.append(dict(row))
        targets={json.loads(row['data']).get('target') for row in matches}
        if matches and len(targets)==1:return matches[0]
        return selected_duplicate

    def row(self,user,aid):
        self.store.require_user(user)
        with self.store.connect() as c:row=c.execute('SELECT * FROM task_attachments WHERE owner=? AND id=?',(user,aid)).fetchone()
        if not row:raise GatewayError('本人没有这个附件')
        return dict(row)

    def associate(self,user,aid,role='',target=''):
        row=self.row(user,aid);data=json.loads(row['data'])
        if self.tasks.get(user,row['task_id'])['job_id']:
            raise GatewayError('这份附件所属任务已提交，原材料保持不变。请重新发送文件，系统会自动用于下一次回放。')
        role=role or data.get('role','');target=target or data.get('target','')
        if row['state']=='ASSOCIATED' and role!=data.get('role'):raise GatewayError('该附件已经关联到确定的角色，不能同时作为另一种依赖。请核对并提供正确文件。')
        if role in ROLES:
            field,target=ROLES[role]
            if not row['filename'].lower().endswith('.so'):raise GatewayError('只有 SO 附件可以关联到依赖角色')
            data.update(role=role,target=target)
            asset=self.registry.record(user,'dependency',row['filename'],
                {'remote_path':data['remote_path'],'sha256':data['sha256'],'size':data['size'],'filename':row['filename'],'target':target})
            self.tasks.update(user,row['task_id'],{field:asset['id']})
            data['asset_id']=asset['id'];state='ASSOCIATED'
        elif role=='yaml' and target:
            if not row['filename'].lower().endswith(('.yaml','.yml')):raise GatewayError('只有 YAML 附件可以关联配置替换目标')
            p=PurePosixPath(target)
            if p.is_absolute() or '..' in p.parts or '\\' in target or str(p)!=target or not target.endswith(('.yaml','.yml')):
                raise GatewayError('YAML 目标必须为 APP 内相对 YAML 路径，不能包含 ..')
            task=self.tasks.get(user,row['task_id']);prior=task['data'].get('yaml_request','')
            with self.store.connect() as c:
                previous=c.execute("SELECT data FROM task_attachments WHERE owner=? AND task_id=? AND id!=? AND state='ASSOCIATED' AND json_extract(data,'$.role')='yaml' AND json_extract(data,'$.target')=?",(user,row['task_id'],aid,target)).fetchall()
            old_operations={'用工作站文件 '+json.loads(x['data'])['remote_path']+' 替换 APP 内 '+target for x in previous}
            prior='；'.join(part for part in prior.split('；') if part not in old_operations and not (part.startswith('用工作站文件 ') and part.endswith(' 替换 APP 内 '+target)))
            operation='用工作站文件 '+data['remote_path']+' 替换 APP 内 '+target
            self.tasks.update(user,row['task_id'],{'yaml_request':prior if operation in prior else (prior+'；' if prior else '')+operation})
            data.update(role=role,target=target);state='ASSOCIATED'
        else:state='NEEDS_TARGET' if role=='yaml' else 'NEEDS_ROLE'
        with self.store.connect() as c:
            if state=='ASSOCIATED':
                c.execute("UPDATE task_attachments SET state='SUPERSEDED',updated=? WHERE owner=? AND task_id=? AND id!=? AND state='ASSOCIATED' AND json_extract(data,'$.target')=?",(time.time(),user,row['task_id'],aid,target))
            c.execute('UPDATE task_attachments SET state=?,data=?,updated=? WHERE owner=? AND id=?',(state,json.dumps(data,ensure_ascii=False),time.time(),user,aid))
            c.execute('UPDATE input_outbox SET sent=2 WHERE owner=? AND event_key=? AND sent=0',(user,aid))
        return self.summary(self.row(user,aid))

    def retry(self,user,aid):
        row=self.row(user,aid);data=json.loads(row['data'])
        if not data.get('remote_path'):
            local=Path(data['local_path'])
            if digest(local)!=data['sha256']:raise GatewayError('本机保留的附件发生变化，请重新发送')
            data['remote_path']=self.registry.ssh.upload(self.store,{'owner':user,'kind':'tasks/'+row['task_id']+'/inputs','data':data},trusted_source=local)
            with self.store.connect() as c:c.execute('UPDATE task_attachments SET data=? WHERE owner=? AND id=?',(json.dumps(data,ensure_ascii=False),user,aid))
        answer=self.associate(user,aid)
        claim='files:'+re.sub(r'\(\d+\)(?=\.)','',row['filename'].lower())
        with self.store.connect() as c:
            c.execute("UPDATE input_batches SET state='RESOLVED',updated=? WHERE owner=? AND state='ABORTED' AND claim=?",(time.time(),user,claim))
        return answer

    def summary(self,row):
        data=json.loads(row['data']);prefix='已收到 '+row['filename']+'。\n'
        if data.get('next_request'):prefix+='这份文件用于下一次回放，已提交任务的材料保持不变。\n'
        if row['state']=='ASSOCIATED':
            role={'camera':'相机 SO','lidar':'雷达 SO','m100':'M100 SO','yaml':'YAML'}[data['role']]
            detail=('本次执行时将替换 '+data['target']+'。\n') if data['role']=='yaml' else ('本次回放将使用这份'+role+'，兼容性还需检查。\n')
            return prefix+'已保存到本人工作站目录。'+detail+('' if self.cfg.get('auto_execute_on_complete') else '可以继续补充材料，或说“检查材料”。')
        if row['state']=='NEEDS_TARGET':return prefix+'已保存到你的工作站目录。它要替换 APP 内哪个 YAML？例如回复“替换 etc/uroad/config/custom.yaml”。'
        if row['state']=='NEEDS_ROLE':return prefix+'已保存到你的工作站目录。这份是相机、雷达还是 M100 的 SO？例如回复“这是雷达SO”。'
        if row['state']=='SUPERSEDED':return prefix+'已被本任务后续附件替代，原文件仍保留。当前使用版本请查看任务单。'
        if row['state']=='DUPLICATE':return prefix+'内容与当前材料一致，未创建重复变更。'
        return prefix+'保存尚未完成，可发送“重试附件 '+row['id']+'”。'

    def command(self,user,text):
        match=re.fullmatch(r'附件角色\s+(att-[a-f0-9]{16})\s+(相机SO|雷达SO|M100SO)',text.strip(),re.I)
        if match:return self.associate(user,match[1],{'相机so':'camera','雷达so':'lidar','m100so':'m100'}[match[2].lower()])
        match=re.fullmatch(r'YAML目标\s+(att-[a-f0-9]{16})\s+(\S+)',text.strip(),re.I)
        if match:return self.associate(user,match[1],'yaml',match[2])
        match=re.fullmatch(r'重试附件\s+(att-[a-f0-9]{16})',text.strip())
        if match:return self.retry(user,match[1])
        match=re.fullmatch(r'(?:替换(?:\s*APP\s*内)?|YAML目标|用来替换)\s*[:：]?\s*(etc/\S+\.ya?ml)',text.strip(),re.I)
        if match:
            task=self.tasks.current(user)
            with self.store.connect() as c:pending=c.execute("SELECT id FROM task_attachments WHERE owner=? AND task_id=? AND state='NEEDS_TARGET'",(user,task['id'] if task else '')).fetchall()
            if len(pending)==1:return self.associate(user,pending[0]['id'],'yaml',match[1])
            return '当前有 '+str(len(pending))+' 个 YAML 待确认替换位置。请用“YAML目标 <att编号> '+match[1]+'”明确选择。'
        role=self.caption_role(text)
        if role:
            task=self.tasks.current(user)
            with self.store.connect() as c:pending=c.execute("SELECT id FROM task_attachments WHERE owner=? AND task_id=? AND state='NEEDS_ROLE'",(user,task['id'] if task else '')).fetchall()
            if len(pending)==1:return self.associate(user,pending[0]['id'],role)
            return '当前任务有 '+str(len(pending))+' 个附件待指定角色。请使用“附件角色 <att编号> 相机SO（或雷达SO、M100SO）”明确选择。'
        return None

    @staticmethod
    def caption_role(text):
        match=re.fullmatch(r'(?:这是|这份是|作为)?(相机SO|雷达SO|M100SO|相机库|雷达库|M100库)[。！!]?',''.join(text.split()),re.I)
        if not match:return None
        return {'相机so':'camera','相机库':'camera','雷达so':'lidar','雷达库':'lidar','m100so':'m100','m100库':'m100'}[match[1].lower()]

    def prompt(self,user,session,text):
        if TRAILER not in text:return None
        message,paths=text.rsplit(TRAILER,1)
        # CC Connect inserts this transport placeholder for an attachment-only
        # message. It is not a user instruction and must not call the model.
        if message.strip()=='Please analyze the attached file(s).':message=''
        if not paths.endswith(')'):raise GatewayError('附件引用不完整')
        values=paths[:-1].split(', ')
        if len(values)>5:raise GatewayError('每次最多接收 5 份附件')
        from .incremental import hold_requested,explicit_check
        parts=re.split(r'[，,；;。\n]',message)
        roles={self.caption_role(part) for part in parts};roles.discard(None)
        if len(roles)>1:raise GatewayError('附件说明包含多个 SO 角色，请分别说明。')
        role=next(iter(roles),None)
        target=re.search(r'(?:替换(?:\s*APP\s*内)?|YAML目标|用来替换)\s*[:：]?\s*((?:etc|config)/[^\s，,；;。]+\.ya?ml)',message,re.I)
        if target and sum(Path(v).suffix.lower() in ('.yaml','.yml') for v in values)>1:
            raise GatewayError('多份 YAML 不能共用一个替换目标，请分别说明目标路径。')
        hold=hold_requested(message)
        check=explicit_check(message);caption_reply=''
        simple={'','YAML','yaml','配置文件','更新配置','配置更新','新配置','上传配置文件'}
        remaining=[part.strip() for part in parts if part.strip() not in simple and not self.caption_role(part)
                   and not hold_requested(part) and not re.fullmatch(r'(?:替换(?:\s*APP\s*内)?|YAML目标|用来替换)\s*[:：]?\s*(?:etc|config)/\S+\.ya?ml',part.strip(),re.I)]
        if remaining:
            if self.caption_handler:
                intent=self.caption_handler(message)
                check=check or intent.get('action')=='check_materials'
                hold=hold or intent.get('action') in ('help','pause_auto','evaluate','queue','status','cancel')
                if intent.get('action')=='help':caption_reply=intent.get('reply','')
            else:hold=True;caption_reply='附件说明尚未完整解析，请单独说明其余修改要求。'
        if hold:
            from .autoflow import AutoFlow
            AutoFlow(self.cfg,self.store).control(user,session,True)
        replies=[]
        for value in values:
            known=role_for(Path(value).name)
            if role and known and known!=role:raise GatewayError('附件名称与指定 SO 角色不一致，请核对文件；未关联到任务。')
            reply=self.receive(user,session,value,role_override=role or '',target_override=target[1] if target else '')
            replies.append(reply)
        if self.cfg.get('require_material_confirmation'):
            replies.append('材料已更新，检查和执行等待卡片“确认执行”。')
            if caption_reply:replies.append(caption_reply)
            return '\n\n'.join(replies)
        if hold:
            replies.append('自动提交已暂停，材料保留。')
            if explicit_check(message):
                from .preparation import PreparationQueue
                replies.append(PreparationQueue(self.cfg,self.store).submit(user,session))
        elif check:
            from .preparation import PreparationQueue
            replies.append(PreparationQueue(self.cfg,self.store).submit(user,session))
        if caption_reply:replies.append(caption_reply)
        return '\n\n'.join(replies)
