"""Sent over SSH stdin. Reuses the pinned worker; never installs remote code."""
import argparse
import base64
import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from report_scope import parse_scene_coverage

SAFE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            digest.update(block)
    return digest.hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def inside(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    require(root in path.parents, 'path is outside the shared data root: ' + str(path))
    return path


def verify_pin(pin):
    package = Path(pin['evaluator']['root'])
    require(package.resolve() == package, 'pinned package was redirected')
    manifest_path = inside(package / pin['evaluator']['manifest_file'], package)
    require(sha(manifest_path) == pin['evaluator']['manifest_sha256'], 'shared package manifest pin mismatch')
    entries = read(manifest_path)['files']
    require(len(entries) == pin['evaluator']['file_count'], 'shared package file count mismatch')
    require(len({f['path'] for f in entries}) == len(entries), 'duplicate package entries')
    for item in entries:
        path = inside(package / item['path'], package)
        require(path.stat().st_size == item['size'] and sha(path) == item['sha256'],
                'shared package pin mismatch: ' + item['path'])
    for name, digest in pin['evaluator']['key_files'].items():
        require(sha(inside(package / name, package)) == digest, 'shared key file pin mismatch')
    worker = Path(pin['worker']['path'])
    require(worker.resolve() == worker and sha(worker) == pin['worker']['sha256'], 'shared worker pin mismatch')
    return package, worker


def task_switches(template):
    switches = {}
    for task, key in [('trajectory', 'eval_traj'), ('vision', 'eval_vision'), ('road', 'eval_road')]:
        values = re.findall(r'^\s*' + key + r':\s*(ON|OFF)\s*(?:#.*)?$', template, re.MULTILINE)
        require(len(values) == 1, 'ambiguous task switch: ' + key)
        switches[task] = values[0] == 'ON'
    require(not switches['road'], 'road evaluation is not supported by this verified entry')
    require(switches['trajectory'] or switches['vision'], 'no supported evaluation task enabled')
    return switches


def discover(request, pin):
    package, _ = verify_pin(pin)
    prediction = inside(request['prediction_path'], pin['prediction_root'])
    require(prediction.is_dir(), 'prediction directory does not exist')
    receipt_path = prediction.with_name(prediction.name + '.bench-eval-product.json')
    receipt = read(receipt_path)
    require(receipt.get('state') == 'SUCCEEDED' and receipt.get('product_type') == 'workstation_predictions',
            'successful workstation product receipt required')
    require(Path(receipt['workstation_product']).resolve() == prediction, 'receipt product path mismatch')
    producer = receipt['producer_run_id']
    require(isinstance(producer, str) and SAFE.fullmatch(producer), 'unsafe producer run ID')
    task = inside(Path(pin['discovery']['tasks_root']) / producer, pin['discovery']['tasks_root'])
    job_path = inside(task / 'inputs/job.json', task)
    job = read(job_path)
    require(job.get('run_id') == producer, 'producer Job and receipt mismatch')
    replay = job['replay_input']
    prepared = inside(replay['prepared_manifest'], pin['discovery']['prepared_manifest_root'])
    truth_root = inside(replay['dataset_root'], pin['discovery']['truth_root'])
    prepared_data = read(prepared)
    truth = {item['scene']: item for item in prepared_data['prepared_scenes']}
    require(len(truth) == len(prepared_data['prepared_scenes']), 'duplicate prepared scenes')
    all_scenes = sorted(p.name for p in prediction.iterdir() if p.is_dir())
    require(all_scenes and all(SAFE.fullmatch(s) for s in all_scenes), 'empty or unsafe scene directory')
    require({f['path'].split('/')[0] for f in receipt['files']} == set(all_scenes),
            'directory scene inventory differs from receipt')
    selected = request.get('scenes')
    if selected is None:
        scenes = all_scenes
    else:
        require(isinstance(selected, list) and selected and len(set(selected)) == len(selected), 'invalid explicit scene selection')
        require(set(selected) <= set(all_scenes), 'selected scene absent from product')
        scenes = sorted(selected)
    rows = []
    for scene in scenes:
        require(scene in truth, 'scene missing from prepared truth manifest: ' + scene)
        item = truth[scene]
        expected = inside(truth_root / scene / 'dataset', truth_root)
        require(Path(item['truth_root']).resolve() == expected and expected.is_dir(), 'truth path mismatch: ' + scene)
        frames = [p for p in expected.iterdir() if p.is_dir()]
        require(len(frames) == item['truth_frames'] and frames, 'truth frame count mismatch: ' + scene)
        for frame in frames:
            inside(frame, expected)
            for path in frame.iterdir():
                if path.is_file():
                    inside(path, expected)
        rows.append({'scene': scene, 'truth_frames': len(frames),
                     'vision_gt_json_files': sum(1 for _ in expected.glob('*/*.json'))})
    switches = task_switches((package / 'etc/evaluation_param.yaml').read_text(encoding='utf-8'))
    required = request.get('required_tasks')
    if required is None:
        required = [k for k in ('trajectory', 'vision') if switches[k]]
    require(isinstance(required, list) and required and set(required) <= {'trajectory', 'vision'}, 'invalid requested tasks')
    require(all(switches[k] for k in required), 'requested task is disabled in pinned configuration')
    provenance_path = task / 'inputs/provenance.json'
    provenance = read(provenance_path) if provenance_path.is_file() else {}
    return {'prediction_path': str(prediction), 'producer_run_id': producer,
            'all_scenes': all_scenes, 'scenes': scenes, 'scene_count': len(scenes),
            'selection': 'all' if selected is None else 'explicit_subset', 'scene_inputs': rows,
            'truth_root': str(truth_root), 'prepared_manifest': str(prepared),
            'prediction_receipt_sha256': sha(receipt_path), 'prepared_manifest_sha256': sha(prepared),
            'job_path': str(job_path), 'job_sha256': sha(job_path),
            'app_artifact': job.get('app_artifact', {}), 'library_provenance': provenance,
            'task_switches': switches, 'required_tasks': required,
            'warnings': ['VISION_GT_MISSING'] if switches['vision'] and any(not r['vision_gt_json_files'] for r in rows) else []}


def emit(phase, **value):
    print(json.dumps({'type': 'progress', 'phase': phase, **value}), flush=True)


def write(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def load_worker(pin):
    _, path = verify_pin(pin)
    spec = importlib.util.spec_from_file_location('pinned_worker', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scene_args(pin, plan, root, index, scene):
    return argparse.Namespace(config=str(root / 'scenes' / ('s%03d' % index) / 'evaluation_param.yaml'),
        config_root=pin['runs_root'], prediction=plan['prediction_path'], prediction_root=pin['prediction_root'],
        truth_root=plan['truth_root'], scene=scene, run_id=root.name + '-s%03d' % index,
        package=pin['evaluator']['root'], prepared_manifest=plan['prepared_manifest'])


def native_report(root):
    require((root / 'exit_code.txt').read_text().strip() == '0', 'native evaluation did not succeed')
    reports = list((root / 'eval_report').glob('eval_report_*.txt'))
    require(len(reports) == 1 and reports[0].stat().st_size > 0, 'expected one nonempty native report')
    return reports[0]


def collect(request, pin, plan, recheck=True):
    require(SAFE.fullmatch(request.get('run_id', '')), 'safe run ID required')
    root = inside(Path(pin['runs_root']) / request['run_id'], pin['runs_root'])
    receipt = read(root / 'directory-receipt.json')
    require(receipt['prediction_path'] == plan['prediction_path'] and receipt['scenes'] == plan['scenes'],
            'existing run uses different data or scene scope')
    require(receipt['discovery']['required_tasks'] == plan['required_tasks'], 'requested task scope changed')
    for key in ('prediction_receipt_sha256', 'prepared_manifest_sha256', 'job_sha256'):
        require(receipt['discovery'][key] == plan[key], 'source provenance changed: ' + key)
    report = native_report(root)
    require(sha(report) == receipt['report_sha256'] and sha(root / 'evaluation_param.yaml') == receipt['config_sha256'],
            'existing evaluation artifacts changed')
    scope = parse_scene_coverage(report.read_text(encoding='utf-8'), plan['scenes'])
    if recheck:
        worker = load_worker(pin)
        for index, scene in enumerate(plan['scenes'], 1):
            worker.precheck(scene_args(pin, plan, root, index, scene))
            emit('reuse_prechecked', index=index, total=plan['scene_count'], scene=scene)
    return {'type': 'result', 'state': 'EVALUATION_COMPLETED', 'receipt': receipt,
            'scene_coverage': scope['scenes'], 'report_base64': base64.b64encode(report.read_bytes()).decode('ascii')}


def execute(request, pin, plan):
    require(SAFE.fullmatch(request.get('run_id', '')), 'safe run ID required')
    root = inside(Path(pin['runs_root']) / request['run_id'], pin['runs_root'])
    if root.exists():
        if (root / 'directory-receipt.json').is_file():
            return collect(request, pin, plan)
        raise ValueError('run already exists without completed receipt; inspect run-state.json; do not rerun or overwrite')
    require(len(root.name) < 110, 'run ID too long for scene attempts')
    root.mkdir()
    write(root / 'run-state.json', {'state': 'PREPARING', 'request': request})
    try:
        worker = load_worker(pin)
        args_list, evidence = [], []
        for index, scene in enumerate(plan['scenes'], 1):
            args = scene_args(pin, plan, root, index, scene)
            with contextlib.redirect_stdout(io.StringIO()):
                worker.prepare(args)
            args_list.append(args)
            item = read(Path(args.config).parent / 'prepared-inputs.json')
            evidence.append({'scene': scene, 'input_identity': item['input_identity'],
                             'truth_frames': len(item['frames'])})
            emit('prepared', index=index, total=plan['scene_count'], scene=scene)
        view = root / 'inputs'
        (view / 'predictions').mkdir(parents=True)
        (view / 'truth').mkdir()
        for args in args_list:
            (view / 'predictions' / args.scene).symlink_to(Path(plan['prediction_path']) / args.scene, target_is_directory=True)
            (view / 'truth' / args.scene).symlink_to(Path(args.config).parent / 'inputs/truth' / args.scene, target_is_directory=True)
        package = Path(pin['evaluator']['root'])
        text = (package / 'etc/evaluation_param.yaml').read_text(encoding='utf-8')
        changes = {'labeled_data_root': str(view / 'truth'), 'pred_data_root': str(view / 'predictions'),
                   'scene_folder_prefix': '', 'max_scenes_for_debug': 0}
        for key, value in changes.items():
            text, count = re.subn(r'(?m)^(\s*' + key + r':\s*).*$', lambda m: m.group(1) + json.dumps(value), text)
            require(count == 1, 'ambiguous native configuration field: ' + key)
        config = root / 'evaluation_param.yaml'
        config.write_text(text, encoding='utf-8')
        config_hash = sha(config)
        for index, args in enumerate(args_list, 1):
            worker.precheck(args)
            emit('prechecked', index=index, total=plan['scene_count'], scene=args.scene)
        verify_pin(pin)
        write(root / 'run-state.json', {'state': 'EVALUATING', 'request': request, 'scene_count': plan['scene_count']})
        emit('evaluating', total=plan['scene_count'], max_scenes_for_debug=0)
        def limits():
            import resource
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            os.nice(10)
        code = 124
        try:
            with (root / 'stdout.log').open('xb') as out, (root / 'stderr.log').open('xb') as err:
                result = subprocess.run([str(package / 'run.sh'), str(config)], cwd=root,
                    stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                    timeout=max(180, min(7200, 180 * plan['scene_count'])), preexec_fn=limits,
                    env=dict(os.environ, OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1'))
                code = result.returncode
        finally:
            (root / 'exit_code.txt').write_text(str(code) + '\n')
        require(code == 0, 'native evaluator failed; inspect preserved stdout/stderr and exit_code')
        evaluated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        report = native_report(root)
        scope = parse_scene_coverage(report.read_text(encoding='utf-8'), plan['scenes'])
        expected_frames = {e['scene']: e['truth_frames'] for e in evidence}
        require(all(r['truth_frames'] == expected_frames[r['scene']] for r in scope['scenes']),
                'native report truth denominator differs from prepared inputs')
        for index, args in enumerate(args_list, 1):
            worker.precheck(args)
            emit('postchecked', index=index, total=plan['scene_count'], scene=args.scene)
        require(sha(config) == config_hash, 'batch configuration changed')
        verify_pin(pin)
        receipt = {'run_id': request['run_id'], 'prediction_path': plan['prediction_path'],
            'scenes': plan['scenes'], 'scene_count': plan['scene_count'], 'discovery': plan,
            'scene_coverage': scope['scenes'], 'config_path': str(config), 'config_sha256': config_hash,
            'report_path': str(report), 'report_sha256': sha(report), 'evaluated_at': evaluated_at,
            'dataset_identity': hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest(),
            'package_manifest_sha256': pin['evaluator']['manifest_sha256'],
            'native_global_aggregation': True, 'config_changes': changes, 'source_postchecks_passed': True}
        write(root / 'directory-receipt.json', receipt)
        write(root / 'run-state.json', {'state': 'EVALUATION_COMPLETED', 'scene_count': plan['scene_count']})
        return collect(request, pin, plan, recheck=False)
    except Exception as exc:
        write(root / 'run-state.json', {'state': 'FAILED', 'error': str(exc), 'request': request})
        raise


def main(request, pin):
    try:
        plan = discover(request, pin)
        if request['action'] == 'discover':
            result = {'type': 'result', 'state': 'DIRECTORY_DISCOVERED', 'discovery': plan, 'mutations_performed': False}
        elif request['action'] == 'execute':
            result = execute(request, pin, plan)
        elif request['action'] == 'collect':
            result = collect(request, pin, plan)
        else:
            raise ValueError('unknown directory action')
        print(json.dumps(result, ensure_ascii=False), flush=True)
    except Exception as exc:
        print(json.dumps({'type': 'error', 'state': 'DIRECTORY_EVALUATION_BLOCKED', 'message': str(exc)}, ensure_ascii=False), flush=True)
        raise SystemExit(2)
