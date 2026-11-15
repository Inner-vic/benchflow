"""Complete only software archiving for an already successful owner's replay.

No job is submitted, replayed, requeued, finished, or notified here.  Successful
evaluation fields may change concurrently; only the archive fields are merged.
"""
import json
from pathlib import PurePosixPath
import re
import time

from .core import GatewayError


STATES = ('SUCCEEDED', 'PARTIAL_SUCCESS', 'POSTPROCESSING')
WARNING = '回放已完成，但成功装配包尚未完成归档登记；原结果保留，不会重新刷包或回放。'
SHA = re.compile(r'^[a-f0-9]{64}$')


class _ArchiveRegisteredDuringMerge(Exception):
    """A real owner/source catalog row appeared after the initial lookup."""
    def __init__(self, receipt):
        self.receipt = receipt


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _clone(value):
    return json.loads(_canonical(value))


def _task(value, run):
    if (not isinstance(value, str) or not value or '\\' in value or any(ord(c) < 32 for c in value)):
        raise GatewayError('回放成功收据缺少真实工作站任务目录。')
    path = PurePosixPath(value)
    if (not path.is_absolute() or str(path) != value or '..' in path.parts
            or path.name != run or path.parent.name != 'tasks'):
        raise GatewayError('回放目录与本任务 Run 身份不一致，不能补归档。')
    return value


def _source(job):
    """Require full and current-precheck receipts, rather than a display status."""
    try:
        payload, result, run = job['payload'], job.get('result') or {}, job['id']
        baseline = payload.get('baseline', {}).get('data', {})
        frozen = baseline.get('binding', {}).get('input_digest')
        if (job['state'] not in STATES or payload.get('action') != 'replay'
                or payload.get('retained_result') or payload.get('registration_only')
                or result.get('simulated') or baseline.get('kind') != 'candidate-v1'
                or not isinstance(frozen, str) or not SHA.fullmatch(frozen)
                or result.get('state') != 'PASS' or result.get('run_id') != run
                or result.get('input_digest') != frozen):
            raise GatewayError('仅支持本人真实成功候选回放的装配补归档；不接受模拟、历史登记或待核验任务。')
        task = _task(result.get('task'), run)
        smoke = result.get('smoke') or {}
        if (smoke.get('simulated') or smoke.get('state') != 'PASS'
                or smoke.get('run_id') != run + '-smoke' or smoke.get('input_digest') != frozen):
            raise GatewayError('缺少本任务真实小场景成功收据，不能补归档。')
        smoke_task = _task(smoke.get('task'), run + '-smoke')
        assets = payload.get('assets') or {}
        package = assets.get('package') or {}
        dependencies = assets.get('dependencies')
        if (package.get('owner') != job['owner'] or package.get('kind') != 'package'
                or not SHA.fullmatch(str(package.get('data', {}).get('sha256', '')))
                or not isinstance(dependencies, list) or len(dependencies) != 3):
            raise GatewayError('装配补归档需要本人的冻结 APP 和完整三份 SO。')
        targets = set()
        for item in dependencies:
            target = item.get('data', {}).get('target')
            if (item.get('owner') != job['owner'] or item.get('kind') != 'dependency'
                    or target not in ('lib/liburoad_m100.so', 'lib/libxcamera.so', 'lib/libxlidar.so')
                    or target in targets or not SHA.fullmatch(str(item.get('data', {}).get('sha256', '')))):
                raise GatewayError('装配补归档的 SO 必须属于本人且角色完整。')
            targets.add(target)
        return {'run_id': run, 'task': task, 'input_digest': frozen,
                'smoke_run': run + '-smoke', 'smoke_task': smoke_task}
    except (KeyError, TypeError, AttributeError):
        raise GatewayError('回放任务或真实成功收据不完整，不能补归档。') from None


def _merge(store, user, job_id, payload, source, changes, success):
    """Compare and swap only archive metadata, preserving concurrent results."""
    allowed = {'assembly', 'assembly_archive'}
    if set(changes) - allowed:
        raise GatewayError('补归档只能更新装配元数据。')
    for _ in range(6):
        with store.connect() as connection:
            row = connection.execute('SELECT * FROM jobs WHERE id=? AND owner=?', (job_id, user)).fetchone()
            if row is None:
                raise GatewayError('本人回放任务已不存在，补归档未写回。')
            current = store.unpack(row)
            if _canonical(current['payload']) != payload or _source(current) != source:
                raise GatewayError('补归档期间源任务或成功身份变化，保留当前结果，不覆盖。')
            if not success:
                # A successful concurrent recovery may have committed its catalog
                # and result while this call was waiting on a failed verifier.
                # Check the durable owner/source registration, never a display
                # assembly.state=SAVED flag. The caller fully revalidates the
                # receipt with record_success before treating this as success.
                registered = connection.execute(
                    'SELECT data FROM assembly_sources WHERE owner=? AND source_job=?',
                    (user, job_id)).fetchone()
                if registered:
                    try:
                        receipt = json.loads(registered['data'])['archive']
                    except (ValueError, KeyError, TypeError):
                        raise GatewayError('本来源装配登记不完整，请管理员核验，不能覆盖成功记录。') from None
                    raise _ArchiveRegisteredDuringMerge(receipt)
            merged = {**(current.get('result') or {}), **_clone(changes)}
            if success:
                merged.pop('assembly_archive_warning', None)
            else:
                merged['assembly_archive_warning'] = WARNING
            if _canonical(merged) == _canonical(current.get('result') or {}):
                return current
            changed = connection.execute('''UPDATE jobs SET result=?,updated=?
                WHERE id=? AND owner=? AND state=? AND updated=? AND payload=? AND result=?''',
                (_canonical(merged), time.time(), job_id, user, current['state'], current['updated'],
                 row['payload'], row['result'])).rowcount
            if changed:
                current['result'] = merged
                return current
    raise GatewayError('任务结果仍在并发更新，装配收据已保留；请稍后仅重试补归档。')


