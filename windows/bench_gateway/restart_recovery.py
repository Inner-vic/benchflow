"""Recover only fully completed retained runs after a local worker interruption."""
import json
import time

from .core import GatewayError,owner_key
from .reconciliation import PROOF_FLAGS


def completed(cfg,store,job,executor=None):
    payload=job['payload'];baseline=payload.get('baseline',{}).get('data',{})
    if (job['state']!='NEEDS_REVIEW' or payload.get('action')!='replay'
            or baseline.get('kind')!='candidate-v1'):
        return False
    digest=baseline.get('binding',{}).get('input_digest');scenes=payload.get('scenes')
    if not isinstance(digest,str) or not isinstance(scenes,list) or not scenes:return False
    from .executor import Executor
    executor=executor or Executor(cfg,store)
    before=json.dumps(job,sort_keys=True)
    shared={'parent_run_id':job['id'],'actor':owner_key(job['owner']),
            'assets':payload['assets'],'baseline':baseline}
    proofs=[]
    for operation,run,selected in (('reconcile_replay',job['id'],scenes),
                                   ('reconcile_precheck',job['id']+'-smoke',scenes[:1])):
        request=executor.request(job['owner'],operation,run_id=run,scenes=selected,**shared)
        proof=executor.remote(request,run+'-restart-check',read_only=True)
        if (not isinstance(proof,dict) or proof.get('state')!='PASS' or proof.get('run_id')!=run
                or proof.get('input_digest')!=digest or proof.get('hardware_actions')!='NOT_RUN'
                or not proof.get('task') or any(proof.get(flag) is not True for flag in PROOF_FLAGS)):
            raise GatewayError('中断任务缺少完整完成、恢复或清理证据，保留待核验，不会重跑。')
        proofs.append(proof)
    result={**proofs[0],'smoke':proofs[1],'workstation':cfg.get('workstation','ubuntu-workstation'),
            'restart_recovery':{'state':'VERIFIED','checked_at':time.time(),'hardware_actions':'NOT_RUN'},
            'assembly_archive_warning':'回放已完成，本次仅核验并补交付结果；装配存档可由管理员按原任务补归档，无需重新回放。'}
    # Use the same atomic replay/evaluation handoff as a normal completion.
    # It reads the latest execution goal and queues at most one postprocess job.
    store.complete_replay(job,result,recovered=True,expected_snapshot=json.loads(before))
    return True
