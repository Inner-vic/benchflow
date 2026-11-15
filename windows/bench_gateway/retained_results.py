"""Administrator registration of explicitly authorized retained workstation results.

Registration validates an existing product and records an owner-scoped source.
It never replays data, invokes an evaluator, or creates a notification outbox.
"""
import hashlib
import json
from pathlib import PurePosixPath
import re
import time
import uuid

from .core import GatewayError, owner_key


KIND = 'retained-result-v1'
SAFE_RUN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
SAFE_SCENE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z')
SHA = re.compile(r'[a-f0-9]{64}\Z')


def _provider():
    from . import workstation_evaluation
    return workstation_evaluation


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _copy(value):
    return json.loads(_canonical(value))


def _absolute(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise GatewayError('已有结果路径必须是规范的工作站绝对路径。')
    path = PurePosixPath(value)
    if not path.is_absolute() or '..' in path.parts or str(path) != value:
        raise GatewayError('已有结果路径必须是规范的工作站绝对路径。')
    return path


def _task_path(source_task, pin):
    try:
        root = _absolute(pin['discovery']['tasks_root'])
        task = _absolute(source_task)
    except (KeyError, TypeError):
        raise GatewayError('评测工具没有有效的固定 Tasks 根目录，请管理员核验部署。') from None
    if task.parent != root or not SAFE_RUN.fullmatch(task.name):
        raise GatewayError('只登记固定 Tasks 根目录下的单个安全任务目录，不能登记其他目录或子路径。')
    return str(task), task.name


def _source(value, source_task, expected_run):
    if not isinstance(value, dict):
        raise GatewayError('工作站没有返回有效的已有结果核验记录。')
    source = _copy(value)
    if (source.get('producer_run_id') != expected_run or source.get('source_task') != source_task
            or source.get('task') != source_task or source.get('hardware_actions') != 'NOT_RUN'):
        raise GatewayError('已有结果核验的任务、生产 Run 或只读边界不匹配，未登记。')
    for key in ('source_identity', 'product_receipt_sha256', 'index_sha256', 'job_sha256', 'prepared_manifest_sha256'):
        if not isinstance(source.get(key), str) or not SHA.fullmatch(source[key]):
            raise GatewayError('已有结果缺少完整的来源身份与文件哈希，未登记：' + key)
    prediction = _absolute(source.get('prediction_path'))
    product_receipt = _absolute(source.get('product_receipt'))
    if (source.get('product') != str(prediction)
            or product_receipt != prediction.with_name(prediction.name + '.bench-eval-product.json')
            or not isinstance(source.get('product_identity'), str) or not source['product_identity']):
        raise GatewayError('已有结果的预测产品与产品收据不匹配，未登记。')
    names = source.get('scene_names')
    if (not isinstance(names, list) or not names or len(names) > 10000
            or any(not isinstance(name, str) or not SAFE_SCENE.fullmatch(name) for name in names)
            or len(set(names)) != len(names) or type(source.get('scene_count')) is not int
            or source['scene_count'] != len(names)):
        raise GatewayError('已有结果场景清单或场景数量无效，未登记。')
    return source


def register_result(cfg, store, user, source_task, label=''):
    """Register one approved Task as a completed source, without running any work.

    The caller is an administrator explicitly authorizing this owner/source pair.
    Ordinary chat must not call this function to discover other users' products.
    """
    store.require_user(user)
    if (not isinstance(label, str) or len(label) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in label)):
        raise GatewayError('已有结果标签只能是至多 200 个字符的单行文字。')
    label = label.strip()
    provider = _provider()
    pin = provider.pin_contract(cfg)
    source_task, run = _task_path(source_task, pin)
    source = _source(provider.inspect_task(cfg, source_task, expected_run=run), source_task, run)
    evaluator = _copy(provider.descriptor(cfg))
    if not isinstance(evaluator, dict) or not evaluator:
        raise GatewayError('尚未配置固定的工作站评测工具，未登记已有结果。')
    source_identity = source['source_identity']
    key = KIND + ':' + hashlib.sha256(_canonical({
        'owner': user, 'source_task': source_task, 'source_identity': source_identity}).encode('utf-8')).hexdigest()
    now = time.time()
    with store.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        # Registration may spend time inspecting a remote product. Recheck access
        # under the transaction before writing its owner-scoped local record.
        enabled = connection.execute('SELECT enabled FROM users WHERE id=?', (user,)).fetchone()
        if not enabled or enabled['enabled'] != 1:
            raise GatewayError('本用户当前不可用，已有结果未登记。')
        old = connection.execute('SELECT * FROM jobs WHERE owner=? AND request_key=?', (user, key)).fetchone()
        if old:
            job = store.unpack(old)
            payload = job['payload']
            baseline = payload.get('baseline') or {}
            if (payload.get('retained_result') is not True or payload.get('source_task') != source_task
                    or payload.get('source_identity') != source_identity or baseline.get('owner') != user
                    or baseline.get('data', {}).get('kind') != KIND):
                raise GatewayError('已有登记身份与保存记录冲突，请管理员核验；不会覆盖旧记录。')
            if baseline['data'].get('evaluator') != evaluator:
                raise GatewayError('来源内容相同，但固定评测工具身份已变化；旧登记保留，请管理员核验评测绑定后再接入。')
            return job
        baseline_id = 'b-' + uuid.uuid4().hex[:20]
        baseline_name = 'retained-' + run[:36] + '-' + source_identity[:20]
        baseline_data = {'kind': KIND, 'gate_verified': False, 'asset_ids': {},
                         'source_task': source_task, 'source_run': run, 'source_identity': source_identity,
                         'evaluator': evaluator, 'label': label, 'registration_only': True}
        baseline = {'id': baseline_id, 'owner': user, 'name': baseline_name,
                    'data': baseline_data, 'created': now}
        payload = {'action': 'replay', 'baseline': baseline, 'source_run': run,
                   'source_task': source_task, 'source_identity': source_identity,
                   'retained_result': True, 'registration_only': True, 'label': label,
                   'evaluate_after': False}
        result = {**source, 'state': 'PASS', 'run_id': run, 'task': source_task,
                  'product': source['prediction_path'], 'receipt': source['product_receipt'],
                  'retained_result': True, 'registration_only': True,
                  'hardware_actions': 'NOT_RUN',
                  'registration_message': '仅登记本人获授权的已有回放结果；本次未刷包、未回放、未执行评测。'}
        job_id = 'gw-' + time.strftime('%Y%m%d-') + uuid.uuid4().hex[:14]
        connection.execute('INSERT INTO baselines(id,owner,name,data,created) VALUES(?,?,?,?,?)',
                           (baseline_id, user, baseline_name, _canonical(baseline_data), now))
        # Deliberately bypass Store.submit/finish: a retained source is not a new
        # hardware job and must not create a completion or queue notification.
        connection.execute('''INSERT INTO jobs(id,owner,session,request_key,state,payload,result,created,updated,lane)
                            VALUES(?,?,?,?,?,?,?,?,?,?)''',
                           (job_id, user, 'admin:retained-result:' + owner_key(user), key,
                            'SUCCEEDED', _canonical(payload), _canonical(result), now, now, 'hardware'))
        return store.unpack(connection.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