def _registered(cfg, store, user, job_id):
    from .assemblies import list_assemblies
    matches = []
    for record in list_assemblies(cfg, store, user):
        for source in record.get('sources', []):
            if source.get('source_job') == job_id:
                matches.append(source.get('archive'))
    if len(matches) > 1:
        raise GatewayError('同一源任务已有多个不同装配登记，请管理员核对，不能覆盖。')
    return matches[0] if matches else None


def _outcome(job, success, duplicate=False, code=None):
    result = job.get('result') or {}
    return {'state': 'ASSEMBLY_RECOVERED' if success else 'ASSEMBLY_PENDING',
            'job_id': job['id'], 'job_state': job['state'], 'idempotent': duplicate,
            'assembly': _clone(result.get('assembly')) if success else None,
            'warning': None if success else WARNING,
            'error_code': code, 'hardware_actions': 'NOT_RUN',
            'queue_changed': False, 'notifications_created': False}


def recover_assembly(cfg, store, user, job_id, executor=None):
    """Register/finish only the owner's archive, irrespective of hardware mode.

    Existing SAVED artifacts use verify_assembly and require no current candidate
    implementation or original inputs.  Without such a receipt save_assembly
    retains all of its existing implementation/input guards; there is no replay
    or recover-delivery fallback.  Ineligible input raises GatewayError.  An
    unavailable archive remains pending without changing the job's state.
    """
    store.require_user(user)
    job = store.job(user, job_id)
    source = _source(job)
    payload = _canonical(job['payload'])
    result = _clone(job['result'])
    from .assemblies import record_success
    registered = _registered(cfg, store, user, job_id)
    if registered:
        receipt = _clone(registered)
        result['assembly_archive'] = receipt
        metadata = record_success(cfg, store, job, result)
        current = _merge(store, user, job_id, payload, source,
                         {**metadata, 'assembly_archive': receipt}, True)
        return _outcome(current, True, True)
    if executor is None:
        from .executor import Executor
        executor = Executor(cfg, store)
    receipt = result.get('assembly_archive')
    verify_existing = isinstance(receipt, dict) and receipt.get('state') == 'SAVED'
    try:
        if verify_existing:
            verified = executor.verify_assembly(_clone(job), _clone(receipt))
            if _canonical(verified) != _canonical(receipt):
                raise GatewayError('远端核验返回了不同的装配工件身份。')
            result['assembly_archive'] = verified
        else:
            # A private clone also protects this function from adapter mutation.
            result = executor.save_assembly(_clone(job), _clone(result), record_stage=False)
            if (not isinstance(result, dict) or not isinstance(result.get('assembly_archive'), dict)
                    or result['assembly_archive'].get('state') != 'SAVED'):
                raise GatewayError('工作站尚未返回完整已保存装配收据。')
        # Revalidate source ownership/role hashes even after a successful remote
        # response.  This also finishes a local catalog write that previously failed.
        metadata = record_success(cfg, store, job, result)
        changes = {**metadata, 'assembly_archive': result['assembly_archive']}
    except Exception:
        try:
            current = _merge(store, user, job_id, payload, source, {}, False)
        except _ArchiveRegisteredDuringMerge as registered:
            # Re-read the successful source and use the normal owner, full/smoke,
            # package/SO identity and immutable catalog checks. A fake SAVED
            # marker, another source's registration, or changed inputs cannot
            # turn a failed verification into a successful recovery.
            latest = store.job(user, job_id)
            if _canonical(latest['payload']) != payload or _source(latest) != source:
                raise GatewayError('补归档期间源任务或成功身份变化，保留当前结果，不覆盖。')
            saved = {**latest['result'], 'assembly_archive': _clone(registered.receipt)}
            metadata = record_success(cfg, store, latest, saved)
            current = _merge(store, user, job_id, payload, source,
                             {**metadata, 'assembly_archive': saved['assembly_archive']}, True)
            return _outcome(current, True, True)
        return _outcome(current, False, code='ARCHIVE_VERIFY_FAILED' if verify_existing else 'ARCHIVE_SAVE_FAILED')
    current = _merge(store, user, job_id, payload, source, changes, True)
    return _outcome(current, True)
