"""Run the pinned evaluator without scanning/hashing prediction or GT contents.

Kept in the gateway; shared workstation scripts and the evaluator are unchanged.
Paths and report coverage are checked, not dataset content immutability.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

POLICY = 'paths-and-report-v1'


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def make_views(worker, plan, root):
    """Normalize legacy numeric GT frame names using filenames, not NPY reads."""
    view = root / 'inputs'
    (view / 'predictions').mkdir(parents=True)
    (view / 'truth').mkdir()
    records = []
    expected = {r['scene']: r['truth_frames'] for r in plan['scene_inputs']}
    for index, scene in enumerate(plan['scenes'], 1):
        truth = worker.inside(Path(plan['truth_root']) / scene / 'dataset', plan['truth_root'])
        frames = sorted(p for p in truth.iterdir() if p.is_dir())
        worker.require(len(frames) == expected[scene], 'truth frame count changed: ' + scene)
        target = view / 'truth' / scene / 'dataset'
        target.mkdir(parents=True)
        names = set()
        for frame in frames:
            worker.inside(frame, truth)
            worker.require(worker.SAFE.fullmatch(frame.name), 'unsafe truth frame name')
            dense = list(frame.glob('dense_labeled_*.npy'))
            worker.require(len(dense) == 1 and dense[0].is_file(), 'one dense truth file required: ' + str(frame))
            worker.inside(dense[0], truth)
            match = re.search(r'_frame_(\d{16})_', dense[0].name)
            worker.require(match is not None, 'unsupported truth timestamp filename')
            name = frame.name.split('_')[0] + '_' + str(int(match.group(1)) // 1000)
            worker.require(name.split('_', 1)[1] not in names, 'duplicate truth timestamp')
            names.add(name.split('_', 1)[1])
            (target / name).symlink_to(frame, target_is_directory=True)
        prediction = worker.inside(Path(plan['prediction_path']) / scene, plan['prediction_path'])
        worker.require(prediction.is_dir(), 'prediction scene missing: ' + scene)
        (view / 'predictions' / scene).symlink_to(prediction, target_is_directory=True)
        records.append({'scene': scene, 'truth_frames': len(frames), 'truth_path': str(truth)})
        worker.emit('prepared', index=index, total=plan['scene_count'], scene=scene)
    return view, records


def execute(worker, original_collect, request, pin, plan):
    worker.require(worker.SAFE.fullmatch(request.get('run_id', '')), 'safe run ID required')
    root = worker.inside(Path(pin['runs_root']) / request['run_id'], pin['runs_root'])
    if root.exists():
        if (root / 'directory-receipt.json').is_file():
            return original_collect(request, pin, plan, recheck=False)
        raise ValueError('run exists without completed receipt; collect/inspect only, never overwrite or rerun')
    root.mkdir(mode=0o700)
    started = time.monotonic()
    worker.write(root / 'run-state.json', {'state': 'PREPARING', 'request': request, 'input_verification': POLICY})
    try:
        package, _ = worker.verify_pin(pin)
        for path in (package / 'run.sh', package / 'build/uroad_offline_eval'):
            worker.require(os.access(path, os.X_OK), 'evaluator is not executable')
        view, records = make_views(worker, plan, root)
        text = (package / 'etc/evaluation_param.yaml').read_text(encoding='utf-8')
        changes = {'labeled_data_root': str(view / 'truth'), 'pred_data_root': str(view / 'predictions'),
                   'scene_folder_prefix': '', 'max_scenes_for_debug': 0}
        for key, value in changes.items():
            text, count = re.subn(r'(?m)^(\s*' + key + r':\s*).*$',
                                 lambda m: m.group(1) + json.dumps(value), text)
            worker.require(count == 1, 'ambiguous native configuration field: ' + key)
        config = root / 'evaluation_param.yaml'
        config.write_text(text, encoding='utf-8')
        config_hash = worker.sha(config)
        prepared_seconds = time.monotonic() - started
        worker.write(root / 'run-state.json', {'state': 'EVALUATING', 'request': request,
                     'scene_count': plan['scene_count'], 'input_verification': POLICY})
        worker.emit('evaluating', total=plan['scene_count'], max_scenes_for_debug=0)

        def limits():
            import resource
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            os.nice(10)

        native_started = time.monotonic()
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
        native_seconds = time.monotonic() - native_started
        worker.require(code == 0, 'native evaluator failed; inspect preserved logs and exit_code')
        report = worker.native_report(root)
        scope = worker.parse_scene_coverage(report.read_text(encoding='utf-8'), plan['scenes'])
        expected = {r['scene']: r['truth_frames'] for r in records}
        worker.require(all(r['truth_frames'] == expected[r['scene']] for r in scope['scenes']),
                       'native report truth denominator differs from selected input')
        worker.require(worker.sha(config) == config_hash, 'batch configuration changed')
        receipt = {'run_id': request['run_id'], 'prediction_path': plan['prediction_path'],
            'scenes': plan['scenes'], 'scene_count': plan['scene_count'], 'discovery': plan,
            'scene_coverage': scope['scenes'], 'config_path': str(config), 'config_sha256': config_hash,
            'report_path': str(report), 'report_sha256': worker.sha(report),
            'evaluated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'dataset_identity': identity({'prepared_manifest_sha256': plan['prepared_manifest_sha256'], 'paths': records}),
            'dataset_identity_scope': 'manifest-and-paths',
            'package_manifest_sha256': pin['evaluator']['manifest_sha256'],
            'native_global_aggregation': True, 'config_changes': changes,
            'input_verification': POLICY, 'input_content_verified': False,
            'source_postchecks_passed': False, 'path_checks_passed': True,
            'timings': {'preparation_seconds': prepared_seconds, 'native_seconds': native_seconds,
                        'result_check_seconds': time.monotonic() - native_started - native_seconds,
                        'total_seconds': time.monotonic() - started},
            'source_data_hash_passes': 0}
        worker.write(root / 'directory-receipt.json', receipt)
        worker.write(root / 'run-state.json', {'state': 'EVALUATION_COMPLETED', 'scene_count': plan['scene_count'],
                     'input_verification': POLICY})
        return original_collect(request, pin, plan, recheck=False)
    except Exception as error:
        worker.write(root / 'run-state.json', {'state': 'FAILED', 'error': str(error),
                     'request': request, 'input_verification': POLICY})
        raise


def install(worker, use_direct):
    """One-time tool verification per SSH invocation; no repeated data scans."""
    original_verify, original_collect = worker.verify_pin, worker.collect
    verified = {}

    def verify_once(pin):
        key = identity({'worker': pin['worker'], 'evaluator': pin['evaluator']})
        if key not in verified:
            verified[key] = original_verify(pin)
        return verified[key]

    worker.verify_pin = verify_once
    # Collect only verifies the saved report/config and source metadata. It
    # never launches computation or rehashes GT, including for old receipts.
    worker.collect = lambda request, pin, plan, recheck=True: original_collect(request, pin, plan, recheck=False)
    if use_direct:
        worker.execute = lambda request, pin, plan: execute(worker, original_collect, request, pin, plan)
