"""Read-only cold-start diagnosis, separate from the deployed evaluation path."""
import json
import math
import subprocess
import time


def inspect_instance(binary,profile):
    """Use the supported CLI; never expose its raw credentials or configuration."""
    try:
        process=subprocess.run([binary,'devspaces','get','--name',profile['instance'],
            '--namespace',profile['namespace'],'--cluster',profile['cluster'],'--timeout','15'],
            capture_output=True,text=True,encoding='utf-8',timeout=25)
        if process.returncode or len(process.stdout)>2*1024*1024:
            raise ValueError('platform query failed')
        reply=json.loads(process.stdout)
        data=reply['data'];meta=data['metadata'];phase=data['status']['phase']
        if (meta['name']!=profile['instance'] or meta['namespace']!=profile['namespace']
                or not isinstance(meta.get('uid'),str) or not meta['uid'] or not isinstance(phase,str)):
            raise ValueError('instance identity missing or different')
        return {'state':'OBSERVED','name':meta['name'],'namespace':meta['namespace'],
                'uid':meta['uid'],'phase':phase,'checked_at':time.time()}
    except Exception as error:
        return {'state':'PLATFORM_UNAVAILABLE','error_type':type(error).__name__,
                'checked_at':time.time()}


def diagnose(instance,heartbeat,profile,*,now=None,previous_generation=None):
    """A missing heartbeat never means the instance is stopped."""
    now=time.time() if now is None else now
    if (instance.get('state')!='OBSERVED' or instance.get('name')!=profile['instance']
            or instance.get('namespace')!=profile['namespace']):
        return {'state':'PLATFORM_UNAVAILABLE','browser_action':'NONE'}
    observed=instance.get('checked_at')
    if type(observed) not in (int,float) or not math.isfinite(observed) or not -30<=now-observed<=60:
        return {'state':'PLATFORM_STALE','browser_action':'NONE'}
    phase=instance.get('phase','').lower()
    if phase in ('stopped','paused'):
        return {'state':'STOPPED','browser_action':'START_FIXED_INSTANCE'}
    if phase in ('stopping','terminating'):
        return {'state':'STOPPING','browser_action':'NONE'}
    if phase in ('starting','creating','pending','scheduling','image_pulling','initializing'):
        return {'state':'STARTING','browser_action':'NONE'}
    if phase in ('failed','error'):
        return {'state':'INSTANCE_FAILED','browser_action':'NONE'}
    if phase not in ('ready','running'):
        return {'state':'UNKNOWN_PLATFORM_STATE','browser_action':'NONE'}
    heartbeat=heartbeat or {}
    if heartbeat.get('instance')!=profile['instance'] or heartbeat.get('profile_sha256')!=profile['profile_sha256']:
        return {'state':'RECEIVER_NOT_READY','browser_action':'NONE','reason':'identity-or-profile-missing-or-different'}
    stamp=heartbeat.get('checked_at')
    if (type(stamp) not in (int,float) or not math.isfinite(stamp)
            or not -30<=now-stamp<=60 or heartbeat.get('state') not in ('READY','EVALUATING')):
        return {'state':'RECEIVER_NOT_READY','browser_action':'NONE','reason':'heartbeat-not-current'}
    generation=heartbeat.get('generation')
    if not generation or previous_generation and generation==previous_generation:
        return {'state':'RECEIVER_NOT_READY','browser_action':'NONE','reason':'new-generation-not-observed'}
    return {'state':'READY','browser_action':'NONE','busy':heartbeat['state']=='EVALUATING',
            'generation':generation}
