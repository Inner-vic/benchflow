"""A narrow Codex exec JSONL facade consumed by CC Connect's stock Codex adapter."""
import json
import os
import sys
import uuid
import threading
import time

from .config import load
from .core import GatewayError
from .parser import parse,quick
from .service import Service,identity


def emit(value):
    from .wording import display_text
    if value.get('type')=='item.completed' and value.get('item',{}).get('type')=='agent_message':
        value={**value,'item':{**value['item'],'text':display_text(value['item']['text'])}}
    print(json.dumps(value,ensure_ascii=False),flush=True)


def main():
    if '--version' in sys.argv:
        print('codex-cli bench-gateway-adapter 0.1.0');return
    if 'app-server' in sys.argv:
        # Native runtime config queries are optional; never start an unrestricted agent.
        print('runtime config unavailable for gateway adapter',file=sys.stderr);return
    emit({'type':'thread.started','thread_id':'gw-session-'+uuid.uuid4().hex})
    batch=None
    try:
        cfg=load();service=Service(cfg)
        session=os.environ.get('CC_SESSION_KEY','')
        if os.environ.get('CC_PROJECT')!=cfg['cc_project']:raise GatewayError('缺少可信 CC Connect 项目上下文')
        user=identity(session)
        if cfg.get('provision_from_feishu',True):service.store.provision(user)
        else:service.store.require_user(user)
        text=sys.stdin.read().strip()
        preamble='Before answering, follow these project-level instructions for this cc-connect session.'
        marker='\n\n---\n\nUser message:\n'
        if text.startswith(preamble) and marker in text:text=text.split(marker,1)[1]
        if not text or len(text)>20000:raise GatewayError('消息为空或过长；文件请通过直接入库通道提供')
        from .maintenance import notice
        maintenance=notice(cfg)
        if maintenance:
            emit({'type':'item.completed','item':{'type':'agent_message','text':maintenance}})
            emit({'type':'turn.completed','usage':{'input_tokens':0,'cached_input_tokens':0,'output_tokens':0}})
            return
        from .menu_events import PREFIX as MENU_PREFIX,handle as handle_menu
        if text.startswith(MENU_PREFIX):
            answer=handle_menu(service,session,text)
            emit({'type':'item.completed','item':{'type':'agent_message','text':answer}})
            emit({'type':'turn.completed','usage':{'input_tokens':0,'cached_input_tokens':0,'output_tokens':0}})
            return
        # The CC adapter adds project/platform preambles on initial turns. The model only
        # receives this owner's catalog and history; it cannot select identities or paths.
        from .tasks import Tasks
        from .attachments import Attachments,TRAILER
        tasks=Tasks(service.store)
        before=tasks.current(user);received_at=time.time();intent=None
        from .card_ui import PREFIX,decode as decode_card,handle as handle_card
        card_event=decode_card(cfg,session,text) if text.startswith(PREFIX) else None
        from .build_messages import PREFIX as SOURCE_PREFIX,decode as decode_source,extract as extract_source,apply as apply_source
        source_event=None;source_files=''
        if text.startswith(SOURCE_PREFIX):
            source_packet=text
            if TRAILER in text:source_packet,source_files=text.rsplit(TRAILER,1)
            source_event=decode_source(cfg,session,source_packet)
        from .incremental import InputBatch,input_claim
        batch=InputBatch(service.store,user,'message' if source_event else input_claim(text,TRAILER))
        service.input_batch_token=batch.token
        def caption_handler(caption):
            from .service import validate_intent
            value=quick(caption)
            if value is None:value=parse(cfg,caption,service.context(user),service.store.history(user,session))
            value=validate_intent(value);inputs=dict(value.get('inputs') or {})
            if value.get('scenes'):inputs['scene_scope']=','.join(str(i) for i in value['scenes'])
            if value.get('evaluate_after'):inputs['goal']='replay_and_evaluate'
            if any(inputs.values()) or value['action']=='new_task':
                service.update_inputs(user,inputs,new=value['action']=='new_task')
            return value
        inbox=Attachments(cfg,service.store,service.registry,caption_handler=caption_handler)
        if source_event:
            if source_files:raise GatewayError('构建卡片与 SO、YAML 请分别发送：先单独转发构建卡，再发送文件附件。本条合并附件尚未关联，未提交执行。')
            values,issue=extract_source(source_event['content'],require_app=True)
            if issue:raise GatewayError(issue)
            answer=apply_source(service,user,values)
            if values:
                service.material_card=True
                intent={'action':'prepare','inputs':{}}
        else:
            answer=handle_card(service,user,session,card_event) if card_event else inbox.prompt(user,session,text)
            if not card_event and answer is not None:
                service.material_card=True
                attachment_task=tasks.current(user)
                if attachment_task and attachment_task['job_id']:
                    # A duplicate of an already submitted input creates no draft.
                    # A short visible receipt is necessary: terminal cards must
                    # not swallow it or revive obsolete preparation feedback.
                    service.material_card=False
                    answer=answer.split('\n',1)[0]+'\n已提交任务的材料未变更，未新增任务或重复执行。'
                elif cfg.get('require_material_confirmation'):
                    from .card_ui import feedback
                    if attachment_task:feedback(service.store,user,attachment_task['id'],answer.split('\n',1)[0])
        if answer is None:answer=inbox.command(user,text)
        if answer is None:answer=tasks.command(user,text)
        if answer is None and text.strip() in ('当前任务','查看当前任务'):answer=service.task_review(user)
        if answer is None:
            intent=quick(text)
            if intent is None:
                pending={'done':False};guard=threading.Lock();key='parse-wait:'+uuid.uuid4().hex
                def waiting():
                    with guard:
                        if pending['done']:return
                        from .progress import notify_input
                        notify_input(service.store,user,session,key,'正在整理这条材料要求，可能需要几十秒；尚未提交台架。完成后会直接回复，不需要重复发送。')
                timer=threading.Timer(8,waiting);timer.daemon=True;timer.start()
                try:intent=parse(cfg,text,service.context(user),service.store.history(user,session))
                finally:
                    with guard:pending['done']=True
                    timer.cancel()
                    from .progress import schema
                    schema(service.store)
                    with service.store.connect() as c:c.execute('UPDATE input_outbox SET sent=2 WHERE owner=? AND event_key=? AND sent=0',(user,key))
            answer=service.handle(session,text,intent)
        after=tasks.current(user)
        changed=bool(after and not after['job_id'] and after['updated']>=received_at and
                     (not before or after['id']!=before['id'] or after['revision']!=before['revision']))
        if cfg.get('auto_execute_on_complete') and not getattr(service,'defer_auto',False) and (changed or (intent and intent['action']=='check_materials')):
            from .autoflow import AutoFlow
            automatic=AutoFlow(cfg,service.store);automatic.arm(user,session,after)
        resolving=bool(source_event or intent and (intent['action'] in ('check_materials','resume_auto','repeat_replay') or intent['action']=='prepare' and any(intent.get('inputs',{}).values())))
        batch.finish(True,changed or resolving,resolve_all=resolving)
        if cfg.get('auto_execute_on_complete') and changed and intent is None and not card_event:
            from .material_summary import describe,text as input_text
            answer+='\n'+input_text(describe(service.store,user))
            from .autoflow import AutoFlow
            automatic=AutoFlow(cfg,service.store)
            receipt=automatic.receipt(user)
            if receipt:answer+='\n'+receipt
        # The shared CC attachment path is transport data, never model context.
        service.store.message(user,session,'user','卡片操作：'+card_event['action'] if card_event else '转发构建卡片：'+source_event['message_id'] if source_event else text.split(TRAILER,1)[0])
        service.store.message(user,session,'assistant',answer)
        from .input_cards import enabled,present
        card_action=bool(changed or getattr(service,'material_card',False) or intent and intent['action'] in ('prepare','new_task','check_materials','pause_auto','resume_auto','repeat_replay','open_materials'))
        control=getattr(service,'execution_control',None)
        callback_result=getattr(service,'card_callback_result',None)
        if callback_result:
            from .card_ui import present as card_present
            if card_present(cfg,service.store,user,session,callback_result):answer='NO_REPLY'
        elif control:
            from .execution_controls import present as control_present
            if control_present(cfg,service.store,control):answer='NO_REPLY'
        elif enabled(cfg) and card_action and present(cfg,service.store,user,session,surface=bool(getattr(service,'surface_material_card',False))):answer='NO_REPLY'
    except (GatewayError,ValueError,KeyError) as exc:
        answer=str(exc)
    except Exception:
        answer='本次处理未完成。请管理员检查本机服务；不要据此认定台架执行成功。'
    finally:
        if batch and not batch.finished:
            try:batch.finish(False)
            except Exception:pass  # The durable open batch remains an admission barrier.
            answer+='\n自动提交已暂停，已收材料保留。请重发未成功的材料，或核对后发送“检查材料”。'
    emit({'type':'item.completed','item':{'type':'agent_message','text':answer}})
    emit({'type':'turn.completed','usage':{'input_tokens':0,'cached_input_tokens':0,'output_tokens':0}})


if __name__=='__main__':main()
