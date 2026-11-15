import json
import os
from pathlib import Path
import subprocess
import time
import threading

from .core import EvaluationOutcomeUnknown,GatewayError,PrecheckFailed,PreDispatchRejected,worker_lock
from .executor import Executor


def tick(cfg,store,executor=None):
    from .queue_status import QueueStatus
    queue=QueueStatus(store)
    head=queue.head()
    if not head:
        queue.publish()
        return None
    executor=executor or Executor(cfg,store)
    candidate=store.job(head['owner'],head['id'])
    if hasattr(executor,'availability'):
        availability=executor.availability(candidate)
        if availability.get('checked_at'):queue.resource(availability)
        if availability.get('state')!='READY':
            queue.publish()
            return 'WAITING_RESOURCE'
    job=store.claim()
    if not job:return None
    if job['payload']['action']!='deployment_check':
        prefix='【模拟模式，未派发台架】' if cfg.get('mode')=='simulate' else ''
        queue.notice(job,prefix+f"轮到任务 {job['id']}，正在复核输入并准备派发。回灌将先做本次小场景预检，通过后正式回灌。")
    queue.publish()
    try:
        result=executor.execute(job)
        if result.get('state')=='RESOURCE_BUSY':
            store.finish(job,'QUEUED',{**(job.get('result') or {}),'error':'等待工作站现有任务完成'})
            queue.resource({'state':'RESOURCE_BUSY','checked_at':time.time()})
            queue.publish()
            return 'WAITING_RESOURCE'
        state='SIMULATED' if result.get('simulated') else 'SUCCEEDED'
        if state=='SUCCEEDED' and job['payload'].get('action')=='replay':
            job['result']=result
            store.complete_replay(job,result)
        else:store.finish(job,state,result)
    except (ConnectionError,subprocess.TimeoutExpired) as exc:
        store.finish(job,'NEEDS_REVIEW',{**(job.get('result') or {}),'error':str(exc)})
    except PreDispatchRejected as exc:
        store.finish(job,'FAILED',{**(job.get('result') or {}),'error':str(exc),'rejection':exc.rejection})
    except PrecheckFailed as exc:
        state='FAILED' if exc.diagnostics.get('safe_to_continue') else 'NEEDS_REVIEW'
        store.finish(job,state,{**(job.get('result') or {}),'error':str(exc),'diagnostics':exc.diagnostics})
    except GatewayError as exc:
        # An error after dispatch is conservatively unknown unless it's purely local.
        state='FAILED' if cfg.get('mode')=='simulate' or not cfg.get('live_enabled') or job['payload']['action']=='evaluate' else 'NEEDS_REVIEW'
        store.finish(job,state,{**(job.get('result') or {}),'error':str(exc),'task':(job.get('result') or {}).get('task') or job['payload'].get('source_task','')})
    except Exception as exc:
        store.finish(job,'NEEDS_REVIEW',{**(job.get('result') or {}),'error':type(exc).__name__+': '+str(exc)})
    queue.publish()
    return job['id']


def postprocess_tick(cfg,store,executor=None):
    """A durable evaluation lane, independent of hardware readiness and its recovery barrier."""
    job=store.claim('postprocess')
    if not job:return None
    from .queue_status import QueueStatus
    queue=QueueStatus(store)
    queue.publish()
    executor=executor or Executor(cfg,store)
    try:
        result=executor.execute(job)
        state='SIMULATED' if result.get('simulated') else ('PARTIAL_SUCCESS' if result.get('evaluation_state')=='PARTIAL' else 'SUCCEEDED')
        store.finish(job,state,result)
    except EvaluationOutcomeUnknown as exc:
        store.finish(job,'NEEDS_REVIEW',{'task':job['payload'].get('source_task',''),'error':str(exc)})
    except GatewayError as exc:
        store.finish(job,'FAILED',{'task':job['payload'].get('source_task',''),'error':str(exc)})
    except Exception as exc:
        # The adapter may have submitted external work. Preserve its identity and require reconciliation.
        error='评测入口超时，远端评测结果待核验' if isinstance(exc,subprocess.TimeoutExpired) else '评测执行中断，请核对评测收据'
        store.finish(job,'NEEDS_REVIEW',{'task':job['payload'].get('source_task',''),'error':error})
    queue.publish()
    return job['id']


class NotificationDeadlineReached(Exception):
    """No further send was started; the durable row remains pending."""
    notification_budget_expired=True


