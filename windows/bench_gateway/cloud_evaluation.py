"""Fixed native evaluator in an authorized mounted-volume attempt, with no data hashing."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import time

SCENE = re.compile(r'20\d{2}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_[A-Za-z0-9]+\Z')


def inside(path, root):
    path, root = Path(path).resolve(strict=True), Path(root).resolve(strict=True)
    if path != root and root not in path.parents:
        raise ValueError('path outside authorized root')
    return path


def resolve_scenes(roots, scenes):
    if not scenes or len(scenes) != len(set(scenes)) or not all(SCENE.fullmatch(s) for s in scenes):
        raise ValueError('exact unique scene identifiers required')
    candidates = {s: [] for s in scenes}
    for source in roots:
        root = Path(source).resolve(strict=True)
        parents = [root]
        children = list(root.iterdir())
        if len(children) > 2000:
            raise ValueError('GT root listing limit exceeded; provide a narrower root')
        parents += [inside(p, root) for p in children if p.is_dir() and re.fullmatch(r'20\d{2}-\d{2}-\d{2}', p.name)]
        for parent in parents:
            for scene in scenes:
                p = parent / scene / 'dataset'
                if p.is_dir():
                    resolved = inside(p, root)
                    if str(resolved) not in candidates[scene]:
                        candidates[scene].append(str(resolved))
    invalid = {s: paths for s, paths in candidates.items() if len(paths) != 1}
    if invalid:
        raise ValueError('GT missing or ambiguous: '+json.dumps(invalid))
    return {s: paths[0] for s, paths in candidates.items()}


def extract(archive, destination, maximum=2*1024**3):
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        if len(members) > 20000 or sum(m.size for m in members) > maximum:
            raise ValueError('archive exceeds bounded task size')
        names = set()
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or '\\' in member.name or not member.isfile() or member.name in names:
                raise ValueError('archive must contain unique regular relative files')
            names.add(member.name)
        tar.extractall(destination, members=members, filter='data')


def prepare_views(matches, predictions, output, expected):
    view = Path(output)
    (view/'truth').mkdir(parents=True)
    (view/'predictions').mkdir()
    counts = {}
    for scene, truth in matches.items():
        truth = Path(truth)
        frames = [p for p in truth.iterdir() if p.is_dir()]
        if not frames or expected is not None and len(frames) != expected[scene]:
            raise ValueError('GT frame count differs from source task: '+scene)
        target = view/'truth'/scene/'dataset'
        target.mkdir(parents=True)
        timestamps = set()
        for frame in frames:
            inside(frame, truth)
            dense = list(frame.glob('dense_labeled_*.npy'))
            if len(dense) != 1:
                raise ValueError('one dense GT required per frame: '+str(frame))
            inside(dense[0], truth)
            match = re.search(r'_frame_(\d{16})_', dense[0].name)
            if not match or not re.fullmatch(r'[A-Za-z0-9._-]+', frame.name):
                raise ValueError('unsupported GT timestamp layout')
            stamp = str(int(match[1])//1000)
            if stamp in timestamps:
                raise ValueError('duplicate GT timestamp')
            timestamps.add(stamp)
            (target/(frame.name.split('_')[0]+'_'+stamp)).symlink_to(frame, target_is_directory=True)
        prediction = inside(Path(predictions)/scene, predictions)
        if not prediction.is_dir():
            raise ValueError('prediction scene missing')
        (view/'predictions'/scene).symlink_to(prediction, target_is_directory=True)
        counts[scene] = len(frames)
    return view, counts


def run(root, runtime_root=None, support_root=None, runtime_profile=None):
    import resource
    root = Path(root).resolve(strict=True)
    allowed = Path(os.environ['BENCH_EVAL_RUN_ROOT']).resolve(strict=True)
    inside(root, allowed)
    request = json.loads((root/'request.json').read_text())
    if root.name != request['run_id'] or not re.fullmatch(r'cloud-eval-\d{8}-[a-f0-9]{12}', root.name):
        raise ValueError('attempt identity mismatch')
    attempt = root/'attempt'
    if (attempt/'receipt.json').is_file():
        return json.loads((attempt/'receipt.json').read_text())
    attempt.mkdir(mode=0o700)  # An incomplete existing attempt must never be rerun.
    start = time.monotonic()
    state = lambda value: (root/'state.json').write_text(json.dumps(value, ensure_ascii=False, indent=2))
    try:
        state({'state': 'PREPARING', 'run_id': request['run_id']})
        extract(root/'predictions.tar.gz', attempt/'prediction-bundle')
        if runtime_root is None:
            extract(root/'evaluator.tar.gz', attempt/'runtime', maximum=128*1024**2)
            runtime = attempt/'runtime'
        else:
            runtime = Path(runtime_root).resolve(strict=True)
        for relative, sha in request['tool_hashes'].items():
            if hashlib.sha256(inside(runtime/relative, runtime).read_bytes()).hexdigest() != sha:
                raise ValueError('fixed evaluator tool changed: '+relative)
        for relative in ('run.sh', 'build/uroad_offline_eval'):
            (runtime/relative).chmod(0o755)
        try:from .cloud_truth import runtime_root as resolve_gt_root
        except ImportError:from cloud_truth import runtime_root as resolve_gt_root
        profile=runtime_profile or {}
        gt_root=resolve_gt_root(request,profile,os.environ.get('BENCH_EVAL_GT_ROOT'))
        matches = resolve_scenes([gt_root], request['scenes'])
        view, counts = prepare_views(matches, attempt/'prediction-bundle'/'predictions',
                                    attempt/'inputs', None if profile.get('gt_policy')=='task-dataset-v1' else request['truth_frames'])
        (root/'scene-matches.json').write_text(json.dumps(matches, ensure_ascii=False, indent=2))
        text = (runtime/'etc/evaluation_param.yaml').read_text()
        changes = {'labeled_data_root': str(view/'truth'), 'pred_data_root': str(view/'predictions'),
                   'scene_folder_prefix': '', 'max_scenes_for_debug': 0}
        for key, value in changes.items():
            text, n = re.subn(r'(?m)^(\s*'+key+r':\s*).*$', lambda m: m[1]+json.dumps(value), text)
            if n != 1:
                raise ValueError('native config path key is ambiguous')
        config = attempt/'evaluation_param.yaml'
        config.write_text(text)
        config_sha = hashlib.sha256(config.read_bytes()).hexdigest()
        env = dict(os.environ)
        env['LD_LIBRARY_PATH'] = str(runtime/'runtime-libs')+(':'+env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
        env.update(OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
        libraries = subprocess.run(['ldd', str(runtime/'build/uroad_offline_eval')], capture_output=True, text=True, env=env)
        (attempt/'runtime-libraries.txt').write_text(libraries.stdout+libraries.stderr)
        if libraries.returncode or 'not found' in libraries.stdout+libraries.stderr:
            raise ValueError('native runtime library unavailable')
        prepared = time.monotonic()-start
        state({'state': 'EVALUATING', 'run_id': request['run_id'], 'scene_count': len(matches)})
        native_start = time.monotonic()
        with (attempt/'stdout.log').open('xb') as out, (attempt/'stderr.log').open('xb') as err:
            process = subprocess.run([str(runtime/'run.sh'), str(config)], cwd=attempt, env=env,
                                     stdout=out, stderr=err, stdin=subprocess.DEVNULL, timeout=2400)
        native_seconds = time.monotonic()-native_start
        (attempt/'exit-code.txt').write_text(str(process.returncode))
        if process.returncode:
            raise ValueError('native evaluator failed; preserved stdout/stderr must be inspected')
        reports = list((attempt/'eval_report').glob('*.txt'))
        if len(reports) != 1 or not reports[0].stat().st_size:
            raise ValueError('one nonempty original TXT report required')
        import sys
        sys.path.insert(0, str(Path(support_root).resolve(strict=True) if support_root else root))
        from report_scope import parse_scene_coverage
        from parse_eval_report import parse_report
        report = reports[0]
        coverage = parse_scene_coverage(report.read_text(), request['scenes'])
        if any(row['truth_frames'] != counts[row['scene']] for row in coverage['scenes']):
            raise ValueError('native TXT denominators differ from selected GT')
        parsed = parse_report(report)
        if hashlib.sha256(config.read_bytes()).hexdigest() != config_sha:
            raise ValueError('frozen task config changed')
        partial = (set(request['required_tasks'])-set(parsed['evaluated_tasks']) or
                   any(row['completed_frames'] != row['truth_frames'] for row in coverage['scenes']))
        receipt = {'state': 'PARTIAL_SUCCESS' if partial else 'SUCCEEDED', 'run_id': request['run_id'],
                   'source_run': request['source_run'], 'owner': request['owner'],
                   'report_path': str(report), 'report_sha256': hashlib.sha256(report.read_bytes()).hexdigest(),
                   'metrics': parsed['metrics'], 'evaluated_tasks': parsed['evaluated_tasks'],
                   'scene_coverage': coverage['scenes'], 'scene_matches': matches,
                   'source_data_hash_passes': 0, 'input_content_verified': False,
                   'preparation_seconds': prepared, 'native_seconds': native_seconds,
                   'total_seconds': time.monotonic()-start,
                   'native_max_rss_kib': resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
                   'native_cpu_seconds': resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime,
                   'required_tasks': request['required_tasks'], 'config_changes': changes}
        if profile.get('gt_policy')=='task-dataset-v1':receipt.update(gt_binding=request['gt_binding'],gt_frames=counts)
        (attempt/'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
        state({'state': receipt['state'], 'run_id': request['run_id'], 'report_path': str(report)})
        return receipt
    except BaseException as error:
        state({'state': 'FAILED', 'run_id': request['run_id'], 'error': str(error)})
        raise
