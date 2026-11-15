"""Fixed workstation extension only; no bench, replay, software or cloud actions."""
import base64
import hashlib
import json
import pathlib
import re
import subprocess
import sys

VERSION='91067586a402bd8deae65e09ff6ae8d01670c9dddbb2af7cc2f30d4a10831cd9'
ENTRY=pathlib.Path('/home/lixiang/bench-eval/extensions/arrival-accuracy/run.py')
ENTRY_SHA='2a5996793a43258f39cb4bd4886a151ba0c86d42765dd8e37195e417e68d4a14'
BASE=pathlib.Path('/mnt/data/bench-eval/tasks')
NAMES=('arrival-accuracy.xlsx','analysis_plan.json')

def run(r):
    if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,95}',r['run_id']) or not re.fullmatch(r'arr-[a-f0-9]{24}',r['attempt']):
        raise ValueError('invalid original task or analysis identity')
    task=BASE/r['run_id'];truth=pathlib.Path(r['truth_root'])
    if str(task)!=r['source_task'] or task.is_symlink() or task.resolve(strict=True)!=task:
        raise ValueError('unexpected delivered task')
    if not truth.is_absolute() or '..' in truth.parts or not truth.is_dir():
        return {'state':'NEEDS_INPUT','code':'TRUTH_ROOT_UNAVAILABLE'}
    index=json.loads((task/'index.json').read_text());scenes=index.get('scenes')
    if index.get('run_id')!=r['run_id'] or not isinstance(scenes,list) or not scenes:
        raise ValueError('source index identity differs')
    missing_csv=missing_signal=0
    for scene in scenes:
        name=scene['scene']
        if pathlib.Path(name).name!=name or name in ('.','..'):raise ValueError('unsafe scene name')
        csv=pathlib.Path(scene['task_copy'])/'sus_preview_timeline.csv'
        if task not in csv.resolve().parents:raise ValueError('distance CSV outside original task')
        missing_csv+=not csv.is_file()
        for wheel in ('FL','FR'):
            signal=truth/name/'original/xcp/BZCU'/('s_InDatCalcFAS_WhlJerk'+wheel+'_mps3.json')
            if truth.resolve() not in signal.resolve().parents:raise ValueError('signal outside selected dataset')
            missing_signal+=not signal.is_file()
    if missing_csv:return {'state':'NEEDS_INPUT','code':'MISSING_DISTANCE_CSV','missing_scenes':missing_csv,'scene_count':len(scenes)}
    if missing_signal:return {'state':'NEEDS_INPUT','code':'MISSING_XCP','missing_signals':missing_signal,'scene_count':len(scenes)}
    if hashlib.sha256(ENTRY.read_bytes()).hexdigest()!=ENTRY_SHA:raise ValueError('extension entry differs')
    active=json.loads((ENTRY.parent/'active.json').read_text())
    if active.get('path')!=str(ENTRY.parent/'versions'/VERSION):raise ValueError('extension version differs')
    output=task/'evaluations/arrival-accuracy'/r['attempt'];receipt=output/'attempt.json'
    if output.exists():
        result=json.loads(receipt.read_text()) if receipt.is_file() else {}
        if result.get('state')!='SUCCEEDED':return {'state':'NEEDS_REVIEW','code':'EXISTING_ATTEMPT_UNCONFIRMED'}
    else:
        completed=subprocess.run([sys.executable,'-B',str(ENTRY),'--enabled','--execute','--task-id',r['run_id'],
            '--truth-root',str(truth),'--attempt-id',r['attempt']],capture_output=True,text=True,timeout=600)
        if completed.returncode:
            result=json.loads(receipt.read_text()) if receipt.is_file() else {}
            return {'state':'FAILED' if result.get('state')=='FAILED' else 'NEEDS_INPUT',
                    'code':'CALCULATION_FAILED' if result.get('state')=='FAILED' else 'INPUT_OR_RUNTIME_CHECK_FAILED'}
        result=json.loads(completed.stdout)
    if (result.get('state')!='SUCCEEDED' or result.get('task_id')!=r['run_id']
            or result.get('run_id')!=r['run_id'] or result.get('output')!=str(output)):
        raise ValueError('analysis receipt identity differs')
    files={}
    for name in NAMES:
        p=output/name;expected=result['artifacts'][name]
        if p.is_symlink() or output.resolve()!=p.resolve(strict=True).parent or not 0<p.stat().st_size<=4*1024*1024:
            raise ValueError('report outside analysis or too large')
        data=p.read_bytes();digest=hashlib.sha256(data).hexdigest()
        if digest!=expected['sha256'] or len(data)!=expected['bytes']:raise ValueError('report identity differs')
        files[name]={'sha256':digest,'bytes':len(data),'data':base64.b64encode(data).decode()}
    return {'state':'SUCCEEDED','output':str(output),'scene_count':result['scene_count'],'files':files}

if __name__=='__main__':
    try:out=run(json.loads(base64.b64decode(sys.argv[1])))
    except Exception as exc:out={'state':'NEEDS_REVIEW','code':'REMOTE_OUTCOME_UNKNOWN','error_type':type(exc).__name__}
    print(json.dumps(out))