def deliver(cfg,store,sender=None,deadline=None):
    if not cfg.get('notifications_enabled'):return 0
    # The private per-call config value preserves two-argument injected delivery
    # adapters while keeping the normal public call and sender(row) unchanged.
    if deadline is None:deadline=cfg.get('_notification_deadline')
    def remaining_timeout(maximum):
        if deadline is None:return maximum
        remaining=deadline-time.monotonic()
        if remaining<=0:raise NotificationDeadlineReached()
        return min(maximum,remaining)
    def expired():return deadline is not None and time.monotonic()>=deadline
    if expired():return 0
    cards=bool(cfg.get('task_cards_enabled'))
    if sender is None and cfg.get('feishu_direct_delivery') is True:
        from .workstation_delivery import sender as workstation_sender
        sender=workstation_sender(cfg,store,remaining_timeout)
    if sender is None:
        binary=cfg['cc_binary']
        def sender(row):
            from .wording import display_text
            argv=[binary,'send','--data-dir',str(Path(cfg['project_root'])/'runtime/cc-connect'),'-p',cfg['cc_project'],'-s',row['session']]
            if not row.get('text_sent'):
                r=subprocess.run(argv+['--stdin'],input=display_text(row['text']),text=True,encoding='utf-8',capture_output=True,timeout=remaining_timeout(30))
                if r.returncode:return False
                if row.get('_outbox_table')=='outbox':
                    # A failed TXT delivery must not resend the already delivered body.
                    with store.connect() as c:
                        changed=c.execute('UPDATE outbox SET text_sent=1 WHERE id=? AND version=?',(row['id'],row['version'])).rowcount
                    if not changed:return False
            if row['attachment']:
                r=subprocess.run(argv+['--file',row['attachment']],capture_output=True,timeout=remaining_timeout(60))
                if r.returncode:return False
            return True
    from .input_cards import enabled as unified_enabled,deliver as deliver_inputs
    unified=unified_enabled(cfg);card_count=0
    if unified:
        from .task_cards import deliver as deliver_cards
        def card_fallback(row):
            if row.get('job'):
                from .service import identity
                source=store.job(identity(row['session']),row['job'])
                if source['state'] not in ('QUEUED','RUNNING','POSTPROCESSING'):
                    # The durable terminal outbox delivers the complete result once.
                    return True
            return sender(row)
        # Completed execution cards take priority over material-stage updates.
        card_count+=deliver_cards(cfg,store,deadline=deadline,fallback=card_fallback)
        if not expired():card_count+=deliver_inputs(cfg,store,deadline=deadline,fallback=sender)
    from .progress import deliver as deliver_progress
    progress_count=card_count+deliver_progress(store,sender,deadline,**({'cards':True,'unified':unified} if cards else {}))
    if expired():return progress_count
    now=time.time()
    with store.connect() as c:
        rows=[dict(r) for r in c.execute('SELECT * FROM outbox WHERE sent=0 AND next_attempt_at<=? ORDER BY id LIMIT 10',(now,))]
        updates=[dict(r) for r in c.execute("SELECT q.*,NULL AS attachment FROM queue_outbox q JOIN jobs j ON j.id=q.job WHERE q.sent=0 AND q.next_attempt_at<=? AND j.state IN ('QUEUED','RUNNING') ORDER BY q.rowid LIMIT 20",(now,))]
    count=progress_count
    # Complete results take precedence over transient positions in the waiting queue.
    for table,pending in (('outbox',rows),('queue_outbox',updates)):
        for row in pending:
            if expired():return count
            row['_outbox_table']=table
            key='id' if table=='outbox' else 'job'
            with store.connect() as c:
                current=c.execute(f'SELECT 1 FROM {table} WHERE {key}=? AND version=? AND sent=0',(row[key],row['version'])).fetchone()
                if table=='queue_outbox' and not c.execute("SELECT 1 FROM jobs WHERE id=? AND state IN ('QUEUED','RUNNING')",(row['job'],)).fetchone():current=None
            if not current:continue
            if cards:
                with store.connect() as c:
                    source=c.execute('SELECT * FROM jobs WHERE id=?',(row['job'],)).fetchone()
                source=store.unpack(source) if source else None
                replay_source=source
                if unified and source and source['payload'].get('automatic') and source['payload'].get('action')=='evaluate':
                    replay_source=store.job(source['owner'],source['payload']['source_job'])
                if replay_source and replay_source['payload'].get('action')=='replay':
                    if table=='queue_outbox':
                        with store.connect() as c:c.execute('UPDATE queue_outbox SET sent=2 WHERE job=? AND version=?',(row['job'],row['version']))
                        continue
                    if unified:
                        with store.connect() as c:card=c.execute('SELECT * FROM task_cards WHERE job=?',(replay_source['id'],)).fetchone()
                        covered=card and card['message_id'] and card['last_sent']>=replay_source['updated']
                        if covered:
                            with store.connect() as c:
                                c.execute('UPDATE outbox SET text_sent=1 WHERE id=? AND version=?',(row['id'],row['version']))
                                if not row['attachment']:c.execute('UPDATE outbox SET sent=2 WHERE id=? AND version=?',(row['id'],row['version']))
                            if not row['attachment']:continue
                            row['text_sent']=1
                        elif not card or card['attempts']<3:continue
                    from .task_status import terminal_text
                    row['text']=terminal_text(source)
            try:ok=sender(row)
            except NotificationDeadlineReached:return count
            except Exception:ok=False
            # An old acknowledgement never consumes a newer result or queue position.
            delay=min(300,max(0,float(cfg.get('notification_retry_seconds',0)))*2**min(row['attempts'],8))
            retry_at=0 if ok else time.time()+delay
            with store.connect() as c:
                changed=c.execute(f'UPDATE {table} SET attempts=attempts+1,sent=?,next_attempt_at=? WHERE {key}=? AND version=?',(int(ok),retry_at,row[key],row['version'])).rowcount
            count+=int(bool(ok and changed))
    from .preparation import PreparationQueue
    if expired():return count
    preparation=PreparationQueue(cfg,store)
    count+=(preparation.deliver(sender) if deadline is None else preparation.deliver(sender,deadline=deadline))
    if not expired():
        from .arrival_analysis import deliver as deliver_arrival
        count+=deliver_arrival(cfg,store,deadline=deadline)
    if not expired():
        from .roadmodel_plugin import deliver as deliver_roadmodel
        deliver_roadmodel(cfg,store,deadline=deadline)
    # Card API failures must not starve final results or required user actions.
    if cards and not unified and not expired():
        from .task_cards import deliver as deliver_cards
        count+=deliver_cards(cfg,store,deadline=deadline,fallback=sender)
    return count


