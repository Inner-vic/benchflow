"""Private queue visibility: personnel, public references and FIFO order only."""
import html
import json
import time
from .availability import description
from .core import owner_key


class QueueStatus:
    def __init__(self,store):self.store=store

    def resource(self,value=None):
        with self.store.connect() as c:
            if value is not None:
                # Dispatch eligibility is not proof of a measured physical connection.
                if value.get('state')=='READY' and not (value.get('probe_kind')=='ssh-readonly-v1' and value.get('workstation_ssh')=='PASS' and value.get('bench_ssh')=='PASS'):
                    value={**value,'state':'UNKNOWN'}
                c.execute('INSERT INTO resource_status VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',(json.dumps(value),))
                return value
            row=c.execute('SELECT data FROM resource_status WHERE id=1').fetchone()
        return json.loads(row['data']) if row else {'state':'UNKNOWN','checked_at':0}

    def snapshot(self,lane='hardware'):
        with self.store.connect() as c:
            rows=[dict(r) for r in c.execute("SELECT id,owner,session,state,json_extract(payload,'$.action') AS action FROM jobs WHERE lane=? AND state IN ('QUEUED','RUNNING','NEEDS_REVIEW') ORDER BY created,id",(lane,))]
        return rows

    def roster(self,user,jid=None):
        """A display projection, never a second queue or a source of scheduling state."""
        self.store.require_user(user)
        rows=self.snapshot()
        if jid and not any(r['id']==jid and r['owner']==user for r in rows):return []
        with self.store.connect() as c:
            names={r['id']:r['label'] for r in c.execute('SELECT id,label FROM users')}
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            refs={r['job_id']:r['reference'] for r in c.execute('''SELECT t.job_id,r.reference FROM task_requests t
                JOIN task_references r ON r.task_id=t.id AND r.owner=t.owner WHERE t.job_id IS NOT NULL''')} if {'task_requests','task_references'}<=tables else {}
        waiting=0;result=[]
        for row in sorted(rows,key=lambda r:{'RUNNING':0,'NEEDS_REVIEW':1,'QUEUED':2}[r['state']]):
            if row['state']=='QUEUED':waiting+=1
            name=names.get(row['owner'],'')
            if not name or name==row['owner']:name='同事·'+owner_key(row['owner'])[2:8]
            result.append({'person':row['owner'],'name':name,'state':row['state'],
                           'position':waiting if row['state']=='QUEUED' else None,
                           'reference':refs.get(row['id'],''),'self':row['owner']==user,
                           'current':row['id']==jid})
        return result

    def roster_text(self,user,jid=None,*,card=False):
        rows=self.roster(user,jid)
        if not rows:return ''
        if card:
            # Task cards only need the current occupant; the caller supplies
            # its own waiting-ahead count. Keep full FIFO details on demand.
            if jid and any(r['current'] and r['state']!='QUEUED' for r in rows):return ''
            lines=[]
            for row in rows:
                if row['state'] not in ('RUNNING','NEEDS_REVIEW'):continue
                person="<person id='"+row['person']+"' show_name=true show_avatar=false></person>"
                if row['self']:person+='（你）'
                lines.append(('当前使用：' if row['state']=='RUNNING' else '待核验：')+person)
            resource=self.resource();stamp=resource.get('checked_at')
            if (not lines and resource.get('state')=='RESOURCE_BUSY' and type(stamp) in (int,float)
                    and -30<=time.time()-stamp<=60):
                lines.append('当前使用：其他入口任务（人员未登记）')
            return '\n'.join(lines)
        # Keep the original status card compact. Show the first eight waiting
        # jobs plus this task if it falls beyond that window; never renumber it.
        waiting=[r for r in rows if r['state']=='QUEUED']
        visible=[r for r in rows if r['state']!='QUEUED' or r['position']<=8 or r['current']]
        if jid and not waiting and all(r['state']=='RUNNING' for r in rows):return ''
        lines=['**台架使用队列**' if card else '台架使用队列：']
        resource=self.resource()
        if (not any(r['state'] in ('RUNNING','NEEDS_REVIEW') for r in rows)
                and resource.get('state')=='RESOURCE_BUSY' and time.time()-resource.get('checked_at',0)<=60):
            lines.append('当前占用：其他入口任务（人员未登记）')
        for row in visible:
            # Native inline personnel shows the real Feishu name without a
            # contact lookup, new permission, mention notification or chat UI.
            person=("<person id='"+row['person']+"' show_name=true show_avatar=false></person>" if card
                    else html.escape(str(row['name']).replace('\n',' ').replace('\r',' ')[:40]))
            if row['self']:person+='（你）'
            state={'RUNNING':'执行中','NEEDS_REVIEW':'待核验','QUEUED':'等待 '+str(row['position'])}[row['state']]
            lines.append(state+'：'+person+(' · '+row['reference'] if row['reference'] else ''))
        hidden=len(rows)-len(visible)
        if hidden:lines.append('另有 '+str(hidden)+' 项等待任务未展开，共 '+str(len(waiting))+' 项等待。')
        return '\n'.join(lines)

    def text(self,user,jid=None,resource=None):
        self.store.require_user(user)
        rows=self.snapshot();waiting=[r for r in rows if r['state']=='QUEUED']
        running=sum(r['state']=='RUNNING' for r in rows)
        resource=self.resource() if resource is None else resource;state=resource['state']
        if time.time()-resource.get('checked_at',0)>60:state='UNKNOWN'
        text=f'全局队列：{running} 个执行中，{len(waiting)} 个就绪等待。\n'+description(state)
        if any(r['state']=='NEEDS_REVIEW' for r in rows):text+='\n'+description('NEEDS_REVIEW')
        elif running:text+='\n'+description('RUNNING')
        for i,row in enumerate(waiting):
            if row['owner']==user and (jid is None or row['id']==jid):
                from .task_history import job_reference
                ref=job_reference(self.store,user,row['id']) or row['id']
                text+=f"\n任务 {ref}：就绪，等待队列第 {i+1} 位，前面 {i} 个等待任务。无需重复提交；可取消等待。"
                text+=(f'\n前方共 {i+running} 个任务（含正在执行的任务）；轮到后执行RoadModel板端评测。' if row.get('action')=='roadmodel_hardware' else f'\n前方共 {i+running} 个任务（含正在执行的任务）；轮到后自动先做小场景，通过后回放本次全部选定场景。')
        roster=self.roster_text(user,jid)
        if roster:text+='\n'+roster
        evaluations=self.snapshot('postprocess')
        if evaluations:
            text+=f"\n独立评测队列：{sum(r['state']=='RUNNING' for r in evaluations)} 个执行中，{sum(r['state']=='QUEUED' for r in evaluations)} 个等待，{sum(r['state']=='NEEDS_REVIEW' for r in evaluations)} 个待核验；不占用台架队列。"
            for i,row in enumerate(r for r in evaluations if r['state']=='QUEUED'):
                if row['owner']==user and (jid is None or row['id']==jid):
                    from .task_history import job_reference
                    text+=f"\n评测任务 {job_reference(self.store,user,row['id']) or row['id']}：等待评测第 {i+1} 位。"
            for row in evaluations:
                if row['owner']==user and row['state']=='NEEDS_REVIEW' and (jid is None or row['id']==jid):
                    from .task_history import job_reference
                    text+=f"\n评测任务 {job_reference(self.store,user,row['id']) or row['id']}：结果待核验，保留原回放目录；只续收原结果，不自动重跑。"
        return text

    def notice(self,job,text):
        from .pcm_test1_ingress import is_pcm
        if is_pcm(job['owner']):return
        if job.get('action')=='roadmodel_hardware' or job.get('payload',{}).get('action')=='roadmodel_hardware':return
        with self.store.connect() as c:
            c.execute('''INSERT INTO queue_outbox(job,session,text,version,sent,attempts) VALUES(?,?,?,1,0,0)
                ON CONFLICT(job) DO UPDATE SET text=excluded.text,version=queue_outbox.version+1,sent=0,attempts=0,next_attempt_at=0
                WHERE queue_outbox.text!=excluded.text''',(job['id'],job['session'],text))

    def publish(self):
        for row in self.snapshot():
            if row['state']=='QUEUED':self.notice(row,self.text(row['owner'],row['id']))
        for row in self.snapshot('postprocess'):
            if row['state']=='QUEUED':self.notice(row,self.text(row['owner'],row['id']))
            elif row['state']=='RUNNING':
                from .task_history import job_reference
                self.notice(row,f"轮到评测任务 {job_reference(self.store,row['owner'],row['id']) or row['id']}，已开始离线评测；不占用台架。原回放结果保留，无需重复提交。")

    def head(self):
        rows=self.snapshot()
        if any(r['state'] in ('RUNNING','NEEDS_REVIEW') for r in rows):return None
        return next((r for r in rows if r['state']=='QUEUED'),None)
