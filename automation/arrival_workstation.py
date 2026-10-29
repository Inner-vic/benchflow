#!/usr/bin/env python3
"""Default-off postprocessing of an already delivered workstation task."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'vendor'))
CORE = ROOT/'vendor/arrival-core/speedbump_accuracy.py'
CORE_SHA = '07e8ee9c2f907a63f357d70067065d11d098f493323928bdd8bb22ef1ca57d8f'
SAFE_ID = re.compile(r'^[a-z0-9][a-z0-9._-]{0,95}$')


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for b in iter(lambda:stream.read(1024*1024),b''):
            h.update(b)
    return h.hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def runtime():
    from trial_arrival_accuracy import load_core
    import openpyxl
    if sha(CORE)!=CORE_SHA:
        raise ValueError('verified core checksum differs')
    load_core(CORE,arrays_only=True)
    if not shutil.which('libreoffice'):
        raise ValueError('LibreOffice required to recalculate and validate XLSX')
    return {'state':'READY','core_sha256':CORE_SHA,'xlsx':'openpyxl '+openpyxl.__version__+' + LibreOffice'}


def plan(task_id, truth_root, bench_root, attempt_id):
    from arrival_input_lineage import verify_upstream
    from probe_arrival_inputs import read_timeline, latest_success_at_time, read_jerk
    if not SAFE_ID.fullmatch(task_id) or not SAFE_ID.fullmatch(attempt_id):
        raise ValueError('invalid task or attempt ID')
    truth_root=truth_root.resolve(strict=True)
    bench_root=bench_root.resolve(strict=True)
    index_path=bench_root/'tasks'/task_id/'index.json'
    index=load(index_path)
    run=index['run_id']
    if not SAFE_ID.fullmatch(run) or index.get('task_id')!=task_id:
        raise ValueError('task identity mismatch')
    upstream={'task_index':index_path,'producer_receipt':Path(index['producer_receipt']),
              **{k:bench_root/'runs'/run/name for k,name in [('transfer_receipt','workstation-predictions.json'),
                 ('run_state','state.json'),('restore_receipt','restore-verification.json')]}}
    evidence={k:{'path':str(p),'sha256':sha(p)} for k,p in upstream.items()}
    scenes=[]; files=[]; signals=[]
    for entry in index['scenes']:
        if not SAFE_ID.fullmatch(entry['scene_id']) or Path(entry['scene']).name!=entry['scene'] or entry['scene'] in ('.','..'):
            raise ValueError('unsafe scene identity')
        timeline=Path(entry['task_copy'])/'sus_preview_timeline.csv'
        if timeline.parent.name != entry['scene'] or (bench_root/'tasks'/task_id).resolve() not in timeline.resolve().parents:
            raise ValueError('timeline is outside delivered task')
        jerk={w:str(truth_root/entry['scene']/'original/xcp/BZCU'/f's_InDatCalcFAS_WhlJerk{w.upper()}_mps3.json') for w in ['fl','fr']}
        scenes.append({'scene_id':entry['scene_id'],'scene':entry['scene'],'timeline':str(timeline),'jerk':jerk})
        for path in [str(timeline),*jerk.values()]:
            p=Path(path)
            files.append({'local':str(p.resolve(strict=True)),'source':path,'sha256':sha(p),'bytes':p.stat().st_size})
        info,raw=read_timeline(timeline,0)
        selected=latest_success_at_time(raw,0)
        if len(selected)<2:
            raise ValueError('fewer than two successful control times: '+entry['scene'])
        jerk_signals=[]
        for wheel,path in jerk.items():
            signal,samples=read_jerk(Path(path),wheel)
            if signal['backwards_timestamp_steps'] or signal['duplicate_timestamp_rows']:
                raise ValueError('jerk times must increase strictly: '+path)
            overlap=min(selected[-1]['timestamp_ns'],samples[-1]['timestamp_ns'])-max(selected[0]['timestamp_ns'],samples[0]['timestamp_ns'])
            if overlap<=0:
                raise ValueError('no overlap between control distance and jerk: '+path)
            jerk_signals.append({'name':f's_InDatCalcFAS_WhlJerk{wheel.upper()}_mps3','samples':signal['samples'],'unit':'m/s^3','overlap_ns':overlap})
        signals.append({'scene_id':entry['scene_id'],'timeline_columns':info['columns'],
                        'successful_control_times':len(selected),'invalid_rows':info['invalid_rows'],'jerk':jerk_signals})
    if not scenes or len({s['scene_id'] for s in scenes})!=len(scenes) or len({s['scene'] for s in scenes})!=len(scenes):
        raise ValueError('missing or duplicate task scenes')
    source={'run_id':run,'product_identity':index['product_identity'],'truth_root':str(truth_root),'upstream':evidence}
    verified=verify_upstream(source,[Path(s['timeline']).parent for s in scenes],
                             {Path(f['local']):f for f in files},Path,load,sha,scene_sources=scenes)
    output=bench_root/'tasks'/task_id/'evaluations/arrival-accuracy'/attempt_id
    if output.exists():
        raise ValueError('attempt already exists; use a new attempt ID')
    runtime_info=runtime()
    code={str(p.relative_to(ROOT)):sha(p) for p in ROOT.glob('*.py')}
    code['vendor/arrival-core/speedbump_accuracy.py']=CORE_SHA
    return {'schema_version':1,'state':'PLAN_READY','enabled':True,'operation':'arrival_accuracy',
            'engine':'bench_csv_trial_v1','native_plugin_equivalence':False,'task_id':task_id,'run_id':run,
            'product_identity':index['product_identity'],'truth_root':str(truth_root),'scenes':scenes,'files':files,
            'signals':signals,'selected_modes':['bench_csv_trial_v1'],'native_modes':[],
            'dit':'not available in this batch','startdist':'shared dual-wheel gate enabled',
            'unavailable':['native signal-family classification','MDF remark DIT label','geometry width/height','post-compensation comparison','calibrated XCP timing offset'],
            'scene_count':len(scenes),'input_root':index['canonical_product'],'upstream':verified,
            'computation_identity':code,'runtime':runtime_info,'output':str(output),'required_formats':['analysis_plan.json','arrival-accuracy.xlsx'],
            'rules':{'time':'payload_ctrl_ts_ms','distance':'fl/fr_dist_out_mm_0','duplicate':'maximum successful publish_seq, whole message',
                     'truth':'independent jerk + speed','gate':'both StartDist[0] == 1, previous hold at each contact',
                     'population':'finite matched gated errors within ±500 mm; flags retained',
                     'statistics':'pooled absolute P90/P95 linear; signed mean and sample SD/variance ddof=1',
                     'extra_compensation':False},
            'dependency':'delivered task prediction timeline + same-scene XCP; no trajectory/vision report or new replay',
            'execution_location':str(ROOT),'raw_inputs_copied':False,'mutations_performed':False}


def verify_snapshot(p):
    references=p['files']+list(p['upstream']['evidence'].values())
    for f in references:
        path=f.get('local') or f['path']
        if sha(path)!=f['sha256']:
            raise ValueError('input/evidence changed: '+path)
    for path,digest in p['computation_identity'].items():
        if sha(ROOT/path)!=digest:
            raise ValueError('calculation code changed after planning')


def execute(p):
    from batch_arrival_accuracy import run_batch
    from render_arrival_workbook import render
    verify_snapshot(p)
    output=Path(p['output']); output.mkdir(parents=True,exist_ok=False)
    write(output/'analysis_plan.json',p)
    receipt={'run_id':p['run_id'],'product_identity':p['product_identity'],'files':p['files']}
    write(output/'input-receipt.json',receipt)
    attempt={'state':'RUNNING','task_id':p['task_id'],'run_id':p['run_id'],'output':str(output)}
    write(output/'attempt.json',attempt)
    try:
        result=run_batch(Path(p['input_root']),CORE,output/'calculation',output/'input-receipt.json',
                         scene_sources=p['scenes'],arrays_only=True)
        if result['state']!='CALCULATED':
            raise ValueError('one or more scenes failed; inspect calculation/batch-result.json')
        result['execution_context']={'computation_identity':p['computation_identity'],'upstream':p['upstream'],
                                     'execution_location':p['execution_location'],'raw_inputs_copied':False}
        write(output/'batch-result.json',result)
        validation=render(result,output)
        write(output/'workbook-validation.json',validation)
        verify_snapshot(p)
        artifacts={n:{'sha256':sha(output/n),'bytes':(output/n).stat().st_size} for n in ['analysis_plan.json','arrival-accuracy.xlsx','batch-result.json','input-receipt.json','workbook-validation.json']}
        attempt.update(state='SUCCEEDED',artifacts=artifacts,scene_count=result['scene_count'],overall=result['overall'])
        write(output/'attempt.json',attempt)
        return attempt
    except Exception as exc:
        attempt.update(state='FAILED',error=str(exc))
        write(output/'attempt.json',attempt)
        raise


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enabled',action='store_true')
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--check-runtime',action='store_true')
    parser.add_argument('--task-id')
    parser.add_argument('--truth-root',type=Path)
    parser.add_argument('--bench-root',type=Path,default=Path('/mnt/data/bench-eval'))
    parser.add_argument('--attempt-id',default=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'z')
    args=parser.parse_args(argv)
    if not args.enabled:
        print(json.dumps({'state':'NOT_REQUESTED','enabled':False,'mutations_performed':False})); return 0
    try:
        if args.check_runtime:
            result=runtime()
            if args.truth_root and not args.truth_root.is_dir():
                raise ValueError('truth root is missing')
        else:
            if not args.task_id or args.truth_root is None:
                raise ValueError('--task-id and --truth-root are required when enabled')
            p=plan(args.task_id,args.truth_root,args.bench_root,args.attempt_id)
            result=execute(p) if args.execute else p
        print(json.dumps(result,ensure_ascii=False,indent=2)); return 0
    except Exception as exc:
        print(json.dumps({'state':'FAILED' if args.execute else 'BLOCKED','error':str(exc)},ensure_ascii=False)); return 2


if __name__=='__main__':
    raise SystemExit(main())