def run(cfg,store,once=False):
    with worker_lock(store.root):
        from .preparation import PreparationQueue
        preparation=PreparationQueue(cfg,store)
        from .autoflow import AutoFlow
        automatic=AutoFlow(cfg,store)
        from .arrival_analysis import recover as recover_arrival,tick as arrival_tick
        recover_arrival(store)
        try:
            from .roadmodel_plugin import recover as recover_roadmodel
            recover_roadmodel(cfg)
        except Exception as error:
            print(json.dumps({'component':'roadmodel','event':'recovery_unavailable','error_type':type(error).__name__}),flush=True)
        preparation.recover()
        pcm_settings=cfg.get('pcm_test1_ingress') or {}
        if pcm_settings.get('enabled') is True:
            from .pcm_test1_ingress import recover as recover_pcm
            recover_pcm(store)
        if cfg.get('defer_assembly_archive'):
            from .archive_queue import recover as recover_archives
            recover_archives(store)
        with store.connect() as c:
            stale=[store.unpack(r) for r in c.execute("SELECT * FROM jobs WHERE state='RUNNING'")]
        for job in stale:
            error='评测调度进程曾中断，请核对评测收据；不会自动重跑评测，也不会重新回灌' if job['lane']=='postprocess' else '调度进程曾中断，请核对远端 Runner 与恢复收据后续交付；不会自动重跑'
            store.finish(job,'NEEDS_REVIEW',{**(job.get('result') or {}),'error':error})
            if cfg.get('recover_completed_on_restart') and job['lane']=='hardware':
                from .restart_recovery import completed
                try:completed(cfg,store,store.job(job['owner'],job['id']))
                except Exception:pass  # Keep the recovery barrier; never dispatch a second run.
        print(json.dumps({'component':'worker','event':'ready','mode':cfg['mode'],'live_enabled':cfg.get('live_enabled',False)}),flush=True)
        stopped=threading.Event()
        notifications_stopped=threading.Event()
        delivery_lock=threading.Lock()
        def notifications():
            while not notifications_stopped.is_set():
                with delivery_lock:
                    if notifications_stopped.is_set():return
                    try:deliver({**cfg,'_notification_deadline':time.monotonic()+8},store)
                    except Exception:pass  # Durable outboxes remain pending for the next attempt.
                notifications_stopped.wait(1)
        notifier=None
        if not once and cfg.get('notifications_enabled'):
            notifier=threading.Thread(target=notifications,daemon=True);notifier.start()
        def preparing():
            while not stopped.is_set():
                if os.environ.get('BENCH_GATEWAY_MANAGED')=='1' and (Path(cfg['project_root'])/'runtime/service/drain.json').exists():return
                try:
                    automatic.tick()
                    preparation.tick()
                    automatic.tick()
                    if pcm_settings.get('enabled') is True:
                        from .pcm_test1_ingress import prepare_one as prepare_pcm
                        prepare_pcm(cfg,store)
                except Exception:print(json.dumps({'component':'preparation','event':'check_failed'}),flush=True)
                stopped.wait(1)
        preparer=None
        if not once:
            preparer=threading.Thread(target=preparing,daemon=True);preparer.start()
        def postprocessing():
            while not stopped.is_set():
                if os.environ.get('BENCH_GATEWAY_MANAGED')=='1' and (Path(cfg['project_root'])/'runtime/service/drain.json').exists():return
                try:postprocess_tick(cfg,store)
                except Exception:print(json.dumps({'component':'postprocess','event':'dispatch_failed'}),flush=True)
                try:arrival_tick(cfg,store)
                except Exception:print(json.dumps({'component':'arrival','event':'analysis_failed'}),flush=True)
                stopped.wait(1)
        postprocessor=None
        if not once:
            postprocessor=threading.Thread(target=postprocessing,daemon=True);postprocessor.start()
        def archiving():
            from .archive_queue import tick as archive_tick
            while not stopped.is_set():
                if os.environ.get('BENCH_GATEWAY_MANAGED')=='1' and (Path(cfg['project_root'])/'runtime/service/drain.json').exists():return
                try:archive_tick(cfg,store)
                except Exception:print(json.dumps({'component':'archive','event':'check_failed'}),flush=True)
                stopped.wait(1)
        archiver=None
        if not once and cfg.get('defer_assembly_archive'):
            archiver=threading.Thread(target=archiving,daemon=True);archiver.start()
        def roadmodel_processing():
            while not stopped.is_set():
                if os.environ.get('BENCH_GATEWAY_MANAGED')=='1' and (Path(cfg['project_root'])/'runtime/service/drain.json').exists():return
                try:
                    from .roadmodel_plugin import tick as roadmodel_tick
                    roadmodel_tick(cfg)
                except Exception:print(json.dumps({'component':'roadmodel','event':'dispatch_failed'}),flush=True)
                stopped.wait(1)
        roadmodel_worker=None
        if not once:
            roadmodel_worker=threading.Thread(target=roadmodel_processing,daemon=True);roadmodel_worker.start()
        try:
            while True:
                # Drain active work before shutting down. Notification delivery runs during long replay calls.
                if os.environ.get('BENCH_GATEWAY_MANAGED')=='1' and (Path(cfg['project_root'])/'runtime/service/drain.json').exists():return
                tick(cfg,store)
                if once:
                    try:
                        from .roadmodel_plugin import tick as roadmodel_tick
                        roadmodel_tick(cfg)
                    except Exception:print(json.dumps({'component':'roadmodel','event':'dispatch_failed'}),flush=True)
                    automatic.tick()
                    preparation.tick()
                    automatic.tick()
                    if pcm_settings.get('enabled') is True:
                        from .pcm_test1_ingress import prepare_one as prepare_pcm
                        prepare_pcm(cfg,store)
                    postprocess_tick(cfg,store)
                    arrival_tick(cfg,store)
                    deliver(cfg,store)
                    if cfg.get('defer_assembly_archive'):
                        from .archive_queue import tick as archive_tick
                        archive_tick(cfg,store)
                        deliver(cfg,store)
                    return
                time.sleep(cfg.get('poll_seconds',3))
        finally:
            stopped.set()
            # No hardware runs here. Finish a bounded active download/check before managed shutdown.
            if preparer:preparer.join()
            if roadmodel_worker:roadmodel_worker.join()
            # Reviewed evaluation adapters have a bounded timeout; do not abandon them during graceful drain.
            if postprocessor:postprocessor.join()
            if archiver:archiver.join()  # Bounded workstation-only operation; never abandon a write.
            if notifier:
                # The notifier remains alive while active preparation/evaluation
                # finishes. Serialize one final bounded pass with its current
                # pass, so shutdown never sends the same row concurrently.
                deadline=time.monotonic()+8
                acquired=delivery_lock.acquire(timeout=max(0,deadline-time.monotonic()))
                try:
                    if acquired:
                        try:deliver({**cfg,'_notification_deadline':deadline},store)
                        except Exception:pass
                finally:
                    notifications_stopped.set()
                    if acquired:delivery_lock.release()
                notifier.join(timeout=max(0,deadline-time.monotonic()))
