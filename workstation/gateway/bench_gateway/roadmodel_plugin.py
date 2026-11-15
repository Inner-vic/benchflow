"""Demo routing alongside existing work orders on the single registered Bot."""
from contextlib import closing
import importlib
from pathlib import Path
import sqlite3
import sys
import time
import json

ROOT = Path('/mnt/data/bench-eval/service/roadmodel/evaluation-native-20261105-r2')
_last_error={}

def unavailable(name,stage,error):
    now=time.monotonic();key=(name,stage)
    if now-_last_error.get(key,float('-inf'))>=60:
        _last_error[key]=now
        print(json.dumps({'component':'roadmodel','event':'backend_unavailable','backend':name,'stage':stage,'error_type':type(error).__name__}),file=sys.stderr,flush=True)


def card_route(state_root, session, event):
    """Use the original card binding when the connector drops entry_schema."""
    root = Path(state_root) / 'roadmodel'
    value = event['value']
    bindings = (
        ('evaluation/runtime.sqlite3', 'tasks', 'evaluation_entry', 'roadmodel-evaluation/v1'),
        ('delivery-demo/runtime.sqlite3', 'tasks', 'delivery_demo_entry', 'delivery-demo/v1'),
        ('model-tickets/tickets.sqlite3', 'tickets', 'model_ticket_entry', 'model-ticket/v2'),
        ('runtime.sqlite3', 'tasks', 'feishu_entry', None),
    )
    matches = []
    for name, table, handler, schema in bindings:
        path = root / name
        if not path.is_file():
            continue
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute(
                f'SELECT 1 FROM {table} WHERE id=? AND message_id=? AND owner=? AND session=?',
                (value.get('task_id'), event['message_id'], event['operator_id'], session),
            ).fetchone()
        if row:
            matches.append((handler, schema))
    if len(matches) != 1:
        return None
    handler, schema = matches[0]
    claimed_schema = value.get('entry_schema')
    if schema and claimed_schema not in (None, schema):
        return None
    return handler, schema


def backend(name):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    module = importlib.import_module('pcm_eval.'+name)
    if ROOT not in Path(module.__file__).resolve().parents:
        raise ValueError('Unexpected RoadModel module release')
    return module


def handle(service, session, text):
    from .plugin_routing import roadmodel
    if not roadmodel(service.cfg,session,text):return None
    try:return _handle(service,session,text)
    except Exception as error:
        unavailable('routing','message',error)
        return 'RoadModel入口暂时不可用，请联系管理员；未转交台架评测执行。已提交任务不自动重跑。'

def _handle(service,session,text):
    entry = backend('evaluation_entry')
    if text.strip() in entry.COMMANDS:
        return entry.handle(service, session, text)
    demo = backend('delivery_demo_entry')
    if text.strip() in demo.COMMANDS:
        return demo.handle(service, session, text)
    from .card_ui import PREFIX, decode
    if text.startswith(PREFIX):
        event = decode(service.cfg, session, text)
        if event.get('action') == 'roadmodel_submit':
            route = card_route(service.cfg['state_root'], session, event)
            if route is None:
                return '找不到这张卡绑定的工单。请发送 RoadModel评测 打开当前入口。'
            handler, schema = route
            if schema is None:
                return backend(handler).handle(service, session, text)
            routed = {**event, 'value': {**event['value'], 'entry_schema': schema}}
            return backend(handler).handle(service, session, text, routed)
    if text.strip() in backend('model_ticket_entry').COMMANDS:
        return backend('model_ticket_entry').handle(service, session, text)
    return None


def recover(cfg):
    for name in ('evaluation_entry', 'delivery_demo_entry', 'feishu_entry'):
        try:
            if name=='feishu_entry':
                from .roadmodel_shared import recover
                recover(cfg,backend(name).runtime(cfg))
            else:backend(name).recover(cfg)
        except Exception as error:unavailable(name,'recover',error)


def tick(cfg):
    for name in ('evaluation_entry', 'delivery_demo_entry', 'model_ticket_entry', 'feishu_entry'):
        try:
            if name=='feishu_entry':
                from .roadmodel_shared import tick
                tick(cfg,backend(name).runtime(cfg))
            else:
                if name in ('evaluation_entry','delivery_demo_entry'):
                    config='evaluation.json' if name=='evaluation_entry' else 'delivery_demo.json'
                    if json.loads((ROOT/'configs'/config).read_text()).get('bench_available') is not False:
                        raise ValueError('This board execution path is not admitted to the shared queue')
                backend(name).tick(cfg)
        except Exception as error:unavailable(name,'tick',error)


def deliver(cfg, store, deadline=None):
    if deadline:
        cfg = {**cfg, '_notification_deadline': min(deadline, cfg.get('_notification_deadline', deadline))}
    for name in ('evaluation_entry', 'delivery_demo_entry', 'model_ticket_entry', 'feishu_entry'):
        if time.monotonic() >= cfg.get('_notification_deadline', time.monotonic()+8):
            return
        try:backend(name).deliver(cfg, store)
        except Exception as error:unavailable(name,'deliver',error)
