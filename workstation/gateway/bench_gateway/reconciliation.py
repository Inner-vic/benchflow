"""Receipt-only recovery of a completed candidate precheck.

An SSH interruption can hide a successful small-scene result from the PC. This
module reconciles that exact run; it never dispatches a replay or an install.
The worker can stay running: the original review barrier blocks dispatch while
evidence is collected, and the final transaction rejects concurrent changes.
"""
from __future__ import annotations

import hashlib
import json
import time

from .core import GatewayError, owner_key


PROOF_FLAGS = ('evidence_only', 'resource_idle', 'restore_verified',
               'cleanup_verified', 'binding_verified')


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _identity(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _contract(job):
    payload = job.get('payload') or {}
    baseline = (payload.get('baseline') or {}).get('data') or {}
    digest = (baseline.get('binding') or {}).get('input_digest')
    scenes = payload.get('scenes')
    if (payload.get('action') != 'replay' or baseline.get('kind') != 'candidate-v1'
            or not isinstance(digest, str) or len(digest) != 64
            or any(c not in '0123456789abcdef' for c in digest)
            or not isinstance(scenes, list) or not scenes
            or any(type(i) is not int or i < 1 for i in scenes)):
        raise GatewayError('仅支持本人候选回灌任务的小场景收据核验；原任务绑定或场景无效。')
    return payload, digest, [scenes[0]], job['id'] + '-smoke'


def _validate_receipt(receipt, smoke_id, input_digest):
    if (not isinstance(receipt, dict) or receipt.get('state') != 'PASS'
            or receipt.get('run_id') != smoke_id
            or receipt.get('input_digest') != input_digest
            or any(receipt.get(flag) is not True for flag in PROOF_FLAGS)):
        raise GatewayError('本次小场景完成、恢复、产品、清理或空闲证据不足，任务保持待核验；不会重跑。')


def _already_reconciled(job, smoke_id, input_digest):
    result = job.get('result') or {}
    saved = result.get('precheck_reconciliation') or {}
    smoke = result.get('smoke')
    if (not isinstance(saved, dict) or saved.get('state') != 'VERIFIED'
            or saved.get('run_id') != smoke_id
            or saved.get('input_digest') != input_digest):
        return False
    _validate_receipt(smoke, smoke_id, input_digest)
    if saved.get('receipt_sha256') != _identity(smoke):
        raise GatewayError('已保存的小场景核验记录不一致，请管理员检查，不能重复续接。')
    return True


def _outcome(job, smoke_id, duplicate):
    state = job['state']
    next_step = ('FULL_REPLAY_ONLY' if state == 'QUEUED' else
                 'KEEP_CURRENT_EXECUTION' if state == 'RUNNING' else
                 'RECONCILE_CURRENT_OUTCOME' if state == 'NEEDS_REVIEW' else
                 'KEEP_TERMINAL_STATE')
    summary = '本任务小场景成功收据已核验；保留原任务，后续只执行正式回灌，不重跑小场景。'
    if duplicate and state not in ('QUEUED', 'RUNNING'):
        summary = '本任务小场景已有核验记录；保留当前任务状态，不重新排队、不重跑。请按当前结果核对正式回灌或交付收据。'
    return {'state': 'PRECHECK_RECOVERED', 'job_id': job['id'],
            'run_id': smoke_id, 'job_state': state,
            'idempotent': duplicate, 'next_step': next_step,
            'smoke': job['result']['smoke'],
            'summary': summary}


def recover_precheck(cfg, store, user, job_id, executor=None):
    """Requeue only after positive workstation evidence for this exact precheck.

    The remote operation is evidence-only. Missing evidence, uncertain runner
    state, or any concurrent change leaves the recovery barrier in place.
    """
    job = store.job(user, job_id)
    payload, input_digest, scenes, smoke_id = _contract(job)
    if _already_reconciled(job, smoke_id, input_digest):
        return _outcome(job, smoke_id, True)
    if job['state'] != 'NEEDS_REVIEW':
        raise GatewayError('只对本人待核验回灌任务续接本次小场景；不能重跑或重复提交。')
    before_payload = _identity(payload)
    before_result = _identity(job.get('result'))
    before_updated = job['updated']
    if executor is None:
        from .executor import Executor
        executor = Executor(cfg, store)
    request = executor.request(user, 'reconcile_precheck', run_id=smoke_id,
                               parent_run_id=job['id'], actor=owner_key(user), assets=payload['assets'],
                               baseline=payload['baseline']['data'], scenes=scenes)
    receipt = executor.remote(request, smoke_id + '-reconcile', read_only=True)
    _validate_receipt(receipt, smoke_id, input_digest)
    with store.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT * FROM jobs WHERE id=? AND owner=?',
                                 (job_id, user)).fetchone()
        if not row:
            raise GatewayError('原任务已变化，核验结果不能写入。')
        current = store.unpack(row)
        if _already_reconciled(current, smoke_id, input_digest):
            return _outcome(current, smoke_id, True)
        if (current['state'] != 'NEEDS_REVIEW'
                or current['updated'] != before_updated
                or _identity(current['payload']) != before_payload
                or _identity(current.get('result')) != before_result):
            raise GatewayError('核验期间原任务或收据发生变化，保持现有状态，请重新核对。')
        saved = {**(current.get('result') or {}), 'smoke': receipt,
                 'stage': '本次小场景收据已核验，等待正式回灌',
                 'precheck_reconciliation': {
                     'schema_version': 1, 'state': 'VERIFIED',
                     'run_id': smoke_id, 'input_digest': input_digest,
                     'checked_at': time.time(), 'receipt_sha256': _identity(receipt),
                     'previous_result_sha256': before_result}}
        if saved.get('error'):
            saved['precheck_reconciliation']['previous_error'] = saved.pop('error')
        changed = connection.execute(
            "UPDATE jobs SET state='QUEUED',result=?,updated=? WHERE id=? AND owner=? AND state='NEEDS_REVIEW'",
            (_canonical(saved), time.time(), job_id, user)).rowcount
        if changed != 1:
            raise GatewayError('原任务状态变化，小场景核验未续接。')
        # Invalidate a pending old NEEDS_REVIEW notice. Keep the row and advance
        # its version so a late delivery acknowledgement cannot hide a new result.
        connection.execute('''UPDATE outbox SET sent=1,version=version+1,
                              attempts=0,text_sent=0,next_attempt_at=0 WHERE job=?''', (job_id,))
        connection.execute('DELETE FROM queue_outbox WHERE job=?', (job_id,))
        current.update(state='QUEUED', result=saved)
    return _outcome(current, smoke_id, False)
